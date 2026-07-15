"""
集成测试的 conftest —— 在导入插件代码前 mock 掉所有 astrbot 依赖。

测试不依赖真实 astrbot 框架，全部在本地可跑。

重要：mock 只在未安装真实 astrbot 时注入。
如果有真实 astrbot（半真 e2e 测试场景），不 mock，让真实框架生效。
这样 tests/real_e2e/ 下的测试能用真实框架，不受此 conftest 污染。
"""

import sys
import types
import logging
from pathlib import Path

# ============================================================
# 1. 让 src 包可被导入（插件用相对导入，需要作为包加载）
# ============================================================
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# ============================================================
# 2. 判断是否需要 mock astrbot
#    如果真实 astrbot 已安装（半真 e2e 场景），不 mock
# ============================================================
def _is_real_astrbot_available() -> bool:
    try:
        import astrbot  # noqa: F401
        return True
    except ImportError:
        return False


class AstrBotConfig(dict):
    """Mock AstrBotConfig —— 就是个 dict，加个 save_config 空方法。
    顶层定义（无论是否 mock 都可用），测试直接 import 用。"""

    def save_config(self):
        pass


if not _is_real_astrbot_available():



    # --- astrbot.api ---
    api_mod = types.ModuleType("astrbot.api")
    _test_logger = logging.getLogger("test_astrbot")
    _test_logger.addHandler(logging.StreamHandler())
    _test_logger.setLevel(logging.INFO)
    api_mod.logger = _test_logger

    # AstrBotConfig 已在模块顶层定义，这里注册到 mock 的 api_mod
    api_mod.AstrBotConfig = AstrBotConfig

    # --- astrbot.api.star ---
    star_mod = types.ModuleType("astrbot.api.star")


    class StarTools:
        """Mock StarTools"""

        @staticmethod
        def get_data_dir(name: str = "") -> str:
            d = Path(__file__).parent / "_test_data" / name
            d.mkdir(parents=True, exist_ok=True)
            return str(d)


    star_mod.StarTools = StarTools
    star_mod.Context = type("Context", (), {})

    # --- astrbot.api.event ---
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})


    class _FilterMock:
        EventMessageType = type("EventMessageType", (), {
            "GROUP_MESSAGE": "GROUP_MESSAGE",
            "PRIVATE_MESSAGE": "PRIVATE_MESSAGE",
        })
        PlatformAdapterType = type("PlatformAdapterType", (), {
            "AIOCQHTTP": "AIOCQHTTP",
            "TELEGRAM": "TELEGRAM",
            "DISCORD": "DISCORD",
            "LARK": "LARK",
            "QQOFFICIAL": "QQOFFICIAL",
            "GEWECHAT": "GEWECHAT",
            "ALL": "ALL",
        })
        PermissionType = type("PermissionType", (), {"ADMIN": "ADMIN"})

        def event_message_type(self, _):
            def deco(func):
                return func
            return deco

        def platform_adapter_type(self, _):
            def deco(func):
                return func
            return deco

        def permission_type(self, _):
            def deco(func):
                return func
            return deco

        def on_platform_loaded(self):
            def deco(func):
                return func
            return deco

        def command(self, *a, **kw):
            def deco(func):
                return func
            return deco


    event_mod.filter = _FilterMock()

    # --- astrbot.api.provider ---
    provider_mod = types.ModuleType("astrbot.api.provider")


    class LLMResponse:
        """Mock LLMResponse"""

        def __init__(self, role="assistant", completion_text="", usage=None,
                     raw_completion=None, is_chunk=False):
            self.role = role
            self.completion_text = completion_text
            self.usage = usage
            self.raw_completion = raw_completion
            self.is_chunk = is_chunk


    provider_mod.LLMResponse = LLMResponse

    # --- astrbot ---
    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.api = api_mod
    astrbot_mod.api.event = event_mod
    astrbot_mod.api.star = star_mod
    astrbot_mod.api.provider = provider_mod

    # 注册所有 mock 模块
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.provider"] = provider_mod
