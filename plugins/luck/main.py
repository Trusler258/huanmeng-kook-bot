"""
每日运势插件（一个命令一个插件）

迁移自 Core 静态命令 .luck，注册为插件命令。
数据沿用 Core 的 data/luck.json（与旧版 .luck 状态兼容）。
同日同人返回相同结果。
"""
from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path

from core.logger import get_logger

logger = get_logger("plugin.luck")

LUCK_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "luck.json"


def _get_today_luck(user_id) -> int | None:
    today = date.today().isoformat()
    if not LUCK_FILE.exists():
        return None
    try:
        with open(LUCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(today, {}).get(str(user_id))
    except Exception as e:
        logger.warning("获取今日运气失败: %s", e)
        return None


def _set_today_luck(user_id, value: int):
    LUCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    data = {}
    if LUCK_FILE.exists():
        try:
            with open(LUCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if today not in data:
        data[today] = {}
    data[today][str(user_id)] = value
    sorted_days = sorted(data.keys(), reverse=True)[:7]
    data = {d: data[d] for d in sorted_days}
    with open(LUCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        self.ctx.capability.register_command(
            name="luck", description="每日运势抽签：.luck（同日同人结果一致）",
            handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        user_id = msg.get("author")
        if user_id is None:
            return "无法识别你的身份 ID。"
        cached = _get_today_luck(user_id)
        if cached is not None:
            return f"(met){user_id}(met) 你今天的运气是 {cached} 喵（今日已抽签）"
        if random.random() < 0.001:
            num = 1000
        else:
            num = random.randint(1, 100)
        _set_today_luck(user_id, num)
        return f"(met){user_id}(met) 你今天的运气是 {num} 喵"
