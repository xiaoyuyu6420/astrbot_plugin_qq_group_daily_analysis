"""
实时消息监控服务（定制版：盯人预警）

两种工作模式，由 message_monitor.monitor_mode 选择：

1. keyword（关键词即时模式）：
   不限发送者，监控群里【任何人】的消息命中关键词/正则 → 立即推送。
   可选用 LLM 二次确认（关掉就是纯规则即时推）。
   适合：盯全群的关键词（API key、资源、特定词），要快。

2. window（整窗汇总模式，默认）：
   攒群里所有人的消息 X 分钟，LLM 在完整上下文里提取目标 QQ 的价值信息。
   适合：盯特定 QQ，需要上下文消歧。

架构（不侵入定时日报链路）：
    keyword: 群消息 → 群在监控列表？→ 关键词命中 → (LLM确认?) → 立即推送
    window:  群消息 → 群在监控列表？→ 攒入缓冲 → flush(X分钟) → LLM 总结 → 推送
"""

import asyncio
import json
import re
import time
from datetime import datetime

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ...infrastructure.analysis.utils.llm_utils import (
    call_provider_with_retry,
    extract_response_text,
)
from ...infrastructure.config.config_manager import ConfigManager
from ...infrastructure.platform.bot_manager import BotManager
from ...utils.logger import logger

# ============================================================
# 关键词即时模式：内置正则规则
# ============================================================

# (pattern, category, description)
BUILTIN_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ── API key / Token 类 ──
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API Key", "疑似 OpenAI Key"),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}"), "API Key", "疑似 Google API Key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "API Key", "疑似 GitHub Token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "API Key", "疑似 Slack Token"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "API Key", "疑似 AWS Access Key"),
    (re.compile(r"[A-Fa-f0-9]{32,}"), "API Key", "疑似 hex 密钥串"),
    (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), "API Key", "疑似 base64 密钥串"),
    # ── 资源链接类 ──
    (re.compile(r"https?://\S{10,}"), "资源链接", "包含网址"),
    (re.compile(r"magnet:\?\S+", re.IGNORECASE), "资源链接", "磁力链接"),
    (re.compile(r"(?:提取码|访问码|密码)\s*[:：]\s*\S+", re.IGNORECASE), "资源链接", "网盘提取码"),
    (re.compile(r"(?:邀请码|邀请|邀请链接)\s*[:：]?\s*\S{4,}", re.IGNORECASE), "渠道", "邀请码"),
]

# 关键词模式：LLM 确认用的 system prompt
_KW_LLM_SYSTEM_PROMPT = (
    "你是一个信息价值判断助手。给你一条群聊消息，判断它是否含有"
    "「可行动的有价值信息」（如可用 API key、可下载资源、具体商机、"
    "一手情报、可直接照做的干货方法）。"
    "忽略闲聊、灌水、玩梗、情绪宣泄。宁缺毋滥。"
)

_KW_LLM_USER_TEMPLATE = """请判断以下群聊消息是否含有可行动的有价值信息。

发送者：{sender}（{sender_name}）
消息内容：
{content}

返回纯 JSON（不要 markdown 代码块），格式：
{{"useful": true/false, "reason": "一句话说明为什么有用/没用", "category": "apikey|资源|商机|情报|其他"}}
"""

# LLM system prompt
_LLM_SYSTEM_PROMPT = (
    "你是一个信息价值提取助手。给你一群人在一段时间内的群聊记录，"
    "请聚焦提取指定用户（会单独标注）发言中「可行动的有价值信息」"
    "（如可用 API key、可下载资源、具体商机、一手情报、可直接照做的干货方法）。"
    "利用上下文判断目标用户的发言是否有价值——"
    "他在回答谁的问题？分享的是什么？别人在讨论什么？"
    "忽略闲聊、灌水、玩梗、情绪宣泄。宁缺毋滥——"
    "如果目标用户这批消息确实没有有价值的信息，就返回 has_value=false。"
)

