"""
定时提醒模块
- 解析自然语言时间："30分钟后"、"1小时后"、"明天14:30"、"14:30"、"3秒后"
- 存储到 data/reminders.json
- 背景任务轮询到期提醒并自动 @ 发送
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.logger import get_logger

logger = get_logger("remind")

_REMIND_FILE = Path(__file__).resolve().parent.parent / "data" / "reminders.json"


def _parse_time(text: str) -> tuple[int, str | None]:
    """
    解析自然语言时间表述，返回 (目标时间戳, 错误消息)。

    支持格式：
      - "X秒后" / "Xs后"
      - "X分钟后" / "Xmin后"
      - "X小时后" / "Xh后"
      - "明天 HH:MM" / "明天HH:MM"
      - "后天 HH:MM"
      - "HH:MM" (今天该时间；若已过期则推到明天)
      - 纯数字 → 视为分钟
    """
    now = datetime.now()
    text = text.strip()

    # ── X秒后 ──
    m = re.match(r'(\d+)\s*秒(?:后)?', text)
    if m:
        return int(now.timestamp() + int(m.group(1))), None

    # ── X分钟后 ──
    m = re.match(r'(\d+)\s*(?:分钟|min)(?:后)?', text, re.IGNORECASE)
    if m:
        return int(now.timestamp() + int(m.group(1)) * 60), None

    # ── X小时后 ──
    m = re.match(r'(\d+)\s*(?:小时|h)(?:后)?', text, re.IGNORECASE)
    if m:
        return int(now.timestamp() + int(m.group(1)) * 3600), None

    # ── 明天 HH:MM ──
    m = re.match(r'明[天日]\s*(\d{1,2}):(\d{2})', text)
    if m:
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0) + timedelta(days=1)
        return int(target.timestamp()), None

    # ── 后天 HH:MM ──
    m = re.match(r'后[天日]\s*(\d{1,2}):(\d{2})', text)
    if m:
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0) + timedelta(days=2)
        return int(target.timestamp()), None

    # ── HH:MM（今天；过期则推到明天）──
    m = re.match(r'(\d{1,2}):(\d{2})$', text)
    if m:
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # 已过期 → 明天
        return int(target.timestamp()), None

    # ── 纯数字 → 视为分钟 ──
    if text.isdigit():
        return int(now.timestamp() + int(text) * 60), None

    return 0, f"无法理解时间「{text[:30]}」，试试「30分钟后」「明天14:30」「14:30」喵~"


def add_reminder(
    chat_id: int,
    user_id: int,
    target_time: int,
    content: str,
    is_group: bool,
) -> dict:
    """添加一条提醒，返回 {id, target_time, content}"""
    _REMIND_FILE.parent.mkdir(parents=True, exist_ok=True)

    all_reminders: list[dict] = []
    if _REMIND_FILE.exists():
        try:
            all_reminders = json.loads(_REMIND_FILE.read_text(encoding="utf-8"))
        except Exception:
            all_reminders = []

    import uuid
    rid = uuid.uuid4().hex[:8]

    record = {
        "id": rid,
        "chat_id": chat_id,
        "user_id": user_id,
        "target_time": target_time,
        "content": content,
        "is_group": is_group,
        "created": int(time.time()),
        "fired": False,
    }

    all_reminders.append(record)
    _REMIND_FILE.write_text(json.dumps(all_reminders, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("提醒已添加: id=%s target=%d content='%s' user=%d",
               rid, target_time, content[:30], user_id)
    return record


def get_pending_reminders() -> list[dict]:
    """获取所有未触发的到期提醒（target_time <= now）"""
    if not _REMIND_FILE.exists():
        return []

    try:
        all_reminders = json.loads(_REMIND_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    now = int(time.time())
    pending = [r for r in all_reminders if not r.get("fired") and r["target_time"] <= now]

    # 标记为已触发
    if pending:
        for r in all_reminders:
            if r["id"] in {p["id"] for p in pending}:
                r["fired"] = True
        _REMIND_FILE.write_text(json.dumps(all_reminders, ensure_ascii=False, indent=2), encoding="utf-8")

    return pending


def clean_old_reminders(max_age_days: int = 7):
    """清理 7 天前的提醒记录"""
    if not _REMIND_FILE.exists():
        return

    try:
        all_reminders = json.loads(_REMIND_FILE.read_text(encoding="utf-8"))
    except Exception:
        return

    cutoff = int(time.time()) - max_age_days * 86400
    kept = [r for r in all_reminders if r.get("created", 0) > cutoff]
    if len(kept) < len(all_reminders):
        _REMIND_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("清理了 %d 条过期提醒", len(all_reminders) - len(kept))


async def remind_checker_loop():
    """
    后台轮询任务：每 30 秒检查一次到期提醒并发送。
    在 bot.py 中通过 asyncio.create_task() 启动。
    """
    from services.sender import send_group_msg, send_private_msg

    logger.info("提醒轮询已启动（每30秒检查一次）")

    while True:
        try:
            pending = get_pending_reminders()
            for r in pending:
                content_text = r["content"]
                user_id = r["user_id"]

                msg = f"⏰ (met){user_id}(met) 提醒时间到喵~ {content_text}"

                try:
                    if r["is_group"]:
                        await send_group_msg(msg, r["chat_id"])
                    else:
                        await send_private_msg(msg, r["user_id"])
                    logger.info("提醒已发送: id=%s user=%d content='%s'",
                               r["id"], user_id, content_text[:30])
                except Exception as e:
                    logger.error("提醒发送失败 id=%s: %s", r["id"], e)

            clean_old_reminders()
        except Exception as e:
            logger.error("提醒轮询异常: %s", e)

        await asyncio.sleep(30)
