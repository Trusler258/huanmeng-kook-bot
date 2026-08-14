"""
Phase 18 测试：Reliability
覆盖：Circuit Breaker 状态机（CLOSED/OPEN/HALF_OPEN）、Timeout、Retry+Backoff、
Fallback、统一 call_with_resilience、Graceful Degradation 降级。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.resilience.circuit import CircuitBreaker, CircuitState, CircuitOpenError
from core.resilience.resilience import (
    call_with_resilience, get_breaker, reset_all_breakers,
)
from core.resilience.degradation import (
    register_degradation, invoke_with_degradation, get_degradation,
)


def test_circuit_breaker_states():
    cb = CircuitBreaker("test-dep", failure_threshold=3, recovery_timeout=1.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request()

    # 连续 3 次失败 → OPEN
    for _ in range(3):
        cb.on_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.allow_request()

    # 冷却后 HALF_OPEN 放行探测
    import time
    time.sleep(1.1)
    assert cb.allow_request()
    assert cb.state == CircuitState.HALF_OPEN

    # 探测成功 → CLOSED
    cb.on_success()
    assert cb.state == CircuitState.CLOSED
    print("✓ test_circuit_breaker_states")


def test_breaker_reopens_on_half_probe_fail():
    cb = CircuitBreaker("test-dep2", failure_threshold=2, recovery_timeout=1.0)
    cb.on_failure(); cb.on_failure()
    assert cb.state == CircuitState.OPEN
    import time
    time.sleep(1.1)
    assert cb.allow_request()  # HALF_OPEN
    cb.on_failure()  # 探测失败 → 重新 OPEN
    assert cb.state == CircuitState.OPEN
    print("✓ test_breaker_reopens_on_half_probe_fail")


def test_call_with_retry_and_backoff():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    async def run():
        result, err = await call_with_resilience(
            "retry-dep", flaky, timeout=1.0, retries=2, backoff_base=0.01)
        return result, err

    result, err = asyncio.run(run())
    assert result == "ok"
    assert err is None
    assert calls["n"] == 3
    print("✓ test_call_with_retry_and_backoff")


def test_call_with_timeout_returns_err():
    async def slow():
        await asyncio.sleep(5)
        return "late"

    async def run():
        result, err = await call_with_resilience(
            "timeout-dep", slow, timeout=0.05, retries=0)
        return result, err

    result, err = asyncio.run(run())
    assert result is None
    assert err is not None and "超时" in err
    print("✓ test_call_with_timeout_returns_err")


def test_call_with_fallback():
    async def always_fail():
        raise RuntimeError("down")

    async def fallback():
        return "fallback-result"

    async def run():
        return await call_with_resilience(
            "fallback-dep", always_fail, retries=0, fallback=fallback)

    result, err = asyncio.run(run())
    assert result == "fallback-result"
    assert err is None
    print("✓ test_call_with_fallback")


def test_breaker_fast_fail():
    cb = CircuitBreaker("fastfail", failure_threshold=1, recovery_timeout=60)
    cb.on_failure()  # OPEN
    # 熔断打开时不调用 fn，直接快速失败
    called = {"n": 0}

    async def fn():
        called["n"] += 1
        return "should-not"

    async def run():
        return await call_with_resilience("fastfail", fn, breaker=cb, retries=0)

    result, err = asyncio.run(run())
    assert called["n"] == 0
    assert err is not None and "熔断" in err
    print("✓ test_breaker_fast_fail")


def test_graceful_degradation():
    async def primary(query):
        raise RuntimeError("embedding down")

    async def fts5(query):
        return f"FTS5: {query}"

    register_degradation("embedding", primary=primary, fallback=fts5,
                         retries=0, failure_threshold=1, recovery_timeout=60)

    async def run():
        return await invoke_with_degradation("embedding", query="hello")

    result, used = asyncio.run(run())
    assert used is True
    assert result == "FTS5: hello"
    # 熔断已打开 → 后续直接走 fallback
    p = get_degradation("embedding")
    assert p.is_degraded()
    print("✓ test_graceful_degradation")


def main():
    reset_all_breakers()
    test_circuit_breaker_states()
    test_breaker_reopens_on_half_probe_fail()
    test_call_with_retry_and_backoff()
    test_call_with_timeout_returns_err()
    test_call_with_fallback()
    test_breaker_fast_fail()
    test_graceful_degradation()
    print("\nPhase 18 全部测试通过 ✓")


if __name__ == "__main__":
    main()