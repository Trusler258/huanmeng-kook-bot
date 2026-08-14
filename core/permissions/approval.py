"""
Phase 17 Permission：高风险操作审批流（Plan → Preview → Approval → Execute）

高风险操作（Shell、文件删除、Git Push、部署、Plugin 安装/更新、生产配置修改等）
不能只靠权限位，必须走审批流程：
    Plan     → 检查权限位 + 评估风险，生成操作计划
    Preview  → 展示"将要做什么"（不可信输入不能自动通过）
    Approval → 人工/gateway 确认；默认拒绝
    Execute  → 仅审批通过后执行

禁止只依赖 Prompt 告诉模型"不要做危险操作"。这里把审批做成硬 gate。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from core.logger import get_logger
from core.permissions.types import Permission, RiskLevel, risk_of
from core.permissions.registry import get_registry, PermissionDenied

logger = get_logger("permissions.approval")

# 审批回调：process(plan) -> bool / str（True 通过，False/str 拒绝）
ApprovalCallback = Callable[["OperationPlan"], bool]


@dataclass
class OperationPlan:
    """一次高风险操作的完整计划。"""
    operation: str
    subject: str = ""
    role: str = "user"
    detail: str = ""                 # 具体做什么
    preview: str = ""                # 将要执行的动作预览
    permission: str = Permission.PROCESS_EXECUTE.value
    risk: RiskLevel = RiskLevel.HIGH
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    approved: bool = False
    approve_reason: str = ""
    result: str = ""                 # execute 后的结果

    def preview_text(self) -> str:
        return (f"[{self.risk.value}] {self.operation}\n"
                f"计划ID: {self.plan_id}\n{self.preview}\n"
                f"详情: {self.detail}")


class ApprovalGate:
    """高风险操作审批闸门。"""

    def __init__(self) -> None:
        self._approver: Optional[ApprovalCallback] = None
        self._pending: dict[str, OperationPlan] = {}

    def set_approver(self, cb: ApprovalCallback) -> None:
        """注入审批回调。回调返回 True=通过，False=str=拒绝。"""
        self._approver = cb

    # ── Plan ───────────────────────────────────────────────
    def plan(self, operation: str, permission: str, subject: str = "",
             role: str = "user", detail: str = "", preview: str = "") -> OperationPlan:
        """创建计划；若权限位不足直接抛 PermissionDenied。"""
        p = OperationPlan(
            operation=operation, subject=subject, role=role,
            detail=detail, preview=preview, permission=permission,
            risk=risk_of(operation),
        )
        # 权限位检查（默认 DENY）
        allowed, reason = get_registry().check(permission, subject, role)
        if not allowed:
            raise PermissionDenied(permission, subject,
                                   f"{operation} 权限不足: {reason}")
        self._pending[p.plan_id] = p
        return p

    # ── Preview + Approval ────────────────────────────────
    def approve(self, plan_id: str, forced: bool = False) -> tuple[bool, str]:
        """对计划做审批。高风险默认拒绝；无 approver 回调时拒绝。"""
        p = self._pending.get(plan_id)
        if not p:
            return False, "计划不存在"
        if forced:
            p.approved = True
            p.approve_reason = "forced-grant"
            logger.warning("强制审批通过 %s (%s)", p.operation, plan_id)
            return True, "forced-grant"
        if self._approver is None:
            p.approve_reason = "no-approver"
            logger.warning("高风险操作 %s 无审批回调，默认拒绝", p.operation)
            return False, "无审批回调，默认拒绝"
        try:
            r = self._approver(p)
        except Exception as e:
            p.approve_reason = f"approver-error:{e}"
            return False, f"审批回调异常: {e}"
        if isinstance(r, bool):
            approved = r
            reason = "approved" if r else "rejected"
        else:
            approved = False
            reason = str(r)
        p.approved = approved
        p.approve_reason = reason
        return approved, reason

    def approve_sync(self, plan: OperationPlan, approved: bool, reason: str) -> None:
        plan.approved = approved
        plan.approve_reason = reason

    # ── Execute ────────────────────────────────────────────
    async def execute(self, plan: OperationPlan, fn: Callable[[OperationPlan], Awaitable]) -> str:
        """仅审批通过后执行。未审批/被拒 → 抛 PermissionDenied，不执行。"""
        if not plan.approved:
            raise PermissionDenied(
                plan.permission, plan.subject,
                f"{plan.operation} 未获审批，拒绝执行")
        try:
            plan.result = str(await fn(plan))
        finally:
            self._pending.pop(plan.plan_id, None)
        return plan.result


# ── 全局单例 ───────────────────────────────────────────────
_gate: Optional[ApprovalGate] = None


def get_approval_gate() -> ApprovalGate:
    global _gate
    if _gate is None:
        _gate = ApprovalGate()
    return _gate