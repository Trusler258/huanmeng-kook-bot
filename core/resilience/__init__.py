"""
Phase 18 Reliability / Graceful Degradation（Huanmeng 2.0）

统一可靠性能力：
- Circuit Breaker：熔断器（快速失败，不无限打挂外部依赖）
- call_with_resilience：Timeout + Retry + Exponential Backoff + Fallback + Circuit Breaker
- Graceful Degradation 注册表：标记各依赖的降级后备，故障时自动切换

覆盖依赖：LLM / Search / HTTP / GitHub / Database / Embedding / Playwright / Plugin。
"""
from __future__ import annotations

from core.resilience.circuit import (
    CircuitBreaker, CircuitState, CircuitOpenError,
)
from core.resilience.resilience import (
    call_with_resilience, call_with_resilience_sync,
    get_breaker, reset_all_breakers, breaker_summaries,
)
from core.resilience.degradation import (
    degradation_registry, register_degradation, get_degradation,
    invoke_with_degradation,
)

__all__ = [
    "CircuitBreaker", "CircuitState", "CircuitOpenError",
    "call_with_resilience", "call_with_resilience_sync",
    "get_breaker", "reset_all_breakers", "breaker_summaries",
    "degradation_registry", "register_degradation", "get_degradation",
    "invoke_with_degradation",
]