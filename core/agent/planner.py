"""
Phase 7 Agent Planner（Huanmeng 2.0）

需求：
- 简单聊天 / 简单 command 不强制规划，继续 Fast Path；
- 复杂任务才生成结构化 Plan；
- Plan 至少含 goal / steps / current_step / required_skills / required_tools / status；
- 规划失败必须 fallback 原有 pipeline（返回 None）。

设计：
- `should_plan()`：纯规则，O(1)。简单聊天/纯标点/短消息/command 直接放行，
  不触发任何 LLM 规划调用。
- `plan()`：仅对 should_plan 通过的复杂任务调用一次 LLM 生成结构化 Plan。
  失败/超时/解析失败 → 返回 None（调用方回退原 pipeline），绝不丢消息。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from core.agent.config import (
    MAX_PLAN_STEPS,
    PLANNER_LLM_TIMEOUT,
    PLANNER_MIN_MSG_LEN,
    PLANNER_TRIGGER_KEYWORDS,
)
from core.agent.skill_registry import get_skill_registry
from core.logger import get_logger
from core.trace import record, record_llm, set_plan_summary

logger = get_logger("agent.planner")


@dataclass
class PlanStep:
    index: int = 0
    action: str = ""            # 步骤描述（面向 LLM/日志）
    tool: str = ""              # 工具名（若该步调工具/search_web 等）
    skill: str = ""             # Skill 名（若该步需加载某 Skill）
    params: dict = field(default_factory=dict)
    status: str = "PENDING"     # PENDING / RUNNING / OK / FAILED
    result: str = ""


@dataclass
class Plan:
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0
    required_skills: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    status: str = "PLANNED"     # PLANNED / RUNNING / COMPLETED / FAILED / REPLAN

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.__dict__ for s in self.steps],
            "current_step": self.current_step,
            "required_skills": self.required_skills,
            "required_tools": self.required_tools,
            "status": self.status,
        }


class AgentPlanner:
    """轻量任务规划器：只对复杂任务生成结构化 Plan。"""

    def __init__(self, skill_registry=None):
        self._skills = skill_registry or get_skill_registry()

    # ── 规则判定：是否需要规划 ──
    def should_plan(self, msg: str, intent: str = "", is_group: bool = True) -> bool:
        """决定是否进入规划。纯规则，不调用 LLM。

        返回 False 的情况（继续 Fast Path）：
        - 空消息 / 过短消息
        - command 意图（指令由指令管道处理，不需 Agent 规划）
        - 纯闲聊（无触发关键词）
        """
        if not msg:
            return False
        text = msg.strip()
        if len(text) < PLANNER_MIN_MSG_LEN:
            return False
        if intent == "command":
            return False
        # 行为类触发词 → 需要规划
        low = text.lower()
        if any(k in low for k in PLANNER_TRIGGER_KEYWORDS):
            return True
        # 复杂多步骤句式（先…再… / 并且… / 同时…）也视为需规划
        import re
        if re.search(r"(先.{1,12}再|然后|并且|同时|顺便|分别)", text):
            return True
        return False

    # ── 生成 Plan ──
    async def plan(self, task_request: str, enabled: bool = True) -> Optional[Plan]:
        """为复杂任务生成结构化 Plan。失败/超时/解析失败 → 返回 None（fallback）。

        enabled=False 时直接返回 None（Agent 全局关闭）。
        """
        if not enabled:
            return None
        if not task_request or not task_request.strip():
            return None

        from core.trace import span as _span
        with _span("planner"):
            return await self._do_plan(task_request)

    async def _do_plan(self, task_request: str) -> Optional[Plan]:
        try:
            from services.llm import call_llm
            from core.config import get_config

            tools_catalog = _tool_catalog()
            skills_catalog = self._skills.metadata()
            skills_text = "\n".join(
                f"- {m.get('name','')}: {m.get('description','')}" for m in skills_catalog
            ) or "（无可用 Skill）"
            tools_text = "\n".join(
                f"- {t['name']}: {t['desc']} 参数({', '.join(t['params'])})" for t in tools_catalog
            ) or "（无可用工具）"

            prompt = f"""你是任务规划器。把用户请求拆分为可执行步骤，输出 JSON。

