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
EVENT_MESSAGE_IGNORED = "message.ignored"
EVENT_MESSAGE_PROCESSING = "message.processing"
EVENT_MESSAGE_REPLIED = "message.replied"
EVENT_MESSAGE_FAILED = "message.failed"
EVENT_TOOL_PERMISSION_DENIED = "tool.permission.denied"
EVENT_PLUGIN_LOADING = "plugin.loading"
EVENT_PLUGIN_LOAD_FAILED = "plugin.load_failed"
EVENT_PLUGIN_ENABLED = "plugin.enabled"
EVENT_PLUGIN_DISABLED = "plugin.disabled"
EVENT_CAPABILITY_REGISTERED = "capability.registered"
EVENT_CAPABILITY_UNREGISTERED = "capability.unregistered"

ALL_EVENTS: tuple[str, ...] = (
    EVENT_MESSAGE_RECEIVED, EVENT_MESSAGE_SENT,
    EVENT_TASK_CREATED, EVENT_TASK_COMPLETED,
    EVENT_TOOL_CALLED, EVENT_TOOL_COMPLETED,
    EVENT_MEMORY_CREATED, EVENT_MEMORY_UPDATED,
    EVENT_PLUGIN_LOADED, EVENT_PLUGIN_UNLOADED, EVENT_PLUGIN_ERROR,
    EVENT_UPDATE_STARTED, EVENT_UPDATE_COMPLETED,
    # ★ v2.3.102 补齐：消息生命周期 / 工具权限 / 插件与能力生命周期
    EVENT_MESSAGE_IGNORED, EVENT_MESSAGE_PROCESSING, EVENT_MESSAGE_REPLIED,
    EVENT_MESSAGE_FAILED, EVENT_TOOL_PERMISSION_DENIED,
    EVENT_PLUGIN_LOADING, EVENT_PLUGIN_LOAD_FAILED, EVENT_PLUGIN_ENABLED,
    EVENT_PLUGIN_DISABLED, EVENT_CAPABILITY_REGISTERED, EVENT_CAPABILITY_UNREGISTERED,
)

# 通配订阅
WILDCARD: str = "*"

def _new_event_id() -> str:
    import uuid
    return "evt_" + uuid.uuid4().hex[:16]


def emit_event(event_type: str, data: Optional[dict] = None, *,
                source: str = "core", plugin_id: Optional[str] = None,
                trace_id: Optional[str] = None) -> Optional[Event]:
    """统一发事件出口：自动补 trace_id / event_id 外壳，异常不外抛。

    用 `emit_event` 而不是直接 `get_event_bus().emit()`，这样所有公开事件都带同一套外壳字段。
    """
    try:
        if trace_id is None:
            try:
                from core.trace import get_trace_id
                trace_id = get_trace_id()
            except Exception:
                trace_id = ""
        bus = get_event_bus()
        ev = Event(str(event_type).lower(), data or {}, trace_id=trace_id or "",
                   source=source, plugin_id=plugin_id)
        bus.emit(ev.name, ev.data, event=ev)
        return ev
    except Exception as e:
        logger.debug("emit_event(%s) 失败（忽略）: %s", event_type, e)
        return None


Handler = Callable[..., Any]


EVENT_SCHEMA_VERSION = 1
# 单个订阅者的执行预算（秒）。★ v2.3.102：此前无超时，慢订阅者会拖住发布方。
# 仅对协程订阅者生效（同步订阅者无法安全取消，只记慢日志）。
SUBSCRIBER_TIMEOUT: float = float(__import__("os").getenv("EVENT_SUBSCRIBER_TIMEOUT", "10"))
SUBSCRIBER_SLOW_WARN: float = 1.0


