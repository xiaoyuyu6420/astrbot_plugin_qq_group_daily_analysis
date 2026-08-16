"""测试：裸密钥形态（fuzzy hex/base64）的误伤防护与确认链路。

背景：原实现把 `[A-Fa-f0-9]{32,}` / `[A-Za-z0-9+/]{40,}` 都算 "API Key"
（critical 秒推、免 LLM 确认），群聊里磁力 hash/长数字/URL 参数段/字母刷屏
全部误报直达推送。现在拆成低置信 "疑似密钥" 类：正则收紧 + 必须 LLM 确认。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.conftest  # noqa: F401

from src.application.services.message_monitor_service import (
    MessageMonitorService,
    _fuzzy_key_hits,
)
from tests.test_sk_monitor_enhancements import (
    FakeAdapter,
    FakeBotManager,
    FakeEvent,
    _make_cfg,
)

# 磁力链 info hash（40 位 hex，群聊常见）
_BTIH = "magnet:?xt=urn:btih:5e8f3a9c2b7d1e6f4a8c0d3b2e1f9a7c6d5e4f3a"
# 真·裸 hex key 形态（独立 token、非纯数字）
_HEX_KEY = "f3a9c2e87b1d4f6a9c2e87b1d4f6a9c2"
# 真·裸 base64 key 形态（大小写+数字混合，44 位）
_B64_KEY = "Ab3xK9mQ2pL8nR4vT6wY1uC5hJ7gF0dSzE3bN2mA9lK6"
# URL 长参数段（40+ 混合字符，但前面是 /，属于 URL 一部分）
_URL_LONG = "https://example.com/s/Ab3xK9mQ2pL8nR4vT6wY1uC5hJ7gF0dSzE3bN2mA9lK6"
# 纯字母刷屏（45 位，无数字）
_LETTERS = "hAHAHAhAhAHahAhAHaHaHAhaHAhaHAhaHAhaHAhaHAhaHAha"


# ============================================================
# 形态层：_fuzzy_key_hits 误伤防护
# ============================================================


class TestFuzzyKeyHits:
    def test_magnet_hash_excluded(self):
        """磁力链 info hash 不算密钥（由资源链接类负责）。"""
        assert _fuzzy_key_hits(_BTIH) == []

    def test_pure_digits_excluded(self):
        """纯数字串（订单号/号码连拼/验证码）不算密钥。"""
        assert _fuzzy_key_hits("订单号 12345678901234567890123456789012 查下") == []

    def test_url_segment_excluded(self):
        """URL 路径里的长 token 段不算密钥。"""
        assert _fuzzy_key_hits(_URL_LONG) == []

    def test_pure_letters_excluded(self):
        """纯字母刷屏（无数字）不算密钥。"""
        assert _fuzzy_key_hits(_LETTERS) == []

    def test_no_lowercase_excluded(self):
        """只有大写+数字（无小写）的标语/编码不算密钥。"""
        assert _fuzzy_key_hits("AB12CD34EF56GH78IJ90KL12MN34OP56QR78ST90UV34WX") == []

    def test_hex_word_middle_excluded(self):
        """单词中段的 hex 串（前面贴着字母）不算独立 token。"""
        assert _fuzzy_key_hits("hash5e8f3a9c2b7d1e6f4a8c0d3b2e1f9a7c6d5e4f3a") == []

    def test_real_hex_key_hits(self):
        hits = _fuzzy_key_hits(f"白嫖一个key: {_HEX_KEY} 大家冲")
        assert ("疑似密钥", "疑似裸 hex 密钥串") in hits

    def test_real_b64_key_hits(self):
        hits = _fuzzy_key_hits(f"中转站key发一下 {_B64_KEY} 速度冲")
        assert ("疑似密钥", "疑似裸 base64 密钥串") in hits

    def test_sk_prefix_not_fuzzy(self):
        """sk- 前缀属于高置信 strict 规则，不进 fuzzy 结果。"""
        assert _fuzzy_key_hits("sk-abcd1234efgh5678ijkl9012mnop3456") == []


# ============================================================
# 行为层：fuzzy 必须过 LLM 确认
# ============================================================


def _svc(cfg=None, adapter=None):
    cfg = cfg or _make_cfg()
    adapter = adapter or FakeAdapter()
    svc = MessageMonitorService(MagicMock(), cfg, FakeBotManager(adapter))
    return svc, adapter


@pytest.mark.asyncio
async def test_fuzzy_only_without_llm_confirm_dropped():
    """fuzzy 命中 + LLM 确认未开 → 宁可漏推不误推，直接丢弃。"""
    svc, adapter = _svc(_make_cfg(use_llm_confirm=False))
    await svc.process(FakeEvent("123", "groupA", f"看看这个 {_HEX_KEY} 像不像key"))
    assert adapter.sent_messages == []


@pytest.mark.asyncio
async def test_fuzzy_with_llm_reject_not_pushed():
    """fuzzy 命中 + LLM 判无用（如文件校验值）→ 不推。"""
    svc, adapter = _svc(_make_cfg(use_llm_confirm=True))
    svc._llm_confirm_keyword = AsyncMock(return_value={"useful": False, "reason": "是文件校验值"})
    await svc.process(FakeEvent("123", "groupA", f"SHA256: {_HEX_KEY} 校验对一下"))
    assert adapter.sent_messages == []


@pytest.mark.asyncio
async def test_fuzzy_with_llm_confirm_pushed_as_normal():
    """fuzzy 命中 + LLM 确认有用 → 以"疑似密钥"类推送（非 critical）。"""
    svc, adapter = _svc(_make_cfg(use_llm_confirm=True))
    svc._llm_confirm_keyword = AsyncMock(return_value={
        "useful": True, "reason": "上下文有分享意图，是中转站 key", "category": "apikey",
    })
    await svc.process(FakeEvent("123", "groupA", f"白嫖key: {_HEX_KEY} 大家快冲"))
    assert len(adapter.sent_messages) == 1
    alert = adapter.sent_messages[0]["text"]
    # LLM 判定类别（apikey）覆盖展示；关键是原文完整送达
    assert "apikey" in alert
    assert _HEX_KEY in alert
    # 非 critical：不应带 critical 专属的验真/平台分析流程
    assert "🔬" not in alert


@pytest.mark.asyncio
async def test_sk_prefix_still_instant_push():
    """对照：sk- 标准前缀仍是 critical 秒推（不受 fuzzy 收紧影响）。"""
    svc, adapter = _svc(_make_cfg(use_llm_confirm=False))
    await svc.process(FakeEvent("123", "groupA", "sk-abcd1234efgh5678ijkl9012mnop3456"))
    assert len(adapter.sent_messages) == 1
