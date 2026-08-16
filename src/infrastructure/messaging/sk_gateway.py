"""SK 聚合网关（轻量 9router/newapi 替代品，集成在插件进程内）。

把监控抓到的 API Key 聚合成 OpenAI 兼容网关：
  POST /v1/chat/completions  → 选一个可用渠道（base_url + key）原样转发，失败自动切换
  GET  /v1/models            → 静态模型名单（展示用；实际请求 model 透传，不做映射）
  GET  /healthz              → 存活检查（含池统计，无敏感信息）

设计取舍（保持简单，借鉴 9router/new-api 的降级策略）：
- 无数据库/前端/计费，只有「读池 + 转发」
- 懒验证：不主动联网验真（避免服务器 IP 被外部 key 后台记录），转发失败才冷却
- master key 鉴权：必须配置非空才启动，否则日志警告跳过
- 渠道选择：usable（最近最少用）→ unverified（先来先用），冷却中的排除
- 失败分级冷却（不永久踢死，到期自动复活，成功即完全复活）：
  401/403/402 → 30 分钟；429 → 指数退避 30s→5min；5xx/网络 → 30s
- 请求本身的问题（400/404/422 等）不换渠道不冷却，上游响应原样透传
- 全池冷却中 → 503 + Retry-After（最早恢复时间）
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from ...utils.logger import logger
from .sk_pool import SkPool

# 展示用模型名单（客户端主要拿它校验/展示，实际请求透传不校验）
_MODEL_LIST: list[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
    "deepseek-chat",
    "deepseek-reasoner",
    "gemini-2.0-flash",
]

# 超时层级（借鉴 new-api/9router：流式不设总时长，用"字节间空闲"做 watchdog）：
# - 非流式：整体 180s
# - 流式：无总时长（长回复不掐断），chunk 间隔 90s 无数据才掐（防上游半死挂死）
# - 连接建立统一 10s
_TIMEOUT_NOSTREAM = ClientTimeout(total=180, connect=10, sock_read=90)
_TIMEOUT_STREAM = ClientTimeout(total=None, connect=10, sock_read=90)
# 转发失败时用于给上游的 UA（部分中转站按 UA 风控）
_UA = "astrbot-sk-gateway/1.0"
# 单个请求最多尝试的渠道数（池子可能很大，全试一遍 502 会等太久）
_MAX_ATTEMPTS = 5


class _ChannelFailed(Exception):
    """当前渠道转发失败（换下一个渠道重试）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class _StreamBroken(Exception):
    """流式转发已开始（响应头已发给客户端）后上游中断。

    此时无法换渠道重试——同一个请求不能发两份响应，只能断开连接。
    """


