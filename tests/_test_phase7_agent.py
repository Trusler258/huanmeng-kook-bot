"""
Phase 7 Agent 测试（Huanmeng 2.0）
覆盖：
- 简单聊天 Fast Path（不应规划）
- 复杂多步骤任务（规划生成结构化 Plan）
- Skill 选择/加载（metadata 级 select + 按需 load）
- Tool 执行 成功/失败/timeout/cancel（经 AgentExecutor）
- Search→总结
- Tool→二次规划（replan）
- 规划失败 fallback（Plan=None）
- 最大步骤限制 / 最大重规划限制
- 上下文预算（Plan 保持短小）
- TaskManager 长任务（轻量）
运行: python _test_phase7_agent.py
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_p7_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 注入假 services.llm，避免 openai 依赖导致导入失败 ──────
# planner/executor/evaluator 在函数内 `from services.llm import call_llm`，
# 预置 sys.modules 让该导入走缓存，测试再 patch 它的 call_llm。
_FAKE_LLM = types.ModuleType("services.llm")


async def _default_call_llm(*a, **kw):
    return ""


_FAKE_LLM.call_llm = _default_call_llm
sys.modules["services.llm"] = _FAKE_LLM
# 确保 services 包可导入（若 __init__ 未加载，先占位避免 ImportError）
if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

from core.trace import new_request
from core.agent.config import (
    MAX_PLAN_STEPS, MAX_REPLANNING, PLANNER_MIN_MSG_LEN,
)
from core.agent.planner import AgentPlanner, Plan, PlanStep
from core.agent.evaluator import ResultEvaluator, Evaluation
from core.agent.executor import AgentExecutor, AgentContext, AgentResult
from core.agent.skill_registry import get_skill_registry


# ── 1. Fast Path：简单聊天 / 简单 command 不规划 ────────────
def test_fast_path_no_plan():
    p = AgentPlanner()
    assert p.should_plan("晚上好呀", intent="chat") is False
    assert p.should_plan("你好，今天天气怎么样？", intent="chat") is False
    assert p.should_plan("查一下", intent="chat") is False  # 过短(<PLANNER_MIN_MSG_LEN)
    assert p.should_plan("帮我查一下量子计算是什么", intent="command") is False  # command
    print("OK test_fast_path_no_plan (简单聊天/command 保持 Fast Path)")


def test_complex_task_should_plan():
    p = AgentPlanner()
    assert p.should_plan("帮我查一下量子计算的原理然后总结给我", intent="search") is True
    assert p.should_plan("帮我分析一下这个数据再对比一下那个", intent="chat") is True
    assert p.should_plan("先查天气再查机票然后给我方案", intent="chat") is True
    print("OK test_complex_task_should_plan (复杂多步骤触发规划)")


# ── 2. 规划失败 fallback：Plan 返回 None ────────────────────
async def test_plan_failure_fallback():
    p = AgentPlanner()
    with patch.object(_FAKE_LLM, "call_llm", side_effect=Exception("LLM 挂了")):
        plan = await p.plan("帮我查一下这个技术方案")
    assert plan is None, "规划失败应返回 None（调用方 fallback 原 pipeline）"
    print("OK test_plan_failure_fallback (规划异常 → None → fallback)")


async def test_plan_empty_returns_none():
    p = AgentPlanner()
    assert await p.plan("") is None
    assert await p.plan("   ") is None
    assert await p.plan("帮我查一下", enabled=False) is None  # 全局关闭
    print("OK test_plan_empty_returns_none")


# ── 3. 规划生成结构化 Plan ────────────────────────────────
async def test_plan_builds_structured_plan():
    p = AgentPlanner()
    fake_json = ('{"goal":"调研量子计算现状","steps":['
                 '{"action":"搜索量子计算基础","tool":"search_web","params":{"query":"量子计算"}},'
                 '{"action":"整理结论","tool":"","skill":""}],'
                 '"required_skills":[],"required_tools":["search_web"]}')
    with patch.object(_FAKE_LLM, "call_llm", return_value=fake_json):
        plan = await p.plan("帮我查一下量子计算")
    assert plan is not None
    assert plan.goal
    assert len(plan.steps) >= 1
    assert plan.required_tools == ["search_web"]
    assert plan.status == "PLANNED"
    assert any(s.tool == "search_web" for s in plan.steps), plan.to_dict()
    d = plan.to_dict()
    assert "goal" in d and "steps" in d and "current_step" in d and "status" in d
    print("OK test_plan_builds_structured_plan (结构化 Plan, steps/tools/skills)")


# ── 4. Skill 选择/加载（metadata 级，不加载全部正文）────────
def test_skill_select_load():
    r = get_skill_registry()
    metas = r.metadata()
    assert isinstance(metas, list)
    # 至少发现 skills/ 下的 skill
    assert len(metas) >= 1, "应发现至少一个 Skill"
    names = [m["name"] for m in metas]
    # 用第一个 skill 名做候选选择
    q = names[0]
    cands = r.select(q, top_k=3)
    assert isinstance(cands, list)
    # 选中后按需加载全文
    text = r.load(names[0])
    assert isinstance(text, str) and len(text) > 0, "Skill 应能按名加载全文"
    print(f"OK test_skill_select_load (发现 {len(metas)} 个 Skill, select→load={len(text)}字)")


async def test_skill_registry_no_llm_for_select():
    # select 是纯规则，绝不触发 LLM 调用
    r = get_skill_registry()
    new_request(conversation_id=1, user_id=1)
    from core.trace import get_llm_call_count
    before = get_llm_call_count()
    r.select("帮我用python写个脚本", top_k=2)
    assert get_llm_call_count() == before, "skill select 不应调用 LLM"
    print("OK test_skill_registry_no_llm_for_select")


# ── 5. Tool 执行：成功 / 失败 / timeout / cancel ───────────
async def _run_executor(plan, tool_impl, evaluator=None, ctx=None):
    from core.agent.executor import AgentExecutor
    ex = AgentExecutor(evaluator=evaluator)
    c = ctx or AgentContext(user_id=1, group_id=0, chat_id=1,
                            sender_name="t", is_group=False, bot_kook=0,
                            original_msg="测试")
    with patch("core.tools.execute_tool", side_effect=tool_impl):
        return await ex.execute(plan, c)


async def test_executor_tool_success():
    new_request(conversation_id=1, user_id=1)
    plan = Plan(goal="查天气", steps=[
        PlanStep(index=0, action="查天气", tool="weather", params={})
    ])

    async def _ok(tool, args, **kw):
        return "今天晴天 25 度，结果汇总如下"

    res = await _run_executor(plan, _ok)
    assert res.status == "COMPLETED", res.status
    assert res.final_text, "应有最终回复"
    print("OK test_executor_tool_success (Tool 成功 → 总结回复)")


async def test_executor_tool_fail_then_replan():
    new_request(conversation_id=1, user_id=1)
    plan = Plan(goal="查两件事", steps=[
        PlanStep(index=0, action="第一步", tool="t1", params={}),
        PlanStep(index=1, action="第二步", tool="t2", params={}),
    ])

    async def _fail_first(tool, args, **kw):
        if tool == "t1":
            return "执行失败，无法获取"
        return "第二步完成，结果数据如下"

    # decide_replan 返回 True → 继续
    class _Eval(ResultEvaluator):
        async def decide_replan(self, *a, **kw):
            return True

    res = await _run_executor(plan, _fail_first, evaluator=_Eval())
    assert res.status == "COMPLETED", res.status
    assert res.final_text
    print("OK test_executor_tool_fail_then_replan (失败→重规划→继续)")


async def test_executor_tool_timeout():
    new_request(conversation_id=1, user_id=1)
    plan = Plan(goal="查天气", steps=[
        PlanStep(index=0, action="查天气", tool="weather", params={})
    ])

    async def _slow(tool, args, **kw):
        await asyncio.sleep(5)
        return "x"

    from core.agent import executor as exmod
    with patch("core.tools.execute_tool", side_effect=_slow), \
         patch.object(exmod, "TOTAL_TASK_TIMEOUT", 0.1):
        res = await AgentExecutor().execute(plan, AgentContext(
            user_id=1, group_id=0, chat_id=1, sender_name="t",
            is_group=False, bot_kook=0, original_msg="测试"))
    assert res.status == "TIMEOUT", res.status
    assert res.final_text, "超时也应给出兜底回复"
    print("OK test_executor_tool_timeout (总任务超时 → TIMEOUT + 兜底回复)")


async def test_executor_cancel_propagates():
    new_request(conversation_id=1, user_id=1)
    plan = Plan(goal="查天气", steps=[
        PlanStep(index=0, action="查天气", tool="weather", params={})
    ])

    async def _hanging(tool, args, **kw):
        await asyncio.sleep(10)
        return "x"

    ex = AgentExecutor()
    c = AgentContext(user_id=1, group_id=0, chat_id=1, sender_name="t",
                     is_group=False, bot_kook=0, original_msg="测试")
    with patch("core.tools.execute_tool", side_effect=_hanging):
        task = asyncio.create_task(ex.execute(plan, c))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
            raise AssertionError("应当 CancelledError")
        except asyncio.CancelledError:
            pass
    assert plan.status == "CANCELLED", plan.status
    print("OK test_executor_cancel_propagates (CancelledError 继续传播)")


# ── 6. 最大步骤限制 ───────────────────────────────────────
async def test_max_plan_steps_limit():
    new_request(conversation_id=1, user_id=1)
    # 造 8 步，但 MAX_PLAN_STEPS=5 → 只执行前 5 步
    steps = [PlanStep(index=i, action=f"步{i}", tool="t", params={}) for i in range(8)]
    plan = Plan(goal="多步任务", steps=steps)

    executed = []

    async def _tool(tool, args, **kw):
        executed.append(tool)
        return "继续"

    from core.agent import executor as exmod
    with patch("core.tools.execute_tool", side_effect=_tool), \
         patch.object(exmod, "MAX_PLAN_STEPS", 5):
        res = await AgentExecutor().execute(plan, AgentContext(
            user_id=1, group_id=0, chat_id=1, sender_name="t",
            is_group=False, bot_kook=0, original_msg="测试"))
    assert len(executed) <= 5, f"执行步骤不应超过 5，实际 {len(executed)}"
    print(f"OK test_max_plan_steps_limit (最大 {MAX_PLAN_STEPS} 步, 实际执行 {len(executed)})")


# ── 7. 最大重规划限制 ─────────────────────────────────────
async def test_max_replanning_limit():
    new_request(conversation_id=1, user_id=1)
    # 每步都失败且 decide_replan 恒 True → 最多重规划 MAX_REPLANNING 次
    steps = [PlanStep(index=i, action=f"步{i}", tool="t", params={}) for i in range(6)]
    plan = Plan(goal="一直失败的任务", steps=steps)

    async def _fail(tool, args, **kw):
        return "失败"

    replan_calls = {"n": 0}

    class _Eval(ResultEvaluator):
        async def decide_replan(self, *a, **kw):
            replan_calls["n"] += 1
            return True

    from core.agent import executor as exmod
    with patch("core.tools.execute_tool", side_effect=_fail), \
         patch.object(exmod, "MAX_REPLANNING", 2):
        await AgentExecutor(evaluator=_Eval()).execute(plan, AgentContext(
            user_id=1, group_id=0, chat_id=1, sender_name="t",
            is_group=False, bot_kook=0, original_msg="测试"))
    assert replan_calls["n"] <= 2, f"重规划次数应≤2，实际 {replan_calls['n']}"
    print(f"OK test_max_replanning_limit (最多重规划 {MAX_REPLANNING} 次, 实际 {replan_calls['n']})")


# ── 8. 上下文预算：Plan 保持短小（不塞完整历史/工具结果）────
def test_plan_stays_small():
    p = AgentPlanner()
    # 步骤被截断到 MAX_PLAN_STEPS，且每步 action 短小
    fake = {"goal": "g" * 10, "steps": [{"action": "a"}] * 20}
    plan = p._build_plan(fake, "帮我查一下")
    assert plan is not None
    assert len(plan.steps) <= MAX_PLAN_STEPS
    assert all(len(s.action) <= 200 for s in plan.steps)
    print(f"OK test_plan_stays_small (Plan 步骤≤{MAX_PLAN_STEPS}, 每步短小)")


# ── 9. 规划摘要/LLM 计数写入 trace ─────────────────────────
async def test_trace_plan_summary_and_llm_count():
    new_request(conversation_id=1, user_id=1)
    from core.trace import get_llm_call_count, trace_summary
    p = AgentPlanner()
    fake_json = ('{"goal":"总结","steps":[{"action":"搜索","tool":"search_web",'
                 '"params":{"query":"x"}}],"required_tools":["search_web"]}')
    with patch.object(_FAKE_LLM, "call_llm", return_value=fake_json):
        plan = await p.plan("帮我搜一下再总结")
    assert plan is not None
    assert get_llm_call_count() >= 1, "规划至少调用 1 次 LLM"
    summ = trace_summary()
    assert "plan" in summ and summ["plan"].get("planned") is True, summ
    print("OK test_trace_plan_summary_and_llm_count (规划摘要 + LLM 计数)")


# ── 10. TaskManager 长任务（轻量：确认 create/set_state/get）─
async def test_task_manager_long_task():
    from core.task_manager import TaskManager, TaskState
    tm = TaskManager()
    task = tm.create(kind="agent", goal="长任务测试", conversation_id=1, user_id=1)
    tm.set_state(task.task_id, TaskState.RUNNING)
    got = tm.get(task.task_id)
    assert got is not None and got.state == TaskState.RUNNING, got
    tm.set_state(task.task_id, TaskState.COMPLETED)
    assert tm.get(task.task_id).state == TaskState.COMPLETED
    print("OK test_task_manager_long_task (create/set_state/get)")


async def main():
    test_fast_path_no_plan()
    test_complex_task_should_plan()
    test_plan_stays_small()

    await test_plan_failure_fallback()
    await test_plan_empty_returns_none()
    await test_plan_builds_structured_plan()

    test_skill_select_load()
    await test_skill_registry_no_llm_for_select()

    await test_executor_tool_success()
    await test_executor_tool_fail_then_replan()
    await test_executor_tool_timeout()
    await test_executor_cancel_propagates()
    await test_max_plan_steps_limit()
    await test_max_replanning_limit()
    await test_trace_plan_summary_and_llm_count()

    await test_task_manager_long_task()

    print("\n=== ALL Phase7 AGENT TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())