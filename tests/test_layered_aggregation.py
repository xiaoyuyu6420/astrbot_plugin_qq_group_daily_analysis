"""分层聚合 + 分类频道单测。"""

import asyncio
import re
from unittest.mock import MagicMock

import pytest

from src.application.services.layered_aggregation import (
    ChannelPacker,
    CrossGroupAggregator,
    GroupCandidateExtractor,
    content_fingerprint,
    extract_all_group_candidates,
)
from src.domain.entities.intel_item import IntelItem
from src.domain.services.intel_taxonomy import (
    CHANNEL_APIKEY,
    CHANNEL_DEAL,
    CHANNEL_INTEL,
    CHANNEL_METHOD,
    CHANNEL_OTHER,
    CHANNEL_RESOURCE,
    PRIORITY_CRITICAL,
    PRIORITY_NORMAL,
    normalize_channel,
)


# 复用 monitor 内置正则
PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API Key", "OpenAI Key"),
    (re.compile(r"https?://\S{10,}"), "资源链接", "网址"),
    (re.compile(r"magnet:\?\S+", re.IGNORECASE), "资源链接", "磁力"),
]


def _msg(sender, group, text, name=""):
    return {
        "sender_id": sender,
        "group_id": group,
        "platform_id": "aiocqhttp:default",
        "text": text,
        "name": name,
    }


# ---------- Taxonomy ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("API Key", CHANNEL_APIKEY),
        ("apikey", CHANNEL_APIKEY),
        ("API key", CHANNEL_APIKEY),
        ("资源链接", CHANNEL_RESOURCE),
        ("资源", CHANNEL_RESOURCE),
        ("商机", CHANNEL_DEAL),
        ("情报", CHANNEL_INTEL),
        ("干货", CHANNEL_METHOD),
        ("方法", CHANNEL_METHOD),
        ("其他", CHANNEL_OTHER),
        ("未确认", CHANNEL_OTHER),
        ("", CHANNEL_OTHER),
        (None, CHANNEL_OTHER),
        ("乱七八糟", CHANNEL_OTHER),
    ],
)
def test_normalize_channel(raw, expected):
    assert normalize_channel(raw) == expected


# ---------- L1 ----------


def test_l1_critical_from_non_target_user_is_kept():
    """critical（apikey）即便来自非目标用户也要保留（防漏密钥）。"""
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=5)
    msgs = [
        _msg("111", "gA", "check this sk-abcdefghijklmnopqrstuvwxyz"),
        _msg("222", "gA", "https://example.com/abc1234567890"),
    ]
    items = extractor.extract(
        group_id="gA",
        messages=msgs,
        watched_user_ids={"999"},  # 111/222 都不是目标
        extra_keywords=[],
    )
    assert len(items) == 1
    assert items[0].channel == CHANNEL_APIKEY
    assert items[0].priority == PRIORITY_CRITICAL


def test_l1_normal_from_non_target_user_dropped():
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=5)
    msgs = [_msg("111", "gA", "https://example.com/abc1234567890")]
    items = extractor.extract(
        group_id="gA",
        messages=msgs,
        watched_user_ids={"999"},
    )
    assert items == []  # 非目标 + 非 critical → 丢


def test_l1_watched_user_normal_kept():
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=5)
    msgs = [_msg("999", "gA", "https://example.com/abc1234567890")]
    items = extractor.extract(
        group_id="gA",
        messages=msgs,
        watched_user_ids={"999"},
    )
    assert len(items) == 1
    assert items[0].channel == CHANNEL_RESOURCE


def test_l1_keyword_hit_becomes_other_normal():
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=5)
    items = extractor.extract(
        group_id="gA",
        messages=[_msg("999", "gA", "求破解版的资源")],
        watched_user_ids={"999"},
        extra_keywords=["破解"],
    )
    assert len(items) == 1
    assert items[0].channel == CHANNEL_OTHER
    assert items[0].priority == PRIORITY_NORMAL


def test_l1_max_candidates_per_group():
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=2)
    msgs = [
        _msg("999", "gA", "sk-aaaaaaaaaaaaaaaaaaaaaa"),
        _msg("999", "gA", "sk-bbbbbbbbbbbbbbbbbbbbbb"),
        _msg("999", "gA", "sk-ccccccccccccccccccccc"),
        _msg("999", "gA", "sk-ddddddddddddddddddddd"),
    ]
    items = extractor.extract("gA", msgs, watched_user_ids={"999"})
    assert len(items) == 2  # critical 优先，截断到 2


