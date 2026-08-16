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
    # sk-ant- 不推断（Anthropic 官方非 OpenAI 兼容协议，等中转站地址）
    pool.add(_SK_C)
    assert pool.entries()[1]["base_url"] == ""
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
    """冷却制状态机：失败进冷却（dead=冷却中），成功完全复活。"""
    pool = _pool(tmp_path)
    pool.add(_SK_A)
    # 转发成功 → usable
    pool.mark_success(_SK_A, _OPENAI)
    assert pool.stats()["usable"] == 1
    # 瞬态失败 → 立即进冷却（不再有"连续 N 次才踢"）
    pool.mark_failure(_SK_A, _OPENAI, kind="transient")
    assert pool.stats()["dead"] == 1
    # 成功复活 → usable 且冷却清零
    pool.mark_success(_SK_A, _OPENAI)
    assert pool.stats()["usable"] == 1 and pool.stats()["dead"] == 0


def test_pool_cooldown_revival(tmp_path):
    """冷却到期后 next_candidates 惰性复活为 unverified。"""
    import time as _time
    pool = _pool(tmp_path)
    pool.add(_SK_A, base_url=_OPENAI)
    pool.mark_failure(_SK_A, _OPENAI, kind="auth")  # 30min 冷却
    assert pool.next_candidates() == []  # 冷却中不可选
    assert 0 < pool.earliest_revival() <= _time.time() + 30 * 60
    # 把冷却时间改成过去 → 复活（保留 fail_count，退避要继续累计）
    pool._entries[0]["cooldown_until"] = int(_time.time()) - 1
    revived = pool.next_candidates()
    assert [e["sk"] for e in revived] == [_SK_A]
    assert revived[0]["status"] == "unverified" and revived[0]["fail_count"] == 1


def test_pool_rate_limit_backoff(tmp_path):
    """429 指数退避：30s→60s→120s→…封顶 5min；成功归零。"""
    import time as _time
    pool = _pool(tmp_path)
    pool.add(_SK_A, base_url=_OPENAI)

    def _cooldown_left() -> int:
        return pool._entries[0]["cooldown_until"] - int(_time.time())

    prev = 0  # 入池时 cooldown_until=0，首次失败后 left≈30s
    seen = []
    for _ in range(8):
        pool.mark_failure(_SK_A, _OPENAI, kind="rate_limit")
        left = _cooldown_left()
        seen.append(left - prev)
        prev = left
    assert seen[0] <= 30  # 第 1 次 30s
    assert all(g <= 5 * 60 for g in seen)  # 封顶 5min
    assert max(seen) > 60  # 确实在指数增长
    # 成功 → fail_count 清零，下次 429 回到 30s 起点
    pool.mark_success(_SK_A, _OPENAI)
    pool.mark_failure(_SK_A, _OPENAI, kind="rate_limit")
    assert _cooldown_left() <= 30


def test_next_candidates_order(tmp_path):
    pool = _pool(tmp_path)
    pool.add(_SK_B)  # 先来
    pool.add(_SK_A)  # 后来
    # usable 优先、unverified 按先来先用
    pool.mark_success(_SK_A, _OPENAI)
    pool.add(_SK_C, base_url="https://api.example.com")
    pool.mark_failure(_SK_C, "https://api.example.com", kind="auth")  # 冷却中排除
    order = [e["sk"] for e in pool.next_candidates()]
    assert order[0] == _SK_A  # usable 优先
    assert _SK_B in order and _SK_C not in order  # unverified 在，冷却中不在


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


def test_gateway_all_cooled_returns_503_with_retry_after(tmp_path):
    """全部渠道冷却中 → 503 + Retry-After（最早恢复时间）。"""

    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        pool.mark_failure(_SK_A, _OPENAI, kind="auth")  # 30min 冷却
        client, _ = await _make_client(pool)
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": []},
            )
            assert resp.status == 503
            retry_after = int(resp.headers.get("Retry-After", "0"))
            assert 0 < retry_after <= 30 * 60
            data = await resp.json()
            assert "冷却" in data["error"]["message"]
        finally:
            await client.close()

    _run(body())


