"""分类日报二次聚合（DigestTheme）测试。

覆盖：
- _should_aggregate 触发判断（条目数阈值 + analyzer 可用性）
- _aggregate_themes 的 related_items 回填（ids → ValueItem 实例）
- _aggregate_themes 降级（analyzer 异常 → 空 themes）
- _section_from_digest：themes 优先 / 空 themes 回退 entries / 游离条目
- pack_digests 文本：叙事 + 全量附录
- pack_digests_payload：聚合 meta
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.application.services.scheduled_category_digest_service import (
    CategoryDigest,
    ScheduledCategoryDigestService,
    ValueItem,
)
from src.domain.models.data_models import DigestTheme
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


def _make_cfg(**auto_overrides) -> ConfigManager:
    auto = {
        "delivery_mode": "by_category",
        "category_push_mode": "split",
        "category_output_format": "text",
        "categories": [],
    }
    auto.update(auto_overrides)
    return ConfigManager(
        AstrBotConfig(
            {
                "basic": {"group_list_mode": "none"},
                "auto_analysis": auto,
                "admin_notify": {"extra_admin_qq": ["10001"]},
            }
        )
    )


def _service(
    cfg: ConfigManager | None = None,
    *,
    theme_analyzer=None,
) -> ScheduledCategoryDigestService:
    if cfg is None:
        cfg = _make_cfg()
    svc = ScheduledCategoryDigestService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=MagicMock(),
    )
    # 测试里 analysis_service 是 MagicMock，_build_theme_analyzer 会返回 None。
    # 显式注入 theme_analyzer（None 或 mock）来控制聚合行为。
    svc._theme_analyzer = theme_analyzer
    return svc


def _make_items(n: int) -> list[ValueItem]:
    return [ValueItem(content=f"条目{i}", source_group_id="g1") for i in range(1, n + 1)]


def test_aggregate_themes_serializes_importance():
    """聚合输入序列化必须携带 importance（第二层提示词据此过滤轻量条目）。"""
    items = [
        ValueItem(content="重要事件", source_group_id="g1", importance="high"),
        ValueItem(content="一般事件", source_group_id="g1", importance="medium"),
        ValueItem(content="轻量事件", source_group_id="g1", importance="low"),
    ]
    captured: dict = {}

    class _FakeAnalyzer:
        async def analyze(self, serialized, umo=None, session_id=None):
            captured["serialized"] = serialized
            return [], MagicMock()

    svc = _service(theme_analyzer=_FakeAnalyzer())
    svc.analysis_service.llm_semaphore = asyncio.Semaphore(1)
    asyncio.run(svc._aggregate_themes(items, "AI", "onebot"))

    assert [d["importance"] for d in captured["serialized"]] == [
        "high",
        "medium",
        "low",
    ]


# ---------------------------------------------------------------------------
# _should_aggregate
# ---------------------------------------------------------------------------


def test_should_aggregate_false_when_no_analyzer():
    """analyzer 不可用时不触发聚合（降级到平铺）。"""
    svc = _service(theme_analyzer=None)
    assert svc._should_aggregate(100) is False


def test_should_aggregate_false_when_below_threshold():
    """条目数 ≤ 图片展示上限时不触发（省 LLM 成本）。"""
    fake = MagicMock()
    svc = _service(theme_analyzer=fake)
    # 默认 digest_max_items_per_section=15，15 条不触发
    assert svc._should_aggregate(15) is False
    # 16 条触发
    assert svc._should_aggregate(16) is True


# ---------------------------------------------------------------------------
# _section_from_digest：themes 优先 / 降级 / 游离条目
# ---------------------------------------------------------------------------


def test_section_from_digest_themes_priority():
    """有 themes 时渲染聚合主题，entries 留空。"""
    svc = _service()
    items = _make_items(5)
    digest = CategoryDigest(
        name="AI",
        items=items,
        groups_analyzed=["g1"],
        themes=[
            DigestTheme(
                title="主题A",
                narrative="叙事A",
                importance="high",
                tags=["技术"],
                related_item_ids=[1, 2],
                related_items=[items[0], items[1]],
            ),
            DigestTheme(
                title="主题B",
                narrative="叙事B",
                importance="low",
                related_item_ids=[3],
                related_items=[items[2]],
            ),
        ],
    )
    section = svc._section_from_digest(digest, show_name=False)
    assert len(section["themes"]) == 2
    assert section["themes"][0]["title"] == "主题A"
    assert section["themes"][0]["importance"] == "high"
    # 图片版不展示原文（原文在邮件/Markdown），entries 置空
    assert section["themes"][0]["entries"] == []
    assert section["entries"] == []
    # 条目 4,5 未归入任何主题 → 游离
    assert len(section["orphans"]) == 2


def test_section_from_digest_fallback_to_entries():
    """无 themes 时回退平铺条目（降级路径）。"""
    svc = _service()
    items = _make_items(3)
    digest = CategoryDigest(name="科技", items=items, groups_analyzed=["g1"])
    section = svc._section_from_digest(digest)
    assert section["themes"] == []
    assert len(section["entries"]) == 3
    assert section["orphans"] == []


# ---------------------------------------------------------------------------
# _aggregate_themes：回填 + 降级
# ---------------------------------------------------------------------------


def test_aggregate_themes_backfills_related_items():
    """聚合成功时，related_item_ids 被解析成原 ValueItem 实例。"""
    items = _make_items(4)
    # mock analyzer 返回带 related_item_ids 的 themes（related_items 尚未回填）
    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(
        return_value=(
            [
                DigestTheme(
                    title="主题X",
                    narrative="叙事X",
                    importance="high",
                    related_item_ids=[1, 3],
                    related_items=[],
                ),
            ],
            MagicMock(),  # token usage，不关心
        )
    )
    svc = _service(theme_analyzer=fake_analyzer)
    svc.analysis_service.llm_semaphore = asyncio.Semaphore(1)

    themes = asyncio.run(svc._aggregate_themes(items, "AI", "onebot"))

    assert len(themes) == 1
    assert themes[0].title == "主题X"
    # related_items 已从 ids 回填为 ValueItem 实例
    assert len(themes[0].related_items) == 2
    assert themes[0].related_items[0].content == "条目1"
    assert themes[0].related_items[1].content == "条目3"


def test_aggregate_themes_degrades_on_exception():
    """analyzer 抛异常时返回空 themes（降级到平铺渲染）。"""
    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(side_effect=RuntimeError("LLM 挂了"))
    svc = _service(theme_analyzer=fake_analyzer)
    svc.analysis_service.llm_semaphore = asyncio.Semaphore(1)

    themes = asyncio.run(svc._aggregate_themes(_make_items(20), "AI", "onebot"))
    assert themes == []


def test_aggregate_themes_empty_when_no_items():
    """空条目列表不触发聚合。"""
    svc = _service(theme_analyzer=MagicMock())
    themes = asyncio.run(svc._aggregate_themes([], "AI", "onebot"))
    assert themes == []


# ---------------------------------------------------------------------------
# pack_digests 文本：叙事 + 精简游离条目
# ---------------------------------------------------------------------------


def test_pack_digests_text_themes_with_compact_orphans():
    """有 themes 时文本 = 叙事主题 + 精简游离条目（非全量附录）。

    核心诉求是「看得完」：顶部聚合主题（精华认知），底部仅展示未被主题归并的
    游离条目（限量、一行一条），不再堆全量附录。
    """
    svc = _service()
    items = _make_items(3)
    digest = CategoryDigest(
        name="AI",
        items=items,
        groups_analyzed=["g1"],
        themes=[
            DigestTheme(
                title="主题A",
                narrative="这是叙事",
                importance="high",
                related_item_ids=[1],
                related_items=[items[0]],
            ),
        ],
    )
    msgs = svc.pack_digests([digest], push_mode="split", date_str="2026-07-30")
    assert len(msgs) == 1
    body = msgs[0]
    # 叙事主题区
    assert "【今日主题】" in body
    assert "主题A" in body
    assert "这是叙事" in body
    # 条目1 已归入主题A
    assert "条目1" in body
    # 条目2/3 是游离条目，精简展示在「其他信息」区
    assert "【其他信息】" in body
    assert "条目2" in body
    assert "条目3" in body
    # 不再有全量附录堆叠
    assert "【全量信息附录】" not in body


def test_pack_digests_text_themes_orphans_respect_limit():
    """游离条目超过上限时截断，标注省略数。"""
    svc = _service()
    # 20 条，主题只归并 1 条 → 19 条游离，超过默认上限 15
    items = _make_items(20)
    digest = CategoryDigest(
        name="AI",
        items=items,
        groups_analyzed=["g1"],
        themes=[
            DigestTheme(
                title="主题A",
                narrative="叙事",
                importance="high",
                related_item_ids=[1],
                related_items=[items[0]],
            ),
        ],
    )
    msgs = svc.pack_digests([digest], push_mode="split", date_str="2026-07-30")
    body = msgs[0]
    assert "另有" in body  # 19 条游离，展示 15 条，另有 4 条
    assert "条目4" in body  # 省略数标注存在


def test_pack_digests_text_fallback_no_themes():
    """无 themes 时文本回退全量平铺（无叙事区/附录区分隔）。"""
    svc = _service()
    items = _make_items(2)
    digest = CategoryDigest(name="科技", items=items, groups_analyzed=["g1"])
    msgs = svc.pack_digests([digest], push_mode="split", date_str="2026-07-30")
    body = msgs[0]
    assert "【今日主题】" not in body
    assert "【全量信息附录】" not in body
    assert "条目1" in body and "条目2" in body


# ---------------------------------------------------------------------------
# pack_digests_payload：聚合 meta
# ---------------------------------------------------------------------------


def test_pack_payload_meta_shows_aggregation():
    """有 themes 时 overall_meta 显示「→ 聚合 N 主题」。"""
    svc = _service()
    items = _make_items(20)
    digest = CategoryDigest(
        name="AI",
        items=items,
        groups_analyzed=["g1", "g2"],
        themes=[
            DigestTheme(title="T1", narrative="N1", importance="high"),
            DigestTheme(title="T2", narrative="N2", importance="medium"),
        ],
    )
    payloads = svc.pack_digests_payload([digest], push_mode="split", date_str="2026-07-30")
    assert len(payloads) == 1
    assert "聚合 2 主题" in payloads[0]["overall_meta"]
    section = payloads[0]["sections"][0]
    assert len(section["themes"]) == 2


def test_pack_payload_meta_no_aggregation_when_no_themes():
    """无 themes 时 overall_meta 不带聚合字样。"""
    svc = _service()
    digest = CategoryDigest(name="科技", items=_make_items(3), groups_analyzed=["g1"])
    payloads = svc.pack_digests_payload([digest], push_mode="split", date_str="2026-07-30")
    assert "聚合" not in payloads[0]["overall_meta"]


# ---------------------------------------------------------------------------
# 配置项
# ---------------------------------------------------------------------------


def test_config_digest_aggregation_enabled_default_true():
    cfg = _make_cfg()
    assert cfg.is_digest_aggregation_enabled() is True


def test_config_digest_aggregation_disabled():
    cfg = _make_cfg(digest_aggregation_enabled=False)
    assert cfg.is_digest_aggregation_enabled() is False


def test_config_digest_max_themes_default_and_clamp():
    cfg = _make_cfg()
    assert cfg.get_digest_max_themes() == 5
    cfg2 = _make_cfg(digest_max_themes=99)
    assert cfg2.get_digest_max_themes() == 10  # clamp 上限
    cfg3 = _make_cfg(digest_max_themes=1)
    assert cfg3.get_digest_max_themes() == 3  # clamp 下限


def test_service_disables_aggregation_when_config_off():
    """配置关闭聚合时，_build_theme_analyzer 返回 None。"""
    cfg = _make_cfg(digest_aggregation_enabled=False)
    svc = ScheduledCategoryDigestService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=MagicMock(),
    )
    assert svc._theme_analyzer is None
    assert svc._should_aggregate(100) is False
