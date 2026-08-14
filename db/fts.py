"""
FTS5 全文检索（Huanmeng 2.0 Phase 2）

- messages_fts：对 messages.content 建 FTS，供消息检索。
- memories_fts：对 memories.content 建 FTS，供记忆关键词检索。
- 使用 external content 表 + triggers 同步，保证与业务表一致。
- 中文分词：unicode61 对 CJK 按单字切分，查询端把中文拆成单字
  以 AND 命中，支持 1~n 字中文关键词（如 "蓝色" → "蓝" AND "色"）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# 建表语句（幂等：IF NOT EXISTS）
_FTS_SQL = [
    # messages FTS
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, role, created_at UNINDEXED, trace_id UNINDEXED,
               content='messages', content_rowid='id', tokenize='trigram')
    """,
    # memories FTS
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, memory_type, created_at UNINDEXED,
               content='memories', content_rowid='id', tokenize='trigram')
    """,
    # 同步触发器（messages）
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid, content, role, created_at, trace_id)
      VALUES (new.id, new.content, new.role, new.created_at, new.trace_id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, content, role, created_at, trace_id)
      VALUES ('delete', old.id, old.content, old.role, old.created_at, old.trace_id);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, content, role, created_at, trace_id)
      VALUES ('delete', old.id, old.content, old.role, old.created_at, old.trace_id);
      INSERT INTO messages_fts(rowid, content, role, created_at, trace_id)
      VALUES (new.id, new.content, new.role, new.created_at, new.trace_id);
    END
    """,
    # 同步触发器（memories）
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
      INSERT INTO memories_fts(rowid, content, memory_type, created_at)
      VALUES (new.id, new.content, new.memory_type, new.created_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, content, memory_type, created_at)
      VALUES ('delete', old.id, old.content, old.memory_type, old.created_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
      INSERT INTO memories_fts(memories_fts, rowid, content, memory_type, created_at)
      VALUES ('delete', old.id, old.content, old.memory_type, old.created_at);
      INSERT INTO memories_fts(rowid, content, memory_type, created_at)
      VALUES (new.id, new.content, new.memory_type, new.created_at);
    END
    """,
]


async def ensure_fts(engine: AsyncEngine) -> None:
    """幂等创建 FTS5 虚拟表与同步触发器。"""
    async with engine.begin() as conn:
        for stmt in _FTS_SQL:
            await conn.execute(text(stmt))


def _plain(q: str) -> str:
    """去掉 FTS5 特殊字符后的纯文本。"""
    import re
    return re.sub(r'["*^:()\-\\]', " ", q).strip()


def _match_expr(q: str) -> str | None:
    """构造 FTS5 MATCH 表达式；有效字符 <3 时返回 None（改用 LIKE 回退）。

    trigram tokenizer 支持中文字符串 >=3 字的子串命中；
    2 字以内的中文关键词（常见）无法被 trigram 命中，走 LIKE。
    """
    plain = _plain(q)
    if len(plain) < 3:
        return None
    tokens = plain.split()
    return " AND ".join(f'"{t}"' for t in tokens)


async def fts_search_messages(engine: AsyncEngine, query: str, limit: int = 20) -> list[dict]:
    """在 messages_fts 中检索消息；短词自动回退 LIKE。"""
    match_expr = _match_expr(query)
    async with engine.connect() as conn:
        if match_expr:
            sql = text(
                "SELECT m.id, m.content, m.role, m.created_at, m.trace_id "
                "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH :q ORDER BY rank LIMIT :lim"
            )
            rows = await conn.execute(sql, {"q": match_expr, "lim": limit})
        else:
            sql = text(
                "SELECT id, content, role, created_at, trace_id FROM messages "
                "WHERE content LIKE :like ORDER BY id DESC LIMIT :lim"
            )
            rows = await conn.execute(sql, {"like": f"%{_plain(query)}%", "lim": limit})
        return [dict(r._mapping) for r in rows]


async def fts_search_memories(engine: AsyncEngine, query: str, limit: int = 20) -> list[dict]:
    """在 memories_fts 中检索记忆；短词自动回退 LIKE。"""
    match_expr = _match_expr(query)
    async with engine.connect() as conn:
        if match_expr:
            sql = text(
                "SELECT m.id, m.content, m.memory_type, m.importance, m.created_at "
                "FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                "WHERE memories_fts MATCH :q ORDER BY rank LIMIT :lim"
            )
            rows = await conn.execute(sql, {"q": match_expr, "lim": limit})
        else:
            sql = text(
                "SELECT id, content, memory_type, importance, created_at FROM memories "
                "WHERE content LIKE :like ORDER BY id DESC LIMIT :lim"
            )
            rows = await conn.execute(sql, {"like": f"%{_plain(query)}%", "lim": limit})
        return [dict(r._mapping) for r in rows]