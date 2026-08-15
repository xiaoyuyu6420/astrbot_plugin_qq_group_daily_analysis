"""SK 池 + SK 聚合网关测试。

覆盖：
- 池：去重 / base_url 推断 / 容量滚动淘汰 / 状态机（usable/dead）/ 候选排序 / 持久化
- 网关：master key 鉴权 / 转发成功 / 失败切换 / 全挂 502 / 池空 503

项目无 pytest-asyncio，async 用例统一用 asyncio.run 包装。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from src.infrastructure.messaging.sk_gateway import SkGateway
from src.infrastructure.messaging.sk_pool import SkPool

_SK_A = "sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_SK_B = "sk-proj-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SK_C = "sk-ant-cccccccccccccccccccccccccccccccccccccccc"
_OPENAI = "https://api.openai.com"


# ---------------------------------------------------------------------------
# SkPool
# ---------------------------------------------------------------------------


def _pool(tmp_path, max_size=10) -> SkPool:
    return SkPool(str(tmp_path / "sk.json"), max_size=max_size)


def test_pool_add_dedup_and_infer_base_url(tmp_path):
    pool = _pool(tmp_path)
    assert pool.add(_SK_A) is True  # 新增
    assert pool.add(_SK_A) is False  # 去重
    entry = pool.entries()[0]
    assert entry["base_url"] == _OPENAI  # sk-proj- → OpenAI 官方
    assert entry["status"] == "unverified"
    # sk-ant- → Anthropic
    pool.add(_SK_C)
    assert pool.entries()[1]["base_url"] == "https://api.anthropic.com"
    # 显式中转站地址优先于推断
    pool.add(_SK_B, base_url="https://api.example.com")
    assert pool.entries()[2]["base_url"] == "https://api.example.com"


def test_pool_rollover_drops_oldest(tmp_path):
    pool = _pool(tmp_path, max_size=2)
    pool.add(_SK_A)
    pool.add(_SK_B)
    pool.add(_SK_C)
    entries = pool.entries()
    assert len(entries) == 2
    # 最旧的 _SK_A 被淘汰
    assert all(e["sk"] != _SK_A for e in entries)


def test_pool_status_machine(tmp_path):
    pool = _pool(tmp_path)
    pool.add(_SK_A)
    # 转发成功 → usable
    pool.mark_success(_SK_A, _OPENAI)
    assert pool.stats()["usable"] == 1
    # 普通失败计数：1 次不死，2 次 dead
    assert pool.mark_failure(_SK_A, _OPENAI) is False
    assert pool.mark_failure(_SK_A, _OPENAI) is True
    assert pool.stats()["dead"] == 1
    # fatal（401/403）直接 dead
    pool.add(_SK_B)
    assert pool.mark_failure(_SK_B, _OPENAI, fatal=True) is True


def test_next_candidates_order(tmp_path):
    pool = _pool(tmp_path)
    pool.add(_SK_B)  # 先来
    pool.add(_SK_A)  # 后来
    # usable 优先、unverified 按先来先用
    pool.mark_success(_SK_A, _OPENAI)
    pool.add(_SK_C)
    pool.mark_failure(_SK_C, "https://api.anthropic.com", fatal=True)  # dead 排除
    order = [e["sk"] for e in pool.next_candidates()]
    assert order[0] == _SK_A  # usable 优先
    assert _SK_B in order and _SK_C not in order  # unverified 在，dead 不在


def test_pool_persists_across_reload(tmp_path):
    path = str(tmp_path / "sk.json")
    pool = SkPool(path)
    pool.add(_SK_A, base_url=_OPENAI)
    pool.mark_success(_SK_A, _OPENAI)
    pool2 = SkPool(path)  # 重新加载
    assert pool2.stats()["usable"] == 1
    assert pool2.entries()[0]["sk"] == _SK_A


# ---------------------------------------------------------------------------
# SkGateway（真实 aiohttp server + mock 上游）
# ---------------------------------------------------------------------------


def _mock_http_response(status: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(
        return_value=payload or {"choices": [{"message": {"content": "ok"}}]}
    )
    resp.headers = {"Content-Type": "application/json"}
    return resp


def _mock_session(*responses) -> MagicMock:
    """mock ClientSession：session.post 按顺序返回给定响应。"""
    session = MagicMock()
    cms = []
    for resp in responses:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        cms.append(cm)
    session.post = MagicMock(side_effect=cms)
    return session


async def _make_client(pool: SkPool, master_key: str = "mk-test"):
    gw = SkGateway(pool, master_key=master_key, host="127.0.0.1", port=0)
    server = TestServer(gw._build_app())
    client = TestClient(server)
    await client.start_server()
    return client, gw


def _run(coro):
    return asyncio.run(coro)


# ── 鉴权 ──────────────────────────────────────────────────────────────


def test_gateway_requires_auth(tmp_path):
    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        client, _ = await _make_client(pool)
        try:
            resp = await client.get("/v1/models")
            assert resp.status == 401
            resp2 = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp2.status == 401
        finally:
            await client.close()

    _run(body())


def test_gateway_no_master_key_does_not_start(tmp_path):
    pool = _pool(tmp_path)
    gw = SkGateway(pool, master_key="", host="127.0.0.1", port=0)
    assert _run(gw.start()) is False


def test_gateway_models_with_auth(tmp_path):
    async def body():
        pool = _pool(tmp_path)
        client, _ = await _make_client(pool)
        try:
            resp = await client.get(
                "/v1/models", headers={"Authorization": "Bearer mk-test"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["object"] == "list" and len(data["data"]) > 0
        finally:
            await client.close()

    _run(body())


# ── 转发 / 切换 ───────────────────────────────────────────────────────


def test_gateway_forward_success_and_marks_usable(tmp_path):
    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        client, gw = await _make_client(pool)
        gw._session = _mock_session(_mock_http_response(200))
        gw._session_or_create = lambda: gw._session
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "ok"
            # 转发成功 → usable
            assert pool.stats()["usable"] == 1
            # 上游收到的是渠道自己的 key（不是 master key）
            call_kwargs = gw._session.post.call_args
            assert call_kwargs.kwargs["headers"]["Authorization"] == f"Bearer {_SK_A}"
        finally:
            await client.close()

    _run(body())


def test_gateway_failover_switches_channel(tmp_path):
    """第一个渠道 401 → 直接判死，换第二个渠道成功。"""

    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        pool.add(_SK_B, base_url="https://api.example.com")
        client, gw = await _make_client(pool)
        gw._session = _mock_session(
            _mock_http_response(401),
            _mock_http_response(200, {"choices": [{"message": {"content": "from-b"}}]}),
        )
        gw._session_or_create = lambda: gw._session
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": []},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "from-b"
            # 第一个渠道被踢 dead，第二个 usable
            stats = pool.stats()
            assert stats["dead"] == 1 and stats["usable"] == 1
        finally:
            await client.close()

    _run(body())


def test_gateway_all_channels_failed_returns_502(tmp_path):
    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        pool.add(_SK_B, base_url="https://api.example.com")
        client, gw = await _make_client(pool)
        gw._session = _mock_session(
            _mock_http_response(401),
            _mock_http_response(503),
        )
        gw._session_or_create = lambda: gw._session
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": []},
            )
            assert resp.status == 502
        finally:
            await client.close()

    _run(body())


def test_gateway_empty_pool_returns_503(tmp_path):
    async def body():
        pool = _pool(tmp_path)  # 空池
        client, _ = await _make_client(pool)
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": []},
            )
            assert resp.status == 503
        finally:
            await client.close()

    _run(body())
