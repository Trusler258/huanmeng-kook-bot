"""
Phase 16 代码级更新：Snapshot + Rollback

在任何"生产应用"之前，先把将要改动的文件快照到 .update_cache/snapshots/<remote_sha>/。
若测试失败 / 启动失败 / Health Check 失败，自动从快照恢复。

关键约束：
- 快照只备份本次更新实际会改动（存在）的本地文件，不整仓复制。
- 恢复是"逐文件回滚"，只覆盖本次改过的文件，不动其他文件。
- 全量下载（新增文件）也纳入快照：新增文件在回滚时删除。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("auto_update.snapshot")

# 需要快照的常备目录（避免误删用户数据/日志）
_SKIP = ("logs/", ".git/", "data/", "__pycache__/", ".update_cache/")


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def snapshot_dir(remote_sha: str) -> Path:
    return _root() / ".update_cache" / "snapshots" / (remote_sha[:12] or "unknown")


def _track_file(rel: str, changes: dict, snapshot: Path, scope: str) -> None:
    """记录一个文件到 changes 清单（scope: modify/delete/add）。"""
    changes.setdefault("files", {})[rel] = scope


def create_snapshot(files: list[dict], remote_sha: str) -> Optional[Path]:
    """
    创建快照：备份本次会改动的本地文件。
    返回快照目录；本地无文件可备份时仍在清单里记录（用于 delete/add 回滚）。
    """
    root = _root()
    snap = snapshot_dir(remote_sha)
    changes: dict = {"remote_sha": remote_sha, "files": {}}
    backed = 0

    for item in files:
        rel = item.get("filename", "")
        if not rel or rel.startswith(_SKIP):
            continue
        status = item.get("status", "")
        local = root / rel
        if status == "removed":
            # 删除：快照记录原文件存在性，回滚时恢复
            if local.exists():
                _backup_file(local, snap, rel)
                backed += 1
            _track_file(rel, changes, snap, "delete")
        elif local.exists():
            # 修改：备份当前内容
            _backup_file(local, snap, rel)
            backed += 1
            _track_file(rel, changes, snap, "modify")
        else:
            # 新增：本地不存在，回滚时删除
            _track_file(rel, changes, snap, "add")

    # 写清单
    try:
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "changes.json").write_text(
            _json(changes), encoding="utf-8")
    except OSError as e:
        logger.warning("快照清单写入失败: %s", e)
        return None

    logger.info("创建快照 %s: 备份 %d 个文件", snap.name, backed)
    return snap


def _backup_file(local: Path, snap: Path, rel: str) -> None:
    """把本地文件复制到快照目录（保留相对路径）。"""
    dest = snap / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dest)
    except OSError as e:
        logger.warning("备份 %s 失败: %s", rel, e)


def rollback(snap: Optional[Path], reason: str = "") -> int:
    """
    从快照回滚本次更新。返回恢复的文件数。
    规则：
    - modify：用快照副本覆盖本地。
    - delete：回滚时本地文件已删除，从快照恢复。
    - add   ：本地为新增文件，回滚时删除。
    """
    if not snap or not snap.exists():
        logger.warning("无可回滚快照，跳过回滚")
        return 0
    root = _root()
    changes_path = snap / "changes.json"
    if not changes_path.exists():
        return 0
    try:
        import json
        changes = json.loads(changes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    restored = 0
    for rel, scope in changes.get("files", {}).items():
        local = root / rel
        snap_file = snap / rel
        try:
            if scope in ("modify", "delete"):
                if snap_file.exists():
                    local.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap_file, local)
                    restored += 1
            elif scope == "add":
                if local.exists():
                    local.unlink()
                    restored += 1
        except OSError as e:
            logger.warning("回滚 %s 失败: %s", rel, e)

    logger.warning("已回滚 %d 个文件 (%s)", restored, reason or "Health Check 失败")
    return restored


def _json(obj) -> str:
    import json
    return json.dumps(obj, indent=2, ensure_ascii=False)