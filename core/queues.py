"""
per-group 异步消息队列 + 渲染队列
- 每个群/私聊独立一个 asyncio.Queue + worker，互不阻塞
- 图片渲染走独立队列，信号量=1，不阻塞聊天
"""

from __future__ import annotations

import asyncio
from core.logger import get_logger

logger = get_logger("queues")

# ── 消息队列 ──
_group_queues: dict[int, asyncio.Queue] = {}
_group_tasks: dict[int, asyncio.Task] = {}

# ── 渲染队列（串行化，避免抢 Chromium）──
_render_queue: asyncio.Queue | None = None
_render_task: asyncio.Task | None = None
_render_semaphore = asyncio.Semaphore(2)  # 最多 2 个并发渲染


def _get_or_create_queue(chat_id: int) -> asyncio.Queue:
    """获取或创建某个对话的独立队列"""
    if chat_id not in _group_queues:
        q = asyncio.Queue()
        _group_queues[chat_id] = q
        task = asyncio.ensure_future(_group_worker(chat_id, q))
        _group_tasks[chat_id] = task
        logger.info("[队列] 创建群%d 的独立消息队列", chat_id)
    return _group_queues[chat_id]


async def _group_worker(chat_id: int, queue: asyncio.Queue):
    """某个群的 worker，串行处理该群消息"""
    from core.pipeline import process_message
    while True:
        try:
            kwargs = await queue.get()
            try:
                await process_message(**kwargs)
            except Exception as e:
                logger.error("[队列] 群%d 处理异常: %s", chat_id, e)
            queue.task_done()
        except asyncio.CancelledError:
            break


async def enqueue_message(**kwargs):
    """将消息投入对应群的队列，不阻塞调用方"""
    chat_id = kwargs.get("chat_id", 0)
    q = _get_or_create_queue(chat_id)
    await q.put(kwargs)
    logger.debug("[队列] 群%d 消息已入队 (队长度=%d)", chat_id, q.qsize())


# ── 渲染队列 ──

async def _render_worker(queue: asyncio.Queue):
    """渲染 worker，串行处理图片生成请求"""
    while True:
        try:
            future, render_fn, args, kwargs = await queue.get()
            async with _render_semaphore:
                try:
                    result = await render_fn(*args, **kwargs)
                    future.set_result(result)
                except Exception as e:
                    future.set_exception(e)
        except asyncio.CancelledError:
            break
        finally:
            queue.task_done()


def start_render_queue():
    """启动渲染队列"""
    global _render_queue, _render_task
    if _render_task is None:
        _render_queue = asyncio.Queue()
        _render_task = asyncio.ensure_future(_render_worker(_render_queue))
        logger.info("[队列] 渲染队列已启动")


async def submit_render(render_fn, *args, **kwargs):
    """提交渲染任务，返回结果（异步等待）"""
    if _render_queue is None:
        start_render_queue()
    future = asyncio.Future()
    await _render_queue.put((future, render_fn, args, kwargs))
    return await future


async def shutdown_queues():
    """关闭所有队列和 worker"""
    for task in list(_group_tasks.values()):
        task.cancel()
    if _render_task:
        _render_task.cancel()
    await asyncio.gather(*_group_tasks.values(), return_exceptions=True)
    logger.info("[队列] 所有队列已关闭")
