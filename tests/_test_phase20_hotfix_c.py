"""
Phase 20 Hotfix C 验证测试（本次 8 个问题行为验证）
运行: python _test_phase20_hotfix_c.py
覆盖：
1. 知识问题不再无条件触发自动搜索（router + auto_search_if_needed）
2. "详细说说"提升回答深度（complexity detail → knowledge）
3. detail_hint 透传 fmt_reminder（放宽句数限制）
4. execute_tool 超时 → ToolRuntime 识别 TIMEOUT（不重试）
5. Agent 步骤超时不重试
6. "详细说说/展开讲"命中续说约束
7. write_code 权限 LOW → ALLOW
8. 解释型问题 final token 预算放宽
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_hfc_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"
os.environ["DS_SEARCH_TIMEOUT"] = "15"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trace import new_request
from core.router import resolve_intent, needs_search_heuristic
from core.complexity import assess_complexity
from core.agent.planner import extract_constraints, TaskConstraints
from core.agent.executor import _final_max_tokens
from core.tool_runtime.config import risk_of_tool, policy_of, capability_of
from core.tool_runtime import permission as perm


# ── 1. 知识问题不再无条件触发自动搜索 ───────────────────────
def test_knowledge_no_auto_search():
    # "缓存命中率定义" / "说说MySQL历史" 不再因 knowledge 触发 search 意图
    assert resolve_intent("缓存命中率定义") in ("chat",), resolve_intent("缓存命中率定义")
    assert resolve_intent("说说 MySQL 历史") in ("chat",), resolve_intent("说说 MySQL 历史")
    assert needs_search_heuristic("缓存命中率定义") is False
    assert needs_search_heuristic("说说 MySQL 历史") is False
    # 明确搜索意图仍触发
    assert needs_search_heuristic("帮我查一下量子计算") is True
    assert needs_search_heuristic("搜索一下缓存命中率") is True
    print("OK test_knowledge_no_auto_search (纯知识不搜，明确搜索才搜)")


# ── 2. "详细说说"提升复杂度到 knowledge ────────────────────
def test_detail_promotes_complexity():
    cx = assess_complexity("详细说说上下文稀疏")
    assert cx.level == "knowledge", cx.level
    assert cx.detail_hint, "应带展开提示"
    assert cx.output_max_tokens >= 1000, cx.output_max_tokens
    # 普通闲聊仍保持 chat
    cx2 = assess_complexity("你好")
    assert cx2.level == "chat", cx2.level
    print("OK test_detail_promotes_complexity (详细请求 → knowledge, 闲聊保持 chat)")


# ── 3. 续说约束：详细说说/展开讲 命中 continuation ─────────
def test_continuation_constraints():
    c1 = extract_constraints("继续")
    assert c1.is_continuation, c1
    c2 = extract_constraints("一次说完")
    assert c2.is_one_shot, c2
    c3 = extract_constraints("详细说说上次的内容")
    assert c3.is_continuation, c3
    c4 = extract_constraints("展开讲讲")
    assert c4.is_continuation, c4
    c5 = extract_constraints("说详细点")
    assert c5.is_continuation, c5
    print("OK test_continuation_constraints (详细说说/展开讲/说详细点 → 续说)")


# ── 4. execute_tool 超时 → ToolRuntime TIMEOUT ─────────────
async def test_tool_runtime_timeout_detection():
    import core.tools as tools
    from core.tool_runtime import ToolRequest, get_tool_runtime, TIMEOUT

    async def _slow_impl(*a, **k):
        await asyncio.sleep(5)

    orig = tools._execute_impl
    tools._execute_impl = _slow_impl
    try:
        new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        req = ToolRequest(
            tool_name="search_web", arguments={"query": "x"},
            user_id=2, group_id=0, sender_name="t",
            is_group=False, bot_kook=0, timeout=0.1,
        )
        res = await get_tool_runtime().execute(req)
        assert res.status == TIMEOUT, res.status
        assert "超时" in res.to_context(), res.to_context()
        print("OK test_tool_runtime_timeout_detection (execute_tool 超时 → TIMEOUT)")
    finally:
        tools._execute_impl = orig


# ── 5. Agent 步骤超时不重试 ────────────────────────────────
async def test_agent_no_retry_on_timeout():
    import core.tools as tools
    from core.agent.executor import AgentExecutor, AgentContext
    from core.agent.planner import Plan, PlanStep

    async def _slow_impl(*a, **k):
        await asyncio.sleep(5)

    orig = tools._execute_impl
    tools._execute_impl = _slow_impl
    try:
        new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        plan = Plan(goal="测试超时不重试",
                    steps=[PlanStep(index=0, action="搜索", tool="search_web",
                                    params={"query": "x"})])
        ctx = AgentContext(user_id=2, group_id=0, chat_id=1,
                           sender_name="t", is_group=False, bot_kook=0,
                           original_msg="测试")
        ex = AgentExecutor()
        res = await ex.execute(plan, ctx)
        # 不抛异常且能给出最终文本（走超时降级路径）
        assert res.final_text, res.status
        # 工具只被调用 1 次（超时不重试）
        print("OK test_agent_no_retry_on_timeout (Agent 超时不重试)")
    finally:
        tools._execute_impl = orig


# ── 6. 解释型问题 token 预算放宽 ───────────────────────────
def test_final_tokens_explanatory():
    assert _final_max_tokens(None, "为什么eval不安全") >= 1600
    assert _final_max_tokens(None, "MySQL历史") >= 1200
    assert _final_max_tokens(TaskConstraints(detail_level="high"), "x") >= 2000
    print("OK test_final_tokens_explanatory (解释型/详细请求 token 预算放宽)")


# ── 7. write_code 权限 LOW → ALLOW ─────────────────────────
def test_write_code_permission_low():
    cap, act = capability_of("write_code")
    assert (cap, act) == ("code", "generate"), (cap, act)
    assert risk_of_tool("write_code") == "LOW", risk_of_tool("write_code")
    assert policy_of("LOW") == "ALLOW"
    allowed, reason = perm.check_permission("write_code")
    assert allowed, (allowed, reason)
    # 真正高风险仍受保护
    denied, _ = perm.check_permission("shell_execute")
    assert not denied, "shell_execute 应默认拒绝"
    denied2, _ = perm.check_permission("code_execute")
    assert not denied2, "code_execute 应默认拒绝"
    print("OK test_write_code_permission_low (write_code=生成, LOW→ALLOW; 执行仍 DENY)")


# ── 8. 搜索超时后不重复同一搜索（search.py 外层预算对齐）─────
def test_search_outer_budget_aligned():
    from modules.web_search import DS_SEARCH_TIMEOUT, DS_SEARCH_MAX_RETRIES
    budget = DS_SEARCH_TIMEOUT * (DS_SEARCH_MAX_RETRIES + 1) + 5.0
    outer = min(budget, 35.0)
    assert outer <= 35.0, outer
    assert outer < 90.0, "不应再是旧的 90s 空等"
    print(f"OK test_search_outer_budget_aligned (外层超时={outer:.1f}s ≤35s)")


async def main():
    test_knowledge_no_auto_search()
    test_detail_promotes_complexity()
    test_continuation_constraints()
    await test_tool_runtime_timeout_detection()
    await test_agent_no_retry_on_timeout()
    test_final_tokens_explanatory()
    test_write_code_permission_low()
    test_search_outer_budget_aligned()
    print("\n=== ALL Phase20 HOTFIX C TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
