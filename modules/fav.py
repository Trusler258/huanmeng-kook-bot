"""
好感度系统（原 fav.py）
- JSON 文件存储，范围 -100 ~ +100
- 默认初始值 +50
- 按聊天上下文隔离：每个群/私聊的好感度独立
- key 格式: g{group_id}:{user_id} (群聊) / p:{user_id} (私聊)
- 自动迁移旧格式数据（旧格式条目会被清理）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.logger import get_logger

logger = get_logger("fav")

FAV_FILE = Path(__file__).resolve().parent.parent / "data" / "fav.json"

# 新格式 key 的正则匹配
_NEW_KEY_PATTERN = re.compile(r'^(g\d+:\d+|p:\d+)$')


def _make_fav_key(chat_id: int, user_id: int, is_group: bool) -> str:
    """生成好感度存储 key

    群聊: g{群号}:{用户QQ}
    私聊: p:{用户QQ}
    """
    if is_group:
        return f"g{chat_id}:{user_id}"
    else:
        return f"p:{user_id}"


def _load_fav() -> dict[str, int]:
    """加载好感度数据，自动检测并清理旧格式条目"""
    if not FAV_FILE.exists():
        return {}
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}

        # 检测旧格式（key 不匹配新 pattern 的条目）
        has_old = False
        new_data = {}
        for k, v in data.items():
            if _NEW_KEY_PATTERN.match(str(k)):
                new_data[str(k)] = int(v)
            else:
                has_old = True
                logger.debug("跳过旧格式好感度条目: %s=%s", k, v)

        if has_old and new_data:
            logger.info("检测到旧格式好感度数据，已自动清理不兼容条目")
            _save_fav(new_data)
        elif has_old and not new_data:
            # 全是旧格式，直接清空
            logger.info("检测到纯旧格式好感度数据，已清空（无法自动迁移缺少 chat_id）")
            _save_fav({})

        return new_data
    except Exception as e:
        logger.warning("读取好感度文件失败: %s", e)
        return {}


def _save_fav(data: dict[str, int]):
    """保存好感度数据到文件"""
    FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FAV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_fav(chat_id: int, user_id: int, is_group: bool) -> int:
    """获取某用户在某聊天中的好感度值，不存在则返回默认值 50"""
    data = _load_fav()
    key = _make_fav_key(chat_id, user_id, is_group)
    return data.get(key, 50)


def update_fav(chat_id: int, user_id: int, delta: int, is_group: bool) -> int:
    """
    更新好感度（增量方式）。

    Args:
        chat_id: 聊天 ID（群号或用户 QQ）
        user_id: 用户 QQ 号
        delta: 好感度变化量
        is_group: 是否群聊

    Returns:
        更新后的好感度值
    """
    data = _load_fav()
    key = _make_fav_key(chat_id, user_id, is_group)
    current = data.get(key, 50)
    new_val = max(-100, min(100, current + delta))
    data[key] = new_val
    _save_fav(data)
    logger.info("好感度更新: %s %+d → %d", key, delta, new_val)
    return new_val


def get_all_fav(chat_id: int | None = None, is_group: bool = True) -> dict[str, int]:
    """
    获取好感度数据。

    Args:
        chat_id: 聊天 ID（指定则只返回该聊天的数据）
        is_group: 是否群聊（仅 chat_id 指定时有效）

    Returns:
        {fav_key: value} 字典
    """
    data = _load_fav()
    if chat_id is None:
        return data

    # 按 chat 过滤
    if is_group:
        prefix = f"g{chat_id}:"
    else:
        prefix = f"p:{chat_id}"

    return {k: v for k, v in data.items() if k.startswith(prefix)}


def ensure_fav(chat_id: int, user_id: int, is_group: bool) -> None:
    """首次对话自动注册好感度（默认 50），已存在则跳过"""
    key = _make_fav_key(chat_id, user_id, is_group)
    data = _load_fav()
    if key in data:
        return
    data[key] = 50
    _save_fav(data)
    logger.info("好感度首次注册: %s = 50", key)


def reset_all_fav() -> bool:
    """清空所有好感度记录。返回是否成功。"""
    try:
        if FAV_FILE.exists():
            FAV_FILE.unlink()
        logger.info("好感度数据已全部清除")
        return True
    except Exception as e:
        logger.error("删除好感度文件失败: %s", e)
        return False
