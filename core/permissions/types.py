"""
Phase 17 Permission：权限位与风险等级（Huanmeng 2.0）

统一权限位（Permission 位）：
    message.read / message.send / memory.read / memory.write / network /
    filesystem.read / filesystem.write / process.execute / github.read /
    github.write / plugin.install / plugin.update / config.write

原则：默认 DENY。任何主体（用户/插件/能力）未被显式授权前，一律拒绝。
外部不可信输入（Web / GitHub / Plugin / 搜索内容）不能修改 System Policy。

强制权限位（高风险操作）：Shell、文件删除、Git Push、部署、Plugin 安装、
Plugin 更新、生产配置修改等，必须绑定"审批"位 —— 即使有权限位也不够，
还要走 Plan → Preview → Approval → Execute 流程。
"""
from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    """统一权限位。默认 DENY。"""

    MESSAGE_READ = "message.read"
    MESSAGE_SEND = "message.send"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    NETWORK = "network"
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    PROCESS_EXECUTE = "process.execute"
    GITHUB_READ = "github.read"
    GITHUB_WRITE = "github.write"
    PLUGIN_INSTALL = "plugin.install"
    PLUGIN_UPDATE = "plugin.update"
    CONFIG_WRITE = "config.write"


# 所有权限位（用于校验合法值）
ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)


class RiskLevel(str, Enum):
    """操作风险等级。LOW 自动放行，HIGH 必须人工审批。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# 高风险操作 → 所需权限位 + 必须审批
# 即便权限位已授权，这些操作仍要求 Approval 流程（Plan→Preview→Approval→Execute）。
RISKY_OPERATIONS: dict[str, dict] = {
    "shell_execute":   {"permission": Permission.PROCESS_EXECUTE.value, "risk": RiskLevel.HIGH},
    "file_delete":     {"permission": Permission.FILESYSTEM_WRITE.value, "risk": RiskLevel.HIGH},
    "git_push":        {"permission": Permission.GITHUB_WRITE.value, "risk": RiskLevel.HIGH},
    "deploy":          {"permission": Permission.PROCESS_EXECUTE.value, "risk": RiskLevel.HIGH},
    "plugin_install":  {"permission": Permission.PLUGIN_INSTALL.value, "risk": RiskLevel.HIGH},
    "plugin_update":   {"permission": Permission.PLUGIN_UPDATE.value, "risk": RiskLevel.HIGH},
    "config_write":    {"permission": Permission.CONFIG_WRITE.value, "risk": RiskLevel.HIGH},
    "github_update":   {"permission": Permission.GITHUB_WRITE.value, "risk": RiskLevel.HIGH},
}


def is_valid_permission(perm: str) -> bool:
    return perm in ALL_PERMISSIONS


def risk_of(operation: str) -> RiskLevel:
    info = RISKY_OPERATIONS.get(operation)
    return info["risk"] if info else RiskLevel.LOW