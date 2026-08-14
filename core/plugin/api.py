"""
Phase 13 Plugin API（Huanmeng 2.0）

插件的唯一公共入口。插件只能通过 ctx 访问公开能力，禁止直接 import Core 内部实现、
直接访问数据库或修改内部 Runtime 对象。

暴露的能力（全部可选，按需使用）：
- message  : 发送 / 回复消息（走 Response Delivery）
- memory   : 记忆写入 / 检索（走 Memory Engine，异步不阻塞）
- event    : 订阅 / 发布事件（走 EventBus）
- timer    : 注册周期定时器（reload/unload 自动取消）
- capability: 注册 Command / Skill / Tool 能力（走 CapabilityRegistry）
- config   : 读取本插件 manifest 声明的静态配置

约束：
- 不暴露 db、core 内部 Runtime、文件系统、网络、进程执行。
- 高风险权限（网络/文件/进程）由 Phase 17 Permission 统一裁决，本层不直接开放。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from core.logger import get_logger
from core.eventbus import EventBus, Event, get_event_bus
from core.capability import (
    Capability, CATEGORY_COMMAND, RUNTIME_COMMAND,
    get_capability_registry,
)

logger = get_logger("plugin.api")


class PluginMessage:
    """消息能力：发送 / 回复。默认走 Response Delivery / Sender。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def send(self, text: str, chat_id: int, is_group: bool = True) -> bool:
        """发送文本消息到频道/私聊。失败返回 False（不抛异常）。"""
        try:
            from services.sender import send_by_chat_type
            await send_by_chat_type(str(text), chat_id, is_group, None if is_group else None)
            return True
        except Exception as e:
            logger.warning("Plugin %s 发送失败: %s", self._plugin, e)
            return False


class PluginMemory:
    """记忆能力：异步写入（不阻塞） + 检索。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def remember(self, content: str, memory_type: str = "knowledge",
                       chat_id: int = 0) -> None:
        """写入一条长期记忆（异步，不阻塞响应）。"""
        try:
            from core.memory_engine import get_memory_engine, normalize_memory_type
            engine = get_memory_engine()
            engine.observe(
                chat_id=chat_id, content=content, tag="plugin",
                author=f"plugin:{self._plugin}",
            )
            _ = normalize_memory_type  # keep import referenced
        except Exception as e:
            logger.warning("Plugin %s 记忆写入降级: %s", self._plugin, e)

    async def recall(self, query: str, chat_id: Optional[int] = None, limit: int = 5) -> list:
        """检索记忆，返回结构化列表（DB 不可用返回空）。"""
        try:
            from core.memory_engine import get_memory_engine
            return await get_memory_engine().retrieve_top(
                query, chat_id=chat_id, limit=limit)
        except Exception as e:
            logger.warning("Plugin %s 记忆检索降级: %s", self._plugin, e)
            return []


class PluginEvent:
    """事件能力：订阅 / 发布。reload/unload 时自动清理。"""

    def __init__(self, plugin_name: str, bus: EventBus):
        self._plugin = plugin_name
        self._bus = bus
        self._handlers: list[tuple[str, Callable]] = []

    def on(self, event_name: str):
        """装饰器订阅事件。"""
        def deco(fn):
            self._handlers.append((event_name, fn))
            self._bus.subscribe(event_name, fn)
            return fn
        return deco

    def subscribe(self, event_name: str, handler: Callable) -> None:
        self._handlers.append((event_name, handler))
        self._bus.subscribe(event_name, handler)

    async def publish(self, event_name: str, data: Optional[dict] = None) -> None:
        await self._bus.publish(event_name, data)

    def clear(self) -> None:
        for name, h in self._handlers:
            self._bus.unsubscribe(name, h)
        self._handlers.clear()


class PluginTimer:
    """定时器能力：注册周期任务。reload/unload 自动取消，防止热更新后重复执行。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name
        self._tasks: list[asyncio.Task] = []

    def every(self, seconds: float):
        """装饰器：每 seconds 秒执行一次。"""
        def deco(fn):
            async def _loop():
                while True:
                    try:
                        await asyncio.sleep(seconds)
                        await fn()
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.warning("Plugin %s 定时器任务异常: %s", self._plugin, e)
            task = asyncio.create_task(_loop())
            self._tasks.append(task)
            return fn
        return deco

    def cancel_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


class PluginCapability:
    """能力注册：让插件向 Core 注册 Command/Skill/Tool 能力，供 CapabilityRouter 使用。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name
        self._registry = get_capability_registry()
        self._registered: list[str] = []

    def register_command(self, name: str, description: str = "",
                         handler: Optional[Callable] = None,
                         permissions: Optional[list[str]] = None) -> None:
        """注册一个命令能力。handler 为 async (msg) -> str|None。"""
        cap = Capability(
            id=f"plugin.{self._plugin}.{name}",
            name=name,
            description=description,
            category=CATEGORY_COMMAND,
            runtime=RUNTIME_COMMAND,
            permissions=permissions or ["message.read", "message.send"],
            source=f"plugin:{self._plugin}",
        )
        self._registry.register(cap)
        if handler is not None:
            self._registry.bind_handler(cap.id, handler)
        self._registered.append(cap.id)

    def unregister_all(self) -> None:
        for cid in self._registered:
            self._registry.unregister(cid)
        self._registered.clear()


class PluginContext:
    """插件上下文：插件唯一的 API 入口。"""

    def __init__(self, plugin_name: str, manifest, bus: Optional[EventBus] = None):
        self.name = plugin_name
        self.manifest = manifest
        self.bus = bus or get_event_bus()
        self.message = PluginMessage(plugin_name)
        self.memory = PluginMemory(plugin_name)
        self.event = PluginEvent(plugin_name, self.bus)
        self.timer = PluginTimer(plugin_name)
        self.capability = PluginCapability(plugin_name)

    def config(self, key: str, default: Any = None) -> Any:
        """读取本插件 manifest.config 里的静态配置。"""
        return self.manifest.config.get(key, default)

    def cleanup(self) -> None:
        """卸载时清理：事件订阅 + 定时器 + 能力注册，防止热更新后重复执行。"""
        self.event.clear()
        self.timer.cancel_all()
        self.capability.unregister_all()