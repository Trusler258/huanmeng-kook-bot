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

from core.logger import get_logger

logger = get_logger("tool.permission")


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


def _is_plugin_tool(tool_name: str) -> bool:
    """工具名是否为插件动态注册的工具（register_tool 注册，source 以 plugin: 开头）。"""
    try:
        from core.capability.registry import get_capability_registry
        return get_capability_registry().find_plugin_tool(tool_name) is not None
    except Exception:
        return False


_WARNED_UNDECLARED: set[str] = set()


def _plugin_tool_cap(tool_name: str):
    """取插件动态注册工具的能力元数据（未注册返回 None）。"""
    try:
        from core.capability.registry import get_capability_registry
        return get_capability_registry().find_plugin_tool(tool_name)
    except Exception:
        return None


def _live_manifest_perms(plugin_name: str):
    """取插件当前 manifest.permissions（取不到返回 None，用于区分"空集"与"不可知"）。"""
    try:
        from core.plugin import get_plugin_manager
        mf = get_plugin_manager().manifest_of(plugin_name)
        if mf is None:
            return None
        return list(getattr(mf, "permissions", None) or [])
    except Exception:
        return None


def check_plugin_tool(tool_name: str) -> tuple[bool, str]:
    """插件工具的执行授权判定。

    ★ v2.3.101：此前这里无条件放行（注册即授权），插件只要 register_tool 就能绕过
    HIGH 风险默认拒绝。现在按"声明 → 归属 → 执行"三段判定：

      1. 工具必须仍在注册表里（插件卸载/禁用后不得继续可调）
      2. 声明过 permissions 的：执行期再验一次，必须仍在 manifest.permissions 内
         （manifest 被收窄后旧注册项自动失效）
      3. 未声明 permissions 的：迁移期放行，但只在"新工具名"场景下成立——
         核心工具名在注册时已被拒绝，这里再做一次兜底拒绝
    """
    cap = _plugin_tool_cap(tool_name)
    if cap is None:
        return False, f"插件工具 {tool_name} 不在注册表（插件可能已卸载/禁用）"

    plugin = cap.source.split(":", 1)[-1] if getattr(cap, "source", "") else "?"
    declared = bool(getattr(cap, "perm_declared", True))
    need = list(getattr(cap, "permissions", None) or [])

    if not declared:
        from core.plugin.api import _is_core_tool_name
        if _is_core_tool_name(tool_name):
            return False, (f"插件 {plugin} 的工具 {tool_name} 占用了核心工具名但未声明权限，"
                           "拒绝执行")
        if tool_name not in _WARNED_UNDECLARED:
            _WARNED_UNDECLARED.add(tool_name)
            logger.warning("插件工具 %s（%s）未声明 permissions，迁移期放行", tool_name, plugin)
        return True, f"plugin tool {tool_name} 未声明权限（迁移期放行，plugin={plugin}）"

    # 以 manifest.permissions 为唯一判据：插件不能自证权限，但也不能被角色授权表误杀
    # （message.read / message.send 这类基础权限不在角色授权表里，不能拿去查 reg.check）。
    live = _live_manifest_perms(plugin)
    if live is not None:
        missing = [p for p in need if p not in live]
        if missing:
            return False, (f"插件 {plugin} 的工具 {tool_name} 所需权限 {missing} "
                           "已不在 manifest.permissions 内（插件被降权？）")
    return True, f"plugin tool {tool_name} 已声明权限 {need} 且归属 {plugin}，放行"


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

    # 1.5 run_code 沙箱执行：权限层只做白名单放行，
    #     真正的"管理员直接放行 / 非管理员审批"控制在工具内部（core.tools._run_code_tool），
    #     避免 HIGH 风险默认 DENY 把管理员也一并挡掉。
    if tool_name == "run_code":
        if tool_name in ALLOWED_TOOLS:
            return True, "run_code 沙箱执行由工具内部按管理员/审批控制"
        return False, "run_code 不在白名单"

    # 插件动态注册的工具：按"声明 → 归属 → 执行"三段判定（★ v2.3.101 修授权洞）
    if _is_plugin_tool(tool_name):
        allowed, reason = check_plugin_tool(tool_name)
        try:
            from core.trace import record
            record("permission", f"plugin tool {tool_name}: {'ALLOW' if allowed else 'DENY'} {reason}")
        except Exception:
            pass
        return allowed, reason

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