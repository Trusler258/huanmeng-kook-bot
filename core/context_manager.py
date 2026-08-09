"""
上下文管理器
- 管理每个对话的上下文消息列表
- 管理每个对话的记忆缓冲区
- 管理活跃的发送任务（支持取消旧任务）
- 上下文自动裁剪（FIFO，不超过配置上限）
- 持久化到 data/context_cache.json，重启不丢瞬时记忆
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from core.logger import get_logger
from core.config import get_config

logger = get_logger("context")

_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "context_cache.json"


class ContextManager:
    """
    全局对话上下文管理。
    
    每个群/私聊维护独立的：
    - group_context: 供 LLM 参考的消息历史（带角色标签）
    - memory_buffer: 供记忆系统使用的原始消息（不带标签）
    - active_send_tasks: 当前活跃的发送任务
    """

    def __init__(self):
        self.group_context: dict[int, list[str]] = {}
        self.memory_buffer: dict[int, list[str]] = {}
        self.active_send_tasks: dict[int, asyncio.Task] = {}
        self._dirty: set[int] = set()
        self._load_from_disk()

    # ── 持久化 ──────────────────────────────────────────

    def _load_from_disk(self):
        """从文件恢复上下文"""
        try:
            if not _CACHE_FILE.exists():
                return
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                self.group_context[int(k)] = v
            logger.info("上下文已从磁盘恢复: %d 个对话", len(raw))
        except Exception as e:
            logger.warning("上下文恢复失败: %s", e)

    def _save_to_disk(self):
        """持久化上下文到文件"""
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # 只存最近 30 条，控制文件大小
            compact = {}
            for k, v in self.group_context.items():
                if v:
                    compact[str(k)] = v[-30:]
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(compact, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("上下文持久化失败: %s", e)

    # ── 上下文操作 ───────────────────────────────────────

    def append_to_context(self, chat_id: int, line: str):
        """追加一条消息到上下文历史"""
        if chat_id not in self.group_context:
            self.group_context[chat_id] = []
        
        self.group_context[chat_id].append(line)
        
        # 裁剪到配置上限
        cfg = get_config()
        max_len = cfg.context_length
        if len(self.group_context[chat_id]) > max_len:
            removed = self.group_context[chat_id][:-max_len]
            self.group_context[chat_id] = self.group_context[chat_id][-max_len:]
            logger.debug("上下文裁剪 [%d]: 移除 %d 条 (上限=%d)",
                       chat_id, len(removed), max_len)

        # 每 3 条写一次磁盘
        if len(self.group_context[chat_id]) % 3 == 0:
            self._save_to_disk()

    def get_context(self, chat_id: int) -> list[str]:
        """获取某对话的完整上下文"""
        return self.group_context.get(chat_id, [])

    # ── 记忆缓冲区操作 ───────────────────────────────────

    def append_to_buffer(self, chat_id: int, line: str):
        """追加一条消息到记忆缓冲区"""
        if chat_id not in self.memory_buffer:
            self.memory_buffer[chat_id] = []
        self.memory_buffer[chat_id].append(line)

    def get_buffer(self, chat_id: int) -> list[str]:
        """获取某对话的记忆缓冲区"""
        return self.memory_buffer.get(chat_id, [])

    def clear_buffer(self, chat_id: int):
        """清空缓冲区"""
        if chat_id in self.memory_buffer:
            self.memory_buffer[chat_id].clear()

    # ── 发送任务管理 ─────────────────────────────────────

    def set_active_send_task(self, chat_id: int, task: asyncio.Task):
        """设置活跃发送任务（会取消旧任务）"""
        old_task = self.active_send_tasks.get(chat_id)
        if old_task is not None and not old_task.done():
            logger.debug("取消旧发送任务 [%d]", chat_id)
            old_task.cancel()
        
        self.active_send_tasks[chat_id] = task
        
        # 注册完成回调以清理引用
        task.add_done_callback(lambda t: self._on_task_done(chat_id, t))

    def cancel_old_task(self, chat_id: int) -> Optional[asyncio.Task]:
        """
        取消旧的发送任务并返回它。
        新任务应该在调用此方法后通过 set_active_send_task 设置。
        """
        return self.active_send_tasks.get(chat_id)

    def _on_task_done(self, chat_id: int, task: asyncio.Task):
        """任务完成后的清理回调"""
        if chat_id in self.active_send_tasks and self.active_send_tasks[chat_id] is task:
            del self.active_send_tasks[chat_id]
            logger.debug("发送任务已清理 [%d]", chat_id)

    # ── 统计与调试 ───────────────────────────────────────

    def get_stats(self) -> dict:
        """获取管理器状态统计"""
        return {
            "active_chats": len(self.group_context),
            "total_context_lines": sum(len(v) for v in self.group_context.values()),
            "total_buffer_lines": sum(len(v) for v in self.memory_buffer.values()),
            "active_tasks": len(self.active_send_tasks),
        }

    def cleanup_inactive(self, max_idle_seconds: float = 3600.0):
        """清理长时间不活动的对话上下文（可选，定时调用）"""
        import time
        now = time.time()
        to_remove = []
        for chat_id in list(self.group_context.keys()):
            ctx = self.group_context[chat_id]
            if not ctx:
                continue
            # 简单启发：如果最后一条消息时间戳... 实际上我们没记录时间戳
            # 这里用条数过少的作为不活跃标记（简单实现）
            pass  # TODO: 添加时间戳追踪后实现真正的过期清理


# ── 全局单例 ────────────────────────────────────────────────
_global_ctx_mgr: Optional[ContextManager] = None


def get_context_mgr() -> ContextManager:
    global _global_ctx_mgr
    if _global_ctx_mgr is None:
        _global_ctx_mgr = ContextManager()
    return _global_ctx_mgr


def init_context():
    """初始化全局上下文管理器"""
    global _global_ctx_mgr
    _global_ctx_mgr = ContextManager()
    logger.info("上下文管理器已初始化（含磁盘持久化）")


def save_context():
    """SAFELY persist all context to disk (call on shutdown)"""
    mgr = get_context_mgr()
    # Transfer all group_context data before saving
    try:
        mgr._save_to_disk()
        logger.info("上下文已写入磁盘")
    except Exception as e:
        logger.warning("上下文写入失败: %s", e)
