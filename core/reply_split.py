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


def _chunks_by_paragraph(text: str, max_len: int = 3000, max_items: int = 10) -> list[str]:
    """按空行切段 + 超长段落按行切分（代码块围栏内一律不切），供普通聊天分句兜底。

    与 core.agent.gateway 的分句逻辑保持一致，让 Fast Path 与 Agent 路径分句行为统一：
    优先依赖结构化标题拆分；标题不足（闲聊/承接上文）时，落到这里按自然段落分条发送，
    避免一条长回复粘成一整条消息。
    """
    text = (text or "").strip()
    if not text:
        return []
    paras: list[str] = []
    buf = ""
    in_fence = False
    for ln in text.split("\n"):
        stripped = ln.strip()
        is_fence = stripped.startswith("```")
        if is_fence:
            in_fence = not in_fence
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if in_fence:
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if stripped == "":
            if buf:
                paras.append(buf)
                buf = ""
            continue
        buf = (buf + "\n" + ln) if buf else ln
    if buf:
        paras.append(buf)

    # 超长段落按行切分（不破坏代码围栏）
    out: list[str] = []
    for p in paras:
        if len(p) <= max_len:
            out.append(p)
            continue
        seg = ""
        f2 = False
        for ln in p.split("\n"):
            s = ln.strip()
            if s.startswith("```"):
                f2 = not f2
                seg = (seg + "\n" + ln) if seg else ln
                continue
            if f2:
                seg = (seg + "\n" + ln) if seg else ln
                continue
            if len(seg) + len(ln) + 1 > max_len:
                if seg:
                    out.append(seg)
                seg = ln
            else:
                seg = (seg + "\n" + ln) if seg else ln
        if seg:
            out.append(seg)
    return out[:max_items] or [text[:max_len]]


def split_reply_for_send(text: str, max_len: int = 3000, max_items: int = 10) -> list[str]:
    """通用回复分句：结构化标题优先，标不足时按空行/长度兜底拆分。

    优先调用 split_knowledge_sentences（如含 >=2 个**小标题**/第X代/编号则严格按阶段分条）；
    若结构化标题不足（普通聊天/承接上文），则按自然段落 + 长度拆成多条，让长回复也能分句发送。
    """
    structured = split_knowledge_sentences(text)
    if len(structured) > 1:
        return structured
    return _chunks_by_paragraph(text, max_len=max_len, max_items=max_items)