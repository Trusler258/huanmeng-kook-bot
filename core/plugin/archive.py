"""`.hmp` 插件包加载（方案 A：plugins/ 只放包，运行时从包内加载，不解压）。

包格式：**zip**（与 .jar 同理，zipimport 只认 zip）
    greet-1.0.0.hmp
    ├── manifest.json
    ├── main.py               ← entrypoint，必须在包根
    └── （可选）其它资源

约束（有意为之）：
- 只支持**入口在包根**的插件：`zipimport.get_code('main')`
- 代码里的 `__file__` 指向 `<包>/main.py`（包内虚拟路径），**不能**用 `Path(__file__).parent`
  读同目录文件——需要资源请用 `read_asset()` / `list_assets()`
- 因此带同目录模块/资源的插件（如 gh/phi/wdsj）暂不能打包，仍用目录形态（两种形态共存）
"""
from __future__ import annotations

import json
import sys
import types
import zipimport
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("plugin.archive")

ARCHIVE_SUFFIX = ".hmp"
MANIFEST_FILE = "manifest.json"


def is_archive(path: Path) -> bool:
    """是不是一个可用的插件包（zip + 根目录有 manifest.json）。"""
    return path.is_file() and path.suffix.lower() == ARCHIVE_SUFFIX and read_manifest(path) is not None

def _read_member(path: Path, member: str) -> Optional[bytes]:
    """读包内单个文件（zipimport 没有读任意文件的 API，用 zipfile）。"""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            try:
                return z.read(member)
            except KeyError:
                return None
    except Exception:
        return None


def read_manifest(path: Path) -> Optional[dict]:
    """从包内读 manifest.json。"""
    raw = _read_member(path, MANIFEST_FILE)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_asset(archive: Path, name: str) -> Optional[bytes]:
    """读包内资源（插件要读同目录文件时用这个，而不是 open(__file__ 同级)）。"""
    name = str(name).replace("\\", "/").lstrip("/")
    if ".." in Path(name).parts:
        raise ValueError(f"路径越界: {name}")
    return _read_member(archive, name)


def list_assets(archive: Path) -> list[str]:
    """列出包内全部文件（调试/迁移用）。"""
    import zipfile
    try:
        with zipfile.ZipFile(archive) as z:
            return [n for n in z.namelist() if not n.endswith("/")]
    except Exception:
        return []


def load_module(archive: Path, entrypoint: str, module_name: str):
    """从包内加载入口模块（zipimport），返回模块对象。

    做法：get_code() 取入口字节码 → 造一个模块对象，`__file__` 指向包内虚拟路径 →
    临时把包加入 sys.path（让包内的 `import xxx` 也能解析）→ exec。
    """
    entry = str(entrypoint).replace("\\", "/").lstrip("/")
    if not entry.endswith(".py"):
        entry += ".py"
    stem = entry[:-3].replace("/", ".")
    imp = zipimport.zipimporter(str(archive))
    code = imp.get_code(stem)

    mod = types.ModuleType(module_name)
    mod.__file__ = f"{archive}/{entry}"
    mod.__loader__ = imp
    mod.__package__ = ""
    sys.modules[module_name] = mod

    added = False
    p = str(archive)
    if p not in sys.path:
        sys.path.insert(0, p)
        added = True
    try:
        exec(code, mod.__dict__)
    finally:
        if added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass
    logger.debug("已从包内加载插件模块: %s ← %s", module_name, archive.name)
    return mod
