"""
Phase 8 Tool Runtime：统一工具执行（Huanmeng 2.0）

在现有 `core.tools.execute_tool`（Function Calling 底层）之上建立 ToolRuntime，
统一负责：Permission、Timeout、Retry、Budget、Trace、Result Normalization。
Model Tool Request（tool_call）与 Tool Execution 解耦 —— 模型只产出 ToolRequest，
ToolRuntime 负责策略与执行。

设计约束（开发纪律）：
- 不推倒重写现有 Function Calling / execute_tool，只在其上包一层。
- 兼容层：保持 execute_tool 接口可用，业务代码可逐步迁移到 ToolRuntime。
- Budget 默认 OPEN（跟随现有行为），可配置上限；超过即返回失败结果，不抛异常。
- CancelledError 必须继续传播（不吞取消）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from core.logger import get_logger
from core.tool_runtime.config import (
    MAX_TOOL_CALLS_PER_REQUEST,
    DEFAULT_RETRY_BUDGET,
    RETRY_BACKOFF_BASE,
)
from core.tool_runtime.permission import check_permission, resolve_permission
from core.tool_runtime.request import ToolRequest
from core.tool_runtime.result import (
    ToolResult, OK, FAILED, TIMEOUT, CANCELLED, DENIED,
)

logger = get_logger("tool_runtime")


class BudgetExhausted(Exception):
    """请求级工具调用预算耗尽。"""


class ToolRuntime:
    """统一工具执行运行时。"""

    def __init__(self, max_calls: int = MAX_TOOL_CALLS_PER_REQUEST):
        self._max_calls = max_calls
        self._calls_this_request = 0

    # ── 预算 ────────────────────────────────────────────────
    def _check_budget(self) -> None:
        if self._max_calls > 0 and self._calls_this_request >= self._max_calls:
            raise BudgetExhausted(
                f"工具调用预算已耗尽（上限 {self._max_calls}）")

    def _consume_budget(self) -> None:
        self._calls_this_request += 1

    def reset_budget(self) -> None:
        self._calls_this_request = 0

    # ── 主入口 ──────────────────────────────────────────────
    async def execute(self, req: ToolRequest) -> ToolResult:
        """执行一次工具调用，返回规范化 ToolResult。永不抛异常（取消除外）。"""
        # 预算
        try:
            self._check_budget()
        except BudgetExhausted as e:
            return self._mk(req, FAILED, error=str(e), duration_ms=0.0)

        # 权限（默认 DENY）
        allowed, reason = check_permission(req.tool_name, None)
        if not allowed:
            logger.warning("工具被拒绝: %s (%s)", req.tool_name, reason)
            return self._mk(req, DENIED, error=reason, duration_ms=0.0)

        # 预算 + 重试
        self._consume_budget()
        retry_budget = req.retry_budget if req.retry_budget is not None else DEFAULT_RETRY_BUDGET
        attempt = 0
        backoff = 0.0
        while True:
            result = await self._exec_once(req)
            if result.is_success() or result.status == CANCELLED or result.status == DENIED:
                return result
            # 可重试：失败/超时
            if attempt >= retry_budget:
                return result
            attempt += 1
            backoff = RETRY_BACKOFF_BASE * (2 ** (req.attempt + attempt - 1))
            logger.warning("工具 %s 第%d次尝试 %s，%.2fs 后重试",
                           req.tool_name, attempt, result.status, backoff)
            await asyncio.sleep(backoff)
            req = req.next_attempt()
            result.retry_count = attempt

    # ── 单次执行 ────────────────────────────────────────────
    async def _exec_once(self, req: ToolRequest) -> ToolResult:
        from core.tools import execute_tool
        from core.trace import get_trace_id
        from core.eventbus import get_event_bus, EVENT_TOOL_CALLED, EVENT_TOOL_COMPLETED
        start = time.perf_counter()
        start_ms = start * 1000.0
        trace_id = req.trace_id or get_trace_id()
        get_event_bus().emit(EVENT_TOOL_CALLED, {
            "tool": req.tool_name, "trace_id": trace_id,
            "tool_call_id": req.tool_call_id, "attempt": req.attempt,
        })
        try:
            content = await execute_tool(
                req.tool_name, req.arguments,
                user_id=req.user_id, group_id=req.group_id,
                sender_name=req.sender_name, is_group=req.is_group,
                bot_qq=req.bot_qq, original_msg=req.original_msg,
                timeout=req.timeout,
            )
            end_ms = time.perf_counter() * 1000.0
            status = OK if content is not None else OK
            self._trace(req, status, start_ms, end_ms, trace_id, req.attempt)
            get_event_bus().emit(EVENT_TOOL_COMPLETED, {
                "tool": req.tool_name, "trace_id": trace_id,
                "tool_call_id": req.tool_call_id, "status": status,
                "duration_ms": round(end_ms - start_ms, 2),
            })
            return self._mk(req, status, content=content or "",
                            start_ms=start_ms, end_ms=end_ms,
                            duration_ms=end_ms - start_ms,
                            trace_id=trace_id, retry_count=req.attempt)
        except asyncio.CancelledError:
            end_ms = time.perf_counter() * 1000.0
            self._trace(req, CANCELLED, start_ms, end_ms, trace_id, req.attempt)
            raise  # 关键：不吞取消
        except asyncio.TimeoutError:
            end_ms = time.perf_counter() * 1000.0
            self._trace(req, TIMEOUT, start_ms, end_ms, trace_id, req.attempt)
            get_event_bus().emit(EVENT_TOOL_COMPLETED, {
                "tool": req.tool_name, "trace_id": trace_id,
                "tool_call_id": req.tool_call_id, "status": TIMEOUT,
                "duration_ms": round(end_ms - start_ms, 2),
            })
            return self._mk(req, TIMEOUT, error="工具超时",
                            start_ms=start_ms, end_ms=end_ms,
                            duration_ms=end_ms - start_ms, trace_id=trace_id,
                            retry_count=req.attempt)
        except Exception as e:
            end_ms = time.perf_counter() * 1000.0
            self._trace(req, FAILED, start_ms, end_ms, trace_id, req.attempt, error=str(e))
            get_event_bus().emit(EVENT_TOOL_COMPLETED, {
                "tool": req.tool_name, "trace_id": trace_id,
                "tool_call_id": req.tool_call_id, "status": FAILED,
                "duration_ms": round(end_ms - start_ms, 2), "error": str(e),
            })
            return self._mk(req, FAILED, error=str(e),
                            start_ms=start_ms, end_ms=end_ms,
                            duration_ms=end_ms - start_ms, trace_id=trace_id,
                            retry_count=req.attempt)

    # ── 工具函数 ────────────────────────────────────────────
    def _mk(self, req, status, *, content="", error=None,
            start_ms=0.0, end_ms=0.0, duration_ms=0.0, trace_id="",
            retry_count=0) -> ToolResult:
        return ToolResult(
            tool_name=req.tool_name, tool_call_id=req.tool_call_id,
            trace_id=trace_id or req.trace_id, status=status,
            content=content, error=error, retry_count=retry_count,
            duration_ms=round(duration_ms, 2),
            start_ms=round(start_ms, 2), end_ms=round(end_ms, 2),
            permission=resolve_permission(req.tool_name),
        )

    def _trace(self, req, status, start_ms, end_ms, trace_id, retry_count,
               error=None) -> None:
        from core.trace import record_tool_call
        record_tool_call(
            req.tool_name, (end_ms - start_ms), status,
            start_ms=start_ms, end_ms=end_ms,
            tool_call_id=req.tool_call_id, retry_count=retry_count,
            error=error,
        )


# ── 全局单例 ────────────────────────────────────────────────
_runtime: Optional[ToolRuntime] = None


def get_tool_runtime() -> ToolRuntime:
    global _runtime
    if _runtime is None:
        _runtime = ToolRuntime()
    return _runtime


async def call_tool(req: ToolRequest) -> ToolResult:
    """便捷入口：直接调用全局 ToolRuntime。"""
    return await get_tool_runtime().execute(req)