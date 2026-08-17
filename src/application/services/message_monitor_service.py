"""
实时消息监控服务（定制版：盯人预警 + 跨群聚合 + 智能降噪）

两种工作模式，由 message_monitor.monitor_mode 选择：

1. keyword（关键词即时模式）：
   不限发送者，监控群里【任何人】的消息命中关键词/正则 → 降噪 → 推送。
   可选用 LLM 二次确认（关掉就是纯规则即时推）。
   适合：盯全群的关键词（API key、资源、特定词），要快。

2. window（整窗汇总模式，默认）：
   攒群里所有人的消息 X 分钟，LLM 在完整上下文里提取目标 QQ 的价值信息。
   可开启跨群聚合，输出统一简报。
   适合：盯特定 QQ，需要上下文消歧。

降噪层（横切两个模式）：
    优先级分级：critical(立即) / normal(批量合并) / low(只进简报)
    冷却：同一发送者@同一群在冷却期内不重复推送
    去重：内容指纹在去重窗口内只推一次
    keyword 批量合并：normal 优先级攒 N 秒合并推送

跨群聚合（window 模式可选）：
    enable_cross_group=true 时，flush 合并所有监控群消息 → LLM 按话题聚类 → 统一简报
    enable_cross_group=false 时，逐群独立推送（原行为）

架构（不侵入定时日报链路）：
    keyword: 命中 → 优先级分级 → 冷却/去重 → critical立即 / normal批量 / low丢弃
    window:  攒消息 → flush → 跨群聚合(可选) → 统一简报 / 逐群汇总
"""

import asyncio
import json
import re
import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ...infrastructure.analysis.utils.llm_utils import (
    call_provider_with_retry,
    extract_response_text,
)
from ...infrastructure.config.config_manager import ConfigManager
from ...infrastructure.platform.bot_manager import BotManager
from ...infrastructure.utils.admin_resolver import resolve_admin_qqs
from ...shared.timezone import now as _tz_now
from ...utils.logger import logger
from .noise_reducer import NoiseReducer

# ============================================================
# 关键词即时模式：内置正则规则
# ============================================================

# (pattern, category, description) —— 标准前缀 key（正则即高置信，critical 秒推）
BUILTIN_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ── API key / Token 类 ──
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API Key", "疑似 OpenAI Key"),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}"), "API Key", "疑似 Google API Key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "API Key", "疑似 GitHub Token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "API Key", "疑似 Slack Token"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "API Key", "疑似 AWS Access Key"),
    # ── 资源链接类（收紧：keyword 模式不命中裸网址，避免普通分享刷屏）──
    (re.compile(r"magnet:\?\S+", re.IGNORECASE), "资源链接", "磁力链接"),
    (re.compile(r"(?:提取码|访问码|密码)\s*[:：]\s*\S+", re.IGNORECASE), "资源链接", "网盘提取码"),
    (re.compile(r"(?:邀请码|邀请|邀请链接)\s*[:：]?\s*\S{4,}", re.IGNORECASE), "渠道", "邀请码"),
]

# ── 裸密钥形态（低置信：仅凭"像随机串"不能确认，必须过 LLM 确认才推送）──
# 要求独立 token：前后不是字母数字（hex）/ 不是字母数字+/=.（base64，
# 排除 URL 的路径与参数段、单词中段）。
_FUZZY_HEX_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32,}(?![0-9A-Za-z])")
_FUZZY_B64_RE = re.compile(r"(?<![0-9A-Za-z+/=.])[A-Za-z0-9+/]{40,}={0,2}(?![0-9A-Za-z+/=])")


def _fuzzy_key_hits(text: str) -> list[tuple[str, str]]:
    """裸 hex/base64 密钥形态扫描（误伤重灾区，规则收紧）。

    排除项（都是群聊常见内容，不是 key）：
    - 纯数字串：订单号/号码连拼/验证码轰炸
    - 磁力链 info hash（btih）：由"资源链接"类负责
    - base64 但非大小写+数字混合：纯字母刷屏、单词串、标语
    - URL 路径/参数段：由 lookbehind 排除（前面是 / . = ? 等）
    命中仅代表形态像 → "疑似密钥"类（normal），必须过 LLM 确认。
    """
    hits: list[tuple[str, str]] = []
    low = text.lower()
    for m in _FUZZY_HEX_RE.finditer(text):
        tok = m.group(0)
        if tok.isdigit():
            continue
        if "btih" in low[max(0, m.start() - 10) : m.start() + 4]:
            continue
        hits.append(("疑似密钥", "疑似裸 hex 密钥串"))
        break
    for m in _FUZZY_B64_RE.finditer(text):
        tok = m.group(0)
        if not (
            any(c.isdigit() for c in tok)
            and any(c.islower() for c in tok)
            and any(c.isupper() for c in tok)
        ):
            continue
        hits.append(("疑似密钥", "疑似裸 base64 密钥串"))
        break
    return hits

# 裸网址：仅 window/cross_group（整窗摘要）模式命中。
# keyword 模式不匹配它——普通分享（GitHub/官网/镜像地址）会刷屏且每条约 1 次 LLM 确认。
_URL_PATTERN = re.compile(r"https?://\S{10,}")

