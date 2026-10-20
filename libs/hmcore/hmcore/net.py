"""网络/缓存小工具 —— 从 plugins/kook 的 `_api_cached` 泛化而来。

原实现是插件类里的方法（60s TTL，命中返回 (val, True)）；
这里抽成可复用对象，插件各自持有一个实例即可：
    cache = TTLCache(60)
    val, hit = await cache.get_or("key", lambda: client.fetch(...))
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional


class TTLCache:
    """极简异步 TTL 缓存：命中返回 (值, True)，未命中调用 factory 后返回 (值, False)。

    factory 返回 None 时不写缓存（与 plugins/kook 原实现一致：失败不污染缓存）。
    """

    def __init__(self, ttl: float = 60.0):
        self.ttl = float(ttl)
        self._data: dict[str, tuple[float, Any]] = {}

    async def get_or(self, key: str, factory: Callable[[], Awaitable[Any]]):
        now = time.time()
        hit = self._data.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1], True
        val = await factory()
        if val is not None:
            self._data[key] = (now, val)
        return val, False

    def invalidate(self, key: Optional[str] = None) -> None:
        """清一个键；key=None 清全部。"""
        if key is None:
            self._data.clear()
        else:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)
