"""
更新分级（severity）— commit message 前缀代号识别

用 GitHub commit message 开头的英文方括号前缀标记更新等级，供
更新检测 / 通知 / 更新策略分级使用：

    [FEAT]   功能新增         → 普通级，不自动更新，用户 .update 才更新
    [BUGFIX] 功能漏洞修复     → 普通级，不自动更新，用户 .update 才更新
    [CORE]   核心漏洞修复     → 普通级，不自动更新，用户 .update 才更新
    [P0]     P0级核心漏洞修复 → 必须更新但非强制，检测到发 card+button 提醒

分级决定更新策略：
- 普通级（FEAT/BUGFIX/CORE）：不主动发卡片通知，仅记录检查状态；
  用户手动 .update 时才走安全更新流水线。
- P0 级：检测到立即发"有 P0 更新"卡片 + 按钮，点击先 .update check
  查看 diff，二次确认后才应用。

约定：commit message 首行为前缀，如 `[P0] 修复登录鉴权漏洞`。
大小写不敏感；带或不带完整括号均可识别（如 `[feature]` / `[P0]`）。
无前缀的 commit 一律视为普通 FEAT 级（不打断自动更新策略）。
"""
from __future__ import annotations

import re

# 等级代号（英文前缀，用于 commit message 识别）
FEAT = "FEAT"
BUGFIX = "BUGFIX"
CORE = "CORE"
P0 = "P0"

# 中文名（用于展示）
_NAMES = {
    FEAT: "功能新增",
    BUGFIX: "功能漏洞修复",
    CORE: "核心漏洞修复",
    P0: "P0级核心漏洞修复",
}

# 各等级对应的中文关键词（兼容无前缀但含关键词的 commit）
_KEYWORDS = {
    FEAT: ("新增", "功能", "feature", "feat", "加入", "支持"),
    BUGFIX: ("修复", "漏洞", "bugfix", "bug", "fix", "修正"),
    CORE: ("核心", "core", "运行时", "runtime", "架构"),
    P0: ("p0", "紧急", "严重", "critical", "高危", "安全", "security"),
}

# 等级优先级（用于多 commit 取最高级）
_PRIORITY = {FEAT: 1, BUGFIX: 2, CORE: 3, P0: 4}

# P0 命中即最高级，无需再判断
_RE_HEADER = re.compile(r"\[([^\]]+)\]")


def name(level: str) -> str:
    """返回等级的中文名"""
    return _NAMES.get(level, "功能新增")


def is_p0(level: str) -> bool:
    return level == P0


def classify_message(message: str) -> str:
    """
    依据单条 commit message 判断等级。
    优先匹配方括号前缀（[FEAT]/[BUGFIX]/[CORE]/[P0]），
    无前缀时用关键词兜底。
    """
    text = (message or "").strip()
    if not text:
        return FEAT
    head = text.split("\n")[0]

    # 1) 方括号前缀
    m = _RE_HEADER.match(head)
    if m:
        tag = m.group(1).strip().upper()
        if tag in _NAMES:
            return tag
        # 兼容连写如 [P0更新] / [CORE-FIX]
        for level in _NAMES:
            if tag.startswith(level):
                return level

    # 2) 关键词兜底（从高优先级名称开始匹配）
    low = text.lower()
    for level in (P0, CORE, BUGFIX, FEAT):
        for kw in _KEYWORDS[level]:
            if kw in low:
                return level
    return FEAT


def classify_commits(messages: list[str]) -> str:
    """
    依据一组 commit message 判断本次更新的最高等级。
    返回 FEAT / BUGFIX / CORE / P0。
    """
    if not messages:
        return FEAT
    top = FEAT
    for m in messages:
        level = classify_message(m)
        if _PRIORITY[level] > _PRIORITY[top]:
            top = level
    return top


def describe(messages: list[str]) -> str:
    """生成面向用户的等级说明文本"""
    top = classify_commits(messages)
    return f"本次更新等级：**{name(top)}** (`[{top}]`)"