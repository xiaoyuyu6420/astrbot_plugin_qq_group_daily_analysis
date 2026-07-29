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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.entities.push_category import PushCategory
from ...domain.services.message_cleaner_service import MessageCleanerService
from ...shared.fingerprint import content_fingerprint
from ...shared.timezone import now as _tz_now
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from ...infrastructure.utils.admin_resolver import resolve_admin_qqs

# 分类摘要单张图最多展示的条目数（与 _format_items 默认一致）
_MAX_ITEMS_PER_SECTION = 12


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
        # 委托给 shared.fingerprint，与降噪层/分层聚合统一（32 字符 + 去标点）
        self.fingerprint = content_fingerprint(self.content)
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

        text_messages = self.pack_digests(digests, push_mode=push_mode)
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
                digests, text_messages, push_mode=push_mode
            )
        else:
            deliveries = [(None, msg) for msg in text_messages]

        sent = await self._send_to_admins(deliveries, platform_id=platform_id)
        # 落盘：把本次 digest 存到磁盘，失败也不影响推送结果
        digest_path = await self._persist_digests(digests, deliveries, sent)
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

    async def _render_image_deliveries(
        self,
        digests: list[CategoryDigest],
        text_messages: list[str],
        push_mode: str,
    ) -> list[tuple[str | None, str]]:
        """按 payload 渲染图片，失败则对应位置 image_url 为 None（走文本兜底）。"""
        trace_id = TraceContext.get()
        payloads = self.pack_digests_payload(digests, push_mode=push_mode)
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
        # 若 payload 更少，剩余纯文本；若文本更少，多余 payload 忽略
        for payload, text in pairs:
            try:
                image_url, _html = await self.report_generator.render_category_digest_image(
                    payload,
                    self.html_render_func,
                    context_label=f"category:{payload.get('title', '')}",
                )
            except Exception as e:
                logger.error(f"[{trace_id}] 分类摘要图片渲染异常: {e}")
                image_url = None
            deliveries.append((image_url, text))

        # 文本比 payload 多的部分纯文本补齐
        if len(text_messages) > len(payloads):
            for text in text_messages[len(payloads) :]:
                deliveries.append((None, text))

        ok_imgs = sum(1 for img, _ in deliveries if img)
        logger.info(
            f"[{trace_id}] 分类摘要图片渲染完成: {ok_imgs}/{len(deliveries)} 成功"
        )
        return deliveries

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

    def pack_digests_payload(
        self,
        digests: list[CategoryDigest],
        push_mode: str = "split",
        date_str: str | None = None,
    ) -> list[dict[str, Any]]:
        """打包成图片模板渲染载荷列表（与 pack_digests 条数语义对齐）。"""
        date_str = date_str or _tz_now().strftime("%Y-%m-%d")
        mode = (push_mode or "split").strip().lower()
        if mode not in ("split", "merged"):
            mode = "split"

        non_empty = [d for d in digests if d.items]
        if not non_empty:
            overview_lines = [
                f"{d.name}：分析 {len(d.groups_analyzed)} 群 / 跳过 {len(d.groups_skipped)}"
                for d in digests
            ]
            return [
                {
                    "title": f"分类日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": "今日无价值信息",
                    "sections": [
                        {
                            "category_name": "",
                            "meta": "今日各分类未提取到有价值信息。",
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
            sections = [self._section_from_digest(d) for d in non_empty]
            total_items = sum(len(d.items) for d in non_empty)
            return [
                {
                    "title": f"分类日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": (
                        f"{len(non_empty)} 个分类 · {total_items} 条"
                    ),
                    "sections": sections,
                }
            ]

        # split：每个非空分类一张图
        payloads: list[dict[str, Any]] = []
        for d in non_empty:
            payloads.append(
                {
                    "title": f"{d.name}日报 {date_str}",
                    "date_str": date_str,
                    "overall_meta": (
                        f"{len(d.groups_analyzed)} 群 · {len(d.items)} 条"
                    ),
                    "sections": [self._section_from_digest(d, show_name=False)],
                }
            )
        return payloads

    @staticmethod
    def _section_from_digest(
        digest: CategoryDigest, show_name: bool = True, max_items: int = _MAX_ITEMS_PER_SECTION
    ) -> dict[str, Any]:
        items = digest.items[:max_items]
        omitted = max(0, len(digest.items) - max_items)
        return {
            "category_name": digest.name if show_name else "",
            "meta": f"{len(digest.groups_analyzed)} 群 · {len(digest.items)} 条",
            "entries": [
                {
                    "index": i,
                    "content": item.content,
                    "reason": item.reason or "",
                    "source_group_id": item.source_group_id or "",
                }
                for i, item in enumerate(items, 1)
            ],
            "omitted": omitted,
        }

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

    @staticmethod
    def _make_image_caption() -> str:
        """分类摘要图片 caption，保留 | 时间戳格式便于去重。"""
        return f"📊 分类情报摘要已生成 | {_tz_now().strftime('%m-%d %H:%M:%S')}"

    async def _send_to_admins(
        self,
        deliveries: list[tuple[str | None, str]] | list[str],
        platform_id: str | None,
    ) -> int:
        """私聊管理员发送 digest。

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

    async def _persist_digests(
        self,
        digests: list[CategoryDigest],
        deliveries: list[tuple[str | None, str]],
        sent_count: int,
    ) -> str | None:
        """持久化 digest 数据到磁盘，用于重渲染/重推。

        Args:
            digests: 分类摘要列表
            deliveries: (image_url|None, text) 元组列表
            sent_count: 成功发送数量

        Returns:
            落盘文件路径，失败返回 None
        """
        trace_id = TraceContext.get()
        try:
            timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
            filename = f"digest_{timestamp}.json"
            filepath = self._digests_dir / filename

            # 序列化 digests
            digest_data = {
                "timestamp": _tz_now().isoformat(),
                "digests": [
                    {
                        "name": d.name,
                        "items": [
                            {
                                "content": item.content,
                                "reason": item.reason,
                                "source_group_id": item.source_group_id,
                                "source_user_id": item.source_user_id,
                                "kind": item.kind,
                                "fingerprint": item.fingerprint,
                            }
                            for item in d.items
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
