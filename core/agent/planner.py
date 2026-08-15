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

# ── Phase 20 Part8：用户任务约束 ──
# 从用户消息中抽取自然语言约束，进入 Task Context，供最终总结与执行循环使用。
# 例："一次说完" → completion_requirement=COMPLETE_IN_ONE_RESPONSE；
#     "继续/再详细/全部整理" → 续说（走 continuation 补齐完整内容）。
@dataclass
class TaskConstraints:
    completion_requirement: str = ""    # COMPLETE_IN_ONE_RESPONSE / CONTENT_STEPWISE / ""
    detail_level: str = ""              # high / medium / low / ""
    output_mode: str = ""               # text / list / step / ""
    user_constraints: list[str] = field(default_factory=list)

    def compact(self) -> str:
        parts = []
        if self.completion_requirement:
            parts.append(f"completion_requirement={self.completion_requirement}")
        if self.detail_level:
            parts.append(f"detail_level={self.detail_level}")
        if self.output_mode:
            parts.append(f"output_mode={self.output_mode}")
        if self.user_constraints:
            parts.append("user_constraints=" + "、".join(self.user_constraints))
        return " ".join(parts)

    is_one_shot = property(lambda self: self.completion_requirement == "COMPLETE_IN_ONE_RESPONSE")
    is_continuation = property(lambda self: self.completion_requirement == "CONTENT_STEPWISE")


# 一次说完 / 续说 / 详细 等约束关键词
_COMPLETE_ONE_SHOT = ("一次说完", "一次给我", "全部整理", "全部给我", "一步到位",
                      "全部一次", "一次说", "一次讲完", "一次性")
_COMPLETE_CONTINUE = ("继续", "再详细", "讲完", "说全", "说完整", "接着讲", "接着说",
                      "继续说", "全部整完", "继续说完")
_DETAIL_HIGH = ("详细", "具体", "写详细", "详细一点", "具体一点", "完整写出", "尽可能详细")
_OUTPUT_LIST = ("列表", "分点", "分条", "分步骤", "逐步", "步骤", "提纲")


def extract_constraints(msg: str) -> TaskConstraints:
    """从用户消息中抽取任务约束（纯规则，不调用 LLM）。"""
    text = msg or ""
    c = TaskConstraints()
    if any(p in text for p in _COMPLETE_ONE_SHOT):
        c.completion_requirement = "COMPLETE_IN_ONE_RESPONSE"
    if any(p in text for p in _COMPLETE_CONTINUE):
        c.completion_requirement = c.completion_requirement or "CONTENT_STEPWISE"
    if any(p in text for p in _DETAIL_HIGH):
        c.detail_level = "high"
    if any(p in text for p in _OUTPUT_LIST):
        c.output_mode = "list"
    for p in (_COMPLETE_ONE_SHOT + _COMPLETE_CONTINUE + _DETAIL_HIGH + _OUTPUT_LIST):
        if p in text:
            c.user_constraints.append(p)
    return c


# ── Phase 20 Part4：纯社交/闲聊/确认短句排除集 ──
# 无论消息多长，只要整体是这类内容，一律不进入 Planner（保持 Fast Path，0 次 Agent LLM）。
# 设计目的：避免"你好/哈哈/谢谢/好的"等普通聊天被当作复杂任务触发规划。
_CASUAL_TOKENS: tuple[str, ...] = (
    "你好", "您好", "你好呀", "哈喽", "嗨", "hello", "hi",
    "在吗", "在不在", "在不", "有人吗",
    "谢谢", "感谢", "多谢", "辛苦了", "拜托",
    "好的", "好滴", "好嘞", "好哒", "明白", "知道了", "了解", "收到",
    "嗯", "嗯嗯", "嗯呢", "哦", "哦哦", "哦好的",
    "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嘻嘻", "啧啧",
    "666", "6", "牛", "牛啊", "厉害", "nb", "牛批",
    "可以", "没问题", "行", "行吧", "对对", "对的", "是的", "没错",
    "再见", "拜拜", "晚安", "早", "早上好", "上午好", "中午好", "下午好", "晚上好",
    "好的谢谢", "谢谢啦", "辛苦啦", "在的", "我在", "怎么啦", "咋啦", "干嘛",
)

