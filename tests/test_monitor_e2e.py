"""
e2e 测试：实时消息监控（整窗汇总模式）全流程

测试链路：消息入队 → flush 触发 → LLM 提取 → 私聊推送
不依赖真实 astrbot / LLM / OneBot，全部 mock。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest 已注册 mock，但 pytest 需要显式导入
import tests.conftest  # noqa: F401

from src.application.services.message_monitor_service import MessageMonitorService
from src.infrastructure.config.config_manager import ConfigManager
from tests.conftest import AstrBotConfig


# ============================================================
# 测试辅助
# ============================================================

def make_config(**overrides) -> ConfigManager:
    """构建一个开了监控的 ConfigManager"""
    base = {
        "message_monitor": {
            "enable_monitor": True,
            "monitored_qqs": ["999"],
            "monitored_groups": ["groupA"],
            "extra_keywords": [],
            "use_llm_confirm": True,
            "flush_interval": 1,  # 1 分钟（测试用短间隔）
            "max_context_messages": 50,
            "alert_admin_qqs": ["888"],
            # 降噪参数
            "cooldown_seconds": 0,
            "dedup_minutes": 0,
            "keyword_batch_seconds": 0,
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
    """模拟 OneBot adapter，带 send_private"""

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
# 测试用例
# ============================================================


@pytest.mark.asyncio
async def test_e2e_target_speaks_llm_finds_value():
    """e2e: 目标QQ发言 → LLM判定有价值 → 私聊推送"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 模拟群聊对话（目标QQ 999 发了 API key）
    events = [
        FakeEvent("111", "groupA", "有没有好用的GPT API", "张三"),
        FakeEvent("222", "groupA", "官方的太贵了", "李四"),
        FakeEvent("999", "groupA", "我搞到一个，sk-abcd1234efgh5678ijkl9012mnop3456", "目标"),
        FakeEvent("222", "groupA", "牛逼能用吗", "李四"),
        FakeEvent("999", "groupA", "能，限免的快抢", "目标"),
    ]

    for evt in events:
        await service.process(evt)

    # 模拟 LLM 返回有价值
    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "items": [{
            "content": "sk-abcd1234efgh5678ijkl9012mnop3456",
            "category": "apikey",
            "reason": "目标分享了一个可用的 OpenAI API key"
        }],
        "summary": "目标分享了一个免费 GPT API key"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp
    ):
        # 手动触发 flush
        await service._flush_all()

    # 验证：推送给 888，内容含 API key
    assert len(fake_adapter.sent_messages) == 1
    msg = fake_adapter.sent_messages[0]
    assert msg["user_id"] == "888"
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in msg["text"]
    assert "apikey" in msg["text"]
    assert "监控预警" in msg["text"]
    print("✓ e2e: 目标发言→LLM有价值→推送成功")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_target_speaks_llm_finds_no_value():
    """e2e: 目标QQ发言但只是闲聊 → LLM判定无价值 → 不推送"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "今天天气不错", "张三"),
        FakeEvent("999", "groupA", "是啊挺好的", "目标"),
        FakeEvent("111", "groupA", "出去走走吧", "张三"),
    ]
    for evt in events:
        await service.process(evt)

    # LLM 判定无价值
    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": False, "items": [], "summary": ""
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp
    ):
        await service._flush_all()

    # 验证：没有推送
    assert len(fake_adapter.sent_messages) == 0
    print("✓ e2e: 目标闲聊→LLM无价值→不推送")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_no_target_in_window_skip():
    """e2e: 窗口里没有目标QQ发言 → 跳过，不调LLM"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 只有非目标QQ发言
    events = [
        FakeEvent("111", "groupA", "哈哈哈", "张三"),
        FakeEvent("222", "groupA", "嘿嘿", "李四"),
    ]
    for evt in events:
        await service.process(evt)

    llm_called = False

    async def mock_llm(*a, **kw):
        nonlocal llm_called
        llm_called = True
        return None

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        side_effect=mock_llm
    ):
        await service._flush_all()

    assert not llm_called, "窗口无目标发言时不应调 LLM"
    assert len(fake_adapter.sent_messages) == 0
    print("✓ e2e: 窗口无目标→跳过LLM→不推送")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_non_monitored_group_ignored():
    """e2e: 非监控群的消息不进缓冲区"""
    cfg = make_config(monitored_groups=["groupA"])
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # groupB 不在监控列表
    await service.process(FakeEvent("999", "groupB", "sk-xxxkey123", "目标"))

    # 缓冲区应该是空的
    assert len(service._buffer) == 0
    print("✓ e2e: 非监控群消息被忽略")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_monitor_disabled():
    """e2e: 开关关闭 → 所有消息忽略"""
    cfg = make_config(enable_monitor=False)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    await service.process(FakeEvent("999", "groupA", "sk-xxx", "目标"))
    assert len(service._buffer) == 0
    print("✓ e2e: 开关关闭→消息忽略")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_llm_unavailable_fallback():
    """e2e: LLM不可用 → 降级推目标QQ原始发言"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "求个API key", "张三"),
        FakeEvent("999", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456", "目标"),
    ]
    for evt in events:
        await service.process(evt)

    # LLM 返回 None（不可用）
    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=None
    ):
        await service._flush_all()

    # 验证：降级推送了目标QQ的原始发言
    assert len(fake_adapter.sent_messages) == 1
    msg = fake_adapter.sent_messages[0]
    assert "sk-abcd1234efgh5678ijkl9012mnop3456" in msg["text"]
    assert "降级" in msg["text"]
    print("✓ e2e: LLM不可用→降级推原始发言")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_context_window_truncation():
    """e2e: 消息超 max_context_messages → 以目标为中心截断"""
    cfg = make_config(max_context_messages=10)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 50条消息，目标QQ在第25条
    events = []
    for i in range(50):
        sid = "999" if i == 24 else str(100 + i)
        name = "目标" if i == 24 else f"用户{sid}"
        events.append(FakeEvent(sid, "groupA", f"消息{i}", name))

    for evt in events:
        await service.process(evt)

    # 捕获送给 LLM 的 prompt
    captured_prompt = {}

    async def mock_llm(context, config_manager, prompt, **kw):
        captured_prompt["text"] = prompt
        resp = MagicMock()
        resp.completion_text = json.dumps({
            "has_value": False, "items": [], "summary": ""
        })
        return resp

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        side_effect=mock_llm
    ):
        await service._flush_all()

    # 验证：LLM prompt 里只有约10条消息（截断后）
    prompt_text = captured_prompt.get("text", "")
    # 数 [序号] 的数量
    import re
    indices = re.findall(r"\[(\d+)\]", prompt_text.split("群聊记录")[1] if "群聊记录" in prompt_text else prompt_text)
    # 截断后最多10条
    assert len(indices) <= 12, f"截断后应<=10条，实际{len(indices)}"
    # 目标QQ的发言在窗口内
    assert "999" in prompt_text
    print(f"✓ e2e: 50条→截断为{len(indices)}条，目标在窗口内")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_full_dialog_context_sent_to_llm():
    """e2e: LLM 收到完整对话（标注发送者，目标用 >>> 标记）"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    events = [
        FakeEvent("111", "groupA", "有没有API", "张三"),
        FakeEvent("999", "groupA", "sk-xxxkey1234567890", "目标"),
        FakeEvent("111", "groupA", "谢谢", "张三"),
    ]
    for evt in events:
        await service.process(evt)

    captured = {}

    async def mock_llm(context, config_manager, prompt, **kw):
        captured["prompt"] = prompt
        resp = MagicMock()
        resp.completion_text = json.dumps({
            "has_value": True,
            "items": [{"content": "sk-xxx", "category": "apikey", "reason": "test"}],
            "summary": "test"
        })
        return resp

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        side_effect=mock_llm
    ):
        await service._flush_all()

    prompt = captured.get("prompt", "")
    # 验证：对话里有所有人的发言
    assert "111" in prompt  # 非目标也在
    assert "999" in prompt  # 目标在
    # 验证：目标用 >>> 标记
    assert ">>>" in prompt
    # 验证：prompt 里说明了目标是谁
    assert "999" in prompt
    print("✓ e2e: LLM收到完整对话上下文（含>>>标注）")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_multiple_flush_cycles():
    """e2e: 多轮 flush——第一轮有值推送，第二轮清空不推送"""
    cfg = make_config()
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    # 第一轮：目标发言
    await service.process(FakeEvent("999", "groupA", "sk-valuable123456789012345", "目标"))

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "items": [{"content": "sk-valuable", "category": "apikey", "reason": "key"}],
        "summary": "有key"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp
    ):
        await service._flush_all()

    assert len(fake_adapter.sent_messages) == 1

    # 第二轮：没有新消息
    await service._flush_all()
    assert len(fake_adapter.sent_messages) == 1  # 不增加

    print("✓ e2e: 多轮flush正确（第二轮空缓冲不推送）")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_push_format_correct():
    """e2e: 推送消息格式正确（含来源/群/条目/时间）"""
    cfg = make_config(flush_interval=10)
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    await service.process(FakeEvent("999", "groupA", "sk-test123456789012345678901", "目标王"))

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "items": [{"content": "sk-test", "category": "apikey", "reason": "可用key"}],
        "summary": "分享了一个API key"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp
    ):
        await service._flush_all()

    msg = fake_adapter.sent_messages[0]["text"]
    assert "🚨" in msg
    assert "目标王" in msg
    assert "999" in msg
    assert "groupA" in msg
    assert "近 10 分钟" in msg
    assert "⏰" in msg
    print("✓ e2e: 推送格式正确")
    print(f"  推送内容预览:\n{msg[:200]}...")

    service.stop()


