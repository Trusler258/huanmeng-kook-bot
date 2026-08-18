# -*- coding: utf-8 -*-
"""
插件分享工具：.hmp 打包 / 解包 / 从聊天下载 / 运行时加载
- .hmp = 仅储存(zip STORED)的插件压缩包，内含 manifest.json 等
- 本地下载目录统一放在 plugins/_down/（discover 因无 manifest 自动跳过该目录）
"""
from __future__ import annotations

import io
import re
import shutil
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
def pack_plugin(name: str) -> tuple[bool, str, Optional[Path]]:
    """把 plugins/<name>/ 打成 plugins/_down/<manifest.name>.hmp（ZIP_STORED）。

    成功时第三位返回生成的 .hmp 绝对路径（供上层直接发送到聊天），失败为 None。
    """
    src = _plugins_root() / name
    if not src.is_dir():
        return False, f"插件目录不存在: {name}", None
    mf = src / "manifest.json"
    if not mf.is_file():
        return False, "缺少 manifest.json", None

    try:
        manifest = json_load(mf)
    except Exception as e:
        return False, f"manifest 解析失败: {e}", None
    pname = (manifest.get("name") or "").strip()
    if pname and not validate_name(pname):
        return False, f"manifest.name 非法: {pname}", None
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
        return False, f"打包失败: {e}", None
    return True, f"已打包 {pname}{HMP_EXT}（{out.stat().st_size} 字节，{n} 个文件）", out


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


def compare_versions(a: str, b: str) -> int:
    """比较两个版本号字符串（支持 1.2.3 / v1.2 / 1.2.3-beta 等）。
    返回：a>b → 1，a<b → -1，相等或无法解析 → 0。"""
    def _nums(v: str) -> list[int]:
        s = re.sub(r"^[vV]", "", (v or "").strip())
        return [int(x) for x in re.findall(r"\d+", s)] or [0]

    na, nb = _nums(a), _nums(b)
    for x, y in zip(na, nb):
        if x != y:
            return 1 if x > y else -1
    return 0 if len(na) == len(nb) else (1 if len(na) > len(nb) else -1)


def unpack_hmp(hmp_path: Path, overwrite: bool = False) -> tuple[bool, str, Optional[dict]]:
    """解包 .hmp 到 plugins/<manifest.name>/（扁平化，防 zip-slip）。

    - overwrite=False 且目标目录已存在时：不落盘，第三位返回冲突信息
      {"name", "local_version", "pkg_version"}，供上层做版本提示/覆盖确认；
    - overwrite=True：先删除旧目录再解包，实现覆盖安装。
    """
    if not hmp_path.is_file():
        return False, f"文件不存在: {hmp_path}", None
    if hmp_path.suffix.lower() != HMP_EXT:
        return False, "不是 .hmp 插件包", None

    target: Optional[Path] = None
    size_in = 0
    pname = ""
    try:
        size_in = hmp_path.stat().st_size
        if size_in > MAX_ZIP_SIZE:
            return False, f"包过大（>{MAX_ZIP_SIZE//1024//1024}MB），拒绝解包", None
        with zipfile.ZipFile(hmp_path) as z:
            all_members = z.namelist()
            if len(all_members) > 2000:
                return False, "包内文件过多，拒绝解包", None
            # 定位真实 manifest（去掉插件目录前缀后的 manifest.json 或根 manifest.json）
            mf_members = [m for m in all_members
                          if _sanitize_member(m) and Path(m).name == "manifest.json"]
            if not mf_members:
                return False, "包内没有 manifest.json", None
            mf_member = mf_members[0]
            try:
                manifest = json_load(io.BytesIO(z.read(mf_member)))
            except Exception as e:
                return False, f"manifest 解析失败: {e}", None
            pname = (manifest.get("name") or "").strip()
            if not validate_name(pname):
                return False, f"manifest.name 非法或缺失: {pname!r}", None
            pkg_version = str(manifest.get("version") or "0.0.0")

            target = _plugins_root() / pname
            if target.exists() and not overwrite:
                local_ver = "0.0.0"
                try:
                    local_ver = str(json_load(target / "manifest.json").get("version") or "0.0.0")
                except Exception:
                    pass
                conflict = {"name": pname,
                            "local_version": local_ver,
                            "pkg_version": pkg_version}
                return False, (f"插件已存在: {pname}（本地 v{local_ver}，包 v{pkg_version}）\n"
                               f"可用 .plugin unload 后重启，或按提示走覆盖安装"), conflict

            # 覆盖模式：先清掉旧目录，避免残留旧文件
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

            # 只保留文件名段，扁平化释放，防目录穿越
            for m in all_members:
                safe = _sanitize_member(m)
                if not safe:
                    continue
                dest = target / Path(safe).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(m))
    except Exception as e:
        return False, f"解包失败: {e}", None

    return True, f"已解包插件 {pname} → plugins/{pname}/", None


