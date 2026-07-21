"""AutoScheduler delivery_mode 分支测试。"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

# generators 依赖 ulid；单测环境可能未装，先 stub 再导入调度器
if "ulid" not in sys.modules:
    _ulid = types.ModuleType("ulid")

    class _ULID:
        def __str__(self) -> str:
            return "01TESTULID00000000000000"

    _ulid.ULID = _ULID
    sys.modules["ulid"] = _ulid

from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.scheduler.auto_scheduler import AutoScheduler
from tests.conftest import AstrBotConfig


def _make_scheduler(delivery_mode: str = "per_group") -> AutoScheduler:
    cfg = ConfigManager(
        AstrBotConfig(
            {
                "basic": {"group_list_mode": "none"},
                "auto_analysis": {
                    "delivery_mode": delivery_mode,
                    "categories": (
                        [{"name": "科技", "groups": ["1"]}]
                        if delivery_mode == "by_category"
                        else []
                    ),
                    "scheduled_group_list_mode": "whitelist",
                    "scheduled_group_list": ["1"] if delivery_mode == "per_group" else [],
                    "auto_analysis_time": ["23:00"],
                },
                "performance": {"max_concurrent_tasks": 2, "stagger_seconds": 0},
            }
        )
    )
    analysis = MagicMock()
    bot = MagicMock()
    bot.get_platform_count = MagicMock(return_value=1)
    bot.get_platform_ids = MagicMock(return_value=["onebot"])
    return AutoScheduler(cfg, analysis, bot, report_generator=MagicMock())


def test_by_category_routes_to_category_digest():
    sched = _make_scheduler("by_category")
    sched._run_category_digest_report = AsyncMock()
    sched._get_scheduled_targets = AsyncMock(
        side_effect=AssertionError("per_group path should not run")
    )

    asyncio.run(sched._run_scheduled_report())
    sched._run_category_digest_report.assert_awaited_once()


def test_per_group_uses_targets_path():
    sched = _make_scheduler("per_group")
    sched._get_scheduled_targets = AsyncMock(return_value=[])
    sched._run_category_digest_report = AsyncMock(
        side_effect=AssertionError("category path should not run")
    )

    asyncio.run(sched._run_scheduled_report())
    sched._get_scheduled_targets.assert_awaited_once()
    sched._run_category_digest_report.assert_not_awaited()


def test_schedule_jobs_skips_incremental_when_by_category():
    sched = _make_scheduler("by_category")
    # force enabled checks
    sched.config_manager.get_incremental_enabled = MagicMock(return_value=True)
    sched._schedule_report_time_jobs = MagicMock()
    sched._schedule_incremental_cron_jobs = MagicMock()
    sched.unschedule_jobs = MagicMock()

    context = MagicMock()
    context.cron_manager.scheduler = MagicMock()
    sched.schedule_jobs(context)

    sched._schedule_report_time_jobs.assert_called_once()
    sched._schedule_incremental_cron_jobs.assert_not_called()