_LLM_USER_TEMPLATE = """以下是群 {group} 在过去约 {minutes} 分钟内的群聊记录（共 {total} 条，{shown} 条送审）。
请聚焦提取用户 [{target}] 发言中的有价值信息。其他人的发言作为上下文参考。

{keywords_hint}

群聊记录（格式：[序号] [用户ID]: 内容，>>> 标记的是目标用户）：
{messages_text}

---

请提取目标用户 [{target}] 发言中的有价值信息。返回纯 JSON（不要 markdown 代码块）：
{{"has_value": true/false, "items": [{{"content": "目标用户的有价值原文", "category": "apikey|资源|商机|情报|其他", "reason": "为什么有价值，可结合上下文"}}], "summary": "一句话概括，无价值时留空"}}
"""

_MAX_LLM_TEXT_CHARS = 4000
_MAX_CONTENT_DISPLAY = 500


class MessageMonitorService:
    """实时消息监控：整窗攒 X 分钟 → LLM 在完整上下文里提取目标 QQ 价值信息 → 推一条汇总"""

    def __init__(
        self,
        context: Context,
        config_manager: ConfigManager,
        bot_manager: BotManager,
    ):
        self.context = context
        self.config_manager = config_manager
        self.bot_manager = bot_manager

        # 缓冲区: {group_id: [ {"text", "time", "sender_id", "name", "platform_id"} ]}
        # 攒群里所有人的消息（不仅目标 QQ），让 LLM 有完整上下文
        self._buffer: dict[str, list[dict]] = {}
        self._buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._stopping = False

    async def process(self, event: AstrMessageEvent) -> None:
        """处理一条群消息。根据 monitor_mode 分流到关键词即时模式或整窗汇总模式。

        任何异常都吞掉，绝不影响 AstrBot 主流程。
        """
        try:
            if not self.config_manager.is_monitor_enabled():
                return

            sender_id = str(event.get_sender_id() or "").strip()
            group_id = str(event.get_group_id() or "").strip()
            if not sender_id or not group_id:
                return

            # 群不在监控群列表 → 跳过（两种模式共用）
            if not self._is_monitored_group(group_id):
                return

            text = self._extract_text(event)
            if not text or not text.strip():
                return

            # 按模式分流
            mode = self.config_manager.get_monitor_mode()
            if mode == "keyword":
                await self._process_keyword(event, sender_id, group_id, text)
            else:
                await self._process_window(event, sender_id, group_id, text)

        except Exception as e:
            logger.error(f"[Monitor] 消息处理异常: {e}", exc_info=True)

    # ============================================================
    # 关键词即时模式
    # ============================================================

    async def _process_keyword(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        group_id: str,
        text: str,
    ) -> None:
        """关键词即时模式：不限发送者，命中关键词/正则 → (LLM确认?) → 立即推送。"""
        text = text.strip()

        # 第一层：正则 + 自定义关键词预筛
        hits = self._regex_scan(text)
        if not hits:
            return  # 绝大多数消息在这里被丢弃

        sender_name = self._safe_sender_name(event, sender_id)
        platform_id = str(event.get_platform_id() or "").strip()

        # 第二层：LLM 确认（可选）
        if self.config_manager.is_llm_confirm_enabled():
            verdict = await self._llm_confirm_keyword(sender_id, sender_name, text)
            if verdict is not None and not verdict.get("useful", True):
                logger.debug(
                    f"[Monitor-KW] {sender_id}@{group_id} 命中但 LLM 判定无用，跳过"
                )
                return
        else:
            verdict = None

        # 立即推送
        await self._push_keyword_alert(
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id,
            text=text,
            hits=hits,
            llm_verdict=verdict,
            platform_id=platform_id,
        )

    def _regex_scan(self, text: str) -> list[tuple[str, str]]:
        """正则 + 自定义关键词扫描。返回命中的 (category, description) 列表。"""
        hits: list[tuple[str, str]] = []
        for pattern, category, desc in BUILTIN_PATTERNS:
            if pattern.search(text):
                hits.append((category, desc))
        for kw in self.config_manager.get_monitor_extra_keywords():
            kw = str(kw).strip()
            if kw and kw in text:
                hits.append(("关键词", f"命中自定义关键词「{kw}」"))
        return hits

    async def _llm_confirm_keyword(
        self, sender_id: str, sender_name: str, text: str
    ) -> dict | None:
        """关键词模式：调 LLM 判断单条消息是否有价值。不可用时返回 None（降级为命中即推）。"""
        try:
            prompt = _KW_LLM_USER_TEMPLATE.format(
                sender=sender_id, sender_name=sender_name, content=text[:2000]
            )
            resp = await call_provider_with_retry(
                context=self.context,
                config_manager=self.config_manager,
                prompt=prompt,
                system_prompt=_KW_LLM_SYSTEM_PROMPT,
            )
            if resp is None:
                return None
            raw = extract_response_text(resp).strip()
            return self._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[Monitor-KW] LLM 确认失败，降级为命中即推: {e}")
            return None

    async def _push_keyword_alert(
        self,
        sender_id: str,
        sender_name: str,
        group_id: str,
        text: str,
        hits: list[tuple[str, str]],
        llm_verdict: dict | None,
        platform_id: str,
    ) -> None:
        """关键词模式：格式化并即时推送。"""
        categories = sorted({c for c, _ in hits})
        category_str = "/".join(categories) if categories else "未知"

        if llm_verdict:
            reason = llm_verdict.get("reason", "")
            llm_category = llm_verdict.get("category", "")
            if llm_category and llm_category != "其他":
                category_str = llm_category
        else:
            reason = "正则/关键词命中" + (
                "（LLM 未启用）" if not self.config_manager.is_llm_confirm_enabled() else "（LLM 降级）"
            )

        hit_details = "、".join(desc for _, desc in hits[:3])
        content_display = text if len(text) <= 800 else text[:800] + " …(截断)"

        alert = (
            f"🚨 关键词命中预警\n"
            f"━━━━━━━━━━━━━\n"
            f"👤 来源：{sender_name} ({sender_id})\n"
            f"📍 群：{group_id}\n"
            f"🏷️ 类别：{category_str}\n"
            f"💡 判断：{reason or hit_details}\n"
            f"\n"
            f"📝 原文：\n{content_display}\n"
            f"\n"
            f"━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        await self._send_alert(
            alert,
            context_desc=f"{sender_name}({sender_id})@{group_id}",
            platform_id=platform_id,
        )

    # ============================================================
    # 整窗汇总模式
    # ============================================================

    async def _process_window(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        group_id: str,
        text: str,
    ) -> None:
        """整窗汇总模式：不过滤发送者，所有人的消息都攒入缓冲区。"""
        sender_name = self._safe_sender_name(event, sender_id)
        platform_id = str(event.get_platform_id() or "").strip()

        async with self._buffer_lock:
            self._buffer.setdefault(group_id, []).append(
                {
                    "text": text.strip(),
                    "time": time.monotonic(),
                    "sender_id": sender_id,
                    "name": sender_name,
                    "platform_id": platform_id,
                }
            )

        self._ensure_flush_task()

    # ============================================================
    # 后台 flush 任务
    # ============================================================

    def _ensure_flush_task(self) -> None:
        if self._stopping:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """每 flush_interval 分钟醒来一次，逐群批量总结推送。"""
        logger.info("[Monitor] 后台 flush 任务已启动")
        while not self._stopping:
            try:
                interval = self.config_manager.get_flush_interval() * 60
                if interval < 10:
                    interval = 10
                await asyncio.sleep(interval)
                if self._stopping:
                    break
                await self._flush_all()
            except asyncio.CancelledError:
                logger.info("[Monitor] flush 任务被取消")
                break
            except Exception as e:
                logger.error(f"[Monitor] flush 循环异常: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def _flush_all(self) -> None:
        """快照所有群缓冲区并清空，逐群处理。"""
        async with self._buffer_lock:
            if not self._buffer:
                return
            batches = {gid: list(msgs) for gid, msgs in self._buffer.items() if msgs}
            self._buffer.clear()

        if not batches:
            return

        logger.info(f"[Monitor] flush 触发：{len(batches)} 个群有待处理窗口")
        for group_id, messages in batches.items():
            try:
                await self._flush_group(group_id, messages)
            except Exception as e:
                logger.error(
                    f"[Monitor] flush 群 {group_id} 异常: {e}", exc_info=True
                )

    async def _flush_group(
        self, group_id: str, messages: list[dict]
    ) -> None:
        """对一个群的一个窗口做：截断 → 目标 QQ 出现？→ LLM 总结 → 推送。"""
        if not messages:
            return

        watched_qqs = set(self.config_manager.get_monitored_qqs())
        if not watched_qqs:
            return  # 没配监控对象

        # 该窗口内是否有目标 QQ 发言？没有就跳过（省 LLM 调用）
        target_senders_in_window = {
            m["sender_id"] for m in messages if m["sender_id"] in watched_qqs
        }
        if not target_senders_in_window:
            return

        platform_id = messages[-1].get("platform_id", "")
        max_ctx = self.config_manager.get_max_context_messages()

        # 按条数截断：如果超出上限，只保留目标 QQ 发言附近的窗口
        window, total_count = self._truncate_window(messages, watched_qqs, max_ctx)

        # 逐个目标 QQ 提取（通常只有一个，但支持多个）
        for target_qq in target_senders_in_window:
            try:
                await self._extract_and_push(
                    group_id=group_id,
                    target_qq=target_qq,
                    window=window,
                    total_count=total_count,
                    shown_count=len(window),
                    platform_id=platform_id,
                )
            except Exception as e:
                logger.error(
                    f"[Monitor] 提取 {target_qq}@{group_id} 异常: {e}",
                    exc_info=True,
                )

    # ============================================================
    # 窗口截断
    # ============================================================

    @staticmethod
    def _truncate_window(
        messages: list[dict],
        target_qqs: set[str],
        max_messages: int,
    ) -> tuple[list[dict], int]:
        """按条数截断。

        - 消息数 <= max_messages：全保留
        - 超出：以目标 QQ 发言为中心，向前向后扩展，尽量覆盖上下文

        Returns:
            (截断后的窗口, 原始总条数)
        """
        total = len(messages)
        if total <= max_messages:
            return messages, total

        # 找到所有目标 QQ 发言的索引
        target_indices = [
            i for i, m in enumerate(messages) if m["sender_id"] in target_qqs
        ]
        if not target_indices:
            return messages[:max_messages], total

        # 以目标发言为中心，向外扩展窗口
        center = target_indices[len(target_indices) // 2]
        half = max_messages // 2
        start = max(0, center - half)
        end = min(total, start + max_messages)
        # 如果右侧不够，向左补
        if end - start < max_messages:
            start = max(0, end - max_messages)

        window = messages[start:end]
        return window, total

    # ============================================================
    # LLM 提取 + 推送
    # ============================================================

    async def _extract_and_push(
        self,
        group_id: str,
        target_qq: str,
        window: list[dict],
        total_count: int,
        shown_count: int,
        platform_id: str,
    ) -> None:
        """构建对话文本 → LLM 提取 → 推送。"""
        # 找目标用户的展示名
        target_name = target_qq
        for m in window:
            if m["sender_id"] == target_qq:
                target_name = m.get("name", target_qq)
                break

        # 构建对话文本（标注发送者，目标 QQ 用 >>> 标记）
        lines = []
        total_len = 0
        for i, msg in enumerate(window, 1):
            is_target = msg["sender_id"] == target_qq
            marker = ">>> " if is_target else "    "
            line = f"{marker}[{i}] [{msg['sender_id']}]: {msg['text']}"
            if total_len + len(line) > _MAX_LLM_TEXT_CHARS:
                lines.append(f"    …(后续已截断)")
                break
            lines.append(line)
            total_len += len(line)
        dialog_text = "\n".join(lines)

        # LLM 提取
        verdict = None
        if self.config_manager.is_llm_confirm_enabled():
            verdict = await self._llm_extract(
                target_qq, group_id, total_count, shown_count, dialog_text
            )

        # 决定是否推送
        if verdict is not None:
            if not verdict.get("has_value", False):
                logger.info(
                    f"[Monitor] {target_qq}@{group_id} 窗口({shown_count}条) "
                    f"LLM 判定无价值，跳过"
                )
                return
        else:
            # LLM 不可用 → 降级：直接推目标用户原始发言
            logger.warning(
                f"[Monitor] {target_qq}@{group_id} LLM 不可用，降级推送原始发言"
            )
            verdict = self._fallback_verdict(target_qq, target_name, window)

        await self._push_summary(
            target_qq=target_qq,
            target_name=target_name,
            group_id=group_id,
            platform_id=platform_id,
            total_count=total_count,
            shown_count=shown_count,
            verdict=verdict,
        )

    async def _llm_extract(
        self,
        target_qq: str,
        group_id: str,
        total_count: int,
        shown_count: int,
        dialog_text: str,
    ) -> dict | None:
        """调用 LLM 在完整上下文中提取目标用户的价值信息。"""
        try:
            keywords = self.config_manager.get_monitor_extra_keywords()
            keywords_hint = ""
            if keywords:
                keywords_hint = f"特别关注这些关键词：{', '.join(keywords)}"

            minutes = self.config_manager.get_flush_interval()
            prompt = _LLM_USER_TEMPLATE.format(
                group=group_id,
                minutes=minutes,
                total=total_count,
                shown=shown_count,
                target=target_qq,
                keywords_hint=keywords_hint,
                messages_text=dialog_text,
            )

            resp = await call_provider_with_retry(
                context=self.context,
                config_manager=self.config_manager,
                prompt=prompt,
                system_prompt=_LLM_SYSTEM_PROMPT,
            )
            if resp is None:
                return None
            raw = extract_response_text(resp).strip()
            return self._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[Monitor] LLM 提取失败: {e}")
            return None

    @staticmethod
    def _parse_llm_json(raw: str) -> dict | None:
        """宽松解析 LLM 返回的 JSON。"""
        if not raw:
            return None
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _fallback_verdict(
        target_qq: str, target_name: str, window: list[dict]
    ) -> dict:
        """LLM 不可用时的降级：提取目标用户原始发言直推。"""
        items = []
        for msg in window:
            if msg["sender_id"] != target_qq:
                continue
            text = msg["text"]
            if len(text) > _MAX_CONTENT_DISPLAY:
                text = text[:_MAX_CONTENT_DISPLAY] + "…"
            items.append({"content": text, "category": "未确认", "reason": ""})
        return {
            "has_value": True,
            "items": items,
            "summary": f"LLM 不可用，原始推送 {target_name} 的 {len(items)} 条发言（降级模式）",
        }

    # ============================================================
    # 推送
    # ============================================================

    def _get_alert_targets(self) -> list[str]:
        targets = self.config_manager.get_alert_admin_qqs()
        if targets:
            return targets
        return self._get_admin_qqs_fallback()

    def _get_admin_qqs_fallback(self) -> list[str]:
        qqs: list[str] = []
        try:
            context = getattr(self.bot_manager, "_context", None)
            if context is not None:
                get_config = getattr(context, "get_config", None)
                if callable(get_config):
                    global_config = get_config()
                    admins_id = (
                        global_config.get("admins_id", [])
                        if isinstance(global_config, dict)
                        else []
                    )
                    if isinstance(admins_id, list):
                        qqs.extend(str(x) for x in admins_id)
        except Exception as e:
            logger.warning(f"[Monitor] 读取 AstrBot 超管配置失败: {e}")
        qqs.extend(self.config_manager.get_extra_admin_qqs())

        seen: set[str] = set()
        result: list[str] = []
        for q in qqs:
            q_clean = str(q).strip()
            if q_clean.isdigit() and q_clean not in seen:
                seen.add(q_clean)
                result.append(q_clean)
        return result

    async def _send_alert(
        self,
        alert: str,
        context_desc: str = "",
        platform_id: str = "",
    ) -> None:
        """公共推送方法：格式化好的 alert 文本私聊发给所有目标。

        Args:
            alert: 完整的预警文本
            context_desc: 日志用的上下文描述（如 "张三@群A，2条有价值"）
            platform_id: 平台 ID（用于获取 adapter）
        """
        targets = self._get_alert_targets()
        if not targets:
            logger.warning(f"[Monitor] 无推送目标（请配置 alert_admin_qqs 或 admins_id）")
            return

        adapter = self.bot_manager.get_adapter(platform_id) if platform_id else None
        if not adapter:
            # 兜底：遍历所有 adapter 找一个支持 send_private 的
            for pid_attempt in self._get_all_platform_ids():
                a = self.bot_manager.get_adapter(pid_attempt)
                if a and hasattr(a, "send_private"):
                    adapter = a
                    break
        if not adapter:
            logger.error("[Monitor] 无法获取支持私聊的 adapter（非 OneBot?）")
            return
        if not hasattr(adapter, "send_private"):
            logger.error("[Monitor] 当前平台 adapter 不支持私聊发送（非 OneBot?）")
            return

        success = 0
        for qq in targets:
            try:
                ok = await adapter.send_private(user_id=qq, text=alert)
                if ok:
                    success += 1
                    extra = f"（{context_desc}）" if context_desc else ""
                    logger.info(f"[Monitor] 已推送给 {qq}{extra}")
                else:
                    logger.warning(f"[Monitor] 推送 {qq} 失败（可能是非好友）")
            except Exception as e:
                logger.error(f"[Monitor] 推送 {qq} 异常: {e}")

        logger.info(f"[Monitor] 推送完成：成功 {success}/{len(targets)}")

    def _get_all_platform_ids(self) -> list[str]:
        """获取所有已注册的平台 ID（兜底用）。"""
        try:
            # bot_manager 通常有 get_all_platforms 或类似方法
            if hasattr(self.bot_manager, "get_all_platforms"):
                platforms = self.bot_manager.get_all_platforms()
                return [str(p) for p in platforms]
        except Exception:
            pass
        return []

    async def _push_summary(
        self,
        target_qq: str,
        target_name: str,
        group_id: str,
        platform_id: str,
        total_count: int,
        shown_count: int,
        verdict: dict,
    ) -> None:
        """格式化汇总预警并私聊推送。"""
        items = verdict.get("items", [])
        summary = verdict.get("summary", "")
        valuable_count = len(items)
        interval_min = self.config_manager.get_flush_interval()

        item_lines = []
        for i, item in enumerate(items, 1):
            content = str(item.get("content", "")).strip()
            category = str(item.get("category", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if len(content) > _MAX_CONTENT_DISPLAY:
                content = content[:_MAX_CONTENT_DISPLAY] + "…"
            line = f"{i}."
            if category and category != "其他":
                line += f" [{category}]"
            line += f' "{content}"'
            if reason:
                line += f"\n   💡 {reason}"
            item_lines.append(line)

        items_section = "\n".join(item_lines) if item_lines else "（未提取到具体条目）"

        alert = (
            f"🚨 监控预警（近 {interval_min} 分钟汇总）\n"
            f"━━━━━━━━━━━━━\n"
            f"👤 目标：{target_name} ({target_qq})\n"
            f"📍 群：{group_id}\n"
            f"📊 窗口 {total_count} 条对话（送审 {shown_count} 条）"
            f"，筛出 {valuable_count} 条有价值\n"
            f"\n"
            f"{items_section}\n"
        )
        if summary:
            alert += f"\n💡 概括：{summary}\n"
        alert += (
            f"\n━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        targets = self._get_alert_targets()
        if not targets:
            logger.warning(
                f"[Monitor] 有值但无推送目标：{target_qq}@{group_id}"
            )
            return

        await self._send_alert(
            alert,
            context_desc=f"{target_name}@{group_id}，{valuable_count}条有价值",
            platform_id=platform_id,
        )

    # ============================================================
    # 过滤
    # ============================================================

    def _is_monitored_group(self, group_id: str) -> bool:
        """该群是否在监控范围内（monitored_groups 空=不盯任何群）。"""
        watched_groups = self.config_manager.get_monitored_groups()
        if not watched_groups:
            # 没配群列表 → 不启动监控（安全默认，避免全群监听）
            return False
        return group_id in watched_groups

    def _extract_text(self, event: AstrMessageEvent) -> str:
        """提取消息纯文本。优先 message_str，兜底遍历消息段。"""
        text = (getattr(event, "message_str", "") or "").strip()
        if text:
            return text
        parts: list[str] = []
        message_obj = getattr(event, "message_obj", None)
        message = getattr(message_obj, "message", None) if message_obj else None
        if message:
            for seg in message:
                seg_type = getattr(seg, "type", "")
                if seg_type in ("Plain", "text"):
                    t = getattr(seg, "text", None) or (
                        seg.data.get("text") if hasattr(seg, "data") else None
                    )
                    if t:
                        parts.append(str(t))
        return " ".join(parts).strip()

    # ============================================================
    # 生命周期
    # ============================================================

    def stop(self) -> None:
        self._stopping = True
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def _safe_sender_name(event: AstrMessageEvent, sender_id: str) -> str:
        for getter in (
            lambda: event.get_sender_name(),
            lambda: getattr(
                getattr(event, "message_obj", None), "sender", None
            ).nickname
            if getattr(getattr(event, "message_obj", None), "sender", None)
            else None,
        ):
            try:
                name = getter()
                name = str(name or "").strip()
                if name and name.lower() not in {"unknown", "none", "null"}:
                    return name
            except Exception:
                continue
        return sender_id
