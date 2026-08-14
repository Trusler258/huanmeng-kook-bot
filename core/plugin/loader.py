"""
Phase 13 Plugin Loader（Huanmeng 2.0）

从 plugins/ 目录发现并加载插件：
- discover()：扫描目录，读取 manifest.json，校验得到 PluginManifest
- load_module()：为 python runtime 动态导入 entrypoint 模块；lua runtime 由 Phase 14 处理
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

from core.logger import get_logger
from core.plugin.manifest import PluginManifest, validate_manifest, RUNTIME_LUA

logger = get_logger("plugin.loader")

MANIFEST_FILE = "manifest.json"


def discover_plugins(plugins_dir: str) -> list[PluginManifest]:
    """扫描目录，返回所有合法插件的 manifest。非法插件跳过并告警。"""
    manifests: list[PluginManifest] = []
    base = Path(plugins_dir)
    try:
        if not base.is_dir():
            return manifests
    except OSError:
        return manifests

    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        mf_path = child / MANIFEST_FILE
        if not mf_path.is_file():
            logger.debug("跳过 %s：缺少 %s", child.name, MANIFEST_FILE)
            continue
        try:
            data = json.loads(mf_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("插件 %s manifest 解析失败: %s", child.name, e)
            continue
        manifest, err = validate_manifest(data)
        if err:
            logger.warning("插件 %s 校验失败: %s", child.name, err)
            continue
        manifest.base_dir = str(child)
        manifests.append(manifest)
    return manifests


def load_module(manifest: PluginManifest) -> Optional[object]:
    """加载 python runtime 插件的 entrypoint 模块，返回模块对象。"""
    if manifest.runtime == RUNTIME_LUA:
        # Lua 插件由 Phase 14 LuaRuntime 处理，不在 python 层动态导入
        return None
    entry = Path(manifest.base_dir) / manifest.entrypoint
    if not entry.is_file():
        logger.warning("插件 %s 入口不存在: %s", manifest.name, entry)
        return None
    module_name = f"_hm_plugin_{manifest.name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(entry))
        if spec is None or spec.loader is None:
            logger.warning("插件 %s 无法创建 spec", manifest.name)
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        logger.warning("插件 %s 加载失败: %s", manifest.name, e)
        sys.modules.pop(module_name, None)
        return None


def locate_plugin_classes(module: object) -> list[type]:
    """从模块中找出 Plugin 类（名为 Plugin 或含 on_load/on_enable 钩子的类）。"""
    classes: list[type] = []
    for attrn in dir(module):
        if attrn.startswith("_"):
            continue
        obj = getattr(module, attrn)
        if not isinstance(obj, type):
            continue
        if attrn == "Plugin" or (
            hasattr(obj, "on_load") and hasattr(obj, "on_unload")
        ):
            classes.append(obj)
    return classes