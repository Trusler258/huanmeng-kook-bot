"""
Legacy 数据源读取（Huanmeng 2.0 Phase 3）

将 Huanmeng 1.0.0 的存量数据定义为只读 Legacy Data Source：
  - data/memory_{chat_id}.md         Markdown 长时记忆
  - data/msglog/msglog_{chat_id}.jsonl  JSONL 消息归档

本模块只做"读取 + 规范化为迁移目标结构"，不写入、不删除旧文件。
迁移完成后新运行时默认只写 SQLite，JSONL/Markdown 暂时保留只读。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from core.logger import get_logger

logger = get_logger("db.legacy")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ── Legacy Message Store ────────────────────────────────────
class LegacyMessageStore:
    """读取 data/msglog/*.jsonl 的旧消息归档。"""

    def __init__(self, data_dir: Path | None = None):
        self._msglog_dir = (data_dir or _DATA_DIR) / "msglog"

    def list_files(self) -> list[Path]:
        if not self._msglog_dir.exists():
            return []
        return sorted(self._msglog_dir.glob("msglog_*.jsonl"))

    @staticmethod
    def parse_chat_id(path: Path) -> int:
        """从文件名 msglog_{chat_id}.jsonl 解析 chat_id。"""
        try:
            return int(path.stem.split("_", 1)[1])
        except (ValueError, IndexError):
            return 0

    def iter_entries(self, chat_id: Optional[int] = None) -> Iterator[dict]:
        """依次产出规范化后的消息 dict（供迁移）。"""
        files = self.list_files()
        if chat_id is not None:
            files = [f for f in files if self.parse_chat_id(f) == chat_id]
        for path in files:
            cid = self.parse_chat_id(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except Exception:
                            continue
                        yield self._normalize(raw, cid)
            except Exception as e:
                logger.warning("读取 legacy msglog 失败 %s: %s", path.name, e)

    @staticmethod
    def _normalize(raw: dict, chat_id: int) -> dict:
        """把旧 msglog 条目规范化为迁移目标结构。"""
        user_id = raw.get("user_id", 0)
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            user_id = 0
        role = "bot" if raw.get("type") == "bot" else "user"
        return {
            "conversation_id": chat_id,
            "user_id": user_id,
            "channel_id": "",
            "message_id": str(raw.get("msg_id", "")),
            "role": role,
            "content": raw.get("content", ""),
            "created_at": int(raw.get("time", 0)) * 1000,
            "metadata": {"legacy": True, "recalled": raw.get("recalled", False)},
            "trace_id": "",
        }


# ── Legacy Memory Store ─────────────────────────────────────
class LegacyMemoryStore:
    """读取 data/memory_{chat_id}.md 的旧 Markdown 记忆。"""

    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir or _DATA_DIR

    def list_files(self) -> list[Path]:
        return sorted(self._data_dir.glob("memory_*.md"))

    @staticmethod
    def parse_memory_id(path: Path) -> str:
        """从文件名 memory_{id}.md 解析 memory_id（可能是纯数字或 persona 后缀）。"""
        return path.stem.split("_", 1)[1]

    def iter_memories(self) -> Iterator[dict]:
        """产出规范化后的记忆 dict（供迁移）。"""
        import re as _re
        for path in self.list_files():
            memory_id = self.parse_memory_id(path)
            # chat_id 可能是数字，也可能含 persona 后缀；尽量转数字
            chat_id = 0
            try:
                chat_id = int(_re.search(r"\d+", memory_id).group())
            except Exception:
                pass
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        yield self._normalize(line, chat_id, memory_id)
            except Exception as e:
                logger.warning("读取 legacy memory 失败 %s: %s", path.name, e)

    @staticmethod
    def _normalize(line: str, chat_id: int, memory_id: str) -> dict:
        """把一行记忆规范化为迁移目标结构。"""
        return {
            "conversation_id": chat_id,
            "user_id": 0,
            "content": line,
            "memory_type": "fact",
            "importance": 0.5,
            "confidence": 1.0,
            "source": "legacy",
            "metadata": {"legacy": True, "memory_id": memory_id},
        }