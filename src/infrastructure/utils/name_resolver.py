"""
群名 / 人名解析器

把 digest 里裸露的 group_id / user_id 解析成人类可读的群名、群名片（或昵称）。
这是「报告可读性」的基石——纯数字 ID `[群975206796]` 对人类毫无意义。

设计要点：
- 复用各平台 adapter 已实现的 get_group_info / get_member_info（OneBot/Telegram/...）
- TTL 缓存：群名变化慢（1h），群名片偶尔变（30min），避免每条消息都打 API
- 失败优雅降级：解析不到就返回 ID 本身，绝不阻塞报告产出
- 并发安全：同一 key 的并发解析只打一次 API（in-flight 去重）

不依赖任何具体平台，通过注入的 adapter 抽象调用。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...utils.logger import logger

# 群名缓存 TTL（秒）：群名很少变，缓存长一点
_GROUP_NAME_TTL = 3600
# 群名片/昵称缓存 TTL（秒）：群名片偶尔改，缓存短一点
_MEMBER_NAME_TTL = 1800


class NameResolver:
    """群名 / 人名解析，带 TTL 缓存 + 并发去重。

    一个 NameResolver 实例对应一次日报任务的生命周期（任务级缓存），
    跨任务不复用——避免长期运行后群名变更不刷新。
    """

    def __init__(self, bot_manager: Any):
        self._bot_manager = bot_manager
        # (platform_id, group_id) -> (display_name, expire_ts)
        self._group_cache: dict[tuple[str | None, str], tuple[str, float]] = {}
        # (platform_id, group_id, user_id) -> (display_name, expire_ts)
        self._member_cache: dict[tuple[str | None, str, str], tuple[str, float]] = {}
        # in-flight 去重：同一 key 并发只打一次 API
        self._group_locks: dict[tuple[str | None, str], asyncio.Future[str]] = {}
        self._member_locks: dict[
            tuple[str | None, str, str], asyncio.Future[str]
        ] = {}

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    async def resolve_group_name(
        self, group_id: str, platform_id: str | None = None
    ) -> str:
        """group_id -> 群名（如「AI搞钱群」）。解析失败返回原 group_id。"""
        if not group_id:
            return ""
        key = (platform_id, str(group_id))

        # 1. 命中未过期缓存
        cached = self._group_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        # 2. in-flight 去重
        inflight = self._group_locks.get(key)
        if inflight is not None:
            return await inflight

        # 3. 发起解析
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._group_locks[key] = fut
        try:
            name = await self._fetch_group_name(group_id, platform_id)
            self._group_cache[key] = (name, time.monotonic() + _GROUP_NAME_TTL)
            fut.set_result(name)
            return name
        except Exception as e:
            logger.debug(f"解析群名失败 group={group_id}: {e}")
            fallback = str(group_id)
            # 失败也短缓存，避免连续重试打爆 API（60s）
            self._group_cache[key] = (fallback, time.monotonic() + 60)
            if not fut.done():
                fut.set_result(fallback)
            return fallback
        finally:
            self._group_locks.pop(key, None)

    async def resolve_user_name(
        self,
        group_id: str,
        user_id: str,
        platform_id: str | None = None,
    ) -> str:
        """(group_id, user_id) -> 群名片优先 / 否则昵称。解析失败返回原 user_id。

        注意：digest 里的 source_user_id 可能已经是昵称（如 LLM 回填的「夙梦」），
        调用方应优先使用已有值，仅当它是纯数字 ID 时才走本方法解析。
        """
        if not user_id:
            return ""
        # 已是可读名字（非纯数字）则直接用，省一次 API
        if not str(user_id).strip().isdigit():
            return str(user_id).strip()

        key = (platform_id, str(group_id), str(user_id))
        cached = self._member_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        inflight = self._member_locks.get(key)
        if inflight is not None:
            return await inflight

        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._member_locks[key] = fut
        try:
            name = await self._fetch_member_name(group_id, user_id, platform_id)
            self._member_cache[key] = (name, time.monotonic() + _MEMBER_NAME_TTL)
            fut.set_result(name)
            return name
        except Exception as e:
            logger.debug(f"解析群成员名失败 group={group_id} user={user_id}: {e}")
            fallback = str(user_id)
            self._member_cache[key] = (fallback, time.monotonic() + 60)
            if not fut.done():
                fut.set_result(fallback)
            return fallback
        finally:
            self._member_locks.pop(key, None)

    async def resolve_group_name_batch(
        self,
        group_ids: list[str],
        platform_id: str | None = None,
    ) -> dict[str, str]:
        """批量解析群名，返回 {group_id: display_name}。"""
        if not group_ids:
            return {}
        results = await asyncio.gather(
            *[self.resolve_group_name(gid, platform_id) for gid in group_ids],
            return_exceptions=True,
        )
        out: dict[str, str] = {}
        for gid, res in zip(group_ids, results):
            out[gid] = str(res) if not isinstance(res, Exception) else str(gid)
        return out

    # ------------------------------------------------------------------
    # 底层 fetch（跨平台 adapter 抽象）
    # ------------------------------------------------------------------

    async def _fetch_group_name(
        self, group_id: str, platform_id: str | None
    ) -> str:
        adapter = self._get_adapter(platform_id)
        if adapter is None:
            return str(group_id)
        # 统一接口：get_group_info（IGroupInfoRepository）
        getter = getattr(adapter, "get_group_info", None)
        if getter is None:
            return str(group_id)
        group = await getter(group_id)
        if group is not None:
            name = getattr(group, "group_name", "") or ""
            if name:
                return name
        return str(group_id)

    async def _fetch_member_name(
        self, group_id: str, user_id: str, platform_id: str | None
    ) -> str:
        adapter = self._get_adapter(platform_id)
        if adapter is None:
            return str(user_id)
        getter = getattr(adapter, "get_member_info", None)
        if getter is None:
            return str(user_id)
        member = await getter(group_id, user_id)
        if member is not None:
            # 群名片优先（最贴近「这个群里大家叫他什么」），其次昵称
            card = getattr(member, "card", None) or ""
            if card:
                return card
            nick = getattr(member, "nickname", "") or ""
            if nick:
                return nick
        return str(user_id)

    def _get_adapter(self, platform_id: str | None) -> Any:
        """按 platform_id 取 adapter，取不到退回唯一/默认 adapter。"""
        adapter = None
        if hasattr(self._bot_manager, "get_adapter"):
            adapter = self._bot_manager.get_adapter(platform_id)
            if adapter is None and platform_id is not None:
                adapter = self._bot_manager.get_adapter(None)
        return adapter