# ── 从聊天提取 .hmp URL ────────────────────────────────

# .hmp 可能以纯 URL 出现（含 query 参数），也可能以 KOOK (file) 标签出现
_HMP_URL_RE = re.compile(r"https?://[^\s\"<>]+?\.hmp(?:\?[^\s\"<>]*)?", re.I)
_FILE_TAG_RE = re.compile(r"\(file\)\s*([^\s\"<>]+)", re.I)


def is_hmp_url(text: str) -> bool:
    """判断给定文本是否一个 .hmp 直链（http/https 且以 .hmp 结尾，允许 query 参数）。

    供 `.plugin -import <url>` 识别用户直接粘贴的下载直链，与聊天附件/引用区分。
    """
    s = (text or "").strip()
    return bool(_HMP_URL_RE.fullmatch(s)) or (
        s.lower().startswith(("http://", "https://"))
        and s.lower().split("?", 1)[0].endswith(HMP_EXT)
    )


def _walk_strings(obj, depth: int = 0, out: Optional[list[str]] = None) -> list[str]:
    """递归收集对象内的所有字符串（去重保序），深度受限避免递归爆炸。

    兼容 dict / list / 常见对象字段（url/name/content/extra/file/files/
    attachments/images/image_list/quote/data），覆盖 KOOK 文件消息附件藏在
    extra 或 data 里的情况。
    """
    if out is None:
        out = []
    if depth > 5 or obj is None:
        return out
    if isinstance(obj, str):
        if obj not in out:
            out.append(obj)
        return out
    if isinstance(obj, (int, float, bool)):
        return out
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, depth + 1, out)
        return out
    if isinstance(obj, (list, tuple, set)):
        for i in obj:
            _walk_strings(i, depth + 1, out)
        return out
    # 其它对象：只探测常见字段，避免遍历整个对象图
    for f in ("url", "name", "content", "extra", "file", "files",
              "attachments", "images", "image_list", "quote", "data"):
        try:
            if not hasattr(obj, f):
                continue
            v = getattr(obj, f)
            if isinstance(v, str):
                if v not in out:
                    out.append(v)
            elif isinstance(v, (dict, list, tuple)):
                _walk_strings(v, depth + 1, out)
        except Exception:
            pass
    return out


def extract_hmp_url(raw_event) -> Optional[str]:
    """从 KOOK 消息（当前消息 / 引用消息）里递归找一个 .hmp 文件 URL。

    兼容：attachments/images 里的 url/name、正文里的 (file)URL、带 query 的裸
    路径以及藏在 extra/data 里的 URL。找不到返回 None。
    """
    if raw_event is None:
        return None
    candidates: list[str] = []
    _walk_strings(raw_event, out=candidates)
    _walk_strings(getattr(raw_event, "quote", None), out=candidates)

    # 1) 整串就是带协议、`.hmp` 结尾的 URL（最直接）；纯文件名会被跳过
    for c in candidates:
        s = str(c)
        if s.lower().startswith(("http://", "https://")) and s.lower().endswith(HMP_EXT):
            return s
    # 2) 从各字符串里再正则抠 `.hmp` URL 或 (file)URL
    for c in candidates:
        m = _HMP_URL_RE.search(c) or _FILE_TAG_RE.search(c)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    return None


async def fetch_hmp_url(raw_event) -> Optional[str]:
    """先走同步遍历；拿不到时用引用消息(id)调 khl MessageAPI.view 取回完整消息再抠 URL。

    KOOK 的文件消息 content 通常是文件 URL（走 img.kookapp.cn 通道），但引用消息
    (QuotedMessage) 里常不带上 content，需按 id 重新拉取。
    """
    url = extract_hmp_url(raw_event)
    if url:
        return url
    if raw_event is None:
        return None
    quote = getattr(raw_event, "quote", None)
    qid = getattr(quote, "id", None) if quote is not None else None
    if not qid:
        return None
    try:
        from khl import api
        from services.delivery import kook_transport
        bot = kook_transport._bot
        client = getattr(bot, "client", None) if bot is not None else None
        gate = getattr(client, "gate", None) if client is not None else None
        if gate is None:
            return None
        body = await gate.exec_req(api.Message.view(msg_id=qid))
        candidates: list[str] = []
        _walk_strings(body, out=candidates)
        for c in candidates:
            s = str(c)
            if s.lower().startswith(("http://", "https://")) and s.lower().endswith(HMP_EXT):
                return s
        for c in candidates:
            m = _HMP_URL_RE.search(c) or _FILE_TAG_RE.search(c)
            if m:
                return m.group(1) if m.lastindex else m.group(0)
    except Exception as e:
        logger.warning("fetch_hmp_url MessageAPI.view 失败: %s", e)
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
    # 刚解包/新增的插件目录还没登记进注册表，先重新扫描，再走 load→init→enable
    mgr.discover()
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