# 关键词模式：LLM 确认用的 system prompt
_KW_LLM_SYSTEM_PROMPT = (
    "你是一个信息价值判断助手。给你一条群聊消息，判断它是否含有"
    "「可行动的有价值信息」（如可用 API key、可下载资源、具体商机、"
    "一手情报、可直接照做的干货方法）。"
    "忽略闲聊、灌水、玩梗、情绪宣泄。宁缺毋滥。\n"
    "密钥/token 判定标准（从严，拿不准就判 false）：\n"
    "- 是真 key：独立成段的随机字符串（通常 32 位以上、字母数字大小写混杂），"
    "且上下文有分享意图（出现 key/密钥/token/api/中转/白嫖/额度/分享/接码 等词，"
    "或明确说'给大家用的'）\n"
    "- 不是 key（判 useful=false）：文件校验值（上下文是 sha256/md5/校验/截图/种子），"
    "磁力链接 hash，长数字串（订单号/QQ号/手机号/验证码），URL 及其参数段，"
    "代码片段里的变量名或哈希，版本号，图片/文件 base64 数据，加密货币地址的普通转账"
)

# sk 密钥专项分析 prompt（命中 API Key 正则后调用，分析平台/来源/可信度）
_SK_ANALYSIS_SYSTEM_PROMPT = (
    "你是一个 API 密钥分析助手。给你一条含有 sk- 开头密钥的群聊消息，"
    "分析这个密钥的来源和可信度。"
)

_SK_ANALYSIS_USER_TEMPLATE = """分析以下群聊消息中的 API 密钥：

发送者：{sender}（{sender_name}）
消息内容：
{content}

请分析并返回纯 JSON（不要 markdown 代码块）：
{{"platform": "OpenAI官方/Claude/第三方中转站/其他/不确定", "source_url": "消息里附带的网址（如中转站地址），没有则留空", "confidence": "high/medium/low", "note": "一句话判断：是否像真实可用密钥，还是测试/玩笑/已失效"}}

判断依据：
- OpenAI 官方密钥：sk- 后接 48 位左右字母数字（如 sk-proj-xxx）
- Claude 密钥：通常 sk-ant- 开头
- 第三方中转站：消息里通常附带中转站网址，sk 格式可能不标准
- 低可信度：明显是示例/测试值（如 sk-test123）、玩梗、无上下文
"""

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

# ============================================================
# 跨群聚合模式：LLM prompt
# ============================================================

_CROSS_GROUP_SYSTEM_PROMPT = (
    "你是一个跨群信息聚合助手。给你多个群在同一时间段内的聊天记录，"
    "请按「话题」聚类提取所有有价值信息，合并不同群中的相同话题，"
    "标注每个话题涉及的来源群。"
    "聚焦「可行动的有价值信息」（API key、资源、商机、情报、干货方法）。"
    "忽略闲聊灌水。如果确实没有有价值信息，返回 has_value=false。"
)

