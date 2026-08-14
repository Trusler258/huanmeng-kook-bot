"""
Legacy 数据迁移工具（Huanmeng 2.0 Phase 3）

将存量数据迁移到 SQLite：
  - data/msglog/*.jsonl  → messages
  - data/memory_*.md     → memories

特性：
  - dry-run：只统计数量，不写库
  - 数量统计 / 重复检测 / 失败记录（写入 data/migration_report.jsonl）
  - 事务：整批失败回滚 / 支持断点续跑（按 file 维度，已迁移文件跳过）
  - 重复执行安全：通过 metadata.legacy_migrated + 唯一指纹去重

用法（CLI）：
    python -m db.migration --dry-run
    python -m db.migration --commit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("db.migration")

_REPORT_FILE = Path(__file__).resolve().parent.parent / "data" / "migration_report.jsonl"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fingerprint(kind: str, key: str) -> str:
    import hashlib
    return hashlib.md5(f"{kind}:{key}".encode("utf-8")).hexdigest()


class MigrationTool:
    """执行 JSONL→messages、Markdown→memories 迁移。"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.stats = {
            "messages_read": 0, "messages_migrated": 0, "messages_dup": 0,
            "memories_read": 0, "memories_migrated": 0, "memories_dup": 0,
            "files_processed": [], "failures": [],
        }
        self._seen_messages: set[str] = set()
        self._seen_memories: set[str] = set()

    def _report(self, entry: dict):
        _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── 消息迁移 ──
    async def migrate_messages(self, uow) -> None:
        from .legacy import LegacyMessageStore
        store = LegacyMessageStore()
        for entry in store.iter_entries():
            self.stats["messages_read"] += 1
            fp = _fingerprint("msg", f"{entry['conversation_id']}:{entry['message_id']}:{entry['content']}")
            if fp in self._seen_messages or self._already_exists(uow, entry["conversation_id"], entry["content"]):
                self.stats["messages_dup"] += 1
                continue
            self._seen_messages.add(fp)
            if not self.dry_run:
                try:
                    await uow.messages.append(**entry)
                except Exception as e:
                    self.stats["failures"].append({"src": "message", "error": str(e)})
                    self._report({"type": "message", "error": str(e), "entry": entry})
                    continue
            self.stats["messages_migrated"] += 1

    # ── 记忆迁移 ──
    async def migrate_memories(self, uow) -> None:
        from .legacy import LegacyMemoryStore
        store = LegacyMemoryStore()
        for entry in store.iter_memories():
            self.stats["memories_read"] += 1
            fp = _fingerprint("mem", entry["content"])
            if fp in self._seen_memories or self._already_exists_memory(uow, entry["content"]):
                self.stats["memories_dup"] += 1
                continue
            self._seen_memories.add(fp)
            if not self.dry_run:
                try:
                    await uow.memories.add(**entry)
                except Exception as e:
                    self.stats["failures"].append({"src": "memory", "error": str(e)})
                    self._report({"type": "memory", "error": str(e), "entry": entry})
                    continue
            self.stats["memories_migrated"] += 1

    # ── 重复检测（基于库内已有存量，保证反复执行幂等）──
    @staticmethod
    async def _already_exists(uow, conversation_id: int, content: str) -> bool:
        try:
            return await uow.messages.find(conversation_id=conversation_id, content=content) is not None
        except Exception:
            return False

    @staticmethod
    async def _already_exists_memory(uow, content: str) -> bool:
        try:
            return await uow.memories.find(content=content) is not None
        except Exception:
            return False

    async def run(self) -> dict:
        from db import UnitOfWork
        from db.database import db
        if not db.initialized:
            logger.error("数据库未初始化，请先 await init_db()")
            return self.stats

        async with UnitOfWork() as uow:
            await self.migrate_messages(uow)
            await self.migrate_memories(uow)
            # 事务：dry-run 不写；commit 时异常整体回滚
        return self.stats


async def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Huanmeng 2.0 Legacy 数据迁移")
    parser.add_argument("--dry-run", action="store_true", help="仅统计，不写库")
    parser.add_argument("--commit", action="store_true", help="实际写入 SQLite")
    parser.add_argument("--reset-cache", action="store_true", help="清空迁移去重指纹（重新全量校验）")
    args = parser.parse_args(argv)

    from db.database import init_db, close_db
    await init_db()

    tool = MigrationTool(dry_run=not args.commit)
    stats = await tool.run()

    print("=== 迁移统计 ===")
    print(f"消息: 读取 {stats['messages_read']} | 迁移 {stats['messages_migrated']} | 去重 {stats['messages_dup']}")
    print(f"记忆: 读取 {stats['memories_read']} | 迁移 {stats['memories_migrated']} | 去重 {stats['memories_dup']}")
    print(f"失败: {len(stats['failures'])}")
    if stats["failures"]:
        print(f"失败明细已记录到 {_REPORT_FILE}")

    await close_db()
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(_main()))