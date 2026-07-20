"""
分层跨群聚合（Phase 1）

L1: 每群本地提炼 candidates（正则/规则，默认不调 LLM）
L2: 跨群指纹去重 + 按 channel 聚合 + 预算截断
ChannelPacker: split/merged 打成推送文案

设计目标：50+ 群时不全量原始聊天塞一次 LLM；critical 不丢。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from ...domain.entities.intel_item import IntelItem
from ...domain.services.intel_taxonomy import (
    ALL_CHANNELS,
    CHANNEL_OTHER,
    DEFAULT_ENABLED_CHANNELS,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    best_priority,
    channel_display,
    channel_emoji,
    channel_priority,
    normalize_channel,
    priority_rank,
)


def content_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", "", text or "").lower()
    normalized = re.sub(r"[^\w]", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:32]


@dataclass
class PatternHit:
    category: str
    description: str
    channel: str
    priority: str


@dataclass
class AggregationResult:
    items: list[IntelItem] = field(default_factory=list)
    dropped_duplicates: int = 0
    group_count: int = 0
    source_message_count: int = 0
    candidate_count: int = 0


class GroupCandidateExtractor:
    """L1：从单群窗口消息中提取 candidates（无 LLM）。"""

    def __init__(
        self,
        patterns: list[tuple[re.Pattern, str, str]],
        max_candidates_per_group: int = 5,
    ):
        self.patterns = patterns
        self.max_candidates_per_group = max(1, int(max_candidates_per_group))

    def extract(
        self,
        group_id: str,
        messages: list[dict],
        watched_user_ids: set[str] | None = None,
        extra_keywords: list[str] | None = None,
    ) -> list[IntelItem]:
        if not messages:
            return []

        watched = watched_user_ids or set()
        keywords = [k for k in (extra_keywords or []) if k]
        items: list[IntelItem] = []
        seen_fp: set[str] = set()

        for msg in messages:
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            sender_id = str(msg.get("sender_id") or "")
            # 有监控名单时：优先目标用户；但 critical 正则对所有人开放（防漏密钥）
            hits = self._match_patterns(text)
            kw_hit = self._match_keywords(text, keywords)
            if not hits and not kw_hit:
                continue
            if watched and sender_id not in watched:
                # 非目标用户：仅保留 critical
                if not any(h.priority == PRIORITY_CRITICAL for h in hits):
                    continue

            channel, priority, raw_cat, reason = self._resolve_label(hits, kw_hit)
            if channel == CHANNEL_OTHER and priority == PRIORITY_LOW and not hits:
                # 纯关键词命中但映射 other：仍保留为 normal 线索
                channel = CHANNEL_OTHER
                priority = PRIORITY_NORMAL
                reason = reason or "命中自定义关键词"

            fp = content_fingerprint(text)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)

            items.append(
                IntelItem(
                    content=text if len(text) <= 500 else text[:500] + "…",
                    channel=channel,
                    priority=priority,
                    reason=reason,
                    source_user_id=sender_id,
                    source_user_name=str(msg.get("name") or ""),
                    source_group_id=group_id,
                    platform_id=str(msg.get("platform_id") or ""),
                    fingerprint=fp,
                    raw_category=raw_cat,
                )
            )

        # 预算：critical 优先，再 normal
        items.sort(key=lambda x: (priority_rank(x.priority), -len(x.content)))
        return items[: self.max_candidates_per_group]

    def _match_patterns(self, text: str) -> list[PatternHit]:
        hits: list[PatternHit] = []
        for pattern, category, desc in self.patterns:
            if pattern.search(text):
                channel = normalize_channel(category)
                hits.append(
                    PatternHit(
                        category=category,
                        description=desc,
                        channel=channel,
                        priority=channel_priority(channel),
                    )
                )
        return hits

    @staticmethod
    def _match_keywords(text: str, keywords: list[str]) -> str | None:
        lower = text.lower()
        for kw in keywords:
            if kw and kw.lower() in lower:
                return kw
        return None

    @staticmethod
    def _resolve_label(
        hits: list[PatternHit], kw_hit: str | None
    ) -> tuple[str, str, str, str]:
        if hits:
            # 取最高优先级 hit
            hits_sorted = sorted(hits, key=lambda h: priority_rank(h.priority))
            best = hits_sorted[0]
            # 若存在 critical 强制用 critical 那条
            for h in hits_sorted:
                if h.priority == PRIORITY_CRITICAL:
                    best = h
                    break
            return best.channel, best.priority, best.category, best.description
        if kw_hit:
            return CHANNEL_OTHER, PRIORITY_NORMAL, "关键词", f"命中关键词「{kw_hit}」"
        return CHANNEL_OTHER, PRIORITY_LOW, "其他", ""


class CrossGroupAggregator:
    """L2：跨群 candidates 去重 + 分类聚合 + 预算截断。"""

    def __init__(
        self,
        max_items_per_channel: int = 5,
        enabled_channels: Iterable[str] | None = None,
    ):
        self.max_items_per_channel = max(1, int(max_items_per_channel))
        enabled = list(enabled_channels) if enabled_channels is not None else list(
            DEFAULT_ENABLED_CHANNELS
        )
        self.enabled_channels = [c for c in enabled if c in ALL_CHANNELS] or list(
            DEFAULT_ENABLED_CHANNELS
        )

    def aggregate(
        self,
        candidates: list[IntelItem],
        group_count: int = 0,
        source_message_count: int = 0,
    ) -> AggregationResult:
        dropped = 0
        by_fp: dict[str, IntelItem] = {}

        for item in candidates:
            fp = item.fingerprint or content_fingerprint(item.content)
            item.fingerprint = fp
            item.channel = normalize_channel(item.channel or item.raw_category)
            if item.channel not in self.enabled_channels:
                # other 默认不在 enabled 时丢弃
                if item.priority != PRIORITY_CRITICAL:
                    continue
                # critical 强制映射进 apikey 频道（若启用）
                if "apikey" in self.enabled_channels:
                    item.channel = "apikey"
                else:
                    continue

            existing = by_fp.get(fp)
            if existing is None:
                by_fp[fp] = item
                continue
            dropped += 1
            # 合并来源群信息到 meta
            groups = set(existing.meta.get("groups") or [existing.source_group_id])
            groups.add(item.source_group_id)
            existing.meta["groups"] = sorted(g for g in groups if g)
            existing.priority = best_priority(existing.priority, item.priority)
            if not existing.reason and item.reason:
                existing.reason = item.reason

        # 按频道截断
        by_channel: dict[str, list[IntelItem]] = defaultdict(list)
        for item in by_fp.values():
            by_channel[item.channel].append(item)

        kept: list[IntelItem] = []
        for channel in self.enabled_channels:
            bucket = by_channel.get(channel, [])
            bucket.sort(key=lambda x: (priority_rank(x.priority), -len(x.content)))
            kept.extend(bucket[: self.max_items_per_channel])

        # critical 全局兜底：即使 channel 截断也尽量保留
        criticals = [
            i
            for i in by_fp.values()
            if i.priority == PRIORITY_CRITICAL and i not in kept
        ]
        if criticals:
            kept.extend(criticals)

        kept.sort(key=lambda x: (priority_rank(x.priority), x.channel, x.source_group_id))
        return AggregationResult(
            items=kept,
            dropped_duplicates=dropped,
            group_count=group_count,
            source_message_count=source_message_count,
            candidate_count=len(candidates),
        )


class ChannelPacker:
    """把聚合结果打成一条或多条推送文本。"""

    def __init__(self, push_mode: str = "split", interval_minutes: int = 10):
        mode = (push_mode or "split").strip().lower()
        self.push_mode = mode if mode in ("split", "merged") else "split"
        self.interval_minutes = max(1, int(interval_minutes))

    def pack(self, result: AggregationResult) -> list[str]:
        if not result.items:
            return []
        if self.push_mode == "merged":
            text = self._pack_merged(result)
            return [text] if text else []
        return self._pack_split(result)

    def _pack_split(self, result: AggregationResult) -> list[str]:
        by_channel: dict[str, list[IntelItem]] = defaultdict(list)
        for item in result.items:
            by_channel[item.channel].append(item)

        messages: list[str] = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for channel in ALL_CHANNELS:
            items = by_channel.get(channel)
            if not items:
                continue
            emoji = channel_emoji(channel)
            name = channel_display(channel)
            lines = [
                f"{emoji} [{name}] 跨群简报 · 近 {self.interval_minutes} 分钟",
                f"📊 {result.group_count} 群参与 · 候选 {result.candidate_count} · 本频道 {len(items)} 条",
            ]
            if result.dropped_duplicates:
                lines.append(f"🧹 已去重 {result.dropped_duplicates} 条重复")
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
            for item in items:
                groups = item.meta.get("groups") or [item.source_group_id]
                group_part = "/".join(str(g) for g in groups if g) or "?"
                user = item.source_user_name or item.source_user_id or "?"
                content = item.content.replace("\n", " ").strip()
                if len(content) > 160:
                    content = content[:160] + "…"
                lines.append(f"· 群{group_part} · {user}")
                lines.append(f"  {content}")
                if item.reason:
                    lines.append(f"  💡 {item.reason}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"⏰ {now}")
            messages.append("\n".join(lines))
        return messages

    def _pack_merged(self, result: AggregationResult) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        by_channel: dict[str, list[IntelItem]] = defaultdict(list)
        for item in result.items:
            by_channel[item.channel].append(item)

        sections: list[str] = []
        for channel in ALL_CHANNELS:
            items = by_channel.get(channel)
            if not items:
                continue
            emoji = channel_emoji(channel)
            name = channel_display(channel)
            block = [f"{emoji} [{name}] ×{len(items)}"]
            for item in items[: self._preview_limit()]:
                groups = item.meta.get("groups") or [item.source_group_id]
                group_part = "/".join(str(g) for g in groups if g) or "?"
                content = item.content.replace("\n", " ").strip()
                if len(content) > 120:
                    content = content[:120] + "…"
                block.append(f"  · [{group_part}] {content}")
            sections.append("\n".join(block))

        if not sections:
            return ""

        header = (
            f"🧠 跨群情报简报（近 {self.interval_minutes} 分钟）\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {result.group_count} 个群 · 源消息 {result.source_message_count} · "
            f"候选 {result.candidate_count} · 输出 {len(result.items)} 条"
        )
        if result.dropped_duplicates:
            header += f" · 去重 {result.dropped_duplicates}"
        return (
            header
            + "\n\n"
            + "\n\n".join(sections)
            + f"\n\n━━━━━━━━━━━━━━━━━━━━━\n⏰ {now}"
        )

    def _preview_limit(self) -> int:
        # merged 模式下每频道预览条数
        return 5


def extract_all_group_candidates(
    batches: dict[str, list[dict]],
    extractor: GroupCandidateExtractor,
    watched_user_ids: set[str] | None = None,
    extra_keywords: list[str] | None = None,
) -> list[IntelItem]:
    """对多群 buffer 跑 L1，合并 candidates。"""
    all_items: list[IntelItem] = []
    for group_id, messages in batches.items():
        all_items.extend(
            extractor.extract(
                group_id=group_id,
                messages=messages,
                watched_user_ids=watched_user_ids,
                extra_keywords=extra_keywords,
            )
        )
    return all_items
