"""月报聚合服务：吃当月周报产物（不足则回退日报），产出榜单 + HTML 存档。

与周报的分工：周报讲「线索怎么走」，月报讲「沉淀下什么」——
主题榜（最终结论）、资源归档、避坑清单、趋势信号。
月报额外产出一份可长期保存的 HTML 存档（不是图片），方便日后检索。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import escape as _esc
from pathlib import Path
from typing import Any

from ...domain.models.data_models import DigestTheme
from ...shared.timezone import now_iso
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from .scheduled_category_digest_service import (
    CategoryDigest,
    ScheduledCategoryDigestService,
    ValueItem,
)
from .weekly_review_service import load_daily_digests, serialize_day_themes

_WEEK_THEME_NARRATIVE_LIMIT = 800


def _prev_month_range(today: datetime) -> tuple[str, str, str]:
    """上一个月的 (月份标签 YYYY-MM, 起始日, 结束日)。"""
    first = today.replace(day=1)
    last_of_prev = first - timedelta(days=1)
    start = last_of_prev.replace(day=1)
    return (
        last_of_prev.strftime("%Y-%m"),
        start.strftime("%Y-%m-%d"),
        last_of_prev.strftime("%Y-%m-%d"),
    )


class MonthlyReviewService:
    """月报：当月周报（或日报兜底）→ LLM 月度榜单 → 图片摘要 + HTML 存档 + 邮件。"""

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
            logger.warning(f"[MonthlyReview] 落盘目录创建失败: {e}")
        self._analyzer = self._build_analyzer()
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
            return WeeklyThemeAnalyzer(context, self.config_manager, period="month")
        except Exception as e:
            logger.warning(f"[MonthlyReview] 分析器装配失败，将走降级: {e}")
            return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self, platform_id: str | None = None) -> dict[str, Any]:
        trace_id = TraceContext.get()
        today = datetime.now()
        month_label, start_str, end_str = _prev_month_range(today)
        logger.info(f"[{trace_id}] 月报开始: {month_label}（{start_str} ~ {end_str}）")

        serialized, items, source_desc = self._collect_month_items(
            start_str, end_str
        )
        if not serialized:
            logger.info(f"[{trace_id}] 月报跳过：当月无周报/日报内容")
            return {"success": False, "reason": "no_content"}

        themes = await self._aggregate(serialized, items)
        digest = CategoryDigest(
            name=f"月报 {month_label}",
            items=items,
            groups_analyzed=[source_desc],
            groups_skipped=[],
            themes=themes,
        )

        sent, image_ok = await self._deliver(digest, platform_id, month_label)
        html_path = self._persist_html(digest, month_label, start_str, end_str)
        json_path = self._persist_json(
            digest, month_label, start_str, end_str, sent, image_ok
        )
        logger.info(
            f"[{trace_id}] 月报完成: themes={len(themes)}, sent={sent}, "
            f"html={html_path}, json={json_path}"
        )
        return {
            "success": sent > 0 or image_ok,
            "themes": len(themes),
            "messages_sent": sent,
            "html_path": str(html_path) if html_path else None,
            "json_path": str(json_path) if json_path else None,
        }

    # ------------------------------------------------------------------
    # 数据收集：优先周报，不足回退日报
    # ------------------------------------------------------------------

    def _collect_month_items(
        self, start_str: str, end_str: str
    ) -> tuple[list[dict], list[ValueItem], str]:
        weekly = self._load_weekly_reviews(start_str, end_str)
        if len(weekly) >= 2:
            return (*self._serialize_weekly(weekly), f"{len(weekly)} 份周报")
        # 回退：直接吃日报（月报仍需工作，哪怕周报还没积累够）
        since = datetime.strptime(start_str, "%Y-%m-%d")
        until = datetime.strptime(end_str, "%Y-%m-%d")
        daily = load_daily_digests(self._digests_dir, since, until)
        serialized, items, groups = serialize_day_themes(daily)
        return serialized, items, f"{len(daily)} 天日报 · {len(groups)} 群"

    def _load_weekly_reviews(
        self, start_str: str, end_str: str
    ) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        if not self._review_dir.exists():
            return out
        for path in sorted(self._review_dir.glob("weekly_*.json")):
            m = re.match(r"weekly_(\d{8})\.json", path.name)
            if not m:
                continue
            date_str = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
            if not (start_str <= date_str <= end_str):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[MonthlyReview] 周报读取失败 {path.name}: {e}")
                continue
            out.append((date_str, data))
        return out

    def _serialize_weekly(
        self, weekly: list[tuple[str, dict]]
    ) -> tuple[list[dict], list[ValueItem]]:
        serialized: list[dict] = []
        items: list[ValueItem] = []
        idx = 0
        for date_str, data in weekly:
            rng = data.get("range") or ["", date_str]
            label = f"周报 {rng[0]}~{rng[1]}"
            for theme in data.get("themes", []):
                title = str(theme.get("title", "")).strip()
                narrative = str(theme.get("narrative", "")).strip()
                if not title and not narrative:
                    continue
                idx += 1
                if len(narrative) > _WEEK_THEME_NARRATIVE_LIMIT:
                    narrative = narrative[:_WEEK_THEME_NARRATIVE_LIMIT] + "…"
                serialized.append(
                    {
                        "item_id": idx,
                        "source": label,
                        "content": f"{title}｜{narrative}" if title else narrative,
                        "reason": "",
                        "importance": theme.get("importance", "medium"),
                    }
                )
                items.append(
                    ValueItem(
                        content=f"{title}" + (f"｜{narrative}" if narrative else ""),
                        reason="",
                        source_group_name=label,
                        kind="topic",
                        importance=theme.get("importance", "medium"),
                    )
                )
        return serialized, items

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    async def _aggregate(
        self, serialized: list[dict], items: list[ValueItem]
    ) -> list[DigestTheme]:
        if not self._analyzer:
            return []
        id_to_item = {i + 1: it for i, it in enumerate(items)}
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            sem = getattr(self.analysis_service, "llm_semaphore", None)
            if sem is not None:
                async with sem:
                    themes, _usage = await self._analyzer.analyze(
                        serialized, umo="monthly_review", session_id=session_id
                    )
            else:
                themes, _usage = await self._analyzer.analyze(
                    serialized, umo="monthly_review", session_id=session_id
                )
        except Exception as e:
            logger.error(f"[MonthlyReview] 月报聚合失败，降级为平铺: {e}")
            return []
        for theme in themes:
            theme.related_items = [
                id_to_item[i] for i in theme.related_item_ids if i in id_to_item
            ]
        return themes

    # ------------------------------------------------------------------
    # 渲染 + 发送
    # ------------------------------------------------------------------

    async def _deliver(
        self, digest: CategoryDigest, platform_id: str | None, month_label: str
    ) -> tuple[int, bool]:
        renderer = self._renderer
        date_str = datetime.now().strftime("%Y-%m-%d")
        push_mode = "merged"

        text_messages = renderer.pack_digests([digest], push_mode=push_mode)
        if not text_messages:
            return 0, False
        # 文本兜底的名头从「分类日报」换成月报
        text_messages = [
            m.replace("📋 分类日报", f"📋 每月情报回顾 {month_label}", 1)
            for m in text_messages
        ]

        output_format = "text"
        if hasattr(self.config_manager, "get_category_output_format"):
            output_format = self.config_manager.get_category_output_format()

        image_ok = False
        if output_format == "image":
            payloads = renderer.pack_digests_payload(
                [digest], push_mode=push_mode, date_str=date_str
            )
            for p in payloads:
                p["title"] = f"每月情报回顾 {month_label}"
                p["overall_meta"] = (
                    f"{len(digest.items)} 条周主题 → {len(digest.themes)} 个月度主题"
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

        if getattr(self.config_manager, "is_monthly_email_enabled", lambda: True)():
            try:
                await renderer._send_digest_email([digest], push_mode)
            except Exception as e:
                logger.warning(f"[MonthlyReview] 月报邮件发送失败: {e}")

        return sent, image_ok

    # ------------------------------------------------------------------
    # 落盘：JSON（下月输入）+ HTML（可长期保存的存档）
    # ------------------------------------------------------------------

    def _persist_json(
        self,
        digest: CategoryDigest,
        month_label: str,
        start_str: str,
        end_str: str,
        sent: int,
        image_ok: bool,
    ) -> Path | None:
        try:
            path = self._review_dir / f"monthly_{month_label}.json"
            payload = {
                "period": "month",
                "month": month_label,
                "range": [start_str, end_str],
                "generated_at": now_iso(),
                "source_items": len(digest.items),
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
            logger.warning(f"[MonthlyReview] 月报 JSON 落盘失败: {e}")
            return None

    def _persist_html(
        self,
        digest: CategoryDigest,
        month_label: str,
        start_str: str,
        end_str: str,
    ) -> Path | None:
        """自包含 HTML 存档（不依赖模板引擎，可直接用浏览器打开）。"""
        try:
            path = self._review_dir / f"monthly_{month_label}.html"
            rank_label = {"high": "重要", "medium": "一般", "low": "轻量"}
            cards: list[str] = []
            for t in digest.themes:
                tags_html = "".join(
                    f'<span class="tag">{_esc(str(x))}</span>' for x in (t.tags or [])
                )
                related_html = ""
                if t.related_items:
                    lis = "".join(
                        f'<li><span class="src">{_esc(it.source_group_name or "")}</span>'
                        f"{_esc(it.content[:300])}</li>"
                        for it in t.related_items
                    )
                    related_html = f'<div class="related"><ul>{lis}</ul></div>'
                cards.append(
                    f'<div class="card imp-{_esc(t.importance)}">'
                    f'<div class="card-head"><span class="card-title">{_esc(t.title)}</span>'
                    f'<span class="badge">{rank_label.get(t.importance, "一般")}</span>{tags_html}</div>'
                    f'<div class="narrative">{_esc(t.narrative)}</div>'
                    f"{related_html}</div>"
                )
            body = "\n".join(cards) or '<p class="empty">本月无沉淀内容</p>'
            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每月情报回顾 {month_label}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Noto Sans SC", sans-serif; max-width: 760px; margin: 0 auto; padding: 28px 20px; background: #f5f6f8; color: #2c3338; line-height: 1.6; }}
h1 {{ font-size: 1.5em; margin: 0 0 4px; }}
.meta {{ color: #8b929a; font-size: 0.86em; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 2px solid #e3e6ea; }}
.card {{ background: #fff; border: 1px solid #e0e3e8; border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); break-inside: avoid; }}
.card.imp-high {{ border-left: 4px solid #c53030; }}
.card.imp-medium {{ border-left: 4px solid #d69e2e; }}
.card.imp-low {{ border-left: 4px solid #3a6ea5; }}
.card-head {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }}
.card-title {{ font-size: 1.06em; font-weight: 700; }}
.badge {{ font-size: 0.7em; padding: 1px 7px; border-radius: 4px; background: #eef3f8; color: #3a6ea5; }}
.tag {{ font-size: 0.72em; color: #8b929a; background: #f0f1f3; border-radius: 4px; padding: 1px 6px; }}
.narrative {{ white-space: pre-line; font-size: 0.94em; color: #3a4046; }}
.related {{ margin-top: 10px; padding: 8px 12px; background: #fafbfc; border-left: 3px solid #d0d4da; border-radius: 0 6px 6px 0; }}
.related ul {{ margin: 0; padding-left: 18px; }}
.related li {{ font-size: 0.85em; color: #4a5258; margin: 5px 0; }}
.src {{ color: #3a6ea5; font-weight: 600; margin-right: 6px; }}
.empty {{ color: #8b929a; text-align: center; padding: 30px 0; }}
.footer {{ margin-top: 20px; padding-top: 12px; border-top: 1px solid #e3e6ea; color: #8b929a; font-size: 0.8em; text-align: center; }}
</style>
</head>
<body>
<h1>每月情报回顾 {month_label}</h1>
<div class="meta">{start_str} ~ {end_str} · {len(digest.themes)} 个主题 · 输入 {len(digest.items)} 条 · 生成 {now_iso()}</div>
{body}
<div class="footer">Generated by AstrBot · QQ 群日报插件 · 月报存档</div>
</body>
</html>"""
            path.write_text(html, encoding="utf-8")
            return path
        except Exception as e:
            logger.warning(f"[MonthlyReview] 月报 HTML 落盘失败: {e}")
            return None
