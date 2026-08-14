"""
Phase 14 Lua Plugin（Huanmeng 2.0）

Lua 插件与 Python 插件共用统一 Plugin API（通过 Bridge 暴露 message/memory/event/
timer/capability/config）。Lua 主要用于 Command、自动回复、小游戏、规则和简单自动化。

安全（Sandbox）：
- 剔除 os / io / package / require / dofile / loadfile / debug 等危险库；
- 禁止 os.execute、任意 filesystem、process、native library、任意网络访问；
- 只能通过 `bridge` 表访问受控能力；
- 具备 timeout（单次执行）、execution budget、call budget、permission。

Lua 插件 DSL（入口 .lua 中可用）：
    bridge.command("name", "描述")              -- 声明命令，实现 function cmd_name(msg) end
    bridge.on_event("event.name", function(e) end)  -- 订阅事件
    bridge.every(seconds, function() end)        -- 周期定时器
    bridge.send(text, chat_id, is_group)         -- 发送消息
    bridge.remember(content, type)               -- 异步写记忆
    bridge.recall(query, limit)                  -- 检索记忆（返回数组）
    bridge.publish("event.name", data)           -- 发布事件
    bridge.config("key", default)                -- 读配置

reload/unload 时由 PluginManager 调用 ctx.cleanup()，取消旧定时器与事件订阅，
防止热更新后事件/定时器重复执行。
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

logger = get_logger("plugin.lua")

# 危险库：从 Lua 全局环境剔除
_DANGEROUS_GLOBALS = (
    "os", "io", "package", "require", "dofile", "loadfile", "load",
    "debug", "collectgarbage", "newproxy", "module",
)

# 超时 / 预算（环境变量可覆盖）
LUA_TIMEOUT: float = float(os.getenv("LUA_TIMEOUT", "2.0"))
LUA_CALL_BUDGET: int = int(os.getenv("LUA_CALL_BUDGET", "50"))
LUA_MAX_TIMERS: int = int(os.getenv("LUA_MAX_TIMERS", "10"))


def _run_in_thread(fn, timeout: float):
    """独立线程执行 fn，超时返回 TimeoutError。超时后线程无法安全终止，
    调用方需将沙箱标记 poisoned 以便下次重建。"""
    box: dict = {"result": None, "error": None, "done": False}

    def target():
        try:
            box["result"] = fn()
            box["done"] = True
        except Exception as e:  # noqa: BLE001
            box["error"] = e
            box["done"] = True

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if not box["done"]:
        return None, True
    return box, False


class LuaSandbox:
    """受限 Lua 沙箱。一个插件一个实例。"""

    def __init__(self, ctx, plugin_name: str):
        self._ctx = ctx
        self._name = plugin_name
        self._budget = LUA_CALL_BUDGET
        self._calls = 0
        self._poisoned = False
        self._lua = None
        self._commands: dict[str, str] = {}   # name -> description
        self._timer_tasks: list[asyncio.Task] = []
        self._ensure()

    # ── 运行时构建 ───────────────────────────────────────
    def _ensure(self) -> None:
        if self._lua is not None and not self._poisoned:
            return
        from lupa import LuaRuntime
        lua = LuaRuntime(unpack_returned_tuples=True)
        g = lua.globals()
        for name in _DANGEROUS_GLOBALS:
            try:
                g[name] = None
            except Exception:
                pass
        g["__bridge"] = lambda name, *a: self.bridge(name, *a)
        self._reset_calls()
        lua.execute(
            "local names={'command','on_event','every','send','remember','recall',"
            "'publish','config'}\n"
            "local t={}\n"
            "for _,n in ipairs(names) do\n"
            "  t[n]=function(...) return __bridge(n, ...) end\n"
            "end\n"
            "bridge=t\n")
        self._lua = lua
        self._poisoned = False

    def _reset_calls(self) -> None:
        self._calls = 0

    def _guard(self) -> None:
        self._calls += 1
        if self._calls > self._budget:
            raise RuntimeError(f"Lua bridge 调用超过预算 {self._budget} 次")

    # ── Lua → Python 桥（__sb:bridge）────────────────────
    def bridge(self, name: str, *args):
        self._guard()
        handler = getattr(self, "_b_" + name, None)
        if handler is None:
            raise RuntimeError(f"未知 bridge 方法: {name}")
        return handler(*args)

    # 各 bridge 能力
    def _b_command(self, name: str, description: str = "") -> None:
        name = str(name)
        self._commands[name] = str(description)
        self._ctx.capability.register_command(
            name=name, description=str(description) or f"Lua 命令 .{name}")

    def _b_on_event(self, event_name: str, fn) -> None:
        self._ctx.event.subscribe(str(event_name), self._make_event_handler(fn))

    def _b_every(self, seconds: float, fn) -> None:
        if len(self._timer_tasks) + len(self._ctx.timer._tasks) >= LUA_MAX_TIMERS:
            raise RuntimeError("Lua 定时器数量超限")
        asyncio.ensure_future(self._timer_loop(float(seconds), fn))

    def _b_send(self, text: str, chat_id: int = 0, is_group: bool = True) -> bool:
        return asyncio.run_coroutine_threadsafe(
            self._ctx.message.send(str(text), int(chat_id), bool(is_group)),
            _current_loop()).result(timeout=5)

    def _b_remember(self, content: str, memory_type: str = "knowledge", chat_id: int = 0) -> None:
        asyncio.run_coroutine_threadsafe(
            self._ctx.memory.remember(str(content), str(memory_type), int(chat_id)),
            _current_loop()).result(timeout=5)

    def _b_recall(self, query: str, limit: int = 5):
        rows = asyncio.run_coroutine_threadsafe(
            self._ctx.memory.recall(str(query), limit=int(limit)),
            _current_loop()).result(timeout=5)
        return rows or []

    def _b_publish(self, event_name: str, data: Optional[dict] = None) -> None:
        asyncio.run_coroutine_threadsafe(
            self._ctx.event.publish(str(event_name), dict(data or {})),
            _current_loop()).result(timeout=5)

    def _b_config(self, key: str, default: Any = None) -> Any:
        return self._ctx.config(key, default)

    # ── 事件/定时器执行 ─────────────────────────────────
    def _make_event_handler(self, fn):
        def handler(event):
            try:
                self._run_lua_callable(fn, _lua_event(event))
            except Exception as e:
                logger.warning("Lua 插件 %s 事件处理异常: %s", self._name, e)
        return handler

    async def _timer_loop(self, seconds: float, fn) -> None:
        task = asyncio.current_task()
        self._timer_tasks.append(task)
        try:
            while True:
                await asyncio.sleep(seconds)
                try:
                    self._run_lua_callable(fn, {})
                except Exception as e:
                    logger.warning("Lua 插件 %s 定时器异常: %s", self._name, e)
        except asyncio.CancelledError:
            self._timer_tasks.remove(task)
            raise

    def _run_lua_callable(self, fn, arg) -> None:
        self._ensure()
        self._reset_calls()
        (box, timedout) = _run_in_thread(lambda: fn(arg), LUA_TIMEOUT)
        if timedout:
            self._poisoned = True
            logger.warning("Lua 插件 %s 执行超时", self._name)
        elif box.get("error"):
            raise box["error"]

    # ── 命令执行 ─────────────────────────────────────────
    def run_command(self, name: str, msg: dict) -> Optional[str]:
        self._ensure()
        self._reset_calls()
        g = self._lua.globals()
        fn = g["cmd_" + name]
        if fn is None:
            return None
        msg_table = _py_to_lua(self._lua, msg)
        (box, timedout) = _run_in_thread(lambda: fn(msg_table), LUA_TIMEOUT)
        if timedout:
            self._poisoned = True
            raise TimeoutError(f"命令 .{name} 执行超时")
        if box.get("error"):
            raise box["error"]
        result = box.get("result")
        return result if isinstance(result, str) else None

    def command_names(self) -> list[str]:
        return list(self._commands.keys())

    # ── 加载 ─────────────────────────────────────────────
    def load(self, code: str) -> None:
        self._ensure()
        self._reset_calls()
        (box, timedout) = _run_in_thread(lambda: self._lua.execute(code), LUA_TIMEOUT)
        if timedout:
            self._poisoned = True
            raise TimeoutError(f"Lua 插件 {self._name} 加载超时")
        if box.get("error"):
            raise box["error"]

    # ── 清理 ─────────────────────────────────────────────
    def cleanup(self) -> None:
        for t in self._timer_tasks:
            t.cancel()
        self._timer_tasks.clear()
        self._commands.clear()
        self._lua = None
        self._poisoned = True


def _py_to_lua(lua, obj):
    if isinstance(obj, dict):
        t = lua.table()
        for k, v in obj.items():
            t[k] = _py_to_lua(lua, v)
        return t
    if isinstance(obj, (list, tuple)):
        t = lua.table()
        for i, v in enumerate(obj, start=1):
            t[i] = _py_to_lua(lua, v)
        return t
    return obj


def _lua_event(event) -> dict:
    data = getattr(event, "data", None) or {}
    return {"name": getattr(event, "name", ""), "data": data}


def _current_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.new_event_loop()


async def load_lua_plugin(manager, rec):
    """加载 Lua 插件：读取入口脚本 → 沙箱执行 → handler 返回沙箱作为 instance。"""
    entry = Path(rec.manifest.base_dir) / rec.manifest.entrypoint
    if not entry.is_file():
        rec.state = "error"
        rec.error = f"Lua 入口不存在: {entry}"
        return False, rec.error
    try:
        code = entry.read_text(encoding="utf-8")
        sandbox = LuaSandbox(rec.ctx, rec.manifest.name)
        sandbox.load(code)
        rec.instance = sandbox
        rec.state = "loaded"
        rec.error = ""
        return True, ""
    except Exception as e:
        rec.state = "error"
        rec.error = str(e)
        return False, rec.error