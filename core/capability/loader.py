"""
Phase 10 Capability System：CapabilityLoader（Huanmeng 2.0）

对 CapabilityRouter 选中的能力做「确认后再加载」：只加载命中的完整内容。
- load_fc_schemas(cap_ids)  → 只返回命中的 Tool Schema（不再全量塞给模型）
- build_command_usage(caps) → 命中的指令用法精简列表
- load_skill_text(name)      → 命中的 Skill 正文（走现有 skill_registry）
- resolve(query, intent)    → 统一编排：目录 + FC Schema + Skill 正文
"""
from __future__ import annotations

from typing import Optional

from core.capability.metadata import (
    Capability,
    CATEGORY_TOOL, CATEGORY_COMMAND, CATEGORY_SKILL,
)
from core.capability.router import get_capability_router


def load_fc_schemas(caps: list[Capability]) -> list[dict]:
    """返回选中 tool 能力的 OpenAI Schema 列表（仅命中项）。"""
    want = {c.id for c in caps if c.category == CATEGORY_TOOL}
    if not want:
        return []
    try:
        from core.tools import TOOLS
    except Exception:
        return []
    out = []
    for t in TOOLS:
        fn = (t or {}).get("function", {})
        if fn.get("name") in want:
            out.append(t)
    return out


def build_command_usage(caps: list[Capability]) -> str:
    """生成命中指令能力的精简用法列表（供 system prompt 注入）。"""
    lines = ["【可调用指令】"]
    for c in caps:
        if c.category != CATEGORY_COMMAND:
            continue
        if c.description:
            lines.append(f"  .{c.name}: {c.description}")
        else:
            lines.append(f"  .{c.name}")
    return "\n".join(lines)


def load_skill_text(name: str) -> str:
    """加载单个 Skill 正文（未命中返回空串）。"""
    if not name:
        return ""
    try:
        from core.agent.skill_registry import get_skill_registry
        return get_skill_registry().load(name) or ""
    except Exception:
        return ""


def load_skills(caps: list[Capability], top_k: int = 2) -> str:
    """加载选中 Skill 能力正文，拼接为一段（预算内）。"""
    parts: list[str] = []
    for c in caps:
        if c.category != CATEGORY_SKILL:
            continue
        name = c.name
        text = load_skill_text(name)
        if text:
            parts.append(f"【Skill: {name}】\n{text}")
        if len(parts) >= top_k:
            break
    return "\n\n".join(parts)


class CapabilityLoader:
    """确认后再加载：对选中能力做精细加载。"""

    def resolve(self, query: str, intent: str = "", top_k: int = 6,
                is_group: bool = True) -> dict:
        """统一编排：返回 {caps, fc_schemas, command_usage, skill_text}。

        caps：命中的能力元数据（目录）；其余字段为按需加载的完整内容。
        """
        caps = get_capability_router().route(query, intent, top_k=top_k, is_group=is_group)
        fc_schemas = load_fc_schemas(caps)
        command_usage = build_command_usage(caps)
        skill_text = load_skills(caps)
        return {
            "caps": caps,
            "fc_schemas": fc_schemas,
            "command_usage": command_usage,
            "skill_text": skill_text,
        }


# ── 全局单例 ───────────────────────────────────────────────
_loader: Optional[CapabilityLoader] = None


def get_capability_loader() -> CapabilityLoader:
    global _loader
    if _loader is None:
        _loader = CapabilityLoader()
    return _loader