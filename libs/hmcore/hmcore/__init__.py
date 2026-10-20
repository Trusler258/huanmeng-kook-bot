"""hmcore —— Huanmeng 插件核心库（.hml）

只收**已有 ≥2 份副本**的通用代码，不放业务逻辑：
- `PluginBase`  : 28 个插件重复的脚手架（__init__ / on_load / on_enable / on_disable / on_unload）
- `load_core`   : 4 份逐字相同的「按文件路径懒加载插件内 core 模块」
- `is_admin`    : 2 份逐字相同的管理员判定
- `ttl_cache`   : 通用 60s TTL 缓存（异步 factory）

用法（插件内）：
    from hmcore import PluginBase, load_core
    from hmcore.net import TTLCache

    class Plugin(PluginBase):
        async def on_load(self):
            self.ctx.capability.register_command("x", "描述", self._cmd)

缺失时插件应保留本地兜底（`try: import hmcore except ImportError:`）。
"""
from .scaffold import PluginBase, load_core, is_admin
from .net import TTLCache

__version__ = "1.0.0"
__all__ = ["PluginBase", "load_core", "is_admin", "TTLCache", "__version__"]
