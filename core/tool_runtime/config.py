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
# write_code 会编译并运行 C++（spawn 进程），默认拒绝，需显式授权
DENIED_TOOLS: set[str] = {
    "write_code",
    "system_status",
}

# 白名单工具：不在白名单的未知工具一律拒绝（默认 DENY）
ALLOWED_TOOLS: set[str] = {
    "weather", "search_web", "earthquake", "read_url", "wdsj", "wdsj_query",
    "wzq", "draw_card", "chess", "calc", "agent_think", "pgr", "whois",
}