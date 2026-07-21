"""
LLM分析器模块
负责协调各个分析器进行话题分析和金句分析
"""

import asyncio

from ...domain.models.data_models import (
    GoldenQuote,
    SummaryTopic,
    TokenUsage,
)
from ...domain.repositories.analysis_repository import IAnalysisProvider
from ...shared.constants import PLUGIN_NAME
from ...utils.logger import logger
from .analyzers.golden_quote_analyzer import GoldenQuoteAnalyzer
from .analyzers.topic_analyzer import TopicAnalyzer
from .utils.json_utils import fix_json
from .utils.llm_utils import call_provider_with_retry


class LLMAnalyzer(IAnalysisProvider):
    """
    LLM分析器
    作为统一入口，协调各个专门的分析器进行不同类型的分析
    保持向后兼容性，提供原有的接口
    """

    topic_analyzer: TopicAnalyzer
    golden_quote_analyzer: GoldenQuoteAnalyzer

    def __init__(self, context, config_manager):
        """
        初始化LLM分析器

        Args:
            context: AstrBot上下文对象
            config_manager: 配置管理器
        """
        self.context = context
        self.config_manager = config_manager

        # 初始化各个专门的分析器
        self.topic_analyzer = TopicAnalyzer(context, config_manager)
        self.golden_quote_analyzer = GoldenQuoteAnalyzer(context, config_manager)

    async def analyze_topics(
        self,
        messages: list[dict],
        umo: str | None = None,
        session_id: str | None = None,
    ) -> tuple[list[SummaryTopic], TokenUsage]:
        """
        使用LLM分析话题
        保持原有接口，委托给专门的TopicAnalyzer处理

        Args:
            messages: 群聊消息列表
            umo: 模型唯一标识符
            session_id: 会话ID (用于调试模式)

        Returns:
            (话题列表, Token使用统计)
        """
        try:
            if not session_id:
                from ...shared.timezone import now as _tz_now

                timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
                if umo:
                    # Sanitize umo for filename (replace : with _)
                    safe_umo = umo.replace(":", "_")
                    session_id = f"{timestamp}_{safe_umo}"
                else:
                    session_id = timestamp

            logger.info(f"开始话题分析, session_id: {session_id}")
            return await self.topic_analyzer.analyze_topics(messages, umo, session_id)
        except Exception as e:
            logger.error(f"话题分析失败: {e}")
            return [], TokenUsage()

    async def analyze_golden_quotes(
        self,
        messages: list[dict],
        umo: str | None = None,
        session_id: str | None = None,
    ) -> tuple[list[GoldenQuote], TokenUsage]:
        """
        使用LLM分析群聊金句
        保持原有接口，委托给专门的GoldenQuoteAnalyzer处理

        Args:
            messages: 群聊消息列表
            umo: 模型唯一标识符
            session_id: 会话ID (用于调试模式)

        Returns:
            (金句列表, Token使用统计)
        """
        try:
            if not session_id:
                from ...shared.timezone import now as _tz_now

                timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
                if umo:
                    safe_umo = umo.replace(":", "_")
                    session_id = f"{timestamp}_{safe_umo}"
                else:
                    session_id = timestamp

            logger.info(f"开始金句分析, session_id: {session_id}")
            return await self.golden_quote_analyzer.analyze_golden_quotes(
                messages, umo, session_id
            )
        except Exception as e:
            logger.error(f"金句分析失败: {e}")
            return [], TokenUsage()

    async def analyze_all_concurrent(
        self,
        messages: list[dict],
        user_activity: dict,
        umo: str | None = None,
        top_users: list[dict] | None = None,
        topic_enabled: bool = True,
        golden_quote_enabled: bool = True,
    ) -> tuple[list[SummaryTopic], list[GoldenQuote], TokenUsage]:
        """
        并发执行分析任务（话题、金句），支持按需启用。

        Args:
            messages: 群聊消息列表
            user_activity: 用户分析统计
            umo: 模型唯一标识符
            top_users: 活跃用户列表(可选)
            topic_enabled: 是否启用话题分析
            golden_quote_enabled: 是否启用金句分析

        Returns:
            (话题列表, 金句列表, 总Token使用统计)
        """
        try:
            from ...shared.timezone import now as _tz_now

            timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
            if umo:
                safe_umo = umo.replace(":", "_")
                session_id = f"{timestamp}_{safe_umo}"
            else:
                session_id = timestamp

            logger.info(
                f"开始并发执行分析任务 (话题:{topic_enabled}, 金句:{golden_quote_enabled})，会话ID: {session_id}"
            )

            # 保存原始消息数据 (Debug Mode)
            if self.config_manager.get_debug_mode():
                self._save_debug_messages(messages, session_id)

            # 构建并发任务列表
            tasks = []
            task_names = []

            if topic_enabled:
                tasks.append(
                    self.topic_analyzer.analyze_topics(messages, umo, session_id)
                )
                task_names.append("topic")

            if golden_quote_enabled:
                tasks.append(
                    self.golden_quote_analyzer.analyze_golden_quotes(
                        messages, umo, session_id
                    )
                )
                task_names.append("golden_quote")

            if not tasks:
                return [], [], TokenUsage()

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            topics, topic_usage = [], TokenUsage()
            golden_quotes, quote_usage = [], TokenUsage()

            for i, result in enumerate(results):
                name = task_names[i]
                if isinstance(result, Exception):
                    logger.error(f"分析任务 {name} 失败: {result}")
                    continue

                if name == "topic" and isinstance(result, tuple):
                    topics, topic_usage = result
                elif name == "golden_quote" and isinstance(result, tuple):
                    golden_quotes, quote_usage = result

            # 合并Token使用统计
            total_usage = TokenUsage(
                prompt_tokens=topic_usage.prompt_tokens
                + quote_usage.prompt_tokens,
                completion_tokens=topic_usage.completion_tokens
                + quote_usage.completion_tokens,
                total_tokens=topic_usage.total_tokens
                + quote_usage.total_tokens,
            )

            logger.info(
                f"并发分析完成 - 话题: {len(topics)}, 金句: {len(golden_quotes)}"
            )
            return (
                topics,
                golden_quotes,
                total_usage,
            )

        except Exception as e:
            logger.error(f"并发分析失败: {e}")
            return [], [], TokenUsage()

    async def analyze_incremental_concurrent(
        self,
        messages: list[dict],
        umo: str | None = None,
        topics_per_batch: int = 2,
        quotes_per_batch: int = 1,
        topic_enabled: bool = True,
        golden_quote_enabled: bool = True,
    ) -> tuple[list[SummaryTopic], list[GoldenQuote], TokenUsage]:
        """
        增量分析模式的并发执行方法。
        仅执行话题分析和金句分析，
        使用较小的批次数量以控制单次分析的输出规模。

        Args:
            messages: 本次增量分析的群聊消息列表
            umo: 模型唯一标识符
            topics_per_batch: 本次批次最大话题数量
            quotes_per_batch: 本次批次最大金句数量
            topic_enabled: 是否启用话题分析
            golden_quote_enabled: 是否启用金句分析

        Returns:
            (话题列表, 金句列表, 总Token使用统计)
        """
        try:
            from ...shared.timezone import now as _tz_now

            timestamp = _tz_now().strftime("%Y%m%d_%H%M%S")
            if umo:
                safe_umo = umo.replace(":", "_")
                session_id = f"incr_{timestamp}_{safe_umo}"
            else:
                session_id = f"incr_{timestamp}"

            logger.info(
                f"开始增量并发分析 (话题:{topic_enabled}/{topics_per_batch}, 金句:{golden_quote_enabled}/{quotes_per_batch})，"
                f"消息数量: {len(messages)}，会话ID: {session_id}"
            )

            # 保存原始消息数据 (Debug Mode)
            if self.config_manager.get_debug_mode():
                self._save_debug_messages(messages, session_id)

            # 设置增量模式的最大数量覆盖值
            self.topic_analyzer._incremental_max_count = topics_per_batch
            self.golden_quote_analyzer._incremental_max_count = quotes_per_batch

            try:
                # 构建并发任务列表（仅话题和金句）
                tasks = []
                task_names = []

                if topic_enabled:
                    tasks.append(
                        self.topic_analyzer.analyze_topics(messages, umo, session_id)
                    )
                    task_names.append("topic")

                if golden_quote_enabled:
                    tasks.append(
                        self.golden_quote_analyzer.analyze_golden_quotes(
                            messages, umo, session_id
                        )
                    )
                    task_names.append("golden_quote")

                if not tasks:
                    return [], [], TokenUsage()

                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 处理结果
                topics, topic_usage = [], TokenUsage()
                golden_quotes, quote_usage = [], TokenUsage()

                for i, result in enumerate(results):
                    name = task_names[i]
                    if isinstance(result, Exception):
                        logger.error(f"增量{name}分析失败: {result}")
                        continue

                    if name == "topic" and isinstance(result, tuple):
                        topics, topic_usage = result
                    elif name == "golden_quote" and isinstance(result, tuple):
                        golden_quotes, quote_usage = result

                # 合并Token使用统计
                total_usage = TokenUsage(
                    prompt_tokens=topic_usage.prompt_tokens
                    + quote_usage.prompt_tokens,
                    completion_tokens=topic_usage.completion_tokens
                    + quote_usage.completion_tokens,
                    total_tokens=topic_usage.total_tokens
                    + quote_usage.total_tokens,
                )

                logger.info(
                    f"增量并发分析完成 - 话题: {len(topics)}, 金句: {len(golden_quotes)}, "
                    f"Token消耗: {total_usage.total_tokens}"
                )
                return topics, golden_quotes, total_usage

            finally:
                # 无论成功或失败，都要恢复原始的最大数量设置
                self.topic_analyzer._incremental_max_count = None
                self.golden_quote_analyzer._incremental_max_count = None

        except Exception as e:
            logger.error(f"增量并发分析失败: {e}", exc_info=True)
            return [], [], TokenUsage()

    def _save_debug_messages(self, messages: list[dict], session_id: str):
        """
        保存调试消息数据到文件（Debug Mode 专用）

        隐私提示：本方法会把**群聊原文**（含昵称、用户 ID、消息内容）
        dump 到磁盘。务必保持 debug_mode 默认关闭，并在不需要时及时
        清理（参见 cleanup_debug_data）。

        Args:
            messages: 群聊消息列表
            session_id: 会话ID
        """
        try:
            import json
            import os
            import tempfile

            from astrbot.api.star import StarTools

            debug_dir = StarTools.get_data_dir(PLUGIN_NAME) / "debug_data"
            debug_dir.mkdir(parents=True, exist_ok=True)

            msg_file_path = debug_dir / f"{session_id}_messages.json"
            # 原子写：避免半截 JSON 污染下次读取
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{session_id}_",
                suffix=".tmp",
                dir=str(debug_dir),
                delete=False,
            )
            try:
                with tmp:
                    json.dump(messages, tmp, ensure_ascii=False, indent=2)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp.name, str(msg_file_path))
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

            # 顺手清理过期 debug 文件，避免长期堆积
            # 默认保留 7 天（debug 数据本就是临时排查用，不需要跟 retention_days 一样长）
            self.cleanup_debug_data(max_age_days=7)
        except Exception:
            pass

    def cleanup_debug_data(self, max_age_days: int = 7) -> int:
        """清理 debug_data 目录下超过 max_age_days 天的 dump 文件。

        在两个时机被调用：
        1. 每次 _save_debug_messages 写完后（顺带清旧的）
        2. 插件启动时（main.py._run_initialization），清掉上次运行遗留

        Returns:
            实际删除的文件数。
        """
        try:
            import time as _time

            from astrbot.api.star import StarTools

            debug_dir = StarTools.get_data_dir(PLUGIN_NAME) / "debug_data"
            if not debug_dir.exists():
                return 0

            cutoff = _time.time() - max_age_days * 86400
            deleted = 0
            for entry in debug_dir.iterdir():
                # 只清 .json 文件，避免误删用户其他东西
                if not entry.is_file() or entry.suffix != ".json":
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        deleted += 1
                except OSError as e:
                    logger.warning(f"清理 debug 文件 {entry.name} 失败: {e}")
            if deleted:
                logger.info(f"已清理 {deleted} 个超过 {max_age_days} 天的 debug dump 文件")
            return deleted
        except Exception as e:
            logger.warning(f"cleanup_debug_data 失败: {e}")
            return 0

    # 向后兼容的方法，保持原有调用方式
    async def _call_provider_with_retry(
        self,
        provider,
        prompt: str,
        umo: str | None = None,
        provider_id_key: str | None = None,
    ):
        """
        向后兼容的LLM调用方法
        现在委托给llm_utils模块处理

        Args:
            provider: LLM服务商实例或None（已弃用，现在使用 provider_id_key）
            prompt: 输入的提示语
            umo: 指定使用的模型唯一标识符
            provider_id_key: 配置中的 provider_id 键名（可选）

        Returns:
            LLM生成的结果
        """
        return await call_provider_with_retry(
            self.context,
            self.config_manager,
            prompt,
            umo,
            provider_id_key,
        )

    def _fix_json(self, text: str) -> str:
        """
        向后兼容的JSON修复方法
        现在委托给json_utils模块处理

        Args:
            text: 需要修复的JSON文本

        Returns:
            修复后的JSON文本
        """
        return fix_json(text)
