"""
Phase 20 Hotfix D2 验证测试（write_code 15s 硬超时取消）
运行: python _test_phase20_hotfix_d2.py
覆盖：
1. effective_tool_timeout：write_code 走工具默认 120s，不被 Agent 15s 覆盖
2. 其他工具（search_web/weather）仍保持自身 timeout
3. Agent executor 对 write_code 步骤构造的 ToolRequest.timeout=120（非 15）
4. execute_tool 直接调用 write_code 时 limit=120s（超时上限宽裕）
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_hfd2_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. effective_tool_timeout 解析 ─────────────────────────
def test_effective_timeout_resolution():
    from core.tools import effective_tool_timeout, TOOL_TIMEOUTS, DEFAULT_TOOL_TIMEOUT
    # write_code 优先工具默认（120s），不被 Agent 全局 15s 覆盖
    assert effective_tool_timeout("write_code", 15.0) == TOOL_TIMEOUTS["write_code"] == 120.0
    # 其他已配置工具保持自身默认
    assert effective_tool_timeout("search_web", 15.0) == TOOL_TIMEOUTS["search_web"] == 15.0
    assert effective_tool_timeout("weather", 15.0) == TOOL_TIMEOUTS["weather"] == 10.0
    # 未配置工具回落 Agent 兜底
    assert effective_tool_timeout("some_unknown_tool", 15.0) == 15.0
    assert effective_tool_timeout("some_unknown_tool", 20.0) == 20.0
    print("OK test_effective_timeout_resolution (write_code=120s, 其他不变)")


# ── 2. Agent executor 构造的 ToolRequest.timeout ───────────
async def test_agent_executor_request_timeout():
    import core.agent.executor as ex
    from core.tool_runtime import ToolRequest
    from core.trace import new_request

    captured = {}

    async def fake_execute(req: ToolRequest):
        captured["timeout"] = req.timeout
        captured["tool"] = req.tool_name
        from core.tool_runtime import ToolResult, OK
        return ToolResult(tool_name=req.tool_name, status=OK, content="已发送 x.py")

    orig_execute = None
    import core.tool_runtime.runtime as rt_mod
    rt = rt_mod.get_tool_runtime()
    orig_execute = rt.execute
    rt.execute = fake_execute  # type: ignore[method-assign]
    try:
        new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        from core.agent.executor import AgentExecutor, AgentContext
        from core.agent.planner import Plan, PlanStep
        plan = Plan(goal="写代码", steps=[PlanStep(index=0, action="写", tool="write_code",
                                                  params={"language": "python", "description": "LLM api 工具"})])
        ctx = AgentContext(user_id=2, group_id=0, chat_id=1,
                           sender_name="t", is_group=False, bot_qq=0,
                           original_msg="python写一个支持openai api格式的LLM api调用工具")
        # 直接调用内部工具执行，避免依赖完整 LLM
        ex_mod = ex
        budget = None
        from core.agent.budget import AgentBudget
        from core.agent.executor import _has_answer_marker  # noqa
        await ex_mod.AgentExecutor()._await_tool(
            plan.steps[0], ctx, [], AgentBudget(), None)
        assert captured.get("tool") == "write_code", captured
        assert captured.get("timeout") == 120.0, \
            f"write_code 的 ToolRequest.timeout 应为 120s，实际 {captured.get('timeout')}"
        print("OK test_agent_executor_request_timeout (Agent 传 write_code timeout=120s)")
    finally:
        rt.execute = orig_execute


# ── 3. execute_tool 直接调用 write_code 的 limit ───────────
async def test_execute_tool_write_code_limit():
    import core.tools as tools
    from core.trace import new_request

    new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
    # 不真正跑 LLM，只验证 timeout 解析路径（_tool_timeout 显式传入 effective 结果）
    limit = tools._tool_timeout("write_code", tools.effective_tool_timeout("write_code", 15.0))
    assert limit == 120.0, limit
    # 且默认（无显式）也走 120s
    assert tools._tool_timeout("write_code", None) == 120.0
    print("OK test_execute_tool_write_code_limit (execute_tool write_code limit=120s)")


# ── 4. 搜索等工具仍保留 timeout（不无差别删除）─────────────
def test_other_tools_timeout_kept():
    from core.tools import effective_tool_timeout, TOOL_TIMEOUTS
    for tool, t in TOOL_TIMEOUTS.items():
        got = effective_tool_timeout(tool, 15.0)
        assert got == t, (tool, got, t)
    print("OK test_other_tools_timeout_kept (全部工具 timeout 保持自身默认)")


async def main():
    test_effective_timeout_resolution()
    await test_agent_executor_request_timeout()
    await test_execute_tool_write_code_limit()
    test_other_tools_timeout_kept()
    print("\n=== ALL Phase20 HOTFIX D2 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