# 强任务动词：裸"帮我"需含此类动词才算真实任务，避免"帮我一下"这类闲聊误触发。
# 单字动词(写/做/查/算/搜)仅通过 PLANNER_TRIGGER_KEYWORDS 的"帮我写"等组合触发，
# 单独出现不触发规划，避免"写"/"查"等短续句误判。
_TASK_VERBS: tuple[str, ...] = (
    "写", "做", "做一", "查", "找", "搜", "分析", "整理", "总结", "对比",
    "研究", "调查", "计算", "算", "部署", "配置", "安装", "生成", "创建",
    "设计", "修改", "优化", "修复", "讲解", "解释", "介绍", "生成一", "编一",
    "搭建", "开发", "实现", "翻译", "转换", "统计", "汇总", "规划", "计划",
    "列出", "出一", "出一份", "编写", "推导", "求解", "评估", "检查", "审查",
)

# Phase 20 Part4：可直接触发规划的多字强任务动词（裸出现即视为复杂任务）。
# 覆盖"分析这个项目并修改优化/整理并搜索多个资料/完成一个多步骤任务/帮我部署 xxx"等。
# 均为 >1 字动词，避免"写/查/算"等单字续句误触发。
_TASK_INTENT_VERBS: tuple[str, ...] = (
    "分析", "部署", "配置", "安装", "整理", "总结", "对比", "研究", "调查",
    "设计", "优化", "修改", "生成", "创建", "搭建", "实现", "完成", "搜索",
    "统计", "汇总", "规划", "计划", "评估", "检查", "审查", "翻译", "转换",
    "编写", "推导", "求解", "修复", "讲解", "介绍", "开发", "构建", "搭建",
    "列出", "策划", "编写", "整理并搜索",
)


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
    # Phase 12：步骤依赖与成功条件（Verifier 用）
    dependencies: dict = field(default_factory=dict)   # {step_index: [前置step_index]}
    success_conditions: list[str] = field(default_factory=list)
    # Phase 20 Part8：用户任务约束（"一次说完"/“继续”/详细 等）
    constraints: TaskConstraints = field(default_factory=TaskConstraints)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.__dict__ for s in self.steps],
            "current_step": self.current_step,
            "required_skills": self.required_skills,
            "required_tools": self.required_tools,
            "status": self.status,
            "dependencies": self.dependencies,
            "success_conditions": self.success_conditions,
            "constraints": {
                "completion_requirement": self.constraints.completion_requirement,
                "detail_level": self.constraints.detail_level,
                "output_mode": self.constraints.output_mode,
                "user_constraints": list(self.constraints.user_constraints),
            },
        }