class Event:
    """一次事件分发的数据载体。

    ★ v2.3.102 Envelope：新增 event_id / schema_version / trace_id / source / plugin_id，
    与原有的 name / data / ts 共存 —— 老订阅方不受影响，新订阅方可用统一外壳做日志关联与去重。
    """

    __slots__ = ("name", "data", "ts", "event_id", "schema_version",
                 "trace_id", "source", "plugin_id")

    def __init__(self, name: str, data: Optional[dict] = None, *,
                 event_id: str = "", trace_id: str = "",
                 source: str = "core", plugin_id: Optional[str] = None):
        self.name = name
        self.data = data or {}
        self.ts = time.time()
        self.event_id = event_id or _new_event_id()
        self.schema_version = EVENT_SCHEMA_VERSION
        self.trace_id = trace_id
        self.source = source
        self.plugin_id = plugin_id

    def to_dict(self) -> dict:
        """统一外壳（日志/审计/跨进程用）。业务载荷仍在 data。"""
        return {
            "event_id": self.event_id,
            "event_type": self.name,
            "schema_version": self.schema_version,
            "timestamp": self.ts,
            "trace_id": self.trace_id,
            "source": self.source,
            "plugin_id": self.plugin_id,
            "data": self.data,
        }


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
    async def publish(self, event_name: str, data: Optional[dict] = None, *,
                      event: Optional["Event"] = None) -> None:
        """发布事件。订阅者异常被隔离；协程订阅者按 SUBSCRIBER_TIMEOUT 取消。

        ★ v2.3.102：此前协程订阅者无超时，一个慢插件会拖住整个发布方。
        现在每个协程订阅者独立 wait_for；超时视为该订阅者失败（不影响其他订阅者）。
        同步订阅者无法安全取消，只记慢日志。
        """
        key = event_name.lower()
        self._record(key)
        handlers = list(self._handlers.get(key, [])) + list(self._handlers.get(WILDCARD, []))
        if not handlers:
            return
        if event is None:
            event = Event(key, data)
        for h in handlers:
            name = getattr(h, "__name__", str(h))
            t0 = time.perf_counter()
            try:
                if not asyncio.iscoroutinefunction(h):
                    res = h(event)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(_run_coro(res))
                else:
                    await asyncio.wait_for(h(event), timeout=SUBSCRIBER_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning("EventBus 订阅者 %s 处理 %s 超时（>%.0fs），已取消",
                               name, key, SUBSCRIBER_TIMEOUT)
            except Exception as e:
                logger.warning("EventBus 订阅者 %s 处理 %s 异常: %s", name, key, e)
            else:
                cost = time.perf_counter() - t0
                if cost >= SUBSCRIBER_SLOW_WARN:
                    logger.warning("EventBus 订阅者 %s 处理 %s 耗时 %.2fs（偏慢）",
                                   name, key, cost)

    def publish_sync(self, event_name: str, data: Optional[dict] = None, *,
                     event: Optional["Event"] = None) -> None:
        """同步发布（无事件循环时使用）；仅调用同步订阅者。"""
        key = event_name.lower()
        self._record(key)
        handlers = list(self._handlers.get(key, [])) + list(self._handlers.get(WILDCARD, []))
        if event is None:
            event = Event(key, data)
        for h in handlers:
            if asyncio.iscoroutinefunction(h):
                continue
            try:
                h(event)
            except Exception as e:
                logger.warning("EventBus 同步订阅者 %s 处理 %s 异常: %s",
                               getattr(h, "__name__", h), key, e)

    def emit(self, event_name: str, data: Optional[dict] = None, *,
             event: Optional["Event"] = None) -> None:
        """fire-and-forget 发布：调度到当前事件循环后台执行，调用方不阻塞。

        若当前无运行中的事件循环，则退化为同步发布（仅触发同步订阅者）。
        `event=` 用于传入已构造好的 Envelope（emit_event 走这条路）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.publish_sync(event_name, data, event=event)
            return
        try:
            loop.create_task(self.publish(event_name, data, event=event))
        except RuntimeError:
            self.publish_sync(event_name, data, event=event)

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