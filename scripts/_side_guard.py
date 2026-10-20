"""双端守卫：识别当前改的是 QQ 还是 KOOK，并扫出「串端」的指令前缀。

背景（2026-10-20）：两端指令前缀不同——
    QQ   = `/~cmd`（另有 `/#cmd` 管理员私聊、`/cmd`）
    KOOK = `.cmd`
把代码/提示语从一端挪到另一端时，最容易把 `/~wdsj` 抄成 KOOK 的、或把 `.draw` 抄进 QQ。

用法：
    python scripts/_side_guard.py ..\\bot\\kookbot\\kook-bot-opensource
    python scripts/_side_guard.py .            # 当前仓库

退出码：0 = 干净；1 = 发现串端（可当 pre-commit 用）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 本端**不该出现**的前缀模式（只扫这些文件，误报最低）
_SCAN_FILES = ("modules/commands.py", "config/lang.toml")


def detect(root: Path) -> dict:
    """判定仓库属于哪一端，并读出关键事实。"""
    info = {
        "root": str(root),
        "side": "未知",
        "version": "",
        "service": "",
        "prefix": "",
        "version_file": "",
    }
    # 版本：QQ 用 config/version.toml，KOOK 用 core/version.py
    ver_toml = root / "config" / "version.toml"
    ver_py = root / "core" / "version.py"
    if ver_toml.exists():
        m = re.search(r'current\s*=\s*"([^"]+)"', ver_toml.read_text(encoding="utf-8", errors="ignore"))
        info["version"] = m.group(1) if m else "?"
        info["version_file"] = "config/version.toml"
    elif ver_py.exists():
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', ver_py.read_text(encoding="utf-8", errors="ignore"))
        info["version"] = m.group(1) if m else "?"
        info["version_file"] = "core/version.py"

    disp = root / "core" / "dispatcher.py"
    cmds = root / "modules" / "commands.py"
    kook_mark = False
    if disp.exists():
        t = disp.read_text(encoding="utf-8", errors="ignore")
        kook_mark = 'startswith(".")' in t or "startswith('.')" in t

    if kook_mark:
        info.update(side="KOOK", service="kook-bot.service", prefix=".cmd")
    elif cmds.exists() and '"/~"' in cmds.read_text(encoding="utf-8", errors="ignore"):
        info.update(side="QQ", service="bot.service", prefix="/~cmd")
    return info


def scan(root: Path, side: str) -> list[tuple[str, int, str]]:
    """扫出本端不该出现的前缀。返回 [(文件, 行号, 内容)]。"""
    hits: list[tuple[str, int, str]] = []
    if side == "KOOK":
        pat = re.compile(r"/[~#]")
    elif side == "QQ":
        # QQ 里出现 ".指令名" 的用法串 → 典型是从 KOOK 抄来的
        pat = re.compile(r"[\"']\.(owner|wdsj|draw|kook|fault|help|cost|info|up|ping|s|remind|favlist|抽|luck)\b")
    else:
        return hits
    for rel in _SCAN_FILES:
        p = root / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pat.search(line):
                hits.append((rel, i, line.strip()[:110]))
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if not (root / "modules").exists():
        print(f"[!] 不像仓库根目录：{root}")
        return 2
    info = detect(root)
    print("=" * 58)
    print(f"  仓库  : {info['root']}")
    print(f"  端    : {info['side']}")
    print(f"  版本  : {info['version']}  ({info['version_file']})")
    print(f"  指令  : {info['prefix']}")
    print(f"  服务  : {info['service']}")
    print("=" * 58)

    hits = scan(root, info["side"])
    if not hits:
        print(f"[OK] 未发现串端前缀（已扫 {', '.join(_SCAN_FILES)}）")
        return 0
    print(f"[!] 发现 {len(hits)} 处可能是另一端的写法：")
    for rel, ln, txt in hits[:20]:
        print(f"  {rel}:{ln}  {txt}")
    if len(hits) > 20:
        print(f"  ……另有 {len(hits) - 20} 处")
    return 1


if __name__ == "__main__":
    sys.exit(main())
