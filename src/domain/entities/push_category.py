"""
用户推送分类（PushCategory）

定时日报「按群归类」的桶：科技 / AI / 自定义 等。
与实时监控的「内容频道」（apikey/resource/...）语义不同。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PushCategory:
    """一个用户分类：名称 + 群列表。"""

    name: str
    groups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "groups": list(self.groups)}


def _normalize_group_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # UMO → 取末段群号；纯群号原样
    if ":" in s:
        s = s.rsplit(":", 1)[-1].strip()
    # 话题/隔离后缀：取 # 或 _ 前的主群号仍保留完整 id，匹配交给 is_group_allowed
    return s


def normalize_push_categories(raw: Any) -> list[PushCategory]:
    """将配置原始值归一化为 list[PushCategory]。

    支持：
    - list[dict | PushCategory]
    - JSON 字符串
    - 单个 dict
    无效项跳过；name 空或 groups 空的分类丢弃。
    """
    if raw is None or raw == "":
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []

    if isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, list):
        return []

    result: list[PushCategory] = []
    for item in raw:
        if isinstance(item, PushCategory):
            name = item.name.strip()
            groups = [_normalize_group_id(g) for g in item.groups]
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            groups_raw = item.get("groups", [])
            if isinstance(groups_raw, str):
                # 允许 "1,2,3" 或 JSON 数组字符串
                gtext = groups_raw.strip()
                if gtext.startswith("["):
                    try:
                        groups_raw = json.loads(gtext)
                    except (json.JSONDecodeError, TypeError):
                        groups_raw = [x.strip() for x in gtext.split(",") if x.strip()]
                else:
                    groups_raw = [x.strip() for x in gtext.split(",") if x.strip()]
            if not isinstance(groups_raw, list):
                groups_raw = [groups_raw]
            groups = [_normalize_group_id(g) for g in groups_raw]
        elif isinstance(item, str):
            # 容错：纯字符串当 name，无群 → 丢弃
            continue
        else:
            continue

        groups = [g for g in groups if g]
        # 保序去重
        seen: set[str] = set()
        uniq: list[str] = []
        for g in groups:
            if g not in seen:
                seen.add(g)
                uniq.append(g)

        if not name or not uniq:
            continue
        result.append(PushCategory(name=name, groups=uniq))

    return result
