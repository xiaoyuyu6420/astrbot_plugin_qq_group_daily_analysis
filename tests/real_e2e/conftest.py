"""
半真 e2e 测试的 conftest

与 tests/conftest.py（mock 版）完全隔离：
- conftest.py mock 掉 astrbot → 给 test_monitor_e2e.py 用（纯逻辑测试）
- conftest_real.py 用真实 astrbot → 给 test_monitor_real_e2e.py 用

隔离方式：通过 pytest marker `real_astrbot` 标记，且 conftest_real.py 不注册任何
sys.modules mock，确保真实 astrbot 被导入。

注意：运行需要 `pip install astrbot ulid-py diskcache`
"""

import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PARENT = PLUGIN_ROOT.parent
PLUGIN_PKG_NAME = PLUGIN_ROOT.name

# 把插件的父目录加入 sys.path，让插件可作为包导入（from astrbot_plugin_xxx.main import ...）
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))


def _import_plugin():
    """导入插件 main.py，触发 @filter 装饰器注册 handler。"""
    return __import__(f"{PLUGIN_PKG_NAME}.main", fromlist=["main"])


@pytest.fixture(scope="session")
def plugin_module():
    """导入真实插件，返回 main 模块。整个 session 只导入一次。"""
    return _import_plugin()


@pytest.fixture
def monitor_handler(plugin_module):
    """返回 monitor_qq_messages 对应的 StarHandlerMetadata（真实注册的）。"""
    from astrbot.core.star.star_handler import star_handlers_registry

    for h in star_handlers_registry._handlers:
        if "monitor_qq" in getattr(h, "handler_name", ""):
            return h
    pytest.fail("monitor_qq_messages handler 未在 registry 中找到")


@pytest.fixture
def telegram_handler(plugin_module):
    """返回 intercept_telegram_messages handler（对照组）。"""
    from astrbot.core.star.star_handler import star_handlers_registry

    for h in star_handlers_registry._handlers:
        if "intercept_telegram" in getattr(h, "handler_name", ""):
            return h
    pytest.fail("intercept_telegram_messages handler 未找到")
