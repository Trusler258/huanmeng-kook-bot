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
from core.agent.evaluator import ResultEvaluator
from core.agent.planner import Plan, PlanStep
from core.agent.skill_registry import get_skill_registry
from core.logger import get_logger
from core.trace import record, record_llm, set_plan_summary
from core.tool_runtime import OK

logger = get_logger("agent.executor")


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
    """按 Plan 执行 Skill/Tool 的执行引擎。"""

    def __init__(self, evaluator: Optional[ResultEvaluator] = None,
                 skill_registry=None):
        self._eval = evaluator or ResultEvaluator()
        self._skills = skill_registry or get_skill_registry()

    async def execute(self, plan: Plan, ctx: AgentContext) -> AgentResult:
        """执行整个 Plan，返回 AgentResult。异常最终转成结构化失败/超时/取消。"""
        from core.trace import span as _span
        start = time.perf_counter()
        plan.status = "RUNNING"
        plan.current_step = 0

        accumulated: list[str] = []
        replans = 0
        retry_left = 1  # 失败后仅允许 1 次整体重试

        try:
            async def _run():
                return await self._run_loop(plan, ctx, accumulated, start,
                                            replans, retry_left)
            result = await asyncio.wait_for(_run(), timeout=TOTAL_TASK_TIMEOUT)
            await self._finalize(plan, result, accumulated, start, ctx)
            return result
        except asyncio.TimeoutError:
            plan.status = "TIMEOUT"
            self._record_loop(plan, ctx)
            final = self._fallback_compose(plan, accumulated,
                                           "任务执行超时，先给出已获取的信息喵~")
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
    async def _run_loop(self, plan, ctx, accumulated, start, replans, retry_left):
        from core.trace import span as _span
        for step in plan.steps[:MAX_PLAN_STEPS]:
            plan.current_step = step.index
            step.status = "RUNNING"
            with _span("plan_step"):
                step_ms = await self._await_tool(step, ctx, accumulated)

            # 记录执行耗时
            record("execution", step_ms)

            # 评估结果
            with _span("evaluation"):
                ev = self._eval.evaluate(
                    step.result, plan.goal, step.index, len(plan.steps),
                    accumulated="\n".join(accumulated),
                )

            step.status = "OK" if ev.verdict in ("ok", "done") else \
                          ("FAILED" if ev.verdict == "fail" else "OK")

            if ev.verdict in ("done", "ok", "continue"):
                continue
            if ev.verdict == "fail":
                # 有限重试
                if retry_left > 0:
                    retry_left -= 1
                    logger.warning("Agent 步骤失败，重试一次: %s", step.action)
                    with _span("plan_step"):
                        step_ms = await self._await_tool(step, ctx, accumulated)
                    record("execution", step_ms)
                    ev = self._eval.evaluate(step.result, plan.goal, step.index,
                                             len(plan.steps),
                                             accumulated="\n".join(accumulated))
                    if ev.verdict not in ("fail",):
                        step.status = "OK"
                        continue
                # 重试耗尽 → 询问是否重规划
                if ev.wants_replan and replans < MAX_REPLANNING:
                    replans += 1
                    plan.status = "REPLAN"
                    remaining = [s.action for s in plan.steps[step.index + 1:]]
                    should = await self._eval.decide_replan(
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
                               accumulated: list[str]) -> None:
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
                          accumulated: list[str]) -> float:
        from core.trace import span as _span
        if step.tool:
            t0 = time.perf_counter()
            await self._exec_tool_async(step, ctx, accumulated)
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

    async def _finalize(self, plan: Plan, result: AgentResult, accumulated: list[str],
                        start: float, ctx: AgentContext) -> None:
        """生成最终回复文本（一次 LLM 总结调用）。"""
        final = await self._compose_final(plan, accumulated, ctx, fallback=None)
        result.final_text = final
        result.sentences = [final] if final else []
        plan.status = result.status
        self._record_loop(plan, ctx)

    async def _compose_final(self, plan: Plan, accumulated: list[str], ctx: AgentContext,
                             fallback: Optional[str] = None) -> str:
        """用一次 LLM 调用把 goal + 已收集信息总结为回复。失败回退 fallback。"""
        info = "\n\n".join(accumulated) if accumulated else "（无外部信息）"
        if not accumulated and fallback is not None:
            return fallback
        try:
            from services.llm import call_llm
            from core.config import get_config
            prompt = (
                "请根据以下任务目标和已获取的信息，用自然、可爱的语气给用户一个完整回答。\n"
                "直接陈述结果，不要提'我搜索了'这类过程词。\n\n"
                f"任务目标：{plan.goal[:300]}\n\n"
                f"已获取信息：\n{info[:2500]}"
            )
            raw = await call_llm(
                get_config().reply_model,
                [{"role": "user", "content": prompt}],
                max_tokens=600, temperature=0.4, timeout=15.0,
            )
            record_llm()
            if raw and raw.strip():
                return raw.strip()[:1500]
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
_executor: Optional[AgentExecutor] = None


def get_executor() -> AgentExecutor:
    global _executor
    if _executor is None:
        _executor = AgentExecutor()
    return _executor