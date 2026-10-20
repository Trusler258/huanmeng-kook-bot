"""插件脚手架 —— 从现有插件里**原样搬过来**的通用部分。

来源（逐字相同，故抽取）：
- `load_core` : plugins/gh、plugins/ping、plugins/voice… 共 4 份
- `is_admin`  : plugins/points、plugins/shop 共 2 份
- `PluginBase`: 28 个插件重复的 __init__ / on_load / on_enable / on_disable / on_unload
"""
from __future__ import annotations

import importlib.util as _ilu
import sys as _sys
from pathlib import Path
from typing import Any, Optional

_CORE_CACHE: dict[str, Any] = {}


def load_core(plugin_file: str | Path, module_file: str,
              module_name: Optional[str] = None) -> Any:
    """按文件路径懒加载「插件目录内的 core 模块」（插件目录不是 package，不能直接 import）。

    与原来 4 份 `_load_core()` 行为一致：首次加载后缓存，后续直接返回；
    加载结果登记进 sys.modules（模块内部若有相对引用也能成立）。

    参数：
        plugin_file : 插件的 `__file__`（本库用它定位插件目录）
        module_file : 插件目录内的文件名，如 "gh_core.py"
        module_name : 可选，默认 `_hm_plugin_<插件目录名>_<模块名>`
    """
    base = Path(plugin_file).resolve().parent
    key = str(base / module_file)
    if key in _CORE_CACHE:
        return _CORE_CACHE[key]

    name = module_name or f"_hm_plugin_{base.name}_{Path(module_file).stem}"
    spec = _ilu.spec_from_file_location(name, base / module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {base / module_file}")
    mod = _ilu.module_from_spec(spec)
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _CORE_CACHE[key] = mod
    return mod


def is_admin(uid, cfg_admins=None) -> bool:
    """管理员判定（与 points / shop 里的 `_is_admin` 等价）。

    先比 `cfg.admin_id_str`（唯一主人），再比传入的管理员列表。
    """
    s = str(uid)
    try:
        from core.config import get_config
        c = get_config()
        if c.admin_id_str and s == str(c.admin_id_str):
            return True
    except Exception:
        pass
    return s in [str(a) for a in (list(cfg_admins or []))]


class PluginBase:
    """插件基类：只提供生命周期默认实现与常用属性，不做任何业务约定。

    插件继承后按需覆盖 on_load / on_enable / on_disable / on_unload；
    未覆盖的钩子只打一行 debug，不产生副作用。
    """

    def __init__(self, ctx):
        self.ctx = ctx

    # ── 常用属性 ──────────────────────────────────────
    @property
    def name(self) -> str:
        return getattr(self.ctx, "name", "") or self.__class__.__module__

    @property
    def log(self):
        return self.ctx.logger

    # ── 生命周期默认实现（插件可覆盖）─────────────────
    async def on_load(self) -> None:
        self.log.debug("[%s] on_load（基类默认实现）", self.name)

    async def on_enable(self) -> None:
        self.log.debug("[%s] on_enable（基类默认实现）", self.name)

    async def on_disable(self) -> None:
        self.log.debug("[%s] on_disable（基类默认实现）", self.name)

    async def on_unload(self) -> None:
        self.log.debug("[%s] on_unload（基类默认实现）", self.name)
