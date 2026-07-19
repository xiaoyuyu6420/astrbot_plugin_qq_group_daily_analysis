"""管理员 QQ 解析工具

把「合并 AstrBot 超管 admins_id + 插件 extra_admin_qq → 过滤 → 去重」的逻辑集中一处。
dispatcher（定时报告私聊）与 message_monitor（盯人预警推送）共用，避免散点复制。

注：AstrBot 没有公开 API 读全局 admins_id，只能从 `bot_manager._context.get_config()` 取，
这里把对私有属性的访问限制在本函数内，外部调用方无需关心。
"""

from typing import Any

from ...utils.logger import logger


def resolve_admin_qqs(bot_manager: Any, extra_qqs: list[str]) -> list[str]:
    """合并 AstrBot 超级管理员 + 额外管理员 QQ，过滤掉非数字项并去重。

    Args:
        bot_manager: AstrBot 的 bot_manager 实例（用于读全局 admins_id）
        extra_qqs: 插件配置里的额外管理员 QQ 列表（已是字符串形式）

    Returns:
        去重后的纯数字 QQ 字符串列表（已排除默认占位 "astrbot" 等非数字项）
    """
    qqs: list[str] = []

    # 1. 从 AstrBot 全局配置读超级管理员 admins_id
    try:
        context = getattr(bot_manager, "_context", None)
        if context is not None:
            get_config = getattr(context, "get_config", None)
            if callable(get_config):
                global_config = get_config()
                admins_id = (
                    global_config.get("admins_id", [])
                    if isinstance(global_config, dict)
                    else []
                )
                if isinstance(admins_id, list):
                    qqs.extend(str(x) for x in admins_id)
    except Exception as e:
        logger.warning(f"读取 AstrBot 超管配置失败: {e}")

    # 2. 合并额外管理员 QQ
    qqs.extend(extra_qqs)

    # 3. 过滤：只保留纯数字（QQ 号），去重，排除默认占位 "astrbot"
    seen: set[str] = set()
    result: list[str] = []
    for q in qqs:
        q_clean = str(q).strip()
        if q_clean.isdigit() and q_clean not in seen:
            seen.add(q_clean)
            result.append(q_clean)
    return result
