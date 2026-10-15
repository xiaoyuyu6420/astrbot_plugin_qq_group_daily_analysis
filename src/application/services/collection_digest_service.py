"""采集简报服务：把"信息采集群"变成定期剪报。

链路：拉采集群消息（水位增量）→ 链接富化（网页标题/摘要）→ LLM 剪报 → 私聊管理员 → 落盘。

与 ScheduledCategoryDigestService 是兄弟关系但走独立链路：
- 分类聚合日报的输入是"聊天"，目标是提取讨论价值；
- 采集简报的输入是"剪藏"（链接/文章/片段），目标是归类整理可溯源的信息条目。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...infrastructure.analysis.utils.llm_utils import (
    call_provider_with_retry,
    extract_response_text,
)
from ...infrastructure.utils.admin_resolver import resolve_admin_qqs
from ...infrastructure.utils.template_utils import render_template
from ...infrastructure.web_fetcher import LinkInfo, extract_urls, fetch_links_batch
from ...shared.timezone import from_timestamp, now_iso, today_str
from ...shared.trace_context import TraceContext
from ...utils.logger import logger

# 水位回退重叠：上次成功时间往前多看 5 分钟，容忍边界消息晚到
_WATERMARK_OVERLAP_SECONDS = 300
# QQ 单条文本安全上限（NapCat 长文本易失败/截断），留余量
_SEND_CHUNK_LIMIT = 3500
# 降级清单里单条消息展示上限
_FALLBACK_ITEM_TEXT_LIMIT = 200

_LLM_SYSTEM_PROMPT = (
    "你是一名个人信息助理，负责把用户随手收集到群里的信息整理成清爽的剪报。"
    "用户会把链接、文章、工具、想法、待办线索丢进采集群，你的任务是归类、摘要、去重、标注来源，"
    "让他花 1 分钟能看完当天收集了什么、哪些值得优先看。"
)

_DEFAULT_PROMPT = """以下是信息采集群过去一段时间收集的原始内容。每条含时间、收集人、消息原文，以及自动抓取的链接标题/摘要（标注了 [链接富化]；没有标注的链接是抓取失败的，凭 URL 域名和上下文判断即可）。

${items_text}

