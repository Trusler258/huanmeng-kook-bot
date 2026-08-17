# -*- coding: utf-8 -*-
"""
插件分享工具：.hmp 打包 / 解包 / 从聊天下载 / 运行时加载
- .hmp = 仅储存(zip STORED)的插件压缩包，内含 manifest.json 等
- 本地下载目录统一放在 plugins/_down/（discover 因无 manifest 自动跳过该目录）
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("plugin.share")

HMP_EXT = ".hmp"
DOWN_DIR_NAME = "_down"
MAX_ZIP_SIZE = 10 * 1024 * 1024      # 单个 .hmp 上限 10MB
MAX_UPLOAD_DL = 50 * 1024 * 1024     # 从聊天下载单文件上限 50MB

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _root() -> Path:
    # 本文件位于 modules/ 下，向上两层即项目根（同 commands.py 等约定）
    return Path(__file__).resolve().parent.parent


def _plugins_root() -> Path:
    p = _root() / "plugins"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _down_dir() -> Path:
    d = _plugins_root() / DOWN_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_name(name: str) -> bool:
    """插件 manifest 名必须为字母/数字/_/-，且不以 _ 开头（避免掩盖内部目录）。"""
    return bool(_NAME_RE.match(name or "")) and not name.startswith("_")


def _sanitize_member(member: str) -> Optional[str]:
    """防 zip-slip：拒绝绝对路径、.. 、空段。"""
    m = (member or "").replace("\\", "/")
    if m.startswith("/"):
        return None
    parts = m.split("/")
    if any(p in ("", "..") for p in parts):
        return None
    return m


# ── 打包 ───────────────────────────────────────────────
def pack_plugin(name: str) -> tuple[bool, str]:
    """把 plugins/<name>/ 打成 plugins/_down/<manifest.name>.hmp（ZIP_STORED）。"""
    src = _plugins_root() / name
    if not src.is_dir():
        return False, f"插件目录不存在: {name}"
    mf = src / "manifest.json"
    if not mf.is_file():
        return False, "缺少 manifest.json"

    try:
        manifest = json_load(mf)
    except Exception as e:
        return False, f"manifest 解析失败: {e}"
    pname = (manifest.get("name") or "").strip()
    if pname and not validate_name(pname):
        return False, f"manifest.name 非法: {pname}"
    pname = pname or name

    out = _down_dir() / f"{pname}{HMP_EXT}"
    n = 0
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(src.rglob("*")):
                rel = f.relative_to(src).as_posix()
                if "__pycache__" in rel:
                    continue
                if f.is_file():
                    z.writestr(rel, f.read_bytes())
                    n += 1
    except Exception as e:
        return False, f"打包失败: {e}"
    return True, f"已打包 {pname}{HMP_EXT}（{out.stat().st_size} 字节，{n} 个文件）"


# ── 解包 ───────────────────────────────────────────────
def peek_hmp_name(hmp_path: Path) -> Optional[str]:
    """只读 .hmp 内的 manifest.name，不落盘（用于加载前定位插件名）。"""
    try:
        with zipfile.ZipFile(hmp_path) as z:
            mf_members = [m for m in z.namelist()
                          if _sanitize_member(m) and Path(m).name == "manifest.json"]
            if not mf_members:
                return None
            manifest = json_load(io.BytesIO(z.read(mf_members[0])))
            name = (manifest.get("name") or "").strip()
            return name if validate_name(name) else None
    except Exception:
        return None


def unpack_hmp(hmp_path: Path) -> tuple[bool, str]:
    """解包 .hmp 到 plugins/<manifest.name>/（扁平化，防 zip-slip）。"""
    if not hmp_path.is_file():
        return False, f"文件不存在: {hmp_path}"
    if hmp_path.suffix.lower() != HMP_EXT:
        return False, "不是 .hmp 插件包"

    target: Optional[Path] = None
    size_in = 0
    try:
        size_in = hmp_path.stat().st_size
        if size_in > MAX_ZIP_SIZE:
            return False, f"包过大（>{MAX_ZIP_SIZE//1024//1024}MB），拒绝解包"
        with zipfile.ZipFile(hmp_path) as z:
            all_members = z.namelist()
            if len(all_members) > 2000:
                return False, "包内文件过多，拒绝解包"
            # 定位真实 manifest（去掉插件目录前缀后的 manifest.json 或根 manifest.json）
            mf_members = [m for m in all_members
                          if _sanitize_member(m) and Path(m).name == "manifest.json"]
            if not mf_members:
                return False, "包内没有 manifest.json"
            mf_member = mf_members[0]
            try:
                manifest = json_load(io.BytesIO(z.read(mf_member)))
            except Exception as e:
                return False, f"manifest 解析失败: {e}"
            pname = (manifest.get("name") or "").strip()
            if not validate_name(pname):
                return False, f"manifest.name 非法或缺失: {pname!r}"

            target = _plugins_root() / pname
            if target.exists():
                return False, f"插件已存在: {pname}（可先 .plugin unload {pname} 或用 [DISABLE] 前缀停用）"

            # 只保留文件名段，扁平化释放，防目录穿越
            for m in all_members:
                safe = _sanitize_member(m)
                if not safe:
                    continue
                dest = target / Path(safe).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(m))
    except Exception as e:
        return False, f"解包失败: {e}"

    return True, f"已解包插件 {pname} → plugins/{pname}/"


# ── 从聊天提取 .hmp URL ────────────────────────────────
def extract_hmp_url(raw_event) -> Optional[str]:
    """从 KOOK 消息（当前回复或引用消息）的 attachments / (file) 标记里找到一个 .hmp URL。"""
    urls: list[str] = []

    def _collect(src):
        if src is None:
            return
        for att_key in ("attachments", "images", "image_list"):
            atts = getattr(src, att_key, None) or []
            if not isinstance(atts, (list, tuple)):
                continue
            for a in atts:
                if isinstance(a, dict):
                    u = a.get("url") or a.get("name") or ""
                else:
                    u = getattr(a, "url", "") or getattr(a, "name", "")
                if u:
                    urls.append(str(u))
        content = getattr(src, "content", "") or ""
        for mm in re.finditer(r"\(file\)(\S+)", content):
            urls.append(mm.group(1))

    _collect(raw_event)
    _collect(getattr(raw_event, "quote", None) if raw_event is not None else None)

    for u in urls:
        if str(u).lower().endswith(HMP_EXT):
            return str(u)
    return None


def local_filename_for(url: str) -> str:
    """从 .hmp URL 推导 _down 里的本地文件名（与 download_hmp 落盘名一致）。"""
    import urllib.parse
    fname = urllib.parse.unquote(Path(urlparse_url(url).path).name)
    if not fname.lower().endswith(HMP_EXT):
        fname = (fname or "plugin") + HMP_EXT
    return Path(fname).name


# ── 下载到 _down ────────────────────────────────────────
def download_hmp(url: str) -> tuple[bool, str]:
    """下载聊天里的 .hmp 到 plugins/_down/ 并保存，返回本地路径。"""
    try:
        import httpx
    except Exception as e:
        return False, f"httpx 不可用: {e}"

    fname = local_filename_for(url)
    dest = _down_dir() / fname
    try:
        with httpx.stream("GET", url, timeout=30.0, follow_redirects=True, verify=False) as resp:
            resp.raise_for_status()
            total = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_UPLOAD_DL:
                        f.close()
                        dest.unlink(missing_ok=True)
                        return False, f"下载超限（>{MAX_UPLOAD_DL//1024//1024}MB）"
                    f.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        return False, f"下载失败: {e}"

    if dest.stat().st_size > MAX_ZIP_SIZE:
        dest.unlink(missing_ok=True)
        return False, f"文件过大（>{MAX_ZIP_SIZE//1024//1024}MB）"
    return True, f"已下载 {fname} → plugins/_down/{fname}"


def list_downloads() -> tuple[bool, list[str]]:
    """列出 _down 下所有 .hmp。"""
    files = sorted(p for p in _down_dir().glob("*" + HMP_EXT))
    return True, [p.name for p in files]


# ── 运行时加载已解包插件 ────────────────────────────────
async def load_local_plugin(name: str) -> tuple[bool, str]:
    """把已解包的插件通过 PluginManager load→init→enable 接入运行时。"""
    from core.plugin import get_plugin_manager
    mgr = get_plugin_manager()
    ok, err = await mgr.load(name)
    if not ok:
        return False, err
    ok, err = await mgr.init(name)
    if not ok:
        return False, err
    ok, err = await mgr.enable(name)
    if not ok:
        return False, err
    return True, f"插件 {name} 已加载并启用，可 .plugin status 确认"


# 小工具
def json_load(obj):
    """obj 可为 Path/str/bytes/类文件对象，统一解析为 dict。"""
    import json
    if isinstance(obj, Path):
        return json.loads(obj.read_text("utf-8"))
    if isinstance(obj, bytes):
        return json.loads(obj.decode("utf-8"))
    if hasattr(obj, "read"):
        return json.load(obj)
    if isinstance(obj, str):
        return json.loads(obj)
    return json.load(obj)


def urlparse_url(url: str):
    from urllib.parse import urlparse
    return urlparse(url)