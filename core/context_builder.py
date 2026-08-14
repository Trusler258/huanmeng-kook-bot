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