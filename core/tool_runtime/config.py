"""
Phase 8 Tool Runtime 配置（Huanmeng 2.0）
统一工具执行的权限/超时/重试/预算默认值，均可通过环境变量覆盖。
"""
from __future__ import annotations

import os

# 单次请求内允许的最大工具调用次数（预算硬上限，防无限循环）
MAX_TOOL_CALLS_PER_REQUEST: int = int(os.getenv("TOOL_MAX_CALLS", "20"))

# 默认重试预算（次）；单工具失败后最多重试次数
DEFAULT_RETRY_BUDGET: int = int(os.getenv("TOOL_RETRY_BUDGET", "1"))

# 重试退避基数（秒）：delay = base * 2 ** attempt
RETRY_BACKOFF_BASE: float = float(os.getenv("TOOL_RETRY_BACKOFF", "0.5"))

# 默认单工具超时（秒），被 core.tools 的工具级默认覆盖
DEFAULT_TOOL_TIMEOUT: float = float(os.getenv("TOOL_TIMEOUT", "15.0"))

# 工具 → 所需权限位（Phase 17 会扩展为完整 Permission 系统）
# 未列出的工具：默认按 SAFE 处理（白名单之外若不在 DENY 则拒绝）
TOOL_PERMISSIONS: dict[str, str] = {
    "weather":       "network.read",
    "search_web":    "network.read",
    "earthquake":    "network.read",
    "read_url":      "network.read",
    "wdsj":          "network.read",
    "wdsj_query":    "network.read",
    "wzq":           "memory.read",
    "draw_card":     "memory.read",
}

# 高风险工具：默认 DENY（即使模型请求也拒绝执行）
# Phase 20 Part5：write_code 已降级为"生成代码"（code.generate, LOW → ALLOW），
# 不再列入 DENIED_TOOLS。此处仅保留真正的高风险操作（系统状态等）。
DENIED_TOOLS: set[str] = {
    "system_status",
}

# 白名单工具：不在白名单的未知工具一律拒绝（默认 DENY）
ALLOWED_TOOLS: set[str] = {
    "weather", "search_web", "earthquake", "read_url", "wdsj", "wdsj_query",
    "wzq", "draw_card", "chess", "calc", "agent_think", "pgr", "whois",
    "write_code",
}


# ── Phase 20 Part5：Capability → Action → Risk → Policy 风险模型 ──
# 核心原则："生成代码"与"执行代码"彻底分离。
#   * code.generate / file.create / file.send → LOW → ALLOW
#   * project.modify / agent.think          → MEDIUM → 记录并放行
#   * code.execute / shell.execute / system.modify / file.delete → HIGH → DENY/Approval
# 工具风险等级由 capability.action 决定，权限策略由风险等级决定。
RISK_LEVELS: dict[str, str] = {
    "network.read":   "LOW",
    "memory.read":    "LOW",
    "data.query":     "LOW",
    "calc.compute":   "LOW",
    "game.play":      "LOW",
    "code.generate":  "LOW",   # 生成代码（不执行）
    "file.create":    "LOW",   # 写普通代码文件
    "file.send":      "LOW",   # 生成附件并发送
    "file.read":      "LOW",
    "agent.think":    "MEDIUM",
    "project.modify": "MEDIUM",
    "code.execute":   "HIGH",  # 执行代码
    "shell.execute":  "HIGH",  # 执行 Shell/系统命令
    "system.status":  "HIGH",
    "system.modify":  "HIGH",  # 修改系统配置
    "file.delete":    "HIGH",  # 删除文件
    "system.admin":   "HIGH",  # 系统管理操作
}

# 风险等级 → 默认策略
#   LOW    → ALLOW（直接放行，不查权限位）
#   MEDIUM → ALLOW_WITH_TRACE（记录 trace 后放行）
#   HIGH   → DENY（默认拒绝；如已通过 registry 显式授权则放行 / 走审批）
RISK_POLICY: dict[str, str] = {
    "LOW":    "ALLOW",
    "MEDIUM": "ALLOW_WITH_TRACE",
    "HIGH":   "DENY",
}

# 工具 → (capability, action)
TOOL_CAPABILITY: dict[str, tuple[str, str]] = {
    "weather":       ("network", "read"),
    "search_web":    ("network", "read"),
    "earthquake":    ("network", "read"),
    "read_url":      ("network", "read"),
    "pgr":           ("network", "read"),
    "whois":         ("network", "read"),
    "wzq":           ("memory", "read"),
    "draw_card":     ("memory", "read"),
    "wdsj":          ("data", "query"),
    "wdsj_query":    ("data", "query"),
    "chess":         ("game", "play"),
    "calc":          ("calc", "compute"),
    "agent_think":   ("agent", "think"),
    "write_code":    ("code", "generate"),   # 只生成并发送文件，不执行
    "run_code":      ("code", "execute"),    # 沙箱真实执行（业务层管理员/审批控制）
    "code_execute":  ("code", "execute"),    # 独立执行入口（默认 HIGH）
    "shell_execute": ("shell", "execute"),
    "system_status": ("system", "status"),
    "git_push":      ("project", "modify"),
    "deploy":        ("system", "admin"),
    "file_delete":   ("file", "delete"),
    "config_write":  ("system", "modify"),
}


def capability_of(tool_name: str) -> tuple[str, str]:
    """返回工具的 (capability, action)。未知工具 → ("unknown", "unknown")。"""
    return TOOL_CAPABILITY.get(tool_name, ("unknown", "unknown"))


def risk_of_tool(tool_name: str) -> str:
    """返回工具风险等级 LOW / MEDIUM / HIGH。未知操作默认 HIGH（保守拒绝）。"""
    cap, action = capability_of(tool_name)
    return RISK_LEVELS.get(f"{cap}.{action}", "HIGH")


def policy_of(risk_level: str) -> str:
    """风险等级 → 默认策略。未知等级默认 DENY。"""
    return RISK_POLICY.get(risk_level, "DENY")