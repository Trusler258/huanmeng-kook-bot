"""知识类回复的结构化分段工具（无外部依赖，可独立测试）。

解决知识/讲解类回答被 LLM 一次性返回、缺乏分节的问题：
把含多个"阶段小标题"的长句按阶段拆成多条独立消息，
每条作为独立消息发送，让每一代/每个阶段清晰分开。
"""
from __future__ import annotations

import re


# 结构化回复的"阶段小标题"行首标记：**加粗** / 第一代·第二阶段·第三期 / 一、二、 / 1. / 起源（1995-2000）
# 只匹配"行首"（标记可带正文，如"第一代（1945-1955）真空管时代：..."），不要求整行是标题。
_SECTION_HEADER_RE = re.compile(
    r'^\s*(?:'
    r'\*\*[^*]{1,30}\*\*'
    r'|第[一二三四五六七八九十]+[代阶段期部分]'
    r'|(?:[一二三四五六七八九十]+|[0-9]{1,2})[、.．·]\s*'
    r'|[\u4e00-\u9fa5A-Za-z]{1,12}（[0-9]{2,4}[-~—]?[0-9]{0,4}）'
    r')'
)
# 围栏代码块起始/结束（``` 或 ```python 等）
_FENCE_RE = re.compile(r'^\s*```')


def _in_code_fence(lines: list[str], idx: int) -> bool:
    """判断第 idx 行是否处于 ``` 围栏代码块内部（含边界行）。"""
    fence = 0
    for i in range(0, min(idx, len(lines)) + 1):
        if _FENCE_RE.match(lines[i]):
            fence += 1
    # 奇数个 ``` 前缀 → 当前处于代码块内
    return fence % 2 == 1


def split_knowledge_sentences(text: str) -> list[str]:
    """把含多个阶段小标题的长句拆成多条（每条作为独立消息发送）。

    仅当文本里出现 >=2 个阶段标记时才拆分，普通短回复不受影响。
    这样即使 LLM 把整个知识回答塞进一条，也能按每一代/每个阶段分条发出。

    Phase 20 Hotfix D：**绝不拆分代码块**。``` 围栏内的行（无论长得像
    **加粗** / 1. / 第X代）一律不作为阶段标题切分，保证代码结构完整。
    """
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    heads = [
        i for i, ln in enumerate(lines)
        if ln.strip() and _SECTION_HEADER_RE.search(ln) and not _in_code_fence(lines, i)
    ]
    if len(heads) < 2:
        return [text]

    # 逐行累积：命中阶段小标题时把前面累积的段落作为一条独立消息 flush 出去。
    # 这样开头的引导语（如"喵~...分为四个阶段"）也会单独成段，不会丢失。
    segs = []
    cur = []
    for i, ln in enumerate(lines):
        if ln.strip() and _SECTION_HEADER_RE.search(ln) and cur \
                and not _in_code_fence(lines, i):
            segs.append("\n".join(cur).strip())
            cur = []
        cur.append(ln)
    tail = "\n".join(cur).strip()
    if tail:
        segs.append(tail)
    return [s for s in segs if s]