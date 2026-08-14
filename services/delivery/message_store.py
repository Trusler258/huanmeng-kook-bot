"""
消息存储（Huanmeng 2.0 Phase 5）

职责：
- 统一 msglog 写入，采用异步队列 + 批量刷盘。
- 不在 KOOK 发送主路径中同步写文件（避免阻塞 event loop）。
- 保持旧 msglog 格式不变：data/msglog/msglog_{chat_id}.jsonl，
  每条 JSON 含 {"msg_id","time","user_id","type","content","recalled"}，
  供 memory.search_msglog 回溯与 db.legacy 迁移读取。

worker 懒启动：首次 log 调用时若尚未启动则自动创建，
确保即使未显式 init_sender 也能正常落盘。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import time

from core.logger import get_logger

logger = get_logger("sender.store")

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "msglog"


class MessageStore:
    """异步消息落盘。"""

    def __init__(self, log_dir=None, max_queue: int = 2000):
        self._log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=max_queue)
        self._task: "asyncio.Task|None" = None
        self._started = False

    def start(self):
        """启动落盘 worker（幂等）。需在运行中的 event loop 内调用。"""
        if not self._started:
            self._started = True
            self._task = asyncio.ensure_future(self._worker())

    async def _worker(self):
        while True:
            try:
                entry = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                # 同步文件 IO 放到线程池，避免阻塞 event loop
                await asyncio.to_thread(self._write, entry)
            except Exception as e:
                logger.warning("msglog 落盘失败 chat=%s: %s", entry.get("chat_id", "?"), e)
            finally:
                self._queue.task_done()

    def _write(self, entry: dict):
        self._log_dir.mkdir(parents=True, exist_ok=True)
        chat_id = entry.pop("chat_id", 0)
        try:
            fpath = self._log_dir / f"msglog_{chat_id}.jsonl"
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            entry["chat_id"] = chat_id  # 归还，避免污染调用方传入的 dict

    def log(self, chat_id, user_id, msg_type: str, content: str):
        """入队一条消息（bot 与用户消息共用）。"""
        self._ensure_started()
        entry = {
            "msg_id": 0,
            "time": int(time()),
            "user_id": user_id,
            "type": msg_type,
            "content": content,
            "recalled": False,
            "chat_id": chat_id,
        }
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("msglog 队列已满，丢弃 chat=%s 的一条消息", chat_id)

    def log_user(self, chat_id, user_id, content: str):
        """记录用户消息到 msglog，供长时记忆回溯用户历史对话。"""
        self.log(chat_id, user_id, "group", content)

    def log_bot(self, chat_id, bot_qq, content: str):
        """记录 bot 发送的消息到 msglog。"""
        self.log(chat_id, bot_qq, "bot", content)

    def pending(self) -> int:
        return self._queue.qsize()

    async def flush(self):
        """等待队列中的消息全部落盘（用于关闭前）。"""
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("msglog flush 超时，仍有 %d 条未落盘", self._queue.qsize())

    async def close(self):
        """落盘剩余消息并关闭 worker。"""
        await self.flush()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._started = False

    def _ensure_started(self):
        if not self._started:
            try:
                self.start()
            except RuntimeError:
                # 无运行中的 event loop：暂不启动，下次 log() 再试。
                # （正常流程中 init_sender 会在事件循环内显式 start()）
                self._started = False