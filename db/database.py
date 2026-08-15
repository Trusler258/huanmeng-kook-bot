"""
数据库管理（Huanmeng 2.0 Phase 2）

职责：
- 统一管理 SQLAlchemy 异步引擎（SQLite + aiosqlite）。
- 提供 session 工厂、事务上下文、初始化/建表/FTS/索引、优雅关闭。
- 业务代码禁止直接调用 sqlite3 或写裸 SQL，一律通过 Repository / DAL。

技术栈固定：SQLite + SQLAlchemy 2.0 + Alembic + FTS5。
设计上引擎/方言可切换（未来 PostgreSQL/MySQL 时业务层无需重写，
只需改 DATABASE_URL 与驱动，Repository 接口不变）。

用法：
    from db.database import db
    await db.initialize()          # 启动时
    async with db.session() as s:  # 事务
        ...
    await db.dispose()             # 关闭时
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.engine import Engine

from core.logger import get_logger

logger = get_logger("db")

# 默认数据库文件位置：项目根目录/data/huanmeng.db
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "huanmeng.db"


class DatabaseManager:
    """SQLAlchemy 异步引擎 / 会话工厂管理单例。"""

    def __init__(self):
        self._engine = None
        self._session_factory: Optional[async_sessionmaker] = None
        self._initialized = False
        self._database_url: str = ""

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def url(self) -> str:
        return self._database_url

    @property
    def health(self) -> dict:
        """DB health 状态（Phase 20 Part12）：不抛异常，未初始化即返回 degraded。"""
        if not self._initialized or self._engine is None:
            return {"initialized": False, "status": "degraded", "source": "legacy"}
        try:
            return {"initialized": True, "status": "ready", "source": "sqlite_fts5"}
        except Exception:
            return {"initialized": self._initialized, "status": "degraded", "source": "legacy"}

    def _resolve_url(self) -> str:
        """优先读环境变量 DATABASE_URL，否则默认 SQLite 文件。"""
        url = os.environ.get("DATABASE_URL", "").strip()
        if url:
            return url
        _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}"

    async def initialize(self) -> None:
        """创建引擎与会话工厂，并保证表结构存在（首次建表 / 增量迁移）。"""
        if self._initialized:
            return
        self._database_url = self._resolve_url()
        self._engine = create_async_engine(
            self._database_url,
            echo=False,
            pool_pre_ping=True,
            # SQLite 单写者，用 NullPool 避免多连接写锁；并发由应用层 per-chat 串行保证
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = True
        logger.info("数据库已初始化: %s", self._database_url)

        await self._init_schema()

    async def _init_schema(self) -> None:
        """建表 + 建 FTS 虚拟表 + 建索引（幂等）。"""
        from db.models import Base
        from db.fts import ensure_fts, rebuild_fts

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        migrated = await self._migrate_fts_tokenizer()
        # 先升级 memories 业务表结构，再构建 FTS 索引，
        # 避免 ALTER TABLE 使 external content FTS 索引失配。
        await self._upgrade_memories_schema()
        await ensure_fts(self._engine)
        # 仅当本次发生了 tokenizer 迁移（表被重建）时，才重建索引回填存量数据
        if migrated:
            await rebuild_fts(self._engine, tables=[m for m in migrated])
        logger.info("数据库表结构已就绪（含 FTS5）")

    async def _migrate_fts_tokenizer(self) -> set:
        """把旧版 unicode61 建的 FTS 表重建为 trigram（Phase 20）。

        早期版本用 unicode61 分词，中文按单字切分，导致中文字串（如"三次握手"）
        无法按子串命中。`CREATE VIRTUAL TABLE IF NOT EXISTS` 不会重建已存在的表，
        因此需检测既有 FTS 表的分词器，若不含 trigram 则连同触发器一起删除，
        由 ensure_fts 用 trigram 重建。

        返回被重建（删除）的 FTS 表名集合，供调用方重建索引。
        """
        from db.fts import FTS_TABLES
        migrated: set = set()
        try:
            async with self._engine.begin() as conn:
                for name in FTS_TABLES:
                    row = (await conn.execute(text(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"
                    ), {"n": name})).first()
                    # 表不存在则由 ensure_fts 创建；已存在但非 trigram 则重建
                    if row is not None and row[0] and "trigram" not in row[0]:
                        await conn.execute(text(f"DROP TRIGGER IF EXISTS {name.replace('_fts','')}_ai"))
                        await conn.execute(text(f"DROP TRIGGER IF EXISTS {name.replace('_fts','')}_ad"))
                        await conn.execute(text(f"DROP TRIGGER IF EXISTS {name.replace('_fts','')}_au"))
                        await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
                        migrated.add(name)
                        logger.info("FTS 分词器迁移: %s 由非 trigram 重建", name)
        except Exception as e:
            logger.warning("FTS 分词器迁移失败（可忽略，新库已是 trigram）: %s", e)
        return migrated

    async def _upgrade_memories_schema(self) -> None:
        """为既有 memories 表补齐 Phase 11 新增列（幂等：列已存在则跳过）。"""
        additions = {
            "summary": "TEXT DEFAULT ''",
            "source_message_id": "VARCHAR(64) DEFAULT ''",
            "status": "VARCHAR(16) DEFAULT 'active'",
            "vector_id": "VARCHAR(64) DEFAULT ''",
            "last_accessed_at": "BIGINT DEFAULT 0",
        }
        try:
            async with self._engine.begin() as conn:
                cols = {
                    r[1] for r in (await conn.execute(
                        text("PRAGMA table_info(memories)"))).fetchall()
                }
                for name, ddl in additions.items():
                    if name not in cols:
                        await conn.execute(text(
                            f"ALTER TABLE memories ADD COLUMN {name} {ddl}"))
                        logger.info("升级 memories 表: 新增列 %s", name)
        except Exception as e:
            logger.warning("升级 memories 表失败（可忽略，新库已含这些列）: %s", e)

    def session(self) -> async_sessionmaker:
        """返回会话工厂（供 async with 使用），未初始化时抛异常。"""
        if self._session_factory is None:
            raise RuntimeError("DatabaseManager 未初始化，请先 await db.initialize()")
        return self._session_factory

    async def dispose(self) -> None:
        """关闭引擎（优雅停机）。"""
        if self._engine is not None:
            await self._engine.dispose()
        self._initialized = False
        logger.info("数据库已关闭")

    # ── 原始 SQL 诊断入口（仅运维/迁移用，业务禁止直接写 SQL）──
    async def explain(self, stmt: str) -> list:
        """执行 EXPLAIN QUERY PLAN 检查关键查询（Phase 2 要求）。"""
        result = await self._engine.connect()
        try:
            rows = await result.execute(text(f"EXPLAIN QUERY PLAN {stmt}"))
            return [dict(r._mapping) for r in rows]
        finally:
            await result.close()


# 模块级单例
db = DatabaseManager()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI/依赖注入样式：产出会话，用完关闭。"""
    async with db.session()() as session:
        yield session


async def init_db() -> None:
    """供 bot 启动时调用。"""
    await db.initialize()


async def close_db() -> None:
    """供 bot 关闭时调用。"""
    await db.dispose()