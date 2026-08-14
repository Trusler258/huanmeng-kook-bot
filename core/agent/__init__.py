"""
Phase 7 Agent 能力包（Huanmeng 2.0）

对外暴露：
  AGENT_ENABLED / MAX_PLAN_STEPS / MAX_REPLANNING / TOOL_TIMEOUT / TOTAL_TASK_TIMEOUT
  get_planner()            AgentPlanner 单例（判断是否规划 + 生成结构化 Plan）
  get_executor()           AgentExecutor 单例（按 Plan 执行 Skill/Tool）
  get_skill_registry()     SkillRegistry 单例（metadata 发现 + 按需加载）
  ResultEvaluator          轻量结果评估器（规则优先，仅重规划调 LLM）
  Plan / PlanStep / AgentResult / AgentContext
"""
from __future__ import annotations

from core.agent.config import (
    AGENT_ENABLED,
    MAX_PLAN_STEPS,
    MAX_REPLANNING,
    TOOL_TIMEOUT,
    TOTAL_TASK_TIMEOUT,
)
from core.agent.evaluator import Evaluation, ResultEvaluator
from core.agent.executor import (
    AgentContext,
    AgentExecutor,
    AgentResult,
    get_executor,
)
from core.agent.planner import (
    Plan,
    PlanStep,
    AgentPlanner,
    get_planner,
)
from core.agent.skill_registry import SkillRegistry, get_skill_registry

__all__ = [
    "AGENT_ENABLED", "MAX_PLAN_STEPS", "MAX_REPLANNING", "TOOL_TIMEOUT",
    "TOTAL_TASK_TIMEOUT",
    "Plan", "PlanStep", "AgentPlanner", "get_planner",
    "AgentExecutor", "AgentContext", "AgentResult", "get_executor",
    "SkillRegistry", "get_skill_registry",
    "ResultEvaluator", "Evaluation",
]