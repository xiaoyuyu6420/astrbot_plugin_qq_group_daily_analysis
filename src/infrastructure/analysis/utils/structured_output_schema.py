from __future__ import annotations

from typing import TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | dict[str, "JSONValue"] | list["JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]


def build_response_format(name: str, schema: JSONObject) -> JSONObject:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def build_topics_schema(max_items: int) -> JSONObject:
    return {
        "type": "array",
        "maxItems": max(1, int(max_items)),
        "items": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "contributors": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "detail": {"type": "string"},
            },
            "required": ["topic", "contributors", "detail"],
            "additionalProperties": False,
        },
    }


def build_golden_quotes_schema(max_items: int) -> JSONObject:
    return {
        "type": "array",
        "maxItems": max(1, int(max_items)),
        "items": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "sender": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["content", "sender", "reason"],
            "additionalProperties": False,
        },
    }


def build_timeline_schema(max_events: int) -> JSONObject:
    """事件脉络叙事 schema：以事件为单位，含叙事/重要性/参与者/时段/标签。

    与话题/金句的平铺结构不同，本结构强调整体性（一个事件=一组关联消息的叙事）。
    importance 枚举用于排序；participants/tags 是数组可空。
    """
    return {
        "type": "array",
        "maxItems": max(1, int(max_events)),
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "narrative": {"type": "string"},
                "importance": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "time_span": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["title", "narrative", "importance"],
            "additionalProperties": False,
        },
    }


def build_digest_themes_schema(max_themes: int) -> JSONObject:
    """分类日报聚合主题 schema：跨群碎条目归并为主题叙事 + 关联条目引用。

    与 timeline_schema 的区别：输入是已提炼的条目（非原始消息），输出需带
    related_item_ids 引用原条目编号，供调用方回填 ValueItem。
    strict schema 要求 all keys in required；participants/time_span 此处不适用。
    """
    return {
        "type": "array",
        "maxItems": max(1, int(max_themes)),
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "narrative": {"type": "string"},
                "importance": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "related_item_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": [
                "title",
                "narrative",
                "importance",
                "tags",
                "related_item_ids",
            ],
            "additionalProperties": False,
        },
    }


