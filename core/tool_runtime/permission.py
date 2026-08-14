"""
Phase 8 → Phase 17 Tool Runtime：权限检查（Huanmeng 2.0）

原则：默认 DENY。只有白名单内的工具才允许执行；高风险工具（编译/运行任意进程、
系统状态等）一律拒绝。外部不可信输入（Web/Plugin/搜索结果）不能改变该策略。

Phase 17：在保留原有工具白名单的基础上，接入统一 PermissionRegistry。
- check_permission 先查统一权限系统（默认 DENY），再查工具白名单。
- 原有 ALLOWED_TOOLS / DENIED_TOOLS 作为最低层兜底，保证既有行为不倒退。
"""
from __future__ import annotations

from typing import Optional

from core.tool_runtime.config import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    TOOL_PERMISSIONS,
)
from core.permissions import get_registry


def resolve_permission(tool_name: str, explicit: Optional[str] = None) -> Optional[str]:
    """返回该工具所需权限位。explicit 优先，其次工具默认映射。"""
    if explicit:
        return explicit
    return TOOL_PERMISSIONS.get(tool_name)


def _bootstrap_role_grants() -> None:
    """把工具白名单映射为统一权限位授权（一次性）。"""
    reg = get_registry()
    # 网络类工具 → 授权 network（admin 级，避免误放行）
    for tool, perm in TOOL_PERMISSIONS.items():
        reg.register_permission(perm)
    # 白名单内的工具所需权限位，admin 角色默认授权
    for tool in ALLOWED_TOOLS:
        perm = TOOL_PERMISSIONS.get(tool)
        if perm:
            reg.grant_role(perm, "admin")


_bootstrapped = False


def check_permission(tool_name: str, explicit: Optional[str] = None) -> tuple[bool, str]:
    """权限检查。返回 (allowed, reason)。

    顺序：
    1. 统一权限系统（默认 DENY）——检查工具所需权限位是否对 admin 授权
    2. 工具白名单过滤器（最低层兜底）
    """
    global _bootstrapped
    if not _bootstrapped:
        _bootstrap_role_grants()
        _bootstrapped = True

    if tool_name in DENIED_TOOLS:
        return False, f"工具 {tool_name} 属于高风险操作，默认拒绝"

    # 统一权限系统：工具所需权限位
    perm = resolve_permission(tool_name, explicit)
    if perm:
        allowed, reason = get_registry().check(perm, role="admin")
        if not allowed:
            return False, f"工具 {tool_name} 权限位 {perm} 未授权: {reason}"

    # 工具白名单底层兜底
    if tool_name not in ALLOWED_TOOLS:
        return False, f"工具 {tool_name} 不在白名单，默认拒绝"
    return True, ""


def permission_of(tool_name: str) -> str:
    """用户可读的权限位描述，用于 trace/日志。"""
    return resolve_permission(tool_name) or f"tool:{tool_name}"