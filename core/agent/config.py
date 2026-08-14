"""
Phase 7 Agent 配置（Huanmeng 2.0）

安全边界全部配置化，复用 Phase 6 web_search 的环境变量覆盖风格：
- 不经 toml 加载，避免改动 bot_config.toml 结构与加载逻辑；
- 默认值内嵌，环境变量可覆盖，测试可注入。

常量：
    AGENT_ENABLED            全局开关（0 关闭，退回原 pipeline）
    MAX_PLAN_STEPS           最大 Plan 步骤数（硬上限，禁止无限 Agent Loop）
    MAX_REPLANNING           最大重规划次数
    TOOL_TIMEOUT             单工具执行超时（秒）
    TOTAL_TASK_TIMEOUT       整个 Agent 任务总超时（秒）
    PLANNER_LLM_TIMEOUT      规划 LLM 调用超时（秒）
    EVALUATOR_LLM_TIMEOUT    重规划判定 LLM 调用超时（秒）
"""
from __future__ import annotations

import os

AGENT_ENABLED: bool = os.getenv("AGENT_ENABLED", "1") == "1"
MAX_PLAN_STEPS: int = int(os.getenv("AGENT_MAX_PLAN_STEPS", "5"))
MAX_REPLANNING: int = int(os.getenv("AGENT_MAX_REPLANNING", "2"))
TOOL_TIMEOUT: float = float(os.getenv("AGENT_TOOL_TIMEOUT", "15"))
TOTAL_TASK_TIMEOUT: float = float(os.getenv("AGENT_TOTAL_TIMEOUT", "60"))
PLANNER_LLM_TIMEOUT: float = float(os.getenv("AGENT_PLANNER_TIMEOUT", "15"))
EVALUATOR_LLM_TIMEOUT: float = float(os.getenv("AGENT_EVALUATOR_TIMEOUT", "10"))

# 规划判定最小消息长度：过短消息不进入 LLM 规划（仍是 Fast Path）
PLANNER_MIN_MSG_LEN: int = int(os.getenv("AGENT_PLANNER_MIN_MSG_LEN", "12"))

# 规划应当触发的行为类关键词（用户明确要求"帮我/执行/找一下/分析"等）
PLANNER_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "帮我", "帮我查", "帮我找", "帮我做", "帮我写", "帮我分析",
    "查一下", "找一下", "执行一下", "分析一下", "研究一下", "计算一下",
    "整理一下", "总结一下", "对比一下", "调查一下", "搜索一下",
    "帮我算", "帮我整理", "帮我对比", "帮我调查", "帮我检查", "帮我看看",
)