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
