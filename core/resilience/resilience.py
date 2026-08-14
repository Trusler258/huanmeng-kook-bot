"""
Phase 18 Reliability：统一可靠性调用（Timeout / Retry / Backoff / Fallback / Circuit Breaker）

为外部依赖（LLM、Search、HTTP、GitHub、Database、Embedding、Playwright、Plugin）提供统一包裹：
    1. Circuit Breaker：熔断器快速失败，不无限打挂的依赖。
    2. Timeout：asyncio.wait_for 硬超时，超时计入失败。
    3. Retry + Exponential Backoff：失败重试（指数退避，含抖动）。
    4. Fallback：主调用失败后尝试备用调用（Graceful Degradation）。

用法：
    result, err = await call_with_resilience(
        name="llm",
        fn=lambda: llm_call(...),
        timeout=15.0, retries=2, backoff_base=0.5,
        fallback=lambda: fallback_llm_call(...),
    )
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Optional, TypeVar

from core.logger import get_logger
from core.resilience.circuit import CircuitBreaker, CircuitOpenError

logger = get_logger("resilience")

T = TypeVar("T")

# 熔断器注册表：name -> CircuitBreaker
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, failure_threshold: int = 5,
                recovery_timeout: float = 30.0) -> CircuitBreaker:
    """获取（或创建）某个依赖的熔断器。"""
    b = _breakers.get(name)
    if b is None:
        b = CircuitBreaker(name, failure_threshold, recovery_timeout)
        _breakers[name] = b
    return b


async def call_with_resilience(
    name: str,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: float = 15.0,
    retries: int = 1,
    backoff_base: float = 0.5,
    use_jitter: bool = True,
    fallback: Optional[Callable[[], Awaitable[T]]] = None,
    breaker: Optional[CircuitBreaker] = None,
):
    """统一可靠性调用。返回 (result, error)。成功时 error 为 None。"""
    cb = breaker or get_breaker(name)
    if not cb.allow_request():
        # 熔断打开：快速失败，尝试 fallback
        if fallback is not None:
            return await _run_fallback(fallback)
        return None, f"熔断器 {name} 已打开，快速失败"

    attempt = 0
    while True:
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
            cb.on_success()
            return result, None
        except asyncio.TimeoutError:
            cb.on_failure()
            err = f"{name} 超时 ({timeout}s)"
        except CircuitOpenError:
            cb.on_failure()
            err = f"{name} 熔断"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            cb.on_failure()
            err = f"{name} 失败: {e}"

        if attempt >= retries:
            break
        attempt += 1
        delay = backoff_base * (2 ** attempt)
        if use_jitter:
            delay *= random.uniform(0.8, 1.2)
        logger.warning("%s 第%d次尝试失败，%.2fs 后重试: %s",
                       name, attempt, delay, err)
        await asyncio.sleep(delay)

    # 重试耗尽 → fallback
    if fallback is not None:
        return await _run_fallback(fallback)
    return None, err


async def _run_fallback(fallback: Callable[[], Awaitable[T]]):
    try:
        result = await fallback()
        return result, None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return None, f"fallback 失败: {e}"


# ── 便捷：同步片段（用于 Memory/Embedding 等非 async 片段） ──
def call_with_resilience_sync(
    name: str,
    fn: Callable[[], T],
    *,
    retries: int = 1,
    backoff_base: float = 0.5,
    fallback: Optional[Callable[[], T]] = None,
    breaker: Optional[CircuitBreaker] = None,
) -> tuple[T, Optional[str]]:
    """同步版本：用于 asyncio.to_thread 不适合的轻量片段。"""
    cb = breaker or get_breaker(name)
    if not cb.allow_request():
        if fallback is not None:
            try:
                return fallback(), None
            except Exception as e:
                return None, f"fallback 失败: {e}"
        return None, f"熔断器 {name} 已打开，快速失败"

    attempt = 0
    while True:
        try:
            result = fn()
            cb.on_success()
            return result, None
        except Exception as e:
            cb.on_failure()
            err = f"{name} 失败: {e}"
        if attempt >= retries:
            break
        attempt += 1
        delay = backoff_base * (2 ** attempt)
        time.sleep(delay)

    if fallback is not None:
        try:
            return fallback(), None
        except Exception as e:
            return None, f"fallback 失败: {e}"
    return None, err


def reset_all_breakers() -> None:
    for b in _breakers.values():
        b.reset()


def breaker_summaries() -> list[dict]:
    return [b.state_summary() for b in _breakers.values()]