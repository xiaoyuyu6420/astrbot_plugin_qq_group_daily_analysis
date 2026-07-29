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
from src.infrastructure.reporting.templates import HTMLTemplates
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
    report_generator=None,
    html_render_func=None,
    bot_manager=None,
) -> ScheduledCategoryDigestService:
    if cfg is None:
        cfg = _make_cfg()
    return ScheduledCategoryDigestService(
        config_manager=cfg,
        analysis_service=MagicMock(),
        bot_manager=bot_manager or MagicMock(),
        report_generator=report_generator,
        html_render_func=html_render_func,
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
    analysis.statistics_service.convert_to_legacy_dict = MagicMock(return_value=[])
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
    analysis.statistics_service.convert_to_legacy_dict = MagicMock(return_value=[])
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


def test_pack_digests_payload_split_and_merged():
    svc = _service()
    digests = [
        CategoryDigest(
            name="科技",
            items=[
                ValueItem(content="A 工具", source_group_id="111", reason="资源"),
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
    split = svc.pack_digests_payload(digests, push_mode="split", date_str="2026-07-21")
    assert len(split) == 2
    assert "科技日报" in split[0]["title"]
    assert split[0]["sections"][0]["category_name"] == ""  # split 不重复分类名
    assert split[0]["sections"][0]["entries"][0]["content"] == "A 工具"
    assert "AI日报" in split[1]["title"]

    merged = svc.pack_digests_payload(digests, push_mode="merged", date_str="2026-07-21")
    assert len(merged) == 1
    assert "分类日报" in merged[0]["title"]
    names = [s["category_name"] for s in merged[0]["sections"]]
    assert names == ["科技", "AI"]


def test_render_category_digest_template_nonempty():
    cfg = _make_cfg()
    html = HTMLTemplates(cfg).render_category_digest(
        title="科技日报 2026-07-21",
        date_str="2026-07-21",
        overall_meta="2 群 · 1 条",
        current_datetime="2026-07-21 12:00:00",
        t2i_font_source="Overseas",
        t2i_google_fonts_mirror="https://fonts.googleapis.com",
        t2i_gstatic_mirror="https://fonts.gstatic.com",
        sections=[
            {
                "category_name": "",
                "meta": "2 群 · 1 条",
                "entries": [
                    {
                        "index": 1,
                        "content": "A 工具发布",
                        "reason": "资源",
                        "source_group_id": "111",
                    }
                ],
                "omitted": 0,
            }
        ],
    )
    assert html
    assert "科技日报" in html
    assert "A 工具发布" in html
    assert "群111" in html


def test_send_to_admins_image_preferred_then_text_fallback():
    """图片发送成功走 image_path；图片发送失败回退 text。"""
    adapter = MagicMock()
    adapter.send_private = AsyncMock(side_effect=[True, False, True])
    # 调用顺序：
    # 1) 第一条 delivery 图片成功
    # 2) 第二条 delivery 图片失败
    # 3) 第二条 delivery 文本回退成功

    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=adapter)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    bot._context = None

    cfg = _make_cfg()
    # resolve_admin_qqs 依赖 extra_admin_qq
    svc = _service(cfg, bot_manager=bot)

    deliveries = [
        ("base64://AAA", "文本兜底1"),
        ("base64://BBB", "文本兜底2"),
    ]
    sent = asyncio.run(svc._send_to_admins(deliveries, platform_id="onebot"))
    assert sent == 2

    calls = adapter.send_private.await_args_list
    assert len(calls) == 3
    # 第一次：图片
    assert calls[0].kwargs.get("image_path") == "base64://AAA"
    # 第二次：图片失败
    assert calls[1].kwargs.get("image_path") == "base64://BBB"
    # 第三次：文本回退
    assert calls[2].kwargs.get("text") == "文本兜底2"
    assert "image_path" not in calls[2].kwargs or not calls[2].kwargs.get("image_path")


def test_run_image_format_uses_render_and_send_private_image():
    """category_output_format=image 时走 render_category_digest_image 并私聊图片。"""
    cfg = _make_cfg(
        category_output_format="image",
        category_push_mode="split",
        categories=[{"name": "科技", "groups": ["111"]}],
    )

    adapter = MagicMock()
    adapter.platform_id = "onebot"
    adapter.send_private = AsyncMock(return_value=True)

    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=adapter)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    bot._context = None

    report_generator = MagicMock()
    report_generator.render_category_digest_image = AsyncMock(
        return_value=("base64://IMGDATA", "<html/>")
    )
    html_render = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")

    svc = _service(
        cfg,
        bot_manager=bot,
        report_generator=report_generator,
        html_render_func=html_render,
    )
    # 跳过真实抽取
    svc._build_category_digest = AsyncMock(
        return_value=CategoryDigest(
            name="科技",
            items=[ValueItem(content="A 工具", source_group_id="111")],
            groups_analyzed=["111"],
        )
    )

    result = asyncio.run(svc.run(platform_id="onebot"))
    assert result["success"] is True
    assert result["output_format"] == "image"
    assert result["messages_sent"] == 1
    report_generator.render_category_digest_image.assert_awaited()
    # 私聊应带 image_path
    kwargs = adapter.send_private.await_args.kwargs
    assert kwargs.get("image_path") == "base64://IMGDATA"


def test_run_image_format_falls_back_to_text_when_render_fails():
    cfg = _make_cfg(
        category_output_format="image",
        categories=[{"name": "科技", "groups": ["111"]}],
    )
    adapter = MagicMock()
    adapter.send_private = AsyncMock(return_value=True)
    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=adapter)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    bot._context = None

    report_generator = MagicMock()
    report_generator.render_category_digest_image = AsyncMock(return_value=(None, None))

    svc = _service(
        cfg,
        bot_manager=bot,
        report_generator=report_generator,
        html_render_func=AsyncMock(),
    )
    svc._build_category_digest = AsyncMock(
        return_value=CategoryDigest(
            name="科技",
            items=[ValueItem(content="A 工具", source_group_id="111")],
            groups_analyzed=["111"],
        )
    )

    result = asyncio.run(svc.run(platform_id="onebot"))
    assert result["success"] is True
    kwargs = adapter.send_private.await_args.kwargs
    assert not kwargs.get("image_path")
    assert "A 工具" in kwargs.get("text", "")


def test_run_text_format_skips_image_render():
    cfg = _make_cfg(
        category_output_format="text",
        categories=[{"name": "科技", "groups": ["111"]}],
    )
    adapter = MagicMock()
    adapter.send_private = AsyncMock(return_value=True)
    bot = MagicMock()
    bot.get_adapter = MagicMock(return_value=adapter)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    bot._context = None

    report_generator = MagicMock()
    report_generator.render_category_digest_image = AsyncMock()

    svc = _service(
        cfg,
        bot_manager=bot,
        report_generator=report_generator,
        html_render_func=AsyncMock(),
    )
    svc._build_category_digest = AsyncMock(
        return_value=CategoryDigest(
            name="科技",
            items=[ValueItem(content="纯文本条目", source_group_id="111")],
            groups_analyzed=["111"],
        )
    )

    result = asyncio.run(svc.run(platform_id="onebot"))
    assert result["output_format"] == "text"
    report_generator.render_category_digest_image.assert_not_awaited()
    kwargs = adapter.send_private.await_args.kwargs
    assert "纯文本条目" in kwargs.get("text", "")
    assert not kwargs.get("image_path")


def test_get_category_output_format_defaults_and_validates():
    cfg = ConfigManager(AstrBotConfig({"auto_analysis": {}}))
    assert cfg.get_category_output_format() == "image"

    cfg2 = ConfigManager(
        AstrBotConfig({"auto_analysis": {"category_output_format": "TEXT"}})
    )
    assert cfg2.get_category_output_format() == "text"

    cfg3 = ConfigManager(
        AstrBotConfig({"auto_analysis": {"category_output_format": "html"}})
    )
    assert cfg3.get_category_output_format() == "image"
