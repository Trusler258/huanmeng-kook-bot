"""
用户名获取模块（KOOK 版）
- 从 roles.toml 预设映射 → khl.py Bot 在线查询 二级查找
- 替换消息中的 (met)id(met) 为 @昵称
- 自动记录 user_id → 昵称 映射到 roles.toml
"""

from __future__ import annotations

import re
import asyncio
from pathlib import Path
from typing import Optional, Union

from core.logger import get_logger
from core.config import get_config

logger = get_logger("username")

# ── 全局 khl.py Bot 引用（由 bot.py 启动时通过 init_username 注入）──
_khl_bot = None

# ── 内存缓存：user_id → 昵称（避免频繁写文件）──
_name_cache: dict[str, str] = {}
_save_lock = asyncio.Lock()


def init_username(khl_bot):
    """注入 khl.py Bot 实例"""
    global _khl_bot
    _khl_bot = khl_bot


async def record_user_name(user_id: Union[int, str], nickname: str):
    """记录 user_id → 昵称 映射
    - 更新内存缓存
    - 异步写回 roles.toml（去重，避免频繁 IO）
    """
    if not nickname or not user_id:
        return
    uid_str = str(user_id)
    if uid_str in _name_cache and _name_cache[uid_str] == nickname:
        return  # 已记录，跳过

    _name_cache[uid_str] = nickname

    # 同步更新 cfg 内存中的 qq_name_map
    try:
        cfg = get_config()
        if not cfg.qq_name_map.get(uid_str):
            cfg.qq_name_map[uid_str] = nickname
            # 异步写回文件（不阻塞消息处理）
            asyncio.ensure_future(_persist_name_map())
    except Exception as e:
        logger.debug("记录用户名失败: %s", e)


async def _persist_name_map():
    """将 qq_name_map 持久化到 roles.toml"""
    async with _save_lock:
        try:
            cfg = get_config()
            roles_path = Path(__file__).resolve().parent.parent / "config" / "roles.toml"
            if not roles_path.exists():
                return

            import toml
            data = toml.loads(roles_path.read_text(encoding="utf-8"))

            # 确保 qq_name_map 段存在
            if "qq_name_map" not in data:
                data["qq_name_map"] = {}

            # 只新增不存在的条目（不覆盖手动配置）
            changed = False
            for uid, name in cfg.qq_name_map.items():
                if uid not in data["qq_name_map"]:
                    data["qq_name_map"][uid] = name
                    changed = True

            if changed:
                roles_path.write_text(toml.dumps(data), encoding="utf-8")
                logger.info("用户名映射已持久化到 roles.toml (+%d 条)", len(cfg.qq_name_map) - len(data.get("qq_name_map", {})))
        except Exception as e:
            logger.warning("持久化用户名映射失败: %s", e)


async def get_or_resolve_username(
    user_id: Union[int, str],
    *,
    channel_id: Optional[Union[int, str]] = None,
) -> str:
    """
    二级查找用户名：
    1. roles.toml 预设（qq_name_map）
    2. khl.py 在线查询（fetch_user）

    Args:
        user_id: KOOK user_id（int 或 str）
        channel_id: 所在字频道 ID（保留参数，KOOK 用户名与频道无关）

    Returns:
        用户昵称，查询失败则返回 user_id 字符串
    """
    uid_str = str(user_id)

    # 第一优先：roles.toml 预设
    cfg = get_config()
    preset_name = cfg.qq_name_map.get(uid_str)
    if preset_name:
        logger.debug("用户 %s 映射到预设名称: %s (来源: roles.toml)", uid_str, preset_name)
        return preset_name

    # 第二优先：khl.py 在线查询
    if _khl_bot is None:
        logger.debug("khl_bot 未注入，无法在线查询 %s", uid_str)
        return uid_str

    try:
        user = await _khl_bot.client.fetch_user(uid_str)
        if user:
            nick = getattr(user, 'username', '') or getattr(user, 'nickname', '') or uid_str
            logger.debug("通过 khl.py 查到 %s 昵称: %s", uid_str, nick)
            return nick
    except Exception as e:
        logger.warning("通过 khl.py 查询用户 %s 失败: %s", uid_str, e)

    return uid_str


async def replace_at_in_message(
    message: str,
    *,
    bot_id: Optional[Union[int, str]] = None,
    bot_name: Optional[str] = None,
    channel_id: Optional[Union[int, str]] = None,
) -> str:
    """
    替换消息中的 (met)id(met) 为 @昵称。

    KOOK 的 @ 格式是 (met)user_id(met)，本函数将其转为可读的 @昵称 文本。
    bot 自身的 @ 保留为 @bot_name（如果提供）。

    Args:
        message: 原始消息文本（含 KMarkdown (met)id(met)）
        bot_id: 机器人自身的 user_id（避免循环查询）
        bot_name: 机器人昵称
        channel_id: 所在字频道 ID

    Returns:
        替换后的消息文本
    """
    bot_id_str = str(bot_id) if bot_id is not None else ""

    def _replace(match):
        uid = match.group(1)
        if uid in ('here', 'all'):
            return '@全体 '
        if bot_id_str and uid == bot_id_str:
            return f'@{bot_name or "bot"} '
        # 异步函数不能在 re.sub 中直接调用，这里用同步回退
        return f'@{uid} '

    # 先用同步方式替换（用预设昵称），未命中的留待后续异步处理
    cfg = get_config()
    pending_uids = []

    def _sync_replace(match):
        uid = match.group(1)
        if uid in ('here', 'all'):
            return '@全体 '
        if bot_id_str and uid == bot_id_str:
            return f'@{bot_name or "bot"} '
        preset = cfg.qq_name_map.get(uid)
        if preset:
            return f'@{preset} '
        pending_uids.append(uid)
        return match.group(0)  # 保留原样，稍后异步替换

    text = re.sub(r'\(met\)(\w+)\(met\)', _sync_replace, message)

    # 如果有未命中的，异步查询并替换
    if pending_uids:
        for uid in pending_uids:
            nick = await get_or_resolve_username(uid, channel_id=channel_id)
            if nick and nick != uid:
                text = text.replace(f'(met){uid}(met)', f'@{nick} ')
            else:
                text = text.replace(f'(met){uid}(met)', f'@{uid} ')

    return text
