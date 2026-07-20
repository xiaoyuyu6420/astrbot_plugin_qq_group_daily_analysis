"""
e2e 测试：跨群聚合模式 + 降噪层集成

测试链路：
- 跨群聚合：多群 buffer → flush → _flush_cross_group → LLM 聚类 → 统一简报推送
- 降噪集成：keyword 模式下的优先级分级、冷却、去重、批量合并
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.conftest  # noqa: F401

from src.application.services.message_monitor_service import MessageMonitorService
from src.application.services.noise_reducer import NoiseReducer
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


# ============================================================
# 测试辅助
# ============================================================


def make_config(**overrides) -> ConfigManager:
    """构建 ConfigManager，默认 window 模式 + 跨群聚合（legacy LLM 路径）。

    本文件测的是 legacy 单次 LLM 聚类路径，强制 aggregation_mode=legacy。
    layered 分层聚合见 tests/test_layered_aggregation.py。
    """
    base = {
        "message_monitor": {
            "enable_monitor": True,
            "monitor_mode": "window",
            "monitored_qqs": ["999"],
            "monitored_groups": ["groupA", "groupB", "groupC"],
            "extra_keywords": [],
            "use_llm_confirm": True,
            "flush_interval": 5,
            "max_context_messages": 50,
            "alert_admin_qqs": ["888"],
            "enable_cross_group": True,
            "aggregation_mode": "legacy",  # 本文件锁定 legacy；layered 走 test_layered_aggregation
            "cooldown_seconds": 60,
            "dedup_minutes": 30,
            "keyword_batch_seconds": 0,  # 测试时禁用批量合并
        }
    }
    for key, val in overrides.items():
        base["message_monitor"][key] = val
    cfg = AstrBotConfig(base)
    return ConfigManager(cfg)


class FakeEvent:
    """模拟 AstrMessageEvent"""

    def __init__(self, sender_id, group_id, text, sender_name=""):
        self._sender_id = sender_id
        self._group_id = group_id
        self._text = text
        self._sender_name = sender_name or f"用户{sender_id}"
        self.message_str = text
        self.message_obj = MagicMock()
        self.message_obj.message = []
        self.message_obj.sender = MagicMock()
        self.message_obj.sender.nickname = self._sender_name

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_platform_id(self):
        return "aiocqhttp:default"

    def get_sender_name(self):
        return self._sender_name

    def get_platform_name(self):
        return "aiocqhttp"


class FakeAdapter:
    """模拟 OneBot adapter"""

    def __init__(self):
        self.sent_messages: list[dict] = []

    async def send_private(self, user_id, text="", image_path=""):
        self.sent_messages.append({"user_id": user_id, "text": text})
        return True


class FakeBotManager:
    """模拟 BotManager"""

    def __init__(self, adapter=None):
        self._adapter = adapter or FakeAdapter()
        self._context = None

    def get_adapter(self, platform_id):
        return self._adapter


# ============================================================
# 跨群聚合测试
# ============================================================


@pytest.mark.asyncio
async def test_cross_group_aggregation_basic():
    """跨群聚合：两个群有消息 → LLM 聚类 → 一份简报推送"""
    cfg = make_config(enable_cross_group=True)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 群 A：目标 QQ 999 分享了 API key
    events_a = [
        FakeEvent("111", "groupA", "有人有 GPT key 吗", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
    ]
    # 群 B：有人在讨论同一话题
    events_b = [
        FakeEvent("222", "groupB", "听说有个 key 流出了", "李四"),
        FakeEvent("999", "groupB", "对，就是我发的那个", "目标"),
    ]

    for evt in events_a + events_b:
        await service.process(evt)

    # 模拟 LLM 返回跨群聚类结果
    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "topics": [
            {
                "topic": "OpenAI Key 流出",
                "groups": ["groupA", "groupB"],
                "items": [
                    {
                        "content": "sk-test1234567890abcdefghij",
                        "source_qq": "999",
                        "category": "apikey",
                        "reason": "疑似 OpenAI API Key"
                    }
                ],
                "summary": "目标用户在两个群分享了同一个 API key"
            }
        ],
        "overall_summary": "两个群讨论同一 API key 泄露事件"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        await service._flush_all()

    # 应该只推送一条（跨群简报）
    assert len(fake_adapter.sent_messages) == 1
    msg = fake_adapter.sent_messages[0]
    assert msg["user_id"] == "888"
    assert "跨群情报简报" in msg["text"]
    assert "OpenAI Key 流出" in msg["text"]
    assert "groupA" in msg["text"] or "groupB" in msg["text"]

    service.stop()


@pytest.mark.asyncio
async def test_cross_group_no_value_no_push():
    """跨群聚合：LLM 判定无价值 → 不推送"""
    cfg = make_config(enable_cross_group=True)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "今天天气真好", "张三"),
        FakeEvent("999", "groupA", "是啊", "目标"),
        FakeEvent("222", "groupB", "吃饭了吗", "李四"),
    ]
    for evt in events:
        await service.process(evt)

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": False,
        "topics": [],
        "overall_summary": ""
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        await service._flush_all()

    assert len(fake_adapter.sent_messages) == 0
    service.stop()


@pytest.mark.asyncio
async def test_cross_group_no_target_qq_skip():
    """跨群聚合：窗口中无目标 QQ → 跳过"""
    cfg = make_config(enable_cross_group=True, monitored_qqs=["999"])
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 只有非目标 QQ 发言
    events = [
        FakeEvent("111", "groupA", "闲聊", "张三"),
        FakeEvent("222", "groupB", "闲聊2", "李四"),
    ]
    for evt in events:
        await service.process(evt)

    # 不需要 mock LLM（不会调用）
    await service._flush_all()
    assert len(fake_adapter.sent_messages) == 0
    service.stop()


@pytest.mark.asyncio
async def test_cross_group_llm_unavailable_fallback():
    """跨群聚合：LLM 不可用 → 降级为逐群处理"""
    cfg = make_config(enable_cross_group=True, use_llm_confirm=False)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events_a = [
        FakeEvent("111", "groupA", "有人有 key 吗", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
    ]
    events_b = [
        FakeEvent("222", "groupB", "看看这个", "李四"),
        FakeEvent("999", "groupB", "分享一下资源", "目标"),
    ]

    for evt in events_a + events_b:
        await service.process(evt)

    # LLM 返回 None（不可用）→ 降级为逐群处理
    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await service._flush_all()

    # 降级为逐群处理，use_llm_confirm=False → 直接推目标用户原始发言
    # groupA 和 groupB 各一条
    assert len(fake_adapter.sent_messages) >= 1
    service.stop()


@pytest.mark.asyncio
async def test_cross_group_disabled_per_group():
    """关闭跨群聚合 → 逐群独立处理"""
    cfg = make_config(enable_cross_group=False)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events_a = [
        FakeEvent("111", "groupA", "聊天", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij_key", "目标"),
    ]
    events_b = [
        FakeEvent("222", "groupB", "聊天", "李四"),
        FakeEvent("999", "groupB", "分享资源 https://example.com/file", "目标"),
    ]

    for evt in events_a + events_b:
        await service.process(evt)

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "items": [{"content": "有价值信息", "category": "apikey", "reason": "test"}],
        "summary": "测试"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        await service._flush_all()

    # 逐群处理：groupA 和 groupB 各推一条
    assert len(fake_adapter.sent_messages) == 2
    service.stop()


# ============================================================
# 降噪集成测试（keyword 模式）
# ============================================================


@pytest.mark.asyncio
async def test_keyword_critical_bypass_cooldown():
    """keyword 模式：critical 优先级绕过冷却"""
    cfg = make_config(
        monitor_mode="keyword",
        monitored_qqs=[],  # keyword 模式不限发送者（聚焦冷却行为）
        cooldown_seconds=300,  # 5 分钟冷却
        use_llm_confirm=False,  # 不用 LLM，直接推
        keyword_batch_seconds=0,  # 不批量合并
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 命中 API Key 正则 → critical 优先级
    evt1 = FakeEvent("111", "groupA", "sk-abcdefghijklmnopqrstuvwxyz123456", "张三")
    await service.process(evt1)
    assert len(fake_adapter.sent_messages) == 1

    # 第二条也命中 API Key → critical 绕过冷却，仍然推送
    evt2 = FakeEvent("111", "groupA", "sk-zyxwvutsrqponmlkjihgfedcba987654", "张三")
    await service.process(evt2)
    assert len(fake_adapter.sent_messages) == 2

    service.stop()


@pytest.mark.asyncio
async def test_keyword_normal_respects_cooldown():
    """keyword 模式：normal 优先级受冷却限制"""
    cfg = make_config(
        monitor_mode="keyword",
        monitored_qqs=[],  # keyword 模式不限发送者（聚焦冷却行为）
        cooldown_seconds=300,  # 5 分钟冷却
        use_llm_confirm=False,
        keyword_batch_seconds=0,  # 不批量合并
        dedup_minutes=0,  # 不去重
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 命中资源链接 → normal 优先级
    evt1 = FakeEvent("111", "groupA", "check this https://example.com/very-long-url-path-here", "张三")
    await service.process(evt1)
    assert len(fake_adapter.sent_messages) == 1

    # 同一发送者@同一群 → 冷却中，被跳过
    evt2 = FakeEvent("111", "groupA", "check this https://another-long-url-path-here.com/resource", "张三")
    await service.process(evt2)
    # 冷却生效，第二条不推
    assert len(fake_adapter.sent_messages) == 1

    service.stop()


@pytest.mark.asyncio
async def test_keyword_dedup():
    """keyword 模式：内容去重"""
    cfg = make_config(
        monitor_mode="keyword",
        monitored_qqs=[],  # keyword 模式不限发送者（聚焦去重行为）
        cooldown_seconds=0,  # 不冷却
        dedup_minutes=30,
        use_llm_confirm=False,
        keyword_batch_seconds=0,
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 第一次推
    evt1 = FakeEvent("111", "groupA", "sk-abcdefghijklmnopqrstuvwxyz123456", "张三")
    await service.process(evt1)
    assert len(fake_adapter.sent_messages) == 1

    # 相同内容不同群 → 去重，不推
    evt2 = FakeEvent("222", "groupB", "sk-abcdefghijklmnopqrstuvwxyz123456", "李四")
    await service.process(evt2)
    assert len(fake_adapter.sent_messages) == 1  # 去重生效

    service.stop()


@pytest.mark.asyncio
async def test_keyword_low_priority_discarded():
    """keyword 模式：low 优先级被丢弃"""
    cfg = make_config(
        monitor_mode="keyword",
        use_llm_confirm=True,
        cooldown_seconds=0,
        dedup_minutes=0,
        keyword_batch_seconds=0,
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # LLM 判定无用 → low 优先级
    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "useful": False,
        "reason": "只是提到链接但没有实际分享",
        "category": "其他"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        # 命中资源链接正则，但 LLM 判定无用 → low → 丢弃
        evt = FakeEvent("111", "groupA", "check this https://example.com/very-long-url-path-here", "张三")
        await service.process(evt)

    assert len(fake_adapter.sent_messages) == 0
    service.stop()


@pytest.mark.asyncio
async def test_keyword_normal_batch():
    """keyword 模式：normal 优先级进入批量合并队列"""
    cfg = make_config(
        monitor_mode="keyword",
        monitored_qqs=[],  # keyword 模式不限发送者（聚焦批量合并行为）
        use_llm_confirm=False,
        cooldown_seconds=0,
        dedup_minutes=0,
        keyword_batch_seconds=60,  # 60 秒批量合并
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 命中资源链接 → normal → 入队
    evt1 = FakeEvent("111", "groupA", "check this https://example.com/very-long-url-path-here", "张三")
    await service.process(evt1)

    # 不应该立即推送（入了批量队列）
    assert len(fake_adapter.sent_messages) == 0
    # 队列中有一条
    assert service._noise_reducer.get_pending_count() == 1

    # 手动触发批量推送
    await service._noise_reducer._flush_pending_normals()

    # 现在推送了
    assert len(fake_adapter.sent_messages) == 1
    assert "批量预警" in fake_adapter.sent_messages[0]["text"]

    service.stop()


# ============================================================
# 跨群简报格式测试
# ============================================================


@pytest.mark.asyncio
async def test_cross_group_brief_format():
    """跨群简报格式包含关键信息"""
    cfg = make_config(enable_cross_group=True, flush_interval=5)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events_a = [
        FakeEvent("111", "groupA", "有人有 GPT key 吗", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
    ]
    events_b = [
        FakeEvent("222", "groupB", "有什么好资源", "李四"),
        FakeEvent("999", "groupB", "百度网盘 提取码：abc123", "目标"),
    ]

    for evt in events_a + events_b:
        await service.process(evt)

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "topics": [
            {
                "topic": "API Key 分享",
                "groups": ["groupA"],
                "items": [
                    {
                        "content": "sk-test1234567890abcdefghij",
                        "source_qq": "999",
                        "category": "apikey",
                        "reason": "疑似 OpenAI Key"
                    }
                ],
                "summary": "目标用户分享了 API key"
            },
            {
                "topic": "网盘资源分享",
                "groups": ["groupB"],
                "items": [
                    {
                        "content": "百度网盘 提取码：abc123",
                        "source_qq": "999",
                        "category": "资源",
                        "reason": "包含提取码"
                    }
                ],
                "summary": "目标用户分享了网盘资源"
            }
        ],
        "overall_summary": "两个群分别分享了 API key 和网盘资源"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        await service._flush_all()

    assert len(fake_adapter.sent_messages) == 1
    text = fake_adapter.sent_messages[0]["text"]
    assert "跨群情报简报" in text
    assert "近 5 分钟" in text
    assert "API Key 分享" in text
    assert "网盘资源分享" in text
    assert "跨群概述" in text
    # API key 话题应该有 🔴 标记
    assert "🔴" in text

    service.stop()


@pytest.mark.asyncio
async def test_cross_group_single_topic():
    """跨群聚合：只有 1 个话题时格式正确"""
    cfg = make_config(enable_cross_group=True)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "聊天", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
    ]
    for evt in events:
        await service.process(evt)

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "topics": [
            {
                "topic": "Key 分享",
                "groups": ["groupA"],
                "items": [
                    {
                        "content": "sk-test1234567890abcdefghij",
                        "source_qq": "999",
                        "category": "apikey",
                        "reason": "API key"
                    }
                ],
                "summary": "分享了 key"
            }
        ],
        "overall_summary": "检测到 API key 分享"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        await service._flush_all()

    assert len(fake_adapter.sent_messages) == 1
    text = fake_adapter.sent_messages[0]["text"]
    assert "话题 1" in text
    assert "Key 分享" in text
    service.stop()


# ============================================================
# _build_cross_group_dialog 测试
# ============================================================


def test_build_cross_group_dialog():
    """跨群对话文本构建：标注来源群，目标 QQ 用 >>> 标记"""
    cfg = make_config(enable_cross_group=True)
    service = MessageMonitorService(MagicMock(), cfg, FakeBotManager())

    messages = [
        {"text": "有人有 key 吗", "sender_id": "111", "name": "张三", "group_id": "groupA", "time": 0},
        {"text": "sk-test123", "sender_id": "999", "name": "目标", "group_id": "groupA", "time": 1},
        {"text": "看看这个", "sender_id": "222", "name": "李四", "group_id": "groupB", "time": 2},
        {"text": "分享资源", "sender_id": "999", "name": "目标", "group_id": "groupB", "time": 3},
    ]

    result = service._build_cross_group_dialog(messages, {"999"})

    # 非目标用户用 4 空格缩进
    assert "    [1] [群groupA] [111]" in result
    # 目标用户用 >>> 标记
    assert ">>> [2] [群groupA] [999]" in result
    assert ">>> [4] [群groupB] [999]" in result
    # 来源群标注
    assert "群groupA" in result
    assert "群groupB" in result

    service.stop()


# ============================================================
# NoiseReducer 与 MessageMonitorService 集成
# ============================================================


@pytest.mark.asyncio
async def test_noise_reducer_integrated_in_service():
    """NoiseReducer 正确集成到 MessageMonitorService"""
    cfg = make_config(monitor_mode="keyword", cooldown_seconds=0, dedup_minutes=0, keyword_batch_seconds=0)
    service = MessageMonitorService(MagicMock(), cfg, FakeBotManager())

    # 验证 noise_reducer 已初始化
    assert service._noise_reducer is not None
    assert isinstance(service._noise_reducer, NoiseReducer)
    # 验证 send_callback 已注入
    assert service._noise_reducer._send_callback is not None

    service.stop()


# ============================================================
# Layered 分层聚合 e2e（不走 LLM）
# ============================================================


@pytest.mark.asyncio
async def test_layered_aggregation_pushes_without_llm():
    """aggregation_mode=layered 时不调 LLM，靠规则即推 critical。"""
    cfg = make_config(aggregation_mode="layered", monitored_qqs=["999"])
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "有人有 GPT key 吗", "张三"),
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
        FakeEvent("999", "groupB", "sk-test1234567890abcdefghij", "目标"),  # 同一 key 跨群
    ]
    for evt in events:
        await service.process(evt)

    # 不 mock LLM：layered 不应调用 call_provider_with_retry
    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        side_effect=AssertionError("layered 不应调 LLM"),
    ):
        await service._flush_all()

    # split 模式：apikey 频道至少一条推送
    assert len(fake_adapter.sent_messages) >= 1
    text = fake_adapter.sent_messages[0]["text"]
    assert "密钥" in text or "apikey" in text.lower()
    assert "888" == fake_adapter.sent_messages[0]["user_id"]

    service.stop()


@pytest.mark.asyncio
async def test_layered_merged_mode_single_message():
    """channel_push_mode=merged 合并成一条总简报。"""
    cfg = make_config(
        aggregation_mode="layered",
        channel_push_mode="merged",
        monitored_qqs=["999"],
    )
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)
    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("999", "groupA", "sk-test1234567890abcdefghij", "目标"),
        FakeEvent("999", "groupB", "https://example.com/abc1234567890", "目标"),
    ]
    for evt in events:
        await service.process(evt)

    await service._flush_all()

    assert len(fake_adapter.sent_messages) == 1
    text = fake_adapter.sent_messages[0]["text"]
    assert "密钥" in text
    assert "资源" in text

    service.stop()
