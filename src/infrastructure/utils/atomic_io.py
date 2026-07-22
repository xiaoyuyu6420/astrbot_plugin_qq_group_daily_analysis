"""原子 JSON 写入工具。

把「同目录临时文件 + fsync + os.replace」的写法抽成一处，避免
`history_repository` 与 `llm_analyzer` 等多处逐行复制实现（历史曾出现
两边实现几乎一致、各自漂移的情况）。

背景：直接 ``open(path, "w")`` 写入，如果进程在写到一半被终止
（AstrBot 重载、SIGKILL、断电），文件会留下半截 JSON，下次
``json.load`` 抛异常，该数据就废了。原子写保证：要么完整的旧文件、
要么完整的新文件，绝不出现中间态。

实现要点：
- 临时文件用 ``tempfile.NamedTemporaryFile`` 创建在**目标同目录**下
  （跨分区 ``os.replace`` 会退化成复制+删除，失去原子性）
- ``fsync`` 在 close 前调用，保证数据落盘
- ``os.replace`` 在 POSIX 上是原子的；Windows 上同盘也是原子
- 任何异常都会清理临时文件，不留垃圾
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """原子写入 JSON 到目标路径。

    先在目标文件同目录下写临时文件，``fsync`` 后用 ``os.replace`` 原子替换。
    任意异常都会清理临时文件，目标文件保持旧状态（或不存在）。

    Args:
        path: 目标文件路径。其父目录需已存在（调用方负责 mkdir）。
        data: 可被 json 序列化的对象。
        indent: JSON 缩进，默认 2。
    """
    path = Path(path)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.stem}_",
        suffix=".tmp",
        dir=str(path.parent),
        delete=False,
    )
    try:
        with tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=indent)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
