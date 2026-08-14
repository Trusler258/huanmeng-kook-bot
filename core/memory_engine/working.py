"""
Phase 11 Memory 2.0：Working Memory（工作记忆 / STM 缓冲，Huanmeng 2.0）

- 短期存放当前会话的原始消息快照，用于后续提炼为长期 Memory。
- 低价值内容（哈哈/好的/谢谢）不进入长期记忆，由 Candidate Extraction 过滤。
- 纯内存结构，按 chat 隔离，带长度上限，先进先出。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger

logger = get_logger("memory.working")

# 每个 chat 工作记忆的最大条数
MAX_WORKING_SIZE = 60
# STM 溢出阈值：缓冲累积到该条数后触发一次异步提炼
OVERFLOW_THRESHOLD = 10


@dataclass
class WorkingEntry:
    time: float
    author: str
    user_id: int = 0
    content: str = ""
    tag: str = "user"
    message_id: str = ""


class WorkingMemory:
    """工作记忆：按 chat_id 维护短期消息缓冲。"""

    def __init__(self, max_size: int = MAX_WORKING_SIZE,
                 overflow_threshold: int = OVERFLOW_THRESHOLD) -> None:
        self._buffers: dict[int, list[WorkingEntry]] = {}
        self._max_size = max_size
        self._overflow_threshold = overflow_threshold

    def append(self, chat_id: int, entry: WorkingEntry) -> list[WorkingEntry]:
        """追加一条工作记忆；返回达到溢出阈值被"弹出"的批次（供提炼）。"""
        buf = self._buffers.setdefault(chat_id, [])
        buf.append(entry)
        if len(buf) > self._max_size:
            buf.pop(0)
        if len(buf) >= self._overflow_threshold:
            snapshot = list(buf)
            buf.clear()
            return snapshot
        return []

    def snapshot(self, chat_id: int, limit: int = 10) -> list[WorkingEntry]:
        """最近 N 条工作记忆（供上下文注入）。"""
        buf = self._buffers.get(chat_id, [])
        return buf[-limit:]

    def clear(self, chat_id: int) -> None:
        self._buffers.pop(chat_id, None)

    def size(self, chat_id: int) -> int:
        return len(self._buffers.get(chat_id, []))