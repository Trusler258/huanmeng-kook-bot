"""
Phase 6 Part4：Context Builder + Prompt 预算

目标：明确区分 prompt 中的不同语义段（system / developer / current conversation /
memory / skills / tool results），并为 memory、skill、tool result 设置独立预算，
避免单个模块把整个上下文挤爆。

设计约束：
- 不改变已有 Skill 文件格式（skills/*.md，首行 `## 节名`）。
- Skill 本阶段先做 metadata/description 级发现：默认只加载「节名 + 首行说明」，
  只有被选中的 Skill 才加载完整正文。
- 对超大文本提供 budget 截断（保留头尾，中段省略），防止 context 无限膨胀。
- 纯函数 / 无副作用，可独立测试。
"""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# ── 独立预算（字符）────────────────────────────────────────
# 默认值：memory / skill / tool result 各自封顶，避免单一模块挤爆上下文。
# 这些是「注入到 LLM 前的字符预算」，可按部署环境调大/调小。
DEFAULT_BUDGETS: dict[str, int] = {
    "memory": 2000,        # 记忆注入上限（与 modules.memory.MEMORY_TOKEN_BUDGET 对齐）
    "skill": 1500,         # 单次选中的 Skill 正文上限
    "tool_result": 3000,   # 工具执行结果上限（含搜索）
    "conversation": 6000,  # 当前对话/历史注入上限
    "system": 4000,        # system 段上限
}


@dataclass
class ContextProfile:
    """一次请求的上下文预算配置。"""
    budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))

    def budget(self, section: str) -> int:
        return self.budgets.get(section, 0)


def truncate(text: str, budget: int) -> str:
    """按字符预算截断：保留头尾，中段省略。budget<=0 表示不截断。"""
    if not text or budget <= 0 or len(text) <= budget:
        return text
    marker = f"\n…[上下文已按预算截断，省略 {len(text) - budget} 字符]…\n"
    # 预留 marker 长度，保证结果总长不超过 budget
    avail = max(1, budget - len(marker))
    head = text[: int(avail * 0.6)]
    tail = text[-(avail - int(avail * 0.6)):]
    return f"{head}{marker}{tail}"


# ── Skill metadata / description 级发现 ────────────────────
_SKILL_META_CACHE: Optional[list[dict]] = None


def _skill_first_description(text: str) -> str:
    """提取 Skill 正文的非空首行作为 description（metadata 用）。"""
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:120]
    return ""


def discover_skills() -> list[dict]:
    """盘点 skills/ 目录下所有 Skill 的 metadata（name + description），不加载正文。

    返回形如 [{"name": "...", "description": "..."}]。
    仅在 reload（reload_skill_cache）时失效，避免每次请求重复读盘。
    """
    global _SKILL_META_CACHE
    if _SKILL_META_CACHE is not None:
        return _SKILL_META_CACHE

    meta: list[dict] = []
    if _SKILLS_DIR.is_dir():
        for sf in sorted(_SKILLS_DIR.glob("*.md")):
            try:
                text = sf.read_text(encoding="utf-8")
                m = re.match(r'^##\s+(\S+)', text)
                if not m:
                    continue
                meta.append({
                    "name": m.group(1).strip(),
                    "description": _skill_first_description(text),
                })
            except Exception:
                continue
    _SKILL_META_CACHE = meta
    return meta


def reload_skill_discovery() -> None:
    """清除 skill metadata 缓存（reload 时调用）。"""
    global _SKILL_META_CACHE
    _SKILL_META_CACHE = None


def load_skill(name: str, budget: Optional[int] = None) -> str:
    """按名称加载选中 Skill 的完整正文（仅被选中的才读取全文）。

    未命中返回 ""。默认按 DEFAULT_BUDGETS['skill'] 截断，budget<=0 不截断。
    """
    if not name:
        return ""
    if not _SKILLS_DIR.is_dir():
        return ""
    target = None
    for sf in sorted(_SKILLS_DIR.glob("*.md")):
        try:
            text = sf.read_text(encoding="utf-8")
            m = re.match(r'^##\s+(\S+)', text)
            if m and m.group(1).strip() == name:
                target = text
                break
        except Exception:
            continue
    if target is None:
        return ""
    limit = DEFAULT_BUDGETS["skill"] if budget is None else budget
    return truncate(target, limit)


