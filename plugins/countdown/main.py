"""
倒计时插件（一个命令一个插件）

迁移自 Core 静态命令 .countdown/.倒计时，注册为插件命令。
数据沿用 Core 的 data/countdown.json（与旧版兼容）。

功能：
  .countdown                   查看最近的
  .countdown list              查看所有
  .countdown add <日期> <事件> 添加
  .countdown del <编号>        删除
  .countdown <日期> <事件>     快捷添加
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from core.logger import get_logger

logger = get_logger("plugin.countdown")

_COUNTDOWN_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "countdown.json"


def _load_countdowns() -> list[dict]:
    if _COUNTDOWN_FILE.exists():
        try:
            return json.loads(_COUNTDOWN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_countdowns(data: list[dict]):
    _COUNTDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COUNTDOWN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_items(items: list[dict], title: str) -> str:
    if not items:
        return "还没有倒计时喵~ 用 `.countdown add <日期> <事件>` 添加一个吧"
    lines = [title]
    now = _dt.datetime.now()
    for i, item in enumerate(items, 1):
        try:
            target = _dt.datetime.strptime(item["date"], "%Y-%m-%d")
            days = (target - now).days
            sign = "已过" if days < 0 else "还有"
            days_str = f"{sign} {abs(days)} 天"
        except Exception:
            days_str = "?"
        lines.append(f"  {i}. [{item['date']}] {item['event']}  ({days_str})")
    return "\n".join(lines)


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("countdown", "倒计时"):
            self.ctx.capability.register_command(
                name=name, description="倒计时：.countdown add/list/del",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        args = msg.get("args") or []

        if not args:
            return _format_items(_load_countdowns(), "【倒计时】")

        action = args[0].lower()

        if action == "list":
            return _format_items(_load_countdowns(), "【倒计时列表】")

        if action in ("del", "delete"):
            if len(args) < 2:
                return "用法: .countdown del <编号>"
            try:
                idx = int(args[1]) - 1
            except ValueError:
                return "编号必须是数字"
            items = _load_countdowns()
            if idx < 0 or idx >= len(items):
                return f"编号越界（共 {len(items)} 条）"
            removed = items.pop(idx)
            _save_countdowns(items)
            return f"已删除: [{removed['date']}] {removed['event']}"

        if action == "add":
            if len(args) < 3:
                return "用法: .countdown add <日期> <事件>\n例如: .countdown add 2026-12-25 圣诞节"
            date_str = args[1]
            event = " ".join(args[2:])
        else:
            # 快捷模式: .countdown 2026-12-25 圣诞节
            date_str = args[0]
            event = " ".join(args[1:])

        try:
            _dt.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return f"日期格式错误: {date_str}\n请使用 YYYY-MM-DD 格式，如 2026-12-25"

        items = _load_countdowns()
        items.append({"date": date_str, "event": event})
        _save_countdowns(items)

        target = _dt.datetime.strptime(date_str, "%Y-%m-%d")
        days = (target - _dt.datetime.now()).days
        days_str = f"还有 {days} 天" if days >= 0 else f"已过 {abs(days)} 天"
        return f"已添加倒计时喵~ [{date_str}] {event} ({days_str})"
