"""周报/月报主题聚合分析器。

与 DigestThemeAnalyzer 的关系：schema、解析、降级、排序全部复用，
只有 prompt 视角不同——

日报回答「今天发生了什么」（事件清单）；
周报/月报只回答「拉长时间才看得出来的东西」：
持续主线（同一话题跨多天/多周）、演化与反转（结论变了）、
跨群共振（同一天多群独立在聊 = 信号强）、反复出现的坑。

输入条目是「日主题/周主题」的二次摘要（content 里已含当日报叙事的压缩），
不是原始消息——周期报告不重新抓取、不复述原文。
"""

from __future__ import annotations

from .digest_theme_analyzer import DigestThemeAnalyzer


class WeeklyThemeAnalyzer(DigestThemeAnalyzer):
    """把过去一周（或一个月）的日报主题再聚合成周期主线。

    period: "week" | "month" —— 只影响 prompt 措辞与主题数上限的默认。
    """

    def __init__(self, context, config_manager, period: str = "week"):
        super().__init__(context, config_manager)
        self.period = period if period in ("week", "month") else "week"

    def get_data_type(self) -> str:
        return "周报主题聚合" if self.period == "week" else "月报主题聚合"

    def get_max_count(self) -> int:
        getter = getattr(self.config_manager, "get_review_max_themes", None)
        if callable(getter):
            try:
                return max(2, int(getter()))
            except Exception:
                pass
        return 6 if self.period == "week" else 10

    def _default_prompt(
        self, items_text: str, max_themes: int, total_items: int
    ) -> str:
        if self.period == "month":
            return self._month_prompt(items_text, max_themes, total_items)
        return self._week_prompt(items_text, max_themes, total_items)

    # ------------------------------------------------------------------
    # 周报 prompt
    # ------------------------------------------------------------------

    def _week_prompt(self, items_text: str, max_themes: int, total_items: int) -> str:
        return f"""你是一个周报主编。下面是过去一周里每天日报已提炼出的主题条目（共 {total_items} 条，每条带 #[编号]、[日期 分类]、重要度和当日叙事）。

读者每天已经看过日报了，**不要把日报再讲一遍**。你的唯一任务是回答那些「只有拉满一周才看得出来」的问题：

## 只收这类主题（宁缺毋滥）

- **持续主线**：同一件事在 ≥2 天出现且有后续（比如一个工具从"被挖出"→"出教程"→"多人实测"）
- **演化与反转**：某个结论/口碑/价格在一周内发生了变化（比如某模型从"传闻降智"→"实测"→"退款"）
- **跨群共振**：同一天或相邻几天里多个群独立讨论同一件事（= 强信号）
- **反复出现的坑/限流/封号**：同类问题一周内出现多次，值得固化成避坑经验

## 不要收的（这些日报已经给过）

- 只在一天出现且没有后续的话题
- 单日事件的过程细节（要细节让读者回去翻日报，周报只给结论和走向）
- 寒暄/玩梗/无信息量内容

## 每个主题怎么写（结构化，不要散文）

【主旨】一句话说清这条线是什么 + 起止日期（如「09-15 起」）
【走向】2~5 条，按时间顺序写这条线怎么演化，每条用「- 」开头，来源用 #[编号] 自然带出
【本周结论】这事现在落到什么状态/共识；没有就省略这行
【相关坑/资源】这条线里若有值得固化的避坑经验或值得收藏的链接/工具，一句话点出；没有就省略

## 重要性排序

- high：持续 ≥3 天，或多群共振，或有明确反转/结论
- medium：跨 2 天，或有后续但影响力一般
- low：原则上不进周报，除非特别有价值

## 输入条目（共 {total_items} 条）

{items_text}

## 返回格式（纯 JSON 数组，最少 1 个、最多 {max_themes} 个主题，按 importance 从高到低排）

```json
[{{
  "title": "一句话说清这条主线（让人秒懂）",
  "narrative": "【主旨】...\\n【走向】\\n- ...\\n- ...\\n【本周结论】...",
  "importance": "high",
  "tags": ["主线"],
  "related_item_ids": [1, 5, 12]
}}]
```

注意：返回纯 JSON，不要 markdown 代码块标记。若本周确实没有值得讲的跨日线索，返回空数组 []。"""

    # ------------------------------------------------------------------
    # 月报 prompt
    # ------------------------------------------------------------------

    def _month_prompt(self, items_text: str, max_themes: int, total_items: int) -> str:
        return f"""你是一个月报主编。下面是过去一个月里每周周报已提炼出的主线条目（共 {total_items} 条，每条带 #[编号]、[日期]、重要度和当周叙事）。

月报是「沉淀层」，读者想一次看清这个月这圈值得留下什么。**不要复述过程**，要出榜单和结论。

## 只收这类主题

- **月度主题榜**：这个月最被反复讨论/最有影响的几件事，各自给出**最终结论/当前状态**
- **资源与工具归档**：本月出现的值得收藏的工具/渠道/资源链接，标注时效（还活着 / 已失效 / 不确定）
- **固化下来的坑**：本月反复踩的坑，写成可以直接照做的避坑条目
- **趋势信号**：能看出方向性变化的事（如某类模型口碑整体转向、某个渠道生态兴衰）

## 不收的

- 月内一闪而过、没有沉淀价值的话题
- 与上月重复且没有新进展的内容（月报不是滚动复读机）

## 每个主题怎么写（结构化）

【定位】一句话：这是什么 + 本月为什么值得记住
【结论】这件事/这个工具/这个话题本月的最终状态（加日期锚点，如「09 中旬起」）
【要点】2~5 条关键事实或数据，每条用「- 」开头，来源用 #[编号] 自然带出
【留存建议】读者该不该收藏/行动/持续跟踪，一句话；没有就省略

## 重要性排序

- high：本月最有沉淀价值的核心主题（榜单前列）
- medium：值得记录但非核心
- low：原则上不进月报

## 输入条目（共 {total_items} 条）

{items_text}

## 返回格式（纯 JSON 数组，最少 1 个、最多 {max_themes} 个主题，按 importance 从高到低排）

```json
[{{
  "title": "主题名（榜单语感，如「workbuddy2api：订阅号包装成 OpenAI API」）",
  "narrative": "【定位】...\\n【结论】...\\n【要点】\\n- ...\\n【留存建议】...",
  "importance": "high",
  "tags": ["榜单"],
  "related_item_ids": [1, 5, 12]
}}]
```

注意：返回纯 JSON，不要 markdown 代码块标记。若本月确实没有值得沉淀的主题，返回空数组 []。"""
