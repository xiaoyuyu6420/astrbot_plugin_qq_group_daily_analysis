"""分类日报二次聚合分析器（解决多群碎条目堆叠问题）。

设计动机：
  分类聚合链路对每个群独立抽取价值条目（话题+信息差），多群结果仅做指纹去重拼接，
  常堆出 50-70 条无层级平铺列表（如"AI 板块折叠 69 条"）。这违背认知科学
  （工作记忆 4±1 组块）——读者无法形成"今天这圈发生了什么"的整体认知。

  本分析器把已提炼的碎条目按语义归并成 3-5 个主题叙事，每个主题挂关联原条目
  （可追溯来源）。与 timeline_analyzer 同属"叙事化降低认知负荷"范式，区别是：
  timeline 输入单群原始消息，本分析器输入多群已提炼条目。

输入：ValueItem 序列化文本列表（每条带编号/来源群/来源人/内容/价值说明）
输出：list[DigestTheme]，按 importance 排序，每个带 related_item_ids 引用原条目

复用 BaseAnalyzer 的全部基础设施：schema 约束 / 解析降级 / schema 重试 /
人格强化 / token 统计，与 timeline_analyzer 同范式。
"""

from __future__ import annotations

import re

from ....domain.models.data_models import DigestTheme
from ....utils.logger import logger
from ..utils.json_utils import parse_json_response
from ..utils.response_validation import validate_digest_themes
from ..utils.structured_output_schema import JSONObject, build_digest_themes_schema
from .base_analyzer import BaseAnalyzer


