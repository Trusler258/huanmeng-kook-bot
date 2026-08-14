"""
ConversationRuntime（Huanmeng 2.0 Phase 4）

现有 core/queues.py 已实现 per-chat asyncio.Queue + Worker：
同一 chat 串行、不同 chat 并行。本模块在其之上增加上层生命周期管理，
绝不重造一个重复的全局消息队列。

职责：
- 消息顺序：路由到 per-chat 队列，同一 conversation 保持顺序，不同 conversation 并行。
- 长任务：Search / Agent / GitHub Update 等交给 TaskManager 后台执行，
  不阻塞 Conversation Worker。
- 后台 Task / Cancel / Shutdown / 生命周期：统一出入口。
- 任务状态查询：task_id / conversation 维度。

用法：
    rt = conversation_runtime
    rt.start()
    await rt.submit_message(chat_id=..., msg_type=..., msg_content=...)
    task = rt.submit_background("search", "查天气", coro_fn, conversation_id=..., user_id=...)
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Optional

from core.logger import get_logger

logger = get_logger("conversation_runtime")


class ConversationRuntime:
    """per-chat Queue/Worker 的上层生命周期管理器。"""

    def __init__(self):
        self._active: dict[int, dict] = {}   # chat_id -> 会话元信息
        self._started: bool = False

    # ── 生命周期 ──
    def start(self):
        """启动渲染队列等基础设施（幂等）。"""
        if self._started:
            return
        try:
            from core.queues import start_render_queue
            start_render_queue()
        except Exception as e:
            logger.warning("渲染队列启动失败: %s", e)
        self._started = True
        logger.info("ConversationRuntime 已启动")

    def _track(self, chat_id: int):
        if chat_id not in self._active:
            self._active[chat_id] = {"created_at": time.time(), "last_seen": time.time()}
        else:
            self._active[chat_id]["last_seen"] = time.time()

    # ── 消息提交（走 per-chat 队列，保序）──
    async def submit_message(self, **kwargs) -> None:
        """把一条消息投入对应 conversation 的队列，不阻塞调用方。

        保持现有语义：同一 chat 串行、不同 chat 并行。
        """
        chat_id = int(kwargs.get("chat_id", 0))
        self._track(chat_id)
        from core.queues import enqueue_message
        await enqueue_message(**kwargs)

    # ── 长任务（交给 TaskManager，不阻塞 Conversation Worker）──
    def submit_background(
        self,
        kind: str,
        goal: str,
        coro_fn: Callable[[object], Awaitable[object]],
        conversation_id: int = 0,
        user_id: int = 0,
        timeout: Optional[float] = None,
    ) -> object:
        """把长任务提交到 TaskManager 后台执行，立即返回 Task 句柄。"""
        from core.task_manager import task_manager
        trace_id = _current_trace_id()
        task = task_manager.create(
            kind=kind, goal=goal, conversation_id=conversation_id,
            user_id=user_id, trace_id=trace_id)
        task_manager.submit(task, coro_fn, timeout=timeout)
        return task

    # ── 任务状态查询 ──
    def get_task(self, task_id: str) -> Optional[object]:
        from core.task_manager import task_manager
        return task_manager.get(task_id)

    def list_tasks(self, conversation_id: Optional[int] = None, limit: int = 50) -> list:
        from core.task_manager import task_manager
        return task_manager.list(conversation_id=conversation_id, limit=limit)

    def cancel_task(self, task_id: str) -> bool:
        from core.task_manager import task_manager
        return task_manager.cancel(task_id)

    # ── 状态 / 关闭 ──
    def status(self) -> dict:
        """输出运行时状态：活跃会话数、队列长度、后台任务数。"""
        from core.queues import _group_queues
        queue_len = {cid: q.qsize() for cid, q in _group_queues.items()}
        from core.task_manager import task_manager
        active_tasks = sum(1 for t in task_manager.list(limit=1000)
                           if not t.is_terminal)
        return {
            "started": self._started,
            "active_conversations": len(self._active),
            "queue_lengths": queue_len,
            "active_tasks": active_tasks,
        }

    async def shutdown(self):
        """优雅关闭：取消所有后台 Task 与消息队列 Worker。"""
        from core.task_manager import task_manager
        await task_manager.shutdown()
        from core.queues import shutdown_queues
        await shutdown_queues()
        self._started = False
        logger.info("ConversationRuntime 已关闭")


def _current_trace_id() -> str:
    try:
        from core.trace import get_trace_id
        return get_trace_id()
    except Exception:
        return ""


# ── 全局单例 ────────────────────────────────────────────────
conversation_runtime = ConversationRuntime()