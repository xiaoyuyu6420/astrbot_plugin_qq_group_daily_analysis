"""采集简报（CollectionDigestService）与链接富化（web_fetcher）测试。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.application.services.collection_digest_service import CollectionDigestService
from src.domain.value_objects.unified_message import (
    MessageContent,
    MessageContentType,
    UnifiedMessage,
)
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.web_fetcher import (
    LinkInfo,
    extract_urls,
    is_fetchable_url,
    parse_link_meta,
)
from tests.conftest import AstrBotConfig


def _make_cfg(**collection_overrides) -> ConfigManager:
    collection = {
        "enabled": True,
        "group_ids": ["888001"],
        "digest_time": ["22:00"],
        "lookback_days": 1,
        "link_fetch_enabled": True,
        "max_links": 30,
        "max_message_chars": 20000,
        "prompt": "",
    }
    collection.update(collection_overrides)
    return ConfigManager(
        AstrBotConfig(
            {
                "basic": {"group_list_mode": "none"},
                "collection_digest": collection,
                "admin_notify": {"extra_admin_qq": ["10001"]},
            }
        )
    )


def _msg(text: str, sender: str = "采集者", ts: int = 1755500000) -> UnifiedMessage:
    contents = (MessageContent(type=MessageContentType.TEXT, text=text),)
    return UnifiedMessage(
        message_id=f"m{ts}",
        sender_id="10086",
        sender_name=sender,
        group_id="888001",
        text_content=text,
        contents=contents,
        timestamp=ts,
        platform="onebot",
    )


def _make_service(cfg: ConfigManager | None = None, tmp_path=None) -> tuple[CollectionDigestService, MagicMock]:
    adapter = MagicMock()
    adapter.fetch_messages = AsyncMock(return_value=[])
    adapter.send_private = AsyncMock(return_value=True)
    bot_manager = MagicMock()
    bot_manager.get_adapter = MagicMock(return_value=adapter)
    bot_manager.get_platform_ids = MagicMock(return_value=["aiocqht:wxid1"])
    bot_manager._context = MagicMock()
    bot_manager._context.get_config = MagicMock(return_value={"admins_id": ["10001"]})

    analysis_service = MagicMock()
    analysis_service.llm_analyzer.context = SimpleNamespace(llm_generate=AsyncMock())

    import pytest

    svc = CollectionDigestService(
        config_manager=cfg or _make_cfg(),
        analysis_service=analysis_service,
        bot_manager=bot_manager,
        data_dir=tmp_path if tmp_path else _TMP_DIR,
    )
    return svc, adapter


_TMP_DIR = "/tmp/test_collection_digest_store"


# ----------------------------------------------------------------------
# web_fetcher 单元
# ----------------------------------------------------------------------


def test_extract_urls_filters_media_and_private():
    text = "看这个 https://example.com/a?x=1 和 http://pic.xx.com/i.jpg 还有 https://192.168.1.1/admin 重复 https://example.com/a?x=1"
    urls = extract_urls(text)
    assert urls == ["https://example.com/a?x=1"]


def test_extract_urls_trims_trailing_punct():
    assert extract_urls("详见 https://example.com/post。") == ["https://example.com/post"]


def test_is_fetchable_url_rejects_unsafe():
    assert not is_fetchable_url("http://localhost/admin")
    assert not is_fetchable_url("ftp://example.com/x")
    assert not is_fetchable_url("https://10.0.0.5/api")
    assert is_fetchable_url("https://mp.weixin.qq.com/s/abc")


def test_parse_link_meta_prefers_og():
    html = """
    <html><head>
    <title>页面标题</title>
    <meta property="og:title" content="OG 标题"/>
    <meta property="og:description" content="OG 摘要&lt;b&gt;加粗&lt;/b&gt;  内容"/>
    </head></html>
    """
    title, desc = parse_link_meta(html)
    assert title == "OG 标题"
    assert desc == "OG 摘要<b>加粗</b> 内容"


def test_parse_link_meta_falls_back_to_title():
    title, desc = parse_link_meta("<html><head><title>只有标题</title></head></html>")
    assert title == "只有标题"
    assert desc == ""


def test_decode_body_gbk():
    from src.infrastructure.web_fetcher import _decode_body

    body = "<html><head><meta charset=\"gbk\"/><title>中文标题</title></head></html>".encode("gbk")
    text = _decode_body(body, "text/html", b"")
    assert "中文标题" in text


# ----------------------------------------------------------------------
# 服务级
# ----------------------------------------------------------------------


def test_run_skips_without_groups(tmp_path):
    svc, adapter = _make_service(_make_cfg(group_ids=[]), tmp_path)
    result = asyncio.run(svc.run())
    assert result["reason"] == "no_groups"
    adapter.fetch_messages.assert_not_awaited()


def test_run_no_content_no_push(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    adapter.fetch_messages = AsyncMock(return_value=[])
    result = asyncio.run(svc.run())
    assert result["reason"] == "no_content"
    adapter.send_private.assert_not_awaited()


def test_run_full_flow_with_link_enrichment(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    adapter.fetch_messages = AsyncMock(
        return_value=[
            _msg("这篇不错 https://example.com/article", ts=1755500100),
            _msg("随便聊两句", ts=1755500200),
            _msg("工具站 https://tool.example.io/", sender="路人", ts=1755500300),
        ]
    )
    fake_links = {
        "https://example.com/article": LinkInfo(
            url="https://example.com/article", title="深度好文", description="关于某某的思考"
        ),
        "https://tool.example.io/": LinkInfo(url="https://tool.example.io/", fetch_failed=True),
    }

    async def fake_llm(**kwargs):
        assert "深度好文" in kwargs["prompt"]
        assert "https://tool.example.io/" in kwargs["prompt"]
        return SimpleNamespace(completion_text="## 剪报\n- 深度好文…")

    with (
        patch(
            "src.application.services.collection_digest_service.fetch_links_batch",
            new=AsyncMock(return_value=fake_links),
        ) as mock_fetch,
        patch(
            "src.application.services.collection_digest_service.call_provider_with_retry",
            new=AsyncMock(side_effect=fake_llm),
        ),
    ):
        result = asyncio.run(svc.run())

    assert result["success"] is True
    assert result["items"] == 3
    assert result["llm_ok"] is True
    mock_fetch.assert_awaited_once()
    # 私聊送达
    adapter.send_private.assert_awaited()
    sent_text = adapter.send_private.await_args_list[0].kwargs.get("text", "")
    assert "剪报" in sent_text
    # 落盘 + 水位
    assert result["digest_path"] and json.loads(
        (tmp_path / "collection_digests" / "watermark.json").read_text()
    )["last_success_ts"] > 0


def test_run_watermark_blocks_second_run(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    adapter.fetch_messages = AsyncMock(
        return_value=[_msg("https://example.com/a", ts=1755500100)]
    )
    with (
        patch(
            "src.application.services.collection_digest_service.fetch_links_batch",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.application.services.collection_digest_service.call_provider_with_retry",
            new=AsyncMock(return_value=SimpleNamespace(completion_text="简报")),
        ),
    ):
        first = asyncio.run(svc.run())
        assert first["success"] is True
        # 水位生效：第二次拉消息应带 since_ts
        second_kwargs = adapter.fetch_messages.await_args.kwargs
        assert second_kwargs.get("since_ts", 0) > 0
        # 窗口内无新消息
        adapter.fetch_messages = AsyncMock(return_value=[])
        second = asyncio.run(svc.run())
        assert second["reason"] == "no_content"


def test_run_llm_failure_falls_back_to_raw_list(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    adapter.fetch_messages = AsyncMock(
        return_value=[_msg("看看 https://example.com/x", ts=1755500100)]
    )
    with (
        patch(
            "src.application.services.collection_digest_service.fetch_links_batch",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.application.services.collection_digest_service.call_provider_with_retry",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = asyncio.run(svc.run())

    assert result["success"] is True
    assert result["llm_ok"] is False
    sent_text = adapter.send_private.await_args_list[0].kwargs.get("text", "")
    assert "https://example.com/x" in sent_text
    assert "采集者" in sent_text


def test_run_splits_long_digest(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    long_text = "很长的内容 " * 2000
    adapter.fetch_messages = AsyncMock(return_value=[_msg(long_text[:800])])
    with (
        patch(
            "src.application.services.collection_digest_service.fetch_links_batch",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.application.services.collection_digest_service.call_provider_with_retry",
            new=AsyncMock(return_value=SimpleNamespace(completion_text="分" * 8000)),
        ),
    ):
        asyncio.run(svc.run())
    texts = [c.kwargs.get("text", "") for c in adapter.send_private.await_args_list]
    assert len(texts) >= 3
    assert all(len(t) <= 3500 for t in texts)


def test_run_command_only_message_skipped(tmp_path):
    svc, adapter = _make_service(tmp_path=tmp_path)
    adapter.fetch_messages = AsyncMock(return_value=[_msg("/help 命令", ts=1755500100)])
    result = asyncio.run(svc.run())
    assert result["reason"] == "no_content"


# ----------------------------------------------------------------------
# 调度注册
# ----------------------------------------------------------------------


def test_scheduler_registers_collection_jobs_even_when_daily_report_disabled():
    import sys
    import types

    # 测试环境无 apscheduler：注入最小假模块（CronTrigger 只需可构造）
    if "apscheduler.triggers.cron" not in sys.modules:
        class _FakeCronTrigger:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        fake_cron = types.ModuleType("apscheduler.triggers.cron")
        fake_cron.CronTrigger = _FakeCronTrigger
        sys.modules["apscheduler"] = types.ModuleType("apscheduler")
        sys.modules["apscheduler.triggers"] = types.ModuleType("apscheduler.triggers")
        sys.modules["apscheduler.triggers.cron"] = fake_cron

    from src.infrastructure.scheduler.auto_scheduler import AutoScheduler

    cfg = _make_cfg()
    # 日报关闭（无白名单无分类）
    cfg_daily_off = ConfigManager(
        AstrBotConfig(
            {
                "basic": {"group_list_mode": "whitelist", "group_list": []},
                "auto_analysis": {"delivery_mode": "per_group"},
                "collection_digest": {
                    "enabled": True,
                    "group_ids": ["888001"],
                    "digest_time": ["09:30", "22:00"],
                },
                "admin_notify": {},
            }
        )
    )

    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.get_job = MagicMock(return_value=None)
    context = MagicMock()
    context.cron_manager.scheduler = scheduler

    auto = AutoScheduler(cfg_daily_off, MagicMock(), MagicMock(), MagicMock(), None)
    auto.schedule_jobs(context)

    job_ids = [c.kwargs.get("id") for c in scheduler.add_job.call_args_list]
    collection_ids = [j for j in job_ids if j and "_collection_" in j]
    assert len(collection_ids) == 2
