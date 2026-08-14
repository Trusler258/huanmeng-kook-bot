"""
Phase 8 Tool Runtime：权限检查（Huanmeng 2.0）

原则：默认 DENY。只有白名单内的工具才允许执行；高风险工具（编译/运行任意进程、
系统状态等）一律拒绝。外部不可信输入（Web/Plugin/搜索结果）不能改变该策略。
Phase 17 会将此升级为完整 Permission 系统（message/memory/network/fs/process...）。
"""
from __future__ import annotations

from typing import Optional

from core.tool_runtime.config import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    TOOL_PERMISSIONS,
)


def resolve_permission(tool_name: str, explicit: Optional[str] = None) -> Optional[str]:
    """返回该工具所需权限位。explicit 优先，其次工具默认映射。"""
    if explicit:
        return explicit
    return TOOL_PERMISSIONS.get(tool_name)


def check_permission(tool_name: str, explicit: Optional[str] = None) -> tuple[bool, str]:
    """权限检查。返回 (allowed, reason)。

    - 不在白名单 → 拒绝（默认 DENY）
    - 在高风险拒绝集 → 拒绝
    - 在白名单 → 允许
    """
    if tool_name in DENIED_TOOLS:
        return False, f"工具 {tool_name} 属于高风险操作，默认拒绝"
    if tool_name not in ALLOWED_TOOLS:
        return False, f"工具 {tool_name} 不在白名单，默认拒绝"
    return True, ""


def permission_of(tool_name: str) -> str:
    """用户可读的权限位描述，用于 trace/日志。"""
    return resolve_permission(tool_name) or f"tool:{tool_name}"