@pytest.mark.asyncio
async def test_e2e_alert_targets_fallback():
    """e2e: alert_admin_qqs 为空时回退到管理员列表"""
    cfg = make_config(alert_admin_qqs=[])
    # 设置 extra_admin_qq
    cfg.config["admin_notify"] = {"extra_admin_qq": ["777"]}
    fake_adapter = FakeAdapter()
    bot_mgr = FakeBotManager(fake_adapter)

    service = MessageMonitorService(MagicMock(), cfg, bot_mgr)

    await service.process(FakeEvent("999", "groupA", "sk-test123456789012345678901", "目标"))

    llm_resp = MagicMock()
    llm_resp.completion_text = json.dumps({
        "has_value": True,
        "items": [{"content": "sk-test", "category": "apikey", "reason": "x"}],
        "summary": "x"
    })

    with patch(
        "src.application.services.message_monitor_service.call_provider_with_retry",
        new_callable=AsyncMock,
        return_value=llm_resp
    ):
        await service._flush_all()

    # 验证：推给了 777（从 admin_notify.extra_admin_qq 回退）
    assert len(fake_adapter.sent_messages) == 1
    assert fake_adapter.sent_messages[0]["user_id"] == "777"
    print("✓ e2e: 推送目标回退到 admin_notify.extra_admin_qq")

    service.stop()
