"""
Phase 11 Memory 2.0（Huanmeng 2.0）

严格区分 Message 和 Memory：
- Message 是原始事实（conversations/messages 表）。
- Memory 是长期提炼（memories 表，带 type/importance/confidence/status 等）。

提供：
- types：Memory 类型与字段定义
- storage：SQLite/FTS5 存储
- working：工作记忆（STM 缓冲）
- pipeline：候选提炼（Extraction→Validation→Dedup→Merge→Save→Consolidation）
- retrieval：Filter→Candidate→Score→Rerank→Dedup→Budget
- engine：统一门面（异步写入，不阻塞响应）
"""
from core.memory_engine.types import (
    MemoryRecord, MEMORY_TYPES,
    MEMORY_TYPE_USER, MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_RELATIONSHIP,
    MEMORY_TYPE_PROJECT, MEMORY_TYPE_TECH, MEMORY_TYPE_EVENT,
    MEMORY_TYPE_KNOWLEDGE, MEMORY_TYPE_TASK,
    MEMORY_STATUS_ACTIVE, MEMORY_STATUS_PENDING, MEMORY_STATUS_MERGED,
    MEMORY_STATUS_SUPERSEDED, MEMORY_STATUS_ARCHIVED,
    normalize_memory_type,
)
from core.memory_engine.engine import MemoryEngine, get_memory_engine
from core.memory_engine.pipeline import MemoryPipeline, is_low_value
from core.memory_engine.retrieval import MemoryRetrieval
from core.memory_engine.storage import MemoryStorage
from core.memory_engine.working import WorkingMemory, WorkingEntry

__all__ = [
    "MemoryRecord", "MEMORY_TYPES",
    "MEMORY_TYPE_USER", "MEMORY_TYPE_PREFERENCE", "MEMORY_TYPE_RELATIONSHIP",
    "MEMORY_TYPE_PROJECT", "MEMORY_TYPE_TECH", "MEMORY_TYPE_EVENT",
    "MEMORY_TYPE_KNOWLEDGE", "MEMORY_TYPE_TASK",
    "MEMORY_STATUS_ACTIVE", "MEMORY_STATUS_PENDING", "MEMORY_STATUS_MERGED",
    "MEMORY_STATUS_SUPERSEDED", "MEMORY_STATUS_ARCHIVED",
    "normalize_memory_type",
    "MemoryEngine", "get_memory_engine",
    "MemoryPipeline", "is_low_value", "MemoryRetrieval",
    "MemoryStorage", "WorkingMemory", "WorkingEntry",
]