# ── 上下文组装助手 ─────────────────────────────────────────
def assemble_sections(
    sections: dict[str, str],
    profile: Optional[ContextProfile] = None,
    order: Optional[list[str]] = None,
) -> str:
    """把多个语义段按顺序组装为一段上下文，每段套用各自预算。

    Args:
        sections: {section_name: content}
        profile: 预算配置（None 用默认）
        order: 输出顺序（None 用 sections 的插入顺序）
    """
    profile = profile or ContextProfile()
    parts: list[str] = []
    for name in (order or list(sections.keys())):
        content = sections.get(name, "")
        if not content:
            continue
        parts.append(truncate(content, profile.budget(name)))
    return "\n\n".join(p for p in parts if p)


# ═══════════════════════════════════════════════════════════
# Phase 9：Context Engine（Retrieve → Rank → Compress → Budget → Inject）
# ═══════════════════════════════════════════════════════════
#
# 动机：Skill / 记忆 / 搜索上下文过多时，若不按优先级与预算管理，会把 LLM 的
# 上下文窗口挤稀，导致核心人格与当前用户消息被淹没、回答质量下降。本引擎保证：
#   - 核心 System Policy 与用户当前消息优先级最高，不可直接丢弃；
#   - 相关 Memory / Tool / Skill 次之；
#   - 低相关历史与 Search 可被压缩或丢弃；
#   - 每种上下文都有明确 Token 预算，并记录实际消耗（system/history/memory/
#     skill/tool/search/task）。
#
# 所有上下文必须经过同一管线：Retrieve（收集）→ Rank（按优先级排序）
# → Compress（对超长段压缩）→ Budget（按预算截断/丢弃）→ Inject（注入组装）。
# 纯函数 / 无副作用，可独立测试。
# ═══════════════════════════════════════════════════════════

# 上下文类型（顺序即注入顺序）
CONTEXT_KINDS: tuple[str, ...] = (
    "system", "task", "memory", "tool", "skill", "conversation", "search",
)

# 优先级：越小越优先（0 最高）
# system=核心 System Policy（最高）；task=当前任务；memory/tool/skill 次之；
# conversation=对话历史；search=搜索结果（可压缩/丢弃）。
PRIORITY: dict[str, int] = {
    "system": 0,
    "task": 1,
    "memory": 2,
    "tool": 3,
    "skill": 4,
    "conversation": 5,
    "search": 6,
}

# 各类型 Token 预算（估算 token，非字符）。__total__ 为全局总预算。
DEFAULT_TOKEN_BUDGETS: dict[str, int] = {
    "system": 2200,        # 核心 System Policy（人格/格式等）
    "task": 500,           # 当前任务上下文
    "memory": 700,         # 相关记忆
    "tool": 900,           # 工具执行结果
    "skill": 600,          # 选中 Skill 正文
    "conversation": 1600,  # 对话历史
    "search": 500,         # 搜索结果（优先级最低，可丢弃）
    "__total__": 7000,     # 全局上限
}

# system 段中属于「Stable Context」的节（人格/格式/语气/好感度/玩模式等固定内容）
STABLE_SECTIONS: frozenset[str] = frozenset({
    "header", "format_rules", "face_lib", "private_tone",
    "anti_repeat", "fav_format", "fav_tiers", "play_mode",
})
# system 段中属于「Dynamic Capability Context」的节（指令/技能/工具等动态能力）
DYNAMIC_SECTIONS: frozenset[str] = frozenset({
    "command_tools", "cmd_list", "skills", "tools",
})


