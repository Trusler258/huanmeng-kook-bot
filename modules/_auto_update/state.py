"""状态追踪: 记录每个文件当前对应的 GitHub blob SHA"""
from __future__ import annotations

import json
from pathlib import Path


STATE_FILE = "update_state.json"


def load_state(root: Path) -> dict:
    """读取状态文件，不存在则返回空结构"""
    path = root / "data" / STATE_FILE
    if not path.exists():
        return {"remote_sha": "", "files": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"remote_sha": "", "files": {}}


def save_state(root: Path, state: dict):
    """写入状态文件"""
    path = root / "data" / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_file_blob(state: dict, rel_path: str) -> str:
    """获取某文件记录的上次更新 blob SHA"""
    return state.get("files", {}).get(rel_path, {}).get("blob_sha", "")


def set_file_blob(state: dict, rel_path: str, blob_sha: str, apply_ok: int, skipped: int):
    """更新某文件的追踪状态"""
    state.setdefault("files", {})[rel_path] = {
        "blob_sha": blob_sha,
        "apply_ok": apply_ok,
        "skipped": skipped,
    }
