"""
报告生成器模块
负责生成各种格式的分析报告
"""

import asyncio
import base64
import hashlib
import html
import os
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from urllib.parse import quote

import aiohttp
import ulid
from diskcache import Cache
from markupsafe import Markup

from ...domain.repositories.report_repository import IReportGenerator
from ...shared.timezone import now as _tz_now
from ...utils.logger import logger
from ..utils.template_utils import render_template
from ..visualization.activity_charts import ActivityVisualizer
from .templates import HTMLTemplates

MAX_CONCURRENT_DOWNLOADS = 10
AVATAR_CACHE_EXPIRE_TIME = 259200
TRANSPARENT_IMAGE_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxIiBoZWlnaHQ9IjEiPjwvc3ZnPg=="
)



class ReportGenerator(IReportGenerator):
    """报告生成器"""

    def __init__(self, config_manager, data_dir):
        self._avatar_session = None
        self.config_manager = config_manager
        self.data_dir = data_dir
        self.activity_visualizer = ActivityVisualizer()
        self.html_templates = HTMLTemplates(config_manager)  # 实例化HTML模板管理器
        # 全局 T2I 渲染信号量，保护本地资源
        # 使用专用的 T2I 并发配置项
        max_concurrent = self.config_manager.get_t2i_max_concurrent()
        self._render_semaphore = asyncio.Semaphore(max_concurrent)

        # 运行时缓存，用于在一次分析任务中避免重复下载同一个头像
        self._avatar_cache = Cache(
            str(self.data_dir / "avatar")
        )  # user_id -> base64_uri
        self._avatar_session_concurrent_semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_DOWNLOADS
        )
        self._avatar_session = None

    @staticmethod
    def _sanitize_path_component(name: str) -> str:
        """消毒单个路径/文件名片段，禁止路径穿越和非法字符。"""
        # 禁止空组件、相对路径控制符："."、".."
        if not name or name in {".", ".."}:
            raise ValueError(f"无效的路径片段: {name!r}")

        # 不允许包含路径分隔符
        name = name.replace("/", "_")
        name = name.replace("\\", "_")

        # 去除非打印字符和非法文件名字符
        name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)

        # 保留中文、字母、数字、下划线、横线和点
        name = name.strip()
        if not name:
            raise ValueError("路径片段经过消毒后为空")

        return name

    def _build_safe_report_path(
        self,
        output_dir: Path,
        filename_format: str,
        group_id: str,
        date: str,
    ) -> Path:
        """根据格式构建安全输出路径，支持子目录和 {ulid}。"""
        generated_ulid = str(ulid.new())
        safe_context = {
            "group_id": group_id,
            "date": date,
            "ulid": generated_ulid,
        }

        try:
            formatted = render_template(filename_format, strict=True, **safe_context)
        except Exception as e:
            raise ValueError(f"文件名模板渲染失败: {e}") from e

        if os.path.isabs(formatted):
            raise ValueError("文件名格式不得为绝对路径")

        relative_path = Path(formatted)
        sanitized_parts = []
        for part in relative_path.parts:
            if part in {".", ".."}:
                raise ValueError("路径中不得包含 '.' 或 '..'。")
            sanitized_parts.append(self._sanitize_path_component(part))

        safe_relative = Path(*sanitized_parts)

        output_dir_resolved = output_dir.resolve(strict=False)
        target_path = (output_dir_resolved / safe_relative).resolve(strict=False)

        # 防止回退到上级目录（使用 Path.relative_to 进行目录包含校验）
        try:
            target_path.relative_to(output_dir_resolved)
        except ValueError:
            raise ValueError("文件路径不在输出目录之内，可能包含路径穿越")

        # 防止与已有文件覆盖（如果用户格式没有唯一标记），追加 ULID 后缀
        if target_path.exists():
            suffix = target_path.suffix
            stem = target_path.stem
            target_path = target_path.with_name(f"{stem}_{generated_ulid}{suffix}")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path

    async def generate_image_report(
        self,
        analysis_result: dict,
        group_id: str,
        html_render_func,
        avatar_url_getter=None,
        nickname_getter=None,
        avatar_cache_namespace: str | None = None,
    ) -> tuple[str | None, str | None]:
        """
        生成图片格式的分析报告

        Args:
            analysis_result: 分析结果字典
            group_id: 群组ID
            html_render_func: HTML渲染函数
            avatar_url_getter: 异步回调函数，接收 user_id 返回 avatar_url/data
            nickname_getter: 昵称获取函数

        Returns:
            tuple[str | None, str | None]: (image_url, html_content)
        """
        html_content = None
        try:
            # 准备渲染数据
            render_payload = await self._prepare_render_data(
                analysis_result,
                chart_template="activity_chart.html",
                avatar_url_getter=avatar_url_getter,
                nickname_getter=nickname_getter,
                avatar_cache_namespace=avatar_cache_namespace,
            )

            # 先渲染HTML模板（使用 Jinja2 渲染器以支持逻辑标签）
            html_content = self.html_templates.render_template(
                "image_template.html", **render_payload
            )
            html_content = self._reuse_avatars_in_final_html(
                html_content,
                render_payload.get("avatar_reuse_registry", {}),
                render_payload.get("avatar_reuse_aliases", {}),
            )

            # 检查HTML内容是否有效
            if not html_content:
                logger.error(
                    f"[T2I] 群 {group_id} 图片报告 HTML 渲染失败：返回空内容"
                )
                return None, None

            html_chars = len(html_content)
            html_kb = html_chars / 1024
            font_source = self.config_manager.get_t2i_font_source()
            template_name = self.config_manager.get_report_template()
            logger.info(
                f"[T2I] 群 {group_id} HTML 准备完成: "
                f"模板={template_name}, 长度={html_chars} 字符 (~{html_kb:.1f} KB), "
                f"字体源={font_source}, "
                f"render_func={getattr(html_render_func, '__name__', type(html_render_func).__name__)}"
            )
            if html_kb > 1500:
                logger.warning(
                    f"[T2I] HTML 体积偏大 (~{html_kb:.1f} KB)，本地渲染容易超时/OOM。"
                    f" 建议：调大 t2i_r*_timeout、降低 device_scale、改用 jpeg，或换 simple 模板"
                )

            # 从配置中获取两轮渲染策略
            render_strategies = self.config_manager.get_t2i_rendering_strategies()

            # 使用信号量控制并发进入渲染引擎
            async with self._render_semaphore:
                logger.info(
                    f"[T2I] 群 {group_id} 进入渲染队列，共 {len(render_strategies)} 轮策略"
                )

                last_exception = None
                attempt_summaries: list[str] = []

                for attempt, image_options in enumerate(render_strategies, 1):
                    # 拷贝一份，避免 pop quality 污染后续轮次
                    options = dict(image_options)
                    if options.get("type") == "png":
                        options.pop("quality", None)

                    timeout_ms = options.get("timeout", "?")
                    img_type = options.get("type", "?")
                    scale = options.get("device_scale_factor_level", "?")
                    logger.info(
                        f"[T2I] 群 {group_id} 第 {attempt}/{len(render_strategies)} 轮: "
                        f"type={img_type}, scale={scale}, timeout={timeout_ms}ms, "
                        f"options={options}"
                    )

                    t0 = time.monotonic()
                    try:
                        # 改为获取 bytes 数据，避免 OneBot 无法访问内部 URL
                        image_data = await html_render_func(
                            html_content,  # 渲染后的HTML内容
                            {},  # 空数据字典，因为数据已包含在HTML中
                            False,  # return_url=False，直接获取图片数据
                            options,
                        )
                        elapsed_ms = int((time.monotonic() - t0) * 1000)

                        if image_data is None:
                            summary = (
                                f"轮次{attempt}: 返回 None "
                                f"(耗时 {elapsed_ms}ms, type={img_type})"
                            )
                            attempt_summaries.append(summary)
                            logger.warning(f"[T2I] {summary}")
                            continue

                        data_desc = self._describe_render_result(image_data)
                        logger.info(
                            f"[T2I] 群 {group_id} 第 {attempt} 轮返回: "
                            f"{data_desc}, 耗时 {elapsed_ms}ms"
                        )

                        is_valid, image_url, invalid_reason = (
                            self._validate_and_normalize_image_data(image_data)
                        )
                        if is_valid and image_url:
                            if isinstance(image_data, bytes):
                                logger.info(
                                    f"[T2I] 群 {group_id} 图片生成成功 "
                                    f"(轮次 {attempt}, {elapsed_ms}ms): "
                                    f"{len(image_data)} bytes base64"
                                )
                            else:
                                logger.info(
                                    f"[T2I] 群 {group_id} 图片生成成功 "
                                    f"(轮次 {attempt}, {elapsed_ms}ms): {image_url}"
                                )
                            return image_url, html_content

                        summary = (
                            f"轮次{attempt}: 无效数据 "
                            f"({invalid_reason or '未知原因'}, "
                            f"耗时 {elapsed_ms}ms, type={img_type})"
                        )
                        attempt_summaries.append(summary)
                        logger.warning(f"[T2I] {summary}")

                    except Exception as e:
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        category, hint = self._classify_t2i_error(e)
                        summary = (
                            f"轮次{attempt}: 异常[{category}] "
                            f"{type(e).__name__}: {e} "
                            f"(耗时 {elapsed_ms}ms, type={img_type}, timeout={timeout_ms}ms)"
                        )
                        attempt_summaries.append(summary)
                        last_exception = e
                        logger.warning(f"[T2I] {summary}")
                        logger.warning(f"[T2I] 诊断提示: {hint}")
                        if attempt < len(render_strategies):
                            logger.info(
                                f"[T2I] 准备尝试第 {attempt + 1} 轮回退策略"
                            )
                        continue

                # 如果所有策略都失败
                logger.error(
                    f"[T2I] 群 {group_id} 全部 {len(render_strategies)} 轮渲染失败。"
                    f" HTML≈{html_kb:.1f}KB, 字体源={font_source}, 模板={template_name}"
                )
                for s in attempt_summaries:
                    logger.error(f"[T2I]   · {s}")
                if last_exception is not None:
                    _, final_hint = self._classify_t2i_error(last_exception)
                    logger.error(f"[T2I] 最终诊断: {final_hint}")
                else:
                    logger.error(
                        "[T2I] 最终诊断: 引擎未抛异常但返回了空/非法数据。"
                        " 常见原因: 1) 远程 T2I 端点返回 HTML 错误页"
                        " 2) Playwright 启动失败但被吞掉"
                        " 3) 超时后返回空结果。"
                        " 请检查 AstrBot 系统配置中的 T2I 端点，"
                        "并确认部署机已执行 `playwright install chromium`"
                    )
                return None, html_content

        except Exception as e:
            category, hint = self._classify_t2i_error(e)
            logger.error(
                f"[T2I] 群 {group_id} 生成图片报告发生严重错误 "
                f"[{category}] {type(e).__name__}: {e}",
                exc_info=True,
            )
            logger.error(f"[T2I] 诊断提示: {hint}")
            return None, html_content
        finally:
            # 清理本次运行的 session 和缓存
            if self._avatar_session:
                await self._avatar_session.close()
                self._avatar_session = None

    async def render_category_digest_image(
        self,
        payload: dict,
        html_render_func,
        context_label: str = "category_digest",
    ) -> tuple[str | None, str | None]:
        """将 by_category 分类摘要 payload 渲染为图片。

        独立于 generate_image_report（后者依赖 per_group 的 statistics/topics 结构）。
        复用 T2I 两轮策略、整体重试、信号量与返回值校验。

        Args:
            payload: 分类摘要模板上下文（title/date_str/sections/...）
            html_render_func: AstrBot html_render 回调
            context_label: 日志标识

        Returns:
            (image_url, html_content)；失败时 image_url 为 None
        """
        html_content = None
        if not html_render_func:
            logger.warning(f"[T2I][{context_label}] 未提供 html_render_func，跳过图片渲染")
            return None, None

        try:
            # 字体/镜像上下文与 per_group 模板保持一致
            render_ctx = {
                "t2i_font_source": self.config_manager.get_t2i_font_source(),
                "t2i_google_fonts_mirror": self.config_manager.get_t2i_google_fonts_mirror(),
                "t2i_gstatic_mirror": self.config_manager.get_t2i_gstatic_mirror(),
                "t2i_atri_font_mirror": self.config_manager.get_t2i_atri_font_mirror(),
                "current_datetime": _tz_now().strftime("%Y-%m-%d %H:%M:%S"),
                **payload,
            }
            html_content = self.html_templates.render_category_digest(**render_ctx)
            if not html_content:
                logger.error(f"[T2I][{context_label}] 分类摘要 HTML 渲染返回空")
                return None, None

            html_kb = len(html_content) / 1024
            logger.info(
                f"[T2I][{context_label}] HTML 准备完成: ~{html_kb:.1f} KB, "
                f"title={payload.get('title', '')!r}"
            )

            retry_count = max(0, self.config_manager.get_image_retry_count())
            interval = max(0, self.config_manager.get_image_retry_interval_seconds())
            total_attempts = retry_count + 1
            render_strategies = self.config_manager.get_t2i_rendering_strategies()

            async with self._render_semaphore:
                for overall in range(1, total_attempts + 1):
                    attempt_summaries: list[str] = []
                    last_exception = None

                    for attempt, image_options in enumerate(render_strategies, 1):
                        options = dict(image_options)
                        if options.get("type") == "png":
                            options.pop("quality", None)

                        timeout_ms = options.get("timeout", "?")
                        img_type = options.get("type", "?")
                        t0 = time.monotonic()
                        try:
                            image_data = await html_render_func(
                                html_content, {}, False, options
                            )
                            elapsed_ms = int((time.monotonic() - t0) * 1000)

                            if image_data is None:
                                summary = (
                                    f"轮次{attempt}: 返回 None "
                                    f"(耗时 {elapsed_ms}ms, type={img_type})"
                                )
                                attempt_summaries.append(summary)
                                logger.warning(f"[T2I][{context_label}] {summary}")
                                continue

                            data_desc = self._describe_render_result(image_data)
                            logger.info(
                                f"[T2I][{context_label}] 第 {attempt} 轮返回: "
                                f"{data_desc}, 耗时 {elapsed_ms}ms"
                            )

                            is_valid, image_url, invalid_reason = (
                                self._validate_and_normalize_image_data(image_data)
                            )
                            if is_valid and image_url:
                                logger.info(
                                    f"[T2I][{context_label}] 图片生成成功 "
                                    f"(整体第 {overall} 次, 策略轮次 {attempt}, "
                                    f"{elapsed_ms}ms)"
                                )
                                return image_url, html_content

                            summary = (
                                f"轮次{attempt}: 无效数据 "
                                f"({invalid_reason or '未知原因'}, "
                                f"耗时 {elapsed_ms}ms, type={img_type})"
                            )
                            attempt_summaries.append(summary)
                            logger.warning(f"[T2I][{context_label}] {summary}")
                        except Exception as e:
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            category, hint = self._classify_t2i_error(e)
                            summary = (
                                f"轮次{attempt}: 异常[{category}] "
                                f"{type(e).__name__}: {e} "
                                f"(耗时 {elapsed_ms}ms, type={img_type}, "
                                f"timeout={timeout_ms}ms)"
                            )
                            attempt_summaries.append(summary)
                            last_exception = e
                            logger.warning(f"[T2I][{context_label}] {summary}")
                            logger.warning(f"[T2I][{context_label}] 诊断提示: {hint}")

                    if overall < total_attempts:
                        logger.warning(
                            f"[T2I][{context_label}] 图片渲染失败 "
                            f"（第 {overall}/{total_attempts} 次），{interval}s 后重试"
                        )
                        await asyncio.sleep(interval)
                    else:
                        logger.error(
                            f"[T2I][{context_label}] 全部 {total_attempts} 次尝试均失败。"
                            f" HTML≈{html_kb:.1f}KB"
                        )
                        for s in attempt_summaries:
                            logger.error(f"[T2I][{context_label}]   · {s}")
                        if last_exception is not None:
                            _, final_hint = self._classify_t2i_error(last_exception)
                            logger.error(
                                f"[T2I][{context_label}] 最终诊断: {final_hint}"
                            )
                        return None, html_content

            return None, html_content
        except Exception as e:
            category, hint = self._classify_t2i_error(e)
            logger.error(
                f"[T2I][{context_label}] 分类摘要图片渲染严重错误 "
                f"[{category}] {type(e).__name__}: {e}",
                exc_info=True,
            )
            logger.error(f"[T2I][{context_label}] 诊断提示: {hint}")
            return None, html_content

    async def generate_html_report(
        self,
        analysis_result: dict,
        group_id: str,
        avatar_url_getter=None,
        nickname_getter=None,
        avatar_cache_namespace: str | None = None,
    ) -> tuple[str | None, str | None]:
        """
        生成HTML格式的分析报告，保存到指定目录

        Args:
            analysis_result: 分析结果字典
            group_id: 群组ID
            avatar_url_getter: 异步回调函数，接收 user_id 返回 avatar_url/data
            nickname_getter: 昵称获取函数

        Returns:
            tuple[str | None, str | None]: (html_path, json_path) - HTML文件路径和JSON文件路径
        """
        try:
            import json

            # 确保输出目录存在（使用 asyncio.to_thread 避免阻塞）
            output_dir = Path(self.config_manager.get_html_output_dir())
            await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)

            # 生成文件路径
            current_date = _tz_now().strftime("%Y%m%d")
            base_html_path = self._build_safe_report_path(
                output_dir,
                self.config_manager.get_html_filename_format(),
                group_id=group_id,
                date=current_date,
            )

            html_path = base_html_path
            if not html_path.suffix:
                html_path = html_path.with_suffix(".html")

            json_path = html_path.with_suffix(".json")

            html_path.parent.mkdir(parents=True, exist_ok=True)

            # 准备渲染数据
            render_data = await self._prepare_render_data(
                analysis_result,
                chart_template="activity_chart.html",
                avatar_url_getter=avatar_url_getter,
                nickname_getter=nickname_getter,
                avatar_cache_namespace=avatar_cache_namespace,
            )
            logger.info(f"HTML 渲染数据准备完成，包含 {len(render_data)} 个字段")

            # 生成 HTML 内容（使用 Jinja2 渲染器，尝试 html_template.html，失败则回退到 image_template.html）
            html_content = None
            try:
                html_content = self.html_templates.render_template(
                    "html_template.html", **render_data
                )
                html_content = self._reuse_avatars_in_final_html(
                    html_content,
                    render_data.get("avatar_reuse_registry", {}),
                    render_data.get("avatar_reuse_aliases", {}),
                )
                logger.info("使用 html_template.html 渲染成功")
            except Exception as e:
                logger.warning(
                    f"html_template.html 不存在或渲染失败，回退到 image_template.html: {e}"
                )
                html_content = self.html_templates.render_template(
                    "image_template.html", **render_data
                )
                html_content = self._reuse_avatars_in_final_html(
                    html_content,
                    render_data.get("avatar_reuse_registry", {}),
                    render_data.get("avatar_reuse_aliases", {}),
                )
                logger.info("使用 image_template.html 渲染成功")

            # 检查HTML内容是否有效
            if not html_content:
                logger.error("HTML报告渲染失败：返回空内容")
                return None, None

            logger.info(f"HTML 内容生成完成，长度: {len(html_content)} 字符")

            # 保存 HTML 文件
            await asyncio.to_thread(
                html_path.write_text, html_content, encoding="utf-8"
            )
            logger.info(f"HTML 报告已保存: {html_path}")

            def json_default_encoder(obj):
                if hasattr(obj, "to_dict") and callable(obj.to_dict):
                    return obj.to_dict()
                if is_dataclass(obj) and not isinstance(obj, type):
                    return asdict(obj)
                if isinstance(obj, (datetime, date)):
                    return obj.isoformat()
                if isinstance(obj, Enum):
                    return obj.value
                if isinstance(obj, (set, tuple)):
                    return list(obj)
                raise TypeError(
                    f"Object of type {type(obj).__name__} is not JSON serializable"
                )

            # 保存原始 JSON 数据
            json_data = {
                "analysis_result": analysis_result,
                "group_id": group_id,
                "generated_at": _tz_now().isoformat(),
            }
            await asyncio.to_thread(
                json_path.write_text,
                json.dumps(
                    json_data,
                    ensure_ascii=False,
                    indent=2,
                    default=json_default_encoder,
                ),
                encoding="utf-8",
            )
            logger.info(f"JSON 数据已保存: {json_path}")

            return str(html_path.absolute()), str(json_path.absolute())

        except Exception as e:
            logger.error(f"生成 HTML 报告失败: {e}", exc_info=True)
            return None, None

    def build_html_caption(self, html_path: str) -> str:
        """根据 html_base_url 生成 HTML 报告链接 caption"""

        caption = "📊 每日群聊分析报告已生成"
        base_url = self.config_manager.get_html_base_url()
        if not base_url or not html_path:
            return caption

        # 支持 html_filename_format 中的子目录，保持相对路径
        output_dir = Path(self.config_manager.get_html_output_dir()).resolve(
            strict=False
        )
        try:
            relative_path = (
                Path(html_path).resolve(strict=False).relative_to(output_dir)
            )
            relative_url = str(relative_path).replace(os.sep, "/")
        except Exception:
            relative_url = Path(html_path).name

        encoded_relative_url = quote(relative_url, safe="/")
        return caption + f"\n{base_url.rstrip('/')}/{encoded_relative_url}"

    def generate_text_report(
        self, analysis_result: dict, image_fallback: bool = False
    ) -> str:
        """生成结构化纯文本报告（QQ 友好，不用 Markdown 语法）。

        定制版：只保留统计/话题/信息差。用分隔线 + 方括号编号 + 留白排版。

        Args:
            image_fallback: True 表示这是图片渲染失败后的降级文本，顶部会加醒目的
                降级提示。False（默认）表示用户主动选的纯文本格式，不加提示。
        """
        stats = analysis_result["statistics"]
        topics = analysis_result.get("topics") or []

        sep = "━" * 20
        date_str = _tz_now().strftime("%Y-%m-%d")

        # 防御性字段访问：兼容 Topic 对象（.name）和历史 dict（topic key）
        def _topic_name(t) -> str:
            return getattr(t, "name", None) or (t.get("topic") if isinstance(t, dict) else "") or ""

        def _topic_contributors(t):
            return getattr(t, "contributors", None) or (t.get("contributors") if isinstance(t, dict) else []) or []

        def _topic_detail(t) -> str:
            return getattr(t, "detail", None) or (t.get("detail") if isinstance(t, dict) else "") or ""

        lines: list[str] = []
        if image_fallback:
            lines += [
                sep,
                "⚠️ 图片渲染失败，以下为降级文本摘要（完整报告见日志/HTML）",
                sep,
                "",
            ]
        lines += [
            f"群聊情报日报 · {date_str}",
            "",
            sep,
            "【基础统计】",
            sep,
            "",
            f"　消息总数　{stats.message_count}",
            f"　参与人数　{stats.participant_count}",
            f"　总字符数　{stats.total_characters}",
            f"　表情数量　{stats.emoji_count}",
            f"　最活跃时段　{stats.most_active_period}",
            "",
        ]

        # 话题板块
        max_topics = self.config_manager.get_max_topics()
        topics_slice = list(topics[:max_topics])
        lines += [sep, f"【有价值话题】共 {len(topics_slice)} 条", sep, ""]
        if topics_slice:
            for i, topic in enumerate(topics_slice, 1):
                name = _topic_name(topic)
                contributors_str = "、".join(_topic_contributors(topic))
                lines.append(f"  【话题 {i}】{name}")
                if contributors_str:
                    lines.append(f"  　参与者：{contributors_str}")
                detail = _topic_detail(topic).strip()
                if detail:
                    lines.append(f"  　{detail}")
                lines.append("")
        else:
            lines += ["（今日无明显有价值话题）", ""]

        # 信息差 / 商机 / 干货板块
        max_golden_quotes = self.config_manager.get_max_golden_quotes()
        golden_quotes = getattr(stats, "golden_quotes", None) or []
        quotes_slice = list(golden_quotes[:max_golden_quotes])
        lines += [sep, f"【信息差 / 商机 / 干货】共 {len(quotes_slice)} 条", sep, ""]
        if quotes_slice:
            for i, gq in enumerate(quotes_slice, 1):
                idx = f"{i:02d}"
                lines.append(f"  【{idx}】“{gq.content}”")
                if gq.sender:
                    lines.append(f"  　—— {gq.sender}")
                reason = (gq.reason or "").strip()
                if reason:
                    lines.append(f"  　价值：{reason}")
                lines.append("")
        else:
            lines += ["（今日未筛到可行动的高价值信息）", ""]

        lines.append(sep)
        return "\n".join(lines)

    async def _prepare_render_data(
        self,
        analysis_result: dict,
        chart_template: str = "activity_chart.html",
        avatar_url_getter=None,
        nickname_getter=None,
        avatar_cache_namespace: str | None = None,
    ) -> dict:
        """准备渲染数据"""
        stats = analysis_result["statistics"]
        topics = analysis_result["topics"]
        activity_viz = stats.activity_visualization

        # 使用Jinja2模板构建话题HTML（批量渲染）
        max_topics = self.config_manager.get_max_topics()
        topics_list = []
        user_analysis = analysis_result.get("user_analysis")
        avatar_reuse_registry: dict[str, str] = {}
        avatar_reuse_aliases: dict[str, str] = {}

        for i, topic in enumerate(topics[:max_topics], 1):
            # 处理话题详情中的用户引用头像
            processed_detail = await self._render_mentions(
                topic.detail,
                avatar_url_getter,
                nickname_getter,
                user_analysis,
                avatar_cache_namespace,
                avatar_reuse_registry,
                avatar_reuse_aliases,
            )
            topics_list.append(
                {
                    "index": i,
                    "topic": topic,
                    "contributors": "、".join(topic.contributors),
                    "detail": processed_detail,
                }
            )

        # 通用模板上下文，包含可能被子模板引用的全局配置
        common_context = {
            "t2i_font_source": self.config_manager.get_t2i_font_source(),
            "t2i_google_fonts_mirror": self.config_manager.get_t2i_google_fonts_mirror(),
            "t2i_gstatic_mirror": self.config_manager.get_t2i_gstatic_mirror(),
            "t2i_atri_font_mirror": self.config_manager.get_t2i_atri_font_mirror(),
        }

        topics_html = self.html_templates.render_template(
            "topic_item.html", topics=topics_list, **common_context
        )
        logger.info(f"话题HTML生成完成，长度: {len(topics_html)}")

        # 信息差/干货 HTML（原金句板块）
        max_golden_quotes = self.config_manager.get_max_golden_quotes()
        quotes_list = []
        golden_quotes = getattr(stats, "golden_quotes", None) or []
        for golden_quote in golden_quotes[:max_golden_quotes]:
            quote_user_id = str(golden_quote.user_id) if golden_quote.user_id else None
            avatar_url = (
                await self._get_user_avatar(
                    quote_user_id,
                    avatar_url_getter,
                    avatar_cache_namespace,
                )
                if quote_user_id
                else None
            )
            if quote_user_id:
                self._register_reusable_avatar(
                    avatar_url,
                    avatar_reuse_registry,
                    avatar_reuse_aliases,
                    avatar_key=self._get_avatar_cache_key(
                        quote_user_id, avatar_cache_namespace
                    ),
                )
            # 处理价值说明中的用户引用头像
            processed_reason = await self._render_mentions(
                golden_quote.reason,
                avatar_url_getter,
                nickname_getter,
                user_analysis,
                avatar_cache_namespace,
                avatar_reuse_registry,
                avatar_reuse_aliases,
            )
            quotes_list.append(
                {
                    "content": golden_quote.content,
                    "sender": golden_quote.sender,
                    "reason": processed_reason,
                    "avatar_url": avatar_url,
                }
            )

        quotes_html = self.html_templates.render_template(
            "quote_item.html", quotes=quotes_list, **common_context
        )
        logger.info(f"信息差HTML生成完成，长度: {len(quotes_html)}")

        # 生成活跃度可视化HTML
        chart_data = self.activity_visualizer.get_hourly_chart_data(
            activity_viz.hourly_activity
        )
        hourly_chart_html = self.html_templates.render_template(
            chart_template, chart_data=chart_data, **common_context
        )
        logger.info(f"活跃度图表HTML生成完成，长度: {len(hourly_chart_html)}")

        # 准备最终渲染数据
        render_data = {
            "t2i_font_source": self.config_manager.get_t2i_font_source(),
            "t2i_google_fonts_mirror": self.config_manager.get_t2i_google_fonts_mirror(),
            "t2i_gstatic_mirror": self.config_manager.get_t2i_gstatic_mirror(),
            "t2i_atri_font_mirror": self.config_manager.get_t2i_atri_font_mirror(),
            "current_date": _tz_now().strftime("%Y年%m月%d日"),
            "current_datetime": _tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "message_count": stats.message_count,
            "participant_count": stats.participant_count,
            "total_characters": stats.total_characters,
            "emoji_count": stats.emoji_count,
            "most_active_period": stats.most_active_period,
            "topics_html": topics_html,
            "quotes_html": quotes_html,
            "hourly_chart_html": hourly_chart_html,
            "total_tokens": stats.token_usage.total_tokens
            if stats.token_usage.total_tokens
            else 0,
            "prompt_tokens": stats.token_usage.prompt_tokens
            if stats.token_usage.prompt_tokens
            else 0,
            "completion_tokens": stats.token_usage.completion_tokens
            if stats.token_usage.completion_tokens
            else 0,
            "avatar_reuse_registry": avatar_reuse_registry,
            "avatar_reuse_aliases": avatar_reuse_aliases,
        }

        logger.info(f"渲染数据准备完成，包含 {len(render_data)} 个字段")
        return render_data

    async def _render_mentions(
        self,
        text: str,
        avatar_url_getter,
        nickname_getter=None,
        user_analysis: dict | None = None,
        avatar_cache_namespace: str | None = None,
        avatar_reuse_registry: dict[str, str] | None = None,
        avatar_reuse_aliases: dict[str, str] | None = None,
    ) -> Markup:
        """
        处理文本，将 [123456] 格式的用户引用替换为头像+名称的胶囊样式
        """
        pattern = r"\[(\d+)\]"
        if not text:
            return Markup("")

        matches = list(re.finditer(pattern, text))
        if not matches:
            return self._escape_text_segment(text)

        async def render_capsule(match: re.Match[str]) -> Markup:
            uid = match.group(1)
            url = await self._get_user_avatar(
                uid, avatar_url_getter, avatar_cache_namespace
            )  # 内部已有缓存，无需顶层并发获取

            name = None
            # 1. 尝试从 LLM 分析结果获取
            if user_analysis and uid in user_analysis:
                stats = user_analysis[uid]
                name = stats.get("nickname") or stats.get("name")
                if self._is_placeholder_display_name(name, uid):
                    name = None

            # 2. 尝试通过回调获取实时昵称
            if not name and nickname_getter:
                try:
                    name = await nickname_getter(uid)
                    if self._is_placeholder_display_name(name, uid):
                        name = None
                except Exception as e:
                    logger.warning(f"获取昵称失败 {uid}: {e}")

            # 胶囊样式 (Capsule Style) - 统一使用
            capsule_style = (
                "display:inline-flex;align-items:center;background:rgba(0,0,0,0.05);"
                "padding:2px 6px 2px 2px;border-radius:12px;margin:0 2px;"
                "vertical-align:middle;border:1px solid rgba(0,0,0,0.1);text-decoration:none;"
            )
            img_style = "width:18px;height:18px;border-radius:50%;margin-right:4px;display:block;"
            name_style = "font-size:0.85em;color:inherit;font-weight:500;line-height:1;"

            # 3. 最终后备: 确保有头像和名称
            final_url = url if url else self._get_default_avatar_base64()
            final_name = (
                name
                if (name and not self._is_placeholder_display_name(name, uid))
                else str(uid)
            )

            avatar_ref = self._register_reusable_avatar(
                final_url,
                avatar_reuse_registry,
                avatar_reuse_aliases,
                avatar_key=self._get_avatar_cache_key(uid, avatar_cache_namespace),
            )
            if avatar_ref:
                avatar_html = (
                    f'<span class="user-capsule-avatar" '
                    f'data-avatar-ref="{html.escape(avatar_ref, quote=True)}" '
                    f'style="{img_style}background-size:cover;background-position:center;'
                    'background-repeat:no-repeat;flex-shrink:0;"></span>'
                )
            else:
                avatar_html = (
                    f'<img src="{html.escape(final_url, quote=True)}" '
                    f'style="{img_style}">'
                )

            return Markup(
                f'<span class="user-capsule" style="{capsule_style}">'
                f"{avatar_html}"
                f'<span style="{name_style}">{html.escape(final_name)}</span>'
                "</span>"
            )

        result: list[Markup | str] = []
        last_end = 0
        for match in matches:
            result.append(self._escape_text_segment(text[last_end : match.start()]))
            result.append(await render_capsule(match))
            last_end = match.end()

        result.append(self._escape_text_segment(text[last_end:]))
        return Markup("").join(result)

    @staticmethod
    def _escape_text_segment(text: str) -> Markup:
        return Markup(html.escape(text, quote=False).replace("\n", "<br>"))

    @staticmethod
    def _is_placeholder_display_name(name: str | None, user_id: str) -> bool:
        """判断展示名称是否为占位值。"""
        if not name:
            return True
        normalized = str(name).strip()
        if not normalized:
            return True
        if normalized.lower() in {"unknown", "none", "null", "nil", "undefined"}:
            return True
        return normalized == str(user_id).strip()

    @staticmethod
    def _safe_url_for_log(url: str | None) -> str:
        """对日志中的 URL 进行脱敏，避免泄露 token。"""
        if not url:
            return ""
        # Telegram file URL: .../file/bot<token>/<file_path>
        return re.sub(r"/bot[^/]+/", "/bot<redacted>/", url)

    @staticmethod
    def _build_avatar_ref(avatar_key: str | None, avatar_url: str) -> str:
        """根据稳定输入生成不暴露平台或用户 ID 的头像引用。"""
        if avatar_key:
            digest = hashlib.sha256(avatar_key.encode("utf-8")).hexdigest()[:24]
            return f"avatar-{digest}"

        digest = hashlib.sha256(avatar_url.encode("utf-8")).hexdigest()[:24]
        return f"avatar-{digest}"

    @staticmethod
    def _register_reusable_avatar(
        avatar_url: str | None,
        avatar_reuse_registry: dict[str, str] | None,
        avatar_reuse_aliases: dict[str, str] | None = None,
        avatar_key: str | None = None,
    ) -> str | None:
        """将 Data URI 头像登记为可复用资源，并返回短引用 ID。"""
        if not avatar_url or avatar_reuse_registry is None:
            return None
        if not avatar_url.startswith("data:image/"):
            return None

        if avatar_reuse_aliases and avatar_url in avatar_reuse_aliases:
            return avatar_reuse_aliases[avatar_url]

        ref = ReportGenerator._build_avatar_ref(avatar_key, avatar_url)
        avatar_reuse_registry.setdefault(ref, avatar_url)
        if avatar_reuse_aliases is not None:
            avatar_reuse_aliases[avatar_url] = ref
        return ref

    @staticmethod
    def _build_avatar_reuse_styles(avatar_reuse_registry: dict[str, str]) -> str:
        """为头像生成一次性复用样式。"""
        if not avatar_reuse_registry:
            return ""

        rules = [
            '<style id="avatar-reuse-styles">',
            ".user-capsule-avatar,img[data-avatar-ref]{background-color:#ddd;background-size:cover;background-position:center;background-repeat:no-repeat;}",
        ]
        for ref, data_uri in avatar_reuse_registry.items():
            escaped_ref = html.escape(ref, quote=True)
            escaped_uri = data_uri.replace("\\", "\\\\").replace('"', '\\"')
            rules.append(
                f'[data-avatar-ref="{escaped_ref}"]'
                f'{{background-image:url("{escaped_uri}");}}'
            )
        rules.append("</style>")
        return "\n".join(rules)

    @staticmethod
    def _reuse_inline_avatar_img_sources(
        html_content: str,
        avatar_reuse_registry: dict[str, str],
        avatar_reuse_aliases: dict[str, str] | None = None,
    ) -> str:
        """将最终 HTML 中的内联 Data URI 头像 img 改为短引用。"""
        if not html_content:
            return html_content

        img_src_pattern = re.compile(
            r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(data:image/[^"\']+)(\2)([^>]*>)',
            re.IGNORECASE | re.DOTALL,
        )

        def replace(match: re.Match[str]) -> str:
            prefix, quote_char, data_uri, _, suffix = match.groups()
            if data_uri == TRANSPARENT_IMAGE_DATA_URI:
                return match.group(0)

            avatar_ref = (
                avatar_reuse_aliases.get(data_uri) if avatar_reuse_aliases else None
            )
            if not avatar_ref:
                return match.group(0)

            escaped_ref = html.escape(avatar_ref, quote=True)
            return (
                f"{prefix}{quote_char}{TRANSPARENT_IMAGE_DATA_URI}{quote_char}"
                f' data-avatar-ref="{escaped_ref}"{suffix}'
            )

        return img_src_pattern.sub(replace, html_content)

    @staticmethod
    def _reuse_avatars_in_final_html(
        html_content: str,
        avatar_reuse_registry: dict[str, str] | None,
        avatar_reuse_aliases: dict[str, str] | None = None,
    ) -> str:
        """复用最终 HTML 中所有内联头像资源，并注入复用样式。"""
        if not html_content:
            return html_content

        registry = avatar_reuse_registry if avatar_reuse_registry is not None else {}
        aliases = avatar_reuse_aliases if avatar_reuse_aliases is not None else {}
        html_content = ReportGenerator._reuse_inline_avatar_img_sources(
            html_content, registry, aliases
        )
        return ReportGenerator._inject_avatar_reuse_styles(
            html_content, ReportGenerator._build_avatar_reuse_styles(registry)
        )

    @staticmethod
    def _inject_avatar_reuse_styles(html_content: str, avatar_reuse_styles: str) -> str:
        """将头像复用样式注入最终 HTML。"""
        if not html_content or not avatar_reuse_styles:
            return html_content

        head_close = re.search(r"</head\s*>", html_content, re.IGNORECASE)
        if head_close:
            return (
                html_content[: head_close.start()]
                + avatar_reuse_styles
                + "\n"
                + html_content[head_close.start() :]
            )
        return avatar_reuse_styles + "\n" + html_content

    def _get_avatar_cache_key(
        self, avatar_id: str, avatar_cache_namespace: str | None = None
    ) -> str:
        """生成头像缓存键，避免不同平台的同一数字 ID 互相污染。"""
        namespace = str(avatar_cache_namespace or "legacy").strip() or "legacy"
        return f"{namespace}:{avatar_id}"

    async def _get_user_avatar(
        self,
        avatar_id: str,
        avatar_url_getter=None,
        avatar_cache_namespace: str | None = None,
    ) -> str:
        """
        获取用户头像的 Base64 Data URI。
        使用磁盘缓存，支持跨任务复用。获取失败时不缓存结果，以便后续请求重试。
        """
        cache_key = self._get_avatar_cache_key(avatar_id, avatar_cache_namespace)
        # 1. 检查缓存 (仅包含成功的头像数据)
        if cache_key in self._avatar_cache:
            data = self._avatar_cache[cache_key]
            if isinstance(data, str):
                return data
            return str(data)

        # 2. 尝试获取头像字节流
        avatar_bytes = await self._get_user_avatar_bytes(avatar_id, avatar_url_getter)

        if not avatar_bytes:
            # 获取失败时返回默认头像，但不存入缓存，以便下次重试
            logger.warning(f"获取用户头像失败 {avatar_id}，本次将使用回退头像")
            return self._get_default_avatar_base64()

        # 3. 获取成功：转换并缓存
        avatar = self._b64_with_mime(avatar_bytes)
        if avatar:
            self._avatar_cache.set(cache_key, avatar, expire=AVATAR_CACHE_EXPIRE_TIME)
            return avatar

        # 最终兜底
        return self._get_default_avatar_base64()

    def _b64_with_mime(self, _bytes: bytes) -> str | None:
        """将字节数据转换为 Base64 Data URI，并自动识别 MIME 类型。"""
        try:
            b64 = base64.b64encode(_bytes).decode("utf-8")
            # 简单判断 mime type
            mime = "image/jpeg"
            if _bytes.startswith(b"\x89PNG"):
                mime = "image/png"
            elif _bytes.startswith(b"GIF8"):
                mime = "image/gif"
            elif _bytes.startswith(b"RIFF") and b"WEBP" in _bytes[8:16]:
                mime = "image/webp"
            elif _bytes.startswith(b"\xff\xd8"):
                mime = "image/jpeg"

            return f"data:{mime};base64,{b64}"
        except Exception as e:
            logger.error(f"base64 转换失败: {e}", exc_info=True)
        return None

    async def _get_user_avatar_bytes(
        self, user_id: str, avatar_url_getter=None
    ) -> bytes | None:
        """核心头像获取逻辑"""
        file_content = None
        if not self._avatar_session:
            self._avatar_session = aiohttp.ClientSession(
                trust_env=True, timeout=aiohttp.ClientTimeout(total=15)
            )
        async with self._avatar_session_concurrent_semaphore:
            avatar_url = None
            if avatar_url_getter:
                try:
                    # avatar_url_getter 应该返回 URL
                    result = await avatar_url_getter(user_id)
                    if result:
                        if result.startswith("http"):
                            avatar_url = result
                        elif result.startswith("base64://"):
                            return base64.b64decode(result[len("base64://") :])
                        elif result.startswith("data:"):
                            parts = result.split(",", 1)
                            if len(parts) == 2:
                                return base64.b64decode(parts[1])
                        else:
                            logger.warning(
                                f"custom avatar_url_getter 返回了非 HTTP URL: {result[:50]}..."
                            )
                except Exception as e:
                    logger.warning(f"使用 custom avatar_url_getter 获取头像失败: {e}")

            if not avatar_url:
                if (
                    avatar_url_getter is None
                    and user_id.isdigit()
                    and 5 <= len(user_id) <= 12
                ):
                    # 强制使用 spec=40
                    avatar_url = (
                        f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=40"
                    )
                else:
                    # 其他平台若无 URL，无法获取头像
                    return None

            # 5. 下载并保存
            safe_avatar_url = self._safe_url_for_log(avatar_url)
            try:
                async with self._avatar_session.get(avatar_url) as response:
                    if response.status == 200:
                        content = await response.read()
                        if content:
                            # 校验文件头
                            is_valid_image = False
                            if content.startswith(b"\xff\xd8"):  # JPEG
                                is_valid_image = True
                            elif content.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
                                is_valid_image = True
                            elif content.startswith(b"GIF8"):  # GIF
                                is_valid_image = True
                            elif (
                                content.startswith(b"RIFF") and b"WEBP" in content[:16]
                            ):  # WebP
                                is_valid_image = True

                            if is_valid_image:
                                file_content = content
                            else:
                                logger.warning(
                                    f"下载的头像数据格式无效 ({safe_avatar_url})"
                                )
                    else:
                        logger.warning(
                            f"下载头像失败 {safe_avatar_url}: {response.status}"
                        )
            except Exception as e:
                logger.warning(f"下载头像网络错误 {safe_avatar_url}: {e}")

            return file_content

    def _get_default_avatar_base64(self) -> str:
        """返回默认头像 (灰色圆形占位符)"""
        # 一个简单的灰色圆圈 SVG 转 Base64
        svg = '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg"><circle cx="50" cy="50" r="50" fill="#ddd"/></svg>'
        b64 = base64.b64encode(svg.encode("utf-8")).decode("utf-8")
        return f"data:image/svg+xml;base64,{b64}"

    async def close(self):
        """释放资源，关闭缓存和 session"""
        if self._avatar_session:
            await self._avatar_session.close()
            self._avatar_session = None

        try:
            if self._avatar_cache:
                self._avatar_cache.close()
                logger.debug("头像缓存已关闭")
        except Exception as e:
            logger.warning(f"关闭头像缓存失败: {e}")

    def _validate_and_normalize_image_data(
        self, image_data
    ) -> tuple[bool, str | None, str | None]:
        """校验 html_render 返回值是否为合法图片，并归一化为可发送的 image_url。

        Returns:
            (is_valid, image_url, invalid_reason)
            - is_valid=True 时 image_url 为 base64:// / 路径 / http(s) / data:image
            - is_valid=False 时 invalid_reason 说明原因
        """
        if image_data is None:
            return False, None, "返回 None"

        is_valid = False
        actual_data_head = None
        invalid_reason = None

        if isinstance(image_data, bytes):
            actual_data_head = image_data[:10]
        elif isinstance(image_data, str) and os.path.exists(image_data):
            try:
                with open(image_data, "rb") as f:
                    actual_data_head = f.read(10)
            except Exception as e:
                return False, None, f"读取临时文件失败: {e}"
        elif isinstance(image_data, str):
            if image_data.startswith(
                ("http://", "https://", "base64://", "data:image")
            ):
                return True, image_data, None
            invalid_reason = (
                f"返回字符串既不是已存在路径也不是 URL/base64 "
                f"(前80字: {image_data[:80]!r})"
            )
        else:
            return False, None, f"不支持的返回类型: {type(image_data).__name__}"

        if actual_data_head is not None and not is_valid:
            if actual_data_head.startswith(b"\xff\xd8") or actual_data_head.startswith(
                b"\x89PNG"
            ):
                is_valid = True
            else:
                html_error = None
                if isinstance(image_data, bytes):
                    html_error = self._extract_html_error_summary(image_data)
                elif isinstance(image_data, str) and os.path.exists(image_data):
                    try:
                        with open(image_data, "rb") as f:
                            html_error = self._extract_html_error_summary(f.read(4096))
                    except Exception:
                        pass
                if html_error:
                    invalid_reason = f"返回错误页而非图片: {html_error}"
                else:
                    invalid_reason = (
                        f"非图片 magic (头部 hex={actual_data_head.hex()})"
                    )

        if not is_valid:
            return False, None, invalid_reason or "未知原因"

        if isinstance(image_data, bytes):
            b64 = base64.b64encode(image_data).decode("utf-8")
            return True, f"base64://{b64}", None
        if isinstance(image_data, str):
            return True, image_data, None
        return False, None, f"校验通过但无法归一化: {type(image_data).__name__}"

    @staticmethod
    def _describe_render_result(image_data) -> str:
        """描述 html_render 返回值，便于日志诊断。"""
        if image_data is None:
            return "None"
        if isinstance(image_data, bytes):
            head = image_data[:8].hex() if image_data else ""
            return f"bytes(len={len(image_data)}, head={head})"
        if isinstance(image_data, str):
            exists = os.path.exists(image_data)
            preview = image_data if len(image_data) <= 120 else image_data[:120] + "..."
            return f"str(len={len(image_data)}, path_exists={exists}, preview={preview!r})"
        return f"{type(image_data).__name__}({image_data!r})"

    @staticmethod
    def _classify_t2i_error(exc: BaseException) -> tuple[str, str]:
        """把渲染异常归类，并给出可操作的排查提示。

        Returns:
            (category, hint)
        """
        msg = str(exc) or ""
        name = type(exc).__name__
        combined = f"{name} {msg}".lower()

        # 超时
        if (
            "timeout" in combined
            or "timed out" in combined
            or "time out" in combined
            or name in {"TimeoutError", "asyncio.TimeoutError"}
            or "TimeoutError" in name
        ):
            return (
                "timeout",
                "渲染超时。建议: 1) 把 t2i_r1_timeout / t2i_r2_timeout 调到 120000~180000 "
                "2) 第一轮改 jpeg + high（别用 png+ultra）"
                "3) 换 simple 模板减小 HTML 体积"
                "4) 字体源改 Mainland 避免 Google Fonts 卡住",
            )

        # Playwright / 浏览器未装
        if any(
            k in combined
            for k in (
                "playwright",
                "chromium",
                "browser",
                "executable doesn't exist",
                "executable_path",
                "browserType.launch",
                "targetclosederror",
                "browsertype",
            )
        ):
            return (
                "browser_missing",
                "Playwright/Chromium 启动失败。在 AstrBot 运行环境执行: "
                "`playwright install chromium`；Linux 再加 `playwright install-deps chromium`，然后重启 AstrBot",
            )

        # 网络 / 远程 T2I 端点
        if any(
            k in combined
            for k in (
                "endpoint",
                "connection",
                "connect",
                "refused",
                "reset",
                "unreachable",
                "name or service not known",
                "nodename nor servname",
                "ssl",
                "certificate",
                "502",
                "503",
                "504",
                "bad gateway",
                "service unavailable",
                "httpx",
                "aiohttp",
                "clientconnector",
            )
        ):
            return (
                "endpoint_network",
                "远程 T2I 端点网络失败。检查 AstrBot 系统配置里的 T2I URL；"
                "本地部署可改用本地 Playwright，或换 HF Space / 国内 CF 代理端点",
            )

        # 内存 / 资源
        if any(
            k in combined
            for k in ("memory", "oom", "cannot allocate", "killed", "resource")
        ):
            return (
                "resource",
                "渲染资源不足（内存/进程被杀）。建议: jpeg + normal scale、减小 HTML、"
                "把 max_concurrent_t2i 保持为 1",
            )

        # 字体相关
        if any(k in combined for k in ("font", "woff", "googleapis", "gstatic")):
            return (
                "font",
                "字体资源异常。把 t2i_font_source 改为 Mainland（fonts.loli.net），"
                "或检查本机能否访问 Google Fonts 镜像",
            )

        return (
            "unknown",
            f"未归类异常 ({name})。请把完整堆栈贴出以便继续排查；"
            f"同时确认: playwright install chromium / T2I 端点 / timeout / 模板体积",
        )

    def _extract_html_error_summary(self, data: bytes) -> str | None:
        """从返回的字节流中尝试提取 HTML 错误信息（如 <title>）"""
        try:
            content = data.decode("utf-8", errors="ignore")
            content_lower = content.lower()
            if "<html" in content_lower or "<!doctype html" in content_lower:
                # 尝试提取标题
                title_match = re.search(
                    r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL
                )
                if title_match:
                    return f"HTML 错误页: {title_match.group(1).strip()}"

                # 尝试提取 h1
                h1_match = re.search(
                    r"<h1>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL
                )
                if h1_match:
                    return f"HTML 错误页: {h1_match.group(1).strip()}"

                return f"HTML 响应 (前100字): {content[:100].strip()}..."
        except Exception:
            pass
        return None
