"""Phase 6 Part1 自测：Trace 增强（tool_call / llm_call_count / slow 分类）"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trace import (
    new_request, current, record, span, record_llm, record_tool_call,
    get_llm_call_count, get_tool_calls,
)
from core.logger import log_trace_summary


async def main():
    req = new_request(conversation_id=123, user_id=456, channel_id="c1", message_id="m1")
    assert req is current()

    # 阶段计时
    with span("judge"):
        await asyncio.sleep(0.01)
    with span("memory"):
        record("memory", 5.0)
    with span("llm"):
        record_llm()
        record_llm()
    record("queue_wait", 2.5)

    # 工具调用记录
    record_tool_call("weather", 120.0, "OK")
    record_tool_call("search_web", 8000.0, "TIMEOUT")
    record_tool_call("calc", 30.0, "FAILED")

    assert get_llm_call_count() == 2, get_llm_call_count()
    tools = get_tool_calls()
    assert len(tools) == 3, tools
    assert tools[1]["status"] == "TIMEOUT", tools
    assert tools[0]["tool_name"] == "weather", tools

    # 慢请求分类测试
    req.start_monotonic -= 4.0  # 模拟 total>=4000ms → slow_request
    assert req.severity() == "slow_request", req.severity()

    phases = req.phases()
    assert "llm" in phases and "judge" in phases and "memory" in phases

    # 输出汇总日志（应含 severity / llm_calls / tools）
    log_trace_summary()
    print("PASS: trace part1")

    # 再次模拟 very_slow
    req.start_monotonic -= 10.0
    assert req.severity() == "very_slow_request", req.severity()
    print("PASS: severity very_slow_request")


asyncio.run(main())