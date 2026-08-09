"""
短时记忆模块
- JSON 缓存最近 N 条消息（跨重启保留）
- 自动触发时滚动窗口，溢出写入长时记忆
- 支持按关键词检索
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("stm")

STM_DIR = Path(__file__).resolve().parent.parent / "data" / "stm"
STM_DIR.mkdir(parents=True, exist_ok=True)
MAX_ENTRIES = 30  # 每个会话保留最近 30 条


def _file(chat_id: int) -> Path:
    return STM_DIR / f"stm_{chat_id}.json"


def add_entry(chat_id: int, tag: str, content: str, author: str = ""):
    """添加一条短时记忆"""
    path = _file(chat_id)
    entries = _read(path)
    entries.append({
        "time": time.time(),
        "tag": tag,
        "author": author,
        "content": content[:500],
    })
    # 滚动窗口
    if len(entries) > MAX_ENTRIES:
        overflow = entries[:-MAX_ENTRIES]
        entries = entries[-MAX_ENTRIES:]
        # 溢出的交给长时记忆
        from modules.memory import merge_overflow_memory
        merge_overflow_memory(chat_id, overflow)
    _write(path, entries)


def get_recent(chat_id: int, count: int = 10) -> list[dict]:
    """获取最近 N 条短时记忆"""
    entries = _read(_file(chat_id))
    return entries[-count:]


def search(chat_id: int, keyword: str, limit: int = 5) -> list[dict]:
    """按关键词搜索短时记忆"""
    entries = _read(_file(chat_id))
    kw = keyword.lower()
    matches = [e for e in entries if kw in e["content"].lower() or kw in e.get("tag", "").lower()]
    return matches[-limit:]


def summarize(chat_id: int) -> str:
    """生成短时记忆摘要"""
    entries = _read(_file(chat_id))
    if not entries:
        return "暂无短时记忆"
    recent = entries[-10:]
    lines = [f"【短时记忆 · 共{len(entries)}条 · 显示最近{len(recent)}条】"]
    for e in recent:
        t = time.strftime("%H:%M", time.localtime(e["time"]))
        lines.append(f"  [{t}] [{e['tag']}] {e['author']}: {e['content'][:80]}")
    return "\n".join(lines)


def clear(chat_id: int) -> str:
    """清空短时记忆"""
    path = _file(chat_id)
    if path.exists():
        path.unlink()
        return "短时记忆已清空"
    return "没有短时记忆"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _write(path: Path, entries: list[dict]):
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
