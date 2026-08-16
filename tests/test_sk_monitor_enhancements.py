"""测试：sk 密钥监控增强（验真 + 转发解析 + 推送格式）

覆盖：
1. SkVerifier 验真逻辑（_extract_base_url、format_verify_result）
2. 验真开关关闭时不发 HTTP
3. 转发聊天记录解析（_flatten_forward_messages）
4. _extract_text 异步提取 forward 段
5. 推送 alert 含「平台/来源/验真」字段（sk 命中时）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.conftest  # noqa: F401

from src.application.services.message_monitor_service import MessageMonitorService
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.messaging.sk_verifier import (
    _extract_base_url,
    format_verify_result,
)
from tests.conftest import AstrBotConfig


# ============================================================
# SkVerifier 单元测试
# ============================================================

class TestExtractBaseUrl:
    def test_plain_domain(self):
        assert _extract_base_url("api.xxx.com") == "https://api.xxx.com"

    def test_with_path(self):
        assert _extract_base_url("https://xxx.com/v1/chat") == "https://xxx.com"

    def test_already_base(self):
        assert _extract_base_url("https://api.openai.com") == "https://api.openai.com"

    def test_none(self):
        assert _extract_base_url(None) is None
        assert _extract_base_url("") is None

    def test_invalid_scheme(self):
        assert _extract_base_url("ftp://xxx.com") is None

    def test_with_port(self):
        assert _extract_base_url("http://localhost:3000/v1") == "http://localhost"


class TestFormatVerifyResult:
    def test_openai_valid(self):
        r = {"openai": "valid", "custom": "skipped", "custom_base_url": ""}
        s = format_verify_result(r)
        assert "OpenAI官方 → 有效" in s
        assert "中转站 → 未提供" in s

    def test_openai_invalid_custom_valid(self):
        r = {"openai": "invalid", "custom": "valid", "custom_base_url": "https://relay.com"}
        s = format_verify_result(r)
        assert "OpenAI官方 → 无效" in s
        assert "中转站(https://relay.com) → 有效" in s

    def test_empty(self):
        assert "验真失败" in format_verify_result({})

    def test_unknown(self):
        r = {"openai": "unknown", "custom": "unknown", "custom_base_url": "https://x.com"}
        s = format_verify_result(r)
        assert "未知" in s


# ============================================================
# 验真开关测试
# ============================================================

def _make_cfg(**overrides) -> ConfigManager:
    base = {
        "message_monitor": {
            "enable_monitor": True,
            "monitor_mode": "keyword",
            "monitored_qqs": [],
            "monitored_groups": ["groupA"],
            "extra_keywords": [],
            "use_llm_confirm": False,
            "flush_interval": 10,
            "max_context_messages": 50,
            "alert_admin_qqs": ["888"],
            "cooldown_seconds": 0,
            "dedup_minutes": 0,
            "keyword_batch_seconds": 0,
            "verify_sk_real": False,
        }
    }
    for k, v in overrides.items():
        base["message_monitor"][k] = v
    return ConfigManager(AstrBotConfig(base))


class FakeAdapter:
    def __init__(self):
        self.sent_messages = []
        self.forward_calls = []

    async def send_private(self, user_id, text="", image_path=""):
        self.sent_messages.append({"user_id": user_id, "text": text})
        return True

    async def get_forward_msg(self, message_id):
        self.forward_calls.append(message_id)
        return None  # 默认返回 None，子测试可覆盖


class FakeBotManager:
    def __init__(self, adapter=None):
        self._adapter = adapter or FakeAdapter()
        self._context = None

    def get_adapter(self, platform_id):
        return self._adapter


class FakeEvent:
    def __init__(self, sender_id, group_id, text="", sender_name="路人", message_obj=None):
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_str = text
        self.message_obj = message_obj or MagicMock()
        if not message_obj:
            self.message_obj.message = []

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_platform_id(self):
        return "aiocqhttp:default"

    def get_sender_name(self):
        return self._sender_name


@pytest.mark.asyncio
async def test_verify_sk_disabled_no_http():
    """verify_sk_real 关闭时，不发起任何 HTTP 请求。"""
    cfg = _make_cfg(verify_sk_real=False)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    with patch("src.infrastructure.messaging.sk_verifier.aiohttp.ClientSession") as mock_session:
        result = await svc._verify_sk_real("sk-abcd1234efgh5678ijkl9012mnop3456", None)
        assert result is None
        mock_session.assert_not_called()
    svc.stop()


@pytest.mark.asyncio
async def test_verify_sk_enabled_calls_verifier():
    """verify_sk_real 开启时，调用 verify_sk。"""
    cfg = _make_cfg(verify_sk_real=True)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    fake_result = {"openai": "invalid", "custom": "skipped", "custom_base_url": ""}
    with patch(
        "src.infrastructure.messaging.sk_verifier.verify_sk",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_verify:
        result = await svc._verify_sk_real(
            "这里有个密钥 sk-abcd1234efgh5678ijkl9012mnop3456 快用", None
        )
        assert result == fake_result
        mock_verify.assert_called_once()
        # 验证传给 verify_sk 的 sk 是从文本提取的最长串
        called_sk = mock_verify.call_args[0][0]
        assert called_sk == "sk-abcd1234efgh5678ijkl9012mnop3456"
    svc.stop()


# ============================================================
# 转发聊天记录解析测试
# ============================================================

def test_flatten_forward_messages():
    """_flatten_forward_messages 正确提取转发包子消息文本。"""
    cfg = _make_cfg()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager())

    forward_data = {
        "messages": [
            {
                "sender": {"card": "张三", "nickname": "zhang"},
                "content": [
                    {"type": "text", "data": {"text": "给大家分享一个key"}},
                    {"type": "text", "data": {"text": "sk-abcd1234efgh5678ijkl9012mnop3456"}},
                ],
            },
            {
                "sender": {"card": "", "nickname": "李四"},
                "content": [
                    {"type": "text", "data": {"text": "谢谢老板"}},
                ],
            },
        ]
    }
    text = svc._flatten_forward_messages(forward_data)
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in text
    assert "[张三]" in text
    assert "谢谢老板" in text
    svc.stop()


def test_flatten_forward_empty():
    """空转发包返回空字符串。"""
    cfg = _make_cfg()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager())
    assert svc._flatten_forward_messages({}) == ""
    assert svc._flatten_forward_messages({"messages": []}) == ""
    svc.stop()


@pytest.mark.asyncio
async def test_extract_text_with_forward_segment():
    """_extract_text 检测 forward 段并调 get_forward_msg 拉取文本。"""
    cfg = _make_cfg()
    adapter = FakeAdapter()
    fwd_mock = AsyncMock(return_value={
        "messages": [
            {
                "sender": {"card": "大佬"},
                "content": [
                    {"type": "text", "data": {"text": "sk-abcd1234efgh5678ijkl9012mnop3456"}},
                ],
            }
        ]
    })
    adapter.get_forward_msg = fwd_mock
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    # 构造一个含 forward 段的 event
    fwd_seg = MagicMock()
    fwd_seg.type = "forward"
    fwd_seg.data = {"id": "resid123"}
    msg_obj = MagicMock()
    msg_obj.message = [fwd_seg]
    # message_str 为空（转发消息通常没有纯文本）
    event = FakeEvent("111", "groupA", text="", message_obj=msg_obj)
    event.message_str = ""

    text = await svc._extract_text(event)
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in text
    fwd_mock.assert_called_once_with("resid123")
    svc.stop()


@pytest.mark.asyncio
async def test_extract_text_forward_failure_degrades():
    """get_forward_msg 失败时降级为空文本（不崩）。"""
    cfg = _make_cfg()
    adapter = FakeAdapter()
    adapter.get_forward_msg = AsyncMock(return_value=None)
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    fwd_seg = MagicMock()
    fwd_seg.type = "forward"
    fwd_seg.data = {"id": "badid"}
    msg_obj = MagicMock()
    msg_obj.message = [fwd_seg]
    event = FakeEvent("111", "groupA", text="", message_obj=msg_obj)
    event.message_str = ""

    text = await svc._extract_text(event)
    assert text == ""  # 降级为空，不崩
    svc.stop()


# ============================================================
# 推送格式测试（含验真字段）
# ============================================================

@pytest.mark.asyncio
async def test_keyword_sk_hit_alert_contains_verify_field():
    """sk 命中推送的 alert 含「平台/验真」字段。"""
    cfg = _make_cfg(use_llm_confirm=False)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))

    # mock LLM 分析返回结果
    svc._analyze_sk_hit = AsyncMock(return_value={
        "platform": "OpenAI官方",
        "source_url": "https://api.openai.com",
        "confidence": "high",
        "note": "疑似真实密钥",
    })
    # mock 验真返回有效
    svc._verify_sk_real = AsyncMock(return_value={
        "openai": "valid", "custom": "skipped", "custom_base_url": ""
    })

    await svc.process(FakeEvent(
        "12345", "groupA",
        "搞到一个key: sk-abcd1234efgh5678ijkl9012mnop3456", "大佬"
    ))

    assert len(adapter.sent_messages) == 1
    alert = adapter.sent_messages[0]["text"]
    assert "关键词命中预警" in alert
    assert "🔬 平台：OpenAI官方" in alert
    assert "可信度：高" in alert
    assert "✅ 验真" in alert
    assert "有效" in alert
    assert "疑似真实密钥" not in alert  # 备注行已精简掉
    svc.stop()


@pytest.mark.asyncio
async def test_keyword_sk_hit_no_llm_no_verify():
    """LLM 分析和验真都不可用时，alert 仍正常推送（只是没有验真字段）。"""
    cfg = _make_cfg(use_llm_confirm=False)
    adapter = FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))
    # 两个都返回 None（降级）
    svc._analyze_sk_hit = AsyncMock(return_value=None)
    svc._verify_sk_real = AsyncMock(return_value=None)

    await svc.process(FakeEvent(
        "12345", "groupA",
        "sk-abcd1234efgh5678ijkl9012mnop3456", "路人"
    ))

    assert len(adapter.sent_messages) == 1
    alert = adapter.sent_messages[0]["text"]
    assert "关键词命中预警" in alert
    assert "🔬 平台" not in alert  # 分析不可用，无此字段
    assert "✅ 验真" not in alert   # 验真未开，无此字段
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in alert  # 原文仍在
    svc.stop()
