"""
统一时区工具

整个插件应当只用这里的 `now()` / `from_timestamp()`，避免散落的
`datetime.now()` / `datetime.fromtimestamp()`（裸调用会受运行环境 TZ 影响 ——
AstrBot 跑在 Docker 默认 UTC 时会把 "每天 09:00 推送" 变成 UTC 09:00，
也会让按小时分桶的统计错位）。

设计要点：
- 时区来源由 ConfigManager 注入（用户在 _conf_schema.json 的 basic.timezone 配置）。
  这里只负责缓存与 fallback —— 缓存层不依赖 AstrBotConfig，避免循环导入。
- 非法/无法识别的时区一律回退到 Asia/Shanghai（中国主战场）。
- 公开 API：
    * now()          —— tz-aware datetime，对外业务逻辑用
    * now_iso()      —— ISO8601 字符串，写日志/JSON 时用
    * from_timestamp(ts) —— Unix 时间戳转 tz-aware datetime（消息分桶/展示）
- 提供一个 today_str(fmt) 用于按本地日期生成 key（历史日报按日期分桶）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .constants import PLUGIN_NAME  # noqa: F401  保留给调用方复用

# 默认时区 —— 配置非法/未设时的兜底
DEFAULT_TZ_NAME = "Asia/Shanghai"

# 进程内缓存解析后的 ZoneInfo，避免每次构造 datetime 都重新解析
# （ZoneInfo 内部有缓存，但显式缓存让 _active_tz_name 与对象一致，便于日志诊断）
_active_tz: ZoneInfo = ZoneInfo(DEFAULT_TZ_NAME)
_active_tz_name: str = DEFAULT_TZ_NAME


def configure_timezone(tz_name: str | None) -> str:
    """设置全局时区。

    Args:
        tz_name: 时区名（IANA 标识，如 "Asia/Shanghai"）。空或非法会回退默认。

    Returns:
        实际生效的时区名（用于日志确认）。
    """
    global _active_tz, _active_tz_name
    name = (tz_name or "").strip() or DEFAULT_TZ_NAME
    try:
        new_tz = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        # 非法时区：不抛异常，回退到默认
        if name != DEFAULT_TZ_NAME:
            # 仅在真的失败时记录 —— 避免初始化阶段默认值也打日志
            import logging
            logging.getLogger(PLUGIN_NAME).warning(
                "无法识别的时区配置 %r，回退到 %s", name, DEFAULT_TZ_NAME
            )
        new_tz = ZoneInfo(DEFAULT_TZ_NAME)
        name = DEFAULT_TZ_NAME

    _active_tz = new_tz
    _active_tz_name = name
    return name


def active_tz() -> ZoneInfo:
    """返回当前生效的 ZoneInfo 对象。"""
    return _active_tz


def active_tz_name() -> str:
    """返回当前生效的时区名（字符串）。"""
    return _active_tz_name


def now() -> datetime:
    """返回带时区的当前时间。所有业务逻辑应使用此函数而非 datetime.now()。"""
    return datetime.now(_active_tz)


def now_utc() -> datetime:
    """返回 UTC 当前时间（用于与 telegram_adapter 等已有 UTC 代码兼容）。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """ISO8601 格式的当前时间字符串（带时区偏移）。"""
    return now().isoformat()


def today_str(fmt: str = "%Y-%m-%d") -> str:
    """按当前时区返回今天的日期字符串（用于历史日报的日期 key）。"""
    return now().strftime(fmt)


def from_timestamp(ts: int | float) -> datetime:
    """把 Unix 时间戳转为带当前时区的 datetime。

    用于消息时间戳的展示与按小时分桶统计。
    """
    return datetime.fromtimestamp(ts, _active_tz)