_CROSS_GROUP_USER_TEMPLATE = """以下是 {group_count} 个群在过去约 {minutes} 分钟内的聊天记录（共 {total} 条）。
每条消息标注了 [群号]。

{keywords_hint}

聊天记录：
{messages_text}

---

请按话题聚类提取有价值信息。返回纯 JSON（不要 markdown 代码块）：
{{"has_value": true/false, "topics": [{{"topic": "话题名", "groups": ["群号列表"], "items": [{{"content": "原文", "source_qq": "发言者QQ", "category": "apikey|资源|商机|情报|其他", "reason": "为什么有价值"}}], "summary": "一句话概括"}}], "overall_summary": "跨群整体概述"}}
"""


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
        # SK 聚合池（由 main.py 注入，与网关共享同一实例；未装配时为 None）
        self.sk_pool = None

        # 缓冲区: {group_id: [ {"text", "time", "sender_id", "name", "platform_id"} ]}
        # 攒群里所有人的消息（不仅目标 QQ），让 LLM 有完整上下文
        self._buffer: dict[str, list[dict]] = {}
        self._buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._stopping = False

        # 智能降噪层
        self._noise_reducer = NoiseReducer(config_manager)
        self._noise_reducer.set_send_callback(self._send_alert)

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

            text = await self._extract_text(event)
            if not text or not text.strip():
                return

            # 按模式分流
            mode = self.config_manager.get_monitor_mode()
            if mode == "keyword":
                logger.debug(f"[Monitor] 收到监控群消息 group={group_id} len={len(text)}")
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
        """关键词即时模式：不限发送者，命中关键词/正则 → 降噪 → 推送。

        降噪流程：
        1. 正则预筛
        2. LLM 确认（可选）
        3. 优先级分级（critical/normal/low）
        4. 冷却/去重检查
        5. critical → 立即推 / normal → 批量合并 / low → 丢弃
        """
        text = text.strip()

        # 第零层：发送者过滤
        # 配了 monitored_qqs 时，keyword 模式只检测这些人的消息（与 schema 描述一致）
        watched_qqs = set(self.config_manager.get_monitored_qqs())
        if watched_qqs and sender_id not in watched_qqs:
            return

        # 第一层：正则 + 自定义关键词预筛
        hits = self._regex_scan(text)
        if not hits:
            return  # 绝大多数消息在这里被丢弃

        sender_name = self._safe_sender_name(event, sender_id)
        platform_id = str(event.get_platform_id() or "").strip()

        # critical（API Key）命中：正则已足够精确（sk- 长密钥等），
        # 跳过 LLM 确认直接秒推——省 token 且不延误最高价值场景。
        # LLM 确认只用于 normal 类（网址/邀请码等模糊命中，噪音多）。
        is_critical_hit = any(c == "API Key" for c, _ in hits)

        # 第二层：LLM 确认（可选，仅 normal 类）
        # 裸密钥形态（"疑似密钥"）低置信：必须过 LLM 确认才推；
        # LLM 确认未开/不可用时宁可漏推不误推，直接丢弃
        fuzzy_only = bool(hits) and all(c == "疑似密钥" for c, _ in hits)
        if not is_critical_hit and fuzzy_only:
            if not self.config_manager.is_llm_confirm_enabled():
                logger.debug(
                    f"[Monitor-KW] {sender_id}@{group_id} 疑似密钥形态命中但 LLM 确认未开，丢弃"
                )
                return

        verdict = None
        if not is_critical_hit and self.config_manager.is_llm_confirm_enabled():
            verdict = await self._llm_confirm_keyword(sender_id, sender_name, text)
            if verdict is not None and not verdict.get("useful", True):
                logger.info(
                    f"[Monitor-KW] {sender_id}@{group_id} 命中但 LLM 判定无用，跳过"
                )
                return

        # 第三层：优先级分级
        priority = self._noise_reducer.classify_priority(hits, verdict)
        logger.info(
            f"[Monitor-KW] 命中 {sender_id}@{group_id}: "
            f"{'、'.join(sorted({c for c, _ in hits}))} 优先级={priority}"
        )

        if priority == NoiseReducer.PRIORITY_LOW:
            # low 优先级：丢弃，等 window 简报兜底
            logger.debug(
                f"[Monitor-KW] {sender_id}@{group_id} 低优先级，丢弃"
            )
            return

        # 第四层：冷却/去重检查（critical 豁免冷却）
        if priority != NoiseReducer.PRIORITY_CRITICAL:
            if self._noise_reducer.check_cooldown(sender_id, group_id):
                logger.debug(
                    f"[Monitor-KW] {sender_id}@{group_id} 冷却中，跳过"
                )
                return

        if self._noise_reducer.check_dedup(text):
            logger.debug(
                f"[Monitor-KW] {sender_id}@{group_id} 内容去重命中，跳过"
            )
            return

        # 第五层：按优先级分流
        batch_sec = self.config_manager.get_keyword_batch_seconds()
        if priority == NoiseReducer.PRIORITY_CRITICAL:
            # critical → 立即推送
            # 密钥类（API Key）命中：追加 LLM 平台分析 + 联网验真
            sk_analysis = None
            verify_result = None
            is_key_hit = any(c == "API Key" for c, _ in hits)
            if is_key_hit:
                sk_analysis = await self._analyze_sk_hit(sender_id, sender_name, text)
                source_url = (sk_analysis or {}).get("source_url") or None
                verify_result = await self._verify_sk_real(text, source_url)
                # 写入 SK 聚合池（网关数据源）；失败不影响推送
                try:
                    await self._harvest_sk_to_pool(
                        text, source_url, group_id, sender_id
                    )
                except Exception as e:
                    logger.warning(f"[Monitor-KW] SK 入池失败: {e}")
            await self._push_keyword_alert(
                sender_id=sender_id,
                sender_name=sender_name,
                group_id=group_id,
                text=text,
                hits=hits,
                llm_verdict=verdict,
                platform_id=platform_id,
                sk_analysis=sk_analysis,
                verify_result=verify_result,
            )
            self._noise_reducer.mark_cooldown(sender_id, group_id)
            self._noise_reducer.mark_dedup(text)
        elif priority == NoiseReducer.PRIORITY_NORMAL:
            if batch_sec > 0:
                # normal + 批量合并开启 → 入队
                await self._noise_reducer.enqueue_normal({
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "group_id": group_id,
                    "text": text,
                    "hits": hits,
                    "llm_verdict": verdict,
                    "platform_id": platform_id,
                })
            else:
                # normal + 批量合并关闭 → 直接推
                await self._push_keyword_alert(
                    sender_id=sender_id,
                    sender_name=sender_name,
                    group_id=group_id,
                    text=text,
                    hits=hits,
                    llm_verdict=verdict,
                    platform_id=platform_id,
                )
                self._noise_reducer.mark_cooldown(sender_id, group_id)
                self._noise_reducer.mark_dedup(text)

    def _regex_scan(self, text: str) -> list[tuple[str, str]]:
        """正则 + 自定义关键词扫描。返回命中的 (category, description) 列表。"""
        hits: list[tuple[str, str]] = []
        for pattern, category, desc in BUILTIN_PATTERNS:
            if pattern.search(text):
                hits.append((category, desc))
        # window/cross_group（整窗摘要）模式下裸网址是有效资源信号；
        # keyword 模式不匹配，避免普通分享刷屏 + 白烧确认 token
        if self.config_manager.get_monitor_mode() != "keyword":
            if _URL_PATTERN.search(text):
                hits.append(("资源链接", "包含网址"))
        else:
            # 裸密钥形态只在 keyword 即时模式扫（低置信，须 LLM 确认）
            hits.extend(_fuzzy_key_hits(text))
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

    async def _harvest_sk_to_pool(
        self,
        text: str,
        source_url: str | None,
        group_id: str,
        sender_id: str,
    ) -> None:
        """把消息里的 sk- 密钥写入聚合池（网关数据源）。

        懒验证：不主动联网验真（避免服务器 IP 被外部 key 后台记录），
        有效性由网关转发时懒判定。同一消息多个 key 全部入池。
        """
        pool = self.sk_pool
        if pool is None:
            return
        # sk-（OpenAI/Claude/中转站）+ AIza（Google）——网关都有对应原生端点可透传
        keys = re.findall(r"sk-[A-Za-z0-9_-]{20,}", text or "")
        keys += re.findall(r"AIza[A-Za-z0-9_-]{35}", text or "")
        if not keys:
            return
        for sk in dict.fromkeys(keys):
            pool.add(
                sk=sk,
                base_url=source_url or "",
                source_group=str(group_id),
                source_user=str(sender_id),
            )

    async def _analyze_sk_hit(
        self, sender_id: str, sender_name: str, text: str
    ) -> dict | None:
        """sk 密钥专项分析：LLM 分析平台/来源网址/可信度。

        在 critical（API Key）命中后调用，辅助判断密钥真实性。
        复用 call_provider_with_retry + _parse_llm_json。
        失败返回 None（推送里显示"分析不可用"）。
        """
        try:
            prompt = _SK_ANALYSIS_USER_TEMPLATE.format(
                sender=sender_id, sender_name=sender_name, content=text[:2000]
            )
            resp = await call_provider_with_retry(
                context=self.context,
                config_manager=self.config_manager,
                prompt=prompt,
                system_prompt=_SK_ANALYSIS_SYSTEM_PROMPT,
            )
            if resp is None:
                return None
            raw = extract_response_text(resp).strip()
            return self._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[Monitor-KW] sk 密钥分析失败，降级跳过: {e}")
            return None

    async def _verify_sk_real(
        self, text: str, source_url: str | None
    ) -> dict[str, str] | None:
        """联网验真 sk（调 OpenAI /v1/models + 中转站）。

        仅当 verify_sk_real 配置开启时调用。失败返回 None。
        注意：会用 sk 调外部 API，服务器 IP 会被对方 key 后台记录。
        """
        getter = getattr(self.config_manager, "is_verify_sk_real_enabled", None)
        if not callable(getter) or not getter():
            return None  # 开关关，跳过
        # 从文本提取 sk- 串（取最长的那个，最可能是真实密钥）
        sk_matches = re.findall(r"sk-[A-Za-z0-9_-]{20,}", text)
        if not sk_matches:
            return None
        sk = max(sk_matches, key=len)
        try:
            from ...infrastructure.messaging.sk_verifier import verify_sk
            return await verify_sk(sk, source_url)
        except Exception as e:
            logger.warning(f"[Monitor-KW] sk 验真失败: {e}")
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
        sk_analysis: dict | None = None,
        verify_result: dict[str, str] | None = None,
    ) -> None:
        """关键词模式：格式化并即时推送。

        sk_analysis：sk 命中后的 LLM 平台/来源分析（None=未分析/非密钥）
        verify_result：联网验真结果（None=未验真/开关关/失败）
        """
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
        content_display = text if len(text) <= 400 else text[:400] + " …(截断)"

        # sk 专项分析 + 验真结果（仅密钥类命中时追加）
        sk_lines = ""
        if sk_analysis:
            platform = sk_analysis.get("platform", "不确定")
            source_url = sk_analysis.get("source_url", "")
            confidence = sk_analysis.get("confidence", "")
            conf_map = {"high": "高", "medium": "中", "low": "低"}
            conf_str = conf_map.get(confidence, confidence)
            sk_lines += f"🔬 平台：{platform}（可信度：{conf_str}）\n"
            if source_url:
                sk_lines += f"🌐 来源：{source_url}\n"
        if verify_result is not None:
            from ...infrastructure.messaging.sk_verifier import format_verify_result
            verify_str = format_verify_result(verify_result)
            sk_lines += f"✅ 验真：{verify_str}\n"

        alert = (
            f"🚨 关键词命中预警〔{category_str}〕\n"
            f"👤 {sender_name} ({sender_id}) @ 群 {group_id}\n"
            f"💡 {reason or hit_details}\n"
        )
        if sk_lines:
            alert += sk_lines
        alert += (
            f"📝 原文：\n{content_display}\n"
            f"⏰ {_tz_now().strftime('%Y-%m-%d %H:%M:%S')}"
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
        """整窗汇总模式：不过滤发送者，所有人的消息都攒入缓冲区。

        【Phase 2: critical 旁路即时推送】
        若 critical_instant_push 开启且本条命中 critical（apikey 等），
        立即走降噪/冷却即时推一条，不必等 flush_interval。缓冲区仍照常攒。
        """
        text = text.strip()
        sender_name = self._safe_sender_name(event, sender_id)
        platform_id = str(event.get_platform_id() or "").strip()

        async with self._buffer_lock:
            self._buffer.setdefault(group_id, []).append(
                {
                    "text": text,
                    "time": time.monotonic(),
                    "sender_id": sender_id,
                    "name": sender_name,
                    "platform_id": platform_id,
                }
            )

        self._ensure_flush_task()

        # critical 旁路：window 模式下也允许 critical 秒推
        if self.config_manager.is_critical_instant_push_enabled():
            hits = self._regex_scan(text)
            if hits:
                priority = self._noise_reducer.classify_priority(hits, None)
                if priority == NoiseReducer.PRIORITY_CRITICAL:
                    await self._push_window_critical_now(
                        sender_id=sender_id,
                        sender_name=sender_name,
                        group_id=group_id,
                        text=text,
                        hits=hits,
                        platform_id=platform_id,
                    )

    async def _push_window_critical_now(
        self,
        sender_id: str,
        sender_name: str,
        group_id: str,
        text: str,
        hits: list[tuple[str, str]],
        platform_id: str,
    ) -> None:
        """window 模式下 critical 命中：走冷却+去重即时推一条。

        与 keyword critical 路径对齐：critical 豁免冷却但仍走去重指纹，
        防止同一密钥在多个群被推 N 次。
        """
        if self._noise_reducer.check_dedup(text):
            logger.debug(
                f"[Monitor-Window] critical 去重命中，跳过即时推: {sender_id}@{group_id}"
            )
            return

        alert = (
            f"🚨 [即时·密钥/凭证] window 模式 critical 命中\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {sender_name}({sender_id}) @ 群{group_id}\n"
            f"🎯 {'; '.join(desc for _, desc in hits[:3])}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 {text[:500]}{'…' if len(text) > 500 else ''}\n"
            f"⏰ {_tz_now().strftime('%H:%M:%S')}"
        )
        await self._send_alert(
            alert,
            context_desc=f"window critical 即时: {sender_name}@{group_id}",
            platform_id=platform_id,
        )
        self._noise_reducer.mark_dedup(text)
        logger.info(
            f"[Monitor-Window] critical 即时推送完成: {sender_id}@{group_id}"
        )

    # ============================================================
    # 后台 flush 任务
    # ============================================================

    def _ensure_flush_task(self) -> None:
        if self._stopping:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        # 同时启动降噪层的批量合并任务
        if self.config_manager.get_monitor_mode() == "keyword":
            batch_sec = self.config_manager.get_keyword_batch_seconds()
            if batch_sec > 0:
                self._noise_reducer.ensure_batch_task()

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
        """快照所有群缓冲区并清空。

        根据 enable_cross_group 配置分流：
        - 开启跨群聚合：合并所有群缓冲 → LLM 聚类 → 统一简报
        - 关闭（默认）：逐群独立处理
        """
        async with self._buffer_lock:
            if not self._buffer:
                return
            batches = {gid: list(msgs) for gid, msgs in self._buffer.items() if msgs}
            self._buffer.clear()

        if not batches:
            return

        # 降噪层清理过期记录
        self._noise_reducer.cleanup()

        logger.info(f"[Monitor] flush 触发：{len(batches)} 个群有待处理窗口")

        if self.config_manager.is_cross_group_enabled():
            # 跨群聚合模式：layered（分层）或 legacy（单次 LLM）
            mode = self.config_manager.get_aggregation_mode()
            if mode == "layered":
                await self._flush_layered(batches)
            else:
                await self._flush_cross_group(batches)
        else:
            # 原有模式：逐群独立处理
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
                lines.append("    …(后续已截断)")
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
        return resolve_admin_qqs(
            self.bot_manager,
            self.config_manager.get_extra_admin_qqs(),
        )

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
            logger.warning("[Monitor] 无推送目标（请配置 alert_admin_qqs 或 admins_id）")
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
                else:
                    logger.warning(f"[Monitor] 推送 {qq} 失败（可能是非好友）")
            except Exception as e:
                logger.error(f"[Monitor] 推送 {qq} 异常: {e}")

        extra = f"（{context_desc}）" if context_desc else ""
        logger.info(f"[Monitor] 推送完成：成功 {success}/{len(targets)}{extra}")

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
            f"⏰ {_tz_now().strftime('%Y-%m-%d %H:%M:%S')}"
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

    async def _extract_text(self, event: AstrMessageEvent) -> str:
        """提取消息纯文本。优先 message_str，兜底遍历消息段。

        支持合并转发消息（forward 段）：转发包的文本不在事件里，需调
        get_forward_msg API 拉取子消息文本合并。失败降级为不解析（只漏转发内容）。
        """
        parts: list[str] = []
        # 普通文本
        msg_str = (getattr(event, "message_str", "") or "").strip()
        if msg_str:
            parts.append(msg_str)
        # 遍历消息段，收集 Plain/text + forward 的 message_id
        forward_ids: list[str] = []
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
                elif seg_type == "forward":
                    # forward 段的 data 含 id（resId）
                    fwd_data = getattr(seg, "data", None) or {}
                    if isinstance(fwd_data, dict):
                        fwd_id = fwd_data.get("id") or fwd_data.get("resId") or ""
                        if fwd_id:
                            forward_ids.append(str(fwd_id))
        # 拉取转发包文本（异步，失败降级）
        for fwd_id in forward_ids[:3]:  # 最多解析 3 层转发，防递归爆炸
            fwd_text = await self._extract_forward_text(event, fwd_id)
            if fwd_text:
                parts.append(fwd_text)
        return " ".join(parts).strip()

    async def _extract_forward_text(
        self, event: AstrMessageEvent, message_id: str
    ) -> str:
        """拉取合并转发消息的子消息文本。

        调 adapter.get_forward_msg 拿转发包，递归提取每条子消息的文本。
        失败/超时返回空字符串（不影响主流程）。
        """
        platform_id = str(event.get_platform_id() or "").strip()
        adapter = self.bot_manager.get_adapter(platform_id) if platform_id else None
        # 兜底：遍历找支持 get_forward_msg 的 adapter
        if not adapter or not hasattr(adapter, "get_forward_msg"):
            for pid in self._get_all_platform_ids():
                a = self.bot_manager.get_adapter(pid)
                if a and hasattr(a, "get_forward_msg"):
                    adapter = a
                    break
        if not adapter or not hasattr(adapter, "get_forward_msg"):
            return ""
        try:
            result = await adapter.get_forward_msg(message_id)
            if not result:
                return ""
            return self._flatten_forward_messages(result)
        except Exception as e:
            logger.warning(f"[Monitor] 转发消息解析失败 (id={message_id}): {e}")
            return ""

    def _flatten_forward_messages(self, forward_data: dict, depth: int = 0) -> str:
        """递归展平 get_forward_msg 返回的转发包为纯文本。

        NapCat/OneBot 返回结构：{"messages": [ {content: [...], ...}, ... ]}
        每条子消息的 content 是消息段列表，提取 text 段。嵌套转发递归（限 3 层）。
        """
        if depth > 3:
            return ""
        messages = forward_data.get("messages") or forward_data.get("message") or []
        if not isinstance(messages, list):
            return ""
        parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            sender_name = ""
            sender = msg.get("sender") or {}
            if isinstance(sender, dict):
                sender_name = sender.get("card") or sender.get("nickname") or ""
            content = msg.get("content") or msg.get("message") or []
            if not isinstance(content, list):
                continue
            for seg in content:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type", "")
                seg_data = seg.get("data") or {}
                if seg_type in ("text", "Plain"):
                    t = seg_data.get("text") or ""
                    if t:
                        prefix = f"[{sender_name}] " if sender_name else ""
                        parts.append(f"{prefix}{t}")
                elif seg_type == "forward":
                    # 嵌套转发：记录 id，但无法在此处异步拉取，跳过（极少见）
                    pass
            # 防止单条过长
            if len(parts) > 200:
                break
        return " ".join(parts).strip()

    # ============================================================
    # 跨群聚合模式
    # ============================================================

    async def _flush_layered(self, batches: dict[str, list[dict]]) -> None:
        """【layered 模式，推荐】分层聚合 → 分类频道推送。

        Phase 1:
          L1 每群本地规则提炼 candidates（无 LLM）
          L2 跨群指纹去重 + 按 channel 聚合 + 预算截断
          ChannelPacker 按 channels_enabled / split|merged 打成推送
        Phase 2:
          L1 可选 LLM 提炼（l1_use_llm，并发受 llm_semaphore 约束）
          L2 超阈值分片提示（shard_threshold）
          单条推送超长自动分页（push_max_chars）
        """
        from .layered_aggregation import (
            ChannelPacker,
            CrossGroupAggregator,
            GroupCandidateExtractor,
            extract_all_group_candidates,
            extract_all_group_candidates_async,
        )

        watched = set(self.config_manager.get_monitored_qqs())
        keywords = self.config_manager.get_monitor_extra_keywords()

        # 1. L1：每群本地规则提炼（可选 LLM）
        llm_refine = None
        if self.config_manager.is_l1_use_llm_enabled():
            llm_refine = self._l1_llm_refine
        # window/跨群模式：裸网址是有效资源信号（keyword 模式不匹配，见 _regex_scan）
        layered_patterns = list(BUILTIN_PATTERNS) + [
            (_URL_PATTERN, "资源链接", "包含网址")
        ]
        extractor = GroupCandidateExtractor(
            patterns=layered_patterns,
            max_candidates_per_group=self.config_manager.get_max_candidates_per_group(),
            llm_refine_callback=llm_refine,
        )

        if llm_refine is not None:
            candidates = await extract_all_group_candidates_async(
                batches=batches,
                extractor=extractor,
                watched_user_ids=watched,
                extra_keywords=keywords,
                parallelism=self.config_manager.get_l1_parallel_groups(),
                llm_semaphore=getattr(self, "llm_semaphore", None),
            )
        else:
            candidates = extract_all_group_candidates(
                batches=batches,
                extractor=extractor,
                watched_user_ids=watched,
                extra_keywords=keywords,
            )

        total_msgs = sum(len(m) for m in batches.values())
        logger.info(
            f"[Monitor-Layered] L1 完成：{len(batches)} 群 · {total_msgs} 条消息"
            f" · 提炼 {len(candidates)} 个 candidates"
            f" · LLM={'on' if llm_refine else 'off'}"
        )

        if not candidates:
            logger.info("[Monitor-Layered] 本轮无 candidates，跳过推送")
            return

        # 2. L2：跨群去重 + 频道聚合（+ 分片提示）
        aggregator = CrossGroupAggregator(
            max_items_per_channel=self.config_manager.get_max_items_per_channel(),
            enabled_channels=self.config_manager.get_channels_enabled(),
            shard_threshold=self.config_manager.get_l2_shard_threshold(),
        )
        if aggregator.should_shard(candidates):
            logger.info(
                f"[Monitor-Layered] L2 触发分片：candidates={len(candidates)}"
                f" > threshold={self.config_manager.get_l2_shard_threshold()}"
                f"，按频道分组聚合：{aggregator.shard_channels()}"
            )
        result = aggregator.aggregate(
            candidates=candidates,
            group_count=len(batches),
            source_message_count=total_msgs,
        )
        logger.info(
            f"[Monitor-Layered] L2 完成：输出 {len(result.items)} 条"
            f" · 去重 {result.dropped_duplicates}"
        )

        if not result.items:
            return

        # 3. ChannelPacker 打包（含分页）
        packer = ChannelPacker(
            push_mode=self.config_manager.get_channel_push_mode(),
            interval_minutes=self.config_manager.get_flush_interval(),
            max_chars=self.config_manager.get_push_max_chars(),
        )
        messages = packer.pack(result)
        logger.info(
            f"[Monitor-Layered] 打包完成：{len(messages)} 条推送"
            f"（max_chars={self.config_manager.get_push_max_chars()}）"
        )

        # 4. 推送（复用 _send_alert，每条独立送）
        # 平台 ID：取首个非空
        platform_id = ""
        for msgs in batches.values():
            if msgs:
                platform_id = msgs[-1].get("platform_id", "")
                break

        for idx, alert_text in enumerate(messages, 1):
            await self._send_alert(
                alert_text,
                context_desc=f"分层简报 {idx}/{len(messages)}（{len(result.items)} 条情报）",
                platform_id=platform_id,
            )

    # ============================================================
    # L1 LLM 提炼（Phase 2）
    # ============================================================

    async def _l1_llm_refine(
        self, group_id: str, items: list
    ) -> list:
        """对单群 L1 candidates 做一次小 LLM 调用：
        合并近义条目、补 reason、归一 channel。

        失败时返回 None（extract_async 会回退规则结果）。
        """
        if not items:
            return items
        try:
            from ...domain.entities.intel_item import IntelItem
            from ...domain.services.intel_taxonomy import normalize_channel

            # 构造紧凑输入：每条 50 字预览
            preview_lines = []
            for i, it in enumerate(items, 1):
                content = (it.content or "")[:80].replace("\n", " ")
                preview_lines.append(
                    f"{i}. channel={it.channel} priority={it.priority} content={content}"
                )
            preview = "\n".join(preview_lines)

            system = (
                "你是情报分类助手。给你一个群里规则筛出的候选情报列表，"
                "请合并近义条目、校准 channel（apikey/resource/deal/intel/method/other）、"
                "为每条补一句简短 reason。返回 JSON。"
            )
            user = (
                f"群 {group_id} 候选数 {len(items)}：\n{preview}\n\n"
                "返回纯 JSON（不要 markdown 代码块），格式：\n"
                '{"items": [{"index": 1, "channel": "apikey", "reason": "疑似 OpenAI Key"}]}\n'
                "index 从 1 开始，对应输入序号。可省略（视为丢弃）。"
            )

            resp = await call_provider_with_retry(
                context=self.context,
                config_manager=self.config_manager,
                prompt=user,
                system_prompt=system,
            )
            if resp is None:
                return items
            raw = extract_response_text(resp).strip()
            data = self._parse_llm_json(raw) or {}
            verdicts = {int(v.get("index", 0)): v for v in (data.get("items") or []) if v}

            refined: list[IntelItem] = []
            for idx, it in enumerate(items, 1):
                v = verdicts.get(idx)
                if not v:
                    continue  # LLM 主动丢弃
                it.channel = normalize_channel(v.get("channel") or it.channel)
                if v.get("reason"):
                    it.reason = str(v.get("reason"))[:200]
                refined.append(it)
            return refined or items  # 若 LLM 全丢则保留原样
        except Exception as e:
            logger.warning(f"[L1-LLM] 群 {group_id} 提炼异常: {e}")
            raise  # 让 extract_async 走回退

    async def _flush_cross_group(self, batches: dict[str, list[dict]]) -> None:
        """【legacy 模式】合并所有群原始消息 → 一次 LLM 聚类 → 统一简报。

        群多时会因 4000 字硬截断丢失内容；保留作为兼容与回滚路径。

        Args:
            batches: {group_id: [消息列表]}，每个群一个列表
        """
        watched_qqs = set(self.config_manager.get_monitored_qqs())

        # 1. 合并所有群消息，标注来源群（用副本避免污染原始数据）
        all_messages: list[dict] = []
        platform_id = ""
        for group_id, messages in batches.items():
            for m in messages:
                entry = dict(m)
                entry["group_id"] = group_id
                all_messages.append(entry)
            if not platform_id and messages:
                platform_id = messages[-1].get("platform_id", "")

        if not all_messages:
            return

        # 2. 过滤：如果配了 monitored_qqs，至少有一个目标 QQ 发言
        if watched_qqs:
            has_target = any(
                m.get("sender_id", "") in watched_qqs for m in all_messages
            )
            if not has_target:
                logger.info("[Monitor-Cross] 跨群窗口中无目标 QQ 发言，跳过")
                return

        # 3. 去重检查（跨群聚合级别：同一内容指纹不重复处理）
        #    这里不做消息级去重，交给 LLM 聚类时自然合并

        # 4. 截断（与单群一致：以目标 QQ 发言为中心，向前向后扩展覆盖上下文）
        max_ctx = self.config_manager.get_max_context_messages()
        all_messages, total = self._truncate_window(
            all_messages, watched_qqs, max_ctx
        )
        shown = len(all_messages)

        # 5. LLM 跨群聚类提取
        dialog_text = self._build_cross_group_dialog(all_messages, watched_qqs)
        verdict = await self._llm_cross_group_extract(
            dialog_text, len(batches), total, shown
        )

        # 6. 决定是否推送
        if verdict is not None:
            if not verdict.get("has_value", False):
                logger.info(
                    f"[Monitor-Cross] 跨群窗口({shown}条) LLM 判定无价值，跳过"
                )
                return
        else:
            # LLM 不可用 → 降级：不做跨群聚合，回退到逐群
            logger.warning(
                "[Monitor-Cross] LLM 不可用，跨群聚合降级为逐群处理"
            )
            for group_id, messages in batches.items():
                try:
                    await self._flush_group(group_id, messages)
                except Exception as e:
                    logger.error(
                        f"[Monitor-Cross] 降级 flush 群 {group_id} 异常: {e}",
                        exc_info=True,
                    )
            return

        # 7. 推送跨群简报
        await self._push_cross_group_brief(verdict, len(batches), total, shown, platform_id)

    def _build_cross_group_dialog(
        self, messages: list[dict], watched_qqs: set[str]
    ) -> str:
        """构建跨群对话文本，每条消息标注来源群，目标 QQ 用 >>> 标记。"""
        lines: list[str] = []
        total_len = 0
        for i, msg in enumerate(messages, 1):
            is_target = msg.get("sender_id", "") in watched_qqs if watched_qqs else False
            marker = ">>> " if is_target else "    "
            group_id = msg.get("group_id", "未知")
            line = f"{marker}[{i}] [群{group_id}] [{msg.get('sender_id', '?')}]: {msg.get('text', '')}"
            if total_len + len(line) > _MAX_LLM_TEXT_CHARS:
                lines.append("    …(后续已截断)")
                break
            lines.append(line)
            total_len += len(line)
        return "\n".join(lines)

    async def _llm_cross_group_extract(
        self,
        dialog_text: str,
        group_count: int,
        total: int,
        shown: int,
    ) -> dict | None:
        """调用 LLM 做跨群聚类提取。"""
        try:
            keywords = self.config_manager.get_monitor_extra_keywords()
            keywords_hint = ""
            if keywords:
                keywords_hint = f"特别关注这些关键词：{', '.join(keywords)}"

            minutes = self.config_manager.get_flush_interval()
            prompt = _CROSS_GROUP_USER_TEMPLATE.format(
                group_count=group_count,
                minutes=minutes,
                total=total,
                shown=shown,
                keywords_hint=keywords_hint,
                messages_text=dialog_text,
            )

            resp = await call_provider_with_retry(
                context=self.context,
                config_manager=self.config_manager,
                prompt=prompt,
                system_prompt=_CROSS_GROUP_SYSTEM_PROMPT,
            )
            if resp is None:
                return None
            raw = extract_response_text(resp).strip()
            return self._parse_llm_json(raw)
        except Exception as e:
            logger.warning(f"[Monitor-Cross] LLM 跨群提取失败: {e}")
            return None

    async def _push_cross_group_brief(
        self,
        verdict: dict,
        group_count: int,
        total: int,
        shown: int,
        platform_id: str,
    ) -> None:
        """格式化跨群简报并推送。"""
        topics = verdict.get("topics", [])
        overall_summary = verdict.get("overall_summary", "")
        interval_min = self.config_manager.get_flush_interval()

        if not topics:
            # 兜底：用 items 格式
            items = verdict.get("items", [])
            if items:
                topics = [{"topic": "有价值信息", "groups": [], "items": items, "summary": ""}]

        topic_lines: list[str] = []
        for i, topic in enumerate(topics, 1):
            topic_name = str(topic.get("topic", f"话题{i}")).strip()
            groups = topic.get("groups", [])
            items = topic.get("items", [])
            topic_summary = str(topic.get("summary", "")).strip()

            # 检测是否有 critical 类别
            has_critical = any(
                str(item.get("category", "")).lower() in ("apikey", "api key")
                for item in items
            )
            critical_marker = " 🔴" if has_critical else ""

            line = f"📌 [话题 {i}] {topic_name}{critical_marker}"
            if groups:
                line += f"\n   涉及群：{', '.join(str(g) for g in groups)}"
            if topic_summary:
                line += f"\n   💡 {topic_summary}"
            else:
                # 从 items 中提取简短描述
                for item in items[:2]:
                    content = str(item.get("content", "")).strip()
                    if len(content) > 100:
                        content = content[:100] + "…"
                    reason = str(item.get("reason", "")).strip()
                    if reason:
                        line += f"\n   💡 {reason}"
                    elif content:
                        line += f"\n   📝 {content}"
            topic_lines.append(line)

        topics_section = "\n\n".join(topic_lines) if topic_lines else "（未提取到有价值话题）"

        valuable_count = sum(len(t.get("items", [])) for t in topics)

        alert = (
            f"🧠 跨群情报简报（近 {interval_min} 分钟）\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {group_count} 个群 · {total} 条消息（送审 {shown} 条）"
            f" · 筛出 {len(topics)} 个话题\n"
            f"\n"
            f"{topics_section}\n"
        )
        if overall_summary:
            alert += f"\n💡 跨群概述：{overall_summary}\n"
        alert += (
            f"\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {_tz_now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        await self._send_alert(
            alert,
            context_desc=f"跨群简报：{group_count}群，{valuable_count}条有价值",
            platform_id=platform_id,
        )

    # ============================================================
    # 生命周期
    # ============================================================

    def stop(self) -> None:
        self._stopping = True
        self._noise_reducer.stop()
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
