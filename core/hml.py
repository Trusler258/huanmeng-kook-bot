"""HuanMeng Lib（`.hml`）装载器 —— 核心库的安装与导入路径。

约定（本文件定义，`plugins/phi/lib/BUILD.md` 里 `.hml` 的"位置按需另定"即由这里落地）：

    libs/<name>/                 ← 安装位置（与 plugins/ 平级）
    ├── hml.json                 ← {"format":"hml","name","version","python","summary","modules"}
    └── <name>/                  ← Python 包，插件 `from <name> import ...` 即可

`.hml` 文件本身是 tar.gz：根目录就是上面这层（hml.json + 包目录）。

启动时调 `ensure_libs_path()` 把 `libs/` 加进 sys.path；插件无需知道安装路径。
安装/卸载/列出：`python -m core.hml <install|list|uninstall> [参数]`
"""
from __future__ import annotations

import json
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

FORMAT_TAG = "hml"
MANIFEST_NAME = "hml.json"
_REQUIRED = ("format", "name", "version", "modules")


def libs_dir() -> Path:
    """库根目录：<项目根>/libs（与 plugins/ 平级）。"""
    return Path(__file__).resolve().parent.parent / "libs"


def ensure_libs_path() -> list[str]:
    """把每个库目录加入 sys.path（幂等）。返回已安装库名列表。

    ⚠️ 只加 `libs/` 不够：`libs/hmcore/` 与包名 `hmcore` 同名，Python 会把它当
    命名空间包解析到那个空目录（`cannot import name ... from 'hmcore' (unknown location)`）。
    因此逐个把 `libs/<name>/` 插到 sys.path 前面，`import hmcore` 才命中
    `libs/hmcore/hmcore/__init__.py`。

    启动早期调用一次即可；插件加载前必须调用，否则 `import hmcore` 会失败。
    """
    d = libs_dir()
    if not d.is_dir():
        return []
    names: list[str] = []
    for it in list_libs():
        names.append(it["name"])
        p = it["path"]
        if p not in sys.path:
            sys.path.insert(0, p)
    return names


def _read_manifest(root: Path) -> Optional[dict]:
    p = root / MANIFEST_NAME
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or any(k not in m for k in _REQUIRED):
        return None
    if m.get("format") != FORMAT_TAG:
        return None
    return m


def list_libs() -> list[dict]:
    """列出已安装的库（读每个目录的 hml.json）。"""
    d = libs_dir()
    out: list[dict] = []
    if not d.is_dir():
        return out
    for child in sorted(d.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        m = _read_manifest(child)
        if m:
            out.append({**m, "path": str(child)})
    return out


def _safe_members(tar: tarfile.TarFile, dest: Path) -> None:
    """拒绝绝对路径 / 路径穿越 / 设备文件，防止解包写到目录外。"""
    dest = dest.resolve()
    for m in tar.getmembers():
        name = m.name.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"禁止的归档路径: {m.name}")
        if m.issym() or m.islnk():
            raise ValueError(f"禁止的链接条目: {m.name}")
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError(f"路径越界: {m.name}")


def install(hml_path: str | Path, *, force: bool = True) -> tuple[bool, str]:
    """安装一个 .hml（tar.gz）。同名库已存在时先备份到 libs/.backup/。"""
    src = Path(hml_path)
    if not src.is_file():
        return False, f"文件不存在: {src}"
    root = libs_dir()
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            with tarfile.open(src, "r:gz") as tar:
                _safe_members(tar, tmp)
                try:
                    tar.extractall(tmp, filter="data")
                except TypeError:      # Python < 3.11.4 不支持 filter
                    tar.extractall(tmp)
        except (tarfile.TarError, ValueError, OSError) as e:
            return False, f"解包失败: {e}"

        m = _read_manifest(tmp)
        if m is None:
            return False, f"{MANIFEST_NAME} 缺失或格式不对（需含 {_REQUIRED}，且 format=hml）"
        name, version = str(m["name"]), str(m["version"])
        if not name.replace("_", "").replace("-", "").isalnum():
            return False, f"非法库名: {name!r}"

        missing = [mod for mod in m.get("modules", []) if not (tmp / str(mod)).is_dir()]
        if missing:
            return False, f"模块目录缺失: {missing}"

        dest = root / name
        if dest.exists():
            if not force:
                return False, f"已安装 {name}（force=False）"
            bak = root / ".backup" / f"{name}-{time.strftime('%Y%m%d_%H%M%S')}"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(bak))
        shutil.copytree(tmp, dest)   # tmp 是临时目录，复制而不是 move（with 退出会删）
        ensure_libs_path()
        return True, f"已安装 {name}@{version} → {dest}"


def uninstall(name: str) -> tuple[bool, str]:
    d = libs_dir() / name
    if not d.is_dir():
        return False, f"未安装: {name}"
    bak = libs_dir() / ".backup" / f"{name}-{time.strftime('%Y%m%d_%H%M%S')}"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(bak))
    return True, f"已卸载 {name}（备份在 {bak}）"


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("用法: python -m core.hml <install|list|uninstall> [参数]")
        return 0
    cmd = argv[0]
    if cmd == "list":
        items = list_libs()
        if not items:
            print(f"(libs/ 为空：{libs_dir()})")
        for it in items:
            print(f"{it['name']:<16} {it['version']:<10} {it.get('summary','')}")
        return 0
    if cmd == "install":
        if len(argv) < 2:
            print("用法: python -m core.hml install <file.hml>")
            return 2
        ok, msg = install(argv[1])
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if cmd == "uninstall":
        if len(argv) < 2:
            print("用法: python -m core.hml uninstall <name>")
            return 2
        ok, msg = uninstall(argv[1])
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    print(f"未知子命令: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
