"""llm_utils.call_provider_with_retry 的调用参数测试。

核心回归：max_tokens 兜底 8192 必须传给 context.llm_generate。
背景：AstrBot 的 anthropic 源在调用方不传 max_tokens 时默认 1024，
推理模型（thinking + 长数组 JSON）必然截断，导致日报分析条目归零。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.analysis.utils.llm_utils import call_provider_with_retry


def _make_context(llm_generate_mock):
    context = MagicMock()
    context.llm_generate = llm_generate_mock
    return context


def _make_config():
    cfg = MagicMock()
    cfg.get_llm_retries.return_value = 1
    cfg.get_llm_backoff.return_value = 1
    cfg.get_enable_streaming_llm_call.return_value = False
    # provider 选择回退链
    cfg.get_llm_provider_id.return_value = None
    return cfg


def test_max_tokens_default_passed_to_llm_generate():
    """不显式传时，max_tokens=8192 兜底必须出现在 llm_generate kwargs。"""
    llm = AsyncMock(return_value=SimpleNamespace(completion_text="[]"))
    context = _make_context(llm)

    async def run():
        resp = await call_provider_with_retry(
            context=context,
            config_manager=_make_config(),
            prompt="hi",
            provider_id="prov/a",
        )
        return resp

    asyncio.run(run())
    kwargs = llm.call_args.kwargs
    assert kwargs.get("max_tokens") == 8192


def test_max_tokens_explicit_override_wins():
    """extra_generate_kwargs 显式传 max_tokens 时优先于兜底值。"""
    llm = AsyncMock(return_value=SimpleNamespace(completion_text="[]"))
    context = _make_context(llm)

    async def run():
        await call_provider_with_retry(
            context=context,
            config_manager=_make_config(),
            prompt="hi",
            provider_id="prov/a",
            extra_generate_kwargs={"max_tokens": 2048},
        )

    asyncio.run(run())
    kwargs = llm.call_args.kwargs
    assert kwargs.get("max_tokens") == 2048
