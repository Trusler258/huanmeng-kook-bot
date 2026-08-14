"""
数据层包（Huanmeng 2.0 Phase 2）

对外暴露：
  db                            DatabaseManager 单例
  init_db() / close_db()        启动/关闭
  UnitOfWork                    （推荐）一次事务内提供全部 Repository

用法（业务层）：
    from db import UnitOfWork
    from db.database import db
    async with UnitOfWork() as uow:      # 自动提交
        msg = await uow.messages.append(...)
        ...

    async with UnitOfWork() as uow:
        ...
        await uow.rollback()              # 失败时回滚
"""
from __future__ import annotations

from dataclasses import dataclass

from .database import DatabaseManager, db, init_db, close_db
from . import repositories as _repos


@dataclass
class UnitOfWork:
    """一次事务内统一暴露全部 Repository。

    所有写入共用同一 AsyncSession，提交时原子生效；异常自动回滚。
    """

    def __post_init__(self):
        self._session = None
        self.messages = None
        self.conversations = None
        self.memories = None
        self.memory_links = None
        self.users = None
        self.user_profiles = None
        self.tasks = None
        self.task_steps = None
        self.tool_calls = None
        self.search_cache = None
        self.events = None
        self.plugins = None
        self.plugin_permissions = None

    async def __aenter__(self) -> "UnitOfWork":
        if not db.initialized:
            raise RuntimeError("数据库未初始化，请先 await init_db()")
        self._session = db.session()()
        # 绑定各 Repository
        self.messages = _repos.MessageRepository(self._session)
        self.conversations = _repos.ConversationRepository(self._session)
        self.memories = _repos.MemoryRepository(self._session)
        self.memory_links = _repos.MemoryLinkRepository(self._session)
        self.users = _repos.UserRepository(self._session)
        self.user_profiles = _repos.UserProfileRepository(self._session)
        self.tasks = _repos.TaskRepository(self._session)
        self.task_steps = _repos.TaskStepRepository(self._session)
        self.tool_calls = _repos.ToolCallRepository(self._session)
        self.search_cache = _repos.SearchCacheRepository(self._session)
        self.events = _repos.EventRepository(self._session)
        self.plugins = _repos.PluginRepository(self._session)
        self.plugin_permissions = _repos.PluginPermissionRepository(self._session)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()

    async def rollback(self):
        if self._session is not None:
            await self._session.rollback()


__all__ = ["DatabaseManager", "db", "init_db", "close_db", "UnitOfWork"]