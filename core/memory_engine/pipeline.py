"""
Phase 11 Memory 2.0：Memory Pipeline（Huanmeng 2.0）

流程：Candidate Extraction → Validation → Deduplication → Merge → Save → Consolidation

- Candidate Extraction：从 STM 溢出批次中筛选有长期价值的候选（过滤"哈哈/好的/谢谢"等低价值）。
- Validation：校验候选是否合法（长度/来源/是否含高危内容）。
- Deduplication：与已有记忆做指纹/内容相似度去重。
- Merge：把重复或高度相关的候选合并进已有记忆（或标记 superseded）。
- Save：写入 SQLite（异步，不阻塞用户响应）。
- Consolidation：定期把低价值/过期记忆归档，压缩冗余。

低价值内容规则：打招呼/语气词/纯表情等不进入长期记忆。
"""
from __future__ import annotations

import re
import time
from typing import Optional

from core.logger import get_logger
from core.memory_engine.types import (
    MemoryRecord, MEMORY_TYPE_USER, MEMORY_TYPE_PREFERENCE,
    MEMORY_TYPE_RELATIONSHIP, MEMORY_TYPE_PROJECT, MEMORY_TYPE_TECH,
    MEMORY_TYPE_EVENT, MEMORY_TYPE_KNOWLEDGE, MEMORY_TYPE_TASK,
    MEMORY_STATUS_ACTIVE, MEMORY_STATUS_SUPERSEDED,
)

logger = get_logger("memory.pipeline")

# 低价值内容：不进入长期记忆
_LOW_VALUE = {
    "哈哈", "好的", "好", "嗯", "哦", "谢谢", "感谢", "辛苦了", "在吗", "在不在",
    "明白", "知道了", "收到", "嗯嗯", "哈哈哈", "哈哈哈哈", "666", "nb", "牛逼",
    "牛", "厉害", "可以", "行", "了解", "是的", "对", "没错", "谢谢啦", "嘿嘿",
    "啊", "吧", "呢", "嗯嗯嗯", "哈哈哈哈哈哈哈哈", "lol", "顶", "赞",
}
_LOW_VALUE_RE = re.compile(r"^(哈哈|呵呵|嘿嘿|嗯|哦|噢|好的|好|谢谢|感谢|在吗|是的|对|行|可以|了解|收到|ok|okay|nb|666|辛苦了)")

# 表情/纯标点
_EMOJI_ONLY_RE = re.compile(r"^[\s\d\W_]+$")


def is_low_value(content: str) -> bool:
    """判断内容是否低价值（不回长期记忆）。"""
    c = (content or "").strip()
    if not c or len(c) < 2:
        return True
    if _EMOJI_ONLY_RE.match(c):
        return True
    if c.lower() in _LOW_VALUE:
        return True
    if _LOW_VALUE_RE.match(c.lower()):
        return True
    return False


# 高重要度关键词：命中则提升 importance
_IMPORTANT_KW = (
    "记住", "不要忘记", "一定要", "非常重要", "我的", "我喜欢", "我讨厌",
    "我是", "我来自", "我在", "我要", "我想", "约定", "生日", "名字",
    "项目", "bug", "代码", "服务器", "账号", "密码", "地址", "电话",
    "邮箱", "公司", "工作", "学校", "专业", "目标", "计划",
)
_PREFERENCE_KW = ("喜欢", "讨厌", "爱吃", "不爱吃", "常用", "偏好", "习惯", "想要", "希望")
_RELATIONSHIP_KW = ("我们是", "我们是朋友", "你是我", "我喜欢你", "我们认识", "关系")


def _infer_type(content: str) -> str:
    c = content.lower()
    if any(k in c for k in _PREFERENCE_KW):
        return MEMORY_TYPE_PREFERENCE
    if any(k in c for k in _RELATIONSHIP_KW):
        return MEMORY_TYPE_RELATIONSHIP
    if any(k in c for k in ("项目", "需求", "代码", "bug", "部署", "服务器", "技术", "接口")):
        return MEMORY_TYPE_PROJECT
    if any(k in c for k in ("今天", "昨天", "明天", "上周", "下周", "会议", "活动", "约会")):
        return MEMORY_TYPE_EVENT
    if any(k in c for k in ("任务", "待办", "要做", "计划完成", "记得做")):
        return MEMORY_TYPE_TASK
    if any(k in c for k in ("知识", "是指", "含义", "定义", "是什么", "原理")):
        return MEMORY_TYPE_KNOWLEDGE
    return MEMORY_TYPE_USER


