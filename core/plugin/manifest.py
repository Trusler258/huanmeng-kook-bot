"""
Plugin Manifest（v2，向下兼容 v1）

描述一个插件的元数据与声明式能力。Manifest 决定插件可被加载的范围与权限，
是隔离与安全的基础：插件只能在其 manifest 声明的 permissions 内行事。

v2 变更（详见 docs/plugin-manifest-v2.md 与 manifest.schema.json）：
  · 新增 manifest_version（schema 版本，缺省=1；平台支持 1..2）
  · 新增声明式 commands / tools（含 aliases，解决无前缀别名如 /phi）
  · 新增 config_schema / secrets（配置类型、敏感项打码）
  · 新增 storage / limits / network / requires / events / hooks / timers
  · 未知字段一律保留进 extra，不报错（前向兼容）

兼容铁律：v1 manifest 的解析结果与行为完全不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 运行时可支持的值
RUNTIME_PYTHON = "python"
RUNTIME_LUA = "lua"
SUPPORTED_RUNTIMES = (RUNTIME_PYTHON, RUNTIME_LUA)

# manifest schema 版本（插件声明的 manifest_version 必须落在支持区间内）
MANIFEST_VERSION_MIN = 1
MANIFEST_VERSION_MAX = 2
SUPPORTED_MANIFEST_VERSIONS = tuple(range(MANIFEST_VERSION_MIN, MANIFEST_VERSION_MAX + 1))

# manifest 必需/可选字段（v1 语义，保持不变）
_REQUIRED_FIELDS = ("name", "version", "runtime", "entrypoint")

# v1/v2 已知字段（其余字段 → extra，保留但不报错）
_KNOWN_FIELDS = frozenset({
    "manifest_version", "name", "version", "runtime", "entrypoint",
    "description", "author", "license", "homepage", "icon", "tags",
    "dependencies", "permissions", "config", "config_schema", "secrets",
    "commands", "tools", "storage", "limits", "network", "requires",
    "enabled_by_default", "priority", "events", "hooks", "timers",
    "i18n", "extra",
})


@dataclass
class PluginManifest:
    # ── v1 字段（顺序与语义不变） ──────────────────────────────
    name: str
    version: str
    runtime: str = RUNTIME_PYTHON
    entrypoint: str = "main.py"
    description: str = ""
    author: str = ""
    dependencies: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    # ── v2 新增（全部可选，缺失即默认值） ──────────────────────
    manifest_version: int = MANIFEST_VERSION_MIN
    license: str = ""
    homepage: str = ""
    icon: str = ""
    tags: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    config_schema: dict = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    storage: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    network: dict = field(default_factory=dict)
    requires: dict = field(default_factory=dict)
    enabled_by_default: bool = True
    priority: int = 100
    events: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    timers: list[dict] = field(default_factory=list)
    i18n: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    # 装载路径（由 loader 填充，非 manifest 声明）
    base_dir: str = ""
    # ★ v2.3.104：True 表示来自 `.hmp` 包（base_dir 指向包文件，不是目录）
    archive: bool = False
    raw: dict = field(default_factory=dict)

    # ── 便捷访问 ────────────────────────────────────────────────
    def command_names(self) -> list[str]:
        """本插件声明的命令名（不含别名）。"""
        return [str(c.get("name")) for c in self.commands if isinstance(c, dict) and c.get("name")]

    def command_aliases(self) -> list[str]:
        """所有别名（含无前缀写法，如 '/phi'），用于平台预注册。"""
        out: list[str] = []
        for c in self.commands:
            if not isinstance(c, dict):
                continue
            for a in (c.get("aliases") or []):
                a = str(a).strip()
                if a and a not in out:
                    out.append(a)
        return out

    def is_secret(self, key: str) -> bool:
        """判断某项配置是否敏感（声明的 secrets 或 config_schema.secret=true）。"""
        if key in (self.secrets or []):
            return True
        spec = (self.config_schema or {}).get(key)
        return bool(isinstance(spec, dict) and spec.get("secret"))

    def config_default(self, key: str, default: Any = None) -> Any:
        """取配置默认值：config_schema.default 优先，其次 config（v1）。"""
        spec = (self.config_schema or {}).get(key)
        if isinstance(spec, dict) and "default" in spec:
            return spec["default"]
        if isinstance(self.config, dict) and key in self.config:
            return self.config[key]
        return default

    def to_light(self) -> dict:
        """给模型/日志的轻量元数据，不含敏感配置。"""
        return {
            "name": self.name,
            "version": self.version,
            "runtime": self.runtime,
            "description": self.description,
            "permissions": list(self.permissions),
            "manifest_version": self.manifest_version,
        }


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def validate_manifest(data: dict) -> tuple[Optional[PluginManifest], Optional[str]]:
    """校验原始 manifest dict（v1 或 v2）。返回 (manifest, error)。"""
    if not isinstance(data, dict):
        return None, "manifest 必须是 JSON 对象"

    # ── schema 版本（先判，避免新版语义被静默忽略） ──────────────
    mv = _as_int(data.get("manifest_version", MANIFEST_VERSION_MIN), MANIFEST_VERSION_MIN)
    if mv > MANIFEST_VERSION_MAX:
        return None, (f"插件要求 manifest_version={mv}，当前平台仅支持 "
                      f"{MANIFEST_VERSION_MIN}..{MANIFEST_VERSION_MAX}")
    if mv < MANIFEST_VERSION_MIN:
        mv = MANIFEST_VERSION_MIN
    # 未知字段（含未来版本的字段）：保留但不报错 —— 前向兼容
    unknown = {k: v for k, v in data.items() if k not in _KNOWN_FIELDS}
    extra = _as_dict(data.get("extra"))
    if unknown:
        extra = {**unknown, **extra}

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

    # v2 结构化字段：类型不对时降级为默认值（宽容解析，不因小错拒绝加载）
    commands = [c for c in _as_list(data.get("commands")) if isinstance(c, dict)]
    tools = [t for t in _as_list(data.get("tools")) if isinstance(t, dict)]
    timers = [t for t in _as_list(data.get("timers")) if isinstance(t, dict)]

    requires = _as_dict(data.get("requires"))
    deps = [str(d) for d in _as_list(data.get("dependencies"))]

    # ── 依赖插件声明的归并（2026-10-12）─────────────────────────────
    # 支持三处写法，统一合并进 requires.plugins（去重保序）：
    #   1) v2 规范:  requires.plugins: ["points", "shop"]
    #   2) v1 兼容:  dependencies:      ["points"]
    #   3) 兜底写法: extra.depends_on / extra.depends / extra.dependencies
    #      （extra 本用于"未知字段前向兼容"，插件作者若把依赖写这里也照样认）
    req_plugins = [str(p) for p in _as_list(requires.get("plugins"))]
    extra_deps: list = []
    for k in ("depends_on", "depends", "dependencies"):
        extra_deps += _as_list(extra.get(k))
    merged_plugins: list[str] = []
    for p in deps + req_plugins + [str(x) for x in extra_deps]:
        if p and p not in merged_plugins:
            merged_plugins.append(p)
    if merged_plugins:
        requires["plugins"] = merged_plugins

    # 软依赖（缺失不阻止加载，仅可通过 ctx.plugins.has() 自行判断）
    opt_plugins = [str(p) for p in _as_list(requires.get("optional_plugins"))]
    for k in ("optional", "optional_plugins"):
        opt_plugins += [str(x) for x in _as_list(extra.get(k))]
    opt_plugins = [p for p in dict.fromkeys(opt_plugins) if p]
    if opt_plugins:
        requires["optional_plugins"] = opt_plugins

    manifest = PluginManifest(
        name=name,
        version=str(data.get("version", "0.0.0")),
        runtime=runtime,
        entrypoint=str(data.get("entrypoint", "main.py")),
        description=str(data.get("description", "")),
        author=str(data.get("author", "")),
        dependencies=deps,
        permissions=permissions,
        config=_as_dict(data.get("config")),
        # v2
        manifest_version=mv,
        license=str(data.get("license", "")),
        homepage=str(data.get("homepage", "")),
        icon=str(data.get("icon", "")),
        tags=[str(t) for t in _as_list(data.get("tags"))],
        commands=commands,
        tools=tools,
        config_schema=_as_dict(data.get("config_schema")),
        secrets=[str(s) for s in _as_list(data.get("secrets"))],
        storage=_as_dict(data.get("storage")),
        limits=_as_dict(data.get("limits")),
        network=_as_dict(data.get("network")),
        requires=requires,
        enabled_by_default=bool(data.get("enabled_by_default", True)),
        priority=_as_int(data.get("priority", 100), 100),
        events=[str(e) for e in _as_list(data.get("events"))],
        hooks=[str(h) for h in _as_list(data.get("hooks"))],
        timers=timers,
        i18n=_as_dict(data.get("i18n")),
        extra=extra,
        raw=data,
    )
    return manifest, None
