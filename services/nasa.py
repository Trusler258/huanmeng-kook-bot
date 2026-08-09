"""
NASA APOD 服务
.nasa [日期]  获取每日天文图片
"""
import json, os, urllib.request
from urllib.parse import quote

_API_KEY = os.environ.get("NASA_API_KEY", "DEMO_KEY")
_BASE = "https://api.nasa.gov/planetary/apod"


def get_apod(date: str = "") -> dict:
    """获取 APOD 数据 → {title, explanation, hdurl, date, media_type}"""
    url = f"{_BASE}?api_key={_API_KEY}"
    if date:
        url += f"&date={date}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
