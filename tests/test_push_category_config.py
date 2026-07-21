"""用户分类配置解析与 delivery_mode 兼容性测试。"""

from src.domain.entities.push_category import PushCategory, normalize_push_categories
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


def test_normalize_list_of_dicts():
    cats = normalize_push_categories(
        [
            {"name": "科技", "groups": ["111", "222", "111"]},
            {"name": "AI", "groups": [333]},
            {"name": "空", "groups": []},
            {"name": "", "groups": ["1"]},
        ]
    )
    assert len(cats) == 2
    assert cats[0].name == "科技"
    assert cats[0].groups == ["111", "222"]
    assert cats[1].name == "AI"
    assert cats[1].groups == ["333"]


def test_normalize_json_string():
    raw = '[{"name":"科技","groups":["123456","234567"]},{"name":"AI","groups":["345678"]}]'
    cats = normalize_push_categories(raw)
    assert [c.name for c in cats] == ["科技", "AI"]
    assert cats[0].groups == ["123456", "234567"]


def test_normalize_umo_group_ids():
    cats = normalize_push_categories(
        [{"name": "科技", "groups": ["onebot:GroupMessage:999888"]}]
    )
    assert cats[0].groups == ["999888"]


def test_normalize_invalid_returns_empty():
    assert normalize_push_categories(None) == []
    assert normalize_push_categories("") == []
    assert normalize_push_categories("not-json") == []
    assert normalize_push_categories(42) == []


def test_config_manager_delivery_defaults():
    cfg = ConfigManager(AstrBotConfig({"auto_analysis": {}}))
    assert cfg.get_delivery_mode() == "per_group"
    assert cfg.get_category_push_mode() == "split"
    assert cfg.get_push_categories() == []
    assert cfg.is_auto_analysis_enabled() is False


def test_config_manager_by_category_enabled_by_categories():
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "auto_analysis": {
                    "delivery_mode": "by_category",
                    "categories": [{"name": "科技", "groups": ["1"]}],
                    "scheduled_group_list": [],  # per_group 名单空也不影响
                }
            }
        )
    )
    assert cfg.get_delivery_mode() == "by_category"
    assert cfg.is_auto_analysis_enabled() is True
    assert cfg.get_push_categories()[0].name == "科技"


def test_config_manager_by_category_empty_disabled():
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "auto_analysis": {
                    "delivery_mode": "by_category",
                    "categories": [],
                }
            }
        )
    )
    assert cfg.is_auto_analysis_enabled() is False


def test_config_manager_categories_json_text():
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "auto_analysis": {
                    "delivery_mode": "by_category",
                    "category_push_mode": "merged",
                    "categories": '[{"name":"AI","groups":["9"]}]',
                }
            }
        )
    )
    assert cfg.get_category_push_mode() == "merged"
    cats = cfg.get_push_categories()
    assert len(cats) == 1
    assert isinstance(cats[0], PushCategory)
    assert cats[0].groups == ["9"]


def test_invalid_delivery_mode_falls_back():
    cfg = ConfigManager(
        AstrBotConfig({"auto_analysis": {"delivery_mode": "whatever"}})
    )
    assert cfg.get_delivery_mode() == "per_group"
