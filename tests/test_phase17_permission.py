"""
Phase 17 测试：Permission / Security
覆盖：默认 DENY / 角色授权 / 主体授权 / 拒绝覆盖 / 权限位缺失 / 审批流 Plan→Preview→Approval→Execute。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.permissions.types import Permission, RiskLevel, risk_of
from core.permissions.registry import PermissionRegistry, PermissionDenied
from core.permissions.approval import ApprovalGate, OperationPlan


def test_default_deny():
    reg = PermissionRegistry()
    allowed, _ = reg.check(Permission.MESSAGE_SEND.value, role="user")
    assert not allowed, "默认必须 DENY"
    # 未授权的高权限操作
    allowed, _ = reg.check(Permission.CONFIG_WRITE.value, role="admin")
    assert not allowed
    print("✓ test_default_deny")


def test_role_grant():
    reg = PermissionRegistry()
    reg.grant_role(Permission.MEMORY_READ.value, "admin")
    # admin 授权，admin 可用
    allowed, _ = reg.check(Permission.MEMORY_READ.value, role="admin")
    assert allowed
    # user 不可用（角色不继承）
    allowed, _ = reg.check(Permission.MEMORY_READ.value, role="user")
    assert not allowed
    print("✓ test_role_grant")


def test_role_hierarchy():
    reg = PermissionRegistry()
    reg.grant_role(Permission.MEMORY_READ.value, "op")
    # op 授权，admin 可继承（admin 等级更高）
    allowed, _ = reg.check(Permission.MEMORY_READ.value, role="admin")
    assert allowed
    print("✓ test_role_hierarchy")


def test_subject_grant_and_deny():
    reg = PermissionRegistry()
    reg.grant_subject(Permission.GITHUB_READ.value, "user_42")
    allowed, _ = reg.check(Permission.GITHUB_READ.value, subject="user_42")
    assert allowed
    allowed, _ = reg.check(Permission.GITHUB_READ.value, subject="user_99")
    assert not allowed
    # 显式拒绝覆盖主体授权
    reg.deny(Permission.GITHUB_READ.value, "user_42")
    allowed, _ = reg.check(Permission.GITHUB_READ.value, subject="user_42")
    assert not allowed, "显式拒绝应覆盖授权"
    print("✓ test_subject_grant_and_deny")


def test_require_raises():
    reg = PermissionRegistry()
    try:
        reg.require(Permission.PROCESS_EXECUTE.value, subject="x", role="user")
        assert False, "应抛出 PermissionDenied"
    except PermissionDenied:
        pass
    print("✓ test_require_raises")


def test_risk_of():
    assert risk_of("git_push") == RiskLevel.HIGH
    assert risk_of("shell_execute") == RiskLevel.HIGH
    assert risk_of("config_write") == RiskLevel.HIGH
    assert risk_of("unknown_op") == RiskLevel.LOW
    print("✓ test_risk_of")


def test_approval_flow():
    gate = ApprovalGate()
    # 先授权 config_write 权限位
    from core.permissions.registry import get_registry
    reg = get_registry()
    reg.grant_role(Permission.CONFIG_WRITE.value, "admin")

    # 注入审批回调：默认拒绝，仅当 preview 含 "allow" 才通过
    gate.set_approver(lambda p: "allow" in p.preview)

    # 1. Plan（权限位已授权）
    plan = gate.plan(
        "config_write", Permission.CONFIG_WRITE.value,
        subject="admin_1", role="admin",
        detail="修改生产配置", preview="allow: 修改 bot_config.toml",
    )
    assert plan.risk == RiskLevel.HIGH

    # 2. Approval（回调拒绝时）
    approved, reason = gate.approve(plan.plan_id)
    assert approved

    # 3. Execute
    async def fake_exec(p):
        return "ok"
    import asyncio
    result = asyncio.run(gate.execute(plan, fake_exec))
    assert result == "ok"
    print("✓ test_approval_flow")


def test_approval_reject_no_approver():
    gate = ApprovalGate()
    from core.permissions.registry import get_registry
    reg = get_registry()
    reg.grant_role(Permission.PLUGIN_UPDATE.value, "admin")
    plan = gate.plan(
        "plugin_update", Permission.PLUGIN_UPDATE.value, role="admin",
        detail="更新插件", preview="update plugin x",
    )
    # 无 approver → 默认拒绝
    approved, reason = gate.approve(plan.plan_id)
    assert not approved
    print("✓ test_approval_reject_no_approver")


def test_plan_denied_without_perm():
    gate = ApprovalGate()
    # 未授权 PLUGIN_INSTALL → plan 直接抛 PermissionDenied
    try:
        gate.plan("plugin_install", Permission.PLUGIN_INSTALL.value, role="user")
        assert False, "权限位不足应抛 PermissionDenied"
    except PermissionDenied:
        pass
    print("✓ test_plan_denied_without_perm")


def main():
    test_default_deny()
    test_role_grant()
    test_role_hierarchy()
    test_subject_grant_and_deny()
    test_require_raises()
    test_risk_of()
    test_approval_flow()
    test_approval_reject_no_approver()
    test_plan_denied_without_perm()
    print("\nPhase 17 全部测试通过 ✓")


if __name__ == "__main__":
    main()