"""
智能降噪层：冷却、去重、优先级分级、keyword 批量合并。

横切 keyword 和 window 两个模式，在推送前进行降噪过滤：

- 优先级分级：根据正则命中类别 + LLM 判定，将告警分为 critical / normal / low
- 冷却：同一发送者@同一群在冷却期内不重复推送（critical 豁免）
- 去重：内容指纹在去重窗口内只推一次（跨群去重）
- keyword 批量合并：normal 优先级不立即推，攒 N 秒合并成一条推送
"""

import asyncio
import hashlib
import re
import time

from ...infrastructure.config.config_manager import ConfigManager
from ...shared.timezone import now as _tz_now
from ...utils.logger import logger


class NoiseReducer:
    """智能降噪：冷却、去重、优先级分级、keyword 批量合并。"""

    PRIORITY_CRITICAL = "critical"  # API key/Token → 立即推
    PRIORITY_NORMAL = "normal"  # 资源链接/自定义关键词 → 批量合并
    PRIORITY_LOW = "low"  # LLM 判定边缘 → 只进简报

    # 内置正则 category → 优先级映射
    CATEGORY_PRIORITY: dict[str, str] = {
        "API Key": "critical",
        "资源链接": "normal",
        "渠道": "normal",
        "关键词": "normal",
    }

    def __init__(self, config_manager: ConfigManager):
        self._config = config_manager

        # 冷却：key = f"{sender_id}@{group_id}" → 上次推送时间戳
        self._cooldowns: dict[str, float] = {}

        # 去重：内容指纹 → 上次推送时间戳
        self._fingerprints: dict[str, float] = {}

        # keyword 批量合并：normal 优先级待推送队列
        self._pending_normal: list[dict] = []
        self._pending_lock = asyncio.Lock()
        self._batch_task: asyncio.Task | None = None
        self._stopping = False

        # 推送回调（由 MessageMonitorService 注入）
        self._send_callback = None

    # ============================================================
    # 优先级判定
    # ============================================================

    def classify_priority(
        self, hits: list[tuple[str, str]], llm_verdict: dict | None
    ) -> str:
        """根据正则命中类别 + LLM 判定，返回优先级。

        规则：
        1. 命中 API Key 类正则 → critical
        2. LLM 判定 useful=false 或 category=其他 → low
        3. 其他正则命中 → 该类别的默认优先级，默认 normal
        """
        # 如果有任何 critical 类别命中 → critical
        for category, _desc in hits:
            if self.CATEGORY_PRIORITY.get(category) == self.PRIORITY_CRITICAL:
                return self.PRIORITY_CRITICAL

        # LLM 判定结果
        if llm_verdict is not None:
            # LLM 明确判定无用 → low
            if not llm_verdict.get("useful", True):
                return self.PRIORITY_LOW
            # LLM 判定类别为"其他" → low
            llm_cat = llm_verdict.get("category", "")
            if llm_cat == "其他":
                return self.PRIORITY_LOW

        # 按命中类别的最高优先级
        best = self.PRIORITY_LOW
        for category, _desc in hits:
            p = self.CATEGORY_PRIORITY.get(category, self.PRIORITY_NORMAL)
            if p == self.PRIORITY_CRITICAL:
                return self.PRIORITY_CRITICAL
            if p == self.PRIORITY_NORMAL and best == self.PRIORITY_LOW:
                best = self.PRIORITY_NORMAL

        # 没有任何命中但有 llm_verdict（keyword 模式不应该走到这）
        if not hits and llm_verdict and llm_verdict.get("useful", False):
            return self.PRIORITY_NORMAL

        return best

    # ============================================================
    # 冷却检查
    # ============================================================

    def check_cooldown(self, sender_id: str, group_id: str) -> bool:
        """同一发送者@同一群是否在冷却期内。

        Returns:
            True = 在冷却期内，应跳过
            False = 不在冷却期，可以推送
        """
        cooldown_sec = self._config.get_cooldown_seconds()
        if cooldown_sec <= 0:
            return False

        key = f"{sender_id}@{group_id}"
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and (now - last) < cooldown_sec:
            logger.debug(
                f"[NoiseReducer] 冷却中：{key}，剩余 {cooldown_sec - (now - last):.0f}s"
            )
            return True

        # 不在冷却期，更新时间戳
        self._cooldowns[key] = now
        return False

    def mark_cooldown(self, sender_id: str, group_id: str) -> None:
        """手动标记冷却时间戳（用于推送成功后）。"""
        cooldown_sec = self._config.get_cooldown_seconds()
        if cooldown_sec > 0:
            self._cooldowns[f"{sender_id}@{group_id}"] = time.monotonic()

    # ============================================================
    # 内容去重
    # ============================================================

    def check_dedup(self, text: str) -> bool:
        """内容指纹是否在去重窗口内已推送过。

        Returns:
            True = 重复，应跳过
            False = 新内容，可以推送
        """
        dedup_min = self._config.get_dedup_minutes()
        if dedup_min <= 0:
            return False

        fp = self._fingerprint(text)
        now = time.monotonic()
        last = self._fingerprints.get(fp)
        if last is not None and (now - last) < (dedup_min * 60):
            logger.debug(f"[NoiseReducer] 去重命中：{fp[:16]}…")
            return True

        # 新内容，记录指纹
        self._fingerprints[fp] = now
        return False

    def mark_dedup(self, text: str) -> None:
        """手动标记内容指纹（用于推送成功后）。"""
        dedup_min = self._config.get_dedup_minutes()
        if dedup_min > 0:
            fp = self._fingerprint(text)
            self._fingerprints[fp] = time.monotonic()

    @staticmethod
    def _fingerprint(text: str) -> str:
        """内容指纹：归一化后取 sha256 前 32 字符。"""
        # 归一化：去空白、转小写、去标点
        normalized = re.sub(r"\s+", "", text).lower()
        normalized = re.sub(r"[^\w]", "", normalized)
        return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[
            :32
        ]

    # ============================================================
    # keyword 批量合并
    # ============================================================

    async def enqueue_normal(self, alert_data: dict) -> None:
        """normal 优先级入队，等 batch_timer 到期后合并推送。

        alert_data 结构：
        {
            "sender_id": str,
            "sender_name": str,
            "group_id": str,
            "text": str,
            "hits": list[tuple[str, str]],
            "llm_verdict": dict | None,
            "platform_id": str,
            "priority": str,
            "enqueued_at": float,  # time.monotonic()
        }
        """
        alert_data["enqueued_at"] = time.monotonic()
        async with self._pending_lock:
            self._pending_normal.append(alert_data)
        self.ensure_batch_task()

        batch_sec = self._config.get_keyword_batch_seconds()
        logger.debug(
            f"[NoiseReducer] normal 告警入队（当前队列 {len(self._pending_normal)}），"
            f"批量间隔 {batch_sec}s"
        )

    def ensure_batch_task(self) -> None:
        """确保批量推送后台任务在运行。"""
        if self._stopping:
            return
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_loop())

    async def _batch_loop(self) -> None:
        """每隔 keyword_batch_seconds 秒检查 pending 并合并推送。"""
        logger.info("[NoiseReducer] 批量合并任务已启动")
        while not self._stopping:
            try:
                batch_sec = self._config.get_keyword_batch_seconds()
                if batch_sec <= 0:
                    batch_sec = 60  # 安全兜底
                await asyncio.sleep(batch_sec)
                if self._stopping:
                    break
                await self._flush_pending_normals()
            except asyncio.CancelledError:
                logger.info("[NoiseReducer] 批量合并任务被取消")
                break
            except Exception as e:
                logger.error(
                    f"[NoiseReducer] 批量合并循环异常: {e}", exc_info=True
                )
                await asyncio.sleep(15)

    async def _flush_pending_normals(self) -> None:
        """取出所有 pending 的 normal 告警，合并为一条推送。"""
        async with self._pending_lock:
            if not self._pending_normal:
                return
            pending = list(self._pending_normal)
            self._pending_normal.clear()

        if not pending:
            return

        if self._send_callback is None:
            logger.warning("[NoiseReducer] 无推送回调，丢弃 pending 告警")
            return

        # 合并为一条推送
        alert = self._format_batch_alert(pending)
        platform_id = pending[0].get("platform_id", "")

        try:
            await self._send_callback(
                alert,
                context_desc=f"批量合并 {len(pending)} 条 normal 告警",
                platform_id=platform_id,
            )
        except Exception as e:
            logger.error(f"[NoiseReducer] 批量推送失败: {e}", exc_info=True)

    @staticmethod
    def _format_batch_alert(pending: list[dict]) -> str:
        """将多条 normal 告警合并为一条格式化文本。"""
        # 去重：同一发送者只保留最近一条
        seen_senders: dict[str, dict] = {}
        for item in pending:
            key = f"{item.get('sender_id', '')}@{item.get('group_id', '')}"
            seen_senders[key] = item  # 后出现的覆盖前面的

        deduped = list(seen_senders.values())

        lines = []
        lines.append(f"📋 关键词批量预警（合并 {len(deduped)} 条）")
        lines.append("━━━━━━━━━━━━━")

        for i, item in enumerate(deduped, 1):
            sender_name = item.get("sender_name", "未知")
            sender_id = item.get("sender_id", "")
            group_id = item.get("group_id", "")
            text = item.get("text", "")
            hits = item.get("hits", [])
            llm_verdict = item.get("llm_verdict")

            # 类别
            categories = sorted({c for c, _ in hits}) if hits else []
            category_str = "/".join(categories) if categories else "关键词"

            reason = ""
            if llm_verdict:
                reason = llm_verdict.get("reason", "")
                llm_cat = llm_verdict.get("category", "")
                if llm_cat and llm_cat != "其他":
                    category_str = llm_cat

            content_display = text if len(text) <= 300 else text[:300] + " …(截断)"

            lines.append(f"\n{i}. 👤 {sender_name}({sender_id}) @ 群{group_id}")
            lines.append(f"   🏷️ {category_str}")
            if reason:
                lines.append(f"   💡 {reason}")
            lines.append(f"   📝 {content_display}")

        lines.append(f"\n━━━━━━━━━━━━━")
        lines.append(f"⏰ {_tz_now().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    def get_pending_count(self) -> int:
        """当前待推送的 normal 告警数量。"""
        return len(self._pending_normal)

    # ============================================================
    # 推送回调注入
    # ============================================================

    def set_send_callback(self, callback) -> None:
        """注入推送回调（async def callback(alert, context_desc, platform_id)）。

        由 MessageMonitorService 在初始化后调用，将 _send_alert 方法传入。
        """
        self._send_callback = callback

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        """启动后台任务（如果 keyword_batch_seconds > 0 且 keyword 模式启用）。"""
        batch_sec = self._config.get_keyword_batch_seconds()
        if batch_sec > 0:
            self.ensure_batch_task()

    def stop(self) -> None:
        """停止后台任务。"""
        self._stopping = True
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()

    def cleanup(self) -> None:
        """清理过期的冷却记录和指纹（建议在 flush 时调用）。"""
        now = time.monotonic()
        cooldown_sec = self._config.get_cooldown_seconds()
        dedup_sec = self._config.get_dedup_minutes() * 60

        # 清理冷却记录
        if cooldown_sec > 0:
            expired = [
                k for k, v in self._cooldowns.items() if (now - v) > cooldown_sec
            ]
            for k in expired:
                del self._cooldowns[k]

        # 清理指纹记录
        if dedup_sec > 0:
            expired = [
                k for k, v in self._fingerprints.items() if (now - v) > dedup_sec
            ]
            for k in expired:
                del self._fingerprints[k]
