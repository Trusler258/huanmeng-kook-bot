"""
统一 EventBus（Huanmeng 2.0）

Phase 13 引入，作为 Plugin 协作的公共基础设施；Phase 15 会扩展事件覆盖面。

设计：
- 事件总线允许订阅 / 发布，解耦各模块（Plugin 之间禁止直接 import 互相依赖，
  需要协作时使用 EventBus 或 Capability）。
- 订阅者按事件名注册；发布时同步分发（异步任务），单个订阅者异常不影响其他订阅者
  与 Core（Graceful Degradation）。
- 事件名统一小写，例：message.received / message.sent / task.created / tool.called ...
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from core.logger import get_logger

logger = get_logger("eventbus")

# 事件名集合（Phase 15 扩展）
EVENT_MESSAGE_RECEIVED = "message.received"
EVENT_MESSAGE_SENT = "message.sent"
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TOOL_CALLED = "tool.called"
EVENT_TOOL_COMPLETED = "tool.completed"
EVENT_MEMORY_CREATED = "memory.created"
EVENT_MEMORY_UPDATED = "memory.updated"
EVENT_PLUGIN_LOADED = "plugin.loaded"
EVENT_PLUGIN_UNLOADED = "plugin.unloaded"
EVENT_PLUGIN_ERROR = "plugin.error"
EVENT_UPDATE_STARTED = "update.started"
EVENT_UPDATE_COMPLETED = "update.completed"

ALL_EVENTS: tuple[str, ...] = (
    EVENT_MESSAGE_RECEIVED, EVENT_MESSAGE_SENT,
    EVENT_TASK_CREATED, EVENT_TASK_COMPLETED,
    EVENT_TOOL_CALLED, EVENT_TOOL_COMPLETED,
    EVENT_MEMORY_CREATED, EVENT_MEMORY_UPDATED,
    EVENT_PLUGIN_LOADED, EVENT_PLUGIN_UNLOADED, EVENT_PLUGIN_ERROR,
    EVENT_UPDATE_STARTED, EVENT_UPDATE_COMPLETED,
)

# 通配订阅
WILDCARD: str = "*"

Handler = Callable[..., Any]


class Event:
    """一次事件分发的数据载体。"""

    __slots__ = ("name", "data", "ts")

    def __init__(self, name: str, data: Optional[dict] = None):
        self.name = name
        self.data = data or {}
        self.ts = time.time()


class EventBus:
    """同步分发的事件总线（异步订阅者会被调度为任务）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._history: list[tuple[str, int]] = []  # (event_name, ts) 用于可观测
        self._history_max = 200

    # ── 订阅 ─────────────────────────────────────────────
    def subscribe(self, event_name: str, handler: Handler) -> None:
        """订阅事件。handler 可以是同步或异步函数。"""
        key = event_name.lower()
        if handler not in self._handlers[key]:
            self._handlers[key].append(handler)
        logger.debug("EventBus subscribe: %s -> %s", key,
                     getattr(handler, "__name__", handler))

    def unsubscribe(self, event_name: str, handler: Handler) -> None:
        key = event_name.lower()
        try:
            self._handlers[key].remove(handler)
        except ValueError:
            pass

    def on(self, event_name: str) -> Callable[[Handler], Handler]:
        """装饰器形式订阅。"""
        def deco(fn: Handler) -> Handler:
            self.subscribe(event_name, fn)
            return fn
        return deco

    # ── 发布 ─────────────────────────────────────────────
    async def publish(self, event_name: str, data: Optional[dict] = None) -> None:
        """发布事件。分发不等待异步订阅者完成；订阅者异常被隔离。"""
        key = event_name.lower()
        self._record(key)
        handlers = list(self._handlers.get(key, [])) + list(self._handlers.get(WILDCARD, []))
        if not handlers:
            return
        event = Event(key, data)
        for h in handlers:
            try:
                res = h(event) if not asyncio.iscoroutinefunction(h) else await h(event)
                if asyncio.iscoroutine(res):
                    # 同步包装函数返回了协程（罕见），调度执行
                    asyncio.create_task(_run_coro(res))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("EventBus 订阅者 %s 处理 %s 异常: %s",
                               getattr(h, "__name__", h), key, e)

    def publish_sync(self, event_name: str, data: Optional[dict] = None) -> None:
        """同步发布（无事件循环时使用）；仅调用同步订阅者。"""
        key = event_name.lower()
        self._record(key)
        handlers = list(self._handlers.get(key, [])) + list(self._handlers.get(WILDCARD, []))
        event = Event(key, data)
        for h in handlers:
            if asyncio.iscoroutinefunction(h):
                continue
            try:
                h(event)
            except Exception as e:
                logger.warning("EventBus 同步订阅者 %s 处理 %s 异常: %s",
                               getattr(h, "__name__", h), key, e)

    # ── 生命周期 / 可观测 ───────────────────────────────
    def clear(self) -> None:
        self._handlers.clear()

    def remove_all(self, owner: str) -> int:
        """移除某归属方（如某 plugin）注册的所有订阅。返回移除数量。"""
        removed = 0
        for k, lst in list(self._handlers.items()):
            kept = [h for h in lst if getattr(h, "__owner__", None) != owner]
            removed += len(lst) - len(kept)
            if kept:
                self._handlers[k] = kept
            else:
                self._handlers.pop(k, None)
        return removed

    def history(self) -> list[tuple[str, int]]:
        return list(self._history)

    def _record(self, key: str) -> None:
        self._history.append((key, time.time()))
        if len(self._history) > self._history_max:
            del self._history[: len(self._history) - self._history_max]


async def _run_coro(coro: Awaitable) -> None:
    try:
        await coro
    except Exception as e:
        logger.warning("EventBus 后台协程异常: %s", e)


# ── 全局单例 ───────────────────────────────────────────────
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """全局 EventBus 单例。"""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus