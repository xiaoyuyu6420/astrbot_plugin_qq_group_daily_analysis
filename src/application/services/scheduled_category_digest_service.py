"""
定时分类聚合摘要服务

只服务 delivery_mode=by_category 的定时链路：
按用户分类（科技/AI/自定义）下挂的群列表，抽取当日有价值信息，
打包成 digest 私聊管理员（默认图片，失败回退文本）。

与实时监控的分层聚合 / 内容频道无关。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.entities.push_category import PushCategory
from ...domain.models.data_models import DigestTheme
from ...domain.services.message_cleaner_service import MessageCleanerService
from ...shared.fingerprint import content_fingerprint
from ...shared.timezone import now as _tz_now
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ...infrastructure.utils.admin_resolver import resolve_admin_qqs
from ...infrastructure.utils.name_resolver import NameResolver

# 分类摘要单张图最多展示的条目数（与 _format_items 默认一致）。
# 保留为兜底默认值；实际运行时优先读配置 digest_max_items_per_section。
_MAX_ITEMS_PER_SECTION = 12
# 摘要内 #[编号] → 来源标签的截断长度
_SOURCE_TAG_GROUP_MAX = 8
_SOURCE_TAG_USER_MAX = 6


@dataclass
class ValueItem:
    """分类摘要中的一条价值信息。"""

    content: str
    reason: str = ""
    source_group_id: str = ""
    source_user_id: str = ""
    # 解析后的人类可读名（由 NameResolver 在聚合阶段回填，默认空）。
    # 渲染层优先用它，缺失时才退回 source_group_id / source_user_id。
    source_group_name: str = ""
    source_user_name: str = ""
    kind: str = "info"  # quote | topic | info
    fingerprint: str = ""

    def ensure_fingerprint(self) -> str:
        if self.fingerprint:
            return self.fingerprint
        # 委托给 shared.fingerprint，与降噪层/分层聚合统一（32 字符 + 去标点）
        self.fingerprint = content_fingerprint(self.content)
        return self.fingerprint


@dataclass
class CategoryDigest:
    """一个用户分类的摘要结果。

    items: 全量原条目（去重后），文本附录兜底信息完整性。
    themes: 二次聚合后的主题（可选）；为空时回退 items 平铺渲染。
    """

    name: str
    items: list[ValueItem] = field(default_factory=list)
    groups_analyzed: list[str] = field(default_factory=list)
    groups_skipped: list[str] = field(default_factory=list)
    themes: list = field(default_factory=list)  # list[DigestTheme]；用 Any[] 避免循环导入


class ScheduledCategoryDigestService:
    """定时分类聚合：抽价值 → 合并去重 → split/merged 打包 → 私聊管理员。"""

    def __init__(
        self,
        config_manager: Any,
        analysis_service: Any,
        bot_manager: Any,
        report_dispatcher: Any | None = None,
        report_generator: Any | None = None,
        html_render_func: Any | None = None,
        data_dir: str | Path | None = None,
    ):
        self.config_manager = config_manager
        self.analysis_service = analysis_service
        self.bot_manager = bot_manager
        self.report_dispatcher = report_dispatcher
        self.report_generator = report_generator
        self.html_render_func = html_render_func
        # 群名/人名解析器：任务级单例，跨群共享缓存（群名 1h、名片 30min TTL）
        self._name_resolver = NameResolver(bot_manager)
        # 落盘目录：{plugin_data}/digests
        if data_dir is not None:
            self._digests_dir = Path(data_dir) / "digests"
        else:
            self._digests_dir = Path("/tmp/astrbot_digests")
        self._digests_dir.mkdir(parents=True, exist_ok=True)
        # 保留天数
        try:
            self._digest_retain_days = int(
                self.config_manager.get_digest_retain_days()
            ) if hasattr(self.config_manager, "get_digest_retain_days") else 7
        except Exception:
            self._digest_retain_days = 7
        # 二次聚合分析器（延迟装配：需要从 llm_analyzer 取 context/config_manager）。
        # 装配失败时置 None，聚合逻辑自动降级到旧的平铺拼接。
        self._theme_analyzer = self._build_theme_analyzer()

    def _build_theme_analyzer(self):
        """构造 DigestThemeAnalyzer，复用 llm_analyzer 的 context/config_manager。

        失败（如测试环境注入 mock analysis_service）返回 None，
        调用方据此降级到旧的平铺拼接逻辑，保证不崩。
        """
        if not self.is_digest_aggregation_enabled():
            return None
        try:
            from ...infrastructure.analysis.analyzers.digest_theme_analyzer import (
                DigestThemeAnalyzer,
            )

            llm_analyzer = getattr(self.analysis_service, "llm_analyzer", None)
            context = getattr(llm_analyzer, "context", None)
            if context is None or self.config_manager is None:
                return None
            return DigestThemeAnalyzer(context, self.config_manager)
        except Exception as e:
            logger.warning(f"DigestThemeAnalyzer 装配失败，降级为平铺拼接: {e}")
            return None

    def is_digest_aggregation_enabled(self) -> bool:
        """配置开关：是否开启二次聚合。"""
        getter = getattr(self.config_manager, "is_digest_aggregation_enabled", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                pass
        return True

    def _get_digest_max_themes(self) -> int:
        getter = getattr(self.config_manager, "get_digest_max_themes", None)
        if callable(getter):
            try:
                return max(3, int(getter()))
            except Exception:
                pass
        return 5

    async def run(self, platform_id: str | None = None) -> dict[str, Any]:
        """执行一次分类聚合定时任务。"""
        trace_id = TraceContext.get()
        categories = self.config_manager.get_push_categories()
        if not categories:
            logger.error(
                f"[{trace_id}] delivery_mode=by_category 但 categories 为空，跳过本次定时"
            )
            return {"success": False, "reason": "empty_categories"}

        push_mode = self.config_manager.get_category_push_mode()
        output_format = "text"
        if hasattr(self.config_manager, "get_category_output_format"):
            output_format = self.config_manager.get_category_output_format()
        max_concurrent = self.config_manager.get_max_concurrent_tasks() or 3
        stagger = self.config_manager.get_stagger_seconds() or 2

        logger.info(
            f"[{trace_id}] 分类聚合定时开始: {len(categories)} 个分类, "
            f"push_mode={push_mode}, output_format={output_format}, "
            f"concurrent={max_concurrent}"
        )

        digests: list[CategoryDigest] = []
        for cat in categories:
            digest = await self._build_category_digest(
                cat,
                platform_id=platform_id,
                max_concurrent=max_concurrent,
                stagger=stagger,
            )
            digests.append(digest)

        # 结果全空时探活 LLM：区分「LLM 挂了」和「确实没内容」。
        # 此前 LLM 全挂（如 402 未订购）会伪装成「未提取到有价值信息」，
        # 误导排障方向（以为是登录/插件问题）。
        llm_error: str | None = None
        if digests and all(not d.items for d in digests):
            llm_error = await self._probe_llm(platform_id)

        text_messages = self.pack_digests(
            digests, push_mode=push_mode, llm_error=llm_error
        )
        if not text_messages:
            logger.info(f"[{trace_id}] 分类聚合无有效内容，不推送")
            return {
                "success": True,
                "reason": "no_value",
                "digests": digests,
                "messages_sent": 0,
            }

        # 组装 (image_url|None, text_fallback) 列表
        deliveries: list[tuple[str | None, str]] = []
        if output_format == "image":
            deliveries = await self._render_image_deliveries(
                digests, text_messages, push_mode=push_mode, llm_error=llm_error
            )
        else:
            deliveries = [(None, msg) for msg in text_messages]

        # 预打包 Markdown：合并转发开启时作为文本节点塞进卡片（不单独发文件），
        # 合并转发未开启时由 _send_md_via_qq 单独发 .md 文件兜底
        try:
            md_contents = self.pack_digests_markdown(
                digests, push_mode=push_mode, llm_error=llm_error
            )
        except Exception as e:
            logger.warning(f"[{trace_id}] Markdown 打包失败: {e}")
            md_contents = []

        sent = await self._send_to_admins(
            deliveries, platform_id=platform_id, md_contents=md_contents or None
        )
        # Markdown 文件推送（合并转发已带走 md 时跳过 QQ 文件；邮件照发）
        md_sent = await self._send_markdown_files(digests, push_mode, platform_id)
        # 落盘：把本次 digest 存到磁盘，失败也不影响推送结果
        digest_path = await self._persist_digests(
            digests, deliveries, sent, llm_error=llm_error
        )
        # 清理过期文件
        self._cleanup_old_digests()
        return {
            "success": sent > 0,
            "digests": digests,
            "messages_sent": sent,
            "message_count": len(deliveries),
            "output_format": output_format,
            "digest_path": digest_path,
        }

    async def _probe_llm(self, platform_id: str | None) -> str | None:
        """结果全空时对 LLM 链路探活。返回错误摘要（None=健康）。

        从 analysis_service.llm_analyzer.context 取 AstrBot 上下文
        （与 _build_theme_analyzer 同一装配路径），取不到则无法探活，
        返回 None 不影响正常输出。
        """
        trace_id = TraceContext.get()
        try:
            llm_analyzer = getattr(self.analysis_service, "llm_analyzer", None)
            context = getattr(llm_analyzer, "context", None)
            if context is None or self.config_manager is None:
                logger.warning(
                    f"[{trace_id}] 无法获取 LLM 上下文，跳过探活"
                )
                return None

            from ...infrastructure.analysis.utils.llm_utils import probe_llm_health

            ok, reason = await probe_llm_health(
                context, self.config_manager, umo=None
            )
            if not ok:
                logger.error(f"[{trace_id}] ⚠️ 分析全空且 LLM 探活失败: {reason}")
                return reason
            logger.info(f"[{trace_id}] LLM 探活正常，全空为真实无内容")
            return None
        except Exception as e:
            logger.warning(f"[{trace_id}] LLM 探活异常（不影响输出）: {e}")
            return None

    async def _render_image_deliveries(
        self,
        digests: list[CategoryDigest],
        text_messages: list[str],
        push_mode: str,
        llm_error: str | None = None,
    ) -> list[tuple[str | None, str]]:
        """按 payload 渲染图片，失败则对应位置 image_url 为 None（走文本兜底）。"""
        trace_id = TraceContext.get()
        payloads = self.pack_digests_payload(
            digests, push_mode=push_mode, llm_error=llm_error
        )
        # pack_digests 在全空时返回 1 条总览，payload 也可能 1 条；对齐长度
        if len(payloads) != len(text_messages):
            logger.warning(
                f"[{trace_id}] 分类摘要 payload({len(payloads)}) 与 "
                f"文本({len(text_messages)}) 条数不一致，按较短者对齐"
            )

        # 无 report_generator / html_render_func 时整批回退文本
        if not self.report_generator or not self.html_render_func:
            logger.warning(
                f"[{trace_id}] 分类摘要图片渲染不可用"
                f"（generator={bool(self.report_generator)}, "
                f"render_func={bool(self.html_render_func)}），回退文本"
            )
            return [(None, msg) for msg in text_messages]

        deliveries: list[tuple[str | None, str]] = []
        pairs = list(zip(payloads, text_messages, strict=False))

        # Phase 3：图片渲染改并发 gather（内部 _render_semaphore 已限流），
        # 取代原先的 for 循环串行——6 张图串行等待是耗时主因之一。
        async def _render_one(payload: dict, text: str) -> tuple[str | None, str]:
            try:
                image_url, _html = await self.report_generator.render_category_digest_image(
                    payload,
                    self.html_render_func,
                    context_label=f"category:{payload.get('title', '')}",
                )
            except Exception as e:
                logger.error(f"[{trace_id}] 分类摘要图片渲染异常: {e}")
                image_url = None
            return (image_url, text)

        rendered = await asyncio.gather(
            *[_render_one(p, t) for p, t in pairs], return_exceptions=False
        )
        deliveries.extend(rendered)

        # 文本比 payload 多的部分纯文本补齐
        if len(text_messages) > len(payloads):
            for text in text_messages[len(payloads) :]:
                deliveries.append((None, text))

        ok_imgs = sum(1 for img, _ in deliveries if img)
        logger.info(
            f"[{trace_id}] 分类摘要图片渲染完成: {ok_imgs}/{len(deliveries)} 成功"
        )
        return deliveries

    async def rerender_and_resend_latest(
        self, platform_id: str | None = None
    ) -> dict[str, Any]:
        """读取最新落盘的 digest json，重建 CategoryDigest，重新渲染图片并发送。

        用途：调试模板/发送方式（如验证合并转发、新排版）时，复用已有数据，
        不重复跑耗时的拉消息+单群 LLM 分析流程。

        旧 digest 兜底：若 themes 为空（旧版 _persist_digests 没存）且条目超阈值，
        会现场补跑二次聚合（调 LLM，耗时几分钟）。
        """
        trace_id = TraceContext.get()
        # 1. 找最新 digest 文件
        digest_files = sorted(self._digests_dir.glob("digest_*.json"))
        if not digest_files:
            return {"success": False, "reason": "no_digest_file"}
        latest = digest_files[-1]
        logger.info(f"[{trace_id}] 重渲染：读取 {latest.name}")

        try:
            raw = await asyncio.to_thread(
                lambda: json.loads(latest.read_text(encoding="utf-8"))
            )
        except Exception as e:
            logger.error(f"[{trace_id}] 读取 digest 失败: {e}")
            return {"success": False, "reason": "read_error"}

        # 2. 重建 CategoryDigest 列表
        digests: list[CategoryDigest] = []
        for d in raw.get("digests", []):
            items = [
                ValueItem(
                    content=it.get("content", ""),
                    reason=it.get("reason", ""),
                    source_group_id=it.get("source_group_id", ""),
                    source_user_id=it.get("source_user_id", ""),
                    source_group_name=it.get("source_group_name", ""),
                    source_user_name=it.get("source_user_name", ""),
                    kind=it.get("kind", "info"),
                    fingerprint=it.get("fingerprint", ""),
                )
                for it in d.get("items", [])
            ]
            # themes（新版 digest 才有；旧版为空，渲染时走降级）
            themes = [
                DigestTheme(
                    title=t.get("title", ""),
                    narrative=t.get("narrative", ""),
                    importance=t.get("importance", "medium"),
                    tags=t.get("tags", []),
                    related_item_ids=t.get("related_item_ids", []),
                    # 回填 related_items：按 ids 从 items 取（1-based）
                    related_items=[
                        items[i - 1] for i in t.get("related_item_ids", []) if 0 < i <= len(items)
                    ],
                )
                for t in d.get("themes", [])
            ]
            digests.append(
                CategoryDigest(
                    name=d.get("name", ""),
                    items=items,
                    groups_analyzed=d.get("groups_analyzed", []),
                    groups_skipped=d.get("groups_skipped", []),
                    themes=themes,
                )
            )

        if not any(d.items for d in digests):
            return {"success": False, "reason": "empty_digest"}

        # 2.5 旧 digest 兜底：若某分类 themes 为空且 items 超阈值，现场补跑二次聚合。
        #     解决「旧版 _persist_digests 没存 themes，重渲染只能降级平铺」的问题。
        #     这一步会调 LLM（耗时几分钟），但只对缺 themes 且条目多的分类跑。
        aggregated_count = 0
        for d in digests:
            if not d.themes and self._should_aggregate(len(d.items)):
                logger.info(
                    f"[{trace_id}] 重渲染：分类 [{d.name}] 无 themes（旧 digest），"
                    f"现场补跑聚合（{len(d.items)} 条）"
                )
                d.themes = await self._aggregate_themes(
                    d.items, d.name, platform_id
                )
                if d.themes:
                    aggregated_count += 1

        # 3. 渲染 + 发送（复用 run 的链路）
        push_mode = self.config_manager.get_category_push_mode()
        output_format = "text"
        if hasattr(self.config_manager, "get_category_output_format"):
            output_format = self.config_manager.get_category_output_format()

        text_messages = self.pack_digests(digests, push_mode=push_mode)
        deliveries: list[tuple[str | None, str]] = []
        if output_format == "image":
            deliveries = await self._render_image_deliveries(
                digests, text_messages, push_mode=push_mode
            )
        else:
            deliveries = [(None, msg) for msg in text_messages]

        # 预打包 Markdown：合并转发开启时作为文本节点塞进卡片（不单独发文件），
        # 合并转发未开启时由 _send_md_via_qq 单独发 .md 文件兜底
        try:
            md_contents = self.pack_digests_markdown(digests, push_mode=push_mode)
        except Exception as e:
            logger.warning(f"[{trace_id}] Markdown 打包失败: {e}")
            md_contents = []

        sent = await self._send_to_admins(
            deliveries, platform_id=platform_id, md_contents=md_contents or None
        )
        # Markdown 文件推送（合并转发已带走 md 时跳过 QQ 文件；邮件照发）
        md_sent = await self._send_markdown_files(digests, push_mode, platform_id)
        logger.info(
            f"[{trace_id}] 重渲染发送完成: {len(deliveries)} 条图片(成功 {sent}), "
            f"Markdown {md_sent} 个文件"
        )
        return {
            "success": sent > 0 or md_sent > 0,
            "messages_sent": sent,
            "markdown_sent": md_sent,
            "message_count": len(deliveries),
            "output_format": output_format,
            "source_file": latest.name,
            "digests_count": len(digests),
            "themes_count": sum(len(d.themes) for d in digests),
            "aggregated_on_rerender": aggregated_count,
        }

    async def _build_category_digest(
        self,
        category: PushCategory,
        platform_id: str | None,
        max_concurrent: int,
        stagger: float,
    ) -> CategoryDigest:
        """每群独立抽价值（话题+金句），合并去重。"""
        digest = CategoryDigest(name=category.name)
        sem = asyncio.Semaphore(max_concurrent)
        results: list[list[ValueItem] | None] = [None] * len(category.groups)

        async def one(idx: int, group_id: str):
            async with sem:
                if not self._is_group_allowed(group_id, platform_id):
                    logger.info(
                        f"分类 [{category.name}] 群 {group_id} 命中 basic 黑名单，跳过"
                    )
                    digest.groups_skipped.append(group_id)
                    results[idx] = []
                    return
                try:
                    items = await self._extract_group_value(
                        group_id, platform_id=platform_id
                    )
                    results[idx] = items
                    digest.groups_analyzed.append(group_id)
                except Exception as e:
                    logger.error(
                        f"分类 [{category.name}] 群 {group_id} 抽取失败: {e}"
                    )
                    digest.groups_skipped.append(group_id)
                    results[idx] = []

        tasks = []
        for idx, gid in enumerate(category.groups):
            if idx > 0 and stagger > 0:
                await asyncio.sleep(stagger)
            tasks.append(asyncio.create_task(one(idx, gid)))
        if tasks:
            await asyncio.gather(*tasks)

        merged: list[ValueItem] = []
        seen_fp: set[str] = set()
        for batch in results:
            if not batch:
                continue
            for item in batch:
                fp = item.ensure_fingerprint()
                if fp in seen_fp:
                    continue
                seen_fp.add(fp)
                merged.append(item)

        # 批量解析群名/人名并回填到每条 item。
        # 只解析去重后实际要展示的条目，避免对被去重的 item 浪费 API。
        # 失败优雅降级：解析不到名字不影响报告产出（保留 ID）。
        await self._resolve_names_for_items(merged, platform_id)

        digest.items = merged

        # 二次聚合：多群碎条目 → 语义主题叙事（解决 69 条平铺认知过载）。
        # 仅当条目数超过图片展示上限、且 analyzer 可用时才触发（省 LLM 成本）。
        # 失败降级：themes 留空，渲染时回退 items 平铺逻辑。
        if self._should_aggregate(len(merged)):
            digest.themes = await self._aggregate_themes(
                merged, category.name, platform_id
            )
            logger.info(
                f"分类 [{category.name}] 二次聚合: {len(merged)} 条 → "
                f"{len(digest.themes)} 个主题"
            )

        logger.info(
            f"分类 [{category.name}] 完成: 分析 {len(digest.groups_analyzed)} 群, "
            f"跳过 {len(digest.groups_skipped)}, 价值条数 {len(merged)}"
            + (
                f", 主题 {len(digest.themes)}"
                if digest.themes
                else ""
            )
        )
        return digest

    def _should_aggregate(self, item_count: int) -> bool:
        """是否触发二次聚合：analyzer 可用 + 条目数超过图片展示上限。"""
        if self._theme_analyzer is None:
            return False
        return item_count > self._get_image_max_items()

    async def _aggregate_themes(
        self,
        items: list[ValueItem],
        category_name: str,
        platform_id: str | None,
    ) -> list[DigestTheme]:
        """对多群碎条目做二次 LLM 聚合，产出主题叙事。

        流程：
        1. 给每条 item 分配 1-based 编号，序列化成 prompt 输入
        2. 复用 analysis_service.llm_semaphore 控制并发
        3. 调 theme_analyzer.analyze 拿 themes
        4. 按 related_item_ids 回填 related_items（原 ValueItem 实例）

        失败返回空列表（调用方据此降级到平铺渲染）。
        """
        if not items or self._theme_analyzer is None:
            return []

        # 序列化：每条 item → {item_id, source, content, reason}
        id_to_item: dict[int, ValueItem] = {}
        serialized: list[dict] = []
        for idx, item in enumerate(items, 1):
            id_to_item[idx] = item
            source_parts: list[str] = []
            if item.source_group_name:
                source_parts.append(item.source_group_name)
            elif item.source_group_id:
                source_parts.append(f"群{item.source_group_id}")
            if item.source_user_name:
                source_parts.append(item.source_user_name)
            serialized.append(
                {
                    "item_id": idx,
                    "source": " ".join(source_parts),
                    "content": item.content,
                    "reason": item.reason or "",
                }
            )

        trace_id = TraceContext.get()
        umo = f"category_digest:{category_name}" if category_name else "category_digest"
        session_id = _tz_now().strftime("%Y%m%d_%H%M%S")

        try:
            sem = getattr(self.analysis_service, "llm_semaphore", None)
            if sem is not None:
                async with sem:
                    themes, _usage = await self._theme_analyzer.analyze(
                        serialized, umo=umo, session_id=session_id
                    )
            else:
                themes, _usage = await self._theme_analyzer.analyze(
                    serialized, umo=umo, session_id=session_id
                )
        except Exception as e:
            logger.error(
                f"[{trace_id}] 分类 [{category_name}] 二次聚合失败，降级平铺: {e}"
            )
            return []

        # 回填 related_items：analyzer 已把 LLM 返回的编号存入 related_item_ids，
        # 此处按编号查表，挂上原 ValueItem 实例（可追溯来源群/人）。
        for theme in themes:
            theme.related_items = [
                id_to_item[item_id]
                for item_id in theme.related_item_ids
                if item_id in id_to_item
            ]
        return themes

    async def _resolve_names_for_items(
        self, items: list[ValueItem], platform_id: str | None
    ) -> None:
        """批量解析群名/人名，回填到 items 的 source_group_name/source_user_name。

        - 群名：按 group_id 去重批量解析（一次日报里同群名只查一次）
        - 人名：digest 里的 source_user_id 多半已是昵称（LLM 回填），
          仅当它是纯数字 ID 时才走 API 解析群名片
        """
        if not items:
            return
        resolver = self._name_resolver

        # 1. 群名：去重后批量
        unique_gids = list(
            {it.source_group_id for it in items if it.source_group_id}
        )
        gid_to_name = await resolver.resolve_group_name_batch(unique_gids, platform_id)

        # 2. 人名：逐个（仅纯数字 ID 需解析），并发去重
        user_keys = list({
            (it.source_group_id, it.source_user_id)
            for it in items
            if it.source_user_id and str(it.source_user_id).strip().isdigit()
        })

        async def _one(gid: str, uid: str) -> tuple[tuple[str, str], str]:
            name = await resolver.resolve_user_name(gid, uid, platform_id)
            return (gid, uid), name

        uid_results: dict[tuple[str, str], str] = {}
        if user_keys:
            pairs = await asyncio.gather(
                *[_one(g, u) for g, u in user_keys], return_exceptions=True
            )
            for p in pairs:
                if isinstance(p, Exception):
                    continue
                uid_results[p[0]] = p[1]

        # 3. 回填
        for it in items:
            if it.source_group_id:
                it.source_group_name = gid_to_name.get(it.source_group_id, "") or ""
            # 已是可读名字直接用；纯数字 ID 用解析结果
            uid = it.source_user_id
            if uid:
                if str(uid).strip().isdigit():
                    it.source_user_name = uid_results.get(
                        (it.source_group_id, uid), str(uid)
                    )
                else:
                    it.source_user_name = str(uid).strip()

    def _is_group_allowed(self, group_id: str, platform_id: str | None) -> bool:
        """分类聚合链路的群准入判定。

        by_category 时，用户已经在 categories 里显式列出该群，即视为已准入，
        不再要求在 basic.group_list 白名单里重复填写（避免三份名单割裂）。
        仅当 basic 设为黑名单模式且群在黑名单内时，才尊重黑名单（显式屏蔽优先级最高）。
        """
        gid = str(group_id).strip()
        umo = f"{platform_id}:GroupMessage:{gid}" if platform_id else gid

        mode = self.config_manager.get_group_list_mode().lower()
        if mode == "blacklist":
            # 黑名单优先级最高：显式屏蔽的群不放行（冲突由 schedule_jobs 打 warning）
            glist = [str(g).strip() for g in self.config_manager.get_group_list()]
            if glist and any(
                self.config_manager.is_group_match(umo, item) for item in glist
            ):
                return False
        # whitelist / none / 黑名单未命中：categories 显式列出即放行
        return True

    async def _extract_group_value(
        self, group_id: str, platform_id: str | None
    ) -> list[ValueItem]:
        """轻量抽取：拉消息 → 清洗 → LLM 信息差/话题 → ValueItem。"""
        adapter = self.bot_manager.get_adapter(platform_id)
        if not adapter:
            # 尝试默认/唯一平台
            adapter = self.bot_manager.get_adapter(None)
        if not adapter:
            raise ValueError(f"无可用适配器，无法分析群 {group_id}")

        days = self.config_manager.get_analysis_days()
        max_count = self.config_manager.get_max_messages()
        raw_messages = await adapter.fetch_messages(
            group_id=group_id, days=days, max_count=max_count
        )
        if not raw_messages:
            return []

        cleaner = MessageCleanerService()
        bot_self_ids = self.config_manager.get_bot_self_ids()
        unified_messages = cleaner.clean_messages(
            raw_messages, bot_self_ids=bot_self_ids, filter_commands=True
        )
        threshold = max(1, min(self.config_manager.get_min_messages_threshold(), 20))
        # 分类摘要阈值放宽：完整日报门槛过高时仍尽量抽价值
        if len(unified_messages) < threshold:
            logger.info(
                f"群 {group_id} 有效消息 {len(unified_messages)} < {threshold}，跳过抽取"
            )
            return []

        svc = self.analysis_service
        user_activity = await asyncio.to_thread(
            svc.analysis_domain_service.analyze_user_activity,
            unified_messages,
            bot_self_ids,
        )
        legacy_messages = svc.statistics_service.convert_to_legacy_dict(
            unified_messages
        )
        resolved_platform = getattr(adapter, "platform_id", platform_id)
        umo = (
            f"{resolved_platform}:GroupMessage:{group_id}"
            if resolved_platform
            else group_id
        )

        topic_enabled = self.config_manager.get_topic_analysis_enabled()
        golden_enabled = self.config_manager.get_golden_quote_analysis_enabled()
        topics, golden_quotes = [], []
        if topic_enabled or golden_enabled:
            async with svc.llm_semaphore:
                (
                    topics,
                    golden_quotes,
                    _usage,
                ) = await svc.llm_analyzer.analyze_all_concurrent(
                    legacy_messages,
                    user_activity,
                    umo=umo,
                    topic_enabled=topic_enabled,
                    golden_quote_enabled=golden_enabled,
                )

        items: list[ValueItem] = []
        for q in golden_quotes or []:
            content, reason, sender = self._coerce_quote(q)
            if not content:
                continue
            items.append(
                ValueItem(
                    content=content,
                    reason=reason,
                    source_group_id=str(group_id),
                    source_user_id=sender,
                    kind="quote",
                )
            )
        for t in topics or []:
            content, reason = self._coerce_topic(t)
            if not content:
                continue
            items.append(
                ValueItem(
                    content=content,
                    reason=reason,
                    source_group_id=str(group_id),
                    kind="topic",
                )
            )
        return items

    @staticmethod
    def _coerce_quote(q: Any) -> tuple[str, str, str]:
        if isinstance(q, dict):
            content = str(q.get("content") or q.get("quote") or "").strip()
            reason = str(q.get("reason") or "").strip()
            sender = str(q.get("sender") or q.get("user_id") or "").strip()
            return content, reason, sender
        content = str(getattr(q, "content", "") or getattr(q, "quote", "") or "").strip()
        reason = str(getattr(q, "reason", "") or "").strip()
        sender = str(
            getattr(q, "sender", "") or getattr(q, "user_id", "") or ""
        ).strip()
        return content, reason, sender

    @staticmethod
    def _coerce_topic(t: Any) -> tuple[str, str]:
        if isinstance(t, dict):
            name = str(t.get("topic") or t.get("name") or "").strip()
            detail = str(t.get("detail") or t.get("summary") or "").strip()
            if name and detail:
                return f"{name}：{detail}", "话题"
            return name or detail, "话题"
        name = str(getattr(t, "topic", "") or getattr(t, "name", "") or "").strip()
        detail = str(getattr(t, "detail", "") or getattr(t, "summary", "") or "").strip()
        if name and detail:
            return f"{name}：{detail}", "话题"
        return name or detail, "话题"

    def pack_digests(
        self,
        digests: list[CategoryDigest],
        push_mode: str = "split",
        date_str: str | None = None,
        llm_error: str | None = None,
    ) -> list[str]:
        """打包成待推送文本列表。"""
        date_str = date_str or _tz_now().strftime("%Y-%m-%d")
        mode = (push_mode or "split").strip().lower()
        if mode not in ("split", "merged"):
            mode = "split"

        non_empty = [d for d in digests if d.items]
        if not non_empty:
            # 全部空也给一条总览，避免「完全无声」；调用方也可选择不发
            empty_lines = [
                f"📋 分类日报 {date_str}",
                "",
            ]
            if llm_error:
                empty_lines.append(
                    f"⚠️ LLM 分析失败，本次结果不可信，请检查 Provider 配置/额度。\n"
                    f"失败原因：{llm_error}"
                )
            else:
                empty_lines.append("今日各分类未提取到有价值信息。")
            for d in digests:
                empty_lines.append(
                    f"- {d.name}：分析 {len(d.groups_analyzed)} 群 / 跳过 {len(d.groups_skipped)}"
                )
            return ["\n".join(empty_lines)]

        if mode == "merged":
            lines = [f"📋 分类日报 {date_str}", ""]
            for d in non_empty:
                lines.append(f"## {d.name}")
                lines.append(
                    f"（{len(d.groups_analyzed)} 群 · {len(d.items)} 条）"
                )
                lines.extend(self._format_digest_text(d))
                lines.append("")
            return ["\n".join(lines).rstrip()]

        # split
        messages: list[str] = []
        for d in non_empty:
            lines = [
                f"📋 {d.name}日报 {date_str}",
                f"（{len(d.groups_analyzed)} 群 · {len(d.items)} 条）",
                "",
            ]
            lines.extend(self._format_digest_text(d))
            messages.append("\n".join(lines).rstrip())
        return messages

    def pack_digests_markdown(
        self,
        digests: list[CategoryDigest],
        push_mode: str = "split",
        date_str: str | None = None,
        llm_error: str | None = None,
    ) -> list[tuple[str, str]]:
        """打包成 Markdown 内容列表。

        Returns:
            list of (filename, markdown_content)
            split 模式：每个分类一个 .md 文件
            merged 模式：合并成一个总 .md 文件
        """
        date_str = date_str or _tz_now().strftime("%Y-%m-%d")
        mode = (push_mode or "split").strip().lower()
        if mode not in ("split", "merged"):
            mode = "split"

        non_empty = [d for d in digests if d.items]
        if not non_empty:
            if llm_error:
                md = (
                    f"# 分类日报 {date_str}\n\n"
                    f"> ⚠️ **LLM 分析失败，本次结果不可信，请检查 Provider 配置/额度。**\n"
                    f">\n"
                    f"> 失败原因：{llm_error}\n"
                )
            else:
                md = f"# 分类日报 {date_str}\n\n今日各分类未提取到有价值信息。\n"
            return [(f"分类日报_{date_str}.md", md)]

        if mode == "merged":
            lines = [f"# 分类日报 {date_str}", ""]
            for d in non_empty:
                lines.append(f"## {d.name}")
                lines.append(
                    f"*{len(d.groups_analyzed)} 群 · {len(d.items)} 条"
                    + (f" · 聚合 {len(d.themes)} 主题" if d.themes else "")
                    + "*"
                )
                lines.append("")
                lines.extend(self._format_digest_markdown(d))
                lines.append("")
                lines.append("---")
                lines.append("")
            content = "\n".join(lines).rstrip()
            return [(f"分类日报_{date_str}.md", content)]

        # split：每个分类一个文件
        files: list[tuple[str, str]] = []
        for d in non_empty:
            safe_name = "".join(
                c for c in d.name if c.isalnum() or c in "_-"
            ) or "category"
            lines = [f"# {d.name}日报 {date_str}", ""]
            lines.append(
                f"*{len(d.groups_analyzed)} 群 · {len(d.items)} 条"
                + (f" · 聚合 {len(d.themes)} 主题" if d.themes else "")
                + "*"
            )
            lines.append("")
            lines.extend(self._format_digest_markdown(d))
            files.append(
                (f"{safe_name}日报_{date_str}.md", "\n".join(lines).rstrip())
            )
        return files

    def pack_digests_email_html(
        self,
        digests: list[CategoryDigest],
        push_mode: str = "split",
        date_str: str | None = None,
        llm_error: str | None = None,
    ) -> list[tuple[str, str]]:
        """打包成邮箱友好的 HTML 列表（带 <details> 折叠交互）。

        用户原文默认折叠，点击展开——解决「原文太多刷屏」问题。
        QQ 邮箱确认支持 <details> 标签（实测 2026-08）。

        Returns: list of (filename, html_content)
        """
        from html import escape as _esc

        date_str = date_str or _tz_now().strftime("%Y-%m-%d")
        mode = (push_mode or "split").strip().lower()
        if mode not in ("split", "merged"):
            mode = "split"

        non_empty = [d for d in digests if d.items]
        if not non_empty:
            if llm_error:
                body = (
                    f"<h1>分类日报 {date_str}</h1>"
                    f'<div style="border-left:4px solid #d93025;background:#fce8e6;'
                    f'padding:10px 14px;border-radius:4px;">'
                    f"<b>⚠️ LLM 分析失败，本次结果不可信。</b><br>"
                    f"请检查 AstrBot Provider 配置/额度。<br>"
                    f"失败原因：{_esc(llm_error)}"
                    f"</div>"
                )
            else:
                body = (
                    f"<h1>分类日报 {date_str}</h1>"
                    "<p>今日各分类未提取到有价值信息。</p>"
                )
            return [(
                f"分类日报_{date_str}.html",
                self._email_wrapper(body, date_str),
            )]

        def _build_category_html(d: CategoryDigest) -> str:
            """单个分类 → HTML 片段（主题叙事可见 + 按群二次折叠原文）。"""
            parts: list[str] = []
            meta = (
                f"{len(d.groups_analyzed)} 群 · {len(d.items)} 条"
                + (f" · 聚合 {len(d.themes)} 主题" if d.themes else "")
            )
            parts.append(f'<h2>{_esc(d.name)}</h2>')
            parts.append(f'<p class="meta">{_esc(meta)}</p>')

            # 全局编号（= digest.items 的 1-based 索引），锚点与摘要 #[n] 对应
            idx_of = {id(it): idx for idx, it in enumerate(d.items, 1)}

            def _narrative_anchored(narrative: str) -> str:
                """摘要里的 #[41] → 可点击锚点 <a href="#item-41">。"""
                if not narrative:
                    return ""
                esc = _esc(narrative)

                def _repl(m: re.Match) -> str:
                    n = int(m.group(1))
                    if 1 <= n <= len(d.items):
                        return f'<a href="#item-{n}">#{n}</a>'
                    return m.group(0)

                return re.sub(r"#\[(\d+)\]", _repl, esc)

            def _group_details(items: list, summary_label: str) -> str:
                """按群分组渲染原文折叠块；条目带全局锚点 id。"""
                groups: dict[str, list[tuple[int, Any]]] = {}
                for item in items:
                    idx = idx_of.get(id(item), 0)
                    gn = item.source_group_name or (
                        f"群{item.source_group_id}" if item.source_group_id else "未知群"
                    )
                    groups.setdefault(gn, []).append((idx, item))
                html_parts: list[str] = []
                for gn, entries in groups.items():
                    html_parts.append(
                        f'<details class="group-details">'
                        f'<summary>📎 {_esc(gn)}（{len(entries)} 条）</summary>'
                    )
                    for idx, item in entries:
                        anchor = f' id="item-{idx}"' if idx else ""
                        html_parts.append(
                            f'<div class="orig-item"{anchor}>'
                            f'{self._format_item_email(item, _esc)}</div>'
                        )
                    html_parts.append("</details>")
                if not groups:
                    html_parts.append(f"<p class=\"meta\">{_esc(summary_label)}</p>")
                return "\n".join(html_parts)

            if d.themes:
                rank_badge = {
                    "high": '<span class="badge badge-high">重要</span>',
                    "medium": '<span class="badge badge-medium">一般</span>',
                    "low": '<span class="badge badge-low">轻量</span>',
                }
                for theme in d.themes:
                    badge = rank_badge.get(theme.importance, rank_badge["medium"])
                    tags_html = (
                        " ".join(f'<span class="tag">{_esc(t)}</span>' for t in theme.tags)
                        if theme.tags
                        else ""
                    )
                    parts.append('<div class="theme">')
                    parts.append(
                        f'<div class="theme-title">{badge} {_esc(theme.title)}</div>'
                    )
                    if tags_html:
                        parts.append(f'<div class="tags">{tags_html}</div>')
                    parts.append(
                        f'<div class="narrative">{_narrative_anchored(theme.narrative)}</div>'
                    )
                    # 关联原文：按群二次折叠（不混 ID 群号）
                    if theme.related_items:
                        parts.append(_group_details(theme.related_items, "关联原文"))
                    parts.append("</div>")

                # 游离条目：按群折叠
                assigned_ids = {
                    id_ for theme in d.themes for id_ in theme.related_item_ids
                }
                orphans = [
                    item
                    for idx, item in enumerate(d.items, 1)
                    if idx not in assigned_ids
                ]
                if orphans:
                    parts.append(_group_details(orphans, "其他未归类信息"))
            else:
                # 降级：全量按群折叠（条目多时默认收起）
                parts.append(_group_details(d.items, "全部信息"))
            return "\n".join(parts)

        if mode == "merged":
            body_parts = [f"<h1>分类日报 {date_str}</h1>"]
            for d in non_empty:
                body_parts.append(_build_category_html(d))
                body_parts.append("<hr>")
            html = self._email_wrapper("\n".join(body_parts), date_str)
            return [(f"分类日报_{date_str}.html", html)]

        # split：每个分类一个文件
        files: list[tuple[str, str]] = []
        for d in non_empty:
            safe_name = "".join(
                c for c in d.name if c.isalnum() or c in "_-"
            ) or "category"
            body = f"<h1>{_esc(d.name)}日报 {date_str}</h1>"
            body += _build_category_html(d)
            html = self._email_wrapper(body, date_str)
            files.append((f"{safe_name}日报_{date_str}.html", html))
        return files

    @staticmethod
    def _format_item_email(item, _esc) -> str:
        """单个 ValueItem → 邮箱 HTML 片段（群标+人名+内容）。"""
        parts = []
        gn = item.source_group_name or (
            f"群{item.source_group_id}" if item.source_group_id else ""
        )
        if gn:
            parts.append(f'<span class="group-tag">[{_esc(gn)}]</span> ')
        if item.source_user_name:
            parts.append(f'<span class="user-name">{_esc(item.source_user_name)}:</span> ')
        parts.append(_esc(item.content))
        line = "".join(parts)
        if item.reason and item.reason != "话题":
            line += f' <span class="reason">{_esc(item.reason)}</span>'
        return line

    @staticmethod
    def _email_wrapper(body_html: str, date_str: str) -> str:
        """包上邮箱友好的 CSS 样式（内联，不依赖外部资源）。"""
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
body {{ margin:0; padding:0; background:#f5f6f8; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; color:#2c3338; }}
.container {{ max-width:680px; margin:0 auto; padding:24px 20px; }}
h1 {{ font-size:22px; color:#1a1a1a; border-bottom:2px solid #e3e6ea; padding-bottom:8px; margin:0 0 16px; }}
h2 {{ font-size:18px; color:#1a1a1a; background:#f0f4f8; padding:8px 12px; border-left:4px solid #6d9afa; border-radius:4px; margin:24px 0 8px; }}
.meta {{ color:#8b929a; font-size:13px; margin:0 0 12px; }}
.theme {{ background:#fff; border:1px solid #e0e3e8; border-radius:8px; padding:14px 16px; margin:12px 0; }}
.theme-title {{ font-size:15px; font-weight:700; color:#1a1a1a; margin-bottom:6px; }}
.narrative {{ line-height:1.7; font-size:14px; color:#3a4046; margin:6px 0 8px; }}
.tags {{ margin:4px 0; }}
.tag {{ background:#f0f1f3; color:#6b7480; padding:1px 6px; border-radius:3px; font-size:12px; margin-right:4px; }}
.badge {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 6px; border-radius:3px; margin-right:6px; }}
.badge-high {{ background:#fde8e8; color:#c53030; }}
.badge-medium {{ background:#fef3c7; color:#92400e; }}
.badge-low {{ background:#e8f0fe; color:#3a6ea5; }}
details {{ margin:8px 0; }}
details summary {{ cursor:pointer; color:#3a6ea5; font-size:13px; font-weight:600; padding:6px 0; }}
details[open] summary {{ color:#c53030; margin-bottom:6px; }}
details.group-details {{ margin:2px 0 6px; padding-left:8px; border-left:2px solid #e3e6ea; }}
details.group-details summary {{ font-size:12.5px; color:#57606a; }}
details.group-details[open] summary {{ color:#3a6ea5; }}
details.group-details .orig-item {{ margin:4px 0; }}
.orig-item a {{ color:#3a6ea5; text-decoration:none; font-weight:600; }}
.narrative a {{ color:#3a6ea5; text-decoration:none; font-weight:600; }}
.orig-item {{ background:#fafbfc; padding:8px 12px; margin:6px 0; font-size:13px; color:#4a5258; line-height:1.5; border-radius:4px; border-left:2px solid #e0e3e8; }}
.group-tag {{ background:#eef3f8; color:#3a6ea5; padding:0 5px; border-radius:3px; font-size:12px; font-weight:600; }}
.user-name {{ color:#8b5a9f; font-weight:600; }}
.reason {{ display:block; color:#8b929a; font-size:12px; margin-top:2px; font-style:italic; }}
hr {{ border:none; border-top:1px solid #e3e6ea; margin:20px 0; }}
</style>
</head><body>
<div class="container">
{body_html}
</div>
</body></html>"""

    def _format_digest_markdown(self, digest: CategoryDigest) -> list[str]:
        """单个分类的 Markdown 渲染：themes 优先，否则全量平铺。

        Markdown 源码格式，可在任何 Markdown 阅读器渲染。
        """
        lines: list[str] = []
        if digest.themes:
            rank_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            for theme in digest.themes:
                emoji = rank_emoji.get(theme.importance, "🟡")
                tags_str = (
                    " ".join(f"`{t}`" for t in theme.tags) if theme.tags else ""
                )
                lines.append(f"### {emoji} {theme.title}")
                if tags_str:
                    lines.append(tags_str)
                    lines.append("")
                lines.append(
                    ScheduledCategoryDigestService._narrative_with_sources(
                        theme.narrative, digest
                    )
                )
                lines.append("")
                if theme.related_items:
                    lines.append("**关联信息：**")
                    lines.append("")
                    for item in theme.related_items:
                        line = "- "
                        if item.source_group_name:
                            line += f"**[{item.source_group_name}]** "
                        elif item.source_group_id:
                            line += f"**[群{item.source_group_id}]** "
                        if item.source_user_name:
                            line += f"*{item.source_user_name}*: "
                        line += item.content
                        lines.append(line)
                    lines.append("")

            # 游离条目
            assigned_ids = {
                id_ for theme in digest.themes for id_ in theme.related_item_ids
            }
            orphans = [
                item
                for idx, item in enumerate(digest.items, 1)
                if idx not in assigned_ids
            ]
            if orphans:
                lines.append("### 📎 其他信息")
                lines.append("")
                for item in orphans:
                    line = "- "
                    if item.source_group_name:
                        line += f"**[{item.source_group_name}]** "
                    elif item.source_group_id:
                        line += f"**[群{item.source_group_id}]** "
                    if item.source_user_name:
                        line += f"*{item.source_user_name}*: "
                    line += item.content
                    lines.append(line)
                lines.append("")
        else:
            # 降级：全量平铺
            for i, item in enumerate(digest.items, 1):
                line = f"{i}. "
                if item.source_group_name:
                    line += f"**[{item.source_group_name}]** "
                elif item.source_group_id:
                    line += f"**[群{item.source_group_id}]** "
                if item.source_user_name:
                    line += f"*{item.source_user_name}*: "
                line += item.content
                lines.append(line)
                if item.reason and item.reason != "话题":
                    lines.append(f"   - {item.reason}")
            lines.append("")
        return lines

    def _format_digest_text(self, digest: CategoryDigest) -> list[str]:
        """单个分类的文本渲染：有 themes 走叙事+精简游离条目，否则全量平铺。

        设计取舍：核心诉求是「看得完」（脑科学 4±1 组块），所以有 themes 时
        不再堆全量附录——顶部是聚合主题叙事（精华认知），底部仅精简展示
        未被主题归并的游离条目（限量、一行一条，不展开 reason）。
        无 themes（聚合未触发/失败）时仍全量平铺，作为降级兜底。
        """
        lines: list[str] = []
        if digest.themes:
            lines.append("【今日主题】")
            rank_label = {"high": "★★★", "medium": "★★", "low": "★"}
            for theme in digest.themes:
                stars = rank_label.get(theme.importance, "★★")
                lines.append(f"{stars} {theme.title}")
                lines.append(
                    ScheduledCategoryDigestService._narrative_with_sources(
                        theme.narrative, digest
                    )
                )
                if theme.related_items:
                    for i, item in enumerate(theme.related_items, 1):
                        lines.append(self._format_single_item(item, i))
                lines.append("")

            # 游离条目：未被任何主题归并的原条目，精简展示（限量，不堆全量）
            assigned_ids = {
                id_ for theme in digest.themes for id_ in theme.related_item_ids
            }
            orphans = [
                item
                for idx, item in enumerate(digest.items, 1)
                if idx not in assigned_ids
            ]
            if orphans:
                # 软上限：游离条目超过图片展示上限时截断，标注省略数
                orphan_max = self._get_image_max_items()
                shown = orphans[:orphan_max]
                omitted = max(0, len(orphans) - orphan_max)
                lines.append("【其他信息】")
                for i, item in enumerate(shown, 1):
                    lines.append(self._format_single_item(item, i))
                if omitted:
                    lines.append(f"… 另有 {omitted} 条")
        else:
            # 回退：聚合未触发/失败时全量平铺（保证不丢信息）
            lines.extend(self._format_items(digest.items, max_items=len(digest.items)))
        return lines

    @staticmethod
    def _format_single_item(item: ValueItem, index: int) -> str:
        """单个 ValueItem → 单行文本（用于 theme 的 related_items 展示）。"""
        head = f"  {index}. "
        if item.source_group_name:
            head += f"[{item.source_group_name}] "
        elif item.source_group_id:
            head += f"[群{item.source_group_id}] "
        uname = item.source_user_name
        if uname:
            head += f"{uname}: "
        head += item.content
        return head

    def pack_digests_payload(
        self,
        digests: list[CategoryDigest],
        push_mode: str = "split",
        date_str: str | None = None,
        llm_error: str | None = None,
    ) -> list[dict[str, Any]]:
        """打包成图片模板渲染载荷列表（与 pack_digests 条数语义对齐）。"""
        date_str = date_str or _tz_now().strftime("%Y-%m-%d")
        mode = (push_mode or "split").strip().lower()
        if mode not in ("split", "merged"):
            mode = "split"

        non_empty = [d for d in digests if d.items]
        # 图片截断数：图片过长手机看费劲，仅图片走配置上限；文本另走全量
        img_max = self._get_image_max_items()
        if not non_empty:
            overview_lines = [
                f"{d.name}：分析 {len(d.groups_analyzed)} 群 / 跳过 {len(d.groups_skipped)}"
                for d in digests
            ]
            overall_meta = (
                f"LLM 分析失败：{llm_error}"
                if llm_error
                else "今日无价值信息"
            )
            meta_line = (
                "⚠️ LLM 分析失败，本次结果不可信，请检查 Provider 配置/额度。"
                if llm_error
                else "今日各分类未提取到有价值信息。"
            )
            return [
                {
                    "title": f"分类日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": overall_meta,
                    "llm_error": llm_error or "",
                    "sections": [
                        {
                            "category_name": "",
                            "meta": meta_line,
                            "entries": [
                                {
                                    "index": i,
                                    "content": line,
                                    "reason": "",
                                    "source_group_id": "",
                                }
                                for i, line in enumerate(overview_lines, 1)
                            ],
                            "omitted": 0,
                        }
                    ]
                    if overview_lines
                    else [],
                }
            ]

        if mode == "merged":
            sections = [
                self._section_from_digest(d, max_items=img_max) for d in non_empty
            ]
            total_items = sum(len(d.items) for d in non_empty)
            total_themes = sum(len(d.themes) for d in non_empty)
            meta = (
                f"{len(non_empty)} 个分类 · {total_items} 条 → 聚合 {total_themes} 主题"
                if total_themes
                else f"{len(non_empty)} 个分类 · {total_items} 条"
            )
            return [
                {
                    "title": f"分类日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": meta,
                    "sections": sections,
                }
            ]

        # split：每个非空分类一张图
        payloads: list[dict[str, Any]] = []
        for d in non_empty:
            meta = (
                f"{len(d.groups_analyzed)} 群 · {len(d.items)} 条 → 聚合 {len(d.themes)} 主题"
                if d.themes
                else f"{len(d.groups_analyzed)} 群 · {len(d.items)} 条"
            )
            payloads.append(
                {
                    "title": f"{d.name}日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": meta,
                    "sections": [
                        self._section_from_digest(
                            d, show_name=False, max_items=img_max
                        )
                    ],
                }
            )
        return payloads

    def _get_image_max_items(self) -> int:
        """读取图片渲染的条目上限，配置缺失时回退到默认值。"""
        getter = getattr(
            self.config_manager, "get_digest_max_items_per_section", None
        )
        if callable(getter):
            try:
                return int(getter())
            except Exception:
                pass
        return _MAX_ITEMS_PER_SECTION

    @staticmethod
    def _serialize_entry(item: ValueItem, index: int) -> dict[str, Any]:
        """单个 ValueItem → 模板渲染用的 entry dict。"""
        return {
            "index": index,
            "content": item.content,
            "reason": item.reason or "",
            # 渲染优先用可读名，缺失才退回 ID；不再只塞纯数字 group_id
            "source_group_id": item.source_group_id or "",
            "source_group_name": item.source_group_name or "",
            "source_user_name": item.source_user_name or "",
        }

    @staticmethod
    def _narrative_with_sources(narrative: str, digest: CategoryDigest) -> str:
        """把摘要文本里的内部编号 #[41] 替换成短来源标签 [群名·人名]。

        编号是给 LLM 归并用的内部引用，读者看到「#[41]」无法跳转、很怪。
        各渲染出口（图片/邮件/文本/Markdown）统一用它替换成可读来源。
        群名截 8 字、人名截 6 字，超长省略号。
        """
        if not narrative or not digest.items:
            return narrative
        id_to_item = {idx: item for idx, item in enumerate(digest.items, 1)}

        def _short(s: str, limit: int) -> str:
            s = (s or "").strip()
            return s if len(s) <= limit else s[:limit] + "…"

        def _repl(m: re.Match) -> str:
            item = id_to_item.get(int(m.group(1)))
            if not item:
                return m.group(0)
            g = _short(
                item.source_group_name or item.source_group_id or "",
                _SOURCE_TAG_GROUP_MAX,
            )
            u = _short(item.source_user_name or "", _SOURCE_TAG_USER_MAX)
            label = f"{g}·{u}" if g and u else (g or u or "群")
            return f"[{label}]"

        return re.sub(r"#\[(\d+)\]", _repl, narrative)

    @staticmethod
    def _section_from_digest(
        digest: CategoryDigest, show_name: bool = True, max_items: int = _MAX_ITEMS_PER_SECTION
    ) -> dict[str, Any]:
        # themes 优先（聚合后认知负荷低）；为空时回退 entries 平铺（降级/未触发聚合）
        if digest.themes:
            # 游离条目：未被任何主题归并的原条目（信息完整性兜底）
            assigned_ids = {
                id_
                for theme in digest.themes
                for id_ in theme.related_item_ids
            }
            # related_item_ids 是 1-based，对应 digest.items 的索引+1
            orphans = [
                item
                for idx, item in enumerate(digest.items, 1)
                if idx not in assigned_ids
            ]
            section: dict[str, Any] = {
                "category_name": digest.name if show_name else "",
                "meta": (
                    f"{len(digest.groups_analyzed)} 群 · {len(digest.items)} 条 "
                    f"→ 聚合 {len(digest.themes)} 主题"
                ),
                "themes": [
                    {
                        "title": theme.title,
                        "narrative": ScheduledCategoryDigestService._narrative_with_sources(
                            theme.narrative, digest
                        ),
                        "importance": theme.importance,
                        "tags": theme.tags or [],
                        # 图片版不展示原文（原文在邮件/Markdown），entries 置空，
                        # 模板据此跳过 related 渲染 —— 避免图里塞 100 条原文
                        "entries": [],
                    }
                    for theme in digest.themes
                ],
                "entries": [],  # 模板里 themes 非空时不渲染 entries
                "orphans": orphans[:max_items],
                "omitted": max(0, len(orphans) - max_items),
            }
            return section

        # 回退：平铺条目（聚合未触发或失败）
        items = digest.items[:max_items]
        omitted = max(0, len(digest.items) - max_items)
        return {
            "category_name": digest.name if show_name else "",
            "meta": f"{len(digest.groups_analyzed)} 群 · {len(digest.items)} 条",
            "themes": [],
            "entries": [
                ScheduledCategoryDigestService._serialize_entry(item, i)
                for i, item in enumerate(items, 1)
            ],
            "orphans": [],
            "omitted": omitted,
        }

    @staticmethod
    def _format_items(items: list[ValueItem], max_items: int = 12) -> list[str]:
        lines: list[str] = []
        for i, item in enumerate(items[:max_items], 1):
            head = f"{i}. "
            # 群标：优先群名，否则群号；前缀「群」字仅在退回 ID 时加（群名自带语义）
            if item.source_group_name:
                head += f"[{item.source_group_name}] "
            elif item.source_group_id:
                head += f"[群{item.source_group_id}] "
            # 人名：有则加（「人:」形式），无则省略
            uname = item.source_user_name
            if uname:
                head += f"{uname}: "
            head += item.content
            lines.append(head)
            if item.reason and item.reason not in ("话题",):
                lines.append(f"   ↳ {item.reason}")
        if len(items) > max_items:
            lines.append(f"… 另有 {len(items) - max_items} 条已省略")
        return lines

    @staticmethod
    def _make_image_caption() -> str:
        """分类摘要图片 caption，保留 | 时间戳格式便于去重。"""
        return f"📊 分类情报摘要已生成 | {_tz_now().strftime('%m-%d %H:%M:%S')}"

    async def _send_markdown_files(
        self,
        digests: list[CategoryDigest],
        push_mode: str,
        platform_id: str | None,
    ) -> int:
        """生成 Markdown 文件 + HTML 邮件，发给管理员。

        两条通道并行（互不影响）：
        - QQ 私聊：发 .md 文件（用 adapter.send_private_file）
        - SMTP 邮件：发 HTML 折叠邮件（用户原文默认收起，点击展开）+ .md 附件

        失败不影响主推送（图片已发），只打 warning。
        """
        trace_id = TraceContext.get()
        sent = 0

        # ---- QQ 私聊发 .md 文件 ----
        try:
            md_files = self.pack_digests_markdown(digests, push_mode=push_mode)
        except Exception as e:
            logger.warning(f"[{trace_id}] Markdown 打包失败: {e}")
            md_files = []

        if md_files:
            qq_sent = await self._send_md_via_qq(md_files, platform_id)
            sent += qq_sent

        # ---- SMTP 发 HTML 邮件 ----
        try:
            email_sent = await self._send_digest_email(digests, push_mode)
            sent += email_sent
        except Exception as e:
            logger.warning(f"[{trace_id}] 邮件发送失败: {e}")

        return sent

    async def _send_md_via_qq(
        self,
        md_files: list[tuple[str, str]],
        platform_id: str | None,
    ) -> int:
        """通过 QQ 私聊发 .md 文件。

        合并转发开启时跳过（md 已作为文本节点塞进合并转发卡片，避免重复发送刷屏）；
        合并转发未开启/失败降级时才走单独发文件兜底。
        """
        trace_id = TraceContext.get()
        if self._should_use_forward_msg():
            # md 已由 _send_as_forward 作为文本节点带入合并转发卡片，不再单独发文件
            logger.info(
                f"[{trace_id}] 合并转发已开启，Markdown 走卡片文本节点，跳过单独发文件"
            )
            return 0
        # 找支持私聊发文件的 adapter
        adapter = self.bot_manager.get_adapter(platform_id)
        if not adapter or not hasattr(adapter, "send_private_file"):
            for pid in getattr(self.bot_manager, "get_platform_ids", lambda: [])():
                a = self.bot_manager.get_adapter(pid)
                if a and hasattr(a, "send_private_file"):
                    adapter = a
                    break
        if not adapter or not hasattr(adapter, "send_private_file"):
            logger.warning(f"[{trace_id}] 无支持私聊发文件的 adapter，Markdown 未发")
            return 0

        admin_qqs = resolve_admin_qqs(
            self.bot_manager,
            self.config_manager.get_extra_admin_qqs(),
        )
        if not admin_qqs:
            return 0

        import tempfile

        sent = 0
        for filename, content in md_files:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".md",
                    delete=False,
                    encoding="utf-8",
                    prefix="digest_",
                ) as f:
                    f.write(content)
                    tmp_path = f.name

                for qq in admin_qqs:
                    try:
                        ok = await adapter.send_private_file(
                            user_id=qq, file_path=tmp_path, filename=filename
                        )
                        if ok:
                            sent += 1
                            logger.info(
                                f"[{trace_id}] Markdown 文件已发: {qq} ← {filename}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[{trace_id}] Markdown 文件发送失败 {qq}: {e}"
                        )
            finally:
                if tmp_path:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception:
                        pass
        return sent

    async def _send_digest_email(
        self,
        digests: list[CategoryDigest],
        push_mode: str,
    ) -> int:
        """SMTP 发 HTML 折叠邮件（用户原文默认收起）+ .md 附件。

        配置不完整/未开启时静默跳过（返回 0）。
        """
        trace_id = TraceContext.get()
        from ...infrastructure.messaging.email_sender import build_email_sender_from_config

        sender = build_email_sender_from_config(self.config_manager)
        if sender is None:
            # 邮件未配置或未开启，静默跳过
            return 0

        recipients = []
        if hasattr(self.config_manager, "get_digest_email_recipients"):
            recipients = self.config_manager.get_digest_email_recipients()
        if not recipients:
            logger.info(f"[{trace_id}] 邮件未配置收件人，跳过")
            return 0

        date_str = _tz_now().strftime("%Y-%m-%d")
        try:
            html_files = self.pack_digests_email_html(digests, push_mode=push_mode, date_str=date_str)
            md_files = self.pack_digests_markdown(digests, push_mode=push_mode, date_str=date_str)
        except Exception as e:
            logger.warning(f"[{trace_id}] 邮件内容打包失败: {e}")
            return 0

        if not html_files:
            return 0

        # md 附件（merged 取第 1 个汇总文件；split 取全部）
        attachments: list[tuple[str, str]] = []
        if push_mode == "merged" and md_files:
            attachments.append(md_files[0])
        else:
            attachments.extend(md_files)

        sent = 0
        for html_filename, html_body in html_files:
            subject = html_filename.replace(".html", "")
            ok = await sender.send_html(
                to=recipients,
                subject=f"📊 {subject}",
                html_body=html_body,
                attachments=attachments,
            )
            if ok:
                sent += 1
                logger.info(
                    f"[{trace_id}] 邮件已发: {recipients} ← {subject} "
                    f"({len(attachments)} 附件)"
                )
        return sent

    async def _send_to_admins(
        self,
        deliveries: list[tuple[str | None, str]] | list[str],
        platform_id: str | None,
        md_contents: list[tuple[str, str]] | None = None,
    ) -> int:
        """私聊管理员发送 digest。

        优先级：
        1. 若开启合并转发且有多条图片 → 打包成一条合并转发卡片（解决刷屏），
           md_contents 会作为文本节点追加在图片节点后
        2. 合并转发失败/未开启/单条 → 降级到逐条发送（图片优先，失败回退文本），
           此路径下 md_contents 不会被发送（由调用方决定是否单独发文件）

        deliveries 支持：
        - list[tuple[image_url|None, text]]：图片优先，失败回退文本
        - list[str]：纯文本（向后兼容）
        """
        trace_id = TraceContext.get()
        adapter = self.bot_manager.get_adapter(platform_id)
        if not adapter or not hasattr(adapter, "send_private"):
            # 遍历找支持私聊的 adapter
            for pid in getattr(self.bot_manager, "get_platform_ids", lambda: [])():
                a = self.bot_manager.get_adapter(pid)
                if a and hasattr(a, "send_private"):
                    adapter = a
                    break
        if not adapter or not hasattr(adapter, "send_private"):
            logger.error(f"[{trace_id}] 分类摘要推送失败：无支持私聊的 adapter")
            return 0

        admin_qqs = resolve_admin_qqs(
            self.bot_manager,
            self.config_manager.get_extra_admin_qqs(),
        )
        if not admin_qqs:
            logger.warning(f"[{trace_id}] 无管理员 QQ，分类摘要未发出")
            return 0

        # 归一化为 (image_url|None, text)
        normalized: list[tuple[str | None, str]] = []
        for item in deliveries:
            if isinstance(item, tuple) and len(item) == 2:
                normalized.append((item[0], item[1]))
            else:
                normalized.append((None, str(item)))

        # 尝试合并转发：多条图片 + adapter 支持 + 配置开启
        if (
            self._should_use_forward_msg()
            and len(normalized) > 1
            and hasattr(adapter, "send_private_forward_msg")
            and all(img for img, _ in normalized)
        ):
            sent = await self._send_as_forward(
                adapter, normalized, admin_qqs, trace_id,
                md_contents=md_contents,
            )
            if sent > 0:
                return sent
            # 合并转发全员失败 → 降级逐条发
            logger.warning(
                f"[{trace_id}] 合并转发失败，降级为逐条发送"
            )

        # 逐条发送（降级路径 / 单条 / 合并转发未开启）
        caption = self._make_image_caption()
        sent = 0
        for image_url, text in normalized:
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
                            ok = await adapter.send_private(user_id=qq, text=text)
                    else:
                        ok = await adapter.send_private(user_id=qq, text=text)
                    if ok:
                        sent += 1
                    else:
                        logger.warning(f"[{trace_id}] 私聊 {qq} 分类摘要失败")
                except Exception as e:
                    logger.error(f"[{trace_id}] 私聊 {qq} 异常: {e}")
                    # 异常时再尝试纯文本，尽量保证推送不丢
                    try:
                        if await adapter.send_private(user_id=qq, text=text):
                            sent += 1
                            logger.info(f"[{trace_id}] 异常后文本回退成功: {qq}")
                    except Exception as e2:
                        logger.error(f"[{trace_id}] 文本回退也失败 {qq}: {e2}")
        logger.info(
            f"[{trace_id}] 分类摘要推送完成: {len(normalized)} 条消息 × "
            f"{len(admin_qqs)} 人, 成功 {sent}"
        )
        return sent

    def _should_use_forward_msg(self) -> bool:
        """是否优先用合并转发发送分类摘要（多条图打包成卡片，避免刷屏）。"""
        getter = getattr(self.config_manager, "is_digest_use_forward_msg", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                pass
        return True

    async def _send_as_forward(
        self,
        adapter,
        normalized: list[tuple[str | None, str]],
        admin_qqs: list[str],
        trace_id: str,
        md_contents: list[tuple[str, str]] | None = None,
    ) -> int:
        """把多条图片打包成合并转发卡片，逐个管理员发送。

        节点结构：标题节点 + N 个分类图片节点 + M 个 md 文本节点（可选）。
        md 文本节点追加在图片节点后，让"想看原始文本"的人能在卡片里直接展开，
        不再单独发 .md 文件（避免刷屏）。
        任一管理员发送成功即计入 sent；某管理员失败则该员整体降级（由调用方重试逐条）。
        """
        # bot self_id 用于节点 uin（合并转发要求）
        bot_self_ids = getattr(adapter, "bot_self_ids", []) or []
        bot_uin = bot_self_ids[0] if bot_self_ids else "10000"

        # 构建节点：1 个标题节点 + N 个分类图片节点
        caption = self._make_image_caption()
        nodes: list[dict] = [
            {
                "type": "node",
                "data": {
                    "name": "分类日报",
                    "uin": bot_uin,
                    "content": [{"type": "text", "data": {"text": caption}}],
                },
            }
        ]
        for image_url, _text in normalized:
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": "分类摘要",
                        "uin": bot_uin,
                        "content": [
                            {"type": "image", "data": {"file": image_url}}
                        ],
                    },
                }
            )

        # md 文本节点追加在图片节点后（每个分类的 md 全文作为一个 text 节点）
        # 让"想看原始文本"的用户能在合并转发卡片里直接展开，不再单独发 .md 文件
        if md_contents:
            for md_name, md_text in md_contents:
                # 节点名用文件名（去 .md 后缀，更整洁）
                node_name = md_name[:-3] if md_name.lower().endswith(".md") else md_name
                nodes.append(
                    {
                        "type": "node",
                        "data": {
                            "name": node_name,
                            "uin": bot_uin,
                            "content": [
                                {"type": "text", "data": {"text": md_text}}
                            ],
                        },
                    }
                )

        sent = 0
        for qq in admin_qqs:
            try:
                ok = await adapter.send_private_forward_msg(
                    user_id=qq, nodes=nodes
                )
                if ok:
                    sent += 1
                    logger.info(f"[{trace_id}] 合并转发成功: {qq} ({len(nodes)} 节点)")
                else:
                    logger.warning(f"[{trace_id}] 合并转发失败: {qq}")
            except Exception as e:
                logger.error(f"[{trace_id}] 合并转发异常 {qq}: {e}")
        return sent

    async def _persist_digests(
        self,
        digests: list[CategoryDigest],
        deliveries: list[tuple[str | None, str]],
        sent_count: int,
        llm_error: str | None = None,
    ) -> str | None:
        """持久化 digest 数据到磁盘，用于重渲染/重推。

        Args:
            digests: 分类摘要列表
            deliveries: (image_url|None, text) 元组列表
            sent_count: 成功发送数量
            llm_error: 结果全空时的 LLM 探活失败原因（None=正常）

        Returns:
            落盘文件路径，失败返回 None
        """
        trace_id = TraceContext.get()
        try:
            timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
            filename = f"digest_{timestamp}.json"
            filepath = self._digests_dir / filename

            # 序列化 digests（含 themes，供重渲染/重推复用）
            digest_data = {
                "timestamp": _tz_now().isoformat(),
                "llm_error": llm_error,
                "digests": [
                    {
                        "name": d.name,
                        "items": [
                            {
                                "content": item.content,
                                "reason": item.reason,
                                "source_group_id": item.source_group_id,
                                "source_user_id": item.source_user_id,
                                "source_group_name": item.source_group_name,
                                "source_user_name": item.source_user_name,
                                "kind": item.kind,
                                "fingerprint": item.fingerprint,
                            }
                            for item in d.items
                        ],
                        "themes": [
                            {
                                "title": t.title,
                                "narrative": t.narrative,
                                "importance": t.importance,
                                "tags": t.tags,
                                "related_item_ids": t.related_item_ids,
                            }
                            for t in d.themes
                        ],
                        "groups_analyzed": d.groups_analyzed,
                        "groups_skipped": d.groups_skipped,
                    }
                    for d in digests
                ],
                "deliveries": [
                    {"image_url": img, "text": txt}
                    for img, txt in deliveries
                ],
                "sent_count": sent_count,
            }

            # 异步写入文件
            def _write():
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(digest_data, f, ensure_ascii=False, indent=2)

            await asyncio.to_thread(_write)
            logger.info(f"[{trace_id}] Digest 落盘成功: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"[{trace_id}] Digest 落盘失败: {e}")
            return None

    def _cleanup_old_digests(self) -> None:
        """清理过期的 digest 文件。"""
        trace_id = TraceContext.get()
        try:
            cutoff_date = datetime.now() - timedelta(days=self._digest_retain_days)
            deleted_count = 0

            for filepath in self._digests_dir.glob("digest_*.json"):
                try:
                    # 从文件名解析时间戳
                    # 格式: digest_YYYYMMDD_HHMMSS.json
                    parts = filepath.stem.split("_")
                    if len(parts) >= 3:
                        date_str = f"{parts[1]}_{parts[2]}"
                        file_time = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
                        if file_time < cutoff_date:
                            filepath.unlink()
                            deleted_count += 1
                except Exception as e:
                    logger.warning(
                        f"[{trace_id}] 解析或删除 digest 文件失败 {filepath}: {e}"
                    )

            if deleted_count > 0:
                logger.info(
                    f"[{trace_id}] 清理过期 digest 文件: {deleted_count} 个"
                )

        except Exception as e:
            logger.error(f"[{trace_id}] 清理过期 digest 文件失败: {e}")
