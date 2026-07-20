"""
领域实体

该模块导出所有领域实体类，包括:
- AnalysisTask: 分析任务聚合根
- GroupAnalysisResult: 群聊分析结果实体
- IncrementalBatch: 增量分析独立批次实体
- IncrementalState: 增量分析聚合视图（报告时使用）
"""

from .analysis_result import (
    ActivityVisualization,
    EmojiStatistics,
    GoldenQuote,
    GroupAnalysisResult,
    GroupStatistics,
    SummaryTopic,
    TokenUsage,
)
from .analysis_task import AnalysisTask, TaskStatus
from .incremental_state import IncrementalBatch, IncrementalState
from .intel_item import IntelItem

__all__ = [
    "AnalysisTask",
    "TaskStatus",
    "GroupAnalysisResult",
    "SummaryTopic",
    "GoldenQuote",
    "TokenUsage",
    "EmojiStatistics",
    "ActivityVisualization",
    "GroupStatistics",
    "IncrementalBatch",
    "IncrementalState",
    "IntelItem",
]