def test_l1_intra_group_dedup():
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=10)
    same = "sk-aaaaaaaaaaaaaaaaaaaaaa"
    msgs = [
        _msg("999", "gA", same),
        _msg("999", "gA", same),  # 重复
    ]
    items = extractor.extract("gA", msgs, watched_user_ids={"999"})
    assert len(items) == 1


# ---------- L2 ----------


def test_l2_cross_group_dedup_merges_sources():
    same = "sk-aaaaaaaaaaaaaaaaaaaaaa"
    fp = content_fingerprint(same)
    cands = [
        IntelItem(
            content=same,
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="111",
            source_group_id="gA",
            fingerprint=fp,
        ),
        IntelItem(
            content=same,
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="222",
            source_group_id="gB",
            fingerprint=fp,
        ),
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands, group_count=2)
    assert len(result.items) == 1
    assert result.dropped_duplicates == 1
    # 合并了来源群
    assert "gA" in result.items[0].meta.get("groups", [])
    assert "gB" in result.items[0].meta.get("groups", [])


def test_l2_truncates_per_channel():
    cands = [
        IntelItem(
            content=f"https://example.com/{i}/resource-path-1234567890",
            channel=CHANNEL_RESOURCE,
            priority=PRIORITY_NORMAL,
            source_user_id="1",
            source_group_id=f"g{i}",
        )
        for i in range(20)
    ]
    agg = CrossGroupAggregator(max_items_per_channel=3)
    result = agg.aggregate(cands)
    assert len([i for i in result.items if i.channel == CHANNEL_RESOURCE]) == 3


def test_l2_other_channel_filtered_when_not_enabled():
    cands = [
        IntelItem(
            content="求破解",
            channel=CHANNEL_OTHER,
            priority=PRIORITY_NORMAL,
            source_user_id="1",
            source_group_id="gA",
        )
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5, enabled_channels=["apikey", "resource"])
    result = agg.aggregate(cands)
    assert result.items == []


def test_l2_critical_always_kept_even_if_channel_disabled():
    """critical 强制进 apikey，即使原 channel 被 enabled 过滤掉。"""
    cands = [
        IntelItem(
            content="sk-aaaaaaaaaaaaaaaaaaaaaa",
            channel=CHANNEL_OTHER,
            priority=PRIORITY_CRITICAL,
            source_user_id="1",
            source_group_id="gA",
        )
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5, enabled_channels=["apikey"])
    result = agg.aggregate(cands)
    assert len(result.items) == 1
    assert result.items[0].channel == CHANNEL_APIKEY


def test_l2_sorts_critical_first():
    cands = [
        IntelItem(
            content="https://example.com/normal-1234567890",
            channel=CHANNEL_RESOURCE,
            priority=PRIORITY_NORMAL,
            source_user_id="1",
            source_group_id="gA",
        ),
        IntelItem(
            content="sk-aaaaaaaaaaaaaaaaaaaaaa",
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="2",
            source_group_id="gB",
        ),
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands)
    assert result.items[0].priority == PRIORITY_CRITICAL


# ---------- ChannelPacker ----------


def test_packer_split_one_message_per_channel():
    cands = [
        IntelItem(
            content="sk-aaaaaaaaaaaaaaaaaaaaaa",
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="111",
            source_user_name="张三",
            source_group_id="gA",
        ),
        IntelItem(
            content="https://example.com/1234567890",
            channel=CHANNEL_RESOURCE,
            priority=PRIORITY_NORMAL,
            source_user_id="222",
            source_group_id="gB",
        ),
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands, group_count=2)
    packer = ChannelPacker(push_mode="split", interval_minutes=10)
    messages = packer.pack(result)
    assert len(messages) == 2
    assert any("密钥" in m for m in messages)
    assert any("资源" in m for m in messages)


def test_packer_merged_single_message():
    cands = [
        IntelItem(
            content="sk-aaaaaaaaaaaaaaaaaaaaaa",
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="111",
            source_group_id="gA",
        ),
        IntelItem(
            content="https://example.com/1234567890",
            channel=CHANNEL_RESOURCE,
            priority=PRIORITY_NORMAL,
            source_user_id="222",
            source_group_id="gB",
        ),
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands, group_count=2)
    packer = ChannelPacker(push_mode="merged", interval_minutes=10)
    messages = packer.pack(result)
    assert len(messages) == 1
    assert "密钥" in messages[0]
    assert "资源" in messages[0]


def test_packer_empty():
    packer = ChannelPacker(push_mode="split")
    assert packer.pack(CrossGroupAggregator().aggregate([])) == []


