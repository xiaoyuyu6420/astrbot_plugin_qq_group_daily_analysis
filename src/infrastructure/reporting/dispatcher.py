import asyncio
import base64
import os
import tempfile
from collections.abc import Callable
from typing import Any

from ...shared.constants import PLUGIN_NAME
from ...shared.timezone import now as _tz_now
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ..utils.admin_resolver import resolve_admin_qqs


class ReportDispatcher:
    """
    报告分发器
    负责协调报告生成、格式选择、消息发送和失败重试
    """

    def __init__(
        self,
        config_manager,
        report_generator,
        message_sender,
    ):
        self.config_manager = config_manager
        self.report_generator = report_generator
        self.message_sender = message_sender
        self._html_render_func: Callable | None = None

    def set_html_render(self, render_func: Callable):
        """设置 HTML 渲染函数 (运行时注入)"""
        self._html_render_func = render_func

    async def dispatch(
        self,
        group_id: str,
        analysis_result: dict[str, Any],
        platform_id: str | None = None,
    ):
        """
        分发分析报告
        """
        trace_id = TraceContext.get()

        # 【定制】管理员私聊通知模式：定时报告发管理员私聊，不发群
        if self.config_manager.is_admin_notify_enabled():
            logger.info(f"[{trace_id}] 管理员私聊通知已开启，报告将私聊发给管理员 (源群: {group_id})")
            await self._dispatch_to_admins(group_id, analysis_result, platform_id)
            return

        output_format = self.config_manager.get_output_format()

        logger.info(
            f"[{trace_id}] 正在分发群 {group_id} 的报告 (格式: {output_format})"
        )

        success = False
        if output_format == "image":
            success = await self._dispatch_image(group_id, analysis_result, platform_id)
        elif output_format == "html":
            success = await self._dispatch_html(group_id, analysis_result, platform_id)
        else:
            success = await self._dispatch_text(group_id, analysis_result, platform_id)

        if success:
            logger.info(f"[{trace_id}] 群 {group_id} 的报告分发成功")
        else:
            logger.warning(f"[{trace_id}] 群 {group_id} 的报告分发失败")

    def _make_avatar_url_getter(self, platform_id: str | None):
        """构造头像获取回调（请求小尺寸头像以优化性能）。

        抽出 _dispatch_image / _dispatch_to_admins 两处重复的闭包，供重试逻辑复用。
        """

        async def avatar_url_getter(user_id: str):
            if not platform_id:
                return None
            adapter = self.message_sender.bot_manager.get_adapter(platform_id)
            if adapter and hasattr(adapter, "get_user_avatar_url"):
                return await adapter.get_user_avatar_url(user_id, size=40)
            return None

        return avatar_url_getter

    async def _render_image_with_retry(
        self,
        analysis_result: dict[str, Any],
        group_id: str,
        platform_id: str | None,
    ) -> tuple[str | None, str | None]:
        """生成图片报告，失败时按配置进行整体重试。

        generate_image_report 内部已有"两轮策略"降级（png→jpeg），
        本方法在其全部失败后，等待 interval 秒再整体重跑，覆盖 T2I 端点
        瞬时故障（网络闪断、Chromium 重启、内存瞬时高峰）。

        顺序执行：第一次调用完全结束（含 finally 的 session 清理）后才重试，
        无并发/状态冲突。
        """
        trace_id = TraceContext.get()
        retry_count = max(0, self.config_manager.get_image_retry_count())
        interval = max(0, self.config_manager.get_image_retry_interval_seconds())

        avatar_url_getter = self._make_avatar_url_getter(platform_id)
        image_url: str | None = None
        html_content: str | None = None

        total_attempts = retry_count + 1
        for attempt in range(1, total_attempts + 1):
            try:
                image_url, html_content = (
                    await self.report_generator.generate_image_report(
                        analysis_result,
                        group_id,
                        self._html_render_func,
                        avatar_url_getter=avatar_url_getter,
                        avatar_cache_namespace=platform_id,
                    )
                )
            except Exception as e:
                logger.error(f"[{trace_id}] Failed to generate image report: {e}")
                image_url = None

            if image_url:
                if attempt > 1:
                    logger.info(
                        f"[{trace_id}] 图片重试第 {attempt - 1} 次成功，群 {group_id}"
                    )
                return image_url, html_content

            if attempt < total_attempts:
                logger.warning(
                    f"[{trace_id}] 图片渲染失败（第 {attempt}/{total_attempts} 次），"
                    f"{interval}s 后重试，群 {group_id}"
                )
                await asyncio.sleep(interval)

        logger.warning(
            f"[{trace_id}] 图片渲染全部 {total_attempts} 次尝试均失败，群 {group_id}"
        )
        return None, html_content

    async def _dispatch_image(
        self, group_id: str, analysis_result: dict[str, Any], platform_id: str | None
    ) -> bool:
        trace_id = TraceContext.get()
        # 1. 检查渲染函数
        if not self._html_render_func:
            logger.warning(f"[{trace_id}] 未设置 HTML 渲染函数，回退到文本模式。")
            return await self._dispatch_text(group_id, analysis_result, platform_id)

        # 2. 生成图片（含失败整体重试）
        image_url, html_content = await self._render_image_with_retry(
            analysis_result, group_id, platform_id
        )

        # 4. 发送图片
        sent = False
        if image_url:
            caption = TraceContext.make_report_caption()
            sent = await self.message_sender.send_image_smart(
                group_id, image_url, caption, platform_id
            )

            # 5. 尝试上传到群文件/群相册（静默处理）
            # 无论消息发送是否成功（如超时回退），只要图片生成了，就尝试备份到群文件
            await self._try_upload_image(group_id, image_url, platform_id)

        if sent:
            return True

        # 6. 最终回退：图片生成/发送均失败，发送降级文本（带醒目提示）
        logger.warning(
            f"[{trace_id}] Image dispatch failed, falling back to text report."
        )
        return await self._dispatch_text(
            group_id, analysis_result, platform_id, image_fallback=True
        )

    async def _dispatch_html(
        self, group_id: str, analysis_result: dict[str, Any], platform_id: str | None
    ) -> bool:
        trace_id = TraceContext.get()

        html_path = None
        try:

            async def avatar_url_getter(user_id: str):
                if not platform_id:
                    return None
                adapter = self.message_sender.bot_manager.get_adapter(platform_id)
                if adapter and hasattr(adapter, "get_user_avatar_url"):
                    return await adapter.get_user_avatar_url(user_id, size=40)
                return None

            html_path, json_path = await self.report_generator.generate_html_report(
                analysis_result,
                group_id,
                avatar_url_getter=avatar_url_getter,
                avatar_cache_namespace=platform_id,
            )
        except Exception as e:
            logger.error(f"[{trace_id}] Failed to generate HTML report: {e}")

        if html_path:
            is_only_url = self.config_manager.get_html_only_url()
            base_url = self.config_manager.get_html_base_url()

            if is_only_url:
                if base_url and base_url.strip():
                    # 获取配置的目录
                    html_output_dir = self.config_manager.get_html_output_dir()

                    # 若用户配置为空，使用默认目录
                    if not html_output_dir:
                        from astrbot.api.star import StarTools

                        html_output_dir = os.path.join(
                            StarTools.get_data_dir(PLUGIN_NAME),
                            "self_hosted_html_reports",
                        )

                    # 计算相对路径并转换为URL
                    rel_path = os.path.relpath(html_path, html_output_dir)
                    url_path = rel_path.replace(os.sep, "/")
                    report_url = f"{base_url.rstrip('/')}/{url_path.lstrip('/')}"

                    sent = await self.message_sender.send_text(
                        group_id,
                        f"📊 今日群聊分析报告已生成：\n{report_url}",
                        platform_id,
                    )

                    if sent:
                        return True
                else:
                    logger.warning(
                        f"[{trace_id}] 群 {group_id} 开启了仅发送外链，但未配置 html_base_url，已进行降级，回退至发送 HTML 文件。"
                    )

            caption = self.report_generator.build_html_caption(html_path)

            sent = await self.message_sender.send_file(
                group_id,
                html_path,
                caption=caption,
                platform_id=platform_id,
            )
            if sent:
                return True

        logger.warning(
            f"[{trace_id}] HTML dispatch failed, falling back to text report."
        )
        return await self._dispatch_text(group_id, analysis_result, platform_id)

    async def _dispatch_text(
        self,
        group_id: str,
        analysis_result: dict[str, Any],
        platform_id: str | None,
        image_fallback: bool = False,
    ) -> bool:
        """分发文本报告。

        Args:
            image_fallback: True 表示这是图片失败后的降级，文本顶部会加醒目提示。
        """
        logger.info(f"[分发器] 正在向群组 {group_id} 分发文本报告")
        text_report = self.report_generator.generate_text_report(
            analysis_result, image_fallback=image_fallback
        )
        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        # 尝试通过适配器发送文本报告
        logger.info(f"[分发器] 正在尝试通过适配器发送文本报告。群: {group_id}")
        try:
            if adapter and await adapter.send_text_report(group_id, text_report):
                return True
            return await self.message_sender.send_text(
                group_id, text_report, platform_id
            )
        except Exception as e:
            logger.error(f"[分发器] 发送文本报告最终失败。群: {group_id}, 错误: {e}")
            return False

    # ================================================================
    # 【定制】管理员私聊通知
    # ================================================================

    def _get_admin_qqs(self) -> list[str]:
        """合并 AstrBot 超级管理员 + 插件配置的额外管理员 QQ，过滤掉非数字项"""
        return resolve_admin_qqs(
            self.message_sender.bot_manager,
            self.config_manager.get_extra_admin_qqs(),
        )

    async def _dispatch_to_admins(
        self,
        group_id: str,
        analysis_result: dict[str, Any],
        platform_id: str | None,
    ) -> None:
        """把报告私聊发给所有管理员（图片优先，失败回退文本）。不发群。"""
        trace_id = TraceContext.get()
        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        if not adapter:
            logger.error(f"[{trace_id}] 管理员通知失败：无法获取 adapter (platform_id={platform_id})")
            return
        if not hasattr(adapter, "send_private"):
            logger.error(f"[{trace_id}] 管理员通知失败：当前平台 adapter 不支持私聊发送 (非 OneBot?)")
            return

        admin_qqs = self._get_admin_qqs()
        if not admin_qqs:
            logger.warning(
                f"[{trace_id}] 未找到有效的管理员 QQ（请在 AstrBot 通用设置 admins_id 填真实 QQ，或插件配置 extra_admin_qq）。报告未发出。"
            )
            return

        logger.info(f"[{trace_id}] 管理员私聊通知目标: {admin_qqs}")

        # 1. 生成图片报告（含失败整体重试；复用 _dispatch_image 的生成逻辑，但不发群）
        image_url: str | None = None
        if self._html_render_func:
            image_url, _ = await self._render_image_with_retry(
                analysis_result, group_id, platform_id
            )

        # 2. 准备文本兜底（私聊场景下，发文本即代表图片失败，带降级提示）
        text_report = self.report_generator.generate_text_report(
            analysis_result, image_fallback=not image_url
        )

        # 3. 逐个私聊发送（图片优先；图片失败则回退文本，保证情报及时送达）
        caption = TraceContext.make_report_caption()
        # 仅在没有降级提示时补群号标识（降级文本已含完整标题）
        if image_url:
            text_payload = f"📋 群聊情报日报（群 {group_id}）：\n\n{text_report}"
        else:
            text_payload = f"（群 {group_id}）\n{text_report}"
        success_count = 0
        for qq in admin_qqs:
            try:
                ok = False
                if image_url:
                    ok = await adapter.send_private(
                        user_id=qq, image_path=image_url, text=caption
                    )
                    if not ok:
                        logger.warning(
                            f"[{trace_id}] 私聊图片发送 {qq} 失败，回退文本"
                        )
                        ok = await adapter.send_private(
                            user_id=qq, text=text_payload
                        )
                else:
                    ok = await adapter.send_private(user_id=qq, text=text_payload)
                if ok:
                    success_count += 1
                    logger.info(f"[{trace_id}] 已私聊发送报告给 {qq}")
                else:
                    logger.warning(f"[{trace_id}] 私聊发送 {qq} 返回失败（可能是非好友）")
            except Exception as e:
                logger.error(f"[{trace_id}] 私聊发送 {qq} 异常: {e}")
                # 异常时再尝试纯文本，尽量保证推送不丢
                try:
                    if await adapter.send_private(user_id=qq, text=text_payload):
                        success_count += 1
                        logger.info(f"[{trace_id}] 异常后文本回退成功: {qq}")
                except Exception as e2:
                    logger.error(f"[{trace_id}] 文本回退也失败 {qq}: {e2}")

        logger.info(
            f"[{trace_id}] 管理员通知完成：成功 {success_count}/{len(admin_qqs)}"
        )

    # ================================================================
    # 图片报告上传到群文件 / 群相册（仅 QQ 平台 image 格式）
    # ================================================================

    async def _try_upload_image(
        self,
        group_id: str,
        image_url: str,
        platform_id: str | None,
    ):
        """
        尝试将图片报告上传到群文件和/或群相册。

        仅在配置启用且平台为 OneBot 时执行，失败静默处理。
        """
        enable_file = self.config_manager.get_enable_group_file_upload()
        enable_album = self.config_manager.get_enable_group_album_upload()
        if not enable_file and not enable_album:
            return

        # 仅 OneBot 平台支持
        adapter = self._get_onebot_adapter(platform_id)
        if not adapter:
            return

        # 将图片保存为临时文件
        image_file = self._save_image_to_temp(image_url, group_id)
        if not image_file:
            return

        try:
            # 上传到群文件
            if enable_file:
                await self._do_upload_group_file(adapter, group_id, image_file)

            # 上传到群相册
            if enable_album:
                await self._do_upload_group_album(adapter, group_id, image_file)
        finally:
            try:
                os.remove(image_file)
            except OSError:
                pass

    async def _do_upload_group_file(self, adapter, group_id: str, file_path: str):
        """上传文件到群文件目录，失败静默"""
        try:
            folder_name = self.config_manager.get_group_file_folder()
            folder_id = None
            if folder_name:
                folder_id = await adapter.find_or_create_folder(group_id, folder_name)
            await adapter.upload_group_file_to_folder(
                group_id=group_id,
                file_path=file_path,
                folder_id=folder_id,
            )
        except Exception as e:
            logger.warning(f"群文件上传失败 (群 {group_id}): {e}")

    async def _do_upload_group_album(self, adapter, group_id: str, file_path: str):
        """上传图片到群相册，失败静默"""
        try:
            album_name = self.config_manager.get_group_album_name()
            strict_mode = self.config_manager.get_group_album_strict_mode()
            album_id = None

            if hasattr(adapter, "find_album_id"):
                if album_name:
                    album_id = await adapter.find_album_id(group_id, album_name)
                    if not album_id and strict_mode:
                        logger.info(
                            f"群相册严格模式开启：在群 {group_id} 中未找到名为 '{album_name}' 的相册，停止上传。"
                        )
                        return
                elif strict_mode:
                    logger.info(
                        f"群相册严格模式开启：未设置目标相册名称，停止上传以防止操作群 {group_id} 的默认相册。"
                    )
                    return

            await adapter.upload_group_album(
                group_id,
                file_path,
                album_id=album_id,
                album_name=album_name,
                strict_mode=strict_mode,
            )
        except Exception as e:
            logger.warning(f"群相册上传失败 (群 {group_id}): {e}")

    def _save_image_to_temp(self, image_url: str, group_id: str) -> str | None:
        """将 base64 图片保存为临时 PNG 文件，返回路径。失败返回 None。"""
        try:
            image_data = None
            if image_url.startswith("base64://"):
                image_data = base64.b64decode(image_url[len("base64://") :])
            elif image_url.startswith("data:"):
                parts = image_url.split(",", 1)
                if len(parts) == 2:
                    image_data = base64.b64decode(parts[1])
            elif os.path.isfile(image_url):
                return os.path.abspath(image_url)
            elif image_url.startswith("file:///"):
                p = image_url[len("file:///") :]
                if os.path.isfile(p):
                    return os.path.abspath(p)

            if not image_data:
                return None

            date_str = _tz_now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(
                tempfile.gettempdir(), f"群聊分析报告_{group_id}_{date_str}.png"
            )
            with open(path, "wb") as f:
                f.write(image_data)
            return path
        except Exception as e:
            logger.debug(f"保存图片到临时文件失败: {e}")
            return None

    def _get_onebot_adapter(self, platform_id: str | None):
        """获取 OneBot 适配器，非 OneBot 平台返回 None。"""
        if not platform_id:
            return None
        adapter = self.message_sender.bot_manager.get_adapter(platform_id)
        if adapter and hasattr(adapter, "upload_group_file_to_folder"):
            return adapter
        return None
