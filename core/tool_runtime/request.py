"""
Phase 8 Tool Runtime：工具请求模型（Huanmeng 2.0）

ToolRequest 把“Model Tool Request”与“Tool Execution”解耦：
模型只产出其 tool_call（tool_call_id + name + arguments），
ToolRuntime 负责把它规范化为 ToolRequest 并执行。
每个 ToolRequest 都携带 tool_call_id / trace_id / timeout / retry_budget，
满足“每次 Tool 都拥有 tool_call_id、trace_id、timeout、retry budget”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolRequest:
    """一次工具调用的请求。由 ToolRuntime 统一执行。"""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    # 请求归属（与 Model Tool Request 对齐）
    tool_call_id: str = ""
    trace_id: str = ""

    # 执行上下文
    user_id: int = 0
    group_id: int = 0
    chat_id: int = 0
    sender_name: str = ""
    is_group: bool = False
    bot_qq: int = 0
    original_msg: str = ""

    # 执行策略（Phase 8 统一由调用方/运行时给定）
    timeout: Optional[float] = None          # 单工具超时（秒），None 走默认
    retry_budget: Optional[int] = None       # 重试预算，None 走默认
    required_permission: Optional[str] = None  # 所需权限位，None 走工具默认映射

    # 内部：预算/重试状态
    attempt: int = 0

    @classmethod
    def from_tool_call(cls, tc: dict, *, trace_id: str = "",
                       **kwargs) -> "ToolRequest":
        """从 Model Tool Call dict 构造 ToolRequest（解耦入口）。"""
        args = tc.get("arguments") or {}
        if isinstance(args, str):
            import json as _json
            try:
                args = _json.loads(args)
            except Exception:
                args = {"raw": args}
        return cls(
            tool_name=tc.get("name", ""),
            arguments=args if isinstance(args, dict) else {"args": args},
            tool_call_id=tc.get("id", ""),
            trace_id=trace_id or tc.get("trace_id", ""),
            **kwargs,
        )

    def next_attempt(self) -> "ToolRequest":
        """返回重试备份（attempt+1），用于重试循环。"""
        return ToolRequest(
            tool_name=self.tool_name, arguments=self.arguments,
            tool_call_id=self.tool_call_id, trace_id=self.trace_id,
            user_id=self.user_id, group_id=self.group_id, chat_id=self.chat_id,
            sender_name=self.sender_name, is_group=self.is_group,
            bot_qq=self.bot_qq, original_msg=self.original_msg,
            timeout=self.timeout, retry_budget=self.retry_budget,
            required_permission=self.required_permission,
            attempt=self.attempt + 1,
        )