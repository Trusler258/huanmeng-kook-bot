"""
KOOK KMarkdown 轻量归一化（发送前，不依赖 LLM）

背景：KOOK 的 KMD 语法与标准 Markdown 有差异——`#` 标题、`##` 等
在 KOOK 客户端不渲染（会原样显示为井号），KMD 只支持 **加粗**、
`-` 列表、`1.` 列表、` ``` ` 围栏代码块、`[链接](url)`、(met)@(met) 等。

本工具只做**结构性**归一化，不改写任何内容措辞（人格/语气保持原样）：
- 识别 ``` 围栏代码块，块内原样保留（绝不破坏代码）；
- 块外行首的 `#`/`##`/`###` 标题 → 转成 KMD 可渲染的 **加粗标题**；
- 其余行原样透传。

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


def normalize_kmd_text(text: str) -> str:
    """把待发送文本中的 Markdown 结构归一化为 KOOK KMD 可渲染形式。

    - ``` 围栏代码块：块内所有行原样透传（含 # 等，不破坏代码）。
    - 块外行首 # 标题：转为 **标题**。
    - 其余：原样。
    """
    if not text:
        return text
    lines = text.replace("\r\n", "\n").split("\n")
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
