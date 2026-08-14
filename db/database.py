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
        from db.fts import ensure_fts

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await ensure_fts(self._engine)
        logger.info("数据库表结构已就绪（含 FTS5）")

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