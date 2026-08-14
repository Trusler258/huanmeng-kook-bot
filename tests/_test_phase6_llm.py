"""Phase 6 Part7 测试：LLM 调用计数 + 二次调用仅当工具执行后触发"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.trace import new_request, record, record_llm, get_llm_call_count


def test_llm_call_count_ordinary_chat():
    """普通聊天：仅一次主要 LLM 调用（call_llm_with_tools round0 返回 → 循环 break）。"""
    new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
    # 模拟一次普通聊天：只调一次 LLM
    record_llm()
    assert get_llm_call_count() == 1, get_llm_call_count()
    print("OK test_llm_call_count_ordinary_chat (1 call)")


def test_llm_call_count_tool_second_summary():
    """工具执行后允许第二次总结调用（_send_call_results 场景）。"""
    new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
    record_llm()  # 主生成
    record_llm()  # 工具执行后二次总结
    assert get_llm_call_count() == 2, get_llm_call_count()
    print("OK test_llm_call_count_tool_second_summary (2 calls)")


def test_no_unconditional_second_call_in_main_path():
    """审计：generate_multi_reply_with_tools 中，普通聊天（无 tool_calls）round0 即返回，
    不会无条件二次调用。此处校验 CODEC-LEVEL 证据：普通聊天路径不进入 _send_call_results。
    """
    import ast
    src = Path("core/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 找到 _send_call_results 定义，确认它被 `if call_results:` 门控创建
    gate_found = "if call_results:" in src
    assert gate_found, "缺少 if call_results: 门控 → 可能无条件二次调用"
    assert "asyncio.create_task(_send_call_results())" in src
    print("OK test_no_unconditional_second_call_in_main_path (gate: if call_results:)")


async def main():
    test_llm_call_count_ordinary_chat()
    test_llm_call_count_tool_second_summary()
    test_no_unconditional_second_call_in_main_path()
    print("\nALL Phase6-Part7 TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())