# ---------- 端到端：50 群规模 ----------


def test_50_groups_does_not_lose_critical():
    """50 群规模 mock：critical 不丢、candidates 数受控。"""
    extractor = GroupCandidateExtractor(patterns=PATTERNS, max_candidates_per_group=5)
    batches = {}
    for gi in range(50):
        gid = f"g{gi}"
        msgs = []
        # 每群 10 条灌水 + 1 条 critical
        for i in range(10):
            msgs.append(_msg("999", gid, f"灌水消息 {i}"))
        msgs.append(_msg("999", gid, "sk-xxxxxxxxxxxxxxxxxxxx"))
        batches[gid] = msgs

    cands = extract_all_group_candidates(
        batches=batches,
        extractor=extractor,
        watched_user_ids={"999"},
    )
    # 每群提炼 1 条 critical（灌水不命中）
    assert len(cands) == 50
    assert all(c.priority == PRIORITY_CRITICAL for c in cands)

    # L2 去重：指纹相同 → 合并成 1
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands, group_count=50)
    assert len(result.items) == 1
    assert result.dropped_duplicates == 49
    # 合并了 50 个来源群
    assert len(result.items[0].meta.get("groups", [])) == 50


# ---------- Phase 2: L2 分片 ----------


def test_l2_should_shard_when_over_threshold():
    agg = CrossGroupAggregator(
        max_items_per_channel=100,
        enabled_channels=["apikey", "resource"],
        shard_threshold=10,
    )
    cands = []
    for i in range(20):
        cands.append(
            IntelItem(
                content=f"sk-unique-{i:03d}abcdefghijklmnopqrst",
                channel=CHANNEL_APIKEY,
                priority=PRIORITY_CRITICAL,
                source_user_id="1",
                source_group_id=f"g{i}",
            )
        )
    assert agg.should_shard(cands) is True


def test_l2_shard_channels_apikey_alone():
    agg = CrossGroupAggregator(
        enabled_channels=["apikey", "resource", "deal", "intel"],
    )
    shards = agg.shard_channels()
    # apikey 单独一片
    assert shards[0] == ["apikey"]
    # 其余按 2 个一组
    flat = [c for s in shards[1:] for c in s]
    assert "resource" in flat and "deal" in flat and "intel" in flat
    for s in shards[1:]:
        assert len(s) <= 2


def test_l2_shard_channels_without_apikey():
    agg = CrossGroupAggregator(enabled_channels=["resource", "deal", "intel"])
    shards = agg.shard_channels()
    flat = [c for s in shards for c in s]
    assert "apikey" not in flat
    assert all(len(s) <= 2 for s in shards)


# ---------- Phase 2: 简报分页 ----------


def test_packer_pagination_splits_long_message():
    """单频道条目很多 → 单条超 max_chars → 分页。"""
    cands = []
    for i in range(30):
        cands.append(
            IntelItem(
                content=f"https://example.com/long-resource-path-{i}-padding-1234567890",
                channel=CHANNEL_RESOURCE,
                priority=PRIORITY_NORMAL,
                source_user_id="1",
                source_user_name=f"用户{i}",
                source_group_id=f"g{i % 5}",
            )
        )
    agg = CrossGroupAggregator(max_items_per_channel=30)
    result = agg.aggregate(cands, group_count=5)
    packer = ChannelPacker(push_mode="split", interval_minutes=10, max_chars=500)
    messages = packer.pack(result)
    # 应该分页（每条 ≤ ~500 + 页脚）
    assert len(messages) > 1
    # 每页带页码标记
    assert all("页" in m for m in messages)


def test_packer_pagination_off_when_short():
    cands = [
        IntelItem(
            content="sk-abcdefghijklmnopqrst",
            channel=CHANNEL_APIKEY,
            priority=PRIORITY_CRITICAL,
            source_user_id="1",
            source_group_id="gA",
        )
    ]
    agg = CrossGroupAggregator(max_items_per_channel=5)
    result = agg.aggregate(cands, group_count=1)
    packer = ChannelPacker(push_mode="split", max_chars=1800)
    messages = packer.pack(result)
    assert len(messages) == 1
    assert "页" not in messages[0]


# ---------- Phase 2: L1 LLM 提炼回调 ----------


