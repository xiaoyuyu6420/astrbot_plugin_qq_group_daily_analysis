"""
e2e 测试：关键词即时推送模式（monitor_mode=keyword）

验证：任何人发命中关键词的消息 → 立即推送（不攒窗口、不盯特定人）
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.conftest  # noqa: F401

from src.application.services.message_monitor_service import MessageMonitorService
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


def make_keyword_config(**overrides) -> ConfigManager:
    """构建关键词模式的 ConfigManager"""
    base = {
        "message_monitor": {
            "enable_monitor": True,
            "monitor_mode": "keyword",
            "monitored_qqs": [],  # 关键词模式不限人
            "monitored_groups": ["groupA"],
            "extra_keywords": ["破解", "激活码"],
            "use_llm_confirm": False,  # 默认不走 LLM，纯规则即时推
            "flush_interval": 10,
            "max_context_messages": 50,
            "alert_admin_qqs": ["888"],
            # 降噪参数：测试默认禁用批量合并和冷却，保证即时推送
            "cooldown_seconds": 0,
            "dedup_minutes": 0,
            "keyword_batch_seconds": 0,
        }
    }
    for key, val in overrides.items():
        base["message_monitor"][key] = val
    return ConfigManager(AstrBotConfig(base))


class FakeEvent:
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
    def __init__(self):
        self.sent_messages = []

    async def send_private(self, user_id, text="", image_path=""):
        self.sent_messages.append({"user_id": user_id, "text": text})
        return True


class FakeBotManager:
    def __init__(self, adapter=None):
        self._adapter = adapter or FakeAdapter()
        self._context = None

    def get_adapter(self, platform_id):
        return self._adapter


@pytest.mark.asyncio
async def test_keyword_apikey_hit_push_immediately():
    """任何人发 API key → 立即推送"""
    cfg = make_keyword_config()
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    # 任何人（非监控 QQ）发了 API key
    await svc.process(FakeEvent("12345", "groupA", "搞到一个key: sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))

    assert len(adapter.sent_messages) == 1
    msg = adapter.sent_messages[0]
    assert msg["user_id"] == "888"
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in msg["text"]
    assert "关键词命中预警" in msg["text"]
    print("✓ 关键词模式：API key 命中 → 立即推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_custom_keyword_hit():
    """自定义关键词命中 → 立即推送"""
    cfg = make_keyword_config(extra_keywords=["破解", "激活码"])
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    await svc.process(FakeEvent("12345", "groupA", "我有个破解版软件分享", "路人"))

    assert len(adapter.sent_messages) == 1
    assert "破解" in adapter.sent_messages[0]["text"]
    print("✓ 关键词模式：自定义关键词「破解」命中 → 推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_normal_chat_no_push():
    """普通闲聊不命中 → 不推送"""
    cfg = make_keyword_config()
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    await svc.process(FakeEvent("12345", "groupA", "今天天气真好啊哈哈哈", "路人"))
    assert len(adapter.sent_messages) == 0
    print("✓ 关键词模式：普通闲聊 → 不推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_non_monitored_group_ignored():
    """非监控群 → 不处理"""
    cfg = make_keyword_config(monitored_groups=["groupA"])
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    await svc.process(FakeEvent("12345", "groupB", "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))
    assert len(adapter.sent_messages) == 0
    print("✓ 关键词模式：非监控群 → 忽略")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_llm_confirms_useful():
    """关键词命中 + LLM 确认有用 → 推送"""
    cfg = make_keyword_config(use_llm_confirm=True)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "useful": True, "reason": "含可用 OpenAI key", "category": "apikey"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock, return_value=llm_resp
    ):
        await svc.process(FakeEvent("12345", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))

    assert len(adapter.sent_messages) == 1
    assert "apikey" in adapter.sent_messages[0]["text"]
    print("✓ 关键词模式：命中 + LLM 确认有用 → 推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_llm_says_not_useful():
    """关键词命中 + LLM 判定无用 → 不推送"""
    cfg = make_keyword_config(use_llm_confirm=True)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "useful": False, "reason": "只是讨论 API key 的概念，没有实际 key", "category": "其他"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock, return_value=llm_resp
    ):
        await svc.process(FakeEvent("12345", "groupA", "api key 怎么申请啊", "路人"))

    assert len(adapter.sent_messages) == 0
    print("✓ 关键词模式：命中但 LLM 判定无用 → 不推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_llm_unavailable_fallback():
    """关键词命中 + LLM 不可用 → 降级推送"""
    cfg = make_keyword_config(use_llm_confirm=True)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock, return_value=None
    ):
        await svc.process(FakeEvent("12345", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))

    assert len(adapter.sent_messages) == 1
    assert "降级" in adapter.sent_messages[0]["text"]
    print("✓ 关键词模式：LLM 不可用 → 降级推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_filter_by_qq():
    """关键词模式 + monitored_qqs 非空 → 只检测这些人的消息（与 schema 描述一致）"""
    cfg = make_keyword_config(monitored_qqs=["999"])
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    # 非目标 QQ 发的 API key → 被 monitored_qqs 过滤掉，不推送
    await svc.process(FakeEvent("12345", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))
    assert len(adapter.sent_messages) == 0

    # 目标 QQ 发同样内容 → 推送
    await svc.process(FakeEvent("999", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "目标"))
    assert len(adapter.sent_messages) == 1
    print("✓ 关键词模式：monitored_qqs 非空时只检测目标 QQ 的消息")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_no_buffer_used():
    """关键词模式不攒缓冲区（即时推，不等待）"""
    cfg = make_keyword_config()
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    await svc.process(FakeEvent("12345", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"))

    # 缓冲区应该是空的（关键词模式不攒消息）
    assert len(svc._buffer) == 0
    # 但推送了
    assert len(adapter.sent_messages) == 1
    print("✓ 关键词模式：不使用缓冲区，即时推送")
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_link_and_code_hit():
    """链接 + 提取码同时命中 → 推送"""
    cfg = make_keyword_config()
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    await svc.process(FakeEvent("12345", "groupA",
        "资源分享 https://pan.baidu.com/s/abcde 提取码: x9y2", "分享者"))

    assert len(adapter.sent_messages) == 1
    msg = adapter.sent_messages[0]["text"]
    assert "https://pan.baidu.com" in msg
    assert "提取码" in msg
    print("✓ 关键词模式：链接+提取码命中 → 推送")
    svc.stop()
