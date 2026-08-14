"""
TaskManager（Huanmeng 2.0 Phase 4）

长任务管理：Search / Agent / GitHub Update 等不能永久阻塞 Conversation Worker，
统一交给 TaskManager 在后台 asyncio Task 中执行，并支持状态查询 / 取消 / 超时。

状态机（TaskState）：
    CREATED → PLANNING → RUNNING → WAITING ⇄ RUNNING → VERIFYING → COMPLETED
                            │                        │
                            └──────── FAILED ────────┘
                            │
                            ├─ CANCELLED   （cancel）
                            └─ TIMEOUT     （超时）

设计要点：
- 后台任务通过 asyncio.create_task 创建，自动复制当前 contextvars
  （RequestContext / trace_id），因此长任务内仍能拿到同一 trace_id。
- 任务状态同写入 SQLite（TaskRepository / TaskStepRepository），
  数据库未初始化时静默降级为纯内存，不影响运行。
- 同一 conversation 的长任务彼此独立，不阻塞其消息队列串行处理。
"""
from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from core.logger import get_logger

logger = get_logger("task_manager")


class TaskState(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass
class TaskStep:
    """Task 内部的一步，也可能落库为 task_steps。"""
    index: int
    action: str = ""
    state: str = "PENDING"
    detail: dict = field(default_factory=dict)
    trace_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Task:
    """一个 Agent 长任务实例（内存态，可选落库）。"""
    task_id: str
    kind: str                       # agent / search / update / ...
    goal: str = ""
    conversation_id: int = 0
    user_id: int = 0
    trace_id: str = ""
    state: TaskState = TaskState.CREATED
    result: dict = field(default_factory=dict)
    error: str = ""

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    steps: list[TaskStep] = field(default_factory=list)

    # 后台运行句柄 / 取消事件（submit 后填充）
    _bg: Optional[asyncio.Task] = None
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # ── 状态辅助 ──
    def set_state(self, state: TaskState):
        self.state = state
        self.updated_at = time.time()
        if state in (TaskState.RUNNING, TaskState.WAITING, TaskState.VERIFYING):
            self.started_at = self.started_at or time.time()
        if state in (TaskState.COMPLETED, TaskState.FAILED,
                     TaskState.CANCELLED, TaskState.TIMEOUT):
            self.finished_at = time.time()

    def add_step(self, action: str = "", state: str = "PENDING",
                 detail: Optional[dict] = None, trace_id: str = "") -> TaskStep:
        step = TaskStep(index=len(self.steps), action=action, state=state,
                        detail=detail or {}, trace_id=trace_id or self.trace_id)
        self.steps.append(step)
        self.updated_at = time.time()
        return step

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.created_at) * 1000.0, 2)

    @property
    def is_terminal(self) -> bool:
        return self.state in (TaskState.COMPLETED, TaskState.FAILED,
                              TaskState.CANCELLED, TaskState.TIMEOUT)


