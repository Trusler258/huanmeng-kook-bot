"""
Phase 10 Capability System：CapabilityRegistry（Huanmeng 2.0）

统一登记 Skill / Command / Tool / Plugin 的能力元数据（metadata 级，不加载正文）。
- discover()：从 core.tools、modules.commands、skills/ 目录收集能力。
- 去重：同一能力（如 tool weather 与 command weather/天气）合并为单一 id。
- register()：供 Plugin（Phase 13+）动态注册新能力。
- 惰性导入，避免 capability ↔ 业务模块循环依赖。
"""
from __future__ import annotations

from typing import Optional

from core.logger import get_logger
from core.capability.metadata import (
    Capability,
    CATEGORY_TOOL, CATEGORY_COMMAND, CATEGORY_SKILL, CATEGORY_PLUGIN,
    RUNTIME_FC, RUNTIME_COMMAND, RUNTIME_SKILL,
)

logger = get_logger("capability.registry")

# 核心常驻能力：普通聊天也保留，避免完全空上下文
CORE_ALWAYS_ON: frozenset[str] = frozenset({
    "help", "ping", "weather", "search_web", "search", "read_url", "calc",
})


class CapabilityRegistry:
    """Capability 注册表：发现 + 登记 + 查询（metadata 级）。"""

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}
        self._loaded = False

    # ── 发现 ──────────────────────────────────────────────
    def discover(self) -> None:
        """从各来源收集并合并能力元数据。"""
        if self._loaded:
            return
        merged: dict[str, Capability] = {}
        for cap in self._discover_tools() + self._discover_commands() + self._discover_skills():
            old = merged.get(cap.id)
            if old is None:
                merged[cap.id] = cap
                continue
            # 合并：补全运行时/来源/别名，保留更完整的 description
            if old.category == CATEGORY_COMMAND and cap.category == CATEGORY_TOOL:
                # 命令优先（保留命令运行时），补充工具来源
                old.source = cap.source
                old.runtime = f"{RUNTIME_COMMAND}+{RUNTIME_FC}"
                old.permissions = cap.permissions or old.permissions
                if not old.description:
                    old.description = cap.description
            elif cap.category == CATEGORY_TOOL and cap.runtime == RUNTIME_FC:
                old.runtime = f"{old.runtime}+{RUNTIME_FC}"
                old.permissions = cap.permissions or old.permissions
            # 别名并入
            for a in cap.aliases:
                if a not in old.aliases:
                    old.aliases.append(a)
        self._caps = merged
        self._loaded = True
        logger.info("CapabilityRegistry 发现 %d 个能力", len(self._caps))

    def _discover_tools(self) -> list[Capability]:
        try:
            from core.tools import TOOLS
            from core.tool_runtime.config import TOOL_PERMISSIONS
        except Exception:
            return []
        out: list[Capability] = []
        for t in TOOLS:
            fn = (t or {}).get("function", {})
            name = fn.get("name", "")
            if not name:
                continue
            out.append(Capability(
                id=name, name=name,
                description=fn.get("description", ""),
                category=CATEGORY_TOOL, runtime=RUNTIME_FC,
                source=f"tool:{name}",
                permissions=[TOOL_PERMISSIONS.get(name, "")] if TOOL_PERMISSIONS.get(name) else [],
                always_on=name in CORE_ALWAYS_ON,
            ))
        return out

    def _discover_commands(self) -> list[Capability]:
        try:
            from modules.commands import COMMAND_MAP
        except Exception:
            return []
        # 命令描述优先复用 llm 的 _CMD_DESC（惰性导入，避免顶层循环）
        try:
            from services.llm import _CMD_DESC
        except Exception:
            _CMD_DESC = {}
        out: list[Capability] = []
        for name in sorted(set(COMMAND_MAP)):
            out.append(Capability(
                id=name, name=name,
                description=_CMD_DESC.get(name, ""),
                category=CATEGORY_COMMAND, runtime=RUNTIME_COMMAND,
                source=f"command:{name}",
                always_on=name in CORE_ALWAYS_ON,
            ))
        return out

    def _discover_skills(self) -> list[Capability]:
        try:
            from core.agent.skill_registry import get_skill_registry
        except Exception:
            return []
        out: list[Capability] = []
        for meta in get_skill_registry().metadata():
            name = meta.get("name", "")
            if not name:
                continue
            out.append(Capability(
                id=f"skill:{name}", name=name,
                description=meta.get("description", ""),
                category=CATEGORY_SKILL, runtime=RUNTIME_SKILL,
                source=f"skill:{name}",
            ))
        return out

    # ── 查询 ──────────────────────────────────────────────
    def all(self) -> list[Capability]:
        self.discover()
        return list(self._caps.values())

    def get(self, cap_id: str) -> Optional[Capability]:
        self.discover()
        return self._caps.get(cap_id)

    def by_category(self, category: str) -> list[Capability]:
        return [c for c in self.all() if c.category == category]

    def always_on(self) -> list[Capability]:
        return [c for c in self.all() if c.always_on]

    # ── 登记（Plugin 用）──────────────────────────────────
    def register(self, cap: Capability) -> None:
        """注册（或覆盖）一个能力。供 Plugin 动态扩展。"""
        self._caps[cap.id] = cap
        self._loaded = True
        logger.info("CapabilityRegistry 注册能力: %s (%s)", cap.id, cap.category)

    def reload(self) -> None:
        self._loaded = False
        self._caps.clear()


# ── 全局单例 ───────────────────────────────────────────────
_registry: Optional[CapabilityRegistry] = None


def get_capability_registry() -> CapabilityRegistry:
    global _registry
    if _registry is None:
        _registry = CapabilityRegistry()
    return _registry