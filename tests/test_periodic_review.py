"""周期复盘（周报/月报）与日报存档模式测试。

覆盖：
- load_daily_digests 时间窗过滤
- serialize_day_themes 拍平日主题
- 周报全流程（LLM mock 掉）：payload 标题覆写、发送、落盘
- 月报：优先吃周报，周报不足时回退日报；HTML 存档内容
- 日报 QQ 推送闸门（存档模式）：QQ 不推、邮件照发、落盘照常
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.application.services.monthly_review_service import (
    MonthlyReviewService,
    _prev_month_range,
)
from src.application.services.scheduled_category_digest_service import (
    CategoryDigest,
    ScheduledCategoryDigestService,
    ValueItem,
)
from src.application.services.weekly_review_service import (
    WeeklyReviewService,
    load_daily_digests,
    serialize_day_themes,
)
from src.domain.models.data_models import DigestTheme
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


# ---------------------------------------------------------------------------
# 公共构造
# ---------------------------------------------------------------------------


def _make_cfg(**overrides) -> ConfigManager:
    auto = {
        "delivery_mode": "by_category",
        "category_push_mode": "split",
        "category_output_format": "text",
        "categories": [],
    }
    auto.update(overrides.pop("auto_analysis", {}))
    cfg = {
        "basic": {"group_list_mode": "none"},
        "auto_analysis": auto,
        "admin_notify": {"extra_admin_qq": ["10001"]},
        "periodic_review": {
            "weekly_enabled": True,
            "weekly_time": "08:30",
            "weekly_day": 0,
            "monthly_enabled": True,
            "monthly_time": "08:45",
            "monthly_day": 1,
        },
    }
    cfg.update(overrides)
    return ConfigManager(AstrBotConfig(cfg))


def _digest_json(day: str, themes: list[dict]) -> dict:
    """构造一份日报存档。day 形如 2026-09-15。"""
    return {
        "timestamp": f"{day}T02:30:00+08:00",
        "llm_error": None,
        "digests": [
            {
                "name": "技术/AI",
                "items": [
                    {
                        "content": f"{day} 的原始条目{i}",
                        "reason": "话题",
                        "source_group_id": "1021544792",
                        "source_group_name": "雷达群",
                        "source_user_name": f"群友{i}",
                    }
                    for i in range(1, 4)
                ],
                "themes": themes,
                "groups_analyzed": ["1021544792"],
                "groups_skipped": [],
            }
        ],
        "deliveries": [],
        "sent_count": 1,
    }


def _theme(title: str, narrative: str, ids: list[int], importance: str = "high") -> dict:
    return {
        "title": title,
        "narrative": narrative,
        "importance": importance,
        "tags": ["测试"],
        "related_item_ids": ids,
    }


def _write_daily(digests_dir, day: datetime, themes: list[dict]) -> None:
    name = f"digest_{day.strftime('%Y%m%d')}_023000.json"
    (digests_dir / name).write_text(
        json.dumps(_digest_json(day.strftime("%Y-%m-%d"), themes), ensure_ascii=False),
        encoding="utf-8",
    )


def _make_weekly_service(tmp_path, cfg=None) -> WeeklyReviewService:
    analysis = MagicMock()
    analysis.llm_analyzer.context = SimpleNamespace()
    svc = WeeklyReviewService(
        config_manager=cfg or _make_cfg(),
        analysis_service=analysis,
        bot_manager=MagicMock(),
        data_dir=tmp_path,
    )
    return svc


# ---------------------------------------------------------------------------
# 加载与序列化
# ---------------------------------------------------------------------------


def test_load_daily_digests_filters_window(tmp_path):
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    today = datetime.now()
    # 窗口内 3 天 + 窗口外 1 天（10 天前）+ 一个坏文件
    for i in (0, 2, 4):
        _write_daily(digests_dir, today - timedelta(days=i), [])
    _write_daily(digests_dir, today - timedelta(days=10), [])
    (digests_dir / "digest_broken.json").write_text("{bad", encoding="utf-8")

    since = today - timedelta(days=6)
    got = load_daily_digests(digests_dir, since, today)
    assert len(got) == 3
    dates = [d for d, _ in got]
    assert dates == sorted(dates)


def test_serialize_day_themes(tmp_path):
    today = datetime.now()
    day1 = today - timedelta(days=2)
    day2 = today - timedelta(days=1)
    t1 = _theme("A模型降价", "【主旨】A 降价", [1, 2])
    t2 = _theme("B工具封号潮", "【主旨】B 封号", [3])
    daily = [
        (day1.strftime("%Y-%m-%d"), _digest_json(day1.strftime("%Y-%m-%d"), [t1])),
        (day2.strftime("%Y-%m-%d"), _digest_json(day2.strftime("%Y-%m-%d"), [t2])),
    ]
    serialized, items, groups = serialize_day_themes(daily)
    assert len(serialized) == 2
    assert len(items) == 2
    assert serialized[0]["item_id"] == 1
    assert serialized[1]["item_id"] == 2
    # 来源带日期+分类，周报能据此判断跨日持续
    assert day1.strftime("%Y-%m-%d") in serialized[0]["source"]
    assert "技术/AI" in serialized[0]["source"]
    assert "A模型降价" in serialized[0]["content"]
    assert "B工具封号潮" in items[1].content
    assert groups == ["1021544792"]


# ---------------------------------------------------------------------------
# 周报全流程（LLM / 发送 mock 掉）
# ---------------------------------------------------------------------------


def test_weekly_run_full_flow(tmp_path):
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    today = datetime.now()
    for i in range(3):
        _write_daily(
            digests_dir,
            today - timedelta(days=i),
            [_theme(f"主题{i}", f"【主旨】第{i}天叙事", [1, 2])],
        )

    svc = _make_weekly_service(tmp_path)
    # LLM 聚合 mock：把 3 个日主题归并成 1 条周主线
    llm_theme = DigestTheme(
        title="workbuddy2api 一周走红",
        narrative="【主旨】这条线本周持续走",
        importance="high",
        tags=["主线"],
        related_item_ids=[1, 2, 3],
    )
    svc._analyzer = MagicMock()
    svc._analyzer.analyze = AsyncMock(return_value=([llm_theme], None))

    # 发送通道 mock：文本模式下只走 _send_to_admins / _send_digest_email
    svc._renderer._send_to_admins = AsyncMock(return_value=1)
    svc._renderer._send_digest_email = AsyncMock(return_value=1)

    result = asyncio.run(svc.run(platform_id=None))

    assert result["success"] is True
    assert result["themes"] == 1
    assert result["days"] == 3

    # 主题回填了 related_items（可溯源）
    assert len(llm_theme.related_items) == 3
    assert svc._renderer._send_to_admins.called
    assert svc._renderer._send_digest_email.called

    # 落盘：weekly_YYYYMMDD.json 带 themes
    saved = list((tmp_path / "reviews").glob("weekly_*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["period"] == "week"
    assert data["themes"][0]["title"] == "workbuddy2api 一周走红"
    assert data["related"] if "related" in data else True


def test_weekly_skips_when_too_few_digests(tmp_path):
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    _write_daily(digests_dir, datetime.now(), [_theme("t", "n", [1])])
    svc = _make_weekly_service(tmp_path)
    result = asyncio.run(svc.run(platform_id=None))
    assert result["success"] is False
    assert result["reason"] == "not_enough_daily_digests"


# ---------------------------------------------------------------------------
# 月报：周报优先 / 日报兜底 / HTML 存档
# ---------------------------------------------------------------------------


def test_monthly_prev_month_range():
    # 9/1 触发 → 回顾 8 月
    label, start, end = _prev_month_range(datetime(2026, 9, 1))
    assert label == "2026-08"
    assert start == "2026-08-01" and end == "2026-08-31"


def test_monthly_prefers_weekly_then_falls_back(tmp_path):
    review_dir = tmp_path / "reviews"
    digests_dir = tmp_path / "digests"
    review_dir.mkdir()
    digests_dir.mkdir()

    cfg = _make_cfg()
    analysis = MagicMock()
    analysis.llm_analyzer.context = SimpleNamespace()
    svc = MonthlyReviewService(
        config_manager=cfg,
        analysis_service=analysis,
        bot_manager=MagicMock(),
        data_dir=tmp_path,
    )

    label, start, end = _prev_month_range(datetime.now())

    # 场景 1：无周报 → 回退日报
    _write_daily(digests_dir, datetime.strptime(start, "%Y-%m-%d"), [_theme("日主题", "叙事", [1])])
    serialized, items, desc = svc._collect_month_items(start, end)
    assert len(serialized) == 1
    assert "日报" in desc

    # 场景 2：丢两份周报进去 → 优先吃周报
    for i, day in enumerate((3, 10)):
        (review_dir / f"weekly_{start[:4]}{start[5:7]}{day:02d}.json").write_text(
            json.dumps(
                {
                    "period": "week",
                    "range": [start, end],
                    "themes": [
                        {
                            "title": f"周主线{i}",
                            "narrative": "【主旨】周叙事",
                            "importance": "high",
                            "tags": [],
                            "related_item_ids": [1],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    serialized2, items2, desc2 = svc._collect_month_items(start, end)
    assert "周报" in desc2
    assert any("周主线0" in s["content"] for s in serialized2)


def test_monthly_html_archive(tmp_path):
    cfg = _make_cfg()
    svc = MonthlyReviewService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=MagicMock(),
        data_dir=tmp_path,
    )
    theme = DigestTheme(
        title="月度榜一",
        narrative="【定位】测试\n【结论】已沉淀",
        importance="high",
        tags=["榜单"],
        related_item_ids=[1],
    )
    theme.related_items = [
        ValueItem(content="关联条目原文", source_group_name="周报 09-01~09-07")
    ]
    digest = CategoryDigest(
        name="月报 2026-08",
        items=theme.related_items,
        groups_analyzed=["1 份周报"],
        themes=[theme],
    )
    path = svc._persist_html(digest, "2026-08", "2026-08-01", "2026-08-31")
    html = path.read_text(encoding="utf-8")
    assert "每月情报回顾 2026-08" in html
    assert "月度榜一" in html
    assert "关联条目原文" in html  # 主题下的关联条目进了存档
    assert "2026-08-01 ~ 2026-08-31" in html


# ---------------------------------------------------------------------------
# 日报存档模式（QQ 推送闸门）
# ---------------------------------------------------------------------------


def test_daily_qq_push_gate_config():
    # 默认：推
    cfg = _make_cfg()
    assert cfg.is_daily_qq_push_enabled(total_items=10) is True
    # 关了且阈值 0：不推
    cfg2 = _make_cfg(
        auto_analysis={"daily_qq_push": False, "daily_qq_push_min_items": 0}
    )
    assert cfg2.is_daily_qq_push_enabled(total_items=10) is False
    # 关了但设了阈值 50：小事不推，大事例外
    cfg3 = _make_cfg(
        auto_analysis={"daily_qq_push": False, "daily_qq_push_min_items": 50}
    )
    assert cfg3.is_daily_qq_push_enabled(total_items=41) is False
    assert cfg3.is_daily_qq_push_enabled(total_items=52) is True


def test_daily_run_archive_mode_skips_qq(tmp_path):
    """存档模式：_send_to_admins 不被调用，邮件照发，落盘照常。"""
    cfg = _make_cfg(
        auto_analysis={"daily_qq_push": False, "daily_qq_push_min_items": 0}
    )
    svc = ScheduledCategoryDigestService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=MagicMock(),
        data_dir=tmp_path,
    )
    digest = CategoryDigest(
        name="技术/AI",
        items=[ValueItem(content="x", source_group_name="雷达群")],
        groups_analyzed=["1"],
        themes=[],
    )
    svc._build_category_digest = AsyncMock(return_value=digest)
    svc.config_manager.get_push_categories = MagicMock(
        return_value=[SimpleNamespace(name="技术/AI", groups=["1"])]
    )
    svc._send_to_admins = AsyncMock(return_value=0)
    svc._send_digest_email = AsyncMock(return_value=1)

    result = asyncio.run(svc.run(platform_id=None))

    assert not svc._send_to_admins.called, "存档模式不应推 QQ"
    assert svc._send_digest_email.called, "邮件通道应保持"
    assert result["success"] is True  # 存档模式不算失败
    assert result["qq_push"] is False
    # 落盘照常
    assert list((tmp_path / "digests").glob("digest_*.json"))
