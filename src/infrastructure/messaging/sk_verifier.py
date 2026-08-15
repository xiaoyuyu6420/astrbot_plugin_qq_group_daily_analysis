"""SK 密钥验真（联网，调 OpenAI 兼容的 /v1/models 接口）。

设计原则：
- 只发 GET /v1/models（只读，不产生费用、不消费额度）
- 每请求超时 8s，任何异常降级为 unknown，不阻塞推送
- trust_env=True 走系统代理（与 generators.py 头像下载同范式）
- 默认关闭（is_verify_sk_real_enabled），用户知情后才开

安全提示：
  验真会用 sk 调外部 API，服务器 IP 会被对方 key 后台记录。
  只调 /v1/models（列出模型），不调 /v1/chat/completions（不产生费用）。
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import aiohttp

from ...utils.logger import logger

# 官方 OpenAI 端点
_OPENAI_BASE = "https://api.openai.com"
# 每请求超时（秒）
_TIMEOUT = 8


def _extract_base_url(source_url: str | None) -> str | None:
    """从消息里的 URL 提取干净的 base_url（scheme://host）。

    用户消息里的中转站地址可能是：
      - https://api.xxx.com/v1
      - api.xxx.com
      - https://xxx.com/chat/completion
    统一提取 scheme://host 作为 base_url。

    只信任 http/https。
    """
    if not source_url:
        return None
    url = source_url.strip()
    if not url:
        return None
    # 补 schema（仅当完全没有 scheme 时）
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        return None


async def _check_endpoint(
    session: aiohttp.ClientSession, base_url: str, sk: str, label: str
) -> str:
    """调单个端点的 /v1/models，返回 valid/invalid/unknown。"""
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {sk}"}
    try:
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            if status == 200:
                logger.info(f"[SkVerifier] {label} 验真：有效（{base_url}, 200）")
                return "valid"
            elif status in (401, 403):
                logger.info(f"[SkVerifier] {label} 验真：无效（{base_url}, {status}）")
                return "invalid"
            else:
                logger.info(f"[SkVerifier] {label} 验真：未知（{base_url}, {status}）")
                return "unknown"
    except asyncio.TimeoutError:
        logger.warning(f"[SkVerifier] {label} 验真超时：{base_url}")
        return "unknown"
    except aiohttp.ClientError as e:
        logger.warning(f"[SkVerifier] {label} 验真网络错误（{base_url}）: {type(e).__name__}: {e}")
        return "unknown"
    except Exception as e:
        logger.warning(f"[SkVerifier] {label} 验真异常（{base_url}）: {type(e).__name__}: {e}")
        return "unknown"


async def verify_sk(
    sk: str, source_url: str | None = None
) -> dict[str, str]:
    """验真一个 sk。

    Args:
        sk: sk- 开头的密钥串
        source_url: 消息里附带的中转站网址（LLM 提取的，可选）

    Returns:
        {"openai": "valid/invalid/unknown", "custom": "valid/invalid/unknown/skipped",
         "custom_base_url": "..."/""}
        - openai：调 api.openai.com 的结果
        - custom：调 source_url 的结果；source_url 为空/无效时为 "skipped"
    """
    result: dict[str, str] = {"openai": "unknown", "custom": "skipped", "custom_base_url": ""}
    if not sk or not sk.strip():
        return result
    sk = sk.strip()

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    # trust_env=True 走系统代理（与 generators.py 同范式）
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            # 1. 官方 OpenAI
            result["openai"] = await _check_endpoint(session, _OPENAI_BASE, sk, "OpenAI官方")

            # 2. 中转站（消息里的网址）
            base_url = _extract_base_url(source_url)
            if base_url and base_url != _OPENAI_BASE:
                result["custom"] = await _check_endpoint(session, base_url, sk, "中转站")
                result["custom_base_url"] = base_url
    except Exception as e:
        logger.error(f"[SkVerifier] 验真整体异常: {type(e).__name__}: {e}")

    return result


def format_verify_result(verify_result: dict[str, str]) -> str:
    """格式化验真结果为推送用的单行文本。"""
    if not verify_result:
        return "验真失败"
    openai_status = verify_result.get("openai", "unknown")
    custom_status = verify_result.get("custom", "skipped")
    status_map = {"valid": "有效", "invalid": "无效", "unknown": "未知", "skipped": "未提供"}

    parts = [f"OpenAI官方 → {status_map.get(openai_status, openai_status)}"]
    if custom_status != "skipped":
        base = verify_result.get("custom_base_url", "")
        parts.append(f"中转站({base}) → {status_map.get(custom_status, custom_status)}")
    else:
        parts.append("中转站 → 未提供")
    return " | ".join(parts)
