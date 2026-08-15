"""
业务 Repository 层（Huanmeng 2.0 Phase 2）

业务层只能使用这些 Repository，禁止直接写 SQL / 调 sqlite3 / 碰 ORM 细节。
每个 Repository 对应一个聚合根，提供领域化的查询方法。

依赖注入：Repository 通过构造传入 AsyncSession，由调用方（Service/UseCase）
在事务上下文中创建。原子性由外层事务保证（见 db.database session 上下文）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from .base import BaseRepository
from .models import (
    Conversation, Event, Memory, MemoryLink, Message, Plugin, PluginPermission,
    SearchCacheEntry, Task, TaskStep, ToolCall, User, UserProfile,
)


# ── conversations ───────────────────────────────────────────
class ConversationRepository(BaseRepository):
    model = Conversation

    async def get_or_create(self, conversation_id: int, channel_id: str = "",
                            conversation_type: str = "group") -> Conversation:
        now = _now_ms()
        obj = await self.find(conversation_id=conversation_id)
        if obj is None:
            obj = await self.create(
                conversation_id=conversation_id, channel_id=channel_id,
                conversation_type=conversation_type, created_at=now, updated_at=now,
            )
        return obj

    async def touch(self, conversation_id: int) -> None:
        await self._session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .values(updated_at=_now_ms())
        )


# ── messages ────────────────────────────────────────────────
class MessageRepository(BaseRepository):
    model = Message

    async def append(self, conversation_id: int, user_id: int, role: str,
                     content: str, channel_id: str = "", message_id: str = "",
                     metadata: Optional[dict] = None, trace_id: str = "") -> Message:
        return await self.create(
            conversation_id=conversation_id, user_id=user_id, role=role,
            content=content, channel_id=channel_id, message_id=message_id,
            meta=metadata or {}, created_at=_now_ms(), trace_id=trace_id,
        )

    async def recent(self, conversation_id: int, limit: int = 50) -> list[Message]:
        stmt = (select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc()).limit(limit))
        result = await self._session.execute(stmt)
        return list(reversed(result.scalars().all()))

    async def by_trace(self, trace_id: str) -> list[Message]:
        return await self.list(trace_id=trace_id)

    async def fts(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 全文检索消息内容。"""
        from .fts import fts_search_messages
        from .database import db
        return await fts_search_messages(db._engine, query, limit)


# ── memories ────────────────────────────────────────────────
class MemoryRepository(BaseRepository):
    model = Memory

    async def add(self, content: str, user_id: int = 0, conversation_id: int = 0,
                  memory_type: str = "fact", importance: float = 0.5,
                  confidence: float = 1.0, source: str = "user",
                  summary: str = "", status: str = "active",
                  source_message_id: str = "", vector_id: str = "",
                  metadata: Optional[dict] = None) -> Memory:
        now = _now_ms()
        return await self.create(
            content=content, user_id=user_id, conversation_id=conversation_id,
            memory_type=memory_type, importance=importance, confidence=confidence,
            source=source, summary=summary, status=status,
            source_message_id=source_message_id, vector_id=vector_id,
            created_at=now, updated_at=now, last_accessed_at=now,
            meta=metadata or {},
        )

    async def top_for_user(self, user_id: int, limit: int = 10) -> list[Memory]:
        stmt = (select(Memory)
                .where(Memory.user_id == user_id)
                .order_by(Memory.importance.desc(), Memory.confidence.desc())
                .limit(limit))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def keyword(self, query: str, limit: int = 20) -> list[Memory]:
        """FTS5 关键词检索记忆。"""
        from .fts import fts_search_memories
        from .database import db
        rows = await fts_search_memories(db._engine, query, limit)
        return rows

    async def search(self, query: str, limit: int = 20,
                     conversation_id: int | None = None,
                     user_id: int | None = None,
                     since_ms: int | None = None) -> list[dict]:
        """FTS5 关键词检索记忆，支持 conversation/user/time 过滤，短词自动回退 LIKE。

        reuse 现有 fts_search_memories DAL；返回 dict 行（含 id/content/created_at 等）。
        """
        from .fts import fts_search_memories
        from .database import db
        return await fts_search_memories(
            db._engine, query, limit,
            conversation_id=conversation_id, user_id=user_id, since_ms=since_ms,
        )


# ── memory_links ────────────────────────────────────────────
class MemoryLinkRepository(BaseRepository):
    model = MemoryLink

    async def link(self, memory_id: int, related_id: int, link_type: str = "related",
                   strength: float = 1.0) -> MemoryLink:
        return await self.create(
            memory_id=memory_id, related_memory_id=related_id,
            link_type=link_type, strength=strength, created_at=_now_ms(),
        )


# ── users & profiles ────────────────────────────────────────
class UserRepository(BaseRepository):
    model = User

    async def get_or_create(self, user_id: int, username: str = "") -> User:
        obj = await self.find(user_id=user_id)
        now = _now_ms()
        if obj is None:
            obj = await self.create(user_id=user_id, username=username,
                                    created_at=now, updated_at=now)
        return obj


