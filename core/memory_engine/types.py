"""
Phase 11 Memory 2.0：记忆类型与数据模型（Huanmeng 2.0）

严格区分 Message 和 Memory：
- Message 是原始事实（conversations/messages 表，逐条记录）。
- Memory 是长期提炼（memories 表，带类型/重要度/置信度/生命周期）。

Memory 类型（type）至少包含：user_fact / preference / relationship / project /
technical_context / event / knowledge / task。

Memory 字段至少包含：id / user_id / conversation_id / type / content / summary /
importance / confidence / created_at / updated_at / last_accessed_at /
source_message_id / metadata / status。

本模块为纯数据定义，无副作用，可独立测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ── Memory 类型 ─────────────────────────────────────────────
MEMORY_TYPE_USER = "user_fact"            # 关于用户的事实
MEMORY_TYPE_PREFERENCE = "preference"     # 用户偏好
MEMORY_TYPE_RELATIONSHIP = "relationship" # 关系（如与 bot 的关系）
MEMORY_TYPE_PROJECT = "project"           # 项目相关内容
MEMORY_TYPE_TECH = "technical_context"    # 技术上下文
MEMORY_TYPE_EVENT = "event"               # 事件
MEMORY_TYPE_KNOWLEDGE = "knowledge"       # 知识
MEMORY_TYPE_TASK = "task"                 # 任务

MEMORY_TYPES: tuple[str, ...] = (
    MEMORY_TYPE_USER, MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_RELATIONSHIP,
    MEMORY_TYPE_PROJECT, MEMORY_TYPE_TECH, MEMORY_TYPE_EVENT,
    MEMORY_TYPE_KNOWLEDGE, MEMORY_TYPE_TASK,
)

# 兼容旧 memory_type（fact/preference/event/persona）映射到新类型
_MEMORY_TYPE_ALIAS = {
    "fact": MEMORY_TYPE_USER,
    "event": MEMORY_TYPE_EVENT,
    "preference": MEMORY_TYPE_PREFERENCE,
    "persona": MEMORY_TYPE_USER,
}


def normalize_memory_type(t: str) -> str:
    """将任意 memory_type 归一化到合法类型；未知类型归为 user_fact。"""
    t = _MEMORY_TYPE_ALIAS.get((t or "").lower().strip(), (t or "").lower().strip())
    return t if t in MEMORY_TYPES else MEMORY_TYPE_USER


# ── Memory 状态 ─────────────────────────────────────────────
MEMORY_STATUS_ACTIVE = "active"       # 正常可用
MEMORY_STATUS_PENDING = "pending"     # 候选，待 Consolidation 确认
MEMORY_STATUS_MERGED = "merged"       # 已合并到其他记忆
MEMORY_STATUS_SUPERSEDED = "superseded"  # 已被更新覆盖
MEMORY_STATUS_ARCHIVED = "archived"   # 已归档（低价值，不参与检索）

MEMORY_STATUSES: tuple[str, ...] = (
    MEMORY_STATUS_ACTIVE, MEMORY_STATUS_PENDING, MEMORY_STATUS_MERGED,
    MEMORY_STATUS_SUPERSEDED, MEMORY_STATUS_ARCHIVED,
)


@dataclass
class MemoryRecord:
    """一条提炼后的记忆。纯数据对象，用于 engine 内外流转。"""
    id: Optional[int] = None
    user_id: int = 0
    conversation_id: int = 0
    type: str = MEMORY_TYPE_USER
    content: str = ""
    summary: str = ""
    importance: float = 0.5
    confidence: float = 1.0
    created_at: int = 0              # 毫秒时间戳
    updated_at: int = 0
    last_accessed_at: int = 0
    source_message_id: str = ""
    metadata: dict = field(default_factory=dict)
    status: str = MEMORY_STATUS_ACTIVE
    vector_id: Optional[str] = None  # 预留：语义向量 id（未来接入向量检索）
    source: str = "user"             # user/bot/system

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "type": self.type,
            "content": self.content,
            "summary": self.summary,
            "importance": self.importance,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "source_message_id": self.source_message_id,
            "metadata": self.metadata,
            "status": self.status,
            "vector_id": self.vector_id,
            "source": self.source,
        }

    @classmethod
    def from_row(cls, row: dict) -> "MemoryRecord":
        """从存储行（dict 映射）构造 MemoryRecord，容忍缺字段。"""
        return cls(
            id=row.get("id"),
            user_id=row.get("user_id", 0),
            conversation_id=row.get("conversation_id", 0),
            type=normalize_memory_type(row.get("memory_type", row.get("type", MEMORY_TYPE_USER))),
            content=row.get("content", ""),
            summary=row.get("summary", ""),
            importance=float(row.get("importance", 0.5) or 0.5),
            confidence=float(row.get("confidence", 1.0) or 1.0),
            created_at=row.get("created_at", 0),
            updated_at=row.get("updated_at", 0),
            last_accessed_at=row.get("last_accessed_at", 0),
            source_message_id=row.get("source_message_id", ""),
            metadata=row.get("metadata", row.get("meta", {})),
            status=row.get("status", MEMORY_STATUS_ACTIVE),
            vector_id=row.get("vector_id"),
            source=row.get("source", "user"),
        )