class AgentPlanner:
    """轻量任务规划器：只对复杂任务生成结构化 Plan。"""

    def __init__(self, skill_registry=None):
        self._skills = skill_registry or get_skill_registry()

    # ── 规则判定：是否需要规划 ──
    def should_plan(self, msg: str, intent: str = "", is_group: bool = True) -> bool:
        """决定是否进入规划。纯规则，不调用 LLM。

        返回 False 的情况（继续 Fast Path，0 次 Agent LLM）：
        - 空消息 / 过短消息
        - command 意图（指令由指令管道处理，不需 Agent 规划）
        - 纯社交/闲聊/确认（你好/哈哈/谢谢/好的 等），无论多长
        - 裸"帮我"但未紧跟强任务动词（如"帮我一下"）
        - 无触发关键词、无复杂句式
        """
        if not msg:
            return False
        text = msg.strip()
        if not text:
            return False
        if intent == "command":
            return False

        # Phase 20 Part4：纯社交/闲聊/确认 → 一律 Fast Path
        if self._is_casual(text):
            logger.debug("Agent: 闲聊短句，保持 Fast Path: %r", text[:30])
            return False

        low = text.lower()
        # Phase 20 P0：复杂度驱动，不因 intent=chat 就一律 Fast Path。
        # 知识类（历史/原理/教程/对比/分析）→ 进 Agent 展开，且豁免长度限制：
        # 短消息（如"mysql历史"）也可能是复杂知识问题，不能因过短被 Fast Path 压短。
        # task 等级仍走下方 PLANNER_MIN_MSG_LEN 限制，避免"查一下"等含糊短命令误触发。
        try:
            from core.complexity import assess_complexity
            _cx = assess_complexity(text)
            if _cx.level == "knowledge":
                # 纯知识问题进 Agent：可搜索/多步展开，避免 Fast Path 压短
                logger.debug("Agent: 知识复杂度(knowledge)，进入规划: %r", text[:30])
                return True
        except Exception:
            pass
        # 过短消息 → Fast Path（chat 与含糊短任务）
        if len(text) < PLANNER_MIN_MSG_LEN:
            return False
        # 明确执行型任务 → 进 Agent
        try:
            from core.complexity import assess_complexity
            _cx = assess_complexity(text)
            if _cx.level == "task":
                logger.debug("Agent: 任务复杂度(task)，进入规划: %r", text[:30])
                return True
        except Exception:
            pass
        # 行为类触发词 → 需要规划
        if any(k in low for k in PLANNER_TRIGGER_KEYWORDS):
            # 裸"帮我"需含强任务动词，避免"帮我一下"误触发
            if "帮我" in low and not self._has_task_verb(text):
                return False
            return True
        # Phase 20 Part4：裸多字强任务动词 → 复杂任务（分析/部署/整理/完成/搜索…）
        if any(v in text for v in _TASK_INTENT_VERBS):
            return True
        # 复杂多步骤句式（先…再… / 并且… / 同时… / 以及…）也视为需规划
        import re
        if re.search(r"(先.{1,12}再|然后|并且|同时|顺便|分别|先.{1,12}而后)", text):
            return True
        return False

    @staticmethod
    def _is_casual(text: str) -> bool:
        """判断是否为纯社交/闲聊/确认内容。

        规则：去掉标点与空白后，若由若干"闲聊token"拼接而成即视为闲聊；
        或整段命中某个闲聊 token（如"好的谢谢"）。避免把带任务词的句子误判。
        """
        import re
        compact = re.sub(r"[\s，。！？、,.!?~～…\-—_]+", "", text).lower()
        # 整段命中长 token（如"好的谢谢"）→ 闲聊
        if any(tk in compact for tk in _CASUAL_TOKENS if len(tk) >= 4):
            return True
        # 逐 token 匹配：若 compact 能被若干闲聊 token 完整覆盖 → 闲聊
        if not compact:
            return True
        rest = compact
        matched_any = False
        while rest:
            hit = None
            # 优先匹配较长的 token，避免"嗯"把"嗯帮我写"错误吞掉
            for tk in sorted(_CASUAL_TOKENS, key=len, reverse=True):
                if rest.startswith(tk):
                    hit = tk
                    break
            if hit is None:
                break
            matched_any = True
            rest = rest[len(hit):]
        return matched_any and not rest

    @staticmethod
    def _has_task_verb(text: str) -> bool:
        """消息中是否含强任务动词。

        全文本搜索（而非'帮我'后固定窗口），因为动词可能出现在语言名之后，
        如"帮我用 python 写一个 2048"。闲聊"帮我一下"不含任何任务动词 → False。
        """
        return any(v in text for v in _TASK_VERBS)

    # ── 生成 Plan ──
    async def plan(self, task_request: str, enabled: bool = True,
                   constraints: Optional[TaskConstraints] = None) -> Optional[Plan]:
        """为复杂任务生成结构化 Plan。失败/超时/解析失败 → 返回 None（fallback）。

        enabled=False 时直接返回 None（Agent 全局关闭）。
        constraints：用户任务约束（"一次说完"/“继续”/详细 等），随 Plan 进入 Task Context。
        """
        if not enabled:
            return None
        if not task_request or not task_request.strip():
            return None

        from core.trace import span as _span
        with _span("planner"):
            plan = await self._do_plan(task_request)
            if plan is not None and constraints is not None:
                plan.constraints = constraints
            return plan

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