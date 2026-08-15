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

from core.logger import get_logger

logger = get_logger("db.fts")

# FTS5 虚拟表名（供 tokenizer 迁移检测使用）
FTS_TABLES = ("messages_fts", "memories_fts")

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


async def rebuild_fts(engine: AsyncEngine, tables=FTS_TABLES) -> None:
    """从 content 表重建 FTS 索引（external content 表迁移/回填用）。

    用 'rebuild' 指令从业务表重建索引，比逐行 INSERT 可靠。
    仅在有存量数据时执行；空表跳过。
    """
    for fts_name in tables:
        try:
            async with engine.begin() as conn:
                src_cnt = (await conn.execute(text(
                    f"SELECT count(*) FROM {fts_name.replace('_fts','')}"))).scalar()
                if src_cnt > 0:
                    await conn.execute(text(
                        f"INSERT INTO {fts_name}({fts_name}) VALUES ('rebuild')"))
                    logger.info("FTS 重建完成 %s (%s rows)", fts_name, src_cnt)
        except Exception as e:
            logger.warning("FTS 重建跳过 %s: %s", fts_name, e)


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


def _memory_filters(conversation_id: int | None, user_id: int | None,
                    since_ms: int | None, prefix: str = "") -> tuple[str, dict]:
    """构造 memories 过滤子句与参数；全部为可选，返回 (where_sql, params)。

    prefix 用于 FTS 路径（表带别名 m.），LIKE 路径传空（裸表）。
    """
    clauses: list[str] = []
    params: dict = {}
    if conversation_id:
        clauses.append(f"{prefix}conversation_id = :conv")
        params["conv"] = conversation_id
    if user_id:
        clauses.append(f"{prefix}user_id = :uid")
        params["uid"] = user_id
    if since_ms:
        clauses.append(f"{prefix}created_at >= :since")
        params["since"] = since_ms
    where_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


async def fts_search_memories(engine: AsyncEngine, query: str, limit: int = 20,
                              conversation_id: int | None = None,
                              user_id: int | None = None,
                              since_ms: int | None = None) -> list[dict]:
    """在 memories_fts 中检索记忆；短词自动回退 LIKE。

    支持 conversation/user/time(since_ms) 过滤。conversation_id 为空时按
    memories 表语义视为不限定（0 表示全局记忆，不参与过滤）。
    """
    match_expr = _match_expr(query)
    async with engine.connect() as conn:
        if match_expr:
            where_sql, params = _memory_filters(conversation_id, user_id, since_ms, prefix="m.")
            sql = text(
                "SELECT m.id, m.content, m.memory_type, m.importance, m.created_at "
                "FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                "WHERE memories_fts MATCH :q" + where_sql + " ORDER BY rank LIMIT :lim"
            )
            q = dict(params, q=match_expr, lim=limit)
            rows = await conn.execute(sql, q)
        else:
            where_sql, params = _memory_filters(conversation_id, user_id, since_ms)
            sql = text(
                "SELECT id, content, memory_type, importance, created_at FROM memories "
                "WHERE content LIKE :like" + where_sql + " ORDER BY id DESC LIMIT :lim"
            )
            q = dict(params, like=f"%{_plain(query)}%", lim=limit)
            rows = await conn.execute(sql, q)
        return [dict(r._mapping) for r in rows]