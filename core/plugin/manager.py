"""
Phase 13 Plugin Manager（Huanmeng 2.0）

插件生命周期管理：discover → validate → load → init → enable → disable → reload → unload → health。

隔离原则：
- 插件只能通过 PluginContext（公开 Plugin API）访问消息/记忆/事件/定时器/能力注册；
- 不暴露 Core 内部实现、数据库、文件系统、网络、进程；
- 插件崩溃被隔离：单个插件异常不影响 Core 与其他插件（Graceful Degradation）；
- reload/unload 时清理事件订阅 / 定时器 / 能力注册，防止热更新后重复执行。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
from core.eventbus import EventBus, get_event_bus, EVENT_PLUGIN_LOADED, EVENT_PLUGIN_UNLOADED, EVENT_PLUGIN_ERROR
from core.plugin.manifest import PluginManifest, RUNTIME_LUA, RUNTIME_PYTHON
from core.plugin.loader import discover_plugins, load_module, locate_plugin_classes
from core.plugin.api import PluginContext

logger = get_logger("plugin.manager")

# 插件状态
STATE_DISCOVERED = "discovered"
STATE_LOADED = "loaded"
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_ERROR = "error"


@dataclass
class PluginRecord:
    manifest: PluginManifest
    state: str = STATE_DISCOVERED
    module: Optional[object] = None
    instance: Optional[object] = None
    ctx: Optional[PluginContext] = None
    error: str = ""
    error_at: float = 0.0
    loaded_at: float = 0.0
    last_health: dict = field(default_factory=dict)


class PluginManager:
    """插件管理器。"""

    def __init__(self, plugins_dir: str, bus: Optional[EventBus] = None):
        self.plugins_dir = plugins_dir
        self.bus = bus or get_event_bus()
        self._records: dict[str, PluginRecord] = {}

    # ── discover / validate ──────────────────────────────
    def discover(self) -> list[PluginManifest]:
        """扫描并登记插件清单（不加载）。"""
        for mf in discover_plugins(self.plugins_dir):
            self._records.setdefault(mf.name, PluginRecord(manifest=mf))
        return [r.manifest for r in self._records.values()]

    def validate(self, name: str) -> tuple[bool, str]:
        rec = self._records.get(name)
        if rec is None:
            return False, f"插件不存在: {name}"
        if not rec.manifest.base_dir:
            return False, f"插件 {name} 未被发现"
        return True, ""

    def list(self) -> list[dict]:
        return [{
            "name": r.manifest.name,
            "version": r.manifest.version,
            "runtime": r.manifest.runtime,
            "state": r.state,
            "error": r.error,
        } for r in self._records.values()]

    # ── load ─────────────────────────────────────────────
    async def load(self, name: str) -> tuple[bool, str]:
        """加载插件模块并实例化。lua runtime 走 Phase 14，此处标记为待 LuaRuntime。"""
        ok, err = self.validate(name)
        if not ok:
            return False, err
        rec = self._records[name]

        if rec.manifest.runtime == RUNTIME_LUA:
            try:
                from core.plugin.lua import load_lua_plugin
            except ImportError:
                rec.state = STATE_ERROR
                rec.error = "Lua runtime 未就绪（Phase 14）"
                await self._emit_error(name, rec.error)
                return False, rec.error
            return await load_lua_plugin(self, rec)

        module = load_module(rec.manifest)
        if module is None:
            rec.state = STATE_ERROR
            rec.error = "模块加载失败"
            await self._emit_error(name, rec.error)
            return False, rec.error

        classes = locate_plugin_classes(module)
        if not classes:
            rec.state = STATE_ERROR
            rec.error = "未找到 Plugin 类"
            await self._emit_error(name, rec.error)
            return False, rec.error

        rec.module = module
        rec.ctx = PluginContext(name, rec.manifest, self.bus)
        try:
            rec.instance = classes[0](rec.ctx)
        except Exception as e:
            rec.state = STATE_ERROR
            rec.error = f"实例化失败: {e}"
            await self._emit_error(name, rec.error)
            return False, rec.error

        rec.state = STATE_LOADED
        rec.loaded_at = time.time()
        rec.error = ""
        return True, ""

    async def init(self, name: str) -> tuple[bool, str]:
        """调用插件 on_load 钩子（初始化）。"""
        rec = self._records.get(name)
        if rec is None or rec.instance is None:
            return False, f"插件 {name} 未加载"
        try:
            hook = getattr(rec.instance, "on_load", None)
            if hook is not None:
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._mark_error(name, e)
            return False, rec.error

    async def enable(self, name: str) -> tuple[bool, str]:
        """启用插件：调用 on_enable 钩子，发布 plugin.loaded 事件。"""
        rec = self._records.get(name)
        if rec is None:
            return False, f"插件不存在: {name}"
        if rec.instance is None:
            ok, err = await self.load(name)
            if not ok:
                return False, err
        rec = self._records[name]
        try:
            hook = getattr(rec.instance, "on_enable", None)
            if hook is not None:
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            rec.state = STATE_ENABLED
            await self.bus.publish(EVENT_PLUGIN_LOADED, {
                "name": name, "version": rec.manifest.version})
            logger.info("Plugin 已启用: %s@%s", name, rec.manifest.version)
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._mark_error(name, e)
            return False, rec.error

    async def disable(self, name: str) -> tuple[bool, str]:
        """禁用插件：调用 on_disable 钩子，清理 ctx 资源。"""
        rec = self._records.get(name)
        if rec is None:
            return False, f"插件不存在: {name}"
        try:
            hook = getattr(rec.instance, "on_disable", None)
            if hook is not None:
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            if rec.ctx is not None:
                rec.ctx.cleanup()
            rec.state = STATE_DISABLED
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._mark_error(name, e)
            return False, rec.error

    async def reload(self, name: str) -> tuple[bool, str]:
        """热重载：禁用 → 卸载 → 重新发现/加载/启用。"""
        ok, _ = await self.disable(name)
        await self.unload(name)
        self._records.pop(name, None)
        self.discover()
        ok, err = await self.load(name)
        if not ok:
            return False, err
        await self.init(name)
        return await self.enable(name)

    async def unload(self, name: str) -> tuple[bool, str]:
        """卸载插件：调用 on_unload，清理资源，发布 plugin.unloaded 事件。"""
        rec = self._records.get(name)
        if rec is None:
            return False, f"插件不存在: {name}"
        try:
            hook = getattr(rec.instance, "on_unload", None)
            if hook is not None:
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            if rec.ctx is not None:
                rec.ctx.cleanup()
            await self.bus.publish(EVENT_PLUGIN_UNLOADED, {"name": name})
            self._records.pop(name, None)
            logger.info("Plugin 已卸载: %s", name)
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._mark_error(name, e)
            return False, rec.error

    # ── health ───────────────────────────────────────────
    def health(self, name: str) -> dict:
        rec = self._records.get(name)
        if rec is None:
            return {"name": name, "ok": False, "state": "missing"}
        ok = rec.state == STATE_ENABLED and rec.error == ""
        rec.last_health = {
            "name": name, "ok": ok, "state": rec.state,
            "error": rec.error, "loaded_at": rec.loaded_at,
        }
        return rec.last_health

    def health_all(self) -> list[dict]:
        return [self.health(name) for name in self._records]

    # ── 批量 ─────────────────────────────────────────────
    async def load_all(self) -> list[str]:
        """加载并启用所有插件。单个失败不影响其他。返回成功列表。"""
        self.discover()
        ok_names: list[str] = []
        for name in list(self._records):
            ok, _ = await self.load(name)
            if not ok:
                continue
            await self.init(name)
            ok2, _ = await self.enable(name)
            if ok2:
                ok_names.append(name)
        return ok_names

    async def shutdown_all(self) -> None:
        for name in list(self._records):
            if self._records[name].instance is not None:
                await self.unload(name)
        self._records.clear()

    # ── 内部 ─────────────────────────────────────────────
    async def _mark_error(self, name: str, exc: Exception) -> None:
        rec = self._records.get(name)
        if rec is None:
            return
        rec.state = STATE_ERROR
        rec.error = str(exc)
        rec.error_at = time.time()
        await self._emit_error(name, rec.error)

    async def _emit_error(self, name: str, msg: str) -> None:
        try:
            await self.bus.publish(EVENT_PLUGIN_ERROR, {"name": name, "error": msg})
        except Exception:
            pass


# ── 全局单例 ───────────────────────────────────────────────
_manager: Optional[PluginManager] = None


def get_plugin_manager(plugins_dir: Optional[str] = None) -> PluginManager:
    global _manager
    if _manager is None:
        default_dir = plugins_dir or _default_plugins_dir()
        _manager = PluginManager(default_dir)
    return _manager


def _default_plugins_dir() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent.parent / "plugins")