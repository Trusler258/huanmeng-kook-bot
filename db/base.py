"""
Repository 基类（Huanmeng 2.0 Phase 2）

业务层只能通过 Repository 访问数据，禁止直接写 SQL / 调 sqlite3。
Repository 封装对单个聚合根的 CRUD，屏蔽具体 ORM/方言差异，
未来切换 PostgreSQL/MySQL 时业务层无需重写。

用法：
    class MessageRepo(BaseRepository[Message]):
        model = Message
        async def recent(self, conv_id, limit=20):
            ...
"""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """泛型 Repository 基类：提供通用 CRUD + 分页。"""

    model: type  # 子类必须指定

    def __init__(self, session: AsyncSession):
        self._session = session

    # ── 查询 ──
    async def get(self, id: int) -> Optional[T]:
        return await self._session.get(self.model, id)

    async def find(self, **filters) -> Optional[T]:
        stmt = select(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        result = await self._session.execute(stmt.limit(1))
        return result.scalars().first()

    async def list(self, limit: int = 100, offset: int = 0, order_by: Any = None, **filters) -> list[T]:
        stmt = select(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, **filters) -> int:
        from sqlalchemy import func
        stmt = select(func.count()).select_from(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        result = await self._session.execute(stmt)
        return int(result.scalar() or 0)

    # ── 写入 ──
    async def create(self, **values) -> T:
        obj = self.model(**values)
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def update(self, id: int, **values) -> Optional[T]:
        stmt = update(self.model).where(self.model.id == id).values(**values)
        await self._session.execute(stmt)
        return await self.get(id)

    async def delete(self, id: int) -> None:
        stmt = delete(self.model).where(self.model.id == id)
        await self._session.execute(stmt)

    async def delete_where(self, **filters) -> int:
        stmt = delete(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        result = await self._session.execute(stmt)
        return result.rowcount