"""
历史记录管理器模块 - 基础设施持久化层
负责存储和查询群聊分析报告的摘要信息
使用 AstrBot 的 put_kv_data/get_kv_data 实现
"""

from typing import Any

from ...shared.timezone import now as _tz_now
from ...utils.logger import logger


class HistoryManager:
    """
    核心组件：历史分析存档管理器

    该类负责将每日生成的群消息分析报告摘要持久化存储，并提供查询接口。
    底层基于 AstrBot 提供的 KV 存储能力（put_kv_data/get_kv_data），
    确保即使在 Bot 重启后也能回溯历史数据。

    索引设计：
    - 由于 AstrBot KV 不支持按前缀扫描，本类维护一份按群分桶的索引
      (Key: ``history_index_{group_id}``)，记录每条摘要的完整 KV key 与
      归档时间戳，供 ``cleanup_old_summaries`` 按保留期删除使用。
    """

    # 索引键前缀；值为 list[{"key": str, "ts": float}]
    INDEX_PREFIX = "history_index"

    def __init__(self, star_instance: Any):
        """
        初始化历史记录管理器。

        Args:
            star_instance (Any): Star 插件实例，用于访问底层持久化引擎
        """
        self.plugin = star_instance

    def _index_key(self, group_id: str) -> str:
        return f"{self.INDEX_PREFIX}_{group_id}"

    async def _get_index(self, group_id: str) -> list[dict]:
        try:
            data = await self.plugin.get_kv_data(self._index_key(group_id), None)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.error(f"读取历史摘要索引失败 (群 {group_id}): {e}")
        return []

    async def _save_index(self, group_id: str, index: list[dict]) -> None:
        try:
            await self.plugin.put_kv_data(self._index_key(group_id), index)
        except Exception as e:
            # 索引写失败不影响摘要本身（最多下次清理漏几条）
            logger.warning(f"保存历史摘要索引失败 (群 {group_id}): {e}")

    async def save_analysis(
        self,
        group_id: str,
        analysis_result: dict[str, Any],
        date_str: str | None = None,
        time_str: str | None = None,
    ) -> bool:
        """
        序列化并存储一份分析报告摘要。

        摘要包含：发言总量、人数、提取的主题摘要及生成时间，不包含完整的原始消息流。

        Args:
            group_id (str): 群组 ID
            analysis_result (dict[str, Any]): 包含 statistics, topics 的完整分析对象
            date_str (str, optional): 归档日期 (YYYY-MM-DD)，缺省为当天
            time_str (str, optional): 归档时间点 (HH-MM)，缺省为当前时刻

        Returns:
            bool: 存储是否成功
        """
        try:
            now = _tz_now()
            if not date_str:
                date_str = now.strftime("%Y-%m-%d")
            if not time_str:
                time_str = now.strftime("%H-%M")

            # 消解非法字符，确保 Key 兼容性
            time_str = time_str.replace(":", "-")

            # 从分析结果中剥离非持久化字段，提取核心统计元数据
            stats = analysis_result.get("statistics")
            topics = analysis_result.get("topics", [])

            summary = {
                "message_count": getattr(stats, "message_count", 0) if stats else 0,
                "participant_count": getattr(stats, "participant_count", 0)
                if stats
                else 0,
                "topics": [{"topic": t.topic, "detail": t.detail} for t in topics],
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

            key = f"analysis_{group_id}_{date_str}_{time_str}"
            await self.plugin.put_kv_data(key, summary)

            # 同步索引（用于按保留期清理）
            try:
                index = await self._get_index(group_id)
                index.append({"key": key, "ts": now.timestamp()})
                await self._save_index(group_id, index)
            except Exception as e:
                logger.warning(f"更新历史摘要索引失败 (群 {group_id}): {e}")

            logger.info(
                f"已保存群 {group_id} 在 {date_str} {time_str} 的分析摘要到历史记录 (Key: {key})"
            )
            return True
        except Exception as e:
            logger.error(f"保存历史分析记录失败: {e}", exc_info=True)
            return False

    async def cleanup_old_summaries(
        self, group_id: str, keep_days: int
    ) -> int:
        """按保留期清理指定群的历史摘要 KV 数据。

        依赖 save_analysis 维护的索引；索引里 ts 早于 ``keep_days`` 天前
        的条目对应的 KV key 会被删除，索引本身也会同步收缩。

        Args:
            group_id: 群 ID
            keep_days: 保留天数；<=0 视为不清理

        Returns:
            实际删除的条目数。
        """
        if keep_days <= 0:
            return 0
        try:
            import time as _time

            index = await self._get_index(group_id)
            if not index:
                return 0

            cutoff = _time.time() - keep_days * 86400
            keep: list[dict] = []
            deleted = 0
            for entry in index:
                ts = entry.get("ts", 0) if isinstance(entry, dict) else 0
                if ts and ts < cutoff:
                    key = entry.get("key") if isinstance(entry, dict) else None
                    if key:
                        try:
                            await self.plugin.delete_kv_data(key)
                            deleted += 1
                        except Exception as e:
                            logger.warning(
                                f"删除历史摘要 {key} 失败 (群 {group_id}): {e}"
                            )
                            # 删失败就保留索引项，下次再试
                            keep.append(entry)
                else:
                    keep.append(entry)

            if deleted:
                await self._save_index(group_id, keep)
                logger.info(
                    f"已清理群 {group_id} 的 {deleted} 条超过 {keep_days} 天的历史摘要"
                )
            return deleted
        except Exception as e:
            logger.error(f"清理历史摘要失败 (群 {group_id}): {e}")
            return 0

    async def get_history(
        self, group_id: str, date_str: str, time_str: str
    ) -> dict[str, Any] | None:
        """
        根据群组、日期和时间点检索一份历史摘要。
        """
        time_str = time_str.replace(":", "-")
        key = f"analysis_{group_id}_{date_str}_{time_str}"
        return await self.plugin.get_kv_data(key, None)

    async def has_history(self, group_id: str, date_str: str, time_str: str) -> bool:
        """
        快速判定是否存在指定时间点的历史分析记录。
        """
        history = await self.get_history(group_id, date_str, time_str)
        return history is not None
