"""
Phase 8 → Phase 17 → Phase 20 Tool Runtime：权限检查（Huanmeng 2.0）

原则：默认 DENY。只有白名单内的工具才允许执行；高风险工具（编译/运行任意进程、
系统状态等）一律拒绝。外部不可信输入（Web/Plugin/搜索结果）不能改变该策略。

Phase 20 Part5：从"工具名 → DENY"升级为 Capability → Action → Risk → Policy。
- 风险等级由 {capability}.{action} 决定（见 config.RISK_LEVELS）。
- 策略由风险等级决定（见 config.RISK_POLICY）：
    LOW    → ALLOW（直接放行）
    MEDIUM → ALLOW_WITH_TRACE（记录 trace 后放行）
    HIGH   → DENY（默认拒绝；如已通过 registry 显式授权则放行）
- 核心原则："生成代码"(code.generate, LOW) 与"执行代码"(code.execute, HIGH) 彻底分离。
- 保留 Phase 17 的 PermissionRegistry 作为 HIGH 操作的显式授权通道。
- 原 ALLOWED_TOOLS / DENIED_TOOLS / TOOL_PERMISSIONS 保留做兼容兜底。
"""
from __future__ import annotations

from typing import Optional

from core.tool_runtime.config import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    TOOL_PERMISSIONS,
    capability_of,
    policy_of,
    risk_of_tool,
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

    Phase 20 Part5 流程：
    1. 显式硬拒绝（DENIED_TOOLS，最高优先级）
    2. 风险等级 → 策略：
       - LOW → ALLOW
       - MEDIUM → ALLOW_WITH_TRACE
       - HIGH → 默认拒绝；如已通过 PermissionRegistry 显式授权则放行
    3. 工具白名单底层兜底（未知工具默认拒绝）
    """
    global _bootstrapped
    if not _bootstrapped:
        _bootstrap_role_grants()
        _bootstrapped = True

    # 1. 显式硬拒绝（最高优先级）
    if tool_name in DENIED_TOOLS:
        return False, f"工具 {tool_name} 属于高风险操作，默认拒绝"

    # 2. 风险等级 → 策略
    risk = risk_of_tool(tool_name)
    policy = policy_of(risk)
    cap, action = capability_of(tool_name)

    if policy == "ALLOW":
        return True, f"risk={risk} policy=ALLOW ({cap}.{action})"

    if policy == "ALLOW_WITH_TRACE":
        # MEDIUM：记录 trace 后放行
        try:
            from core.trace import record
            record("permission", f"{tool_name} risk={risk} MEDIUM 放行")
        except Exception:
            pass
        return True, f"risk={risk} policy=ALLOW_WITH_TRACE ({cap}.{action})"

    # 3. HIGH：默认拒绝；如已显式授权(registry)则放行
    perm = resolve_permission(tool_name, explicit)
    if perm:
        allowed, reason = get_registry().check(perm, role="admin")
        if allowed:
            return True, f"risk={risk} 显式授权: {reason}"
        return False, f"risk={risk} policy=DENY: 权限位 {perm} 未授权 ({reason})"

    # 未知工具（unknown.unknown）→ 默认拒绝
    if tool_name not in ALLOWED_TOOLS:
        return False, f"risk={risk} 工具 {tool_name} 不在白名单，默认拒绝"
    return False, f"risk={risk} policy=DENY: 高风险操作 {tool_name} 默认拒绝，需显式授权"


def permission_of(tool_name: str) -> str:
    """用户可读的权限位描述，用于 trace/日志。"""
    return resolve_permission(tool_name) or f"tool:{tool_name}"