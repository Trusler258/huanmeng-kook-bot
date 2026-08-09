"""
反刷屏禁言模块 (spam_guard.py)

功能：
- 跟踪每个群内每个用户对机器人的 @mention 频次
- 检测重复/相似消息的连续 @mention（刷屏行为）
- 超过阈值时自动禁言 + 发送警告消息
- 管理员和好友自动豁免

触发条件（全部满足）：
  1. 在时间窗口内（默认 3 分钟）
  2. 用户 @机器人 的次数 >= 阈值（默认 8 次）
  3. 消息内容重复（归一化后相似）

禁言参数：
  - 禁言时长：默认 30 分钟（1800 秒）
  - 禁言后冷却：同一用户 10 分钟内不会重复禁言
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from core.logger import get_logger
from core.config import get_config

logger = get_logger("spam_guard")

# ── 默认配置 ────────────────────────────────────────────────
_DEFAULT_THRESHOLD = 8          # 触发禁言的重复 @ 次数
_DEFAULT_WINDOW = 180           # 检测时间窗口（秒）
_DEFAULT_DURATION = 1800        # 禁言时长（秒），0 = 仅警告不禁言
_DEFAULT_COOLDOWN = 600         # 禁言后冷却期（秒）


@dataclass
class _UserRecord:
    """单个用户的刷屏追踪记录"""
    timestamps: list[float] = field(default_factory=list)    # @mention 时间戳
    messages: list[str] = field(default_factory=list)        # 归一化后的消息内容
    last_mute_time: float = 0.0                              # 上次被禁言的时间


# ── 全局状态 ────────────────────────────────────────────────
# key: group_id, value: {user_id: _UserRecord}
_records: dict[int, dict[int, _UserRecord]] = defaultdict(dict)


def _normalize_msg(text: str) -> str:
    """归一化消息文本用于相似度比较"""
    # 去掉 @mention
    text = re.sub(r'@\S+\s*', '', text)
    # 去掉 CQ 码
    text = re.sub(r'\[CQ:[^\]]+\]', '', text)
    # 去掉所有标点和空格（保留字母数字中文下划线）
    text = re.sub(r'[^\w\u4e00-\u9fff]', '', text, flags=re.UNICODE).lower()
    return text.strip()

def _get_threshold() -> int:
    """从配置读取阈值"""
    try:
        return get_config().spam_threshold
    except Exception:
        return _DEFAULT_THRESHOLD


def _get_duration() -> int:
    """从配置读取禁言时长"""
    try:
        return get_config().mute_duration
    except Exception:
        return _DEFAULT_DURATION


def _is_exempt(user_id: int) -> bool:
    """检查用户是否豁免（管理员/好友）"""
    cfg = get_config()
    if user_id == cfg.admin_qq:
        return True
    if user_id in cfg.friend_qqs:
        return True
    return False


def _get_record(group_id: int, user_id: int) -> _UserRecord:
    """获取或创建用户记录"""
    if group_id not in _records:
        _records[group_id] = {}
    if user_id not in _records[group_id]:
        _records[group_id][user_id] = _UserRecord()
    return _records[group_id][user_id]


def check_and_record(
    group_id: int,
    user_id: int,
    msg_text: str,
) -> bool:
    """
    记录一次 @mention 并检查是否需要禁言。

    Args:
        group_id: 群号
        user_id: 用户 QQ 号
        msg_text: 原始消息文本

    Returns:
        True 表示已触发禁言（调用方应跳过正常回复流程）
    """
    now = time.time()

    # 豁免检查
    if _is_exempt(user_id):
        return False

    record = _get_record(group_id, user_id)

    # 冷却期内不重复禁言
    if now - record.last_mute_time < _DEFAULT_COOLDOWN:
        logger.debug("用户 %d 在冷却期内 (群 %d)，跳过刷屏检测", user_id, group_id)
        return False

    # 清理过期记录（超出时间窗口的）
    # 必须同时过滤 timestamps 和 messages，否则 zip 会错位配对
    cutoff = now - _DEFAULT_WINDOW
    pairs = [(t, m) for t, m in zip(record.timestamps, record.messages) if t > cutoff]
    record.timestamps = [p[0] for p in pairs]
    record.messages = [p[1] for p in pairs]

    # 归一化消息
    normalized = _normalize_msg(msg_text)

    # 记录本次 @mention
    record.timestamps.append(now)
    record.messages.append(normalized)

    # 检查是否达到阈值
    count = len(record.timestamps)
    threshold = _get_threshold()

    logger.debug("刷屏检测: user=%d 群=%d 当前=%d/%d", user_id, group_id, count, threshold)

    if count < threshold:
        return False

    # 检查消息重复度（最近 threshold 条中，归一化后相同消息占比 >= 60%）
    recent_msgs = record.messages[-threshold:]
    if not recent_msgs:
        return False

    # 找出现最多的消息
    msg_counts: dict[str, int] = {}
    for m in recent_msgs:
        if not m:  # 跳过空消息（纯@的情况）
            continue
        msg_counts[m] = msg_counts.get(m, 0) + 1

    if not msg_counts:
        # 全是空消息（纯@），也视为刷屏
        logger.info("刷屏检测触发: user=%d 群=%d 连续 %d 次纯@无内容", user_id, group_id, count)
        return True

    max_count = max(msg_counts.values())
    # 去重后消息种类少 = 内容高度重复
    unique_ratio = len(msg_counts) / len([m for m in recent_msgs if m])

    is_spam = (max_count >= threshold * 0.5) or (unique_ratio <= 0.4)

    if not is_spam:
        return False

    logger.warning(
        "刷屏检测触发! user=%d 群=%d 次数=%d 最重复=%d/%d 去重率=%.0f%%",
        user_id, group_id, count, max_count, len(recent_msgs), unique_ratio * 100,
    )
    return True


async def execute_mute(
    group_id: int,
    user_id: int,
    sender_name: str,
) -> str | None:
    """
    执行禁言操作：调用 API 禁言 + 发送警告消息。

    Args:
        group_id: 群号
        user_id: 用户 QQ 号
        sender_name: 用户昵称

    Returns:
        None 表示成功；失败返回错误描述
    """
    duration = _get_duration()
    record = _get_record(group_id, user_id)
    record.last_mute_time = time.time()

    # 清空计数（禁言后重新计算）
    record.timestamps.clear()
    record.messages.clear()

    from services.sender import send_group_msg

    if duration > 0:
        # KOOK 禁言：通过 khl.py Bot 实例调用
        from services.sender import _bot as _khl_bot
        if _khl_bot is not None:
            try:
                # KOOK 中禁言需要 guild_id 和 user_id
                # channel_id → guild_id 的映射由 khl.py 内部处理
                from services.sender import _channel_cache
                ch = _channel_cache.get(str(group_id))
                if ch is not None:
                    guild_id = getattr(ch, 'guild_id', None)
                    if guild_id:
                        from khl import User
                        # khl.py 的 mute 需要 guild 对象
                        guild = await _khl_bot.client.fetch_guild(guild_id)
                        user = await _khl_bot.client.fetch_user(user_id)
                        await guild.mute_user(user, duration)
                    else:
                        logger.warning("无法获取 guild_id，跳过禁言: channel=%s", group_id)
                else:
                    logger.warning("频道未缓存，跳过禁言: channel=%s", group_id)
            except Exception as e:
                logger.error("KOOK 禁言失败: user=%s channel=%s err=%s", user_id, group_id, e)

    # 生成可读时长
    if duration >= 3600:
        hours = duration // 3600
        minutes = (duration % 3600) // 60
        duration_text = f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
    elif duration >= 60:
        minutes = duration // 60
        duration_text = f"{minutes}分钟"
    else:
        duration_text = f"{duration}秒"

    # 发送警告消息
    cfg = get_config()
    if duration > 0:
        warn_msg = (
            f"(met){user_id}(met) "
            f"检测到刷屏行为，{sender_name} 已被禁言 {duration_text} 喵！\n"
            f"请停止重复@我，再犯会继续加长禁言时间哦~"
        )
    else:
        warn_msg = (
            f"(met){user_id}(met) "
            f"检测到刷屏行为，请停止重复@我喵！\n"
            f"再继续的话就要被禁言了哦~"
        )

    try:
        await send_group_msg(warn_msg, group_id)
        logger.info("禁言警告已发送: user=%d 群=%d 时长=%s", user_id, group_id, duration_text)
        return None
    except Exception as e:
        logger.error("发送禁言警告失败: %s", e)
        return f"发送禁言警告失败: {e}"
