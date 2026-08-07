"""
数据模型定义
包含所有分析相关的数据结构
"""

from dataclasses import dataclass, field


@dataclass
class SummaryTopic:
    """话题总结数据结构"""

    topic: str
    contributors: list[str]
    detail: str
    contributor_ids: list[str] = field(
        default_factory=list
    )  # 贡献者ID列表 (用于显示头像)


@dataclass
class TimelineEvent:
    """事件脉络叙事（取代平铺话题/金句，以「事件」为单位还原讨论脉络）。

    设计依据（认知科学 / schema theory）：
    人脑理解信息的效率取决于「框架先行，细节填充」。先建立事件骨架
    （发生了什么），再在骨架上挂细节（谁说了什么），比平铺一堆孤立句子
    的理解负荷低得多。因此本结构以「一个完整事件」为单位，叙事串起脉络，
    细节在叙事中自然带出。
    """

    title: str  # 一句话说清这是什么事件（不限字数，让人秒懂）
    narrative: str  # 来龙去脉叙事：起因→关键发言→结论，细节自然带出
    importance: str = "medium"  # high | medium | low（排序用）
    participants: list[str] = field(default_factory=list)  # 关键参与者用户 ID
    time_span: str = ""  # "14:00-14:20" 事件发生时段
    tags: list[str] = field(default_factory=list)  # 商机/技术/避坑 等标签


@dataclass
class DigestTheme:
    """分类日报的聚合主题（二次 LLM 对多群碎条目的语义归并结果）。

    设计依据（认知科学 / schema theory）：
    人脑理解信息的效率取决于「框架先行，细节填充」。多群各自抽取的碎条目
    平铺成 69 条无层级列表，超出工作记忆容量（4±1 组块），读者无法形成
    「今天这圈发生了什么」的整体认知。先聚合成 3-5 个主题叙事（框架），
    再在主题下挂关联原条目（细节），认知负荷骤降。

    与 TimelineEvent 的区别：TimelineEvent 是单群事件脉络；DigestTheme 是
    跨群主题归并，且需保留 related_items 以追溯信息来源。
    """

    title: str  # 主题标题（一句话，如"Claude 4.5 发布引发讨论"）
    narrative: str  # 叙事概括：来龙去脉，自然带出关键信息
    importance: str = "medium"  # high | medium | low（排序用）
    tags: list[str] = field(default_factory=list)  # 商机/技术/避坑 等标签
    # 归并到本主题下的原条目编号（1-based，与 prompt 输入编号对齐）。
    # analyzer 存 ids；service 层据此回填 related_items。
    related_item_ids: list[int] = field(default_factory=list)
    # 回填后的原条目实例（可追溯来源）。
    # 用 Any[] 而非 ValueItem[] 避免域层→应用层循环导入；运行时存 ValueItem。
    related_items: list = field(default_factory=list)


@dataclass
class GoldenQuote:
    """群聊金句数据结构"""

    content: str
    sender: str
    reason: str
    user_id: str = ""  # 原 qq 字段


@dataclass
class TokenUsage:
    """Token使用统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class EmojiStatistics:
    """表情统计数据结构"""

    face_count: int = 0  # QQ基础表情数量
    mface_count: int = 0  # 动画表情数量
    bface_count: int = 0  # 超级表情数量
    sface_count: int = 0  # 小表情数量
    other_emoji_count: int = 0  # 其他表情数量
    face_details: dict = field(default_factory=dict)  # 具体表情ID统计 {face_id: count}

    @property
    def total_emoji_count(self) -> int:
        """总表情数量"""
        return (
            self.face_count
            + self.mface_count
            + self.bface_count
            + self.sface_count
            + self.other_emoji_count
        )


@dataclass
class ActivityVisualization:
    """活跃度可视化数据结构"""

    hourly_activity: dict = field(default_factory=dict)  # {hour: count}
    daily_activity: dict = field(default_factory=dict)  # {date: count}
    user_activity_ranking: list = field(default_factory=list)  # 用户活跃度排行
    peak_hours: list = field(default_factory=list)  # 高峰时段
    activity_heatmap_data: dict = field(default_factory=dict)  # 热力图数据


@dataclass
class GroupStatistics:
    """群聊统计数据结构"""

    message_count: int
    total_characters: int
    participant_count: int
    most_active_period: str
    golden_quotes: list[GoldenQuote]
    emoji_count: int  # 保持向后兼容
    emoji_statistics: EmojiStatistics = field(default_factory=EmojiStatistics)
    activity_visualization: ActivityVisualization = field(
        default_factory=ActivityVisualization
    )
    token_usage: TokenUsage = field(default_factory=TokenUsage)
