"""
增量水位线（watermark）单元测试。

覆盖 P0 第四项「增量水位线补拉」的核心契约：
1. LLM 慢时（wall_clock 推进很多）水位线仍等于本批次最后消息时间戳，
   不会被 wall_clock 回退 —— 这是原 bug 的修复点
2. 时钟异常（消息时间戳远超 fetch 起点）时回退到 fetch_start_ts，
   防止毒化水位线导致后续永远拉不到消息
3. 边界：last_message_timestamp=0、刚好在 margin 边界上、负 margin 等
"""

import tests.conftest  # noqa: F401  触发 mock 注入
from src.application.services.analysis_application_service import (
    WATERMARK_FUTURE_MARGIN,
    compute_safe_watermark,
)


class TestComputeSafeWatermark:
    """纯函数 compute_safe_watermark 的契约测试。"""

    def test_normal_returns_last_message_timestamp(self):
        """正常场景：水位线 = 本批次最后消息时间戳。"""
        fetch_start = 1_700_000_000
        last_msg = fetch_start + 100  # 消息略晚于 fetch 起点（正常）
        assert compute_safe_watermark(last_msg, fetch_start) == last_msg

    def test_llm_slow_does_not_use_wall_clock(self):
        """LLM 慢时：即便 wall_clock 已推进很多，水位线仍是 last_msg_ts。

        这是原 bug 的回归测试。原代码用 min(last_msg_ts, wall_clock+60)，
        LLM 跑 10 分钟后 wall_clock 推进 600s，若 last_msg_ts 因某种原因
        略大于 wall_clock+60 会被截断。新逻辑完全不看 wall_clock。
        """
        fetch_start = 1_700_000_000
        last_msg = fetch_start + 50
        # 模拟 LLM 跑了 10 分钟后的 wall_clock（比 last_msg 大很多）
        wall_clock_after_llm = fetch_start + 600
        # 新逻辑不接收 wall_clock 参数 —— 水位线只取决于 last_msg 与 fetch_start
        result = compute_safe_watermark(last_msg, fetch_start)
        assert result == last_msg
        # 明确：结果不等于 wall_clock（原 bug 会卡在 wall_clock+60）
        assert result != wall_clock_after_llm
        assert result != wall_clock_after_llm + 60

    def test_future_timestamp_falls_back_to_fetch_start(self):
        """时钟异常：消息时间戳远超 fetch 起点（>1h），回退到 fetch_start。"""
        fetch_start = 1_700_000_000
        # 消息时间戳在 2 小时后 —— 明显异常
        last_msg = fetch_start + WATERMARK_FUTURE_MARGIN + 1
        assert compute_safe_watermark(last_msg, fetch_start) == fetch_start

    def test_exactly_at_margin_boundary_is_accepted(self):
        """刚好等于 margin 边界：视为合法，不回退。"""
        fetch_start = 1_700_000_000
        last_msg = fetch_start + WATERMARK_FUTURE_MARGIN
        assert compute_safe_watermark(last_msg, fetch_start) == last_msg

    def test_just_over_margin_falls_back(self):
        """刚超过 margin 1 秒：回退。"""
        fetch_start = 1_700_000_000
        last_msg = fetch_start + WATERMARK_FUTURE_MARGIN + 1
        assert compute_safe_watermark(last_msg, fetch_start) == fetch_start

    def test_zero_last_message_returns_zero(self):
        """本批次没有有效消息时间戳：返回 0（调用方不推进水位线）。"""
        fetch_start = 1_700_000_000
        assert compute_safe_watermark(0, fetch_start) == 0

    def test_negative_last_message_returns_zero(self):
        """负时间戳视为无效：返回 0。"""
        fetch_start = 1_700_000_000
        assert compute_safe_watermark(-1, fetch_start) == 0

    def test_last_message_before_fetch_start_is_accepted(self):
        """消息时间戳早于 fetch 起点（正常：fetch 拉的是历史消息）：接受。"""
        fetch_start = 1_700_000_000
        last_msg = fetch_start - 3600  # 1 小时前
        assert compute_safe_watermark(last_msg, fetch_start) == last_msg

    def test_custom_margin(self):
        """自定义 margin 生效。"""
        fetch_start = 1_700_000_000
        last_msg = fetch_start + 100
        # margin=50 时 100 > 50，回退
        assert compute_safe_watermark(last_msg, fetch_start, future_margin=50) == fetch_start
        # margin=200 时 100 < 200，接受
        assert compute_safe_watermark(last_msg, fetch_start, future_margin=200) == last_msg

    def test_equal_to_fetch_start_is_accepted(self):
        """消息时间戳恰好等于 fetch 起点：接受（边界）。"""
        fetch_start = 1_700_000_000
        assert compute_safe_watermark(fetch_start, fetch_start) == fetch_start


