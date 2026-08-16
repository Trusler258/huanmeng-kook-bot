"""
Phase 20 Hotfix D4 验证测试（KMD 字面转义还原，不二次转义）
运行: python _test_phase20_hotfix_d4.py
验收：
① \\n → 真实换行
② 加粗 ** 保持不变
③ ``code``（行内代码）保持不变
④ ```python ... ``` 保持完整
⑤ 普通 ~、" 不产生多余反斜杠
⑥ KMD 转义字符只有 LLM 明确生成的才保留（\\~ / \\` 等误转义还原，正常字符不加反斜杠）
⑦ JSON 正常解析 / fallback 不退化
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.kmd import normalize_kmd_text
from services.llm import _parse_reply, normalize_final_reply, _restore_literal_escapes


# ── ① \n → 真实换行 ────────────────────────────────────────
def test_newline_restored():
    out = normalize_kmd_text("第一行\\n第二行")
    assert "第一行\n第二行" == out, repr(out)
    assert "\\n" not in out
    print("OK test_newline_restored (\\n → 真实换行)")


# ── ② 加粗保持不变 ─────────────────────────────────────────
def test_bold_kept():
    out = normalize_kmd_text("**核心功能**")
    assert out == "**核心功能**", repr(out)
    assert "\\*" not in out, "不应给 * 加反斜杠"
    print("OK test_bold_kept (** 加粗保持不变)")


# ── ③ 行内代码保持不变 ─────────────────────────────────────
def test_inline_code_kept():
    out = normalize_kmd_text("用 `code` 表示")
    assert "`code`" in out, repr(out)
    assert "\\`" not in out, "不应给 ` 加反斜杠"
    print("OK test_inline_code_kept (`code` 行内代码保持不变)")


# ── ④ 代码块完整 ───────────────────────────────────────────
def test_code_block_kept():
    text = ("```python\\n"
            "import openai\\n"
            "client = openai.OpenAI()\\n"
            "```")
    out = normalize_kmd_text(text)
    assert "```python" in out, repr(out)
    assert "import openai" in out
    assert "```" in out
    print("OK test_code_block_kept (```python ... ``` 完整)")


# ── ⑤ 普通 ~、" 不产生多余反斜杠 ───────────────────────────
def test_plain_tilde_quote():
    out = normalize_kmd_text("波浪线 ~ 和引号 \" 正常")
    assert "~" in out and '"' in out, repr(out)
    assert "\\~" not in out and '\\"' not in out, repr(out)
    print("OK test_plain_tilde_quote (普通 ~ \" 无多余反斜杠)")


# ── ⑥ 误转义还原：\\~ / \\` / \\" → 真实字符 ──────────────
def test_mistaken_escapes_restored():
    out = normalize_kmd_text("a\\~b\\`c\\\"d")
    assert "a~b`c\"d" == out, repr(out)
    assert "\\~" not in out and "\\`" not in out and '\\"' not in out, repr(out)
    print("OK test_mistaken_escapes_restored (\\~\\`\\\" → ~`\")")


# ── ⑦ JSON 正常解析 / fallback 不退化 ──────────────────────
def test_json_parse_and_fallback():
    # 正常 JSON（\\n 是合法转义）→ 真实换行
    raw_ok = '{"replies": ["第一行\\n第二行"], "fav": 0, "calls": [], "face": null, "mood": "开心", "action": "", "at": null, "mode": null, "origin": "user", "actor": {}}'
    replies, *_ = _parse_reply(raw_ok)
    assert replies and "\n" in replies[0], repr(replies)
    print("OK test_json_parse_ok (正常 JSON \\n → 换行)")

    # 非法转义 JSON（\\~）→ fallback 还原，不泄漏
    raw_bad = '{"replies": ["第一行\\~第二行\\`代码\\`"]}'
    replies2, *_ = _parse_reply(raw_bad, quiet=True)
    joined = "".join(replies2)
    assert "\\~" not in joined and "\\`" not in joined, repr(replies2)
    assert "~" in joined and "`" in joined, repr(replies2)
    print("OK test_json_fallback_unescape (非法转义 fallback 还原)")

    # 纯文本放行 / 非法 JSON 走 persona fallback
    assert normalize_final_reply("纯文本喵~") == "纯文本喵~"
    assert normalize_final_reply('{"replies": [') is None
    print("OK test_fallback_kept (纯文本放行, 非法 JSON 走 fallback)")


# ── 附带：_restore_literal_escapes 纯函数 ──────────────────
def test_restore_literal_escapes_fn():
    assert _restore_literal_escapes("a\\nb") == "a\nb"
    assert _restore_literal_escapes("a\\r\\nb") == "a\nb"
    assert _restore_literal_escapes("a\\tb") == "a\tb"
    assert _restore_literal_escapes("a\\`b") == "a`b"
    assert _restore_literal_escapes("a\\~b") == "a~b"
    assert _restore_literal_escapes('a\\"b') == 'a"b'
    assert _restore_literal_escapes("") == ""
    print("OK test_restore_literal_escapes_fn (纯函数全序列还原)")


async def main():
    test_newline_restored()
    test_bold_kept()
    test_inline_code_kept()
    test_code_block_kept()
    test_plain_tilde_quote()
    test_mistaken_escapes_restored()
    test_json_parse_and_fallback()
    test_restore_literal_escapes_fn()
    print("\n=== ALL Phase20 HOTFIX D4 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
