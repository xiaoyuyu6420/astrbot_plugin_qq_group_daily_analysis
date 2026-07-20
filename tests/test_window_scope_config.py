"""window_scope（单群/多群）配置兼容性测试。"""

from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


def _cfg(**mm_overrides) -> ConfigManager:
    base = {
        "message_monitor": {
            "enable_monitor": True,
            "monitor_mode": "window",
            "monitored_qqs": ["999"],
            "monitored_groups": ["groupA"],
            **mm_overrides,
        }
    }
    return ConfigManager(AstrBotConfig(base))


def test_default_scope_is_per_group():
    cfg = _cfg()
    assert cfg.get_window_scope() == "per_group"
    assert cfg.is_cross_group_enabled() is False


def test_window_scope_cross_group():
    cfg = _cfg(window_scope="cross_group")
    assert cfg.get_window_scope() == "cross_group"
    assert cfg.is_cross_group_enabled() is True


def test_window_scope_per_group_explicit():
    cfg = _cfg(window_scope="per_group")
    assert cfg.get_window_scope() == "per_group"
    assert cfg.is_cross_group_enabled() is False


def test_legacy_enable_cross_group_true():
    """旧键 enable_cross_group=true 应映射为 cross_group。"""
    cfg = _cfg(enable_cross_group=True)
    assert cfg.get_window_scope() == "cross_group"
    assert cfg.is_cross_group_enabled() is True


def test_legacy_enable_cross_group_false():
    cfg = _cfg(enable_cross_group=False)
    assert cfg.get_window_scope() == "per_group"
    assert cfg.is_cross_group_enabled() is False


def test_window_scope_overrides_legacy_flag():
    """新键优先于旧键。"""
    cfg = _cfg(window_scope="per_group", enable_cross_group=True)
    assert cfg.get_window_scope() == "per_group"
    assert cfg.is_cross_group_enabled() is False


def test_invalid_window_scope_falls_back_to_legacy_or_default():
    cfg = _cfg(window_scope="whatever", enable_cross_group=True)
    assert cfg.get_window_scope() == "cross_group"

    cfg2 = _cfg(window_scope="whatever")
    assert cfg2.get_window_scope() == "per_group"
