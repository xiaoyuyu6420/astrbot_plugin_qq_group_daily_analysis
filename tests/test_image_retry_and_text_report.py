"""图片渲染重试机制 + 降级文本格式测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.reporting.dispatcher import ReportDispatcher
from src.infrastructure.reporting.generators import ReportGenerator
from src.domain.value_objects.golden_quote import GoldenQuote
from src.domain.value_objects.topic import Topic


# ============================================================
# 辅助：构造 ConfigManager / Dispatcher / Generator 的 mock
# ============================================================


def _make_config_manager(retry_count=1, interval=0):
    """构造一个最小 ConfigManager mock，只暴露重试相关 getter。"""
    cm = MagicMock()
    cm.get_image_retry_count.return_value = retry_count
    cm.get_image_retry_interval_seconds.return_value = interval
    cm.get_max_topics.return_value = 5
    cm.get_max_golden_quotes.return_value = 8
    return cm


def _make_dispatcher(config_manager=None, report_generator=None, **retry_kwargs):
    if config_manager is None:
        config_manager = _make_config_manager(**retry_kwargs)
    if report_generator is None:
        report_generator = MagicMock()
    message_sender = MagicMock()
    message_sender.bot_manager = MagicMock()
    return ReportDispatcher(config_manager, report_generator, message_sender)


# ============================================================
# 测试 1：_render_image_with_retry 首次成功不重试
# ============================================================


def test_render_image_no_retry_on_success():
    dispatcher = _make_dispatcher()
    dispatcher._html_render_func = lambda x: x  # 非 None 即可

    gen = MagicMock()
    gen.generate_image_report = AsyncMock(return_value=("base64://img", "<html>"))
    dispatcher.report_generator = gen

    image_url, html = asyncio.run(
        dispatcher._render_image_with_retry({"x": 1}, "111", "onebot")
    )
    assert image_url == "base64://img"
    assert html == "<html>"
    # 只调用了一次（成功就返回，不重试）
    assert gen.generate_image_report.call_count == 1


# ============================================================
# 测试 2：_render_image_with_retry 失败后按配置重试，最终成功
# ============================================================


def test_render_image_retries_then_succeeds():
    """第一次失败（返回 None），重试后成功。"""
    dispatcher = _make_dispatcher(retry_count=1, interval=0)
    dispatcher._html_render_func = lambda x: x

    gen = MagicMock()
    gen.generate_image_report = AsyncMock(
        side_effect=[
            (None, "<html>"),  # 第一次失败
            ("base64://img", "<html>"),  # 重试成功
        ]
    )
    dispatcher.report_generator = gen

    image_url, html = asyncio.run(
        dispatcher._render_image_with_retry({"x": 1}, "111", "onebot")
    )
    assert image_url == "base64://img"
    assert gen.generate_image_report.call_count == 2


# ============================================================
# 测试 3：_render_image_with_retry 全部失败返回 None
# ============================================================


def test_render_image_all_attempts_fail():
    """重试次数用尽仍失败，返回 (None, html)。"""
    dispatcher = _make_dispatcher(retry_count=2, interval=0)
    dispatcher._html_render_func = lambda x: x

    gen = MagicMock()
    gen.generate_image_report = AsyncMock(return_value=(None, "<html>"))
    dispatcher.report_generator = gen

    image_url, html = asyncio.run(
        dispatcher._render_image_with_retry({"x": 1}, "111", "onebot")
    )
    assert image_url is None
    assert html == "<html>"
    # 初次 + 2 次重试 = 3 次
    assert gen.generate_image_report.call_count == 3


# ============================================================
# 测试 4：retry_count=0 时不重试
# ============================================================


def test_render_image_no_retry_when_count_zero():
    """retry_count=0 时，失败立即返回，不重试。"""
    dispatcher = _make_dispatcher(retry_count=0, interval=0)
    dispatcher._html_render_func = lambda x: x

    gen = MagicMock()
    gen.generate_image_report = AsyncMock(return_value=(None, "<html>"))
    dispatcher.report_generator = gen

    image_url, _ = asyncio.run(
        dispatcher._render_image_with_retry({"x": 1}, "111", "onebot")
    )
    assert image_url is None
    assert gen.generate_image_report.call_count == 1


# ============================================================
# 测试 5：异常被捕获，视为失败继续重试
# ============================================================


def test_render_image_exception_treated_as_failure():
    """generate_image_report 抛异常时，应被捕获并进入重试。"""
    dispatcher = _make_dispatcher(retry_count=1, interval=0)
    dispatcher._html_render_func = lambda x: x

    gen = MagicMock()
    gen.generate_image_report = AsyncMock(
        side_effect=[
            RuntimeError("T2I 端点崩溃"),  # 第一次异常
            ("base64://img", "<html>"),  # 重试成功
        ]
    )
    dispatcher.report_generator = gen

    image_url, _ = asyncio.run(
        dispatcher._render_image_with_retry({"x": 1}, "111", "onebot")
    )
    assert image_url == "base64://img"
    assert gen.generate_image_report.call_count == 2


# ============================================================
# 测试 6-8：generate_text_report 新格式
# ============================================================


def _make_stats(**overrides):
    """构造一个 mock statistics 对象。"""
    defaults = {
        "message_count": 829,
        "participant_count": 42,
        "total_characters": 12345,
        "emoji_count": 200,
        "most_active_period": "夜间 22-24 点",
        "golden_quotes": [],
    }
    defaults.update(overrides)
    stats = MagicMock()
    for k, v in defaults.items():
        setattr(stats, k, v)
    return stats


def _make_generator():
    """构造 ReportGenerator（只测 generate_text_report，不触发 LLM/渲染）。

    generate_text_report 只依赖 config_manager + _tz_now()，但 __init__ 会初始化
    T2I 信号量和 avatar Cache，所以补全必要参数。
    """
    cm = MagicMock()
    cm.get_t2i_max_concurrent.return_value = 1
    cm.get_max_topics.return_value = 5
    cm.get_max_golden_quotes.return_value = 8
    import tempfile
    from pathlib import Path

    return ReportGenerator(config_manager=cm, data_dir=Path(tempfile.mkdtemp()))


def test_text_report_structured_format_with_fallback():
    """降级文本应包含分隔线、方括号编号、降级提示。"""
    gen = _make_generator()
    topics = [
        Topic(name="API 限流讨论", contributors=("111", "222"), detail="讨论了限流对策"),
    ]
    quotes = [
        GoldenQuote(content="免费 API key", sender="333", reason="资源干货"),
    ]
    result = {"statistics": _make_stats(golden_quotes=quotes), "topics": topics}

    text = gen.generate_text_report(result, image_fallback=True)

    # 降级提示
    assert "⚠️ 图片渲染失败" in text
    # 结构化标记
    assert "━" in text  # 分隔线
    assert "【基础统计】" in text
    assert "【有价值话题】" in text
    assert "【信息差 / 商机 / 干货】" in text
    # 方括号编号
    assert "【话题 1】" in text
    assert "【01】" in text
    # 话题字段（验证 topic.topic bug 已修复为 .name）
    assert "API 限流讨论" in text
    # 金句内容
    assert "免费 API key" in text
    assert "资源干货" in text


def test_text_report_no_fallback_banner_when_not_image_fallback():
    """主动文本格式（非降级）不应有图片失败提示。"""
    gen = _make_generator()
    result = {"statistics": _make_stats(), "topics": []}

    text = gen.generate_text_report(result, image_fallback=False)

    assert "⚠️ 图片渲染失败" not in text
    assert "群聊情报日报" in text


def test_text_report_empty_state():
    """无话题无金句时，应显示空状态文案。"""
    gen = _make_generator()
    result = {"statistics": _make_stats(golden_quotes=[]), "topics": []}

    text = gen.generate_text_report(result)

    assert "（今日无明显有价值话题）" in text
    assert "（今日未筛到可行动的高价值信息）" in text
