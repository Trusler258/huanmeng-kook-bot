"""
Phase 8 Tool Runtime：结果规范化（Huanmeng 2.0）

ToolResult 统一承载工具执行结果，供上层（LLM 循环 / Agent Executor）消费。
status ∈ OK / FAILED / TIMEOUT / CANCELLED / DENIED。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 状态常量
OK = "OK"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
DENIED = "DENIED"


@dataclass
class ToolResult:
    """一次工具执行的规范化结果。"""

    tool_name: str
    tool_call_id: str = ""
    trace_id: str = ""
    status: str = OK
    content: str = ""                 # 规范化后的自然语言结果（喂给 LLM）
    error: Optional[str] = None
    retry_count: int = 0
    duration_ms: float = 0.0
    start_ms: float = 0.0
    end_ms: float = 0.0
    permission: Optional[str] = None  # 实际使用的权限位

    def is_success(self) -> bool:
        return self.status == OK

    def is_retriable(self) -> bool:
        """是否值得重试：失败/超时且非取消/拒绝。"""
        return self.status in (FAILED, TIMEOUT)

    def to_context(self) -> str:
        """规范化后的文本，用于回填 LLM 的 role=tool 消息。"""
        if self.status == DENIED:
            return f"[工具 {self.tool_name} 已被拒绝执行] {self.error or '无权限'}"
        return self.content or ""

    def summary(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "status": self.status,
            "retry_count": self.retry_count,
            "duration_ms": round(self.duration_ms, 2),
        }