"""SK 密钥池（落盘 JSON，SK 聚合网关的数据源）。

监控命中 API Key 时写入；网关从池子里选可用渠道转发。
设计原则：
- 原子写（tmp + rename），并发读安全
- 按 (sk, base_url) 去重，重复命中只刷新来源与时间
- 容量上限滚动淘汰最旧（default 200）
- 状态机（冷却制，借鉴 9router/new-api：失败只定时拉黑，不永久踢死）：
    unverified（未用过）→ usable（转发成功过）→ dead（冷却中，到期自动回 unverified）
  冷却时长按失败类型分级：鉴权失败长冷却、限流指数退避、瞬态错误短冷却。
  转发成功即完全复活（清零计数与冷却）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

from ...utils.logger import logger

_STATUS_UNVERIFIED = "unverified"
_STATUS_USABLE = "usable"
_STATUS_DEAD = "dead"

# 失败类型 → 冷却策略（秒）。401/403 多为临时风控/中转站抽风而非密钥真死，
# 长冷却而非永久踢，给"重启才能复活"的池子留自愈路径。
_COOLDOWN_AUTH = 30 * 60  # 401/403：30 分钟
_COOLDOWN_TRANSIENT = 30  # 5xx/网络/超时：30 秒
_COOLDOWN_RATE_BASE = 30  # 429：指数退避 30s→封顶 5min（key 活着只是限流）
_COOLDOWN_RATE_MAX = 5 * 60
# 网关默认 base_url 推断规则（sk- 前缀 → 官方端点）
# 注意：不推断 sk-ant-（Anthropic 官方是 /v1/messages 协议，不是 OpenAI 兼容的
# /v1/chat/completions，网关转发必败还白白把 key 踢成 dead）。
# sk-ant- 只能等 LLM 提取到 OpenAI 兼容中转站地址时才入渠道。
_BASE_HINTS: tuple[tuple[str, str], ...] = (
    ("sk-proj-", "https://api.openai.com"),
)


def _infer_base_url(sk: str) -> str:
    """按 sk 前缀推断官方端点；返回空串表示无法推断（等 LLM 提取的中转站地址）。"""
    for prefix, base in _BASE_HINTS:
        if sk.startswith(prefix):
            return base
    return ""


class SkPool:
    """SK 池：内存缓存 + JSON 落盘。"""

    def __init__(self, path: str, max_size: int = 200):
        self.path = path
        self.max_size = max(1, int(max_size))
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._entries = data
        except Exception as e:
            logger.warning(f"[SkPool] 加载失败，按空池启动: {e}")
            self._entries = []

    def _persist(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(self.path) or ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._entries, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception as e:
            logger.warning(f"[SkPool] 落盘失败: {e}")

    # ------------------------------------------------------------------
    # 写入 / 更新
    # ------------------------------------------------------------------

    def add(
        self,
        sk: str,
        base_url: str = "",
        source_group: str = "",
        source_user: str = "",
    ) -> bool:
        """新增/刷新一条 key。返回是否新增（False = 已存在仅刷新）。"""
        sk = (sk or "").strip()
        if not sk:
            return False
        base_url = (base_url or _infer_base_url(sk)).strip().rstrip("/")
        now = int(time.time())

        with self._lock:
            for entry in self._entries:
                if entry.get("sk") == sk and entry.get("base_url") == base_url:
                    entry["first_seen"] = entry.get("first_seen", now)
                    if source_group:
                        entry["source_group"] = source_group
                    if source_user:
                        entry["source_user"] = source_user
                    self._persist()
                    return False

            self._entries.append(
                {
                    "sk": sk,
                    "base_url": base_url,
                    "status": _STATUS_UNVERIFIED,
                    "fail_count": 0,
                    "cooldown_until": 0,
                    "first_seen": now,
                    "last_used": 0,
                    "source_group": source_group,
                    "source_user": source_user,
                }
            )
            # 容量滚动：超出淘汰最旧（first_seen 最早的）
            if len(self._entries) > self.max_size:
                self._entries.sort(key=lambda e: e.get("first_seen", 0))
                del self._entries[: len(self._entries) - self.max_size]
            self._persist()
            return True

    def mark_success(self, sk: str, base_url: str) -> None:
        """转发成功：置 usable，清零失败计数与冷却（成功即完全复活）。"""
        now = int(time.time())
        with self._lock:
            for entry in self._entries:
                if entry.get("sk") == sk and entry.get("base_url") == base_url:
                    entry["status"] = _STATUS_USABLE
                    entry["fail_count"] = 0
                    entry["cooldown_until"] = 0
                    entry["last_used"] = now
                    self._persist()
                    return

    def mark_failure(self, sk: str, base_url: str, kind: str = "transient") -> None:
        """转发失败：按失败类型进冷却（dead = 冷却中，到期自动复活）。

        kind: "auth"（401/403）| "rate_limit"（429）| "transient"（5xx/网络/超时）
        """
        with self._lock:
            for entry in self._entries:
                if entry.get("sk") == sk and entry.get("base_url") == base_url:
                    fails = int(entry.get("fail_count", 0)) + 1
                    entry["fail_count"] = fails
                    if kind == "auth":
                        cooldown = _COOLDOWN_AUTH
                        reason = "鉴权失败(401/403)"
                    elif kind == "rate_limit":
                        cooldown = min(
                            _COOLDOWN_RATE_BASE * (2 ** (fails - 1)),
                            _COOLDOWN_RATE_MAX,
                        )
                        reason = f"限流(429)第{fails}次"
                    else:
                        cooldown = _COOLDOWN_TRANSIENT
                        reason = "瞬态错误"
                    entry["status"] = _STATUS_DEAD
                    entry["cooldown_until"] = int(time.time()) + cooldown
                    logger.info(
                        f"[SkPool] {sk[:12]}... {reason}，冷却 {cooldown}s 后自动复活"
                    )
                    self._persist()
                    return

    def next_candidates(self) -> list[dict[str, Any]]:
        """渠道选择顺序：usable（最近最少用）→ unverified（先来先用）。

        冷却到期的 dead 渠道惰性复活为 unverified（只改内存不落盘——
        重启后 cooldown_until 已过期，同样会在此复活，无损失）。
        复活不清零 fail_count：持续 429 的渠道退避时长要继续增长，
        只有转发成功（mark_success）才完全清零。
        """
        now = int(time.time())
        with self._lock:
            usable = []
            unverified = []
            for e in self._entries:
                status = e.get("status")
                if status == _STATUS_DEAD and int(e.get("cooldown_until", 0)) <= now:
                    e["status"] = _STATUS_UNVERIFIED
                    status = _STATUS_UNVERIFIED
                if status == _STATUS_USABLE:
                    usable.append(e)
                elif status == _STATUS_UNVERIFIED:
                    unverified.append(e)
        usable.sort(key=lambda e: e.get("last_used", 0))
        unverified.sort(key=lambda e: e.get("first_seen", 0))
        return usable + unverified

    def earliest_revival(self) -> int:
        """全池冷却时，最早复活的 Unix 时间戳（0 = 无冷却渠道）。用于 503 Retry-After。"""
        now = int(time.time())
        with self._lock:
            pending = [
                int(e.get("cooldown_until", 0))
                for e in self._entries
                if e.get("status") == _STATUS_DEAD
                and int(e.get("cooldown_until", 0)) > now
            ]
        return min(pending) if pending else 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": len(self._entries),
                "usable": sum(1 for e in self._entries if e.get("status") == _STATUS_USABLE),
                "unverified": sum(
                    1 for e in self._entries if e.get("status") == _STATUS_UNVERIFIED
                ),
                "dead": sum(1 for e in self._entries if e.get("status") == _STATUS_DEAD),
            }

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)