class DigestThemeAnalyzer(BaseAnalyzer[DigestTheme, list[dict]]):
    """跨群碎条目 → 聚合主题叙事。

    输入数据是「条目序列化 dict 列表」，每条含：
      item_id (int), source (str), content (str), reason (str)
    build_prompt 将其编排成 LLM 可读的编号列表文本。
    """

    def get_provider_id_key(self) -> str:
        """复用话题分析的 provider 配置（可与话题共用 LLM 后端）。"""
        return "topic_provider_id"

    def get_data_type(self) -> str:
        return "分类主题聚合"

    def get_max_count(self) -> int:
        """聚合主题数上限（防爆 token + 控制认知负荷到 4±1 组块）。"""
        getter = getattr(self.config_manager, "get_digest_max_themes", None)
        if callable(getter):
            try:
                return max(3, int(getter()))
            except Exception:
                pass
        return 5

    def get_response_schema_name(self) -> str:
        return "digest_themes"

    def get_response_schema(self) -> JSONObject:
        return build_digest_themes_schema(self.get_max_count())

    # ------------------------------------------------------------------
    # prompt 构建
    # ------------------------------------------------------------------

    def build_prompt(self, data: list[dict]) -> str:
        """data: 条目序列化列表，每条含 item_id/source/content/reason/importance。"""
        if not isinstance(data, list) or not data:
            return ""

        # 编排成带编号的条目列表（编号即 item_id，LLM 用它引用），重要度标记前置
        level_tag = {"high": "[重要]", "medium": "[一般]", "low": "[轻量]"}
        lines: list[str] = []
        for item in data:
            item_id = item.get("item_id", "?")
            source = item.get("source", "")
            content = item.get("content", "")
            reason = item.get("reason", "")
            tag = level_tag.get(str(item.get("importance", "")).strip().lower(), "[一般]")
            line = f"[#{item_id}] {tag} {source}"
            if content:
                line += f"：{content}"
            if reason:
                line += f"（{reason}）"
            lines.append(line)
        items_text = "\n".join(lines)
        max_themes = self.get_max_count()

        return self._default_prompt(items_text, max_themes, len(data))

    def _default_prompt(
        self, items_text: str, max_themes: int, total_items: int
    ) -> str:
        return f"""你是一个日报主编。下面是从多个群聊中提炼出的 {total_items} 条信息条目（每条带 #[编号]、来源群和重要度标记）。
它们彼此孤立、有重复、有关联。请把它们**按语义归并**成 **2~{max_themes} 个核心主题**，让读者一分钟看懂「今天这圈到底发生了哪几件事」。

## 主题标准（宁缺毋滥）

一个主题 = 多条围绕同一件事/同一话题、有信息增量的信息归并。**凑不出就少给**：今天只有 2 件值得讲的事就只给 2 个主题，绝不为了凑数硬造主题。

重要度标记含义：`[重要]`=high（有明确价值/可行动/多群讨论）、`[一般]`=medium（有一定信息量）、`[轻量]`=low（轻量）。
**`[轻量]` 条目除非特别亮眼，否则不要收进任何主题。**

## 不要收进主题的（宁可漏收）

- 广告/推广/抽奖/集赞/砍价
- 纯晒图/晒战绩/晒日常，无讨论无信息
- 打卡签到、无上下文转发（链接/视频不带说明）
- 寒暄、情绪、玩梗、碎片化吐槽
- 判断标准：读者看到这条会觉得「这有啥」，就不收

## 每个主题怎么写（结构化，不要写散文）

严格按这个结构，用「【】」前缀分行：

【主旨】一句话说清这件事是什么（让人秒懂）
【要点】2~5 条，每条一行用「- 」开头，放最有价值/最可行动的信息，提到来源用 #[编号] 自然带出
【结论】有结论/共识/可行动建议就一句话点出；没有就省略这行

**禁止**用「这是一组关于xxx的信息」这种空洞分类语，禁止罗列条目原文，禁止自由发挥写成长段散文。

## 重要性排序

给每个主题标 importance：
- high：有明确价值/可行动/或汇聚了多条高价值信息的核心主题
- medium：有一定信息量但非核心
- low：轻量但值得一看

## 关联条目

每个主题必须列出 related_item_ids——即归并到该主题下的原条目 #[编号]。一条条目可归入多个主题（如果它横跨多个话题）。无关紧要的游离条目**不要硬塞**。

## 待聚合的信息条目（共 {total_items} 条）

{items_text}

---

## 返回格式（纯 JSON 数组，最少 1 个、最多 {max_themes} 个主题，按 importance 从高到低排）

```json
[{{
  "title": "一句话说清这是什么主题（让人秒懂）",
  "narrative": "【主旨】...\\n【要点】\\n- ...\\n- ...\\n【结论】...",
  "importance": "high",
  "tags": ["商机"],
  "related_item_ids": [1, 5, 12]
}}]
```

注意：返回纯 JSON，不要 markdown 代码块标记。若条目确实没有值得讲的主题，返回空数组 []。"""

    # ------------------------------------------------------------------
    # 解析 / 降级
    # ------------------------------------------------------------------

    def parse_structured_response(
        self, result_text: str
    ) -> tuple[bool, list[dict] | None, str | None]:
        """聚合主题返回 JSON 数组，复用通用数组解析。"""
        return parse_json_response(result_text, self.get_data_type())

    def validate_parsed_data(
        self, data_list: list[dict]
    ) -> tuple[bool, list[dict] | None, str | None]:
        return validate_digest_themes(data_list)

    def extract_with_regex(self, result_text: str, max_count: int) -> list[dict]:
        """正则降级：从 LLM 文本里抢救 title/narrative 字段，importance 默认 medium。

        分级结构和关联关系会退化，但保证不空手——用 title/narrative 抢救。
        """
        items: list[dict] = []
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
                        "importance": "medium",
                        "tags": [],
                        "related_item_ids": [],
                    }
                )
            if len(items) >= max_count:
                break
        return items

    def create_data_objects(self, data_list: list[dict]) -> list[DigestTheme]:
        """dict 列表 → DigestTheme 列表，按 importance 排序（high→medium→low）。

        related_item_ids 在此存入（analyzer 持有 LLM 返回的编号）；
        related_items 留空，由 service 层根据 ids 回填原 ValueItem 实例。
        """
        themes: list[DigestTheme] = []
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
            tags = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()][
                :5
            ]
            raw_ids = d.get("related_item_ids") or []
            related_item_ids: list[int] = []
            if isinstance(raw_ids, list):
                for x in raw_ids:
                    try:
                        related_item_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
            themes.append(
                DigestTheme(
                    title=title,
                    narrative=narrative,
                    importance=importance,
                    tags=tags,
                    related_item_ids=related_item_ids,
                    related_items=[],  # service 层回填
                )
            )

        themes.sort(key=lambda t: rank.get(t.importance, 1))
        return themes