用户请求：{task_request[:500]}

可用工具（名称+说明+参数摘要）：
{tools_text}

可用能力(Skill)：
{skills_text}

输出 JSON（不要输出其他内容）：
{{
  "goal": "一句话目标",
  "steps": [{{"action":"步骤描述","tool":"工具名或空","skill":"Skill名或空","params":{{"参数名":"值"}}}}],
  "required_skills": ["用到的Skill名"],
  "required_tools": ["用到的工具名"]
}}

规则：
- 简单问题不要拆步骤，steps 里 1 步即可。
- 需要联网信息用 tool="search_web"；需要查询数据用对应工具；需要特定能力用 skill。
- 不需要工具/能力的分析步骤，tool 和 skill 都留空。
- 最多 {MAX_PLAN_STEPS} 步。不要编造工具或 Skill 名。"""

            raw = await call_llm(
                get_config().reply_model,
                [{"role": "user", "content": prompt}],
                max_tokens=1200, temperature=0.2,
                timeout=PLANNER_LLM_TIMEOUT,
            )
            record_llm()
            if not raw or not raw.strip():
                logger.warning("Planner: LLM 返回空，放弃规划")
                return None

            data = _extract_json(raw)
            return self._build_plan(data, task_request)
        except Exception as e:
            logger.warning("Planner 规划失败，fallback 原 pipeline: %s", e)
            return None

    def _build_plan(self, data: Optional[dict], task_request: str) -> Optional[Plan]:
        """把 LLM 返回的 dict 校验并构建为 Plan；不合法返回 None。"""
        if not data or not isinstance(data, dict):
            return None
        goal = str(data.get("goal", "")).strip() or task_request[:80]
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            # 至少 1 步
            steps_raw = [{"action": goal}]

        steps: list[PlanStep] = []
        for i, s in enumerate(steps_raw[:MAX_PLAN_STEPS]):
            if not isinstance(s, dict):
                continue
            action = str(s.get("action", "")).strip() or f"步骤{i + 1}"
            tool = str(s.get("tool", "") or "").strip()
            skill = str(s.get("skill", "") or "").strip()
            params = s.get("params")
            steps.append(PlanStep(
                index=i, action=action, tool=tool, skill=skill,
                params=params if isinstance(params, dict) else {},
            ))
        if not steps:
            return None

        req_tools = data.get("required_tools")
        req_skills = data.get("required_skills")
        plan = Plan(
            goal=goal,
            steps=steps,
            required_tools=[str(t) for t in req_tools] if isinstance(req_tools, list) else [],
            required_skills=[str(sk) for sk in req_skills] if isinstance(req_skills, list) else [],
        )
        set_plan_summary(planned=True, reason="complex_task",
                         steps=len(steps),
                         tools=list(plan.required_tools),
                         skills=list(plan.required_skills))
        logger.info("Agent 规划完成 goal=%r steps=%d tools=%s skills=%s",
                    goal[:40], len(steps), plan.required_tools, plan.required_skills)
        return plan


# ── 工具目录：只暴露 名称/描述/参数摘要，不暴露完整 schema ──
def _tool_catalog() -> list[dict]:
    """从 get_tool_schemas 提取轻量目录（name/desc/参数名），不给完整 schema。"""
    from core.tools import get_tool_schemas
    out = []
    for t in get_tool_schemas():
        fn = t.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}).get("properties", {})
        out.append({"name": name, "desc": desc,
                    "params": list(params.keys()) if isinstance(params, dict) else []})
    return out


def _extract_json(raw: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON dict（容忍代码块/前后缀）。"""
    if not raw:
        return None
    text = raw.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 从文本中提取第一个 { ... }
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


# ── 全局单例 ────────────────────────────────────────────────
_planner: Optional[AgentPlanner] = None


def get_planner() -> AgentPlanner:
    global _planner
    if _planner is None:
        _planner = AgentPlanner()
    return _planner