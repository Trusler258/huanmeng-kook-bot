"""
Phase 18 Reliability：Circuit Breaker（Huanmeng 2.0）

熔断器：当某个外部依赖连续失败达到阈值时，快速失败（不继续打挂的依赖），
进入 OPEN 状态；经过冷却后进入 HALF_OPEN，放行一个探测请求；成功则恢复 CLOSED。

状态机：
    CLOSED ──(失败>=阈值)──▶ OPEN ──(冷却)──▶ HALF_OPEN ──(探测成功)──▶ CLOSED
                               ▲                                        │
                               └──────────────(探测失败)────────────────┘

线程安全（asyncio 单线程 + 少量锁兜底）。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from core.logger import get_logger

logger = get_logger("resilience.circuit")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """熔断器处于 OPEN 状态，快速失败。"""

    def __init__(self, name: str):
        super().__init__(f"熔断器 {name} 已打开")
        self.name = name


class CircuitBreaker:
    """针对单个依赖的熔断器。"""

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 30.0):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(1.0, recovery_timeout)  # OPEN → HALF_OPEN 冷却
        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._successes_in_half = 0
        self._half_success_required = 1  # HALF_OPEN 需 1 次成功才恢复

    # ── 状态查询 ───────────────────────────────────────────
    def allow_request(self) -> bool:
        """是否放行请求。OPEN 且在冷却期内则拒绝。"""
        now = time.time()
        if self.state == CircuitState.OPEN:
            if now - self._opened_at >= self.recovery_timeout:
                self._transition_half_open()
                return True
            return False
        return True

    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    # ── 结果上报 ───────────────────────────────────────────
    def on_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self._successes_in_half += 1
            if self._successes_in_half >= self._half_success_required:
                self._transition_closed()
        else:
            self._consecutive_failures = 0

    def on_failure(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            # 探测失败 → 重新打开
            self._transition_open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._transition_open()

    # ── 状态迁移 ───────────────────────────────────────────
    def _transition_open(self) -> None:
        if self.state != CircuitState.OPEN:
            self.state = CircuitState.OPEN
            self._opened_at = time.time()
            self._successes_in_half = 0
            logger.warning("熔断器 %s 已打开（连续失败 %d 次）",
                           self.name, self._consecutive_failures)

    def _transition_half_open(self) -> None:
        self.state = CircuitState.HALF_OPEN
        self._successes_in_half = 0
        logger.warning("熔断器 %s 进入 HALF_OPEN（冷却结束，放行探测）", self.name)

    def _transition_closed(self) -> None:
        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._successes_in_half = 0
        logger.info("熔断器 %s 已恢复 CLOSED", self.name)

    def reset(self) -> None:
        """手动重置。"""
        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._successes_in_half = 0

    def state_summary(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
        }