请整理成一份剪报，要求：
1. 按主题归类（如"文章/资讯"、"工具/资源"、"观点/想法"、"待办/线索"，按实际内容灵活命名，不硬套固定分类；某类没内容就不出现）
2. 每条信息：一句话摘要，保留关键数字与结论，并保留原始链接以便溯源；明显同主题的多条合并为一条并标注"（含 N 条相关）"
3. 与采集无关的日常闲聊、表情、打卡直接忽略；垃圾信息不进入简报
4. 末尾给"收集概览"：今天共收集 N 条 · M 个主题 · 值得优先看的 1-3 条（一句话理由）
5. 输出纯 markdown 文本（二级/三级标题 + 列表），不要输出 JSON 或代码块，全文控制在 ${max_chars} 字内，干净不啰嗦"""


@dataclass
class CollectedItem:
    """一条采集记录（消息或纯链接）。"""

    time_str: str
    sender: str
    text: str
    urls: list[str] = field(default_factory=list)
    group_id: str = ""


class CollectionDigestService:
    """采集简报：水位增量拉取采集群 → 链接富化 → LLM 剪报 → 私聊管理员。"""

    def __init__(
        self,
        config_manager: Any,
        analysis_service: Any,
        bot_manager: Any,
        data_dir: str | Path | None = None,
    ):
        self.config_manager = config_manager
        self.analysis_service = analysis_service
        self.bot_manager = bot_manager
        if data_dir is not None:
            self._store_dir = Path(data_dir) / "collection_digests"
        else:
            self._store_dir = Path("/tmp/astrbot_collection_digests")
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[CollectionDigest] 落盘目录创建失败: {e}")
        try:
            retain = int(getattr(config_manager, "get_digest_retain_days", lambda: 7)())
        except Exception:
            retain = 7
        self._retain_days = max(1, retain)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self, platform_id: str | None = None) -> dict[str, Any]:
        """执行一次采集简报任务。"""
        trace_id = TraceContext.get()
        group_ids = self.config_manager.get_collection_group_ids()
        if not group_ids:
            logger.info(f"[{trace_id}] 采集简报：未配置采集群，跳过")
            return {"success": False, "reason": "no_groups"}

        since_ts = self._load_watermark()
        if since_ts is None:
            days = max(1, self.config_manager.get_collection_lookback_days())
            since_ts = int(time.time() - days * 86400)
            window_desc = f"回看 {days} 天"
        else:
            window_desc = "自上次简报以来"

        logger.info(
            f"[{trace_id}] 采集简报开始: {len(group_ids)} 个群, {window_desc}"
        )

        items = await self._collect_items(group_ids, platform_id, since_ts)
        if not items:
            logger.info(f"[{trace_id}] 采集简报：窗口内无有效采集内容，不推送")
            return {"success": True, "reason": "no_content", "items": 0, "messages_sent": 0}

        # 链接富化：全局去重后截断，避免一天几百条链接拖垮抓取
        enriched = await self._enrich_links(items)
        items_text = self._build_items_text(items, enriched)

        digest_text = await self._generate_digest(items_text, total_items=len(items))
        llm_ok = digest_text is not None
        if not llm_ok:
            digest_text = self._fallback_digest(items, enriched)

        sent = await self._send_to_admins(digest_text, platform_id)
        digest_path = self._persist(digest_text, items_count=len(items), llm_ok=llm_ok)
        if sent > 0:
            self._save_watermark()
        self._cleanup_old_files()
        logger.info(
            f"[{trace_id}] 采集简报完成: items={len(items)}, llm_ok={llm_ok}, "
            f"sent={sent}, path={digest_path}"
        )
        return {
            "success": sent > 0,
            "items": len(items),
            "messages_sent": sent,
            "llm_ok": llm_ok,
            "digest_path": str(digest_path),
        }

    # ------------------------------------------------------------------
    # 采集：拉消息 → 过滤
    # ------------------------------------------------------------------

    async def _collect_items(
        self, group_ids: list[str], platform_id: str | None, since_ts: int
    ) -> list[CollectedItem]:
        adapter = self._resolve_adapter(platform_id)
        if not adapter:
            logger.error(f"[CollectionDigest] 无可用 adapter，无法拉取采集群消息")
            return []

        max_count = max(200, self.config_manager.get_max_messages() or 1000)
        items: list[CollectedItem] = []
        for gid in group_ids:
            gid = str(gid).strip()
            if not gid:
                continue
            try:
                messages = await adapter.fetch_messages(
                    group_id=gid, days=30, max_count=max_count, since_ts=since_ts
                )
            except Exception as e:
                logger.warning(f"[CollectionDigest] 拉取采集群 {gid} 失败: {e}")
                continue
            for msg in messages or []:
                text = (getattr(msg, "text_content", "") or "").strip()
                urls = extract_urls(text)
                # 图片等富媒体段的资源链接不参与网页富化，但保留消息本身
                if not text or text.startswith("/"):
                    if not urls:
                        continue
                if len(text) > 800:
                    text = text[:800] + "…"
                ts = getattr(msg, "timestamp", 0) or 0
                items.append(
                    CollectedItem(
                        time_str=from_timestamp(ts).strftime("%m-%d %H:%M") if ts else "",
                        sender=(getattr(msg, "sender_card", "") or getattr(msg, "sender_name", "") or getattr(msg, "sender_id", "") or "?"),
                        text=text,
                        urls=urls,
                        group_id=gid,
                    )
                )
        # 时间序排列
        items.sort(key=lambda it: it.time_str)
        return items

    def _resolve_adapter(self, platform_id: str | None):
        adapter = self.bot_manager.get_adapter(platform_id)
        if adapter and hasattr(adapter, "fetch_messages"):
            return adapter
        for pid in getattr(self.bot_manager, "get_platform_ids", lambda: [])():
            a = self.bot_manager.get_adapter(pid)
            if a and hasattr(a, "fetch_messages"):
                return a
        return None

    # ------------------------------------------------------------------
    # 链接富化
    # ------------------------------------------------------------------

    async def _enrich_links(self, items: list[CollectedItem]) -> dict[str, LinkInfo]:
        if not self.config_manager.is_collection_link_fetch_enabled():
            return {}
        max_links = max(5, self.config_manager.get_collection_max_links())
        all_urls: list[str] = []
        seen: set[str] = set()
        for it in items:
            for u in it.urls:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)
        if len(all_urls) > max_links:
            logger.info(
                f"[CollectionDigest] 链接数 {len(all_urls)} 超上限 {max_links}，仅富化前 {max_links} 条"
            )
            all_urls = all_urls[:max_links]
        if not all_urls:
            return {}
        try:
            return await fetch_links_batch(all_urls)
        except Exception as e:
            logger.warning(f"[CollectionDigest] 链接富化整体失败: {e}")
            return {}

    def _build_items_text(
        self, items: list[CollectedItem], enriched: dict[str, LinkInfo]
    ) -> str:
        max_chars = max(4000, self.config_manager.get_collection_max_message_chars())
        lines: list[str] = []
        used = 0
        for it in items:
            parts = [f"- [{it.time_str}] {it.sender}: {it.text or '（无文字，仅链接）'}"]
            for u in it.urls:
                info = enriched.get(u)
                if info and not info.fetch_failed:
                    rich = f"  [链接富化] 标题: {info.title or '无'}"
                    if info.description:
                        rich += f" | 摘要: {info.description}"
                    parts.append(f"{rich}\n  链接: {u}")
                else:
                    parts.append(f"  链接: {u}")
            line = "\n".join(parts)
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # LLM 剪报
    # ------------------------------------------------------------------

    async def _generate_digest(self, items_text: str, total_items: int) -> str | None:
        context = self._llm_context()
        if context is None:
            logger.warning("[CollectionDigest] 无法获取 LLM 上下文，降级为机械清单")
            return None
        prompt_template = self.config_manager.get_collection_prompt() or _DEFAULT_PROMPT
        prompt = render_template(
            prompt_template,
            items_text=items_text,
            total_items=total_items,
            max_chars=2000,
        )
        try:
            resp = await call_provider_with_retry(
                context=context,
                config_manager=self.config_manager,
                prompt=prompt,
                system_prompt=_LLM_SYSTEM_PROMPT,
            )
        except Exception as e:
            logger.warning(f"[CollectionDigest] LLM 调用失败: {e}")
            return None
        if resp is None:
            return None
        text = extract_response_text(resp).strip()
        return text or None

    def _llm_context(self):
        llm_analyzer = getattr(self.analysis_service, "llm_analyzer", None)
        context = getattr(llm_analyzer, "context", None)
        return context if context is not None else getattr(self.analysis_service, "context", None)

    def _fallback_digest(
        self, items: list[CollectedItem], enriched: dict[str, LinkInfo]
    ) -> str:
        """LLM 不可用时的降级：机械清单直推，保证采集内容不丢。"""
        lines = ["📋 采集简报（LLM 暂不可用，原始清单）", ""]
        for it in items:
            text = it.text[:_FALLBACK_ITEM_TEXT_LIMIT] + ("…" if len(it.text) > _FALLBACK_ITEM_TEXT_LIMIT else "")
            entry = f"- [{it.time_str}] {it.sender}: {text or '（仅链接）'}"
            info = enriched.get(it.urls[0]) if it.urls else None
            if info and not info.fetch_failed and info.title:
                entry += f"\n  ↳ {info.title}"
            for u in it.urls:
                entry += f"\n  {u}"
            lines.append(entry)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 推送与落盘
    # ------------------------------------------------------------------

    async def _send_to_admins(self, digest_text: str, platform_id: str | None) -> int:
        trace_id = TraceContext.get()
        adapter = self.bot_manager.get_adapter(platform_id)
        if not adapter or not hasattr(adapter, "send_private"):
            for pid in getattr(self.bot_manager, "get_platform_ids", lambda: [])():
                a = self.bot_manager.get_adapter(pid)
                if a and hasattr(a, "send_private"):
                    adapter = a
                    break
        if not adapter or not hasattr(adapter, "send_private"):
            logger.error(f"[{trace_id}] 采集简报推送失败：无支持私聊的 adapter")
            return 0

        admin_qqs = resolve_admin_qqs(
            self.bot_manager, self.config_manager.get_extra_admin_qqs()
        )
        if not admin_qqs:
            logger.warning(f"[{trace_id}] 无管理员 QQ，采集简报未发出")
            return 0

        chunks = _split_message(digest_text, _SEND_CHUNK_LIMIT)
        sent = 0
        for qq in admin_qqs:
            for i, chunk in enumerate(chunks):
                try:
                    if await adapter.send_private(user_id=qq, text=chunk):
                        sent += 1
                    else:
                        logger.warning(f"[{trace_id}] 采集简报发送 {qq} 第{i+1}段失败")
                except Exception as e:
                    logger.error(f"[{trace_id}] 采集简报发送 {qq} 异常: {e}")
        return sent

    def _persist(self, digest_text: str, items_count: int, llm_ok: bool) -> Path | None:
        try:
            path = self._store_dir / f"collection_{today_str('%Y%m%d_%H%M')}.md"
            header = (
                f"# 采集简报 {today_str('%Y-%m-%d %H:%M')}\n\n"
                f"> 条目: {items_count} · LLM: {'ok' if llm_ok else 'fallback'} · 生成: {now_iso()}\n\n"
            )
            path.write_text(header + digest_text + "\n", encoding="utf-8")
            return path
        except Exception as e:
            logger.warning(f"[CollectionDigest] 简报落盘失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 水位
    # ------------------------------------------------------------------

    def _watermark_path(self) -> Path:
        return self._store_dir / "watermark.json"

    def _load_watermark(self) -> int | None:
        try:
            data = json.loads(self._watermark_path().read_text(encoding="utf-8"))
            ts = int(data.get("last_success_ts", 0))
            if ts <= 0:
                return None
            return ts - _WATERMARK_OVERLAP_SECONDS
        except Exception:
            return None

    def _save_watermark(self) -> None:
        try:
            self._watermark_path().write_text(
                json.dumps({"last_success_ts": int(time.time())}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[CollectionDigest] 水位写入失败: {e}")

    def _cleanup_old_files(self) -> None:
        try:
            cutoff = time.time() - self._retain_days * 86400
            for f in self._store_dir.glob("collection_*.md"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except Exception:
            pass


def _split_message(text: str, limit: int) -> list[str]:
    """按长度切段，优先在换行处切。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    return [c for c in chunks if c]
