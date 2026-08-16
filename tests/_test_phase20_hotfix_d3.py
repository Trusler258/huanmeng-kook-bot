"""
Phase 20 Hotfix D3 验证测试（最终回复转义泄漏修复）
运行: python _test_phase20_hotfix_d3.py
覆盖验收：
① 普通多段回答：字面 \\n 必须变成真实换行
② Markdown：**标题**\\n\\n- 条目 正常换行
③ Python 代码块：```python ... ``` 完整保留
④ Python 字符串里的 \\n 等合法内容不能被错误转换（代码块内）
⑤ JSON 正常解析和 fallback 都不能退化
⑥ write_code 成功后文字说明正常（无字面转义）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.kmd import normalize_kmd_text
from services.llm import _parse_reply, normalize_final_reply


# ── ① 普通多段回答：字面 \n → 真实换行 ─────────────────────
def test_literal_newline_restored():
    text = "**核心功能**\\n- 支持 chat/completions\\n- 支持流式"
    out = normalize_kmd_text(text)
    assert "\n" in out, repr(out)
    assert "\\n" not in out, repr(out)  # 不再有字面 \n
    assert out.startswith("**核心功能**\n- "), repr(out)
    print("OK test_literal_newline_restored (\\n → 真实换行)")


# ── ② Markdown 多段 ────────────────────────────────────────
def test_markdown_paragraphs():
    text = "**标题**\\n\\n- 条目一\\n- 条目二"
    out = normalize_kmd_text(text)
    assert "**标题**\n\n- 条目一\n- 条目二" == out, repr(out)
    assert "\\n" not in out
    print("OK test_markdown_paragraphs (Markdown 结构正常换行)")


# ── ③ Python 代码块完整保留 ────────────────────────────────
def test_code_fence_preserved():
    text = ("**使用示例**\\n\\n"
            "```python\\n"
            "import openai\\n"
            "client = openai.OpenAI()\\n"
            "```")
    out = normalize_kmd_text(text)
    assert "```python" in out, repr(out)
    assert "import openai" in out, repr(out)
    assert "```" in out
    # 代码块围栏不再被转义
    assert "\\`\\`\\`" not in out, repr(out)
    print("OK test_code_fence_preserved (```python 完整保留)")


def test_escaped_backtick_fence_restored():
    # LLM 输出字面 \\`\\`\\`python ... \\`\\`\\` → 还原为真实 ``` 围栏
    text = "**示例**\\n\\n\\`\\`\\`python\\nprint(1)\\n\\`\\`\\`"
    out = normalize_kmd_text(text)
    assert "```python" in out, repr(out)
    assert "print(1)" in out, repr(out)
    assert "```" in out
    assert "\\`" not in out, repr(out)  # 不再有字面 \\`
    print("OK test_escaped_backtick_fence_restored (\\`\\`\\`python → ```python)")


# ── ④ 代码块内合法 \n 不被错误转换 ─────────────────────────
def test_code_string_escapes_kept():
    text = ("```python\\n"
            "s = \"a\\\\nb\"   # 字符串里的字面 \\\\n\\n"
            "print(s)\\n"
            "```")
    out = normalize_kmd_text(text)
    # 代码块内：\\n（Python 源码里的转义）必须保留为 \\n
    assert "\\n" in out, repr(out)  # 代码内字面 \n 两字符仍在
    # 但代码块围栏外无字面 \n 泄漏
    head, _, tail = out.partition("```python")
    assert "\\n" not in head, repr(head)
    assert "```" in tail
    print("OK test_code_string_escapes_kept (代码块内 \\\\n 保留)")


# ── ⑤ JSON 正常解析 / fallback 不退化 ──────────────────────
def test_json_parse_and_fallback():
    # 正常 JSON（模型正确转义 \n）→ 真实换行，无二次破坏
    raw_ok = '{"replies": ["第一行\\n第二行"], "fav": 0, "calls": [], "face": null, "mood": "开心", "action": "", "at": null, "mode": null, "origin": "user", "actor": {}}'
    replies, *_ = _parse_reply(raw_ok)
    assert replies and "\n" in replies[0], repr(replies)
    assert "\\n" not in replies[0], repr(replies)
    print("OK test_json_parse_ok (正常 JSON 解析不退化)")

    # 纯文本（非 JSON）→ normalize_final_reply 原样放行
    assert normalize_final_reply("纯文本回复喵~") == "纯文本回复喵~"
    # 非法 JSON → 返回 None（触发 persona fallback，不泄漏原始 JSON）
    assert normalize_final_reply('{"replies": [') is None
    print("OK test_json_fallback (纯文本放行, 非法 JSON 走 fallback)")


# ── ⑥ write_code 成功后的说明文字 ──────────────────────────
def test_write_code_follow_text():
    # Agent _compose_final 的最终文本（LLM 输出带字面转义）→ 还原
    text = ("文件 llm_api.py 已经写好并发送给你了喵~\\n\\n"
            "**核心功能**\\n- 支持 OpenAI API 格式\\n- 支持流式输出")
    out = normalize_kmd_text(text)
    assert "llm_api.py" in out
    assert "**核心功能**\n- 支持 OpenAI API 格式" in out, repr(out)
    assert "\\n" not in out
    print("OK test_write_code_follow_text (文件已发送+说明文字正常)")


# ── 附带：经 normalize_final_reply → kmd 全链路 ─────────────
def test_full_chain():
    # Agent 路径：LLM 返回带字面转义的 JSON → normalize → kmd
    raw = '{"replies": ["**文件已发送**\\\\n\\\\n- 内容1\\\\n- 内容2"], "fav": 0, "calls": [], "face": null, "mood": "开心", "action": "", "at": null, "mode": null, "origin": "user", "actor": {}}'
    normalized = normalize_final_reply(raw)
    assert normalized, normalized
    out = normalize_kmd_text(normalized)
    assert "**文件已发送**\n\n- 内容1\n- 内容2" == out, repr(out)
    print("OK test_full_chain (normalize_final_reply → kmd 全链路还原)")


async def main():
    test_literal_newline_restored()
    test_markdown_paragraphs()
    test_code_fence_preserved()
    test_escaped_backtick_fence_restored()
    test_code_string_escapes_kept()
    test_json_parse_and_fallback()
    test_write_code_follow_text()
    test_full_chain()
    print("\n=== ALL Phase20 HOTFIX D3 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
