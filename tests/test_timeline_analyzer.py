"""事件脉络叙事分析器（TimelineAnalyzer）的纯解析逻辑测试。

不依赖真实 LLM —— 测试 prompt 构建、schema、校验排序、降级、create_data_objects
这些确定性逻辑。LLM 调用本身的重试/熔断由 BaseAnalyzer 已有的集成测试覆盖。
"""

from unittest.mock import MagicMock

from src.domain.models.data_models import TimelineEvent
from src.infrastructure.analysis.analyzers.timeline_analyzer import TimelineAnalyzer
from src.infrastructure.analysis.utils.response_validation import (
    validate_timeline_events,
)
from src.infrastructure.analysis.utils.structured_output_schema import (
    build_timeline_schema,
)


def _make_analyzer(max_events=12) -> TimelineAnalyzer:
    cfg = MagicMock()
    cfg.get_max_timeline_events.return_value = max_events
    cfg.get_timeline_prompt.return_value = None  # 用默认 prompt
    cfg.get_bot_self_ids.return_value = []
    cfg.get_max_topics.return_value = 12
    return TimelineAnalyzer(context=MagicMock(), config_manager=cfg)


def test_schema_has_narrative_importance_participants():
    """schema：事件含 title/narrative/importance，importance 枚举 high/medium/low。"""
    schema = build_timeline_schema(max_events=8)
    item_props = schema["items"]["properties"]
    assert {"title", "narrative", "importance", "participants", "tags"} <= set(
        item_props.keys()
    )
    assert set(item_props["importance"]["enum"]) == {"high", "medium", "low"}
    # maxItems 在数组顶层（限制事件数量），不在 items（items 是单元素 schema）
    assert schema["maxItems"] == 8


def test_validate_filters_empty_events():
    """校验：title+narrative 全空的条目被过滤。"""
    data = [
        {"title": "有内容", "narrative": "叙事", "importance": "high"},
        {"title": "", "narrative": "", "importance": "low"},  # 全空，应过滤
        {"title": "只有标题", "narrative": "", "importance": "medium"},
    ]
    ok, filtered, err = validate_timeline_events(data)
    assert ok and err is None
    assert len(filtered) == 2  # 全空的被滤掉


def test_validate_normalizes_importance():
    """importance 非法值归一为 medium。"""
    ok, filtered, _ = validate_timeline_events(
        [{"title": "x", "narrative": "y", "importance": "critical"}]
    )
    assert ok
    assert filtered[0]["importance"] == "medium"


def test_build_prompt_includes_messages_and_negative_examples():
    """prompt 含消息文本、负面示例（解决杂质多）、禁止模板（解决套路化）。"""
    analyzer = _make_analyzer()
    # 构造 legacy dict 格式消息
    data = [
        {
            "time": 1753740000,
            "sender": {"user_id": "111", "nickname": "u1", "card": ""},
            "message": [{"type": "text", "data": {"text": "今天聊个重要的事"}}],
        }
    ]
    prompt = analyzer.build_prompt(data)
    assert "今天聊个重要的事" in prompt
    # 负面示例（解决杂质多）
    assert "哈哈哈哈" in prompt
    # 禁止模板（解决套路化）
    assert "禁止" in prompt or "不要套" in prompt
    assert prompt  # 非空


def test_build_prompt_empty_data_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer.build_prompt([]) == ""


def test_create_data_objects_sorted_by_importance():
    """create_data_objects：按 importance high→medium→low 排序。"""
    analyzer = _make_analyzer()
    data = [
        {"title": "low事件", "narrative": "n", "importance": "low"},
        {"title": "high事件", "narrative": "n", "importance": "high"},
        {"title": "mid事件", "narrative": "n", "importance": "medium"},
    ]
    events = analyzer.create_data_objects(data)
    assert [e.importance for e in events] == ["high", "medium", "low"]
    assert isinstance(events[0], TimelineEvent)


def test_create_data_objects_drops_empty():
    """title+narrative 全空的事件被丢弃。"""
    analyzer = _make_analyzer()
    data = [
        {"title": "有效", "narrative": "n", "importance": "high"},
        {"title": "", "narrative": "", "importance": "low"},
    ]
    events = analyzer.create_data_objects(data)
    assert len(events) == 1


