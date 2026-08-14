"""
Phase 6 Part5/Part6 测试：
- Part5: execute_tool 单工具超时（TIMEOUT 状态）、CancelledError 传播、_tool_timeout 解析
- Part6: DS 原生搜索超时/重试常量、阶段 trace 记录
运行: python _test_phase6_tool_search.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trace import new_request, get_tool_calls


# ── Part5: _tool_timeout 解析 ──────────────────────────────
def test_tool_timeout_resolution():
    from core.tools import _tool_timeout, DEFAULT_TOOL_TIMEOUT, TOOL_TIMEOUTS
    # 显式参数优先
    assert _tool_timeout("search_web", 5.0) == 5.0
    # 工具默认
    assert _tool_timeout("search_web", None) == TOOL_TIMEOUTS["search_web"]
    # 未知工具 → 全局默认
    assert _tool_timeout("unknown_tool", None) == DEFAULT_TOOL_TIMEOUT
    print("OK test_tool_timeout_resolution")


# ── Part5: execute_tool 超时记录 TIMEOUT ───────────────────
async def test_execute_tool_timeout():
    import core.tools as tools

    async def _slow_impl(*a, **k):
        await asyncio.sleep(5)

    orig = tools._execute_impl
    tools._execute_impl = _slow_impl
    try:
        req = new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        result = await tools.execute_tool(
            "search_web", {"query": "x"}, user_id=2, group_id=0,
            sender_name="t", is_group=False, bot_qq=0, timeout=0.1,
        )
        assert "超时" in result, result
        calls = get_tool_calls()
        assert calls and calls[-1]["status"] == "TIMEOUT", calls
        assert calls[-1]["tool_name"] == "search_web", calls
        print("OK test_execute_tool_timeout", result)
    finally:
        tools._execute_impl = orig


# ── Part5: execute_tool CancelledError 正确传播 ────────────
async def test_execute_tool_cancelled():
    import core.tools as tools

    async def _hanging_impl(*a, **k):
        await asyncio.sleep(10)

    orig = tools._execute_impl
    tools._execute_impl = _hanging_impl
    try:
        task = asyncio.create_task(tools.execute_tool(
            "weather", {}, user_id=2, group_id=0, sender_name="t",
            is_group=False, bot_qq=0, timeout=30,
        ))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
            raise AssertionError("应当抛出 CancelledError")
        except asyncio.CancelledError:
            pass
        calls = get_tool_calls()
        assert calls and calls[-1]["status"] == "CANCELLED", calls
        print("OK test_execute_tool_cancelled (CancelledError 传播, status=CANCELLED)")
    finally:
        tools._execute_impl = orig


# ── Part6: DS 搜索常量 ─────────────────────────────────────
def test_ds_search_constants():
    from modules.web_search import DS_SEARCH_TIMEOUT, DS_SEARCH_MAX_RETRIES
    assert 0 < DS_SEARCH_TIMEOUT <= 30, DS_SEARCH_TIMEOUT
    assert DS_SEARCH_MAX_RETRIES >= 0, DS_SEARCH_MAX_RETRIES
    print(f"OK test_ds_search_constants timeout={DS_SEARCH_TIMEOUT}s retries={DS_SEARCH_MAX_RETRIES}")


async def main():
    test_tool_timeout_resolution()
    await test_execute_tool_timeout()
    await test_execute_tool_cancelled()
    test_ds_search_constants()
    print("\n=== Phase6 Part5/Part6 全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())