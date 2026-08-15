"""SK 聚合网关（轻量 9router/newapi 替代品，集成在插件进程内）。

把监控抓到的 API Key 聚合成 OpenAI 兼容网关：
  POST /v1/chat/completions  → 选一个可用渠道（base_url + key）原样转发，失败自动切换
  GET  /v1/models            → 静态模型名单（展示用；实际请求 model 透传，不做映射）
  GET  /healthz              → 存活检查（含池统计，无敏感信息）

设计取舍（保持简单）：
- 无数据库/前端/计费，只有「读池 + 转发」
- 懒验证：不主动联网验真（避免服务器 IP 被外部 key 后台记录），转发失败才踢渠道
- master key 鉴权：必须配置非空才启动，否则日志警告跳过
- 渠道选择：usable（最近最少用）→ unverified（先来先用），dead 排除
- 失败判定：401/403 密钥无效直接踢；429/5xx/网络错误计数，连续 2 次踢
"""

from __future__ import annotations

import asyncio
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

# 转发超时：非流式整体 180s；流式首包等待 90s（响应过程不再卡死总时长）
_TIMEOUT = ClientTimeout(total=180, sock_read=90)
# 转发失败时用于给上游的 UA（部分中转站按 UA 风控）
_UA = "astrbot-sk-gateway/1.0"


class _ChannelFailed(Exception):
    """当前渠道转发失败（换下一个渠道重试）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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
        # 渠道选择 + 状态更新串行化，避免并发请求重复选同一 key
        self._select_lock = asyncio.Lock()

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
            return web.json_response(
                {
                    "error": {
                        "message": "SK 池暂无可用渠道（key 池为空或全部失效）",
                        "type": "no_available_channel",
                    }
                },
                status=503,
            )

        # 串行选择：避免并发请求抢同一渠道；单个渠道失败立即换下一个
        async with self._select_lock:
            errors: list[str] = []
            for entry in usable:
                try:
                    return await self._forward(entry, body, stream, request)
                except _ChannelFailed as e:
                    errors.append(f"{entry['sk'][:10]}...@{(entry['base_url'] or '').split('//')[-1]}: {e.message}")
                    continue
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
            # trust_env 走系统代理（与 sk_verifier 同范式）
            self._session = ClientSession(timeout=_TIMEOUT, trust_env=True)
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
        try:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status in (401, 403):
                    self._pool.mark_failure(sk, base_url, fatal=True)
                    raise _ChannelFailed(f"上游鉴权失败 {resp.status}")
                if resp.status in (429, 500, 502, 503, 504):
                    self._pool.mark_failure(sk, base_url)
                    raise _ChannelFailed(f"上游错误 {resp.status}")
                if resp.status >= 400:
                    self._pool.mark_failure(sk, base_url)
                    raise _ChannelFailed(f"上游错误 {resp.status}")

                if stream:
                    return await self._forward_stream(resp, sk, base_url, request)
                data = await resp.json()
                self._pool.mark_success(sk, base_url)
                return web.json_response(data, status=resp.status)
        except _ChannelFailed:
            raise
        except Exception as e:
            # 网络/超时类失败：计数但不判死，下个请求可能恢复
            logger.debug(f"[SkGateway] 渠道 {sk[:10]}...@{(base_url or '').split('//')[-1]} 转发异常: {type(e).__name__}: {e}", exc_info=True)
            self._pool.mark_failure(sk, base_url)
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
        except (asyncio.CancelledError, ConnectionResetError):
            # 客户端断开/异常：不判渠道失败（key 本身可能没问题）
            raise
        finally:
            await response.write_eof()
        self._pool.mark_success(sk, base_url)
        return response

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _check_auth(self, request: web.Request) -> bool:
        if not self._master_key:
            return False
        return request.headers.get("Authorization", "") == f"Bearer {self._master_key}"

    @staticmethod
    def _unauthorized() -> web.Response:
        return web.json_response(
            {"error": {"message": "未授权：请带 Authorization: Bearer <master_key>", "type": "unauthorized"}},
            status=401,
        )