class TaskManager:
    """长任务注册 / 调度 / 状态管理。线程与 async 安全（asyncio 单线程）。"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}

    # ── 创建 ──
    def create(self, kind: str, goal: str = "", conversation_id: int = 0,
               user_id: int = 0, trace_id: str = "",
               task_id: Optional[str] = None) -> Task:
        tid = task_id or uuid.uuid4().hex[:16]
        task = Task(task_id=tid, kind=kind, goal=goal,
                    conversation_id=conversation_id, user_id=user_id,
                    trace_id=trace_id)
        self._tasks[tid] = task
        self._persist(task)
        logger.info("[任务] 创建 %s task=%s conv=%s goal=%r",
                    kind, tid, conversation_id, goal)
        return task

    # ── 提交后台执行（不阻塞调用方 / Conversation Worker）──
    def submit(self, task: Task, coro_fn: Callable[[Task], Awaitable[object]],
               timeout: Optional[float] = None) -> asyncio.Task:
        """把 coro_fn(task) 放到独立 asyncio Task 后台跑。

        自动复制当前 contextvars（trace_id 等），任务结束/取消/超时
        更新 TaskState 并写库。返回管理句柄。
        """
        ctx = contextvars.copy_context()

        async def runner():
            task.set_state(TaskState.RUNNING)
            self._persist(task)
            try:
                if timeout is not None:
                    fut = asyncio.ensure_future(coro_fn(task))
                    task._bg = fut
                    try:
                        result = await asyncio.wait_for(asyncio.shield(fut), timeout)
                    except asyncio.TimeoutError:
                        task.set_state(TaskState.TIMEOUT)
                        task.error = f"timeout after {timeout}s"
                        fut.cancel()
                        self._persist(task)
                        return
                else:
                    result = await coro_fn(task)
                if task.state not in (TaskState.CANCELLED, TaskState.TIMEOUT):
                    task.set_state(TaskState.COMPLETED)
                    task.result = _as_dict(result)
                    self._persist(task)
                    logger.info("[任务] 完成 task=%s kind=%s", task.task_id, task.kind)
            except asyncio.CancelledError:
                task.set_state(TaskState.CANCELLED)
                self._persist(task)
                raise
            except Exception as e:
                task.set_state(TaskState.FAILED)
                task.error = str(e)
                self._persist(task)
                logger.error("[任务] 失败 task=%s kind=%s: %s",
                             task.task_id, task.kind, e)

        task._bg = ctx.run(asyncio.create_task, runner())
        return task._bg

    # ── 查询 ──
    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self, conversation_id: Optional[int] = None,
             limit: int = 50) -> list[Task]:
        tasks = list(self._tasks.values())
        if conversation_id is not None:
            tasks = [t for t in tasks if t.conversation_id == conversation_id]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    def count(self) -> int:
        return len(self._tasks)

    # ── 控制 ──
    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.is_terminal:
            return False
        task._cancel_event.set()
        if task._bg is not None and not task._bg.done():
            task._bg.cancel()
        task.set_state(TaskState.CANCELLED)
        self._persist(task)
        logger.info("[任务] 取消 task=%s", task_id)
        return True

    def set_state(self, task_id: str, state: TaskState, result: Optional[dict] = None,
                  error: str = "") -> Optional[Task]:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.set_state(state)
        if result is not None:
            task.result = _as_dict(result)
        if error:
            task.error = error
        self._persist(task)
        return task

    def add_step(self, task_id: str, action: str = "", state: str = "PENDING",
                 detail: Optional[dict] = None) -> Optional[TaskStep]:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        step = task.add_step(action=action, state=state,
                             detail=detail, trace_id=task.trace_id)
        self._persist_step(task, step)
        return step

    # ── 关闭 ──
    async def shutdown(self):
        for task in list(self._tasks.values()):
            if task._bg is not None and not task._bg.done():
                task._bg.cancel()
        bgs = [t._bg for t in self._tasks.values() if t._bg is not None]
        if bgs:
            await asyncio.gather(*bgs, return_exceptions=True)
        logger.info("[任务] TaskManager 已关闭，剩余 %d 个", len(self._tasks))

    # ── 落库（尽力而为，DB 未初始化时静默跳过）──
    def _persist(self, task: Task):
        try:
            from db import UnitOfWork
            import asyncio as _a
            # 在事件循环上下文内执行；若无可复用循环则跳过（依赖调用方已初始化 db）
            try:
                _a.get_running_loop()
            except RuntimeError:
                return
        except Exception:
            return
        self._schedule_persist(task, None)

    def _persist_step(self, task: Task, step: TaskStep):
        self._schedule_persist(task, step)

    def _schedule_persist(self, task: Task, step: Optional[TaskStep]):
        """把 Task / Step 异步写库，不阻塞 TaskManager 调用线程。"""
        tid = task.task_id
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _write():
            try:
                from db import UnitOfWork
                async with UnitOfWork() as uow:
                    existing = await uow.tasks.by_task_id(tid)
                    if existing is None:
                        await uow.tasks.create_task(
                            task_id=tid, conversation_id=task.conversation_id,
                            user_id=task.user_id, kind=task.kind,
                            goal=task.goal, trace_id=task.trace_id)
                    await uow.tasks.set_state(tid, task.state.value,
                                              result=task.result or None,
                                              error=task.error)
                    if step is not None:
                        await uow.task_steps.add_step(
                            task_id=tid, step_index=step.index, action=step.action,
                            state=step.state, detail=step.detail, trace_id=step.trace_id)
            except Exception as e:
                logger.debug("[任务] 写库跳过 task=%s: %s", tid, e)

        try:
            loop.create_task(_write())
        except RuntimeError:
            pass


def _as_dict(result: object) -> dict:
    if isinstance(result, dict):
        return result
    if result is None:
        return {}
    return {"result": str(result)}


# ── 全局单例 ────────────────────────────────────────────────
task_manager = TaskManager()