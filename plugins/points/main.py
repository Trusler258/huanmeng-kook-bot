"""
每日签到 + 积分系统 插件（KOOK，独立于商店插件运行）

功能（插件内注册命令，前缀 .）：
  签到 : .签到 / .checkin / .sign   每日签到得积分（含连续签到加成）→ 写入 plugins/points/data.json
  积分 : .积分 / .points            查看本人余额/连续/累计
         管理员维护：.积分 加 <ID> <数量> | .积分 减 <ID> <数量> | .积分 设 <ID> <数量>

数据：持久化到 plugins/points/data.json，作为积分余额的唯一数据源，
      商店插件(shop)读同一文件完成扣款/消费。

管理员判定：core 配置的 admin 或 manifest.config.admins 中列出的 ID。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

_DATA_FILE = Path(__file__).resolve().parent / "data.json"
_LOCK = RLock()


# ── 数据读写（线程安全）────────────────────────────────────

def load_data() -> dict:
    with _LOCK:
        if _DATA_FILE.exists():
            try:
                return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"users": {}}


def save_data(data: dict) -> None:
    with _LOCK:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_user(data: dict, uid) -> dict:
    u = data["users"].setdefault(str(uid), {})
    u.setdefault("points", 0)
    u.setdefault("last_checkin", "")
    u.setdefault("streak", 0)
    u.setdefault("total", 0)
    return u


def _date(tz: int) -> str:
    return (datetime.utcnow() + timedelta(hours=max(-23, min(23, tz)))).strftime("%Y-%m-%d")


def _yesterday(tz: int) -> str:
    base = datetime.utcnow() + timedelta(hours=max(-23, min(23, tz)))
    return (base - timedelta(days=1)).strftime("%Y-%m-%d")


def _is_admin(uid, cfg_admins) -> bool:
    s = str(uid)
    try:
        from core.config import get_config
        c = get_config()
        if c.admin_id_str and s == str(c.admin_id_str):
            return True
    except Exception:
        pass
    return s in [str(a) for a in (list(cfg_admins or []))]


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("checkin", "签到", "sign"):
            self.ctx.capability.register_command(
                name=name, description="每日签到得积分：.签到",
                handler=self._cmd_checkin)
        for name in ("points", "积分"):
            self.ctx.capability.register_command(
                name=name, description="查看积分余额：.积分",
                handler=self._cmd_points)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    # ── 签到 ────────────────────────────────────────────
    async def _cmd_checkin(self, msg):
        uid = msg.get("author")
        name = str(msg.get("sender") or uid)
        if uid is None:
            return "无法识别你的身份 ID。"

        base = int(self.ctx.config("base_reward", 100))
        bonus_step = int(self.ctx.config("streak_bonus", 20))
        bonus_cap = int(self.ctx.config("streak_bonus_cap", 5))
        cur = str(self.ctx.config("currency", "积分"))
        tz = int(self.ctx.config("tz_hours", 8))
        today = _date(tz)

        data = load_data()
        u = ensure_user(data, uid)
        if u["last_checkin"] == today:
            return (f"(met){uid}(met) 你今天已经签到过啦，明天再来"
                    f"（连续 {u['streak']} 天，当前 {u['points']} {cur}）")
        if u["last_checkin"] == _yesterday(tz):
            streak = u["streak"] + 1
        else:
            streak = 1
        bonus = bonus_step * min(streak, bonus_cap)
        gain = base + bonus
        u["points"] = u["points"] + gain
        u["last_checkin"] = today
        u["streak"] = streak
        u["total"] = u["total"] + 1
        save_data(data)

        parts = [f"✅ 签到成功，{name} 获得 {gain} {cur}！"]
        if bonus > 0:
            parts.append(f"（基础 {base} + 连续 {streak} 天加成 {bonus}）")
        parts.append(f"当前余额：{u['points']} {cur}，累计签到 {u['total']} 天")
        return "\n".join(parts)

    # ── 积分查询 / 管理员维护 ──────────────────────────
    async def _cmd_points(self, msg):
        uid = msg.get("author")
        args = [str(a) for a in (msg.get("args") or [])]
        cur = str(self.ctx.config("currency", "积分"))
        if uid is None:
            return "无法识别你的身份 ID。"

        op = (args[0] if args else "").lower()
        if op in ("加", "add", "+", "奖励", "give"):
            return await self._adjust(uid, args, set_val=False, negate=False)
        if op in ("减", "sub", "-", "扣", "take"):
            return await self._adjust(uid, args, set_val=False, negate=True)
        if op in ("设", "set", "=", "赋", "设置"):
            return await self._adjust(uid, args, set_val=True, negate=False)

        data = load_data()
        u = ensure_user(data, uid)
        return (f"(met){uid}(met) 你的余额：{u['points']} {cur}\n"
                f"连续签到 {u['streak']} 天 · 累计 {u['total']} 天\n"
                f"管理员可用 `积分加/减/设 <ID> <数量>` 调整余额")

    async def _adjust(self, uid, args, set_val: bool, negate: bool) -> str:
        cur = str(self.ctx.config("currency", "积分"))
        if not _is_admin(uid, self.ctx.config("admins", [])):
            return "❌ 仅管理员可调整积分余额。"
        if len(args) < 3:
            return "用法：`.积分 加/减/设 <对方ID> <数量>`"
        target = args[1].strip()
        try:
            amount = int(args[2])
        except ValueError:
            return "❌ 数量必须是整数。"
        if amount < 0:
            return "❌ 数量不能为负，正数用 `减` 扣减。"
        if negate:
            amount = -amount
        data = load_data()
        t = ensure_user(data, target)
        if set_val:
            t["points"] = amount
        else:
            t["points"] = max(0, t["points"] + amount)
        save_data(data)
        verb = "已设" if set_val else ("已扣减" if amount < 0 else "已增加")
        return f"✅ 已对 `{target}` {verb} {abs(amount)} {cur}，当前 {t['points']} {cur}。"