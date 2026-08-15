"""SK 密钥池（落盘 JSON，SK 聚合网关的数据源）。

监控命中 API Key 时写入；网关从池子里选可用渠道转发。
设计原则：
- 原子写（tmp + rename），并发读安全
- 按 (sk, base_url) 去重，重复命中只刷新来源与时间
- 容量上限滚动淘汰最旧（default 200）
- 状态机：unverified（未用过）→ usable（转发成功过）→ dead（连续失败被踢）
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

# 连续失败多少次标记 dead（懒验证：不主动联网验真，转发失败才踢）
_DEAD_AFTER_FAILS = 2
# 网关默认 base_url 推断规则（sk- 前缀 → 官方端点）
_BASE_HINTS: tuple[tuple[str, str], ...] = (
    ("sk-ant-", "https://api.anthropic.com"),
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
        """转发成功：置 usable，清零失败计数。"""
        now = int(time.time())
        with self._lock:
            for entry in self._entries:
                if entry.get("sk") == sk and entry.get("base_url") == base_url:
                    entry["status"] = _STATUS_USABLE
                    entry["fail_count"] = 0
                    entry["last_used"] = now
                    self._persist()
                    return

    def mark_failure(
        self, sk: str, base_url: str, fatal: bool = False
    ) -> bool:
        """转发失败：计数 +1；fatal（401/403 密钥本身无效）直接标记 dead。返回是否已 dead。"""
        with self._lock:
            for entry in self._entries:
                if entry.get("sk") == sk and entry.get("base_url") == base_url:
                    if fatal:
                        entry["status"] = _STATUS_DEAD
                        logger.info(f"[SkPool] {sk[:12]}... 鉴权失败（401/403），标记失效")
                    else:
                        entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
                        if entry["fail_count"] >= _DEAD_AFTER_FAILS:
                            entry["status"] = _STATUS_DEAD
                            logger.info(
                                f"[SkPool] {sk[:12]}... 连续失败 {entry['fail_count']} 次，标记失效"
                            )
                    self._persist()
                    return entry["status"] == _STATUS_DEAD
        return False

    def next_candidates(self) -> list[dict[str, Any]]:
        """渠道选择顺序：usable（最近最少用）→ unverified（先来先用）→ dead 排除。"""
        with self._lock:
            usable = [
                e for e in self._entries if e.get("status") == _STATUS_USABLE
            ]
            unverified = [
                e for e in self._entries
                if e.get("status") == _STATUS_UNVERIFIED
            ]
        usable.sort(key=lambda e: e.get("last_used", 0))
        unverified.sort(key=lambda e: e.get("first_seen", 0))
        return usable + unverified

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
