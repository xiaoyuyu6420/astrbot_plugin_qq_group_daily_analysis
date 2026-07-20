"""
领域服务 - 分析业务逻辑服务

该模块导出所有封装核心业务逻辑的领域服务，
用于分析群聊数据。这些服务是平台无关的。

服务分类:
- 分析器服务: 话题分析、金句分析
- 计算服务: 统计计算
"""

from .golden_quote_analyzer import GoldenQuoteAnalyzerAdapter, IGoldenQuoteAnalyzer
from .incremental_merge_service import IncrementalMergeService
from .intel_taxonomy import (
    ALL_CHANNELS,
    CHANNEL_APIKEY,
    CHANNEL_DEAL,
    CHANNEL_INTEL,
    CHANNEL_METHOD,
    CHANNEL_OTHER,
    CHANNEL_RESOURCE,
    DEFAULT_ENABLED_CHANNELS,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    best_priority,
    channel_display,
    channel_emoji,
    channel_priority,
    normalize_channel,
    priority_rank,
)
from .statistics_calculator import StatisticsCalculator
from .topic_analyzer import ITopicAnalyzer, TopicAnalyzerAdapter

__all__ = [
    # 统计与报告服务
    "StatisticsCalculator",
    # 增量合并服务
    "IncrementalMergeService",
    # 话题分析服务
    "ITopicAnalyzer",
    "TopicAnalyzerAdapter",
    # 金句分析服务
    "IGoldenQuoteAnalyzer",
    "GoldenQuoteAnalyzerAdapter",
    # 情报分类体系
    "ALL_CHANNELS",
    "CHANNEL_APIKEY",
    "CHANNEL_DEAL",
    "CHANNEL_INTEL",
    "CHANNEL_METHOD",
    "CHANNEL_OTHER",
    "CHANNEL_RESOURCE",
    "DEFAULT_ENABLED_CHANNELS",
    "PRIORITY_CRITICAL",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "best_priority",
    "channel_display",
    "channel_emoji",
    "channel_priority",
    "normalize_channel",
    "priority_rank",
]
