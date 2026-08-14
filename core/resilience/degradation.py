"""
Phase 18 Reliability：Graceful Degradation（Huanmeng 2.0）

优雅降级注册表：每种依赖声明一个"主调用"和"降级后备"，主调用失败/熔断时自动切换后备，
保证 Core 在某个依赖挂掉时仍可用（普通聊天继续、退回 FTS5、备用 Provider 等）。

    degrade("embedding", primary, fallback=FTS5)
    result = await invoke_with_degradation("embedding", query)

用法（装饰器或注册）：
    register_degradation("search", primary=search_fn, fallback=lambda **k: "")
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from core.logger import get_logger
from core.resilience.resilience import call_with_resilience, get_breaker

logger = get_logger("resilience.degradation")


class DegradationPolicy:
    """一个依赖的降级策略。"""

    def __init__(self, name: str, primary=None, fallback=None,
                 timeout: float = 15.0, retries: int = 1,
                 failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.name = name
        self.primary = primary
        self.fallback = fallback
        self.timeout = timeout
        self.retries = retries
        self.breaker = get_breaker(name, failure_threshold, recovery_timeout)
        self._last_degraded_at = 0.0

    def is_degraded(self) -> bool:
        """当前是否处于降级态（熔断打开）。"""
        return self.breaker.is_open()

    async def invoke(self, **kwargs):
        """主调用 → 失败自动降级。返回 (result, used_fallback)。"""
        if self.primary is None:
            return None, False
        if self.breaker.is_open():
            # 熔断打开：直接走降级
            if self.fallback is not None:
                return await self._call(self.fallback, kwargs), True
            return None, True

        result, err = await call_with_resilience(
            self.name, lambda: self._call(self.primary, kwargs),
            timeout=self.timeout, retries=self.retries,
            breaker=self.breaker,
        )
        if err is not None and self.fallback is not None:
            logger.warning("%s 降级到 fallback: %s", self.name, err)
            return await self._call(self.fallback, kwargs), True
        return result, False

    async def _call(self, fn, kwargs):
        r = fn(**kwargs)
        if asyncio.iscoroutine(r):
            return await r
        return r


# 注册表
_policies: dict[str, DegradationPolicy] = {}


def register_degradation(name: str, primary=None, fallback=None,
                         timeout: float = 15.0, retries: int = 1,
                         failure_threshold: int = 5, recovery_timeout: float = 30.0,
                         policy: Optional[DegradationPolicy] = None) -> DegradationPolicy:
    """注册（或覆盖）某依赖的降级策略。"""
    p = policy or DegradationPolicy(
        name, primary, fallback, timeout, retries, failure_threshold, recovery_timeout)
    _policies[name] = p
    return p


def get_degradation(name: str) -> Optional[DegradationPolicy]:
    return _policies.get(name)


async def invoke_with_degradation(name: str, **kwargs):
    """按依赖名调用，主失败自动降级。返回 (result, used_fallback)。"""
    p = _policies.get(name)
    if p is None:
        raise KeyError(f"未注册降级策略: {name}")
    return await p.invoke(**kwargs)


def degradation_registry() -> dict[str, dict]:
    return {n: {"degraded": p.is_degraded()} for n, p in _policies.items()}