from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class TopicItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    contributors: list[str]
    detail: str

    @field_validator("topic", "detail", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> str:
        return str(value).strip()

    @field_validator("contributors", mode="before")
    @classmethod
    def _normalize_contributors(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        contributors: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                contributors.append(text)
        return contributors


class GoldenQuoteItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    sender: str
    reason: str

    @field_validator("content", "sender", "reason", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> str:
        return str(value).strip()


class TimelineEventModel(BaseModel):
    """事件脉络叙事条目校验。extra=ignore 容忍 LLM 多返回的额外字段。"""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    narrative: str = ""
    importance: str = "medium"
    participants: list[str] = []
    time_span: str = ""
    tags: list[str] = []

    @field_validator("title", "narrative", "importance", "time_span", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> str:
        return str(value).strip()

    @field_validator("importance", mode="before")
    @classmethod
    def _normalize_importance(cls, value: object) -> str:
        v = str(value).strip().lower()
        return v if v in ("high", "medium", "low") else "medium"

    @field_validator("participants", "tags", mode="before")
    @classmethod
    def _normalize_str_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(x).strip() for x in value if str(x).strip()]


def validate_timeline_events(
    data_list: list[dict],
) -> tuple[bool, list[dict] | None, str | None]:
    """校验事件脉络 LLM 返回，过滤掉 title+narrative 全空的条目。"""
    try:
        normalized = [
            TimelineEventModel.model_validate(item).model_dump()
            for item in data_list
        ]
        # 过滤全空条目
        filtered = [
            d for d in normalized if d["title"] or d["narrative"]
        ]
        return True, filtered, None
    except ValidationError as e:
        return False, None, str(e)


def validate_topic_items(
    data_list: list[dict],
) -> tuple[bool, list[dict] | None, str | None]:
    try:
        normalized = [
            TopicItemModel.model_validate(item).model_dump() for item in data_list
        ]
        return True, normalized, None
    except ValidationError as e:
        return False, None, str(e)


def validate_golden_quote_items(
    data_list: list[dict],
) -> tuple[bool, list[dict] | None, str | None]:
    try:
        normalized = [
            GoldenQuoteItemModel.model_validate(item).model_dump() for item in data_list
        ]
        return True, normalized, None
    except ValidationError as e:
        return False, None, str(e)


class DigestThemeModel(BaseModel):
    """分类日报聚合主题条目校验。extra=ignore 容忍 LLM 多返回的额外字段。"""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    narrative: str = ""
    importance: str = "medium"
    tags: list[str] = []
    # 关联原条目编号（1-based，与 prompt 输入的条目编号对齐）
    related_item_ids: list[int] = []

    @field_validator("title", "narrative", "importance", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> str:
        return str(value).strip()

    @field_validator("importance", mode="before")
    @classmethod
    def _normalize_importance(cls, value: object) -> str:
        v = str(value).strip().lower()
        return v if v in ("high", "medium", "low") else "medium"

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(x).strip() for x in value if str(x).strip()]

    @field_validator("related_item_ids", mode="before")
    @classmethod
    def _normalize_related_ids(cls, value: object) -> list[int]:
        if not isinstance(value, list):
            return []
        ids: list[int] = []
        for x in value:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        return ids


def validate_digest_themes(
    data_list: list[dict],
) -> tuple[bool, list[dict] | None, str | None]:
    """校验分类聚合主题 LLM 返回，过滤掉 title+narrative 全空的条目。"""
    try:
        normalized = [
            DigestThemeModel.model_validate(item).model_dump() for item in data_list
        ]
        filtered = [d for d in normalized if d["title"] or d["narrative"]]
        return True, filtered, None
    except ValidationError as e:
        return False, None, str(e)

