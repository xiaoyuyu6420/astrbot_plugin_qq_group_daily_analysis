"""
定时分类聚合摘要服务

只服务 delivery_mode=by_category 的定时链路：
按用户分类（科技/AI/自定义）下挂的群列表，抽取当日有价值信息，
打包成文本 digest 私聊管理员。

与实时监控的分层聚合 / 内容频道无关。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ...domain.entities.push_category import PushCategory
from ...domain.services.message_cleaner_service import MessageCleanerService
from ...shared.timezone import now as _tz_now
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ...infrastructure.utils.admin_resolver import resolve_admin_qqs


@dataclass
class ValueItem:
    """分类摘要中的一条价值信息。"""

    content: str
    reason: str = ""
    source_group_id: str = ""
    source_user_id: str = ""
    kind: str = "info"  # quote | topic | info
    fingerprint: str = ""

    def ensure_fingerprint(self) -> str:
        if self.fingerprint:
            return self.fingerprint
        norm = re.sub(r"\s+", "", (self.content or "").lower())
        self.fingerprint = hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return self.fingerprint


@dataclass
class CategoryDigest:
    """一个用户分类的摘要结果。"""

    name: str
    items: list[ValueItem] = field(default_factory=list)
    groups_analyzed: list[str] = field(default_factory=list)
    groups_skipped: list[str] = field(default_factory=list)


class ScheduledCategoryDigestService:
    """定时分类聚合：抽价值 → 合并去重 → split/merged 打包 → 私聊管理员。"""

    def __init__(
        self,
        config_manager: Any,
        analysis_service: Any,
        bot_manager: Any,
        report_dispatcher: Any | None = None,
    ):
        self.config_manager = config_manager
        self.analysis_service = analysis_service
        self.bot_manager = bot_manager
        self.report_dispatcher = report_dispatcher

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
        max_concurrent = self.config_manager.get_max_concurrent_tasks() or 3
        stagger = self.config_manager.get_stagger_seconds() or 2

        logger.info(
            f"[{trace_id}] 分类聚合定时开始: {len(categories)} 个分类, "
            f"push_mode={push_mode}, concurrent={max_concurrent}"
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

        messages = self.pack_digests(digests, push_mode=push_mode)
        if not messages:
            logger.info(f"[{trace_id}] 分类聚合无有效内容，不推送")
            return {
                "success": True,
                "reason": "no_value",
                "digests": digests,
                "messages_sent": 0,
            }

        sent = await self._send_to_admins(messages, platform_id=platform_id)
        return {
            "success": sent > 0,
            "digests": digests,
            "messages_sent": sent,
            "message_count": len(messages),
        }

    async def _build_category_digest(
        self,
        category: PushCategory,
        platform_id: str | None,
        max_concurrent: int,
        stagger: float,
    ) -> CategoryDigest:
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

        digest.items = merged
        logger.info(
            f"分类 [{category.name}] 完成: 分析 {len(digest.groups_analyzed)} 群, "
            f"跳过 {len(digest.groups_skipped)}, 价值条数 {len(merged)}"
        )
        return digest

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
                self.config_manager._is_group_match(umo, item) for item in glist
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
        legacy_messages = svc.statistics_service._convert_to_legacy_dict(
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
                "今日各分类未提取到有价值信息。",
            ]
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
                lines.extend(self._format_items(d.items))
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
            lines.extend(self._format_items(d.items))
            messages.append("\n".join(lines).rstrip())
        return messages

    @staticmethod
    def _format_items(items: list[ValueItem], max_items: int = 12) -> list[str]:
        lines: list[str] = []
        for i, item in enumerate(items[:max_items], 1):
            head = f"{i}. "
            if item.source_group_id:
                head += f"[群{item.source_group_id}] "
            head += item.content
            lines.append(head)
            if item.reason and item.reason not in ("话题",):
                lines.append(f"   ↳ {item.reason}")
        if len(items) > max_items:
            lines.append(f"… 另有 {len(items) - max_items} 条已省略")
        return lines

    async def _send_to_admins(
        self, messages: list[str], platform_id: str | None
    ) -> int:
        """私聊管理员发送文本 digest。"""
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

        sent = 0
        for msg in messages:
            for qq in admin_qqs:
                try:
                    ok = await adapter.send_private(user_id=qq, text=msg)
                    if ok:
                        sent += 1
                    else:
                        logger.warning(f"[{trace_id}] 私聊 {qq} 分类摘要失败")
                except Exception as e:
                    logger.error(f"[{trace_id}] 私聊 {qq} 异常: {e}")
        logger.info(
            f"[{trace_id}] 分类摘要推送完成: {len(messages)} 条消息 × "
            f"{len(admin_qqs)} 人, 成功 {sent}"
        )
        return sent
