"""
Phase 11 Memory 2.0：Memory Retrieval（Huanmeng 2.0）

流程：Filter → Candidate Retrieval → Score → Rerank → Deduplicate → Context Budget

- Filter：按 conversation / user / time / type 预过滤。
- Candidate Retrieval：FTS5 关键词候选（只取限定数量，避免全量扫描）。
- Score + Rerank：对候选做相关度 & 重要度 & 时效加权排序。
- Deduplicate：去重相似记忆。
- Context Budget：只把少量最相关内容注入 Prompt（预算内），不随记忆总量线性增长。
"""
from __future__ import annotations

import re
import time
from typing import Optional

from core.logger import get_logger
from core.memory_engine.types import MemoryRecord, normalize_memory_type

logger = get_logger("memory.retrieval")

# 注入预算（字符上限）
DEFAULT_BUDGET = 2000
# 单次最多检索候选数（限制检索成本）
MAX_CANDIDATES = 30
# 命中后注入的最多条数
MAX_INJECT = 8


def _keywords(text: str) -> set[str]:
    """提取关键词 + 中文 2-gram（提升中文召回率）。"""
    t = str(text).lower()
    toks = set(re.findall(r"[a-z0-9_]{2,}", t))
    zh = re.findall(r"[\u4e00-\u9fff]+", t)
    for seg in zh:
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return toks


def _score(candidate: MemoryRecord, query_kw: set[str]) -> float:
    """相关度打分：关键词命中 + 重要度 + 时效（last_accessed_at 越近越高）。"""
    if not query_kw:
        return candidate.importance * 0.5
    hay = (candidate.content + " " + candidate.summary).lower()
    hit = sum(1 for kw in query_kw if kw and kw in hay)
    if hit == 0:
        return 0.0
    recency = 1.0
    if candidate.last_accessed_at:
        age_days = max(0, (time.time() * 1000 - candidate.last_accessed_at) / 86400000)
        recency = max(0.3, 1.0 - age_days / 30.0)
    return hit * 2.0 + candidate.importance * 1.5 + recency * 0.5


class MemoryRetrieval:
    """记忆检索：只返回少量真正相关内容（预算内）。"""

    def __init__(self, storage=None) -> None:
        self._storage = storage

    async def retrieve(self, session, query: str,
                       conversation_id: Optional[int] = None,
                       user_id: Optional[int] = None,
                       since_ms: Optional[int] = None,
                       memory_type: Optional[str] = None,
                       budget: int = DEFAULT_BUDGET,
                       max_inject: int = MAX_INJECT) -> list[MemoryRecord]:
        """完整检索流程，返回注入预算内、按相关度排序的记忆。"""
        if not query or not query.strip():
            return []
        query_kw = _keywords(query)

        # 1. Filter + 2. Candidate Retrieval（限定候选数）
        candidates = await self._storage.search(
            session, query, limit=MAX_CANDIDATES,
            conversation_id=conversation_id, user_id=user_id,
            since_ms=since_ms, memory_type=memory_type,
        )
        if not candidates:
            return []

        # 3 & 4. Score + Rerank
        scored = [(c, _score(c, query_kw)) for c in candidates]
        scored = [(c, s) for c, s in scored if s > 0]

        # 5. Deduplicate（内容相似度）
        seen: list[str] = []
        deduped: list[MemoryRecord] = []
        for c, s in sorted(scored, key=lambda x: -x[1]):
            if any(_sim(c.content, x) > 0.9 for x in seen):
                continue
            seen.append(c.content)
            deduped.append(c)
            if len(deduped) >= max_inject:
                break

        # 6. Context Budget（字符上限）
        result: list[MemoryRecord] = []
        used = 0
        for c in deduped:
            line = _fmt(c)
            if used + len(line) > budget:
                break
            result.append(c)
            used += len(line)
        return result

    async def retrieve_text(self, session, query: str,
                            conversation_id: Optional[int] = None,
                            user_id: Optional[int] = None,
                            since_ms: Optional[int] = None,
                            memory_type: Optional[str] = None,
                            budget: int = DEFAULT_BUDGET) -> str:
        """检索并格式化为 LLM 可读文本（兼容 modules.memory.get_top_memories 语义）。"""
        recs = await self.retrieve(
            session, query, conversation_id=conversation_id, user_id=user_id,
            since_ms=since_ms, memory_type=memory_type, budget=budget,
        )
        if not recs:
            return ""
        header = "【长期记忆】"
        lines = [header] + [_fmt(r) for r in recs]
        return "\n".join(lines)


def _fmt(r: MemoryRecord) -> str:
    """格式化为一行记忆文本。"""
    label = f"[{r.type}]" if r.type not in ("user_fact",) else ""
    return f"{label} {r.content}".strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = len(sa | sb)
    return len(sa & sb) / union if union else 0.0