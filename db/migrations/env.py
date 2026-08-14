"""Alembic env.py — 从 Huanmeng 数据层读取 URL 与 metadata。

要点：
- 数据库 URL：优先 env DATABASE_URL，否则默认 SQLite 文件（与 db.database 一致）。
- target_metadata：直接 import db.models.Base.metadata，保证与 ORM 一致。
- SQLite 下多线程写会锁，迁移是单进程连执行，无需特殊处理。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 把项目根加入 sys.path（env.py 位于 db/migrations/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.models import Base  # noqa: E402

config = context.config


def _resolve_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    default_db = PROJECT_ROOT / "data" / "huanmeng.db"
    default_db.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{default_db}"


if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = _resolve_url().replace("+aiosqlite", "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url().replace("+aiosqlite", "")
    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()