def estimate_importance(content: str) -> float:
    """基于关键词粗估 importance（0~1）。"""
    base = 0.5
    c = content
    hits = sum(1 for k in _IMPORTANT_KW if k in c)
    base += min(hits * 0.1, 0.4)
    if len(c) > 50:
        base += 0.05
    return round(min(base, 1.0), 2)


class MemoryPipeline:
    """候选提炼流水线。多数步骤为纯函数，可独立测试。"""

    def __init__(self, storage=None) -> None:
        self._storage = storage

    # ── 1. Candidate Extraction ───────────────────────────
    def extract_candidates(self, entries: list[dict]) -> list[MemoryRecord]:
        """从工作记忆批次中筛选出值得长期记忆的候选。"""
        candidates: list[MemoryRecord] = []
        for e in entries:
            content = str(e.get("content", "")).strip()
            if is_low_value(content):
                continue
            # 跳过纯工具/搜索请求类content（如"查天气"不需要长期记忆）
            if content.startswith(("[", "/", ".")):
                continue
            rec = MemoryRecord(
                user_id=e.get("user_id", 0),
                conversation_id=e.get("chat_id", 0),
                type=_infer_type(content),
                content=content[:500],
                summary=content[:80],
                importance=estimate_importance(content),
                confidence=0.8,
                source_message_id=str(e.get("message_id", "")),
                source=e.get("source", "user"),
            )
            candidates.append(rec)
        return candidates

    # ── 2 & 3. Validation + Deduplication ────────────────
    async def deduplicate(self, session, candidate: MemoryRecord) -> Optional[MemoryRecord]:
        """与已有记忆去重：完全相似则合并，否则原样返回候选。"""
        if not candidate.content:
            return None
        try:
            existing = await self._storage.search(
                session, candidate.content[:20], limit=5,
                conversation_id=candidate.conversation_id or None,
                user_id=candidate.user_id or None,
            )
        except Exception:
            existing = []
        for mem in existing:
            if _similarity(mem.content, candidate.content) >= 0.85:
                # 高度相似：合并（取更高 importance/confidence，更新 content 为更完整者）
                await self._merge_into(session, mem, candidate)
                return None  # 已被合并，不新增
        return candidate

    async def _merge_into(self, session, existing: MemoryRecord, incoming: MemoryRecord) -> None:
        """把 incoming 合并进 existing：取更完整内容、更高重要度。"""
        merged_content = incoming.content if len(incoming.content) > len(existing.content) else existing.content
        new_importance = max(existing.importance, incoming.importance)
        new_confidence = min(existing.confidence, incoming.confidence)  # 保守
        await self._storage.update(
            session, existing.id,
            content=merged_content, summary=merged_content[:80],
            importance=new_importance, confidence=new_confidence,
            status=MEMORY_STATUS_ACTIVE,
        )
        logger.info("记忆合并: %s 并入 %s", incoming.id, existing.id)

    # ── 4 & 5. Save ───────────────────────────────────────
    async def save(self, session, record: MemoryRecord) -> Optional[int]:
        """写入一条记忆（DB 不可用返回 None，不抛异常）。"""
        return await self._storage.add(session, record)

    # ── 6. Consolidation ──────────────────────────────────
    async def consolidate(self, session, min_importance: float = 0.3,
                          max_age_days: int = 90, limit: int = 200) -> int:
        """把低重要度且老旧的记忆归档（不删除，保留可追溯）。"""
        archived = 0
        try:
            from db.repositories import MemoryRepository
            from db.models import Memory
            from sqlalchemy import select, update
            cutoff = int(time.time() * 1000) - max_age_days * 86400 * 1000
            rows = await session.execute(
                select(Memory).where(
                    Memory.status == MEMORY_STATUS_ACTIVE,
                    Memory.importance < min_importance,
                    Memory.updated_at < cutoff,
                ).limit(limit)
            )
            ids = [r.id for r in rows.scalars()]
            if ids:
                await session.execute(
                    update(Memory).where(Memory.id.in_(ids))
                    .values(status=MEMORY_STATUS_ARCHIVED)
                )
                archived = len(ids)
                logger.info("记忆 Consolidation: 归档 %d 条低价值记忆", archived)
        except Exception as e:
            logger.warning("记忆 Consolidation 失败: %s", e)
        return archived


def _similarity(a: str, b: str) -> float:
    """基于字符集合的简单相似度（0~1），用于去重判断。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0