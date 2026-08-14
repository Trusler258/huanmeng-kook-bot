"""
Phase 11 Memory 2.0：MemoryEngine（Huanmeng 2.0）

统一入口，整合：
- storage（SQLite/FTS5 存储）
- working（工作记忆 / STM 缓冲）
- pipeline（Candidate Extraction → Validation → Deduplication → Merge → Save → Consolidation）
- retrieval（Filter → Candidate Retrieval → Score → Rerank → Deduplicate → Context Budget）

关键约束：
- 记忆写入必须异步，不能阻塞用户响应（asyncio.create_task 后台执行）。
- 普通"哈哈/好的/谢谢"等低价值内容不得进入长期记忆。
- 即使 Memory 几十万条，也只将少量真正相关内容放入 Prompt。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from core.logger import get_logger
from core.memory_engine.storage import MemoryStorage
from core.memory_engine.working import WorkingMemory, WorkingEntry
from core.memory_engine.pipeline import MemoryPipeline, is_low_value
from core.memory_engine.retrieval import MemoryRetrieval

logger = get_logger("memory.engine")


class MemoryEngine:
    """记忆引擎门面。"""

    def __init__(self) -> None:
        self.storage = MemoryStorage()
        self.working = WorkingMemory()
        self.pipeline = MemoryPipeline(storage=self.storage)
        self.retrieval = MemoryRetrieval(storage=self.storage)

    # ── Working Memory 写入 ──────────────────────────────
    def observe(self, chat_id: int, content: str, author: str = "",
                user_id: int = 0, tag: str = "user",
                message_id: str = "") -> None:
        """记录一条消息到工作记忆；若溢出则异步触发提炼（不阻塞调用方）。"""
        entry = WorkingEntry(
            time=time.time(), author=author, user_id=user_id,
            content=content, tag=tag, message_id=message_id,
        )
        overflow = self.working.append(chat_id, entry)
        if overflow:
            asyncio.create_task(self._ingest(chat_id, overflow))

    def working_snapshot(self, chat_id: int, limit: int = 10) -> list[WorkingEntry]:
        return self.working.snapshot(chat_id, limit)

    # ── 异步提炼 ─────────────────────────────────────────
    async def _ingest(self, chat_id: int, entries: list[WorkingEntry]) -> None:
        """后台提炼：Candidate Extraction → Validation → Dedup → Merge → Save。"""
        try:
            from db.database import db
            if not db.initialized:
                return
            records = self.pipeline.extract_candidates([
                {"content": e.content, "user_id": e.user_id, "chat_id": chat_id,
                 "message_id": e.message_id, "source": e.tag}
                for e in entries
            ])
            if not records:
                return
            async with db.session()() as s:
                for rec in records:
                    final = await self.pipeline.deduplicate(s, rec)
                    if final is not None:
                        await self.pipeline.save(s, rec)
            logger.info("异步记忆提炼完成 chat=%d: 候选%d 保存%d",
                        chat_id, len(records), len(records))
        except Exception as e:
            logger.warning("异步记忆提炼失败 chat=%d: %s", chat_id, e)

    # ── 检索（同步暴露经 asyncio.run 包装不适用，故此方法为 async）──
    async def retrieve(self, query: str, chat_id: Optional[int] = None,
                       user_id: Optional[int] = None, since_ms: Optional[int] = None,
                       memory_type: Optional[str] = None,
                       budget: int = 2000) -> str:
        """检索记忆并返回 LLM 可读文本。DB 不可用返回空串（Graceful Degradation）。"""
        try:
            from db.database import db
            if not db.initialized:
                return ""
            async with db.session()() as s:
                return await self.retrieval.retrieve_text(
                    s, query, conversation_id=chat_id, user_id=user_id,
                    since_ms=since_ms, memory_type=memory_type, budget=budget,
                )
        except Exception as e:
            logger.warning("记忆检索降级（返回空）: %s", e)
            return ""

    async def retrieve_top(self, query: str, chat_id: Optional[int] = None,
                           user_id: Optional[int] = None, limit: int = 8) -> list:
        """返回结构化记忆列表（供测试/调试）。"""
        try:
            from db.database import db
            if not db.initialized:
                return []
            async with db.session()() as s:
                return await self.retrieval.retrieve(
                    s, query, conversation_id=chat_id, user_id=user_id,
                    max_inject=limit,
                )
        except Exception:
            return []

    # ── 生命周期 ─────────────────────────────────────────
    async def consolidate(self, min_importance: float = 0.3, max_age_days: int = 90) -> int:
        try:
            from db.database import db
            if not db.initialized:
                return 0
            async with db.session()() as s:
                return await self.pipeline.consolidate(
                    s, min_importance=min_importance, max_age_days=max_age_days)
        except Exception as e:
            logger.warning("Consolidation 降级: %s", e)
            return 0


# ── 全局单例 ───────────────────────────────────────────────
_engine: Optional[MemoryEngine] = None


def get_memory_engine() -> MemoryEngine:
    global _engine
    if _engine is None:
        _engine = MemoryEngine()
    return _engine