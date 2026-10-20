"""把 libs/<name>/ 打包成 `<name>-<version>.hml`（tar.gz），并自校验。

用法：
    python scripts/build_hml.py hmcore            # 打包 libs/hmcore → dist/hmcore-1.0.0.hml
    python scripts/build_hml.py hmcore --no-verify

自校验：解包到临时目录 → 校验 hml.json/模块目录 → 把临时目录加进 sys.path 后真实 import 一次
（import 失败则打包失败，避免发出坏包）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBS = ROOT / "libs"
DIST = ROOT / "dist"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def _filter(ti: tarfile.TarInfo):
    parts = Path(ti.name).parts
    if any(p in SKIP_DIRS for p in parts):
        return None
    if Path(ti.name).suffix in SKIP_SUFFIX:
        return None
    return ti


def verify(hml: Path, modules: list[str]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with tarfile.open(hml, "r:gz") as tar:
            tar.extractall(tmp)
        if not (tmp / "hml.json").is_file():
            return False, "解包后缺少 hml.json"
        sys.path.insert(0, str(tmp))
        try:
            for m in modules:
                if m in sys.modules:
                    del sys.modules[m]
                importlib.import_module(m)
        except Exception as e:
            return False, f"import {m} 失败: {type(e).__name__}: {e}"
        finally:
            sys.path.remove(str(tmp))
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="libs/ 下的库目录名")
    ap.add_argument("--no-verify", action="store_true", help="跳过解包 + import 自校验")
    args = ap.parse_args()

    src = LIBS / args.name
    mf = src / "hml.json"
    if not mf.is_file():
        print(f"FAIL 缺少 {mf}")
        return 1
    meta = json.loads(mf.read_text(encoding="utf-8"))
    for k in ("format", "name", "version", "modules"):
        if k not in meta:
            print(f"FAIL hml.json 缺字段 {k}")
            return 1
    if meta["format"] != "hml":
        print(f"FAIL format 必须是 hml（当前 {meta['format']}）")
        return 1

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"{meta['name']}-{meta['version']}.hml"
    with tarfile.open(out, "w:gz") as tar:
        for child in sorted(src.iterdir()):
            if child.name in SKIP_DIRS:
                continue
            tar.add(child, arcname=child.name, filter=_filter)
    size = out.stat().st_size
    print(f"打包 {meta['name']}@{meta['version']} → {out} ({size} 字节)")

    if not args.no_verify:
        ok, msg = verify(out, list(meta["modules"]))
        print(("自校验 OK  " if ok else "自校验 FAIL  ") + msg)
        if not ok:
            out.unlink(missing_ok=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
