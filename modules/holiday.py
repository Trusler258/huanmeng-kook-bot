"""
节假日查询模块
- 每日 00:00 自动拉取 timor.tech 法定节假日 API
- 支持：法定节假日、周末、调休补班、工作日
- 注入到 LLM 上下文
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from core.logger import get_logger

logger = get_logger("holiday")

# 今日节假日信息
_today_info: dict = {}
_lock = asyncio.Lock()

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


async def _fetch_today() -> dict:
    """从 timor.tech 拉取今日节假日信息"""
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://timor.tech/api/holiday/info/{today}"
    try:
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("code") == 0:
                t = data.get("type") or {}
                h = data.get("holiday") or {}
                return {
                    "date": today,
                    "day_type": t.get("type", 0),   # 0=工作日 1=周末 2=节日 3=补班
                    "type_name": t.get("name", ""),
                    "is_holiday": bool(h.get("holiday")),
                    "holiday_name": h.get("name", ""),
                    "wage": h.get("wage", 1),
                    "target": h.get("target", ""),   # 补班所属节日（如"春节"）
                }
    except Exception as e:
        logger.warning("节假日 API 拉取失败: %s", e)
    return {}


async def refresh_holiday():
    """刷新当日节假日信息"""
    global _today_info
    async with _lock:
        _today_info = await _fetch_today()
        if _today_info:
            logger.info("节假日: %s → %s", _today_info["date"], _today_info["type_name"])


def get_today_holiday_text() -> str:
    """返回当日节假日文本，用于注入 LLM 上下文。普通工作日返回空。"""
    if not _today_info:
        return ""
    info = _today_info
    now = datetime.now()
    today_str = now.strftime("%Y年%m月%d日")
    weekday = "日一二三四五六"[now.weekday()]

    # 补班
    if info["day_type"] == 3:
        target = info.get("target", "")
        text = f"今天是{today_str}（周{weekday}），{info['type_name']}"
        if target:
            text += f"（为{target}调休补班）"
        return text

    # 法定节假日
    if info["day_type"] == 2:
        name = info["holiday_name"].replace("（休）", "").replace("（班）", "")
        return f"今天是{today_str}（周{weekday}），{name}，法定节假日放假中"

    # 周末
    if info["day_type"] == 1:
        return f"今天是{today_str}，周末（周{weekday}）"

    # 普通工作日不注入
    return ""


async def _daily_loop():
    """后台循环：等到下一个 00:00，然后每 24h 刷新一次"""
    while True:
        now = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        wait_sec = (next_midnight - now).total_seconds()
        logger.info("节假日每日刷新: 等待 %.0f 秒 (下次 %s)", wait_sec, next_midnight.strftime("%H:%M:%S"))
        await asyncio.sleep(wait_sec)
        await refresh_holiday()


async def start_holiday_service():
    """启动节假日服务：立即拉一次，然后每日刷新"""
    await refresh_holiday()
    asyncio.create_task(_daily_loop())
    logger.info("节假日服务已启动")
