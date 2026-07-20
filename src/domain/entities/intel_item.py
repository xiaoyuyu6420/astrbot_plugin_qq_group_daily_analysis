"""
情报条目（IntelItem）

统一 keyword / window / 跨群分层聚合的最小价值单元。
identity 使用 platform + id，避免写死 QQ 数字假设，便于后续微信/TG。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IntelItem:
    """一条可推送/可聚合的情报候选。"""

    content: str
    channel: str  # taxonomy channel_id: apikey|resource|deal|intel|method|other
    priority: str  # critical|normal|low
    reason: str = ""
    source_user_id: str = ""
    source_user_name: str = ""
    source_group_id: str = ""
    platform_id: str = ""
    fingerprint: str = ""
    topic: str = ""
    raw_category: str = ""  # 原始正则/LLM 类别标签（兼容旧字符串）
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "channel": self.channel,
            "priority": self.priority,
            "reason": self.reason,
            "source_user_id": self.source_user_id,
            "source_user_name": self.source_user_name,
            "source_group_id": self.source_group_id,
            "platform_id": self.platform_id,
            "fingerprint": self.fingerprint,
            "topic": self.topic,
            "raw_category": self.raw_category,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntelItem":
        return cls(
            content=str(data.get("content", "")),
            channel=str(data.get("channel", "other")),
            priority=str(data.get("priority", "normal")),
            reason=str(data.get("reason", "")),
            source_user_id=str(data.get("source_user_id", "")),
            source_user_name=str(data.get("source_user_name", "")),
            source_group_id=str(data.get("source_group_id", "")),
            platform_id=str(data.get("platform_id", "")),
            fingerprint=str(data.get("fingerprint", "")),
            topic=str(data.get("topic", "")),
            raw_category=str(data.get("raw_category", "")),
            meta=dict(data.get("meta") or {}),
        )
