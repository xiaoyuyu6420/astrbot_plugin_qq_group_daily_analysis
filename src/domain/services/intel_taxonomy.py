"""
情报分类体系（Taxonomy）

固定频道，避免 LLM 自由标签漂移。旧正则类别 / LLM category 统一映射到 channel_id。
"""

from __future__ import annotations

from dataclasses import dataclass


PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"

CHANNEL_APIKEY = "apikey"
CHANNEL_RESOURCE = "resource"
CHANNEL_DEAL = "deal"
CHANNEL_INTEL = "intel"
CHANNEL_METHOD = "method"
CHANNEL_OTHER = "other"

ALL_CHANNELS: tuple[str, ...] = (
    CHANNEL_APIKEY,
    CHANNEL_RESOURCE,
    CHANNEL_DEAL,
    CHANNEL_INTEL,
    CHANNEL_METHOD,
    CHANNEL_OTHER,
)

DEFAULT_ENABLED_CHANNELS: tuple[str, ...] = (
    CHANNEL_APIKEY,
    CHANNEL_RESOURCE,
    CHANNEL_DEAL,
    CHANNEL_INTEL,
    CHANNEL_METHOD,
)


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: str
    display_name: str
    default_priority: str
    emoji: str


CHANNEL_SPECS: dict[str, ChannelSpec] = {
    CHANNEL_APIKEY: ChannelSpec(CHANNEL_APIKEY, "密钥/凭证", PRIORITY_CRITICAL, "🔴"),
    CHANNEL_RESOURCE: ChannelSpec(CHANNEL_RESOURCE, "资源链接", PRIORITY_NORMAL, "📦"),
    CHANNEL_DEAL: ChannelSpec(CHANNEL_DEAL, "商机", PRIORITY_NORMAL, "💰"),
    CHANNEL_INTEL: ChannelSpec(CHANNEL_INTEL, "情报", PRIORITY_NORMAL, "📡"),
    CHANNEL_METHOD: ChannelSpec(CHANNEL_METHOD, "干货方法", PRIORITY_NORMAL, "🛠"),
    CHANNEL_OTHER: ChannelSpec(CHANNEL_OTHER, "其他", PRIORITY_LOW, "📎"),
}

# 旧正则 category → channel
_LEGACY_REGEX_CATEGORY_MAP: dict[str, str] = {
    "API Key": CHANNEL_APIKEY,
    "资源链接": CHANNEL_RESOURCE,
    "渠道": CHANNEL_RESOURCE,
    "关键词": CHANNEL_OTHER,
}

# LLM / 自由文本 category → channel
_LLM_CATEGORY_MAP: dict[str, str] = {
    "apikey": CHANNEL_APIKEY,
    "api key": CHANNEL_APIKEY,
    "api_key": CHANNEL_APIKEY,
    "密钥": CHANNEL_APIKEY,
    "凭证": CHANNEL_APIKEY,
    "资源": CHANNEL_RESOURCE,
    "资源链接": CHANNEL_RESOURCE,
    "链接": CHANNEL_RESOURCE,
    "渠道": CHANNEL_RESOURCE,
    "商机": CHANNEL_DEAL,
    "deal": CHANNEL_DEAL,
    "情报": CHANNEL_INTEL,
    "intel": CHANNEL_INTEL,
    "干货": CHANNEL_METHOD,
    "方法": CHANNEL_METHOD,
    "method": CHANNEL_METHOD,
    "其他": CHANNEL_OTHER,
    "other": CHANNEL_OTHER,
    "未确认": CHANNEL_OTHER,
}


def normalize_channel(raw: str | None) -> str:
    """把任意旧标签/LLM 标签归一到 channel_id。"""
    if not raw:
        return CHANNEL_OTHER
    text = str(raw).strip()
    if text in CHANNEL_SPECS:
        return text
    if text in _LEGACY_REGEX_CATEGORY_MAP:
        return _LEGACY_REGEX_CATEGORY_MAP[text]
    key = text.lower()
    if key in _LLM_CATEGORY_MAP:
        return _LLM_CATEGORY_MAP[key]
    # 宽松包含
    for k, channel in _LLM_CATEGORY_MAP.items():
        if k and k in key:
            return channel
    return CHANNEL_OTHER


def channel_priority(channel: str) -> str:
    spec = CHANNEL_SPECS.get(channel) or CHANNEL_SPECS[CHANNEL_OTHER]
    return spec.default_priority


def channel_display(channel: str) -> str:
    spec = CHANNEL_SPECS.get(channel) or CHANNEL_SPECS[CHANNEL_OTHER]
    return spec.display_name


def channel_emoji(channel: str) -> str:
    spec = CHANNEL_SPECS.get(channel) or CHANNEL_SPECS[CHANNEL_OTHER]
    return spec.emoji


def priority_rank(priority: str) -> int:
    """数字越小优先级越高，用于排序截断。"""
    if priority == PRIORITY_CRITICAL:
        return 0
    if priority == PRIORITY_NORMAL:
        return 1
    return 2


def best_priority(*priorities: str) -> str:
    if any(p == PRIORITY_CRITICAL for p in priorities):
        return PRIORITY_CRITICAL
    if any(p == PRIORITY_NORMAL for p in priorities):
        return PRIORITY_NORMAL
    return PRIORITY_LOW
