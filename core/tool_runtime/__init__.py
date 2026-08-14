"""
Phase 8 Tool Runtime 包（Huanmeng 2.0）

在现有 Function Calling 之上建立统一工具执行层：
        Model Tool Request → ToolRequest → ToolRuntime.execute → ToolResult
统一负责 Permission / Timeout / Retry / Budget / Trace / Result Normalization。

对外暴露：
  ToolRequest / ToolResult / ToolRuntime
  get_tool_runtime() / call_tool(req)
  状态常量 OK/FAILED/TIMEOUT/CANCELLED/DENIED
"""
from __future__ import annotations

from core.tool_runtime.request import ToolRequest
from core.tool_runtime.result import (
    ToolResult, OK, FAILED, TIMEOUT, CANCELLED, DENIED,
)
from core.tool_runtime.runtime import (
    ToolRuntime, BudgetExhausted, get_tool_runtime, call_tool,
)

__all__ = [
    "ToolRequest", "ToolResult", "ToolRuntime",
    "OK", "FAILED", "TIMEOUT", "CANCELLED", "DENIED",
    "BudgetExhausted", "get_tool_runtime", "call_tool",
]