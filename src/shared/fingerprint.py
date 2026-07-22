"""内容指纹工具。

供降噪层、分层聚合、定时分类摘要等去重场景复用，避免散落的
「归一化 → sha256 → 截断」实现发生漂移（历史上曾出现 16/32 字符、
是否去标点等不一致）。
"""

from __future__ import annotations

import hashlib
import re

# 默认截断长度：sha256 hex 前 32 字符（128 bit），碰撞概率足够低
DEFAULT_DIGEST_LEN = 32


def content_fingerprint(text: str, digest_len: int = DEFAULT_DIGEST_LEN) -> str:
    """计算内容指纹：归一化后取 sha256 前若干字符。

    归一化步骤（与历史 noise_reducer / layered_aggregation 一致）：
    1. 去除所有空白字符
    2. 转小写
    3. 去除所有非字母数字字符（标点、符号）

    Args:
        text: 原始文本。None / 空串返回同一指纹。
        digest_len: 截断长度，默认 32。

    Returns:
        指纹字符串（小写 hex）。
    """
    normalized = re.sub(r"\s+", "", text or "").lower()
    normalized = re.sub(r"[^\w]", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[
        :digest_len
    ]
