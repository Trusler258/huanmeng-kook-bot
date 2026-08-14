"""
Phase 10 Capability System（Huanmeng 2.0）

统一 Skill / Command / Tool / Plugin 为 Capability 抽象：
- metadata：id / name / description / tags / category / permissions / runtime
- registry：CapabilityRegistry（发现 + 登记 + 查询）
- router：CapabilityRouter（query + intent → 相关能力，metadata 级）
- loader：CapabilityLoader（确认后按需加载 Tool Schema / Skill 正文 / 指令用法）

原则：模型只看到当前请求相关的能力目录，确认需要后再加载完整内容；
禁止每次请求把全部 COMMAND_MAP / Skill / Tool Schema 塞进 System Prompt。
"""
from core.capability.metadata import (
    Capability,
    CATEGORY_TOOL, CATEGORY_COMMAND, CATEGORY_SKILL, CATEGORY_PLUGIN,
    RUNTIME_FC, RUNTIME_COMMAND, RUNTIME_SKILL, RUNTIME_PYTHON, RUNTIME_LUA,
)
from core.capability.registry import get_capability_registry
from core.capability.router import get_capability_router
from core.capability.loader import get_capability_loader, load_fc_schemas

__all__ = [
    "Capability",
    "CATEGORY_TOOL", "CATEGORY_COMMAND", "CATEGORY_SKILL", "CATEGORY_PLUGIN",
    "RUNTIME_FC", "RUNTIME_COMMAND", "RUNTIME_SKILL", "RUNTIME_PYTHON", "RUNTIME_LUA",
    "get_capability_registry", "get_capability_router", "get_capability_loader",
    "load_fc_schemas",
]