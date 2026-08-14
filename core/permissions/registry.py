"""
Phase 17 Permission：PermissionRegistry（Huanmeng 2.0）

统一权限注册与检查。默认 DENY —— 只有显式授权（grant）的主体才允许。
- 支持按角色（admin/op/friend/user）与按主体的白名单授权。
- 支持拒绝覆盖（deny）优先级最高。
- 提供 callback 机制，供 Plugin / Capability 注册自己的权限位。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from core.logger import get_logger
from core.permissions.types import (
    Permission, ALL_PERMISSIONS, is_valid_permission,
)

logger = get_logger("permissions")

# 角色等级（数值越大权限越高）
ROLE_ORDER: dict[str, int] = {"user": 0, "friend": 1, "op": 2, "admin": 3}


class PermissionDenied(Exception):
    """权限不足。"""

    def __init__(self, permission: str, subject: str, reason: str = ""):
        super().__init__(reason or f"权限不足: {permission} (subject={subject})")
        self.permission = permission
        self.subject = subject


class PermissionRegistry:
    """统一权限注册表。线程安全（读多写少）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # permission -> set(role)  （角色级授权）
        self._role_grants: dict[str, set[str]] = {}
        # permission -> set(subject) （主体级授权，subject 如 user_id 字符串）
        self._subject_grants: dict[str, set[str]] = {}
        # 显式拒绝（优先级最高）
        self._denied: dict[str, set[str]] = {}
        # 自定义权限位注册
        self._custom_perms: set[str] = set()
        # 权限检查回调（外部可注入，用于合并其它来源的授权）
        self._check_callbacks: list[Callable[[str, str, str], Optional[bool]]] = []

    # ── 注册 ───────────────────────────────────────────────
    def register_permission(self, perm: str) -> None:
        with self._lock:
            self._custom_perms.add(perm)

    def grant_role(self, permission: str, role: str) -> None:
        self._grant(self._role_grants, permission, role)

    def grant_subject(self, permission: str, subject: str) -> None:
        self._grant(self._subject_grants, permission, subject)

    def deny(self, permission: str, subject_or_role: str) -> None:
        with self._lock:
            self._denied.setdefault(permission, set()).add(subject_or_role)

    def _grant(self, store: dict, permission: str, who: str) -> None:
        if not is_valid_permission(permission) and permission not in self._custom_perms:
            logger.warning("注册未知权限位: %s", permission)
        with self._lock:
            store.setdefault(permission, set()).add(who)

    def add_check_callback(self, cb: Callable[[str, str, str], Optional[bool]]) -> None:
        """外部回调：(permission, subject, role) -> True允许/False拒绝/None忽略。"""
        with self._lock:
            self._check_callbacks.append(cb)

    # ── 检查 ───────────────────────────────────────────────
    def check(self, permission: str, subject: str = "",
              role: str = "user") -> tuple[bool, str]:
        """默认 DENY。返回 (allowed, reason)。"""
        # 回调优先（可合并外部授权源）
        for cb in list(self._check_callbacks):
            try:
                r = cb(permission, subject, role)
            except Exception:
                r = None
            if r is not None:
                return (r, "external-callback") if r else (False, "external-callback-denied")

        # 显式拒绝优先
        if subject and subject in self._denied.get(permission, set()):
            return False, f"{subject} 被显式拒绝 {permission}"
        if role in self._denied.get(permission, set()):
            return False, f"角色 {role} 被显式拒绝 {permission}"

        # 主体级授权
        if subject and subject in self._subject_grants.get(permission, set()):
            return True, f"subject:{subject}"

        # 角色级授权：高等级角色继承低等级角色授权
        role_rank = ROLE_ORDER.get(role, 0)
        for granted_role, rank in ROLE_ORDER.items():
            if rank <= role_rank and granted_role in self._role_grants.get(permission, set()):
                return True, f"role:{granted_role}"

        return False, f"默认拒绝 {permission} (subject={subject}, role={role})"

    def require(self, permission: str, subject: str = "",
                role: str = "user") -> None:
        """检查并抛 PermissionDenied。"""
        allowed, reason = self.check(permission, subject, role)
        if not allowed:
            raise PermissionDenied(permission, subject, reason)


# ── 全局单例 ───────────────────────────────────────────────
_registry: Optional[PermissionRegistry] = None


def get_registry() -> PermissionRegistry:
    global _registry
    if _registry is None:
        _registry = PermissionRegistry()
    return _registry


def check_permission(permission: str, subject: str = "",
                     role: str = "user") -> tuple[bool, str]:
    """兼容入口：默认 DENY。"""
    return get_registry().check(permission, subject, role)