"""
自动更新引擎 v2 — Git Patch 行级增量合并

流程:
  1. 取 GitHub diff → .patch 文本数组
  2. .bot_protect 优先合并（先更新保护规则）
  3. 逐文件解析 patch → hunks → 上下文匹配 → 行级替换
  4. 只改变化的行，不动任何本地未涉及的行
  5. 更新 state.json 追踪每个文件 blob SHA
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import httpx
from pathlib import Path
from urllib.parse import urlparse, quote

from core.logger import get_logger
from modules._auto_update.patcher import parse_patch, apply_hunks
from modules._auto_update.state import load_state, save_state, get_file_blob, set_file_blob

logger = get_logger("auto_update")

# 格式: "你的GitHub用户名/你的仓库名"，可用环境变量 GITHUB_REPO 覆盖（服务器无需改代码）
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Trusler258/huanmeng-kook-bot").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "master").strip()
# GitHub API 用 token 认证可将匿名配额 60 次/小时 提升到 5000 次/小时，避免 .update 频繁拉取被打 403 限流
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}" if GITHUB_REPO else ""
CACHE_DIR = ".update_cache"

# _get_head_sha 命中限流时的特殊返回标记
_RATE_LIMITED = "__RATE_LIMITED__"


def _gh_headers() -> dict:
    """构造 GitHub API 请求头：有 token 时带 Authorization，否则匿名。"""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _is_rate_limited(resp) -> bool:
    """判断是否命中 GitHub API 限流（403 rate limit exceeded / 429）。"""
    return resp is not None and getattr(resp, "status_code", 0) in (403, 429) and \
        "rate limit" in (getattr(resp, "text", "") or "").lower()

# 文件下载镜像
RAW_MIRRORS = [
    "https://raw.githubusercontent.com",
    "https://raw.gitmirror.com",
    "https://gh-proxy.com/raw.githubusercontent.com",
]


def _normalize_raw_url(raw_url: str) -> str:
    """对 raw_url 中的路径部分做 URL 编码，处理文件名的中文/空格等非 ASCII 字符。

    safe='/%' 保证已存在的百分号编码（如 GitHub API 返回的 %E5%B9%BB）不被二次编码，
    仅对空格、中文等未编码字符做转义，适配含中文/空格的文件名下载。
    """
    try:
        parsed = urlparse(raw_url)
        encoded_path = quote(parsed.path, safe="/%")
        return parsed._replace(path=encoded_path).geturl()
    except Exception:
        return raw_url


def _normalize_rel_path(rel_path: str) -> str:
    """对仓库内相对路径做 URL 编码，用于拼进 API/raw 的 URL 路径。"""
    try:
        return quote(rel_path, safe="/")
    except Exception:
        return rel_path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_protect_list() -> set[str]:
    path = _root() / ".bot_protect"
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("merge:"):
            entries.add(line.rstrip("/"))
    return entries


def _load_merge_list() -> set[str]:
    """读取 .bot_protect 中以 `merge:` 前缀标记的文件。

    语义：这些文件本地存在未推送改动，更新时不能直接覆盖（强制对齐会丢本地功能），
    也不能像普通保护一样跳过（会漏掉远程更新）→ 走 LLM 三路融合。
    每行格式：merge: <路径或glob>
    """
    path = _root() / ".bot_protect"
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("merge:"):
            p = line[len("merge:"):].strip().rstrip("/")
            if p:
                entries.add(p)
    return entries


def _is_protected(rel_path: str, protect: set[str]) -> bool:
    for p in protect:
        if fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(rel_path, p + "/*"):
            return True
        if rel_path.startswith(p + "/") or rel_path == p:
            return True
    return False


def _skip_prefix(rel_path: str) -> bool:
    """总是跳过的路径前缀"""
    # config/ 为部署配置，包含 token/密钥/角色等，禁止被自动更新覆盖
    # tests/ 为开发测试用例，生产环境无需同步
    # plugins/ 为插件目录，由运维/用户在服务器上自行管理，禁止被自动更新覆盖
    return rel_path.startswith(("logs/", ".git/", "data/", "__pycache__/", "config/", "tests/", "plugins/"))


async def _fetch_compare(base_sha: str, head_sha: str) -> list[dict] | None:
    """获取两个 commit 之间的 diff 文件列表（含 .patch 字段）"""
    if not base_sha:
        return None
    try:
        url = f"{GITHUB_API}/compare/{base_sha[:7]}...{head_sha[:7]}"
        headers = _gh_headers()
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("files", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.warning("Compare 基准 SHA 已失效（force-push），需全量同步")
        else:
            logger.warning("Compare API 失败: %s", e)
        return None
    except Exception as e:
        logger.warning("Compare API 失败: %s", e)
        return None


async def _fetch_commit_log(base_sha: str, head_sha: str) -> str:
    """获取两个 commit 之间的提交日志（用于更新报告）"""
    if not base_sha:
        return ""
    try:
        url = f"{GITHUB_API}/compare/{base_sha[:7]}...{head_sha[:7]}"
        headers = _gh_headers()
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            commits = data.get("commits", [])
            if not commits:
                return ""
            lines = ["\n更新日志:"]
            for c in commits:
                sha = c.get("sha", "")[:7]
                msg = c.get("commit", {}).get("message", "").split("\n")[0][:60]
                lines.append(f"  {sha}  {msg}")
            return "\n".join(lines)
    except Exception:
        return ""


async def _get_head_sha() -> str | None:
    """获取远程 HEAD commit SHA；命中 GitHub API 限流时返回特殊标记。"""
    try:
        url = f"{GITHUB_API}/commits/{GITHUB_BRANCH}"
        headers = _gh_headers()
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers=headers)
            if _is_rate_limited(resp):
                logger.warning("GitHub API 被限流(403/429)，请配置 GITHUB_TOKEN 或等配额恢复")
                return _RATE_LIMITED
            resp.raise_for_status()
            return resp.json().get("sha", "")
    except Exception as e:
        logger.warning("获取 HEAD 失败: %s", e)
        return None


async def _fetch_all_files(head_sha: str) -> list[dict] | None:
    """获取某个 commit 的完整文件列表（用于首次/force-push 全量同步）"""
    try:
        url = f"{GITHUB_API}/commits/{head_sha}"
        headers = _gh_headers()
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            tree_url = data.get("commit", {}).get("tree", {}).get("url", "")
            if not tree_url:
                return None
            # 递归获取所有文件
            tree_resp = await client.get(tree_url + "?recursive=1", headers=headers)
            tree_resp.raise_for_status()
            tree_data = tree_resp.json()
            files = []
            for item in tree_data.get("tree", []):
                if item["type"] == "blob":
                    rel = item["path"]
                    file_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{_normalize_rel_path(rel)}"
                    files.append({
                        "filename": rel,
                        "status": "added",
                        "raw_url": file_url,
                    })
            return files
    except Exception as e:
        logger.warning("获取完整文件列表失败: %s", e)
        return None


async def _get_blob_sha(rel_path: str, commit_sha: str) -> str:
    """获取某个文件在指定 commit 中的 blob SHA"""
    try:
        url = f"{GITHUB_API}/contents/{_normalize_rel_path(rel_path)}?ref={commit_sha}"
        headers = _gh_headers()
        async with httpx.AsyncClient(timeout=5, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("sha", "")
    except Exception:
        return ""


def compute_local_blob(root: Path, rel_path: str) -> str:
    """计算本地文件的 git blob SHA（纯本地，不依赖网络）。

    blob = SHA1("blob <字节数>\\0<内容>")，与 GitHub 仓库逐字节一致时
    与远端 _get_blob_sha 返回值相等。用于检测本地文件是否被外部改动
    （如 scp 覆盖 / 手动编辑），从而决定走 patch 还是全量对齐。
    """
    import hashlib
    fpath = root / rel_path
    if not fpath.exists():
        return ""
    try:
        data = fpath.read_bytes()
        return hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\x00" + data
        ).hexdigest()
    except Exception:
        return ""


def _read_local(root: Path, rel_path: str) -> list[str]:
    """读取本地文件，返回行列表 (保留换行符)"""
    fpath = root / rel_path
    if not fpath.exists():
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return f.readlines()
    except Exception:
        return []


def _write_local(root: Path, rel_path: str, lines: list[str]):
    """写入本地文件"""
    fpath = root / rel_path
    fpath.parent.mkdir(parents=True, exist_ok=True)
    # 备份
    bak = fpath.with_suffix(fpath.suffix + ".bak")
    if fpath.exists():
        try:
            bak.write_bytes(fpath.read_bytes())
        except Exception:
            pass
    fpath.write_text("".join(lines), encoding="utf-8")


async def check_and_update(check_only: bool = False, force: bool = False) -> str:
    root = _root()
    state = load_state(root)

    # 1. 获取 HEAD SHA
    head = await _get_head_sha()
    if head == _RATE_LIMITED:
        return "GitHub API 被限流(403/429)：匿名配额 60 次/小时已用尽。请配置 GITHUB_TOKEN 或 1 小时后重试"
    if not head:
        return "无法连接 GitHub，请检查网络"

    # 2. 比对
    stored = state.get("remote_sha", "")
    if not force and stored == head:
        return "已是最新"

    # 3. 获取 diff（404 时 base SHA 已失效，清空后自动全量重试）
    base = stored if stored and not force else ""
    files = await _fetch_compare(base, head)
    if not files:
        if stored:
            logger.info("△ 旧基线 SHA 失效，清除后全量获取最新文件")
            state["remote_sha"] = head
            state.pop("files", None)
            save_state(root, state)
            # 递归重试：空 base → 获取完整文件树
            files = await _fetch_all_files(head)
            if not files:
                return "无法获取完整文件列表，请检查网络"
        else:
            files = await _fetch_all_files(head) if not files else files
            if not files:
                state["remote_sha"] = head
                save_state(root, state)
                return "无法获取完整文件列表，请稍后再试"

    # 3.5 获取更新日志
    commit_log = await _fetch_commit_log(base, head) if base else ""

    # 4. 处理 .bot_protect（优先合并）
    _merge_bot_protect_priority(files, root, head, state)

    # 5. 读取保护列表（此时已是最新）
    protect = _load_protect_list()

    # 6. 逐文件 patch 合并
    ok = 0
    skip = 0
    updated_files: list[str] = []

    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel):
            continue

        patch_text = item.get("patch", "")
        status = item.get("status", "")

        # 删除
        if status == "removed":
            local = root / rel
            if local.exists() and not _is_protected(rel, protect):
                if not check_only:
                    local.unlink(missing_ok=True)
                    updated_files.append(f"[删除] {rel}")
            continue

        # 跳过保护文件
        if _is_protected(rel, protect):
            continue

        # 无 patch → 全量下载（首次/force-push 场景）
        if not patch_text:
            raw_url = _normalize_raw_url(item.get("raw_url", ""))
            if raw_url and not check_only:
                try:
                    async with httpx.AsyncClient(timeout=15, verify=False) as dl:
                        dl_resp = await dl.get(raw_url)
                        dl_resp.raise_for_status()
                    _write_local(root, rel, dl_resp.text)
                    try:
                        blob = await _get_blob_sha(rel, head)
                        set_file_blob(state, rel, blob, 1, 0)
                    except Exception:
                        pass
                    updated_files.append(rel)
                    ok += 1
                except Exception as e:
                    logger.warning("下载 %s 失败: %s", rel, e)
            elif raw_url:
                updated_files.append(f"[新增] {rel}")
            continue

        # 显示模式
        if check_only:
            updated_files.append(f"[待更新] {rel}")
            continue

        # 解析 + 合并
        hunks = parse_patch(patch_text)
        if not hunks:
            # 无 hunk → 可能是二进制/重命名，跳过
            continue

        local_lines = _read_local(root, rel)
        merged, aok, sk = apply_hunks(local_lines, hunks)

        # 该文件有 patch 但一个 hunk 都没落地（上下文全对不上，即
        # "hash/内容与远程基准脱节"）→ 行级合并无法完成。
        # 自动回退全量下载 raw_url 强行对齐 HEAD，并重建 blob，避免
        # "永久跳过 + 漏更新"。保护文件 (.bot_protect) 不受此影响。
        if aok == 0 and sk > 0:
            raw_url = _normalize_raw_url(item.get("raw_url", ""))
            if raw_url:
                try:
                    async with httpx.AsyncClient(timeout=15, verify=False) as dl:
                        dl_resp = await dl.get(raw_url)
                        dl_resp.raise_for_status()
                    _write_local(root, rel, dl_resp.text)
                    try:
                        blob = await _get_blob_sha(rel, head)
                        set_file_blob(state, rel, blob, 1, 0)
                    except Exception:
                        pass
                    updated_files.append(f"[强制对齐] {rel}")
                    ok += 1
                    logger.warning("文件 %s patch 全部跳过，已回退全量下载对齐 HEAD", rel)
                    continue
                except Exception as e:
                    logger.warning("文件 %s patch 全跳过且下载失败: %s", rel, e)
            ok += aok
            skip += sk
            continue

        if aok > 0:
            _write_local(root, rel, merged)
            blob = await _get_blob_sha(rel, head)
            set_file_blob(state, rel, blob, aok, sk)
            updated_files.append(rel)
        ok += aok
        skip += sk

    # 7. 保存状态：严格事务——仅当没有任何 hunk 跳过时才推进 remote_sha
    #    （GitHub HEAD 已完整落地）；存在跳过说明本地与远程未对齐，
    #    保留旧 remote_sha，下次 .update 仍会重试，杜绝永久漏更新。
    if skip == 0:
        state["remote_sha"] = head
    else:
        logger.warning("存在 %d 个跳过 hunk，本轮不推进 remote_sha，保留 %s",
                       skip, state.get("remote_sha"))
    save_state(root, state)

    # 8. 返回结果
    parts = []
    if updated_files:
        if check_only:
            parts.append(f"待更新 {len(updated_files)} 个文件:")
            parts.extend(f"  {f}" for f in updated_files)
        else:
            parts.append(f"已更新 {len(updated_files)} 个文件 "
                         f"({ok} hunks 成功, {skip} hunks 跳过)")
    if not updated_files:
        parts.append("已是最新")
    if commit_log:
        parts.append(commit_log)
    return "\n".join(parts)


def _merge_bot_protect_priority(
    files: list[dict], root: Path, head: str, state: dict
):
    """优先合并 .bot_protect 文件，确保后续文件应用最新保护规则"""
    for item in files:
        if item.get("filename") == ".bot_protect":
            patch_text = item.get("patch", "")
            if not patch_text:
                continue
            hunks = parse_patch(patch_text)
            if not hunks:
                continue
            local_lines = _read_local(root, ".bot_protect")
            merged, _, _ = apply_hunks(local_lines, hunks)
            _write_local(root, ".bot_protect", merged)
            break