def test_gateway_400_passthrough_no_failover(tmp_path):
    """400（请求本身的问题）：不换渠道不冷却，上游响应原样透传。"""

    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        pool.add(_SK_B, base_url="https://api.example.com")
        client, gw = await _make_client(pool)
        gw._session = _mock_session(
            _mock_http_response(400, {"error": {"message": "max_tokens 太大"}})
        )
        gw._session_or_create = lambda: gw._session
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": []},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"]["message"] == "max_tokens 太大"
            # 只调了第一个渠道（没换渠道），且它没进冷却
            assert gw._session.post.call_count == 1
            assert pool.stats()["dead"] == 0
        finally:
            await client.close()

    _run(body())


def test_gateway_real_session_forward_smoke(tmp_path, monkeypatch):
    """不 mock ClientSession，走真实 HTTP（本地 mock 上游）。

    覆盖 _session_or_create 真实路径——之前所有转发测试都 mock 掉了它，
    导致超时常量改名后的 NameError 没被抓到。
    """
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)

    from aiohttp import web as aioweb

    async def body():
        upstream_auth = []

        async def upstream(request):
            upstream_auth.append(request.headers.get("Authorization"))
            await request.json()
            return aioweb.json_response(
                {"choices": [{"message": {"content": "real-io"}}]}
            )

        upstream_app = aioweb.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream)
        up = TestServer(upstream_app)
        await up.start_server()
        base = str(up.make_url("/")).rstrip("/")

        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=base)
        client, gw = await _make_client(pool)
        try:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer mk-test"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "real-io"
            # 上游收到的是渠道 key；渠道转正为 usable
            assert upstream_auth == [f"Bearer {_SK_A}"]
            assert pool.stats()["usable"] == 1
        finally:
            await client.close()
            if gw._session and not gw._session.closed:
                await gw._session.close()
            await up.close()

    _run(body())


def test_gateway_stream_broken_no_failover(tmp_path):
    """流式开始后上游中断：不换渠道重发（post 只调一次），渠道记一次失败。"""

    async def body():
        pool = _pool(tmp_path)
        pool.add(_SK_A, base_url=_OPENAI)
        pool.add(_SK_B, base_url="https://api.example.com")
        client, gw = await _make_client(pool)

        broken_resp = MagicMock()
        broken_resp.status = 200
        broken_resp.headers = {"Content-Type": "text/event-stream"}

        async def _explode(*_a, **_k):
            raise RuntimeError("upstream died mid-stream")
            yield b""  # pragma: no cover

        broken_resp.content = _explode()
        gw._session = _mock_session(broken_resp)
        gw._session_or_create = lambda: gw._session
        try:
            resp = None
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer mk-test"},
                    json={"model": "gpt-4o", "stream": True, "messages": []},
                )
                await resp.read()
            except Exception:
                pass  # 流中断的客户端表现（断连/空体）都算符合预期
            # 关键断言：没有对同一请求换渠道重发
            assert gw._session.post.call_count == 1
            # 渠道 A 记一次失败进冷却，渠道 B 未被消耗
            assert pool.stats()["dead"] == 1
        finally:
            await client.close()

    _run(body())


def test_pool_revival_keeps_backoff_progress(tmp_path):
    """429 冷却→到期复活→再 429：退避累计（60s 起），而非每次回到 30s。"""
    import time as _time
    pool = _pool(tmp_path)
    pool.add(_SK_A, base_url=_OPENAI)
    pool.mark_failure(_SK_A, _OPENAI, kind="rate_limit")  # 第 1 次：30s
    assert pool._entries[0]["cooldown_until"] - int(_time.time()) <= 30
    # 冷却到期 → 惰性复活（不清 fail_count）
    pool._entries[0]["cooldown_until"] = int(_time.time()) - 1
    assert len(pool.next_candidates()) == 1
    assert pool._entries[0]["fail_count"] == 1  # 复活保留计数
    pool.mark_failure(_SK_A, _OPENAI, kind="rate_limit")  # 第 2 次：60s
    left2 = pool._entries[0]["cooldown_until"] - int(_time.time())
    assert left2 > 30  # 退避在累计，不是回到 30s 起点
