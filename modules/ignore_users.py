"""
全群用户忽略管理（data/ignored_users.json）
"""

from __future__ import annotations
import json
from pathlib import Path

_FILE = Path(__file__).resolve().parent.parent / "data" / "ignored_users.json"


def _load() -> set[int]:
    """从文件加载忽略列表"""
    try:
        if _FILE.exists():
            return set(json.loads(_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save(data: set[int]):
    """保存忽略列表到文件"""
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(sorted(data), ensure_ascii=False), encoding="utf-8")


def is_ignored(user_id: int) -> bool:
    """检查用户是否被忽略"""
    return user_id in _load()


def add_ignored(user_id: int):
    """忽略一个用户"""
    s = _load()
    s.add(user_id)
    _save(s)


def remove_ignored(user_id: int) -> bool:
    """解除忽略，返回是否真的删掉了"""
    s = _load()
    if user_id in s:
        s.discard(user_id)
        _save(s)
        return True
    return False


def list_ignored() -> list[int]:
    return sorted(_load())
