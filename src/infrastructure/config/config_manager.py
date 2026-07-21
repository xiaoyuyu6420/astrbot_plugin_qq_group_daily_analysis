"""
配置管理模块 - 基础设施层
负责处理插件配置
"""

from astrbot.api import AstrBotConfig
from astrbot.api.star import StarTools

from ...shared.constants import PLUGIN_NAME
from ...utils.logger import logger
from ..utils.template_utils import upgrade_str_format_template


class ConfigManager:
    """配置管理器

    配置结构采用分组嵌套方式，顶层分为以下分组：
    - basic: 基础设置
    - auto_analysis: 自动分析设置
    - llm: LLM 设置
    - analysis_features: 分析功能开关
    - incremental: 增量分析设置
    - prompts: 提示词模板
    """

    def __init__(self, config: AstrBotConfig):
        self.config = config

    def _get_group(self, group: str) -> dict:
        """获取指定分组的配置字典，不存在时返回空字典"""
        return self.config.get(group, {})

    def _ensure_group(self, group: str) -> dict:
        """确保指定分组存在并返回其字典引用"""
        if group not in self.config:
            self.config[group] = {}
        return self.config[group]

    def get_group_list_mode(self) -> str:
        """获取群组列表模式 (whitelist/blacklist/none)"""
        # 定制版默认 whitelist，与 _conf_schema.json 保持一致
        return self._get_group("basic").get("group_list_mode", "whitelist")

    def get_group_list(self) -> list[str]:
        """获取群组列表（用于黑白名单）"""
        return self._get_group("basic").get("group_list", [])

    def is_group_allowed(self, group_id_or_umo: str) -> bool:
        """
        根据配置的白/黑名单判断是否允许在该群聊中使用
        支持传入 simple group_id 或 UMO (Unified Message Origin)
        """
        mode = self.get_group_list_mode().lower()
        if mode not in ("whitelist", "blacklist", "none"):
            mode = "none"

        if mode == "none":
            return True

        glist = [str(g).strip() for g in self.get_group_list()]
        target = str(group_id_or_umo).strip()

        is_in_list = any(self._is_group_match(target, item) for item in glist)

        if mode == "whitelist":
            return is_in_list
        if mode == "blacklist":
            return not is_in_list

        return True

    def _is_group_match(self, target: str, item: str) -> bool:
        """
        核心匹配逻辑：判断名单中的 item 是否匹配目标的 target (Unified Message Origin, UMO 或 纯 ID)。
        支持处理 Telegram 话题 (#) 和 独立隔离会话 (_) 的双向穿透匹配。
        """
        if item == target:
            return True

        # 分解目标 UMO 的前缀和 ID 部分 (如 default:GroupMessage:ID)
        if ":" in target:
            target_prefix, target_id = target.rsplit(":", 1)
        else:
            target_prefix, target_id = "", target

        # 生成目标 ID 的所有“穿透”候选 (处理隔离模式和话题)
        candidates = {target_id}
        if "#" in target_id:
            candidates.add(target_id.split("#", 1)[0])
        if "_" in target_id:
            for part in target_id.split("_"):
                candidates.add(part)

        # 检查名单项 (item) 的格式
        if ":" in item:
            i_prefix, i_id = item.rsplit(":", 1)
            # 名单项带前缀时，前缀必须匹配 (如果 target 本身没前缀，则允许作为跨平台通用 ID 匹配)
            if target_prefix and i_prefix != target_prefix:
                return False
        else:
            i_id = item

        # [修复] 名单项 ID 也可能包含复合形式 (如 UserId_GroupId)，需要拆解匹配
        item_variants = {i_id}
        if "#" in i_id:
            item_variants.add(i_id.split("#", 1)[0])
        if "_" in i_id:
            for part in i_id.split("_"):
                item_variants.add(part)

        # 只要两边的 ID “核心部分”存在交集，即视为匹配成功
        return not item_variants.isdisjoint(candidates)

    def get_max_messages(self) -> int:
        """获取最大消息数量"""
        return self._get_group("basic").get("max_messages", 1000)

    def get_analysis_days(self) -> int:
        """获取分析天数"""
        return self._get_group("basic").get("analysis_days", 1)

    def get_auto_analysis_time(self) -> list[str]:
        """获取自动分析时间列表"""
        group = self._get_group("auto_analysis")
        val = group.get("auto_analysis_time", ["09:00"])
        # 兼容旧版本字符串配置
        if isinstance(val, str):
            val_list = [val]
            # 自动修复配置格式
            try:
                auto_group = self._ensure_group("auto_analysis")
                auto_group["auto_analysis_time"] = val_list
                self.config.save_config()
                logger.info(f"自动修复配置格式 auto_analysis_time: {val} -> {val_list}")
            except Exception as e:
                logger.warning(f"修复配置格式失败: {e}")
            return val_list
        return val if isinstance(val, list) else ["09:00"]

    def get_enable_auto_analysis(self) -> bool:
        """
        获取是否启用自动分析（兼容旧接口）。

        旧版本使用 auto_analysis.enable_auto_analysis 布尔值；
        新版本改为由 scheduled_group_list_mode + scheduled_group_list 推导。
        """
        return self.is_auto_analysis_enabled()

    def get_output_format(self) -> str:
        """获取输出格式"""
        return self._get_group("basic").get("output_format", "image")

    def get_min_messages_threshold(self) -> int:
        """获取最小消息阈值"""
        return self._get_group("basic").get("min_messages_threshold", 50)

    def get_topic_analysis_enabled(self) -> bool:
        """获取是否启用话题分析"""
        return self._get_group("analysis_features").get("topic_analysis_enabled", True)

    def get_golden_quote_analysis_enabled(self) -> bool:
        """获取是否启用信息差/干货提取（原金句分析）"""
        return self._get_group("analysis_features").get(
            "golden_quote_analysis_enabled", True
        )

    def get_max_topics(self) -> int:
        """获取最大话题数量"""
        return self._get_group("analysis_features").get("max_topics", 5)

    def get_max_golden_quotes(self) -> int:
        """获取最大金句数量"""
        return self._get_group("analysis_features").get("max_golden_quotes", 5)

    def get_llm_retries(self) -> int:
        """获取LLM请求重试次数"""
        return self._get_group("llm").get("llm_retries", 2)

    def get_llm_backoff(self) -> int:
        """获取LLM请求重试退避基值（秒），实际退避会乘以尝试次数"""
        return self._get_group("llm").get("llm_backoff", 2)

    def get_enable_streaming_llm_call(self) -> bool:
        """获取是否启用流式 LLM 调用"""
        return self._get_group("llm").get("enable_streaming_llm_call", False)

    def get_debug_mode(self) -> bool:
        """获取是否启用调试模式"""
        return self._get_group("basic").get("debug_mode", False)

    def get_enable_base64_image(self) -> bool:
        """获取是否启用 Base64 图片传输"""
        return self._get_group("basic").get("enable_base64_image", False)

    def get_t2i_rendering_strategies(self) -> list[dict]:
        """获取用户配置的两轮 T2I 渲染策略"""
        group = self._get_group("t2i_rendering")

        return [
            # 第一轮：质量优先
            {
                "full_page": True,
                "type": group.get("t2i_r1_type", "png"),
                "quality": group.get("t2i_r1_quality", 100),
                "device_scale_factor_level": group.get("t2i_r1_device_scale", "ultra"),
                "timeout": group.get("t2i_r1_timeout", 30000),
            },
            # 第二轮：稳定性/回退优先
            {
                "full_page": True,
                "type": group.get("t2i_r2_type", "jpeg"),
                "quality": group.get("t2i_r2_quality", 80),
                "device_scale_factor_level": group.get("t2i_r2_device_scale", "normal"),
                "timeout": group.get("t2i_r2_timeout", 60000),
            },
        ]

    def get_t2i_font_source(self) -> str:
        """获取 T2I 字体源 (Mainland/Overseas)"""
        return self._get_group("t2i_rendering").get("t2i_font_source", "Overseas")

    def get_t2i_google_fonts_mirror(self) -> str:
        """根据环境选择获取 Google Fonts 镜像地址"""
        source = self.get_t2i_font_source()
        group = self._get_group("t2i_rendering")
        if source == "Mainland":
            return group.get("t2i_mainland_google_fonts", "https://fonts.loli.net")
        return group.get("t2i_overseas_google_fonts", "https://fonts.googleapis.com")

    def get_t2i_gstatic_mirror(self) -> str:
        """根据环境选择获取 Gstatic 镜像地址"""
        source = self.get_t2i_font_source()
        group = self._get_group("t2i_rendering")
        if source == "Mainland":
            return group.get("t2i_mainland_gstatic", "https://gstatic.loli.net")
        return group.get("t2i_overseas_gstatic", "https://fonts.gstatic.com")

    def get_t2i_atri_font_mirror(self) -> str:
        """获取 ATRI 主题字体镜像地址 (目前保持不变，如有需要可后续添加 Mainland/Overseas 配置)"""
        return self._get_group("t2i_rendering").get(
            "t2i_atri_font_mirror", "https://tc.ciallo.ccwu.cc"
        )

    def get_llm_provider_id(self) -> str:
        """获取主 LLM Provider ID"""
        return self._get_group("llm").get("llm_provider_id", "")

    def get_topic_provider_id(self) -> str:
        """获取话题分析专用 Provider ID"""
        return self._get_group("llm").get("topic_provider_id", "")

    def get_golden_quote_provider_id(self) -> str:
        """获取金句分析专用 Provider ID"""
        return self._get_group("llm").get("golden_quote_provider_id", "")

    def get_keep_original_persona(self) -> bool:
        """获取是否继承会话原始人格设定"""
        return self._get_group("analysis_features").get("keep_original_persona", False)

    def get_use_plugin_specific_persona(self) -> bool:
        """获取是否强制使用插件指定的人格设定"""
        return self._get_group("analysis_features").get(
            "use_plugin_specific_persona", False
        )

    def get_plugin_specific_persona_id(self) -> str:
        """获取插件指定的全局人格 ID (通过 select_persona 接口选择)"""
        return self._get_group("analysis_features").get(
            "plugin_specific_persona_id", ""
        )

    def get_bot_self_ids(self) -> list:
        """获取机器人自身的 ID 列表 (兼容 bot_qq_ids)"""
        basic = self._get_group("basic")
        ids = basic.get("bot_self_ids", [])
        if not ids:
            ids = basic.get("bot_qq_ids", [])
        return ids

    def get_html_output_dir(self) -> str:
        """获取HTML输出目录"""

        default_path = StarTools.get_data_dir(PLUGIN_NAME) / "self_hosted_html_reports"
        val = self._get_group("html").get("html_output_dir")
        return val if val else str(default_path)

    def get_html_base_url(self) -> str:
        """获取HTML外链Base URL"""
        return self._get_group("html").get("html_base_url", "")

    def get_html_only_url(self) -> bool:
        """获取是否仅输出外链而不发送文件本体"""
        return self._get_group("html").get("html_only_url", False)

    def set_html_only_url(self, enabled: bool):
        """设置是否仅输出外链而不发送文件本体"""
        self._ensure_group("html")["html_only_url"] = enabled
        self.config.save_config()

    def get_html_filename_format(self) -> str:
        """获取HTML文件名格式"""
        return self._get_group("html").get(
            "html_filename_format", "群聊分析报告_{group_id}_{date}.html"
        )

    def get_topic_analysis_prompt(self, style: str = "topic_prompt") -> str:
        """获取话题分析提示词模板"""
        prompts_config = self._get_group("prompts").get("topic_analysis_prompts", {})
        prompt = prompts_config.get(style, "")
        if prompt:
            return prompt
        return ""

    def get_golden_quote_analysis_prompt(
        self, style: str = "golden_quote_v2_prompt"
    ) -> str:
        """获取金句分析提示词模板"""
        prompts_config = self._get_group("prompts").get(
            "golden_quote_analysis_prompts", {}
        )
        prompt = prompts_config.get(style, "")
        if prompt:
            return prompt
        return ""

    def _upgrade_config_item(self, group: str, key: str, setter_func):
        """升级指定配置项的值（从 str.format -> string.Template），并回写。"""
        # 如果是 prompts，则先取 prompts 分组，再取子分组 (group)
        if group in (
            "topic_analysis_prompts",
            "golden_quote_analysis_prompts",
        ):
            target_group = self._get_group("prompts").get(group, {})
        else:
            target_group = self._get_group(group)

        val = target_group.get(key, "")
        if not val or not isinstance(val, str):
            return False

        upgraded_val, upgraded = upgrade_str_format_template(val)
        if upgraded and upgraded_val != val:
            setter_func(upgraded_val)
            logger.info(
                f"配置项 {group}.{key} 发现旧版语法并已自动升级为 string.Template 格式。"
            )
            return True
        return False

    def upgrade_prompt_templates(self):
        """启动时调用，扫描并升级所有可配置的模板（含 prompt 和文件名）。"""
        modified = False
        # 1. 提示词模板升级
        modified |= self._upgrade_config_item(
            "topic_analysis_prompts",
            "topic_prompt",
            self.set_topic_analysis_prompt,
        )
        modified |= self._upgrade_config_item(
            "golden_quote_analysis_prompts",
            "golden_quote_v2_prompt",
            self.set_golden_quote_analysis_prompt,
        )

        # 2. 文件名格式升级
        modified |= self._upgrade_config_item(
            "html",
            "html_filename_format",
            self.set_html_filename_format,
        )

        if modified:
            logger.info(
                "已完成所有配置模板从 str.format 到 string.Template 的安全迁移。（已自动回写配置）"
            )
        return modified

    def set_topic_analysis_prompt(self, prompt: str):
        """设置话题分析提示词模板"""
        prompts = self._ensure_group("prompts")
        if "topic_analysis_prompts" not in prompts:
            prompts["topic_analysis_prompts"] = {}
        prompts["topic_analysis_prompts"]["topic_prompt"] = prompt
        self.config.save_config()

    def set_golden_quote_analysis_prompt(self, prompt: str):
        """设置金句分析提示词模板"""
        prompts = self._ensure_group("prompts")
        if "golden_quote_analysis_prompts" not in prompts:
            prompts["golden_quote_analysis_prompts"] = {}
        prompts["golden_quote_analysis_prompts"]["golden_quote_v2_prompt"] = prompt
        self.config.save_config()

    def set_output_format(self, format_type: str):
        """设置输出格式"""
        valid_formats = ["image", "text", "html"]
        if format_type.lower() not in valid_formats:
            raise ValueError(
                f"无效的输出格式: {format_type}。有效选项: {valid_formats}"
            )

        self._ensure_group("basic")["output_format"] = format_type.lower()
        self.config.save_config()

    def set_group_list_mode(self, mode: str):
        """设置群组列表模式"""
        self._ensure_group("basic")["group_list_mode"] = mode
        self.config.save_config()

    def set_group_list(self, groups: list[str]):
        """设置群组列表"""
        self._ensure_group("basic")["group_list"] = groups
        self.config.save_config()

    def get_max_concurrent_tasks(self) -> int:
        """获取自动分析最大并发群数"""
        return self._get_group("performance").get("max_concurrent_groups", 3)

    def get_llm_max_concurrent(self) -> int:
        """获取全局 LLM 最大并发请求数"""
        return self._get_group("performance").get("max_concurrent_llm", 3)

    def get_t2i_max_concurrent(self) -> int:
        """获取全局图片渲染（T2I）最大并发数"""
        return self._get_group("performance").get("max_concurrent_t2i", 1)

    def get_stagger_seconds(self) -> int:
        """获取多群分析任务启动时的交错间隔（秒）"""
        return self._get_group("performance").get("stagger_seconds", 2)

    def set_max_concurrent_tasks(self, count: int):
        """设置自动分析最大并发数"""
        self._ensure_group("performance")["max_concurrent_groups"] = count
        self.config.save_config()

    def set_max_messages(self, count: int):
        """设置最大消息数量"""
        self._ensure_group("basic")["max_messages"] = count
        self.config.save_config()

    def set_analysis_days(self, days: int):
        """设置分析天数"""
        self._ensure_group("basic")["analysis_days"] = days
        self.config.save_config()

    def set_auto_analysis_time(self, time_val: str | list[str]):
        """设置自动分析时间点"""
        self._ensure_group("auto_analysis")["auto_analysis_time"] = time_val
        self.config.save_config()

    def get_scheduled_group_list_mode(self) -> str:
        """获取定时分析名单模式 (whitelist/blacklist)"""
        return self._get_group("auto_analysis").get(
            "scheduled_group_list_mode", "whitelist"
        )

    def set_scheduled_group_list_mode(self, mode: str):
        """设置定时分析名单模式"""
        self._ensure_group("auto_analysis")["scheduled_group_list_mode"] = mode
        self.config.save_config()

    def get_scheduled_group_list(self) -> list[str]:
        """获取定时分析目标群列表（仅 delivery_mode=per_group）"""
        return self._get_group("auto_analysis").get("scheduled_group_list", [])

    def get_delivery_mode(self) -> str:
        """定时推送形态：per_group（单群完整日报）或 by_category（用户分类聚合）。

        缺省/非法值回退 per_group，兼容旧配置。
        """
        mode = str(
            self._get_group("auto_analysis").get("delivery_mode", "per_group")
        ).strip().lower()
        if mode not in ("per_group", "by_category"):
            return "per_group"
        return mode

    def get_category_push_mode(self) -> str:
        """分类聚合推送形态：split（每分类一条）或 merged（一条分块）。"""
        mode = str(
            self._get_group("auto_analysis").get("category_push_mode", "split")
        ).strip().lower()
        if mode not in ("split", "merged"):
            return "split"
        return mode

    def get_push_categories(self) -> list:
        """解析用户分类列表，归一化为 list[PushCategory]。

        支持：
        - list[dict]：[{"name":"科技","groups":["1","2"]}, ...]
        - JSON 字符串（面板 text 编辑器）
        无效项跳过；空 name / 无 groups 的分类丢弃。
        """
        from ...domain.entities.push_category import normalize_push_categories

        raw = self._get_group("auto_analysis").get("categories", [])
        return normalize_push_categories(raw)

    def is_auto_analysis_enabled(self) -> bool:
        """判断定时分析是否应按配置开启。

        - by_category：categories 非空即视为开启
        - per_group：白名单非空或黑名单模式
        """
        if self.get_delivery_mode() == "by_category":
            return len(self.get_push_categories()) > 0
        mode = self.get_scheduled_group_list_mode()
        lst = self.get_scheduled_group_list()
        return (mode == "whitelist" and len(lst) > 0) or (mode == "blacklist")

    # ==================== 管理员私聊通知配置 ====================

    def is_admin_notify_enabled(self) -> bool:
        """是否开启「定时报告私聊管理员」模式（开启后不发群，只私聊管理员）"""
        # 定制版默认开启，与 _conf_schema.json 保持一致
        return bool(self._get_group("admin_notify").get("enable_admin_notify", True))

    def get_extra_admin_qqs(self) -> list[str]:
        """获取额外管理员 QQ 列表（除 AstrBot 超管外，额外接收报告的人）"""
        raw = self._get_group("admin_notify").get("extra_admin_qq", [])
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    # ==================== 实时消息监控配置 ====================

    def is_monitor_enabled(self) -> bool:
        """是否开启实时消息监控（盯人预警）"""
        return bool(self._get_group("message_monitor").get("enable_monitor", False))

    def get_monitor_mode(self) -> str:
        """监控触发方式：keyword（关键词即时）或 window（整窗汇总，默认）。

        与 window_scope（单群/多群）正交：先选触发方式，window 再选分析范围。
        """
        mode = str(self._get_group("message_monitor").get("monitor_mode", "window")).strip().lower()
        if mode not in ("keyword", "window"):
            mode = "window"
        return mode

    def get_window_scope(self) -> str:
        """window 模式的分析范围：per_group（单群独立）或 cross_group（多群汇总）。

        兼容旧配置 enable_cross_group：
        - 若显式配置了 window_scope，以之为准
        - 否则 enable_cross_group=true → cross_group，false/缺省 → per_group
        """
        group = self._get_group("message_monitor")
        raw = str(group.get("window_scope", "")).strip().lower()
        if raw in ("per_group", "cross_group"):
            return raw
        # 旧键兼容
        if bool(group.get("enable_cross_group", False)):
            return "cross_group"
        return "per_group"

    def get_monitored_qqs(self) -> list[str]:
        """要监控的 QQ 号列表"""
        raw = self._get_group("message_monitor").get("monitored_qqs", [])
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    def get_monitored_groups(self) -> list[str]:
        """限定监控的群号列表（空=不限群）"""
        raw = self._get_group("message_monitor").get("monitored_groups", [])
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    def get_monitor_extra_keywords(self) -> list[str]:
        """自定义监控关键词"""
        raw = self._get_group("message_monitor").get("extra_keywords", [])
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    def is_llm_confirm_enabled(self) -> bool:
        """每个批次是否用 LLM 提取有价值信息"""
        return bool(self._get_group("message_monitor").get("use_llm_confirm", True))

    def get_flush_interval(self) -> int:
        """批量汇总间隔（分钟）。每隔这么久把积攒的消息总结推送一次。"""
        try:
            val = int(self._get_group("message_monitor").get("flush_interval", 10))
            return max(1, val)  # 至少 1 分钟
        except (TypeError, ValueError):
            return 10

    def get_max_context_messages(self) -> int:
        """送给 LLM 的对话窗口最大条数。超出则只保留目标 QQ 发言附近的上下文。"""
        try:
            val = int(
                self._get_group("message_monitor").get("max_context_messages", 50)
            )
            return max(10, val)  # 至少 10 条，太少没上下文意义
        except (TypeError, ValueError):
            return 50

    def get_alert_admin_qqs(self) -> list[str]:
        """预警推送目标 QQ（空=回退到管理员列表）"""
        raw = self._get_group("message_monitor").get("alert_admin_qqs", [])
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    def is_cross_group_enabled(self) -> bool:
        """是否开启跨群聚合简报（window 模式下合并多群输出）。

        新配置读 window_scope；旧配置 enable_cross_group 仍兼容。
        """
        return self.get_window_scope() == "cross_group"

    def get_cooldown_seconds(self) -> int:
        """keyword 模式推送冷却间隔（秒）。同一发送者@同一群在此期间不重复推送。0=不冷却。"""
        try:
            val = int(self._get_group("message_monitor").get("cooldown_seconds", 60))
            return max(0, val)
        except (TypeError, ValueError):
            return 60

    def get_dedup_minutes(self) -> int:
        """内容去重窗口（分钟）。内容相似的消息在此窗口内只推一次。0=不去重。"""
        try:
            val = int(self._get_group("message_monitor").get("dedup_minutes", 30))
            return max(0, val)
        except (TypeError, ValueError):
            return 30

    def get_keyword_batch_seconds(self) -> int:
        """keyword 模式 normal 优先级批量合并间隔（秒）。0=不合并（立即推）。"""
        try:
            val = int(
                self._get_group("message_monitor").get("keyword_batch_seconds", 60)
            )
            return max(0, val)
        except (TypeError, ValueError):
            return 60

    # ==================== 分层聚合 / 分类频道 ====================

    def get_aggregation_mode(self) -> str:
        """跨群聚合方式：layered（分层，默认）或 legacy（单次 LLM）。"""
        mode = str(self._get_group("message_monitor").get("aggregation_mode", "layered")).strip().lower()
        return mode if mode in ("layered", "legacy") else "layered"

    def get_channel_push_mode(self) -> str:
        """分类频道推送形态：split（每频道一条，默认）或 merged（合并总简报）。"""
        mode = str(self._get_group("message_monitor").get("channel_push_mode", "split")).strip().lower()
        return mode if mode in ("split", "merged") else "split"

    def get_channels_enabled(self) -> list[str]:
        """启用的频道列表。空=用默认全开（不含 other）。"""
        raw = self._get_group("message_monitor").get("channels_enabled", [])
        if not isinstance(raw, list):
            raw = [raw]
        from ...domain.services.intel_taxonomy import ALL_CHANNELS, DEFAULT_ENABLED_CHANNELS

        channels = [str(x).strip().lower() for x in raw if str(x).strip()]
        channels = [c for c in channels if c in ALL_CHANNELS]
        return channels or list(DEFAULT_ENABLED_CHANNELS)

    def get_max_items_per_channel(self) -> int:
        """每频道最多保留条数。"""
        try:
            val = int(
                self._get_group("message_monitor").get("max_items_per_channel", 5)
            )
            return max(1, val)
        except (TypeError, ValueError):
            return 5

    def get_max_candidates_per_group(self) -> int:
        """每群 L1 最多候选条数。"""
        try:
            val = int(
                self._get_group("message_monitor").get("max_candidates_per_group", 5)
            )
            return max(1, val)
        except (TypeError, ValueError):
            return 5

    def is_critical_instant_push_enabled(self) -> bool:
        """window 模式下 critical 是否秒推（不等 flush）。默认 True。"""
        return bool(
            self._get_group("message_monitor").get("critical_instant_push", True)
        )

    def is_l1_use_llm_enabled(self) -> bool:
        """L1 是否调用 LLM 做提炼/合并。默认 False（50+ 群省钱）。"""
        return bool(self._get_group("message_monitor").get("l1_use_llm", False))

    def get_l1_parallel_groups(self) -> int:
        """L1 并发提炼的群数上限。默认 8。"""
        try:
            val = int(self._get_group("message_monitor").get("l1_parallel_groups", 8))
            return max(1, val)
        except (TypeError, ValueError):
            return 8

    def get_l2_shard_threshold(self) -> int:
        """L2 触发分片的 candidate 阈值。默认 60（超过则按频道分批调 LLM）。"""
        try:
            val = int(self._get_group("message_monitor").get("l2_shard_threshold", 60))
            return max(10, val)
        except (TypeError, ValueError):
            return 60

    def get_push_max_chars(self) -> int:
        """单条推送最大字符数，超出分页。默认 1800。"""
        try:
            val = int(self._get_group("message_monitor").get("push_max_chars", 1800))
            return max(500, val)
        except (TypeError, ValueError):
            return 1800

    def set_scheduled_group_list(self, groups: list[str]):
        """设置定时分析目标群列表"""
        self._ensure_group("auto_analysis")["scheduled_group_list"] = groups
        self.config.save_config()

    def is_group_in_filtered_list(
        self, group_umo_or_id: str, mode: str, group_list: list
    ) -> bool:
        """
        通用的名单判定逻辑。

        逻辑如下：
        - whitelist 模式：
            - 如果列表为空，则视为“此级别未开启”。
            - 如果不为空，仅在列表中的通过。
        - blacklist 模式：
            - 在列表中的不通过。
            - 如果列表为空，则全部通过。
        """
        group_list = [str(x).strip() for x in group_list]
        target = str(group_umo_or_id).strip()

        if mode == "whitelist":
            if not group_list:
                # 白名单为空：此级别不开启 (按需开启逻辑)
                return False
            return any(self._is_group_match(target, item) for item in group_list)
        else:  # blacklist
            if not group_list:
                # 黑名单为空：全通过
                return True
            return not any(self._is_group_match(target, item) for item in group_list)

    def set_min_messages_threshold(self, threshold: int):
        """设置最小消息阈值"""
        self._ensure_group("basic")["min_messages_threshold"] = threshold
        self.config.save_config()

    def set_topic_analysis_enabled(self, enabled: bool):
        """设置是否启用话题分析"""
        self._ensure_group("analysis_features")["topic_analysis_enabled"] = enabled
        self.config.save_config()

    def set_golden_quote_analysis_enabled(self, enabled: bool):
        """设置是否启用信息差/干货提取（原金句分析）"""
        self._ensure_group("analysis_features")["golden_quote_analysis_enabled"] = (
            enabled
        )
        self.config.save_config()

    def set_max_topics(self, count: int):
        """设置最大话题数量"""
        self._ensure_group("analysis_features")["max_topics"] = count
        self.config.save_config()

    def set_max_golden_quotes(self, count: int):
        """设置最大金句数量"""
        self._ensure_group("analysis_features")["max_golden_quotes"] = count
        self.config.save_config()

    def set_html_filename_format(self, format_str: str):
        """设置HTML文件名格式"""
        self._ensure_group("html")["html_filename_format"] = format_str
        self.config.save_config()

    def get_report_template(self) -> str:
        """获取报告模板名称"""
        return self._get_group("basic").get("report_template", "scrapbook")

    def set_report_template(self, template_name: str):
        """设置报告模板名称"""
        self._ensure_group("basic")["report_template"] = template_name
        self.config.save_config()

    def get_enable_user_card(self) -> bool:
        """获取是否使用用户群名片"""
        return self._get_group("basic").get("enable_user_card", False)

    def get_enable_analysis_reply(self) -> bool:
        """获取是否在群分析完成后发送文本回复"""
        return self._get_group("basic").get("enable_analysis_reply", False)

    def set_enable_analysis_reply(self, enabled: bool):
        """设置是否在群分析完成后发送文本回复"""
        self._ensure_group("basic")["enable_analysis_reply"] = enabled
        self.config.save_config()

    # ========== 群文件/群相册上传配置 ==========

    def get_enable_group_file_upload(self) -> bool:
        """获取是否启用群文件上传"""
        return self._get_group("qq_group_upload").get("enable_group_file_upload", False)

    def get_group_file_folder(self) -> str:
        """获取群文件上传目录名，空字符串表示根目录"""
        return self._get_group("qq_group_upload").get("group_file_folder", "")

    def get_enable_group_album_upload(self) -> bool:
        """获取是否启用群相册上传（仅 NapCat）"""
        return self._get_group("qq_group_upload").get(
            "enable_group_album_upload", False
        )

    def get_group_album_name(self) -> str:
        """获取目标群相册名称，空字符串表示默认相册"""
        return self._get_group("qq_group_upload").get("group_album_name", "")

    def get_group_album_strict_mode(self) -> bool:
        """获取群相册上传严格模式开关。"""
        return bool(
            self._get_group("qq_group_upload").get("group_album_strict_mode", True)
        )

    def set_group_album_strict_mode(self, enabled: bool):
        """设置群相册上传严格模式"""
        self._ensure_group("qq_group_upload")["group_album_strict_mode"] = enabled
        self.config.save_config()

    # ========== 增量分析配置 ==========

    def get_incremental_enabled(self) -> bool:
        """获取是否开启了增量分析（由名单状态决定）"""
        mode = self.get_incremental_group_list_mode()
        lst = self.get_incremental_group_list()
        # 如果是白名单且不为空，或者是黑名单模式，则视为功能“开启”
        return (mode == "whitelist" and len(lst) > 0) or (mode == "blacklist")

    def get_incremental_group_list_mode(self) -> str:
        """获取增量分析名单模式 (whitelist/blacklist)"""
        return self._get_group("incremental").get(
            "incremental_group_list_mode", "whitelist"
        )

    def get_incremental_group_list(self) -> list[str]:
        """获取增量分析群列表"""
        return self._get_group("incremental").get("incremental_group_list", [])

    def get_incremental_fallback_enabled(self) -> bool:
        """获取增量分析失败回退到全量分析的开关（默认启用）"""
        return self._get_group("incremental").get("incremental_fallback_enabled", True)

    def get_incremental_report_immediately(self) -> bool:
        """获取是否启用增量分析立即发送报告（调试用）"""
        return self._get_group("incremental").get(
            "incremental_report_immediately", False
        )

    def set_incremental_report_immediately(self, enabled: bool):
        """设置增量分析是否立即发送报告"""
        self._ensure_group("incremental")["incremental_report_immediately"] = enabled
        self.config.save_config()

    def get_incremental_interval_minutes(self) -> int:
        """获取增量分析间隔（分钟）"""
        return self._get_group("incremental").get("incremental_interval_minutes", 120)

    def get_incremental_max_daily_analyses(self) -> int:
        """获取每天最大增量分析次数"""
        return self._get_group("incremental").get("incremental_max_daily_analyses", 8)

    def get_incremental_safe_limit(self) -> int:
        """获取单次增量分析的安全分析/同步上限 (Safe Count)"""
        return self._get_group("incremental").get("incremental_safe_limit", 2000)

    def get_incremental_min_messages(self) -> int:
        """获取触发增量分析的最小消息数阈值"""
        return self._get_group("incremental").get("incremental_min_messages", 20)

    def get_incremental_topics_per_batch(self) -> int:
        """获取单次增量分析提取的最大话题数"""
        return self._get_group("incremental").get("incremental_topics_per_batch", 3)

    def get_incremental_quotes_per_batch(self) -> int:
        """获取单次增量分析提取的最大金句数"""
        return self._get_group("incremental").get("incremental_quotes_per_batch", 3)

    def get_incremental_active_start_hour(self) -> int:
        """获取增量分析活跃时段起始小时（24小时制）"""
        return self._get_group("incremental").get("incremental_active_start_hour", 8)

    def get_incremental_active_end_hour(self) -> int:
        """获取增量分析活跃时段结束小时（24小时制）"""
        return self._get_group("incremental").get("incremental_active_end_hour", 23)

    def get_incremental_stagger_seconds(self) -> int:
        """获取多群增量分析的交错间隔（秒），避免 API 压力"""
        return self._get_group("incremental").get("incremental_stagger_seconds", 30)

    def save_config(self):
        """保存配置到AstrBot配置系统"""
        try:
            self.config.save_config()
            logger.info("配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def get_timezone(self) -> str:
        """获取配置的时区名（IANA，如 Asia/Shanghai）。

        留空或非法由 shared.timezone.configure_timezone 兜底回退，
        这里只如实返回用户填的值。
        """
        return self._get_group("basic").get("timezone", "") or ""

    def reload_config(self, on_applied=None):
        """热更新入口。

        背景：AstrBot 没有官方 on_config_updated 钩子（Star 基类只有
        initialize/terminate），ConfigManager.config 本身是 dict 引用，
        所有 getter 已经是实时读取 —— 配置值本身是热的。
        真正"改完要重启才生效"的只有两类一次性消费：
            1) GlobalRateLimiter（启动时调用 get_instance 设并发上限）
            2) AutoScheduler 注册的 APScheduler cron 任务
        以及时区缓存（shared.timezone._active_tz）。

        本函数做三件事：
            1. configure_timezone(get_timezone()) 刷时区缓存
            2. 调用 on_applied 回调（由 main.py 注入：重排调度 + 重配限流器）
            3. 日志记录

        Args:
            on_applied: 可选的回调，签名 () -> None。由 main.py 在初始化时
                注入，避免 ConfigManager 反向依赖 auto_scheduler/resilience。
        """
        try:
            logger.info("重新加载配置...")
            # 1. 时区（即使没回调也要刷 —— 业务模块靠 shared.timezone.now()）
            from ...shared.timezone import configure_timezone

            tz_name = configure_timezone(self.get_timezone())
            logger.info("时区生效: %s", tz_name)
            # 2. 让 main.py 注入的回调做剩下的重活（重排调度、重配限流器）
            if on_applied is not None:
                on_applied()
            logger.info("配置重载完成")
        except Exception as e:
            logger.error(f"重新加载配置失败: {e}")