def test_extract_with_regex_recovers_title_narrative():
    """正则降级：从 LLM 文本抢救 title+narrative，importance 默认 low。"""
    analyzer = _make_analyzer()
    text = (
        '{"title": "抢救标题", "narrative": "抢救的叙事内容"} '
        '{"title": "第二", "narrative": "n2"}'
    )
    items = analyzer.extract_with_regex(text, max_count=5)
    assert len(items) == 2
    assert "抢救标题" in items[0]["title"]
    assert items[0]["importance"] == "low"  # 降级默认


def test_validate_parsed_data_passes_valid_list():
    """validate_parsed_data 走 validate_timeline_events。"""
    analyzer = _make_analyzer()
    ok, data, err = analyzer.validate_parsed_data(
        [{"title": "x", "narrative": "y", "importance": "high"}]
    )
    assert ok and err is None
    assert data[0]["importance"] == "high"


# ====================================================================
# 接入主线：TopicAnalyzer 在事件叙事模式下委托 TimelineAnalyzer
# ====================================================================


def _make_topic_cfg(*, timeline_enabled=True):
    cfg = MagicMock()
    cfg.get_timeline_narrative_enabled.return_value = timeline_enabled
    cfg.get_max_timeline_events.return_value = 12
    cfg.get_max_topics.return_value = 12
    cfg.get_topic_analysis_prompt.return_value = None
    cfg.get_bot_self_ids.return_value = []
    cfg.get_timeline_prompt.return_value = None
    return cfg


def test_topic_analyzer_delegates_to_timeline_when_enabled():
    """开启事件叙事时，analyze_topics 走 _analyze_as_timeline，产出适配的 SummaryTopic。"""
    import asyncio
    from unittest.mock import patch
    from src.infrastructure.analysis.analyzers.topic_analyzer import TopicAnalyzer
    from src.domain.models.data_models import SummaryTopic

    cfg = _make_topic_cfg(timeline_enabled=True)
    analyzer = TopicAnalyzer(context=MagicMock(), config_manager=cfg)

    # mock TimelineAnalyzer.analyze 返回事件叙事（带 importance）
    async def fake_analyze(messages, umo=None, session_id=None):
        return (
            [
                TimelineEvent(
                    title="网易招开发", narrative="夙梦分享网易春风招开发...",
                    importance="high", participants=["100"],
                ),
                TimelineEvent(
                    title="闲聊天气", narrative="有人聊天气",
                    importance="low", participants=["200"],
                ),
            ],
            MagicMock(),
        )

    # 构造 legacy 消息
    messages = [
        {
            "time": 1753740000,
            "sender": {"user_id": "100", "nickname": "夙梦", "card": ""},
            "message": [{"type": "text", "data": {"text": "网易春风招开发"}}],
        }
    ]

    # TimelineAnalyzer 是延迟导入的，patch 其真实模块路径
    with patch(
        "src.infrastructure.analysis.analyzers.timeline_analyzer.TimelineAnalyzer"
    ) as MockTL:
        MockTL.return_value.analyze = fake_analyze
        topics, usage = asyncio.run(analyzer.analyze_topics(messages, "umo", "sid"))

    # 适配成 SummaryTopic，high 事件在前（排序）
    assert len(topics) == 2
    assert all(isinstance(t, SummaryTopic) for t in topics)
    # high 事件带 🔥 标记，title 映射到 topic，narrative 映射到 detail
    assert topics[0].topic.startswith("🔥")
    assert "网易招开发" in topics[0].topic
    assert "夙梦分享" in topics[0].detail
    # participants 映射到 contributors（昵称解析）+ contributor_ids
    assert topics[0].contributor_ids == ["100"]
    assert "夙梦" in topics[0].contributors


def test_topic_analyzer_falls_back_when_timeline_disabled():
    """关闭事件叙事时，_use_timeline_narrative 返回 False（走旧平铺话题路径）。"""
    from src.infrastructure.analysis.analyzers.topic_analyzer import TopicAnalyzer

    cfg_off = _make_topic_cfg(timeline_enabled=False)
    analyzer = TopicAnalyzer(context=MagicMock(), config_manager=cfg_off)
    assert analyzer._use_timeline_narrative() is False

    cfg_on = _make_topic_cfg(timeline_enabled=True)
    analyzer_on = TopicAnalyzer(context=MagicMock(), config_manager=cfg_on)
    assert analyzer_on._use_timeline_narrative() is True


