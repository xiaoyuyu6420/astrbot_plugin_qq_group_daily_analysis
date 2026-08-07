"""
事件脉络叙事分析器（质量改造核心）

设计动机：
  原话题/金句 prompt 产出的是孤立条目（标题+描述 / 原文+价值说明），彼此无脉络，
  读者要自己重建「今天发生了什么」。按认知科学（schema theory：框架先行降低认知
  负荷），本分析器让 LLM 以「事件」为单位还原讨论脉络——起因→关键发言→结论，
  细节在叙事中自然带出。同时给 importance 排序、明确负面示例、禁止套路化模板。

输入：单群完整消息流（legacy dict 列表）
输出：list[TimelineEvent]，按 importance 排序

不合并多群：每群独立分析，保留单群上下文的细腻度，不赌多群混杂的幻觉风险。
"""

from __future__ import annotations

import re

from ....domain.models.data_models import TimelineEvent
from ....utils.logger import logger
from ...utils.template_utils import render_template
from ..utils.json_utils import parse_json_response
from ..utils.response_validation import validate_timeline_events
from ..utils.structured_output_schema import JSONObject, build_timeline_schema
from .base_analyzer import BaseAnalyzer
from .topic_analyzer import TopicAnalyzer  # 复用消息提取逻辑


class TimelineAnalyzer(BaseAnalyzer[TimelineEvent, list[dict]]):
    """每群独立 → 事件脉络叙事时间线。

    复用 TopicAnalyzer 的消息提取（legacy dict → [HH:MM] [用户ID]: 内容），
    重写 prompt 为「事件叙事」，产出带 importance 排序的 TimelineEvent。
    """

    def __init__(self, context, config_manager):
        super().__init__(context, config_manager)
        # 借用 TopicAnalyzer 的消息提取能力（它处理了 @/回复/机器人过滤等细节）
        self._topic_extractor = TopicAnalyzer(context, config_manager)

    # ------------------------------------------------------------------
    # BaseAnalyzer 契约
    # ------------------------------------------------------------------

    def get_provider_id_key(self) -> str:
        """复用话题分析的 provider 配置（可与话题共用 LLM 后端）。"""
        return "topic_provider_id"

    def get_data_type(self) -> str:
        return "事件脉络"

    def get_max_count(self) -> int:
        """单群最多产出的事件数（防爆 token + 控制信息密度）。"""
        if self._incremental_max_count is not None:
            return self._incremental_max_count
        getter = getattr(self.config_manager, "get_max_timeline_events", None)
        if callable(getter):
            try:
                return max(3, int(getter()))
            except Exception:
                pass
        return 12

    def get_response_schema_name(self) -> str:
        return "timeline_events"

    def get_response_schema(self) -> JSONObject:
        return build_timeline_schema(self.get_max_count())

    # ------------------------------------------------------------------
    # prompt 构建（复用消息提取，重写为事件叙事）
    # ------------------------------------------------------------------

    def build_prompt(self, data: list[dict]) -> str:
        if not isinstance(data, list) or not data:
            return ""

        # 复用 TopicAnalyzer 的消息提取逻辑（含 @/回复/机器人过滤/清理）
        text_messages = self._topic_extractor.extract_text_messages(data)
        if not text_messages:
            logger.warning("事件脉络分析：未提取到有效文本消息，返回空 prompt")
            return ""

        messages_text = "\n".join(
            f"[{m['time']}] [{m['user_id']}]: {m['content']}" for m in text_messages
        )
        max_events = self.get_max_count()

        # 优先用配置的自定义 prompt，否则用内置事件叙事默认
        prompt_template = self._get_prompt_template()
        if prompt_template:
            try:
                return render_template(
                    prompt_template,
                    max_events=max_events,
                    messages_text=messages_text,
                )
            except Exception as e:
                logger.warning(f"应用事件脉络 prompt 失败，回退默认: {e}")

        return self._default_prompt(messages_text, max_events)

    def _get_prompt_template(self) -> str | None:
        getter = getattr(self.config_manager, "get_timeline_prompt", None)
        if callable(getter):
            try:
                tpl = getter()
                if tpl and tpl.strip():
                    return tpl
            except Exception:
                pass
        return None

    def _default_prompt(self, messages_text: str, max_events: int) -> str:
        return f"""你是一个群聊观察者。请阅读以下群聊记录，找出今天发生的「值得讲的事件」，然后用讲故事的方式讲清楚，让读者一眼 get 到「今天这群里发生了什么」。

## 什么是「值得讲的事件」

一个事件 = 多条消息围绕同一话题形成的、有脉络的讨论或信息交流。
必须满足其一：有信息增量（学到/获得什么）、有讨论过程（问题→回答→结论）、或是一个值得注意的动向。

## 不要收的（给具体例子，宁可漏收也不要滥收）

- 纯情绪/玩梗：「哈哈哈哈」「草」「确实」「???」
- 无信息寒暄：「早」「在吗」「睡了」「晚安」
- 碎片化吐槽没有上下文：「这游戏真恶心」（然后没了）
- 重复刷屏、表情包接龙
判断标准：如果抽离出来单独看，读者会觉得「这有啥」，就不要。

## 每个事件怎么写（叙事，不要套模板）

用你自己的话讲清楚这件事的来龙去脉：
- 它怎么起的（谁提了个什么）
- 关键转折/核心信息（谁说了关键的、有价值的）
- 最后怎么样了（结论/共识/或悬而未决）
自然地把具体细节、人名（用 [用户ID] 格式）带进去，像跟朋友复述「今天群里有个事」。
**禁止**用「这是一条xxx，价值在于xxx」这种格式，禁止罗列要点，要写成连贯的话。

## 重要性排序

给每个事件标 importance：
- high：有明确价值/可行动/或引发大量讨论的核心事件
- medium：有一定信息量但非核心
- low：轻量但提一下无妨

## 消息记录

{messages_text}

---

## 返回格式（纯 JSON 数组，最多 {max_events} 个事件，按 importance 从高到低排）

```json
[{{
  "title": "一句话说清这是什么事件（不限字数，但要让人秒懂）",
  "narrative": "用你自己的话讲清楚来龙去脉，细节自然带出，提到用户用 [用户ID] 格式",
  "importance": "high",
  "participants": ["123456789"],
  "time_span": "14:00-14:20",
  "tags": ["商机"]
}}]
```

注意：返回纯 JSON，不要 markdown 代码块标记。若确无值得讲的事件，返回空数组 []。"""

    # ------------------------------------------------------------------
    # 解析 / 降级
    # ------------------------------------------------------------------

    def parse_structured_response(
        self, result_text: str
    ) -> tuple[bool, list[dict] | None, str | None]:
        """事件脉络返回的是 JSON 数组，复用通用数组解析。"""
        return parse_json_response(result_text, self.get_data_type())

    def validate_parsed_data(
        self, data_list: list[dict]
    ) -> tuple[bool, list[dict] | None, str | None]:
        return validate_timeline_events(data_list)

    def extract_with_regex(self, result_text: str, max_count: int) -> list[dict]:
        """正则降级：从 LLM 文本里抢救 title/narrative 字段，importance 默认 low。

        分级结构会退化，但保证不空手——用 narrative/title 抢救。
        """
        items: list[dict] = []
        # 匹配 "title": "..." 和紧随的 "narrative": "..."
        for m in re.finditer(
            r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"[^}]*?"narrative"\s*:\s*"((?:[^"\\]|\\.)*)"',
            result_text,
            re.DOTALL,
        ):
            title = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
            narrative = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
            if title.strip() or narrative.strip():
                items.append(
                    {
                        "title": title.strip(),
                        "narrative": narrative.strip(),
                        "importance": "low",
                        "participants": [],
                        "time_span": "",
                        "tags": [],
                    }
                )
            if len(items) >= max_count:
                break
        return items

    def create_data_objects(self, data_list: list[dict]) -> list[TimelineEvent]:
        """dict 列表 → TimelineEvent 列表，按 importance 排序（high→medium→low）。"""
        events: list[TimelineEvent] = []
        rank = {"high": 0, "medium": 1, "low": 2}

        for d in data_list[: self.get_max_count()]:
            if not isinstance(d, dict):
                continue
            title = str(d.get("title", "")).strip()
            narrative = str(d.get("narrative", "")).strip()
            if not title and not narrative:
                continue
            importance = str(d.get("importance", "medium")).strip().lower()
            if importance not in rank:
                importance = "medium"
            participants = [
                str(p).strip()
                for p in (d.get("participants") or [])
                if str(p).strip()
            ][:5]
            tags = [
                str(t).strip() for t in (d.get("tags") or []) if str(t).strip()
            ][:5]
            events.append(
                TimelineEvent(
                    title=title,
                    narrative=narrative,
                    importance=importance,
                    participants=participants,
                    time_span=str(d.get("time_span", "")).strip(),
                    tags=tags,
                )
            )

        events.sort(key=lambda e: rank.get(e.importance, 1))
        return events