class UserProfileRepository(BaseRepository):
    model = UserProfile

    async def get_or_create(self, user_id: int) -> UserProfile:
        obj = await self.find(user_id=user_id)
        now = _now_ms()
        if obj is None:
            obj = await self.create(user_id=user_id, fav=0, persona={}, settings={},
                                    created_at=now, updated_at=now)
        return obj

    async def set_fav(self, user_id: int, fav: int) -> None:
        await self._session.execute(
            update(UserProfile).where(UserProfile.user_id == user_id).values(
                fav=fav, updated_at=_now_ms())
        )


# ── tasks & steps ───────────────────────────────────────────
class TaskRepository(BaseRepository):
    model = Task

    async def create_task(self, task_id: str, conversation_id: int, user_id: int,
                          kind: str = "agent", goal: str = "", trace_id: str = "") -> Task:
        now = _now_ms()
        return await self.create(
            task_id=task_id, conversation_id=conversation_id, user_id=user_id,
            kind=kind, state="CREATED", goal=goal, trace_id=trace_id,
            created_at=now, updated_at=now,
        )

    async def set_state(self, task_id: str, state: str, result: Optional[dict] = None,
                        error: str = "") -> None:
        values = {"state": state, "updated_at": _now_ms()}
        if result is not None:
            values["result"] = result
        if error:
            values["error"] = error
        await self._session.execute(update(Task).where(Task.task_id == task_id).values(**values))

    async def by_task_id(self, task_id: str) -> Optional[Task]:
        return await self.find(task_id=task_id)


class TaskStepRepository(BaseRepository):
    model = TaskStep

    async def add_step(self, task_id: str, step_index: int, action: str = "",
                       state: str = "PENDING", detail: Optional[dict] = None,
                       trace_id: str = "") -> TaskStep:
        now = _now_ms()
        return await self.create(
            task_id=task_id, step_index=step_index, state=state, action=action,
            detail=detail or {}, trace_id=trace_id, created_at=now, updated_at=now,
        )


# ── tool_calls ──────────────────────────────────────────────
class ToolCallRepository(BaseRepository):
    model = ToolCall

    async def begin(self, tool_name: str, arguments: dict, conversation_id: int = 0,
                    user_id: int = 0, tool_call_id: str = "", trace_id: str = "") -> ToolCall:
        return await self.create(
            tool_call_id=tool_call_id, conversation_id=conversation_id, user_id=user_id,
            tool_name=tool_name, arguments=arguments, state="RUNNING",
            trace_id=trace_id, created_at=_now_ms(),
        )

    async def finish(self, id: int, result: dict, duration_ms: int, state: str = "OK",
                     error: str = "") -> None:
        await self._session.execute(
            update(ToolCall).where(ToolCall.id == id).values(
                result=result, state=state, duration_ms=duration_ms, error=error)
        )


# ── search_cache ────────────────────────────────────────────
class SearchCacheRepository(BaseRepository):
    model = SearchCacheEntry

    async def get(self, query: str, engine: str = "default") -> Optional[SearchCacheEntry]:
        return await self.find(query=query, engine=engine)

    async def put(self, query: str, result: dict, engine: str = "default",
                  ttl_seconds: int = 3600) -> SearchCacheEntry:
        now = _now_ms()
        # 缓存按 (query, engine) 唯一：写入前清掉旧条目，避免同 key 累积出多条，
        # 否则 find 可能命中陈旧的已过期行导致缓存失效/不一致。
        await self.delete_where(query=query, engine=engine)
        return await self.create(
            query=query, engine=engine, result=result,
            created_at=now, expires_at=now + ttl_seconds * 1000,
        )


# ── events ──────────────────────────────────────────────────
class EventRepository(BaseRepository):
    model = Event

    async def emit(self, event_type: str, conversation_id: int = 0, user_id: int = 0,
                   trace_id: str = "", payload: Optional[dict] = None) -> Event:
        return await self.create(
            event_type=event_type, conversation_id=conversation_id, user_id=user_id,
            trace_id=trace_id, payload=payload or {}, created_at=_now_ms(),
        )


# ── plugins & permissions ───────────────────────────────────
class PluginRepository(BaseRepository):
    model = Plugin

    async def get_or_register(self, plugin_id: str, name: str = "", version: str = "0.0.0",
                              kind: str = "python", entry: str = "") -> Plugin:
        obj = await self.find(plugin_id=plugin_id)
        now = _now_ms()
        if obj is None:
            obj = await self.create(plugin_id=plugin_id, name=name, version=version,
                                    kind=kind, entry=entry, enabled=True,
                                    created_at=now, updated_at=now)
        return obj


class PluginPermissionRepository(BaseRepository):
    model = PluginPermission

    async def check(self, plugin_id: str, user_id: int = 0, conversation_id: int = 0) -> bool:
        perm = await self.find(plugin_id=plugin_id, user_id=user_id, conversation_id=conversation_id)
        if perm is None:
            return True  # 默认允许
        return bool(perm.allowed)


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)