"""定时分类聚合打包与白名单过滤测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.application.services.scheduled_category_digest_service import (
    CategoryDigest,
    ScheduledCategoryDigestService,
    ValueItem,
)
from src.domain.entities.push_category import PushCategory
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


def _service(cfg: ConfigManager | None = None) -> ScheduledCategoryDigestService:
    if cfg is None:
        cfg = ConfigManager(
            AstrBotConfig(
                {
                    "basic": {"group_list_mode": "none"},
                    "auto_analysis": {
                        "delivery_mode": "by_category",
                        "category_push_mode": "split",
                        "categories": [],
                    },
                    "admin_notify": {"extra_admin_qq": ["10001"]},
                }
            )
        )
    return ScheduledCategoryDigestService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=MagicMock(),
    )


def test_pack_split_one_message_per_category():
    svc = _service()
    digests = [
        CategoryDigest(
            name="科技",
            items=[
                ValueItem(content="A 工具发布", source_group_id="111", reason="资源"),
                ValueItem(content="B 商机", source_group_id="222"),
            ],
            groups_analyzed=["111", "222"],
        ),
        CategoryDigest(
            name="AI",
            items=[ValueItem(content="模型更新", source_group_id="333")],
            groups_analyzed=["333"],
        ),
        CategoryDigest(name="闲聊", items=[], groups_analyzed=["444"]),
    ]
    msgs = svc.pack_digests(digests, push_mode="split", date_str="2026-07-21")
    assert len(msgs) == 2
    assert "科技日报 2026-07-21" in msgs[0]
    assert "[群111]" in msgs[0]
    assert "AI日报 2026-07-21" in msgs[1]
    assert "闲聊" not in msgs[0] and "闲聊" not in msgs[1]


def test_pack_merged_single_message_with_sections():
    svc = _service()
    digests = [
        CategoryDigest(
            name="科技",
            items=[ValueItem(content="条目1", source_group_id="1")],
            groups_analyzed=["1"],
        ),
        CategoryDigest(
            name="AI",
            items=[ValueItem(content="条目2", source_group_id="2")],
            groups_analyzed=["2"],
        ),
    ]
    msgs = svc.pack_digests(digests, push_mode="merged", date_str="2026-07-21")
    assert len(msgs) == 1
    body = msgs[0]
    assert "分类日报 2026-07-21" in body
    assert "## 科技" in body
    assert "## AI" in body
    assert "条目1" in body and "条目2" in body


def test_pack_all_empty_returns_overview():
    svc = _service()
    digests = [
        CategoryDigest(name="科技", items=[], groups_analyzed=[], groups_skipped=["1"]),
    ]
    msgs = svc.pack_digests(digests, push_mode="split", date_str="2026-07-21")
    assert len(msgs) == 1
    assert "未提取到有价值信息" in msgs[0]


def test_value_item_fingerprint_dedup_key():
    a = ValueItem(content="同一 内容")
    b = ValueItem(content="同一内容")
    assert a.ensure_fingerprint() == b.ensure_fingerprint()


def test_categories_groups_auto_admitted_in_whitelist_mode():
    """by_category 下，categories 里的群即使不在 basic 白名单也应自动放行。

    用户只在 categories 里填了群号，basic.group_list 留空/不全，定时聚合不应再静默跳过。
    """
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "basic": {
                    "group_list_mode": "whitelist",
                    "group_list": ["111"],
                },
                "auto_analysis": {
                    "delivery_mode": "by_category",
                    "categories": [
                        {"name": "科技", "groups": ["111", "222"]},
                    ],
                },
                "admin_notify": {"extra_admin_qq": ["10001"]},
                "analysis_features": {
                    "topic_analysis_enabled": False,
                    "golden_quote_analysis_enabled": False,
                },
            }
        )
    )

    analysis = MagicMock()
    analysis.analysis_domain_service.analyze_user_activity = MagicMock(return_value={})
    analysis.statistics_service._convert_to_legacy_dict = MagicMock(return_value=[])
    analysis.llm_semaphore = asyncio.Semaphore(1)
    analysis.llm_analyzer.analyze_all_concurrent = AsyncMock(
        return_value=([], [], None)
    )

    adapter = MagicMock()
    adapter.platform_id = "onebot"
    adapter.fetch_messages = AsyncMock(
        return_value=[
            SimpleNamespace(
                sender_id="1",
                sender_name="u",
                content="hello world enough text",
                timestamp=1,
                message_id="m1",
            )
        ]
        * 30
    )

    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=adapter)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    bot._context = None

    # clean_messages path needs real cleaner-compatible messages; mock extract instead
    svc = ScheduledCategoryDigestService(cfg, analysis, bot)
    svc._extract_group_value = AsyncMock(
        side_effect=lambda gid, platform_id=None: [
            ValueItem(content=f"from {gid}", source_group_id=str(gid))
        ]
    )

    digest = asyncio.run(
        svc._build_category_digest(
            PushCategory(name="科技", groups=["111", "222"]),
            platform_id="onebot",
            max_concurrent=2,
            stagger=0,
        )
    )
    # 新语义：111 和 222 都应被分析（222 虽不在 basic 白名单，但在 categories 里 → 放行）
    assert digest.groups_analyzed == ["111", "222"]
    assert digest.groups_skipped == []
    assert len(digest.items) == 2


def test_blacklist_still_skips_categories_group():
    """by_category 下，basic 黑名单优先级最高：categories 里列了的群仍会被黑名单挡掉。

    验证"显式屏蔽"语义不被 categories 放行逻辑覆盖。
    """
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "basic": {
                    "group_list_mode": "blacklist",
                    "group_list": ["222"],  # 222 被显式屏蔽
                },
                "auto_analysis": {
                    "delivery_mode": "by_category",
                    "categories": [
                        {"name": "科技", "groups": ["111", "222"]},
                    ],
                },
                "admin_notify": {"extra_admin_qq": ["10001"]},
                "analysis_features": {
                    "topic_analysis_enabled": False,
                    "golden_quote_analysis_enabled": False,
                },
            }
        )
    )

    analysis = MagicMock()
    analysis.analysis_domain_service.analyze_user_activity = MagicMock(return_value={})
    analysis.statistics_service._convert_to_legacy_dict = MagicMock(return_value=[])
    analysis.llm_semaphore = asyncio.Semaphore(1)
    analysis.llm_analyzer.analyze_all_concurrent = AsyncMock(
        return_value=([], [], None)
    )

    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=MagicMock(platform_id="onebot"))
    bot.get_platform_ids = MagicMock(return_value=["onebot"])

    svc = ScheduledCategoryDigestService(cfg, analysis, bot)
    svc._extract_group_value = AsyncMock(
        side_effect=lambda gid, platform_id=None: [
            ValueItem(content=f"from {gid}", source_group_id=str(gid))
        ]
    )

    digest = asyncio.run(
        svc._build_category_digest(
            PushCategory(name="科技", groups=["111", "222"]),
            platform_id="onebot",
            max_concurrent=2,
            stagger=0,
        )
    )
    # 111 放行；222 命中黑名单 → 跳过
    assert digest.groups_analyzed == ["111"]
    assert digest.groups_skipped == ["222"]
    assert len(digest.items) == 1
    assert digest.items[0].source_group_id == "111"
