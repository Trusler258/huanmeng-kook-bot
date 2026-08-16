"""
KOOK KMarkdown 轻量归一化（发送前，不依赖 LLM）

背景：KOOK 的 KMD 语法与标准 Markdown 有差异——`#` 标题、`##` 等
在 KOOK 客户端不渲染（会原样显示为井号），KMD 只支持 **加粗**、
`-` 列表、`1.` 列表、` ``` ` 围栏代码块、`[链接](url)`、(met)@(met) 等。

本工具只做**结构性**归一化，不改写任何内容措辞（人格/语气保持原样）：
- 识别 ``` 围栏代码块，块内原样保留（绝不破坏代码）；
- 块外行首的 `#`/`##`/`###` 标题 → 转成 KMD 可渲染的 **加粗标题**；
- 还原 LLM 输出的字面转义序列（\\n → 换行、\\` → `），代码块内不还原。

调用位置：pipeline / gateway 在真正发送（send_sentences / send_by_chat_type）
之前对每条待发文本调用，确保"用户要求 KMD/结构化排版时内容真正以 KMD
语法发出"，而不是让 LLM 只口头承诺"我会用 KMD"。
"""
from __future__ import annotations

import re

# 行首 Markdown 标题（# / ## / ### ... 后跟至少一个空格）
_HEADING_RE = re.compile(r'^\s*#{1,6}\s+(.+?)\s*$')
# 围栏代码块起始/结束（可带语言标记，如 ```python）
_FENCE_RE = re.compile(r'^\s*```')

# 字面转义序列（LLM 在 JSON 字符串里输出 \\n / \\r\\n / \\t / \\` / \\~ / \\" 等，
# json.loads 只还原合法 JSON 转义：\\n→换行；而 \\~ / \\` 是**非法 JSON 转义**，
# loads 会失败走 fallback，字面两字符原样保留。此处统一在块外还原）。仅块外还原。
_LITERAL_NL = "\\n"
_LITERAL_CRLF = "\\r\\n"
_LITERAL_TAB = "\\t"
_LITERAL_BACKTICK = "\\`"
_LITERAL_TILDE = "\\~"
_LITERAL_QUOTE = "\\\""


def _unescape_literal_outside_fence(lines: list[str]) -> list[str]:
    """把代码块外的字面转义序列还原为真实字符。

    - \\r\\n / \\n → 真实换行
    - \\t → 真实制表符
    - \\` → 反引号（如 `\\`\\`\\`python` → ```python）
    - \\~ → 波浪线（LLM 输出 \\~ 时多为误转义，还原为普通 ~）
    - \\" → 双引号（LLM 输出 \\" 时还原为普通 "）
    代码块围栏内部**不还原**，保护 Python 字符串 / 正则 / 转义序列中的合法反斜杠。
    正常 KMD 字符（** 加粗、` 反引号、~ 波浪线）不带反斜杠，原样保留、不加转义。
    """
    out: list[str] = []
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)  # 代码块内原样
            continue
        # 块外：先还原 CRLF（避免 \\r 残留），再还原 \\n
        ln = ln.replace(_LITERAL_CRLF, "\n").replace(_LITERAL_NL, "\n")
        ln = ln.replace(_LITERAL_TAB, "\t")
        ln = ln.replace(_LITERAL_BACKTICK, "`")
        ln = ln.replace(_LITERAL_TILDE, "~")
        ln = ln.replace(_LITERAL_QUOTE, '"')
        out.append(ln)
    return out


def normalize_kmd_text(text: str) -> str:
    """把待发送文本中的 Markdown 结构归一化为 KOOK KMD 可渲染形式。

    - 先把整段按行拆开，识别 ``` 围栏代码块；
    - ``` 围栏代码块：块内所有行原样透传（含 # 与转义序列，不破坏代码）；
    - 块外行首 # 标题：转为 **标题**；
    - 块外字面转义序列（\\n/\\r\\n/\\t/\\`）还原为真实字符；
    - 其余：原样。
    """
    if not text:
        return text
    lines = text.replace("\r\n", "\n").split("\n")
    # 第一步：还原块外字面转义（可能把 `\\`\\`\\`python` 还原出围栏行）
    lines = _unescape_literal_outside_fence(lines)
    # 第二步：KMD 结构归一化（# 标题 → 加粗）
    out: list[str] = []
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)  # 代码块内原样
            continue
        m = _HEADING_RE.match(ln)
        if m:
            out.append("**" + m.group(1).strip() + "**")
            continue
        out.append(ln)
    return "\n".join(out)
