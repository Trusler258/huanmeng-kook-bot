"""
Phase 17 Permission / Security（Huanmeng 2.0）

统一权限系统：默认 DENY + 角色/主体授权 + 高风险操作审批（Plan→Preview→Approval→Execute）。

对外公开：
- Permission（权限位枚举）
- PermissionRegistry（注册/检查，默认 DENY）
- ApprovalGate（高风险操作审批闸门）
- check_permission 便捷函数
- resolve_role：把 user_id 解析为角色（复用 core.config 的 admin/op/friend 判定）
"""
from __future__ import annotations

from core.permissions.types import (
    Permission, RiskLevel, ALL_PERMISSIONS, RISKY_OPERATIONS,
    is_valid_permission, risk_of,
)
from core.permissions.registry import (
    PermissionRegistry, PermissionDenied, get_registry, check_permission,
)
from core.permissions.approval import (
    ApprovalGate, OperationPlan, get_approval_gate,
)

__all__ = [
    "Permission", "RiskLevel", "ALL_PERMISSIONS", "RISKY_OPERATIONS",
    "is_valid_permission", "risk_of",
    "PermissionRegistry", "PermissionDenied", "get_registry", "check_permission",
    "ApprovalGate", "OperationPlan", "get_approval_gate",
    "resolve_role",
]


def resolve_role(user_id, group_id: int = 0) -> str:
    """把 user_id 解析为角色（user/friend/op/admin）。复用 core.config 判定。"""
    try:
        from core.config import get_config
        cfg = get_config()
    except Exception:
        return "user"
    try:
        if cfg.is_admin(user_id, group_id):
            return "admin"
        if cfg.is_op(user_id):
            return "op"
        if user_id in cfg.friend_qqs:
            return "friend"
    except Exception:
        pass
    return "user"