class SkGateway:
    """aiohttp 网关服务，随插件生命周期启停。"""

    def __init__(self, pool: SkPool, master_key: str, host: str = "0.0.0.0", port: int = 6187):
        self._pool = pool
        self._master_key = (master_key or "").strip()
        self._host = host
        self._port = int(port)
        self._runner: web.AppRunner | None = None
        self._session: ClientSession | None = None
        self._site: web.TCPSite | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """启动服务。master key 未配置返回 False（不启动）。"""
        if not self._master_key:
            logger.warning(
                "[SkGateway] gateway_master_key 未配置，SK 聚合网关不启动"
            )
            return False
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await self._site.start()
        except OSError as e:
            logger.error(f"[SkGateway] 端口 {self._port} 监听失败: {e}")
            await self._runner.cleanup()
            self._runner = None
            return False
        logger.info(
            f"[SkGateway] SK 聚合网关已启动 http://{self._host}:{self._port} "
            f"（池 {self._pool.stats()}）"
        )
        return True

    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=1024 * 1024 * 4)
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_get("/healthz", self._handle_healthz)
        return app

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("[SkGateway] SK 聚合网关已停止")

    # ------------------------------------------------------------------
    # 处理函数
    # ------------------------------------------------------------------

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pool": self._pool.stats()})

    async def _handle_models(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._unauthorized()
        return web.json_response(
            {"object": "list", "data": [{"id": m, "object": "model"} for m in _MODEL_LIST]}
        )

    async def _handle_chat_completions(self, request: web.Request) -> web.StreamResponse | web.Response:
        if not self._check_auth(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}},
                status=400,
            )
        stream = bool(body.get("stream", False))

        candidates = self._pool.next_candidates()
        usable = [c for c in candidates if c.get("base_url")]
        if not usable:
            # 区分池空 vs 全部冷却中（后者给出最早恢复时间，客户端可定时重试）
            revival = self._pool.earliest_revival()
            if revival:
                wait = max(1, revival - int(time.time()))
                msg = f"SK 池全部渠道冷却中，最早 {wait}s 后有渠道恢复"
            else:
                wait = 0
                msg = "SK 池暂无可用渠道（key 池为空或全部失效）"
            headers = {"Retry-After": str(wait)} if wait else {}
            return web.json_response(
                {"error": {"message": msg, "type": "no_available_channel"}},
                status=503,
                headers=headers,
            )

        # 并发请求各自独立选渠道（next_candidates 返回池快照，内部有锁）；
        # 单个渠道失败立即换下一个，最多试 _MAX_ATTEMPTS 个。
        errors: list[str] = []
        chain: list[str] = []  # 尝试链路（日志用）
        started = time.monotonic()
        for entry in usable[:_MAX_ATTEMPTS]:
            tag = f"{entry['sk'][:6]}@{(entry['base_url'] or '').split('//')[-1]}"
            chain.append(tag)
            try:
                resp = await self._forward(entry, body, stream, request)
                logger.info(
                    f"[SkGateway] {body.get('model', '?')} {'→'.join(chain)} "
                    f"耗时 {time.monotonic() - started:.1f}s"
                )
                return resp
            except _ChannelFailed as e:
                errors.append(f"{tag}: {e.message}")
                continue
        logger.warning(
            f"[SkGateway] {body.get('model', '?')} {'→'.join(chain)} 全部失败"
        )
        if len(usable) > _MAX_ATTEMPTS:
            errors.append(f"（仅尝试了前 {_MAX_ATTEMPTS}/{len(usable)} 个渠道）")
        return web.json_response(
            {
                "error": {
                    "message": "所有渠道转发失败: " + "; ".join(errors),
                    "type": "all_channels_failed",
                }
            },
            status=502,
        )

    # ------------------------------------------------------------------
    # 转发
    # ------------------------------------------------------------------

    def _session_or_create(self) -> ClientSession:
        if self._session is None or self._session.closed:
            # trust_env 走系统代理（与 sk_verifier 同范式）；
            # session 级默认非流式超时，流式请求在 post() 处单独覆盖
            self._session = ClientSession(timeout=_TIMEOUT_NOSTREAM, trust_env=True)
        return self._session

    async def _forward(
        self, entry: dict[str, Any], body: dict[str, Any], stream: bool, request: web.Request
    ) -> web.StreamResponse | web.Response:
        sk = entry["sk"]
        base_url = entry["base_url"]
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {sk}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        }
        session = self._session_or_create()
        # 流式不设总时长（chunk 间隔 90s 无数据由 sock_read 掐断，防挂死不掐长回复）
        timeout = _TIMEOUT_STREAM if stream else _TIMEOUT_NOSTREAM
        try:
            async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                if resp.status in (401, 403):
                    self._pool.mark_failure(sk, base_url, kind="auth")
                    raise _ChannelFailed(f"上游鉴权失败 {resp.status}")
                if resp.status == 402:
                    # 余额不足：这个渠道短期内不会有额度，同鉴权失败走长冷却
                    self._pool.mark_failure(sk, base_url, kind="auth")
                    raise _ChannelFailed("上游余额不足 402")
                if resp.status == 429:
                    self._pool.mark_failure(sk, base_url, kind="rate_limit")
                    raise _ChannelFailed("上游限流 429")
                if resp.status >= 500:
                    self._pool.mark_failure(sk, base_url, kind="transient")
                    raise _ChannelFailed(f"上游错误 {resp.status}")
                if resp.status >= 400:
                    # 请求本身的问题（400/404/422 等）：换渠道也没用，
                    # 不冷却渠道，把上游原始响应透传给客户端（最有诊断价值）
                    try:
                        data = await resp.json()
                        return web.json_response(data, status=resp.status)
                    except Exception:
                        text = await resp.text()
                        return web.json_response(
                            {"error": {"message": text[:500], "type": "upstream_error",
                                       "upstream_status": resp.status}},
                            status=resp.status,
                        )

                if stream:
                    return await self._forward_stream(resp, sk, base_url, request)
                data = await resp.json()
                self._pool.mark_success(sk, base_url)
                return web.json_response(data, status=resp.status)
        except (_ChannelFailed, _StreamBroken):
            # _StreamBroken：响应已发给客户端（流式已开始），绝不能换渠道重发
            raise
        except Exception as e:
            # 网络/超时类失败：短冷却，下个请求可能恢复
            logger.debug(f"[SkGateway] 渠道 {sk[:10]}...@{(base_url or '').split('//')[-1]} 转发异常: {type(e).__name__}: {e}", exc_info=True)
            self._pool.mark_failure(sk, base_url, kind="transient")
            raise _ChannelFailed(f"{type(e).__name__}") from e

    async def _forward_stream(
        self, resp, sk: str, base_url: str, request: web.Request
    ) -> web.StreamResponse:
        """流式透传：上游 SSE 分块原样写回。"""
        response = web.StreamResponse(status=resp.status)
        content_type = resp.headers.get("Content-Type", "text/event-stream")
        response.headers["Content-Type"] = content_type
        await response.prepare(request)
        try:
            async for chunk in resp.content:
                if chunk:
                    await response.write(chunk)
        except asyncio.CancelledError:
            # 客户端断开（aiohttp 取消）：不判渠道失败，直接让取消传播
            logger.info(f"[SkGateway] 流结束 client_gone（渠道 {sk[:6]}...）")
            raise
        except ConnectionResetError as e:
            # 客户端连接重置：key 本身可能没问题，不记失败；
            # 但客户端已不在，不能换渠道重发 → 按流已断处理。
            # 权衡：极罕见时上游 socket RST 也抛此类型，会漏记一次失败
            # （后果轻微，下次失败仍会进冷却）
            logger.info(f"[SkGateway] 流结束 client_gone（渠道 {sk[:6]}... 连接重置）")
            raise _StreamBroken() from e
        except asyncio.TimeoutError as e:
            # sock_read 超时 = 90s 无新字节，上游半死挂住
            logger.warning(f"[SkGateway] 流结束 timeout（渠道 {sk[:6]}... 90s 无数据）")
            self._pool.mark_failure(sk, base_url, kind="transient")
            raise _StreamBroken() from e
        except Exception as e:
            # 上游流中断：响应头已发给客户端，无法换渠道重放，只能断开；
            # 记一次失败让连续中断的渠道自然进入冷却
            logger.warning(
                f"[SkGateway] 流结束 upstream_err（渠道 {sk[:6]}...）: {type(e).__name__}"
            )
            self._pool.mark_failure(sk, base_url, kind="transient")
            raise _StreamBroken() from e
        finally:
            await response.write_eof()
        logger.info(f"[SkGateway] 流结束 done（渠道 {sk[:6]}...）")
        self._pool.mark_success(sk, base_url)
        return response

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _check_auth(self, request: web.Request) -> bool:
        if not self._master_key:
            return False
        # 常量时间比较，避免按位泄露 master key；
        # compare_digest 只接受 ASCII str，统一 encode 防 TypeError
        received = request.headers.get("Authorization", "").encode("utf-8", "replace")
        expected = f"Bearer {self._master_key}".encode("utf-8", "replace")
        return hmac.compare_digest(received, expected)

    @staticmethod
    def _unauthorized() -> web.Response:
        return web.json_response(
            {"error": {"message": "未授权：请带 Authorization: Bearer <master_key>", "type": "unauthorized"}},
            status=401,
        )
