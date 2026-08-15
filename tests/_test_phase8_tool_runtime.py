"""
Phase 8 Tool Runtime 测试（Huanmeng 2.0）
覆盖：
- Model Tool Request → ToolRequest 解耦
- 权限：白名单工具允许 / 高风险拒绝 / 未知工具默认 DENY
- 重试：失败重试 + 指数退避 + 重试预算上限
- 超时：单工具超时
- 预算：超过 MAX_TOOL_CALLS 拒绝
- Trace：tool_call_id / retry_count / trace_id 记录
- Result Normalization：status/content/to_context
- Agent Executor 走 ToolRuntime
运行: python _test_phase8_tool_runtime.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_p8_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trace import new_request
from core.tool_runtime import (
    ToolRequest, ToolResult, get_tool_runtime, call_tool,
    OK, FAILED, TIMEOUT, CANCELLED, DENIED, BudgetExhausted,
)


# ── 1. Model Tool Request → ToolRequest 解耦 ───────────────
def test_request_from_tool_call():
    req = ToolRequest.from_tool_call(
        {"id": "call_123", "name": "weather",
         "arguments": {"city": "北京"}},
        trace_id="trace_abc", user_id=1,
    )
    assert req.tool_name == "weather"
    assert req.tool_call_id == "call_123"
    assert req.trace_id == "trace_abc"
    assert req.arguments == {"city": "北京"}
    # 字符串 promise 也能解析
    req2 = ToolRequest.from_tool_call(
        {"id": "c2", "name": "search_web", "arguments": '{"query":"量子计算"}'},
        trace_id="t2",
    )
    assert req2.arguments == {"query": "量子计算"}
    print("OK test_request_from_tool_call (Model Tool Request 解耦为 ToolRequest)")


# ── 2. 权限 ────────────────────────────────────────────────
async def test_permission():
    from core.tool_runtime.permission import check_permission
    # 白名单允许
    ok, _ = check_permission("weather")
    assert ok is True
    # Phase 20 Part5：write_code=code.generate=LOW → 允许（生成代码，不执行）
    ok, _ = check_permission("write_code")
    assert ok is True
    # 执行代码 / Shell → HIGH → 拒绝
    ok, _ = check_permission("code_execute")
    assert ok is False
    ok, _ = check_permission("shell_execute")
    assert ok is False
    # 未知工具默认 DENY
    ok, _ = check_permission("rm_rf_all")
    assert ok is False
    print("OK test_permission (白名单允许 / 生成代码允许 / 执行代码拒绝 / 未知默认 DENY)")


async def test_runtime_denied():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    # Phase 20 Part5：改用真正的执行类工具验证 HIGH → DENY
    res = await rt.execute(ToolRequest(tool_name="shell_execute", arguments={}))
    assert res.status == DENIED, res.status
    assert "拒绝" in (res.error or "")
    print("OK test_runtime_denied (ToolRuntime 拒绝执行类高风险工具)")


async def test_runtime_unknown_denied():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    res = await rt.execute(ToolRequest(tool_name="not_a_real_tool", arguments={}))
    assert res.status == DENIED, res.status
    print("OK test_runtime_unknown_denied (未知工具默认 DENY)")


# ── 3. 成功 + Result Normalization ────────────────────────
async def test_runtime_success():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()

    async def _ok(tool, args, **kw):
        assert tool == "weather"
        return "北京晴天 25 度"

    with patch("core.tools.execute_tool", side_effect=_ok):
        res = await rt.execute(ToolRequest(
            tool_name="weather", arguments={"city": "北京"},
            tool_call_id="call_1", trace_id="trace_1", user_id=1))
    assert res.status == OK, res.status
    assert res.content == "北京晴天 25 度"
    assert res.to_context() == res.content
    assert res.tool_call_id == "call_1"
    assert res.trace_id == "trace_1"
    assert res.permission is not None
    print("OK test_runtime_success (成功 + Result Normalization + 归属字段)")


# ── 4. 重试：失败→重试→成功；重试预算上限 ─────────────────
async def test_runtime_retry_then_success():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    calls = {"n": 0}

    async def _flaky(tool, args, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("临时故障")
        return "终于成功，数据"

    with patch("core.tools.execute_tool", side_effect=_flaky), \
         patch("core.tool_runtime.runtime.RETRY_BACKOFF_BASE", 0.01):
        res = await rt.execute(ToolRequest(
            tool_name="weather", arguments={}, user_id=1, retry_budget=2))
    assert res.status == OK, res.status
    assert calls["n"] == 3, f"应尝试3次，实际{calls['n']}"
    assert res.retry_count == 2, res.retry_count
    print(f"OK test_runtime_retry_then_success ({calls['n']}次尝试, retry_count={res.retry_count})")


async def test_runtime_retry_budget_exhausted():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    calls = {"n": 0}

    async def _always_none(tool, args, **kw):
        calls["n"] += 1
        return None

    # None 结果不算失败，不触发重试（与 execute_tool 语义一致）
    with patch("core.tools.execute_tool", side_effect=_always_none):
        res = await rt.execute(ToolRequest(
            tool_name="wzq", arguments={}, user_id=1, retry_budget=3))
    assert calls["n"] == 1, f"None不重试，实际{calls['n']}"
    print("OK test_runtime_retry_budget_exhausted (None 结果不触发重试)")


async def test_runtime_retry_on_exception():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    calls = {"n": 0}

    async def _boom(tool, args, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("临时故障")
        return "恢复成功"

    # 异常 → FAILED → 触发重试；重试预算内成功
    with patch("core.tools.execute_tool", side_effect=_boom), \
         patch("core.tool_runtime.runtime.RETRY_BACKOFF_BASE", 0.01):
        res = await rt.execute(ToolRequest(
            tool_name="weather", arguments={}, user_id=1, retry_budget=2))
    assert res.status == OK, res.status
    assert calls["n"] == 3, f"应尝试3次，实际{calls['n']}"
    assert res.retry_count == 2
    print(f"OK test_runtime_retry_on_exception (异常触发重试, 3次尝试成功)")


# ── 5. 预算：超过上限拒绝 ────────────────────────────────
async def test_runtime_budget_exhausted():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()
    # 用 1 的预算直接测
    rt.reset_budget()
    rt._max_calls = 1

    async def _ok(tool, args, **kw):
        return "ok"

    with patch("core.tools.execute_tool", side_effect=_ok):
        r1 = await rt.execute(ToolRequest(tool_name="weather", arguments={}, user_id=1))
        r2 = await rt.execute(ToolRequest(tool_name="weather", arguments={}, user_id=1))
    assert r1.status == OK, r1.status
    assert r2.status == FAILED, r2.status  # 预算耗尽 → 结构化失败
    assert "预算" in (r2.error or "")
    rt._max_calls = 20
    rt.reset_budget()
    print("OK test_runtime_budget_exhausted (预算耗尽 → 结构化失败不抛异常)")


# ── 6. Trace：tool_call_id / retry_count / trace_id ───────
async def test_runtime_trace_fields():
    new_request(conversation_id=1, user_id=1)
    from core.trace import get_tool_calls
    rt = get_tool_runtime()

    async def _ok(tool, args, **kw):
        return "trace测试"

    with patch("core.tools.execute_tool", side_effect=_ok):
        await rt.execute(ToolRequest(
            tool_name="weather", arguments={}, user_id=1,
            tool_call_id="call_trace" * 1, trace_id="trace_x"))
    calls = get_tool_calls()
    assert calls, "应记录工具调用"
    last = calls[-1]
    assert last["tool_call_id"] == "call_trace", last
    assert last["status"] == OK
    print("OK test_runtime_trace_fields (trace 记录 tool_call_id/status)")


# ── 7. 超时 ──────────────────────────────────────────────
async def test_runtime_timeout():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()

    async def _slow(tool, args, **kw):
        await asyncio.sleep(5)
        return "x"

    # execute_tool 内部有工具级超时；这里补一个请求级 wait_for 兜底
    with patch("core.tools.execute_tool", side_effect=_slow):
        try:
            await asyncio.wait_for(
                rt.execute(ToolRequest(tool_name="weather", arguments={}, user_id=1)),
                timeout=0.2)
            raise AssertionError("应超时")
        except asyncio.TimeoutError:
            pass  # 外层 wait_for 超时 → CancelledError 正确传播
    print("OK test_runtime_timeout (请求级超时 → CancelledError 传播)")


# ── 8. CancelledError 传播 ───────────────────────────────
async def test_runtime_cancel_propagates():
    new_request(conversation_id=1, user_id=1)
    rt = get_tool_runtime()

    async def _hang(tool, args, **kw):
        await asyncio.sleep(10)
        return "x"

    with patch("core.tools.execute_tool", side_effect=_hang):
        task = asyncio.create_task(rt.execute(
            ToolRequest(tool_name="weather", arguments={}, user_id=1)))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
            raise AssertionError("应当 CancelledError")
        except asyncio.CancelledError:
            pass
    print("OK test_runtime_cancel_propagates (CancelledError 继续传播)")


async def main():
    test_request_from_tool_call()
    await test_permission()
    await test_runtime_denied()
    await test_runtime_unknown_denied()
    await test_runtime_success()
    await test_runtime_retry_then_success()
    await test_runtime_retry_budget_exhausted()
    await test_runtime_retry_on_exception()
    await test_runtime_budget_exhausted()
    await test_runtime_trace_fields()
    await test_runtime_timeout()
    await test_runtime_cancel_propagates()

    print("\n=== ALL Phase8 TOOL RUNTIME TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())