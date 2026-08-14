"""
Phase 10 Capability System：能力元数据（Huanmeng 2.0）

把 Skill / Command / Tool / Plugin 统一为 Capability 抽象。Capability Metadata
至少包含：id、name、description、tags、category、permissions、runtime。

设计约束：
- 纯数据 / 无副作用，可独立测试。
- capability 是「元数据 + 加载器标识」的轻量描述，不携带完整实现；
  模型一开始只能看到 to_light() 的精简元数据，确认需要后再加载完整内容。
- 兼容性：新增文件，不改变现有 Tool/Command/Skill 的调用方式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# capability 的来源类别
CATEGORY_TOOL = "tool"            # Function Calling 工具（core.tools）
CATEGORY_COMMAND = "command"      # 指令（modules.commands.COMMAND_MAP）
CATEGORY_SKILL = "skill"          # 技能（skills/*.md，按需全文加载）
CATEGORY_PLUGIN = "plugin"        # 插件（Phase 13+，Python/Lua）

# 运行时标识
RUNTIME_FC = "fc"                 # Function Calling
RUNTIME_COMMAND = "command"       # 指令 handler
RUNTIME_SKILL = "skill"           # skill 正文
RUNTIME_PYTHON = "python"         # Python 插件
RUNTIME_LUA = "lua"               # Lua 插件


@dataclass
class Capability:
    """一份能力元数据。id 全局唯一，同一能力可同时具备多种运行时。"""
    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = CATEGORY_COMMAND
    permissions: list[str] = field(default_factory=list)
    runtime: str = RUNTIME_COMMAND
    # 来源标识（用于加载完整内容）
    source: str = ""
    # 别名（如 weather 的中文别名）
    aliases: list[str] = field(default_factory=list)
    # 是否核心常驻能力（如 help/ping，普通聊天也保留）
    always_on: bool = False

    def to_light(self) -> dict:
        """模型可见的精简元数据（不含任何完整实现）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category,
        }

    def to_dict(self) -> dict:
        """完整元数据（含 permissions/runtime/source），供注册表/审计。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category,
            "permissions": list(self.permissions),
            "runtime": self.runtime,
            "source": self.source,
            "aliases": list(self.aliases),
            "always_on": self.always_on,
        }