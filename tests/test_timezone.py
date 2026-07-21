"""
时区工具 + 配置热更新 单元测试。

覆盖 P0 第二项「配置热更新 + 时区统一」的核心契约：
1. shared.timezone 在合法/非法/空 时区名下都能给出可用 ZoneInfo
2. ConfigManager.reload_config 真的会刷时区缓存并调用注入的回调
3. ConfigManager.get_timezone 能正确读取配置
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import tests.conftest  # noqa: F401  触发 mock 注入
from src.infrastructure.config.config_manager import ConfigManager
from src.shared import timezone as tz_mod
from src.shared.timezone import (
    DEFAULT_TZ_NAME,
    active_tz,
    active_tz_name,
    configure_timezone,
    from_timestamp,
    now,
    today_str,
)
from tests.conftest import AstrBotConfig


class TestSharedTimezone:
    """shared.timezone 工具函数行为。"""

    def setup_method(self):
        # 每个测试前重置回默认，避免相互污染
        configure_timezone(DEFAULT_TZ_NAME)

    def test_default_is_shanghai(self):
        """未配置时默认 Asia/Shanghai。"""
        assert active_tz_name() == "Asia/Shanghai"
        assert active_tz() == ZoneInfo("Asia/Shanghai")

    def test_configure_valid_timezone(self):
        """合法时区名能被识别并生效。"""
        result = configure_timezone("UTC")
        assert result == "UTC"
        assert active_tz_name() == "UTC"
        assert active_tz() == ZoneInfo("UTC")

    def test_configure_invalid_falls_back_to_default(self):
        """非法时区名回退到默认，不抛异常。"""
        result = configure_timezone("Mars/Olympus")
        assert result == DEFAULT_TZ_NAME
        assert active_tz_name() == DEFAULT_TZ_NAME

    def test_configure_empty_falls_back_to_default(self):
        """空字符串/None 都走默认。"""
        assert configure_timezone("") == DEFAULT_TZ_NAME
        assert configure_timezone(None) == DEFAULT_TZ_NAME

    def test_now_is_timezone_aware(self):
        """now() 返回的 datetime 必须是 aware（这是业务逻辑的前提）。"""
        dt = now()
        assert dt.tzinfo is not None
        assert dt.utcoffset() is not None

    def test_now_respects_active_timezone(self):
        """切换时区后，now() 的偏移量跟着变。"""
        configure_timezone("UTC")
        utc_now = now()
        assert utc_now.utcoffset() == timedelta(0)

        configure_timezone("Asia/Tokyo")
        tokyo_now = now()
        assert tokyo_now.utcoffset() == timedelta(hours=9)

    def test_today_str_uses_active_timezone(self):
        """today_str() 按当前时区返回日期，与 now() 一致。"""
        configure_timezone("Asia/Shanghai")
        assert today_str() == now().strftime("%Y-%m-%d")

    def test_today_str_custom_format(self):
        """支持自定义格式。"""
        configure_timezone("UTC")
        assert today_str("%Y/%m/%d") == now().strftime("%Y/%m/%d")

    def test_from_timestamp_uses_active_timezone(self):
        """时间戳按当前时区解析为本地时间。"""
        # 已知的 UTC 瞬时：2024-01-01 00:00:00 UTC
        ts = 1704067200
        configure_timezone("UTC")
        dt_utc = from_timestamp(ts)
        assert dt_utc.strftime("%Y-%m-%d %H:%M:%S") == "2024-01-01 00:00:00"

        configure_timezone("Asia/Shanghai")
        dt_sh = from_timestamp(ts)
        # 上海 +08:00
        assert dt_sh.strftime("%Y-%m-%d %H:%M:%S") == "2024-01-01 08:00:00"

    def test_now_utc_returns_utc(self):
        """now_utc() 不受 configure_timezone 影响。"""
        configure_timezone("Asia/Tokyo")
        utc_dt = tz_mod.now_utc()
        assert utc_dt.utcoffset() == timedelta(0)


class TestConfigManagerTimezone:
    """ConfigManager 的时区读取 + reload 行为。"""

    def test_get_timezone_default_empty(self):
        """没填 timezone 字段时返回空字符串（让 configure_timezone 走默认）。"""
        cfg = AstrBotConfig({"basic": {}})
        cm = ConfigManager(cfg)
        assert cm.get_timezone() == ""

    def test_get_timezone_reads_user_value(self):
        """能读出用户填的时区。"""
        cfg = AstrBotConfig({"basic": {"timezone": "UTC"}})
        cm = ConfigManager(cfg)
        assert cm.get_timezone() == "UTC"

    def test_reload_config_updates_timezone_cache(self):
        """reload_config 应当把时区缓存刷成用户配置的值。"""
        # 初始时区设为 UTC（与默认上海不同，便于断言变化）
        configure_timezone("UTC")
        assert active_tz_name() == "UTC"

        # 用户把配置改成东京
        cfg = AstrBotConfig({"basic": {"timezone": "Asia/Tokyo"}})
        cm = ConfigManager(cfg)

        cm.reload_config()  # 无 callback，只刷时区

        # reload 之后，时区缓存应当反映配置
        assert active_tz_name() == "Asia/Tokyo"

    def test_reload_config_invalid_timezone_falls_back(self):
        """用户填了非法时区，reload 后回退到默认，不抛异常。"""
        cfg = AstrBotConfig({"basic": {"timezone": "Not/A/Zone"}})
        cm = ConfigManager(cfg)
        # 不应抛异常
        cm.reload_config()
        assert active_tz_name() == DEFAULT_TZ_NAME

    def test_reload_config_invokes_callback(self):
        """reload_config 应当调用注入的回调（main.py 用它重排调度+重配限流器）。"""
        cfg = AstrBotConfig({"basic": {"timezone": "UTC"}})
        cm = ConfigManager(cfg)

        called = {"count": 0}

        def on_applied():
            called["count"] += 1

        cm.reload_config(on_applied=on_applied)
        assert called["count"] == 1

    def test_reload_config_callback_exception_does_not_break_timezone(self):
        """回调抛异常时，时区缓存仍应已生效（先于回调设置）。"""
        configure_timezone("UTC")
        cfg = AstrBotConfig({"basic": {"timezone": "Asia/Shanghai"}})
        cm = ConfigManager(cfg)

        def bad_callback():
            raise RuntimeError("boom")

        # 不应抛异常出去
        cm.reload_config(on_applied=bad_callback)
        # 时区在回调之前已设置
        assert active_tz_name() == "Asia/Shanghai"
