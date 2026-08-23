"""
批量 LLM 三路融合脚本 — 用于版本严重落后、本地改动多的 bot 实例全量修复。

用法：在 bot 根目录下运行
    python3 scripts/batch_llm_merge.py

功能：
  1. 读取 data/update_state.json 获取 BASE（上次对齐的 remote_sha）
  2. 拿 GitHub API 获取最新 master HEAD（REMOTE）
  3. 遍历仓库所有 .py 文件，对每个文件：
     - LOCAL == REMOTE → 跳过
     - LOCAL == BASE  → 直接覆盖为 REMOTE（无本地改动）
     - LOCAL != BASE  → 三路 LLM 融合（保留本地改动 + 应用远程更新）
  4. 跳过 >60KB 文件、受保护文件、plugins/ 目录
  5. 完成后重建 update_state.json 对齐最新 commit

依赖：httpx, toml（服务器通常已有）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import httpx

# ── 配置 ───────────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Trusler258/huanmeng-kook-bot").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
ROOT = Path.cwd()

# 跳过前缀（不处理这些目录/文件）
SKIP_PREFIXES = (
    "plugins/", "data/", "config/", "server_config_backup/",
    ".update_cache/", "__pycache__/", ".git/", "scripts/",
    "venv/", ".venv/", "env/", ".env", ".gitignore",
    ".bot_protect", "requirements.txt", "phone_tunnel_url.txt",
)

# 最大文件大小（超过不送 LLM）
MAX_FILE_SIZE = 60000


def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


async def _gh_get(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30, verify=False) as cl:
        resp = await cl.get(url, headers=_gh_headers())
        resp.raise_for_status()
        return resp.json()


async def get_head_sha() -> str:
    data = await _gh_get(f"{GITHUB_API}/commits/master")
    return data["sha"]


async def get_file_tree(sha: str) -> list[dict]:
    """获取仓库在指定 commit 的完整文件树（递归）"""
    data = await _gh_get(f"{GITHUB_API}/git/trees/{sha}?recursive=1")
    return data.get("tree", [])


async def fetch_file_content(rel_path: str, ref: str) -> str:
    """通过 GitHub API 获取某个文件在指定 ref 下的内容（base64 解码）"""
    url = f"{GITHUB_API}/contents/{rel_path}?ref={ref}"
    try:
        data = await _gh_get(url)
        return base64.b64decode(data.get("content", "") or "").decode("utf-8", errors="replace")
    except Exception:
        return ""


def load_update_state() -> dict:
    path = ROOT / "data" / "update_state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"remote_sha": "", "files": {}}


def save_update_state(state: dict) -> None:
    path = ROOT / "data" / "update_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_local(rel_path: str) -> str:
    p = ROOT / rel_path
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_local(rel_path: str, content: str) -> None:
    p = ROOT / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def compute_blob(rel_path: str) -> str:
    """计算本地文件的 git blob SHA"""
    p = ROOT / rel_path
    if not p.exists():
        return ""
    data = p.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()


def should_skip(rel_path: str) -> bool:
    if not rel_path.endswith(".py"):
        return True
    return any(rel_path.startswith(p) for p in SKIP_PREFIXES)


def strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = t[first_nl + 1:] if first_nl != -1 else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.rstrip() + "\n"


def load_protect_list() -> set[str]:
    """读取 .bot_protect 中的保护文件列表"""
    path = ROOT / ".bot_protect"
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def load_llm_config() -> tuple[str, str, str]:
    """从 config/bot_config.toml 和 .env 读取 LLM 配置"""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    cfg_path = ROOT / "config" / "bot_config.toml"
    if not cfg_path.exists():
        raise FileNotFoundError("config/bot_config.toml 不存在")

    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    model_cfg = cfg.get("models", {}).get("replyer_1", {})
    provider = model_cfg.get("provider", "")

    # 从 .env 读 API key 和 URL
    env = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")

    api_url = env.get(f"{provider.upper()}_URL", "")
    api_key = env.get(f"{provider.upper()}_KEY", "")
    model_name = model_cfg.get("model", "")

    if not api_url or not api_key:
        raise RuntimeError(f"未找到 LLM 配置: provider={provider}, URL={api_url}, KEY={bool(api_key)}")

    return api_url, api_key, model_name


async def call_llm(api_url: str, api_key: str, model: str, prompt: str) -> str:
    """调用 OpenAI 兼容 API"""
    async with httpx.AsyncClient(timeout=180, verify=False) as cl:
        resp = await cl.post(
            f"{api_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def merge_one(
    rel_path: str,
    base_text: str,
    local_text: str,
    head_text: str,
    api_url: str,
    api_key: str,
    model: str,
) -> Optional[str]:
    """LLM 三路融合一个文件，返回融合后内容；失败返回 None"""
    prompt = (
        "你是资深 Python 工程师。下面给出同一个文件的三个版本：\n\n"
        "# [BASE] 旧基线版本（上次更新时的状态）：\n"
        "```python\n" + base_text + "\n```\n\n"
        "# [LOCAL] 服务器本地当前版本（相对 BASE 含有本地未推送的改动，必须保留其功能意图）：\n"
        "```python\n" + local_text + "\n```\n\n"
        "# [REMOTE] 远程最新版本（相对 BASE 含有别人已推送的更新，必须完整应用）：\n"
        "```python\n" + head_text + "\n```\n\n"
        "请融合这三个版本：\n"
        "1. 完整保留 LOCAL 相对 BASE 的所有本地改动（这是本机独有功能，不能丢）；\n"
        "2. 完整应用 REMOTE 相对 BASE 的所有远程更新（不能遗漏）；\n"
        "3. 两者冲突时，若远程改动是结构性的（改名/重构）则跟随 REMOTE 并保留本地功能意图。\n"
        "输出完整的融合后文件内容：纯代码，不要任何解释，不要 markdown 代码围栏，不要省略号。"
    )

    try:
        raw = await call_llm(api_url, api_key, model, prompt)
        if not raw or len(raw) < 50:
            return None
        out = strip_code_fence(raw)
        compile(out, rel_path, "exec")  # 语法校验
        return out
    except SyntaxError as e:
        print(f"  [LLM] 语法错误 {rel_path}:{e.lineno} {e.msg}")
        return None
    except Exception as e:
        print(f"  [LLM] 调用失败 {rel_path}: {e}")
        return None


async def main():
    print("=" * 60)
    print("  批量 LLM 三路融合")
    print("=" * 60)

    # 1. 读配置
    print("\n[1/5] 读取 LLM 配置...")
    try:
        api_url, api_key, model = load_llm_config()
        print(f"  Provider URL: {api_url}")
        print(f"  Model: {model}")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # 2. 获取远程版本
    print("\n[2/5] 获取远程版本...")
    state = load_update_state()
    base_sha = state.get("remote_sha", "")
    print(f"  BASE (上次对齐): {base_sha[:8] if base_sha else '(无/首次)'}")

    head = await get_head_sha()
    print(f"  HEAD (最新):     {head[:8]}")

    if base_sha == head:
        print("  已是最新，无需修复。")
        return

    # 3. 获取文件树
    print("\n[3/5] 获取远程文件树...")
    tree = await get_file_tree(head)
    py_files = [
        item for item in tree
        if item["type"] == "blob" and item["path"].endswith(".py")
        and not should_skip(item["path"])
    ]
    print(f"  远程 .py 文件: {len(py_files)} 个（排除 plugins/ 等）")

    # 保护列表
    protect = load_protect_list()
    actionable = [f for f in py_files if f["path"] not in protect]
    if len(actionable) < len(py_files):
        print(f"  受保护排除: {len(py_files) - len(actionable)} 个")

    # 报告用户
    print(f"\n  将处理 {len(actionable)} 个文件。")
    if base_sha:
        input("  按 Enter 开始，Ctrl+C 取消...")
    else:
        print("  首次运行（无 BASE），所有文件将直接覆盖为远程最新。")
        input("  按 Enter 开始...")

    # 4. 逐文件处理
    print("\n[4/5] 逐文件处理...")
    ok = 0
    skip = 0
    merged = 0
    failed = 0
    total = len(actionable)

    for i, item in enumerate(actionable, 1):
        rel = item["path"]
        local_text = read_local(rel)

        if not local_text:
            # 本地缺失 → 直接下载
            try:
                head_text = await fetch_file_content(rel, head)
                if head_text:
                    write_local(rel, head_text)
                    ok += 1
                    print(f"  [{i}/{total}] DOWNLOAD {rel}")
                else:
                    failed += 1
                    print(f"  [{i}/{total}] FAIL {rel} (远程也不存在)")
            except Exception as e:
                failed += 1
                print(f"  [{i}/{total}] FAIL {rel}: {e}")
            continue

        # 获取远程最新
        try:
            head_text = await fetch_file_content(rel, head)
        except Exception as e:
            print(f"  [{i}/{total}] SKIP {rel} (获取远程失败: {e})")
            skip += 1
            continue

        if not head_text:
            print(f"  [{i}/{total}] SKIP {rel} (远程无此文件)")
            skip += 1
            continue

        # LOCAL == REMOTE → 跳过
        if local_text == head_text:
            skip += 1
            if i % 10 == 0:
                print(f"  [{i}/{total}] SKIP {rel} (已最新)")
            continue

        # 无 BASE → 首次运行，直接覆盖
        if not base_sha:
            write_local(rel, head_text)
            ok += 1
            print(f"  [{i}/{total}] OVERWRITE {rel}")
            continue

        # 获取 BASE 版本
        try:
            base_text = await fetch_file_content(rel, base_sha)
        except Exception:
            base_text = ""

        # LOCAL == BASE → 无本地改动，直接覆盖
        if local_text == base_text:
            write_local(rel, head_text)
            ok += 1
            print(f"  [{i}/{total}] OVERWRITE {rel} (无本地改动)")
            continue

        # 大小检查
        if len(local_text) > MAX_FILE_SIZE or len(head_text) > MAX_FILE_SIZE:
            print(f"  [{i}/{total}] SKIP {rel} (>60KB，保留本地)")
            skip += 1
            continue

        # LLM 三路融合
        print(f"  [{i}/{total}] LLM_MERGE {rel} (base={len(base_text)} local={len(local_text)} head={len(head_text)})...")
        result = await merge_one(rel, base_text, local_text, head_text, api_url, api_key, model)
        if result:
            write_local(rel, result)
            merged += 1
            print(f"  [{i}/{total}]   OK  {rel}")
        else:
            failed += 1
            print(f"  [{i}/{total}]   FAIL {rel} (保留本地)")

        # 小延迟避免 API 限流
        await asyncio.sleep(0.3)

    # 5. 重建 update_state
    print("\n[5/5] 重建 update_state...")
    state["remote_sha"] = head
    state["files"] = {}
    # 重新计算所有已处理文件的 blob
    for item in actionable:
        rel = item["path"]
        if not should_skip(rel) and (ROOT / rel).exists():
            blob = compute_blob(rel)
            if blob:
                state["files"][rel] = {"sha": blob, "ok": 1, "skip": 0}
    save_update_state(state)
    print(f"  remote_sha = {head}")

    # 报告
    print("\n" + "=" * 60)
    print(f"  完成！")
    print(f"  直接覆盖: {ok}")
    print(f"  LLM 融合: {merged}")
    print(f"  跳过:     {skip}")
    print(f"  失败:     {failed}")
    print(f"  总计:     {total}")
    print("=" * 60)

    if failed:
        print(f"\n  {failed} 个文件融合失败，本地改动已保留，可手动处理。")
    else:
        print("\n  全部成功！systemctl restart kook-bot.service 后生效。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n已取消。")
    except Exception as e:
        print(f"\n\n错误: {e}")
        traceback.print_exc()
        sys.exit(1)