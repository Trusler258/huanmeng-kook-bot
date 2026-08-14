"""
Phase 11 Memory 2.0：存储层（Huanmeng 2.0）

通过 Repository / DAL 访问 SQLite + FTS5，业务层禁止直接写 SQL。
- add / update / touch / merge / archive / search

搜索遵循「Filter → Candidate Retrieval → Score → Rerank → Deduplicate → Context Budget」，
即便记忆几十万条，也只返回少量真正相关的内容。
"""
from __future__ import annotations

import time
from typing import Optional

from core.logger import get_logger
from core.memory_engine.types import (
    MemoryRecord, MEMORY_STATUS_ACTIVE, MEMORY_STATUS_MERGED,
    MEMORY_STATUS_SUPERSEDED, MEMORY_STATUS_ARCHIVED, normalize_memory_type,
)

logger = get_logger("memory.storage")


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemoryStorage:
    """记忆存储：对 MemoryRepository + FTS 的领域化封装。"""

    # ── 写入 ──────────────────────────────────────────────
    async def add(self, session, record: MemoryRecord) -> Optional[int]:
        """新增一条记忆，返回新 id；DB 不可用返回 None。"""
        from db.repositories import MemoryRepository
        now = _now_ms()
        repo = MemoryRepository(session)
        try:
            obj = await repo.add(
                content=record.content,
                user_id=record.user_id,
                conversation_id=record.conversation_id,
                memory_type=record.type,
                importance=record.importance,
                confidence=record.confidence,
                source=record.source,
                summary=record.summary,
                status=record.status,
                source_message_id=record.source_message_id,
                vector_id=record.vector_id or "",
                metadata=record.metadata,
            )
            return obj.id
        except Exception as e:
            logger.warning("记忆写入失败: %s", e)
            return None

    async def get(self, session, memory_id: int) -> Optional[MemoryRecord]:
        from db.repositories import MemoryRepository
        try:
            obj = await MemoryRepository(session).get(memory_id)
            if obj is None:
                return None
            return MemoryRecord(
                id=obj.id, user_id=obj.user_id, conversation_id=obj.conversation_id,
                type=normalize_memory_type(obj.memory_type), content=obj.content,
                importance=obj.importance, confidence=obj.confidence,
                created_at=obj.created_at, updated_at=obj.updated_at,
                metadata=obj.meta or {}, source=obj.source,
            )
        except Exception as e:
            logger.warning("记忆读取失败: %s", e)
            return None

    async def update(self, session, memory_id: int, **fields) -> None:
        """更新记忆字段（content/importance/confidence/status/summary 等）。"""
        from db.repositories import MemoryRepository
        try:
            values = {"updated_at": _now_ms()}
            for k, v in fields.items():
                if k == "type":
                    values["memory_type"] = v
                elif k == "metadata":
                    values["meta"] = v
                else:
                    values[k] = v
            await MemoryRepository(session).update(memory_id, **values)
        except Exception as e:
            logger.warning("记忆更新失败: %s", e)

    async def touch(self, session, memory_id: int) -> None:
        """更新 last_accessed_at（检索命中时调用）。"""
        from db.repositories import MemoryRepository
        from db.models import Memory
        from sqlalchemy import update
        try:
            await session.execute(
                update(Memory).where(Memory.id == memory_id)
                .values(last_accessed_at=_now_ms())
            )
        except Exception as e:
            logger.warning("记忆 touch 失败: %s", e)

    async def mark_merged(self, session, memory_id: int, into_id: int) -> None:
        """标记一条记忆已合并到另一条。"""
        await self.update(session, memory_id, status=MEMORY_STATUS_MERGED,
                          metadata={"merged_into": into_id})

    async def mark_superseded(self, session, memory_id: int) -> None:
        await self.update(session, memory_id, status=MEMORY_STATUS_SUPERSEDED)

    async def archive(self, session, memory_id: int) -> None:
        await self.update(session, memory_id, status=MEMORY_STATUS_ARCHIVED)

    # ── 检索 ──────────────────────────────────────────────
    async def search(self, session, query: str, limit: int = 20,
                     conversation_id: Optional[int] = None,
                     user_id: Optional[int] = None,
                     since_ms: Optional[int] = None,
                     memory_type: Optional[str] = None) -> list[MemoryRecord]:
        """候选检索：FTS5 关键词 + 过滤（conversation/user/time/type）。"""
        from db.repositories import MemoryRepository
        try:
            rows = await MemoryRepository(session).search(
                query, limit=limit * 3,   # 多取一些候选供 Score/Rerank
                conversation_id=conversation_id,
                user_id=user_id,
                since_ms=since_ms,
            )
        except Exception as e:
            logger.warning("记忆检索失败: %s", e)
            return []
        out = []
        for r in rows:
            rec = MemoryRecord.from_row(r)
            if memory_type and rec.type != normalize_memory_type(memory_type):
                continue
            if rec.status and rec.status not in (MEMORY_STATUS_ACTIVE,):
                continue
            out.append(rec)
        return out[:limit]

    async def recent(self, session, limit: int = 20,
                     conversation_id: Optional[int] = None,
                     user_id: Optional[int] = None) -> list[MemoryRecord]:
        """最近记忆（fallback / 无 query 时）。"""
        from db.repositories import MemoryRepository
        try:
            objs = await MemoryRepository(session).list(
                limit=limit, order_by=None,
                conversation_id=conversation_id or 0,
                user_id=user_id or 0,
            )
            return [MemoryRecord.from_row({
                "id": o.id, "user_id": o.user_id, "conversation_id": o.conversation_id,
                "memory_type": o.memory_type, "content": o.content,
                "importance": o.importance, "confidence": o.confidence,
                "created_at": o.created_at, "updated_at": o.updated_at,
                "source": o.source, "metadata": o.meta or {},
            }) for o in objs]
        except Exception as e:
            logger.warning("最近记忆读取失败: %s", e)
            return []