class TestWatermarkDoesNotSkipMessagesDuringSlowLlm:
    """端到端语义测试：LLM 慢时期间到达的消息不会被跳过。

    场景模拟：
    - T0: fetch，拉到 last_msg_ts = T0
    - T0 ~ T0+600: LLM 跑 10 分钟，期间群里来了 200 条消息
    - T0+600: LLM 完成，水位线应 = T0（不是 T0+600）
    - 下一轮 fetch(since_ts=T0)：能拉到 T0 之后的所有消息，包括那 200 条
    """

    def test_slow_llm_watermark_equals_last_msg_not_wall_clock(self):
        t0 = 1_700_000_000
        last_msg_ts = t0  # 本批次最后一条
        # LLM 跑了 10 分钟
        wall_clock_after = t0 + 600

        # 新逻辑：水位线 = last_msg_ts，与 wall_clock 无关
        watermark = compute_safe_watermark(last_msg_ts, fetch_start_ts=t0)
        assert watermark == last_msg_ts

        # 模拟下一轮：消息 ts 在 (last_msg_ts, wall_clock_after] 的都会被拉到
        # 因为 since_ts = watermark = last_msg_ts，过滤条件是 ts > since_ts
        mid_messages = [t0 + i for i in range(1, 201)]  # 200 条中间消息
        pulled = [ts for ts in mid_messages if ts > watermark]
        assert len(pulled) == 200, "LLM 慢时期间到达的消息全部应被下一轮拉取"

        # 对比：如果用旧逻辑 min(last_msg, wall_clock+60) = min(t0, t0+660) = t0
        # 旧逻辑在这个场景其实也不漏 —— 真正的 bug 是 last_msg 略大于 wall_clock+60 时
        # 被截断。下面测那个场景。
        _ = wall_clock_after  # 仅用于文档说明

    def test_old_bug_last_msg_slightly_after_wall_clock_plus_60(self):
        """原 bug 场景：last_msg_ts 略大于 wall_clock+60 时被截断。

        原代码：safe_ts = min(last_msg_ts, wall_clock+60)
        若 last_msg_ts = wall_clock + 70，safe_ts = wall_clock+60
        下一轮 since_ts = wall_clock+60，会漏掉 wall_clock+60 ~ wall_clock+70 的消息。

        新代码：不看 wall_clock，safe_ts = last_msg_ts，不漏。
        """
        fetch_start = 1_700_000_000
        # 消息时间戳比 fetch 起点晚 70 秒（完全正常，消息就是这段时间到的）
        last_msg = fetch_start + 70
        # 模拟 LLM 跑了 0 秒（瞬时完成），wall_clock ≈ fetch_start
        # 旧逻辑：min(fetch_start+70, fetch_start+60) = fetch_start+60 —— 漏 10 秒
        # 新逻辑：fetch_start+70 —— 不漏
        watermark = compute_safe_watermark(last_msg, fetch_start)
        assert watermark == last_msg

        # 验证：ts 在 (fetch_start+60, last_msg] 的消息不会被漏
        borderline = fetch_start + 65
        assert borderline <= watermark  # 水位线覆盖了这条
