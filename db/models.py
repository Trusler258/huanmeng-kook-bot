"""
ORM 模型定义（Huanmeng 2.0 Phase 2）

核心表：
  conversations  / messages / memories / memory_links / users / user_profiles
  tasks / task_steps / tool_calls / search_cache / events / plugins / plugin_permissions

约定：
- 所有表主键用 Integer 自增 id（SQLite/Postgres/MySQL 通用）。
- 时间统一存 int 毫秒时间戳（created_at/updated_at），避免跨库时区问题。
- metadata 用 JSON Text 存储（跨库兼容），通过 TypeDecorator 序列化。
- 索引命名 index_{table}_{col}，便于 Alembic 迁移与 EXPLAIN 检查。
"""
from __future__ import annotations

import json

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index,
    Integer, LargeBinary, String, Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class JSONText(Text):
    """JSON 文本列：读写自动序列化/反序列化 dict/list。"""

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return json.dumps(value, ensure_ascii=False)
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            try:
                return json.loads(value)
            except Exception:
                return None
        return process


# ── conversations ───────────────────────────────────────────
class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, nullable=False)      # chat_id
    channel_id = Column(String(64), default="")
    conversation_type = Column(String(16), default="group")   # group / person
    title = Column(String(256), default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_conversations_conversation_id", "conversation_id"),
        Index("idx_conversations_updated_at", "updated_at"),
    )


# ── messages ────────────────────────────────────────────────
class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    channel_id = Column(String(64), default="")
    message_id = Column(String(64), default="")
    role = Column(String(16), nullable=False)                 # system/user/bot/tool
    content = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    meta = Column("metadata", JSONText, default=dict)          # 扩展字段（列名 metadata）
    trace_id = Column(String(32), default="")

    __table_args__ = (
        Index("idx_messages_conversation_id", "conversation_id"),
        Index("idx_messages_user_id", "user_id"),
        Index("idx_messages_channel_id", "channel_id"),
        Index("idx_messages_created_at", "created_at"),
        Index("idx_messages_trace_id", "trace_id"),
    )


# ── memories ────────────────────────────────────────────────
class Memory(Base):
    __tablename__ = "memories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, default=0)
    user_id = Column(BigInteger, default=0)
    content = Column(Text, nullable=False)
    summary = Column(Text, default="")                       # 记忆摘要
    memory_type = Column(String(32), default="fact")          # user_fact/preference/event/...
    importance = Column(Float, default=0.5)
    confidence = Column(Float, default=1.0)
    source = Column(String(32), default="user")              # user/bot/system
    source_message_id = Column(String(64), default="")        # 来源消息
    status = Column(String(16), default="active")             # active/pending/merged/superseded/archived
    vector_id = Column(String(64), default="")                # 预留：语义向量 id
    last_accessed_at = Column(BigInteger, default=0)          # 最近访问
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    meta = Column("metadata", JSONText, default=dict)

    __table_args__ = (
        Index("idx_memories_user_id", "user_id"),
        Index("idx_memories_conversation_id", "conversation_id"),
        Index("idx_memories_importance", "importance"),
        Index("idx_memories_confidence", "confidence"),
        Index("idx_memories_memory_type", "memory_type"),
        Index("idx_memories_updated_at", "updated_at"),
        Index("idx_memories_status", "status"),
        Index("idx_memories_user_importance", "user_id", "importance"),
    )


# ── memory_links ────────────────────────────────────────────
class MemoryLink(Base):
    __tablename__ = "memory_links"
    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(Integer, nullable=False)
    related_memory_id = Column(Integer, nullable=False)
    link_type = Column(String(32), default="related")
    strength = Column(Float, default=1.0)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_memory_links_memory_id", "memory_id"),
        Index("idx_memory_links_related", "related_memory_id"),
    )


# ── users ───────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    username = Column(String(128), default="")
    nickname = Column(String(128), default="")
    avatar = Column(String(512), default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_users_user_id", "user_id", unique=True),
    )


# ── user_profiles ───────────────────────────────────────────
class UserProfile(Base):
    __tablename__ = "user_profiles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    fav = Column(Integer, default=0)                          # 好感度
    persona = Column(JSONText, default=dict)                  # 用户专属人设
    settings = Column(JSONText, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_user_profiles_user_id", "user_id", unique=True),
    )