def estimate_tokens(text: str) -> int:
    """粗略估算文本 Token 数：中文≈1字/token，其它≈4字符/token。

    仅用于预算与统计（不参与精确计费），DeepSeek/OpenAI 实际用量以 usage 为准。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - cjk
    return cjk + other // 4 + 1


def truncate_by_tokens(text: str, token_limit: int) -> str:
    """按 Token 预算截断（保留头尾，中段省略）。token_limit<=0 返回空。

    中文为主的场景下 1 token≈1 字符，直接用字符预算近似截断。
    """
    if token_limit <= 0:
        return ""
    if not text or estimate_tokens(text) <= token_limit:
        return text
    return truncate(text, max(1, int(token_limit)))


@dataclass
class TokenStats:
    """各类上下文的 Token 消耗统计（system/history/memory/skill/tool/search/task）。"""
    system_tokens: int = 0
    history_tokens: int = 0
    memory_tokens: int = 0
    skill_tokens: int = 0
    tool_tokens: int = 0
    search_tokens: int = 0
    task_tokens: int = 0

    _KIND_ATTR: dict[str, str] = field(default_factory=lambda: {
        "system": "system_tokens",
        "conversation": "history_tokens",
        "memory": "memory_tokens",
        "skill": "skill_tokens",
        "tool": "tool_tokens",
        "search": "search_tokens",
        "task": "task_tokens",
    })

    def add(self, kind: str, tokens: int) -> None:
        attr = self._KIND_ATTR.get(kind)
        if attr:
            setattr(self, attr, getattr(self, attr) + tokens)

    def total(self) -> int:
        total = 0
        for attr in self._KIND_ATTR.values():
            total += getattr(self, attr)
        return total

    def to_dict(self) -> dict:
        return {
            "system_tokens": self.system_tokens,
            "history_tokens": self.history_tokens,
            "memory_tokens": self.memory_tokens,
            "skill_tokens": self.skill_tokens,
            "tool_tokens": self.tool_tokens,
            "search_tokens": self.search_tokens,
            "task_tokens": self.task_tokens,
            "total_tokens": self.total(),
        }


@dataclass
class ContextItem:
    """一条待注入的上下文单元。"""
    kind: str                    # CONTEXT_KINDS 之一
    content: str                 # 原始内容
    priority: int = 5            # 优先级（越小越优先）
    source: str = ""             # 来源标注（如 skill 名 / 记忆段 / 工具名）
    tokens: int = 0              # 估算 token（rank 后填充）
    droppable: bool = True       # 预算不足时是否可丢弃（system 不可丢）


@dataclass
class BuiltContext:
    """Context Engine 的产出：最终注入文本 + Token 统计 + 被丢弃项。"""
    text: str = ""
    stats: TokenStats = field(default_factory=TokenStats)
    dropped: list[str] = field(default_factory=list)


class ContextEngine:
    """统一上下文处理引擎：Retrieve(收集) → Rank → Compress → Budget → Inject。

    用法：
        engine = ContextEngine()
        built = engine.build([
            ContextItem(kind="memory", content="...", priority=2),
            ContextItem(kind="system", content="...", priority=0, droppable=False),
        ])
        llm_context = built.text
        log_stat(built.stats.to_dict())
    """

    def __init__(self,
                 token_budgets: Optional[dict[str, int]] = None,
                 char_budgets: Optional[dict[str, int]] = None,
                 with_headers: bool = True):
        self.token_budgets = dict(DEFAULT_TOKEN_BUDGETS if token_budgets is None else token_budgets)
        self.char_budgets = dict(DEFAULT_BUDGETS if char_budgets is None else char_budgets)
        self.with_headers = with_headers

    # ── Retrieve：收集（由调用方组装 items；此处仅兜底去空）──
    def retrieve(self, items: list[ContextItem]) -> list[ContextItem]:
        return [it for it in items if it.content]

    # ── Rank：按优先级排序（同优先级保留插入顺序，稳定排序）──
    def rank(self, items: list[ContextItem]) -> list[ContextItem]:
        return sorted(items, key=lambda it: it.priority)

    # ── Compress：对超长段落压缩（保留头尾，中段省略）──
    def compress(self, items: list[ContextItem]) -> list[ContextItem]:
        for it in items:
            char_budget = self.char_budgets.get(it.kind, 0)
            if char_budget and len(it.content) > char_budget:
                it.content = truncate(it.content, char_budget)
            it.tokens = estimate_tokens(it.content)
        return [it for it in items if it.content]

    # ── Budget：按分类型预算 + 全局预算截断/丢弃 ──
    def budget(self, items: list[ContextItem]) -> tuple[list[ContextItem], TokenStats, list[str]]:
        global_budget = self.token_budgets.get("__total__", 0)
        used_total = 0
        used_by_kind: dict[str, int] = {}
        kept: list[ContextItem] = []
        dropped: list[str] = []
        for it in items:
            it.tokens = estimate_tokens(it.content)
            if not it.content:
                continue
            kind_budget = self.token_budgets.get(it.kind, 0)
            cur = used_by_kind.get(it.kind, 0)
            over_kind = kind_budget and (cur + it.tokens > kind_budget)
            over_global = global_budget and (used_total + it.tokens > global_budget)
            if over_kind or over_global:
                if it.droppable:
                    dropped.append(f"{it.kind}:{it.source or it.content[:20]}")
                    continue
                # 不可丢弃（如 system）→ 截断到剩余预算，而不是丢弃
                limit: Optional[int] = None
                if kind_budget:
                    limit = kind_budget - cur
                if global_budget:
                    g_room = global_budget - used_total
                    limit = g_room if limit is None else min(limit, g_room)
                if limit is not None and limit > 0:
                    it.content = truncate_by_tokens(it.content, limit)
                    it.tokens = estimate_tokens(it.content)
                else:
                    dropped.append(f"{it.kind}:{it.source or it.content[:20]}(不可丢弃但预算为0)")
                    continue
            kept.append(it)
            used_total += it.tokens
            used_by_kind[it.kind] = used_by_kind.get(it.kind, 0) + it.tokens
        stats = TokenStats()
        for it in kept:
            stats.add(it.kind, it.tokens)
        return kept, stats, dropped

    # ── Inject：按类型分组组装为最终可注入文本 ──
    def inject(self, items: list[ContextItem]) -> str:
        by_kind: dict[str, list[ContextItem]] = {}
        for it in items:
            by_kind.setdefault(it.kind, []).append(it)
        parts: list[str] = []
        for kind in CONTEXT_KINDS:
            group = by_kind.get(kind)
            if not group:
                continue
            body = "\n\n".join(it.content for it in group if it.content)
            if not body:
                continue
            if self.with_headers:
                parts.append(f"# {kind}\n{body}")
            else:
                parts.append(body)
        return "\n\n".join(p for p in parts if p)

    # ── 完整管线 ──
    def build(self, items: list[ContextItem]) -> BuiltContext:
        retrieved = self.retrieve(items)
        ranked = self.rank(retrieved)
        compressed = self.compress(ranked)
        kept, stats, dropped = self.budget(compressed)
        text = self.inject(kept)
        return BuiltContext(text=text, stats=stats, dropped=dropped)


def build_context_from_parts(
    parts: dict[str, str],
    budgets: Optional[dict[str, int]] = None,
    with_headers: bool = True,
) -> BuiltContext:
    """把 {kind: content} 组装为带预算的上下文。kind∈CONTEXT_KINDS。

    这是 pipeline 最常用的入口：调用方只负责按类型收集内容，预算/优先级/丢
    弃全由引擎处理。system 段默认不可丢弃（最高优先级）。
    """
    engine = ContextEngine(budgets, with_headers=with_headers)
    items: list[ContextItem] = []
    for kind, content in parts.items():
        if not content:
            continue
        items.append(ContextItem(
            kind=kind,
            content=content,
            priority=PRIORITY.get(kind, 5),
            source=kind,
            droppable=(kind != "system"),
        ))
    return engine.build(items)


def classify_system_sections(sections: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """把 build_system_sections 产出的 system 各节划分为 Stable / Dynamic 两类。

    Returns:
        (stable, dynamic)：stable=人格/格式/语气/好感度/玩模式等固定内容；
        dynamic=指令/技能/工具等动态能力内容。
    """
    stable = {k: v for k, v in sections.items() if k in STABLE_SECTIONS and v}
    dynamic = {k: v for k, v in sections.items() if k in DYNAMIC_SECTIONS and v}
    return stable, dynamic