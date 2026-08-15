"""
Phase 7 Agent Executor / Execution Engine（Huanmeng 2.0）

需求：
- 按照 Plan 执行 Skill/Tool，每一步记录 trace / task_step / tool_call；
- 执行结果返回 planner；成功继续下一步，失败允许有限重试或调整步骤；
- 达到最大步骤数立即停止，禁止无限 Agent Loop；
- 最大 plan steps / 最大重规划次数 / 单工具 timeout / 总任务 timeout 全部配置化；
- CancelledError 必须继续传播；任何异常最终转成结构化失败结果。

输出：AgentResult（含最终回复文本 + 状态 + 统计），供 pipeline 薄适配层发送。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from core.agent.config import (
    MAX_PLAN_STEPS,
    MAX_REPLANNING,
    TOOL_TIMEOUT,
    TOTAL_TASK_TIMEOUT,
)
from core.agent.budget import AgentBudget, LoopDetector
from core.agent.verifier import AgentVerifier
from core.agent.evaluator import ResultEvaluator
from core.agent.planner import Plan, PlanStep, TaskConstraints
from core.agent.skill_registry import get_skill_registry
from core.logger import get_logger
from core.trace import record, record_llm, set_plan_summary
from core.tool_runtime import OK

logger = get_logger("agent.executor")

# 结果中视为"已含答案/完成"的标记（供 Verifier 判定 goal_satisfied）
_DONE_MARKERS = ("完成", "已发送", "已生成", "如下", "结果", "资料", "数据", "总结",
                 "答案", "结论", "信息")


def _has_answer_marker(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _DONE_MARKERS)


@dataclass
class AgentContext:
    """执行 Agent 所需的请求上下文（由 pipeline 薄适配层填充）。"""
    user_id: int = 0
    group_id: int = 0
    sender_name: str = ""
    is_group: bool = True
    bot_qq: int = 0
    chat_id: int = 0
    original_msg: str = ""


@dataclass
class AgentResult:
    plan: Optional[Plan] = None
    final_text: str = ""
    sentences: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tool_calls: list = field(default_factory=list)
    status: str = "COMPLETED"   # COMPLETED / FAILED / CANCELLED / TIMEOUT
    error: str = ""


class AgentExecutor:
    """按 Plan 执行 Skill/Tool 的执行引擎（Phase 12：集成 AgentBudget + Verifier + LoopDetector）。"""

    def __init__(self, evaluator: Optional[ResultEvaluator] = None,
                 skill_registry=None, verifier: Optional[AgentVerifier] = None):
        self._eval = evaluator or ResultEvaluator()
        self._verifier = verifier or AgentVerifier()
        self._skills = skill_registry or get_skill_registry()

    async def execute(self, plan: Plan, ctx: AgentContext,
                      budget: Optional[AgentBudget] = None) -> AgentResult:
        """执行整个 Plan，返回 AgentResult。异常最终转成结构化失败/超时/取消。"""
        from core.trace import span as _span
        start = time.perf_counter()
        plan.status = "RUNNING"
        plan.current_step = 0

        # Phase 12：创建预算（复用默认或外部注入）与循环检测器
        budget = budget or AgentBudget(
            max_steps=MAX_PLAN_STEPS,
            deadline_ms=time.monotonic() * 1000.0 + TOTAL_TASK_TIMEOUT * 1000.0,
        )
        detector = LoopDetector()

        accumulated: list[str] = []
        replans = 0
        retry_left = 1  # 失败后仅允许 1 次整体重试

        try:
            async def _run():
                return await self._run_loop(plan, ctx, accumulated, start,
                                            replans, retry_left, budget, detector)
            result = await asyncio.wait_for(_run(), timeout=TOTAL_TASK_TIMEOUT)
            await self._finalize(plan, result, accumulated, start, ctx)
            return result
        except asyncio.TimeoutError:
            plan.status = "TIMEOUT"
            self._record_loop(plan, ctx)
            # Phase 20 Part13：超时提示统一走 persona.timeout_message
            try:
                from core.persona import timeout_message
                _timeout_fallback = timeout_message()
            except Exception:
                _timeout_fallback = "任务执行超时，先给出已获取的信息。"
            final = self._fallback_compose(plan, accumulated, _timeout_fallback)
            return AgentResult(plan=plan, final_text=final, sentences=[final],
                               status="TIMEOUT",
                               llm_calls=0,
                               tool_calls=self._tool_call_snapshot(plan))
        except asyncio.CancelledError:
            plan.status = "CANCELLED"
            self._record_loop(plan, ctx)
            raise  # 关键：CancelledError 必须继续传播
        except Exception as e:
            plan.status = "FAILED"
            logger.error("Agent 执行异常: %s", e)
            self._record_loop(plan, ctx)
            final = self._fallback_compose(plan, accumulated,
                                           f"任务执行出错：{e}")
            return AgentResult(plan=plan, final_text=final, sentences=[final],
                               status="FAILED", error=str(e),
                               llm_calls=0, tool_calls=self._tool_call_snapshot(plan))

    # ── 主循环 ──
    async def _run_loop(self, plan, ctx, accumulated, start, replans, retry_left,
                        budget: AgentBudget, detector: LoopDetector):
        from core.trace import span as _span
        for step in plan.steps[:budget.max_steps]:
            # 预算 / 截止时间检查：超时即停止
            if budget.hit_deadline():
                logger.info("Agent 达到 deadline，停止执行")
                break
            plan.current_step = step.index
            step.status = "RUNNING"
            with _span("plan_step"):
                step_ms = await self._await_tool(step, ctx, accumulated, budget, detector)

            # 记录执行耗时
            record("execution", step_ms)

            # 评估结果（Phase 12 用 Verifier 判定）
            vr = self._verifier.verify_step(
                step.result, step.index, len(plan.steps),
                has_answer_marker=_has_answer_marker(step.result),
            )

            step.status = "OK" if vr.verdict in ("ok", "done", "continue") else \
                          ("FAILED" if vr.verdict == "fail" else "OK")

            # 目标已满足 → 立即停止，不再调用后续 Tool
            if vr.goal_satisfied or vr.verdict == "done":
                logger.info("Agent 目标已满足，停止执行: %s", step.action)
                break

            if vr.verdict in ("ok", "continue"):
                # 无进展检测：结果为空且无新信息 → no_progress
                if not step.result:
                    if detector.on_progress(False):
                        logger.info("Agent 检测到 no_progress，停止: %s", step.action)
                        break
                else:
                    detector.on_progress(True)
                continue

            if vr.verdict == "fail":
                # 有限重试
                if retry_left > 0:
                    retry_left -= 1
                    logger.warning("Agent 步骤失败，重试一次: %s", step.action)
                    with _span("plan_step"):
                        step_ms = await self._await_tool(step, ctx, accumulated, budget, detector)
                    record("execution", step_ms)
                    vr = self._verifier.verify_step(
                        step.result, step.index, len(plan.steps),
                        has_answer_marker=_has_answer_marker(step.result),
                    )
                    if vr.verdict not in ("fail",):
                        step.status = "OK"
                        continue
                # 重试耗尽 → 询问是否重规划
                if vr.wants_replan and replans < MAX_REPLANNING:
                    replans += 1
                    plan.status = "REPLAN"
                    remaining = [s.action for s in plan.steps[step.index + 1:]]
                    should = await self._verifier.decide_replan(
                        plan.goal, step.action, remaining,
                        accumulated="\n".join(accumulated),
                    )
                    if should:
                        logger.info("Agent 重新规划继续: %s (replan#%d)",
                                    step.action, replans)
                        continue
                # 放弃 → 停止，给已有信息
                logger.info("Agent 放弃该步骤，转总结: %s", step.action)
                break

        plan.status = "COMPLETED"
        return AgentResult(plan=plan, status="COMPLETED",
                           llm_calls=0, tool_calls=self._tool_call_snapshot(plan))

    # 工具步必须在 async 上下文 await
    async def _exec_tool_async(self, step: PlanStep, ctx: AgentContext,
                               accumulated: list[str],
                               budget: AgentBudget,
                               detector: LoopDetector) -> None:
        # 预算检查：工具调用次数 / 搜索次数 / 截止时间
        if not budget.can_call_tool():
            step.result = "工具调用预算或截止时间已达上限，停止调用工具"
            return
        if step.tool == "search_web":
            if not budget.can_search():
                step.result = "搜索预算已达上限，停止搜索"
                budget.use_tool()
                return
            budget.use_search()
        # 重复动作检测：连续相同 tool+params → 停止
        if detector.on_tool_call(step.tool or "", step.params or {}):
            step.result = "检测到重复调用相同工具，已停止"
            budget.use_tool()
            return
        budget.use_tool()
        # Phase 8：Agent 不得直接执行工具，统一走 ToolRuntime（权限/超时/重试/预算/Trace）
        from core.tool_runtime import ToolRequest, get_tool_runtime
        from core.trace import get_trace_id
        req = ToolRequest(
            tool_name=step.tool or "",
            arguments=step.params or {},
            trace_id=get_trace_id(),
            user_id=ctx.user_id, group_id=ctx.group_id, chat_id=ctx.chat_id,
            sender_name=ctx.sender_name, is_group=ctx.is_group,
            bot_qq=ctx.bot_qq, original_msg=ctx.original_msg,
            timeout=TOOL_TIMEOUT,
        )
        try:
            res = await get_tool_runtime().execute(req)
            step.result = res.to_context()
            if res.status != OK:
                step.status = "FAILED"
        except asyncio.CancelledError:
            step.status = "CANCELLED"
            raise
        except Exception as e:
            step.result = f"工具执行出错: {e}"
        if step.result:
            accumulated.append(f"[工具:{step.tool}]\n{step.result}")

    # ── 供测试/同步调用的工具执行入口（实际走 async）──
    async def _await_tool(self, step: PlanStep, ctx: AgentContext,
                          accumulated: list[str],
                          budget: Optional[AgentBudget] = None,
                          detector: Optional[LoopDetector] = None) -> float:
        from core.trace import span as _span
        budget = budget or AgentBudget()
        detector = detector or LoopDetector()
        if step.tool:
            t0 = time.perf_counter()
            await self._exec_tool_async(step, ctx, accumulated, budget, detector)
            return (time.perf_counter() - t0) * 1000.0
        if step.skill:
            t0 = time.perf_counter()
            step.result = self._skills.load(step.skill)
            if step.result:
                accumulated.append(f"[Skill:{step.skill}]\n{step.result}")
            return (time.perf_counter() - t0) * 1000.0
        step.result = ""
        return 0.0

    # ── 工具调用统计快照 ──
    def _tool_call_snapshot(self, plan: Plan) -> list:
        from core.trace import get_tool_calls
        return get_tool_calls()

    def _record_loop(self, plan: Plan, ctx: AgentContext) -> None:
        set_plan_summary(planned=True, status=plan.status,
                         steps=len(plan.steps),
                         current_step=plan.current_step,
                         tools=[s.tool for s in plan.steps if s.tool],
                         skills=[s.skill for s in plan.steps if s.skill])

    async def try_continue_task(self, ctx: AgentContext,
                                additional: str = "") -> Optional[AgentResult]:
        """Phase 20 Part8：续说。用户说"继续/再详细/全部整理"时，基于上次已收集信息
        重新总结成更完整回复。无续说状态 → 返回 None。

        不做任何工具调用（信息已在上一次收集），仅一次 LLM 总结，避免重复 LLM/Tool。
        """
        state = get_continuation(int(ctx.chat_id or 0), int(ctx.user_id or 0))
        if not state:
            return None
        plan = Plan(
            goal=state.get("goal", ctx.original_msg or ""),
            steps=[PlanStep(action=a) for a in state.get("plan_steps", [])],
            constraints=state.get("constraints") or TaskConstraints(),
        )
        plan.constraints.completion_requirement = "COMPLETE_IN_ONE_RESPONSE"
        if state.get("constraints") and state["constraints"].is_continuation:
            plan.constraints = state["constraints"]
        accumulated = list(state.get("accumulated", []))
        final = await self._compose_final(plan, accumulated, ctx, fallback=None)
        if not final:
            return None
        return AgentResult(plan=plan, final_text=final, sentences=[final] if final else [],
                           status="COMPLETED", llm_calls=0,
                           tool_calls=self._tool_call_snapshot(plan))

    async def _finalize(self, plan: Plan, result: AgentResult, accumulated: list[str],
                        start: float, ctx: AgentContext) -> None:
        """生成最终回复文本（一次 LLM 总结调用）。"""
        final = await self._compose_final(plan, accumulated, ctx, fallback=None)
        result.final_text = final
        result.sentences = [final] if final else []
        plan.status = result.status
        self._record_loop(plan, ctx)
        # Phase 20 Part8：持久化续说状态，供"继续/再详细/全部整理"复用
        if accumulated or plan.steps:
            _CONTINUATION[(int(ctx.chat_id or 0), int(ctx.user_id or 0))] = {
                "goal": plan.goal,
                "accumulated": list(accumulated),
                "constraints": plan.constraints,
                "plan_steps": [s.action for s in plan.steps],
            }

    async def _compose_final(self, plan: Plan, accumulated: list[str], ctx: AgentContext,
                             fallback: Optional[str] = None) -> str:
        """用一次 LLM 调用把 goal + 已收集信息总结为回复。失败回退 fallback。

        Phase 20 Part8：尊重用户任务约束——
        - COMPLETE_IN_ONE_RESPONSE：明确要求一次性给全，禁止反问"先听哪块"。
        - detail_level=high：要求尽可能完整详细。
        - output_mode=list：要求分点/分步骤输出。
        """
        info = "\n\n".join(accumulated) if accumulated else "（无外部信息）"
        if not accumulated and fallback is not None:
            return fallback
        cons = getattr(plan, "constraints", None)
        try:
            from services.llm import call_llm
            from services.llm import _build_system_text
            from core.config import get_config
            cfg = get_config()
            # Phase 20 Part9：Agent 与普通聊天共用同一套系统提示（Persona/Tone/Format），
            # 避免"普通聊天有人设、Agent 没人设"。
            system_text = _build_system_text(cfg.bot_name, cfg.system_prompt, ctx.is_group)
            guide = ""
            if cons is not None:
                if cons.is_one_shot or cons.is_continuation:
                    guide += ("\n用户明确要求一次性/继续把内容说完。请直接给出完整内容，"
                              "不要反问用户想先看哪部分，不要只给大纲，把能给出的都一次性写完。")
                if cons.detail_level == "high":
                    guide += "\n用户要求详细。请尽量完整、具体、详细地展开所有要点。"
                if cons.output_mode == "list":
                    guide += "\n用户希望分点/分条/分步骤输出，请用清晰的编号列表组织。"
                if cons.user_constraints:
                    guide += "\n用户原话约束：" + "、".join(cons.user_constraints)
            prompt = (
                "请根据以下任务目标和已获取的信息，用你一贯的语气，给用户一个完整回答。\n"
                "直接陈述结果，不要提'我搜索了'这类过程词。"
                f"{guide}\n\n"
                f"任务目标：{plan.goal[:300]}\n\n"
                f"已获取信息：\n{info[:4000]}"
            )
            raw = await call_llm(
                cfg.reply_model,
                [{"role": "system", "content": system_text},
                 {"role": "user", "content": prompt}],
                max_tokens=1200 if (cons is not None and cons.is_one_shot) else 600,
                temperature=0.4, timeout=20.0,
            )
            record_llm()
            if raw and raw.strip():
                return raw.strip()[:3000]
        except Exception as e:
            logger.warning("Agent 最终总结 LLM 失败: %s", e)
        # 回退：给 goal + 原始信息片段
        if accumulated:
            return plan.goal[:200] + "\n\n" + accumulated[0][:800]
        return fallback or plan.goal[:300]

    def _fallback_compose(self, plan: Plan, accumulated: list[str],
                          fallback: str) -> str:
        """纯同步兜底：不调用 LLM。有已收集信息则拼一段，否则用 fallback。"""
        if accumulated:
            info = "\n\n".join(accumulated)[:1200]
            return f"{plan.goal[:200]}\n\n{info}\n\n{fallback}"
        return fallback


# ── 全局单例 ────────────────────────────────────────────────
# Phase 20 Part8：续说状态（chat_id,user_id）→ {"goal","accumulated","constraints","plan_steps"}
_CONTINUATION: dict = {}


def has_continuation(chat_id: int, user_id: int) -> bool:
    return (int(chat_id or 0), int(user_id or 0)) in _CONTINUATION


def get_continuation(chat_id: int, user_id: int) -> Optional[dict]:
    return _CONTINUATION.get((int(chat_id or 0), int(user_id or 0)))


def clear_continuation(chat_id: int, user_id: int) -> None:
    _CONTINUATION.pop((int(chat_id or 0), int(user_id or 0)), None)


_executor: Optional[AgentExecutor] = None


def get_executor() -> AgentExecutor:
    global _executor
    if _executor is None:
        _executor = AgentExecutor()
    return _executor