@pytest.mark.asyncio
async def test_l1_llm_refine_callback_invoked():
    """配置 callback 后 extract_async 走 LLM 路径。"""
    calls = []

    async def refine(group_id, items):
        calls.append((group_id, len(items)))
        # 模拟 LLM 只保留第一条 + 改 reason
        if items:
            items[0].reason = "LLM 校准"
            return [items[0]]
        return items

    extractor = GroupCandidateExtractor(
        patterns=PATTERNS,
        max_candidates_per_group=5,
        llm_refine_callback=refine,
    )
    msgs = [
        _msg("999", "gA", "sk-aaaaaaaaaaaaaaaaaaaaaa"),
        _msg("999", "gA", "https://example.com/1234567890"),
    ]
    items = await extractor.extract_async("gA", msgs, watched_user_ids={"999"})
    assert calls == [("gA", 2)]
    assert len(items) == 1
    assert items[0].reason == "LLM 校准"


@pytest.mark.asyncio
async def test_l1_llm_refine_failure_falls_back():
    """LLM 提炼抛异常 → 回退规则结果。"""

    async def refine(group_id, items):
        raise RuntimeError("LLM down")

    extractor = GroupCandidateExtractor(
        patterns=PATTERNS,
        max_candidates_per_group=5,
        llm_refine_callback=refine,
    )
    msgs = [_msg("999", "gA", "sk-aaaaaaaaaaaaaaaaaaaaaa")]
    items = await extractor.extract_async("gA", msgs, watched_user_ids={"999"})
    # 回退规则结果：1 条 critical
    assert len(items) == 1
    assert items[0].priority == PRIORITY_CRITICAL


@pytest.mark.asyncio
async def test_extract_async_parallelism_with_llm():
    """多群并行 L1 LLM 提炼，并发受 sem 控制。"""
    started = 0
    max_concurrent = 0

    async def refine(group_id, items):
        nonlocal started, max_concurrent
        started += 1
        max_concurrent = max(max_concurrent, started)
        await asyncio.sleep(0.01)
        started -= 1
        return items

    extractor = GroupCandidateExtractor(
        patterns=PATTERNS,
        max_candidates_per_group=5,
        llm_refine_callback=refine,
    )
    batches = {}
    for gi in range(10):
        batches[f"g{gi}"] = [_msg("999", f"g{gi}", "sk-aaaaaaaaaaaaaaaaaaaaaa")]

    from src.application.services.layered_aggregation import (
        extract_all_group_candidates_async,
    )

    items = await extract_all_group_candidates_async(
        batches=batches,
        extractor=extractor,
        watched_user_ids={"999"},
        parallelism=3,
    )
    assert len(items) == 10
    # 并发上限不超过 parallelism（可能等于，也可能因为调度略低）
    assert max_concurrent <= 3


# ---------- Phase 2: critical 旁路 ----------


@pytest.mark.asyncio
async def test_window_critical_instant_push():
    """window 模式下命中 critical → 立即推一条，不等 flush。"""
    from src.application.services.message_monitor_service import (
        MessageMonitorService,
    )
    from src.infrastructure.config.config_manager import ConfigManager
    from tests.conftest import AstrBotConfig

    cfg = ConfigManager(
        AstrBotConfig(
            {
                "message_monitor": {
                    "enable_monitor": True,
                    "monitor_mode": "window",
                    "window_scope": "per_group",
                    "monitored_groups": ["gA"],
                    "monitored_qqs": ["999"],
                    "alert_admin_qqs": ["888"],
                    "critical_instant_push": True,
                    "cooldown_seconds": 0,
                    "dedup_minutes": 0,
                    "flush_interval": 999,  # 测试期间不要触发 flush
                }
            }
        )
    )

    class FakeAdapter:
        def __init__(self):
            self.sent = []

        async def send_private(self, user_id, text="", image_path=""):
            self.sent.append({"user_id": user_id, "text": text})
            return True

    class FakeBotMgr:
        def __init__(self, adapter):
            self._a = adapter

        def get_adapter(self, pid):
            return self._a

        def get_all_adapters(self):
            return [self._a]

    adapter = FakeAdapter()
    bot_mgr = FakeBotMgr(adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    evt = type(
        "E",
        (),
        {
            "get_sender_id": lambda self: "999",
            "get_group_id": lambda self: "gA",
            "get_platform_id": lambda self: "aiocqhttp:default",
            "get_sender_name": lambda self: "目标",
            "message_str": "sk-aaaaaaaaaaaaaaaaaaaaaa",
            "message_obj": MagicMock(message=[], sender=MagicMock(nickname="目标")),
        },
    )()

    await service.process(evt)
    assert len(adapter.sent) == 1
    assert "密钥" in adapter.sent[0]["text"] or "apikey" in adapter.sent[0]["text"].lower()
    service.stop()
