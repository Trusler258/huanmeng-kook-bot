"""
天气查询模块（从 commands.py 抽离）
- 7 天天气预报 API
- 口语化播报 + 表格展示
- 支持 i18n
- ✨ 精美卡片图片模式（Playwright 截图）
"""

from __future__ import annotations

import httpx
import re
from typing import Optional

from core.logger import get_logger
from core.config import get_config
from utils.format_lang import format_lang

logger = get_logger("weather")

# 快递状态映射（i18n 友好）
WEATHER_API_URL_TEMPLATE = "https://60s.viki.moe/v2/weather/forecast?query={city}&days=7&encoding=json"


async def query_weather(city: str) -> Optional[dict]:
    """
    调用天气 API 获取 7 天预报数据。
    
    Args:
        city: 城市名称
        
    Returns:
        API 返回的完整 JSON 数据；失败返回 None
    """
    url = WEATHER_API_URL_TEMPLATE.format(city=city.rstrip("市"))
    logger.info("查询天气: city=%s url=%s...", city, url[:50])
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("天气API HTTP错误: %d", resp.status_code)
                return None
            data = resp.json()
            if data.get("code") != 200:
                logger.warning("天气API业务错误: %s", data.get("message"))
                return None
            return data
    except Exception as e:
        logger.error("天气查询失败: %s", e)
        return None


def build_weather_report(data: dict, user_id: int) -> str:
    """
    从 API 数据构建完整的天气报告（口语化 + 表格）。
    
    Args:
        data: 天气 API 返回的 dict
        user_id: 请求者 QQ 号（用于判断是否管理员）
        
    Returns:
        完整的 Markdown 格式天气报告
    """
    location_data = data["data"]["location"]
    daily_list = data["data"]["daily_forecast"][:7]

    # 组装地名
    loc_name = (
        f"{location_data.get('province', '')}"
        f"{location_data.get('city', '')}"
        f"{location_data.get('county', '')}"
    ).strip() or "未知"

    cfg = get_config()
    is_admin = (user_id == cfg.admin_qq)

    # ── 口语化播报部分 ──
    talk_parts = _build_talk_section(daily_list, loc_name, is_admin)
    talk_text = "\n".join(talk_parts)

    # ── 表格部分 ──
    table_text = _build_table_section(daily_list)

    return talk_text + "\n\n" + table_text


def _build_talk_section(daily_list: list, loc_name: str, is_admin: bool) -> list[str]:
    """构建口语化播报文本列表"""
    parts: list[str] = []

    # 开场白
    if is_admin:
        parts.append(format_lang("weather.talk_admin", location=loc_name))
    else:
        parts.append(format_lang("weather.talk_user", location=loc_name))

    # 温度范围
    temps = [(d["max_temperature"], d["min_temperature"]) for d in daily_list]
    max_temp_val = max(t[0] for t in temps)
    min_temp_val = min(t[1] for t in temps)
    max_day_idx = [t[0] for t in temps].index(max_temp_val)
    min_day_idx = [t[1] for t in temps].index(min_temp_val)
    max_day_str = daily_list[max_day_idx]["date"][-5:]
    min_day_str = daily_list[min_day_idx]["date"][-5:]

    parts.append(format_lang(
        "weather.talk_temp_range",
        min=min_temp_val, max=max_temp_val,
        max_day=max_day_str, min_day=min_day_str,
    ))

    # 雨天提醒
    rains: list[str] = []
    for d in daily_list:
        date_str = d["date"][-5:]
        day_cond = d["day_condition"]
        night_cond = d.get("night_condition", day_cond)
        if "雨" in day_cond or "雨" in night_cond:
            rains.append(f"{date_str}会下{day_cond}到{night_cond}")

    if rains:
        parts.append(format_lang("weather.talk_rain", rain_dates="、".join(rains)))
    else:
        parts.append(format_lang("weather.talk_no_rain"))

    # 空气质量
    avg_aqi = sum(d.get("aqi", 0) for d in daily_list) // len(daily_list)
    if avg_aqi <= 50:
        aqi_feel = format_lang("weather.talk_aqi_good")
    elif avg_aqi <= 100:
        aqi_feel = format_lang("weather.talk_aqi_moderate")
    else:
        aqi_feel = format_lang("weather.talk_aqi_poor")
    parts.append(f"{aqi_feel}。")

    parts.append(format_lang("weather.talk_footer"))
    return parts


def _build_table_section(daily_list: list) -> str:
    """构建逐日预报表格"""
    lines: list[str] = []

    # 表头
    lines.append(format_lang("weather.table_header_prefix"))
    header_line = (
        f"{format_lang('weather.table_header_date'):<6} "
        f"{format_lang('weather.table_header_condition'):<16} "
        f"{format_lang('weather.table_header_temp'):<12} "
        f"{format_lang('weather.table_header_wind'):<18} "
        f"{format_lang('weather.table_header_aqi')}"
    )
    lines.append(header_line)

    # 数据行
    for d in daily_list:
        date_str = d["date"][-5:]
        day_cond = d["day_condition"]
        night_cond = d.get("night_condition", day_cond)
        cond = f"{day_cond} / {night_cond}"

        high = d["max_temperature"]
        low = d["min_temperature"]
        temp = f"{high}℃ / {low}℃"

        wind_day = f"{d.get('day_wind_direction', '')}{d.get('day_wind_power', '')}"
        wind_night = f"{d.get('night_wind_direction', '微风')}{d.get('night_wind_power', '1-3级')}"
        wind = f"{wind_day} / {wind_night}" if wind_day != wind_night else wind_day

        aqi_val = d.get("aqi", 0)
        quality = d.get("air_quality", "未知")
        aqi_str = f"{quality}({aqi_val})"

        lines.append(f"{date_str:<6} {cond:<16} {temp:<12} {wind:<18} {aqi_str}")

    return "\n".join(lines)
