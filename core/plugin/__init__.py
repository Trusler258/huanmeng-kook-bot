"""
Phase 13 Plugin Runtime（Huanmeng 2.0）

Plugin 成为完整独立系统：
- manifest：PluginManifest（name/version/runtime/entrypoint/dependencies/permissions）
- manager：PluginManager（discover/validate/load/init/enable/disable/reload/unload/health）
- api：PluginContext（公开 Plugin API：message/memory/event/timer/pipeline/background/capability/config）
- loader：从 plugins/ 目录发现与加载插件

隔离原则：插件只能使用公开 Plugin API、Capability API、EventBus 与公开 Service；
禁止 import Core 内部实现、直接修改 Pipeline、直接访问数据库或修改内部 Runtime 对象。
Core 不因新增一个 Plugin 而修改。
"""
from core.plugin.manifest import (
    PluginManifest, validate_manifest, RUNTIME_PYTHON, RUNTIME_LUA,
)
from core.plugin.api import (
    PluginContext, PluginPipeline, PluginBackground, get_pipeline_hooks,
)
from core.plugin.manager import (
    PluginManager, PluginRecord, get_plugin_manager,
    STATE_DISCOVERED, STATE_LOADED, STATE_ENABLED, STATE_DISABLED, STATE_ERROR,
)

__all__ = [
    "PluginManifest", "validate_manifest", "RUNTIME_PYTHON", "RUNTIME_LUA",
    "PluginContext", "PluginPipeline", "PluginBackground", "get_pipeline_hooks",
    "PluginManager", "PluginRecord", "get_plugin_manager",
    "STATE_DISCOVERED", "STATE_LOADED", "STATE_ENABLED", "STATE_DISABLED", "STATE_ERROR",
]