"""把 plugins/<name>/ 打包成 `<name>-<version>.hmp`（zip），并自校验。

用法：
    python scripts/build_hmp.py dice
    python scripts/build_hmp.py dice weather echo        # 批量
    python scripts/build_hmp.py --all-single-file        # 只打「纯单文件」插件

`--all-single-file` 只处理 main.py + manifest.json 两个文件的插件（其余依赖同目录
模块/资源，当前加载器不支持，跳过并列出）。

自校验：zipimport 读入口 + 解包后真实 import 一次，失败则删包。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
DIST = ROOT / "dist"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def pack(name: str) -> tuple[bool, str]:
    src = PLUGINS / name
    mf = src / "manifest.json"
    if not mf.is_file():
        return False, f"{name}: 缺少 manifest.json"
    meta = json.loads(mf.read_text(encoding="utf-8"))
    out = DIST / f"{name}-{meta.get('version', '0.0.0')}.hmp"
    DIST.mkdir(parents=True, exist_ok=True)

    files = []
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in sorted(fns):
            if Path(fn).suffix in SKIP_SUFFIX:
                continue
            fp = Path(dp) / fn
            files.append((fp, str(fp.relative_to(src)).replace("\\", "/")))

    # 入口必须在包根，否则 zipimport 取不到
    entry = str(meta.get("entrypoint", "main.py")).replace("\\", "/")
    if "/" in entry:
        return False, f"{name}: entrypoint 必须位于包根（当前 {entry}）"
    if entry not in [arc for _, arc in files]:
        return False, f"{name}: 包内缺少入口 {entry}"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for fp, arc in files:
            z.write(fp, arcname=arc)

    ok, msg = verify(out, meta)
    if not ok:
        out.unlink(missing_ok=True)
        return False, f"{name}: 自校验失败 {msg}"
    return True, f"{name}@{meta.get('version')} → {out.name} ({out.stat().st_size} 字节, {len(files)} 文件)"


def verify(hml: Path, meta: dict) -> tuple[bool, str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with zipfile.ZipFile(hml) as z:
            z.extractall(tmp)
        entry = str(meta.get("entrypoint", "main.py"))
        modname = "_hmp_verify"
        spec = importlib.util.spec_from_file_location(modname, tmp / entry)
        if spec is None or spec.loader is None:
            return False, "无法创建 spec"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            return False, f"import {entry} 失败: {type(e).__name__}: {e}"
        finally:
            sys.modules.pop(modname, None)
        if not any(n == "Plugin" for n in dir(mod)):
            return False, "包内未找到 Plugin 类"
    return True, "ok"


def is_single_file(name: str) -> bool:
    d = PLUGINS / name
    if not d.is_dir():
        return False
    files = [f for f in d.iterdir() if f.is_file() and f.suffix not in SKIP_SUFFIX]
    return {f.name for f in files} <= {"main.py", "manifest.json"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="插件目录名")
    ap.add_argument("--all-single-file", action="store_true", help="打包所有纯单文件插件")
    args = ap.parse_args()

    if args.all_single_file:
        names = [d.name for d in sorted(PLUGINS.iterdir())
                 if d.is_dir() and not d.name.startswith(("[", ".")) and is_single_file(d.name)]
        skipped = [d.name for d in sorted(PLUGINS.iterdir())
                   if d.is_dir() and not d.name.startswith(("[", ".")) and not is_single_file(d.name)]
        print(f"纯单文件插件 {len(names)} 个: {names}")
        print(f"跳过（依赖同目录文件）{len(skipped)} 个: {skipped}")
    else:
        names = args.names
    if not names:
        print("用法: python scripts/build_hmp.py <name...> | --all-single-file")
        return 2

    bad = 0
    for n in names:
        ok, msg = pack(n)
        print(("OK   " if ok else "FAIL ") + msg)
        if not ok:
            bad += 1
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