# ── tasks ───────────────────────────────────────────────────
class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False)
    conversation_id = Column(BigInteger, default=0)
    user_id = Column(BigInteger, default=0)
    kind = Column(String(32), default="agent")               # agent/search/update/...
    state = Column(String(16), default="CREATED")             # TaskState
    goal = Column(Text, default="")
    result = Column(JSONText, default=dict)
    error = Column(Text, default="")
    trace_id = Column(String(32), default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_tasks_task_id", "task_id", unique=True),
        Index("idx_tasks_conversation_id", "conversation_id"),
        Index("idx_tasks_user_id", "user_id"),
        Index("idx_tasks_state", "state"),
        Index("idx_tasks_trace_id", "trace_id"),
        Index("idx_tasks_updated_at", "updated_at"),
    )


# ── task_steps ──────────────────────────────────────────────
class TaskStep(Base):
    __tablename__ = "task_steps"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False)
    step_index = Column(Integer, default=0)
    state = Column(String(16), default="PENDING")
    action = Column(String(64), default="")
    detail = Column(JSONText, default=dict)
    trace_id = Column(String(32), default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_task_steps_task_id", "task_id"),
        Index("idx_task_steps_trace_id", "trace_id"),
    )


# ── tool_calls ──────────────────────────────────────────────
class ToolCall(Base):
    __tablename__ = "tool_calls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_call_id = Column(String(64), default="")
    conversation_id = Column(BigInteger, default=0)
    user_id = Column(BigInteger, default=0)
    tool_name = Column(String(128), nullable=False)
    arguments = Column(JSONText, default=dict)
    result = Column(JSONText, default=dict)
    state = Column(String(16), default="PENDING")            # PENDING/RUNNING/OK/ERROR
    error = Column(Text, default="")
    duration_ms = Column(Integer, default=0)
    trace_id = Column(String(32), default="")
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_tool_calls_tool_call_id", "tool_call_id"),
        Index("idx_tool_calls_conversation_id", "conversation_id"),
        Index("idx_tool_calls_user_id", "user_id"),
        Index("idx_tool_calls_tool_name", "tool_name"),
        Index("idx_tool_calls_trace_id", "trace_id"),
        Index("idx_tool_calls_created_at", "created_at"),
    )


# ── search_cache ────────────────────────────────────────────
class SearchCacheEntry(Base):
    __tablename__ = "search_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    query = Column(String(512), nullable=False)
    engine = Column(String(32), default="default")
    result = Column(JSONText, default=dict)
    created_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, default=0)

    __table_args__ = (
        Index("idx_search_cache_query", "query"),
        Index("idx_search_cache_query_engine", "query", "engine"),
        Index("idx_search_cache_expires_at", "expires_at"),
    )


# ── events（Runtime 生命周期）──────────────────────────────
class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)           # bot_started/task_completed/...
    conversation_id = Column(BigInteger, default=0)
    user_id = Column(BigInteger, default=0)
    trace_id = Column(String(32), default="")
    payload = Column(JSONText, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_events_event_type", "event_type"),
        Index("idx_events_trace_id", "trace_id"),
        Index("idx_events_created_at", "created_at"),
    )


# ── plugins ─────────────────────────────────────────────────
class Plugin(Base):
    __tablename__ = "plugins"
    id = Column(Integer, primary_key=True, autoincrement=True)
    plugin_id = Column(String(64), nullable=False)
    name = Column(String(128), default="")
    version = Column(String(32), default="0.0.0")
    kind = Column(String(16), default="python")               # python / lua
    entry = Column(String(256), default="")
    enabled = Column(Boolean, default=True)
    meta = Column("metadata", JSONText, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_plugins_plugin_id", "plugin_id", unique=True),
    )


# ── plugin_permissions ──────────────────────────────────────
class PluginPermission(Base):
    __tablename__ = "plugin_permissions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    plugin_id = Column(String(64), nullable=False)
    user_id = Column(BigInteger, default=0)
    conversation_id = Column(BigInteger, default=0)
    allowed = Column(Boolean, default=True)
    mask = Column(Integer, default=0)                          # 位掩码权限
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_plugin_permissions_plugin_user", "plugin_id", "user_id"),
        Index("idx_plugin_permissions_plugin_conversation", "plugin_id", "conversation_id"),
    )