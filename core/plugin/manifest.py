"""
Phase 13 Plugin Manifest（Huanmeng 2.0）

描述一个插件的元数据与声明式权限。Manifest 决定插件可被加载的范围与权限，
是隔离与安全的基础：插件只能在其 manifest 声明的 permissions 内行事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 运行时可支持的值
RUNTIME_PYTHON = "python"
RUNTIME_LUA = "lua"
SUPPORTED_RUNTIMES = (RUNTIME_PYTHON, RUNTIME_LUA)

# manifest 必需/可选字段
_REQUIRED_FIELDS = ("name", "version", "runtime", "entrypoint")


@dataclass
class PluginManifest:
    name: str
    version: str
    runtime: str = RUNTIME_PYTHON
    entrypoint: str = "main.py"
    description: str = ""
    author: str = ""
    dependencies: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    # 装载路径（由 loader 填充，非 manifest 声明）
    base_dir: str = ""
    raw: dict = field(default_factory=dict)

    def to_light(self) -> dict:
        """给模型/日志的轻量元数据，不含敏感配置。"""
        return {
            "name": self.name,
            "version": self.version,
            "runtime": self.runtime,
            "description": self.description,
            "permissions": list(self.permissions),
        }


def validate_manifest(data: dict) -> tuple[Optional[PluginManifest], Optional[str]]:
    """校验原始 manifest dict。返回 (manifest, error)。"""
    if not isinstance(data, dict):
        return None, "manifest 必须是 JSON 对象"

    for f in _REQUIRED_FIELDS:
        if not data.get(f):
            return None, f"缺少必需字段: {f}"

    name = str(data["name"]).strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        return None, f"非法插件名: {name!r}"

    runtime = str(data.get("runtime", RUNTIME_PYTHON)).lower()
    if runtime not in SUPPORTED_RUNTIMES:
        return None, f"不支持的 runtime: {runtime}（支持 {SUPPORTED_RUNTIMES}）"

    permissions = data.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        return None, "permissions 必须是字符串数组"

    manifest = PluginManifest(
        name=name,
        version=str(data.get("version", "0.0.0")),
        runtime=runtime,
        entrypoint=str(data.get("entrypoint", "main.py")),
        description=str(data.get("description", "")),
        author=str(data.get("author", "")),
        dependencies=[str(d) for d in data.get("dependencies", [])],
        permissions=permissions,
        config=data.get("config", {}) if isinstance(data.get("config", {}), dict) else {},
        raw=data,
    )
    return manifest, None