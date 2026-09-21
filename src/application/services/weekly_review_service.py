"""周报/月报聚合服务：读已落盘的日报 digest 存档，产出周期回顾。

与日报的分工（不重叠）：
- 日报回答「今天发生了什么」——事件清单
- 周报回答「这条线一周怎么走的」——持续主线 / 演化反转 / 跨群共振 / 反复的坑
- 月报回答「这个月沉淀了什么」——主题榜单 / 资源归档 / 避坑清单

周期报告**不重新拉取 QQ 消息**：输入全部是已落盘的 digest JSON，
口径与日报一致、零抓取成本、可离线重跑。
渲染与发送完全复用 ScheduledCategoryDigestService（同一套图片模板/邮件/私聊通道）。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.models.data_models import DigestTheme
from ...shared.timezone import now_iso, today_str
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from .scheduled_category_digest_service import (
    CategoryDigest,
    ScheduledCategoryDigestService,
    ValueItem,
)

# 喂给 LLM 的单条日主题叙事截断长度（控制周报输入规模）
_DAY_THEME_NARRATIVE_LIMIT = 600


def load_daily_digests(
    digests_dir: Path, since: datetime, until: datetime
) -> list[tuple[str, dict]]:
    """读取日期落在 [since.date(), until.date()] 内的日报存档。

    返回 [(YYYY-MM-DD, digest_json)] 按日期升序；文件损坏/解析失败自动跳过。
    """
    out: list[tuple[str, dict]] = []
    if not digests_dir.exists():
        return out
    for path in sorted(digests_dir.glob("digest_*.json")):
        m = re.match(r"digest_(\d{8})_(\d{6})\.json", path.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if not (since.date() <= file_date <= until.date()):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Review] 日报存档读取失败 {path.name}: {e}")
            continue
        out.append((file_date.strftime("%Y-%m-%d"), data))
    return out


def serialize_day_themes(
    daily: list[tuple[str, dict]],
) -> tuple[list[dict], list[ValueItem], list[str]]:
    """把每日已聚合的主题拍平成「日主题条目」。

    返回 (analyzer 输入序列化列表, 对应的 ValueItem 列表, 出现过的群名列表)。
    analyzer 输入的 item_id 与 ValueItem 列表的下标（1-based）一一对应。
    """
    serialized: list[dict] = []
    items: list[ValueItem] = []
    groups: list[str] = []
    idx = 0
    for date_str, data in daily:
        for cat in data.get("digests", []):
            cat_name = cat.get("name", "")
            for theme in cat.get("themes", []):
                title = str(theme.get("title", "")).strip()
                narrative = str(theme.get("narrative", "")).strip()
                if not title and not narrative:
                    continue
                idx += 1
                if len(narrative) > _DAY_THEME_NARRATIVE_LIMIT:
                    narrative = narrative[:_DAY_THEME_NARRATIVE_LIMIT] + "…"
                serialized.append(
                    {
                        "item_id": idx,
                        "source": f"{date_str} {cat_name}".strip(),
                        "content": f"{title}｜{narrative}" if title else narrative,
                        "reason": "",
                        "importance": theme.get("importance", "medium"),
                    }
                )
                items.append(
                    ValueItem(
                        content=f"{title}" + (f"｜{narrative}" if narrative else ""),
                        reason="",
                        source_group_name=f"{date_str} {cat_name}".strip(),
                        kind="topic",
                        importance=theme.get("importance", "medium"),
                    )
                )
            for g in cat.get("groups_analyzed", []):
                g = str(g)
                if g and g not in groups:
                    groups.append(g)
    return serialized, items, groups


class WeeklyReviewService:
    """周报：最近 N 天日报存档 → LLM 聚合主线 → 图片 + 邮件 → 落盘。"""

    def __init__(
        self,
        config_manager: Any,
        analysis_service: Any,
        bot_manager: Any,
        report_generator: Any = None,
        html_render_func: Any = None,
        data_dir: str | Path | None = None,
    ):
        self.config_manager = config_manager
        self.analysis_service = analysis_service
        self.bot_manager = bot_manager
        base = Path(data_dir) if data_dir else Path("/tmp/astrbot_reviews")
        self._digests_dir = base / "digests"
        self._review_dir = base / "reviews"
        try:
            self._review_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[WeeklyReview] 落盘目录创建失败: {e}")
        self._analyzer = self._build_analyzer()
        # 渲染/发送委托给日报服务（同一套模板、转发卡片、邮件通道）
        self._renderer = ScheduledCategoryDigestService(
            config_manager=config_manager,
            analysis_service=analysis_service,
            bot_manager=bot_manager,
            report_generator=report_generator,
            html_render_func=html_render_func,
            data_dir=data_dir,
        )

    def _build_analyzer(self):
        try:
            from ...infrastructure.analysis.analyzers.weekly_theme_analyzer import (
                WeeklyThemeAnalyzer,
            )

            llm_analyzer = getattr(self.analysis_service, "llm_analyzer", None)
            context = getattr(llm_analyzer, "context", None)
            if context is None or self.config_manager is None:
                return None
            return WeeklyThemeAnalyzer(context, self.config_manager, period="week")
        except Exception as e:
            logger.warning(f"[WeeklyReview] 分析器装配失败，将走降级: {e}")
            return None

    def _lookback_days(self) -> int:
        getter = getattr(self.config_manager, "get_weekly_lookback_days", None)
        if callable(getter):
            try:
                return max(2, int(getter()))
            except Exception:
                pass
        return 7

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self, platform_id: str | None = None) -> dict[str, Any]:
        trace_id = TraceContext.get()
        today = datetime.now()
        days = self._lookback_days()
        since = today - timedelta(days=days - 1)

        daily = load_daily_digests(self._digests_dir, since, today)
        if len(daily) < 2:
            logger.info(
                f"[{trace_id}] 周报跳过：窗口内日报存档不足（{len(daily)} 份 < 2）"
            )
            return {"success": False, "reason": "not_enough_daily_digests"}

        serialized, items, groups = serialize_day_themes(daily)
        if not serialized:
            logger.info(f"[{trace_id}] 周报跳过：窗口内日报均无主题内容")
            return {"success": False, "reason": "no_day_themes"}

        start_str = daily[0][0]
        end_str = daily[-1][0]
        logger.info(
            f"[{trace_id}] 周报开始: {start_str}~{end_str}，"
            f"{len(daily)} 份日报 → {len(serialized)} 条日主题"
        )

        themes = await self._aggregate(serialized, items)
        digest = CategoryDigest(
            name=f"周报 {start_str} ~ {end_str}",
            items=items,
            groups_analyzed=groups,
            groups_skipped=[],
            themes=themes,
        )

        sent, image_ok = await self._deliver(digest, platform_id, start_str, end_str)
        path = self._persist(digest, start_str, end_str, sent, image_ok)
        logger.info(
            f"[{trace_id}] 周报完成: themes={len(themes)}, sent={sent}, "
            f"image_ok={image_ok}, path={path}"
        )
        return {
            "success": sent > 0 or image_ok,
            "themes": len(themes),
            "days": len(daily),
            "messages_sent": sent,
            "review_path": str(path) if path else None,
        }

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    async def _aggregate(
        self, serialized: list[dict], items: list[ValueItem]
    ) -> list[DigestTheme]:
        if not self._analyzer:
            return []
        id_to_item = {i + 1: it for i, it in enumerate(items)}
        umo = "weekly_review"
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            sem = getattr(self.analysis_service, "llm_semaphore", None)
            if sem is not None:
                async with sem:
                    themes, _usage = await self._analyzer.analyze(
                        serialized, umo=umo, session_id=session_id
                    )
            else:
                themes, _usage = await self._analyzer.analyze(
                    serialized, umo=umo, session_id=session_id
                )
        except Exception as e:
            logger.error(f"[WeeklyReview] 周报聚合失败，降级为时间线平铺: {e}")
            return []
        for theme in themes:
            theme.related_items = [
                id_to_item[i] for i in theme.related_item_ids if i in id_to_item
            ]
        return themes

    # ------------------------------------------------------------------
    # 渲染 + 发送（复用日报链路）
    # ------------------------------------------------------------------

    async def _deliver(
        self,
        digest: CategoryDigest,
        platform_id: str | None,
        start_str: str,
        end_str: str,
    ) -> tuple[int, bool]:
        renderer = self._renderer
        date_str = datetime.now().strftime("%Y-%m-%d")
        push_mode = "merged"

        text_messages = renderer.pack_digests([digest], push_mode=push_mode)
        if not text_messages:
            return 0, False
        # 文本兜底的名头从「分类日报」换成周报（日报打头的字样对周报是误导）
        text_messages = [
            m.replace("📋 分类日报", f"📋 每周情报回顾 {start_str} ~ {end_str}", 1)
            for m in text_messages
        ]

        output_format = "text"
        if hasattr(self.config_manager, "get_category_output_format"):
            output_format = self.config_manager.get_category_output_format()

        deliveries: list[tuple[str | None, str]] = []
        image_ok = False
        if output_format == "image":
            payloads = renderer.pack_digests_payload(
                [digest], push_mode=push_mode, date_str=date_str
            )
            for p in payloads:
                p["title"] = f"每周情报回顾 {start_str} ~ {end_str}"
                p["overall_meta"] = (
                    f"{len(digest.groups_analyzed)} 群 · {len(digest.items)} 条日主题 "
                    f"→ {len(digest.themes)} 条主线"
                )
            deliveries = await renderer._render_image_deliveries(
                [digest], text_messages, push_mode=push_mode
            )
            image_ok = any(img for img, _ in deliveries)
        else:
            deliveries = [(None, m) for m in text_messages]

        md_contents = None
        try:
            md_contents = renderer.pack_digests_markdown([digest], push_mode=push_mode)
        except Exception:
            pass

        sent = await renderer._send_to_admins(
            deliveries, platform_id=platform_id, md_contents=md_contents
        )

        if getattr(self.config_manager, "is_weekly_email_enabled", lambda: True)():
            try:
                await renderer._send_digest_email([digest], push_mode)
            except Exception as e:
                logger.warning(f"[WeeklyReview] 周报邮件发送失败: {e}")

        return sent, image_ok

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def _persist(
        self,
        digest: CategoryDigest,
        start_str: str,
        end_str: str,
        sent: int,
        image_ok: bool,
    ) -> Path | None:
        try:
            path = self._review_dir / f"weekly_{end_str.replace('-', '')}.json"
            payload = {
                "period": "week",
                "range": [start_str, end_str],
                "generated_at": now_iso(),
                "day_items": len(digest.items),
                "groups": digest.groups_analyzed,
                "sent": sent,
                "image_ok": image_ok,
                "themes": [
                    {
                        "title": t.title,
                        "narrative": t.narrative,
                        "importance": t.importance,
                        "tags": t.tags,
                        "related_item_ids": t.related_item_ids,
                    }
                    for t in digest.themes
                ],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return path
        except Exception as e:
            logger.warning(f"[WeeklyReview] 周报落盘失败: {e}")
            return None
