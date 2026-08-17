"""
每日签到 + 积分系统 + 积分商店 插件（KOOK，Huanmeng 2.0）

功能（插件内注册命令，前缀 .）：
  签到   : .签到 / .checkin / .sign         每日签到得积分（含连续签到加成）
  积分   : .积分 / .points                  查看本人积分余额/连续/Nick
          管理员维护：.积分 加 <ID> <数量> | .积分 减 <ID> <数量> | .积分 设 <ID> <数量>
  商店   : .商店 / .shop                    商品列表
          购买：.商店 buy <商品名> / .商店 购买 <商品名>
          维护(仅 admin)：.商店 add <名> <价格> [描述] | .商店 del <名> | .商店 price <名> <新价格>

数据：持久化到本插件目录 data.json（plugins/checkin/data.json）。
      该目录被 .update 与插件加载器按插件隔离，数据不会随更新覆盖。

管理员判定：core 配置的 admin 或 manifest.config.admins 中列出的 ID。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional

_DATA_FILE = Path(__file__).resolve().parent / "data.json"
_LOCK = RLock()


# ── 数据读写（线程安全）────────────────────────────────────

def _load() -> dict:
    with _LOCK:
        if _DATA_FILE.exists():
            try:
                return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"users": {}, "shop": []}


def _save(data: dict) -> None:
    with _LOCK:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_user(data: dict, uid) -> dict:
    u = data["users"].setdefault(str(uid), {})
    u.setdefault("points", 0)
    u.setdefault("last_checkin", "")
    u.setdefault("streak", 0)
    u.setdefault("total", 0)
    return u


def _date(tz_hours: int) -> str:
    base = datetime.utcnow() + timedelta(hours=max(-23, min(23, tz_hours)))
    return base.strftime("%Y-%m-%d")


def _yesterday(tz_hours: int) -> str:
    base = datetime.utcnow() + timedelta(hours=max(-23, min(23, tz_hours)))
    return (base - timedelta(days=1)).strftime("%Y-%m-%d")


def _cur(tz_hours: int) -> str:
    base = datetime.utcnow() + timedelta(hours=max(-23, min(23, tz_hours)))
    return base.strftime("%H:%M")


# ── 管理员判定 ────────────────────────────────────────────

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
        for name in ("shop", "商店"):
            self.ctx.capability.register_command(
                name=name, description="积分商店：.商店 buy <商品>",
                handler=self._cmd_shop)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    # ── 签到 ────────────────────────────────────────────
    async def _cmd_checkin(self, msg):
        uid = msg.get("author")
        mid = msg.get("chat_id")
        is_group = bool(msg.get("is_group"))
        name = str(msg.get("sender") or uid)
        if uid is None:
            return "无法识别你的身份 ID。"

        base = int(self.ctx.config("base_reward", 100))
        bonus_step = int(self.ctx.config("streak_bonus", 20))
        bonus_cap = int(self.ctx.config("streak_bonus_cap", 5))
        cur_label = str(self.ctx.config("currency", "积分"))
        tz = int(self.ctx.config("tz_hours", 8))
        today = _date(tz)

        data = _load()
        u = _ensure_user(data, uid)
        if u["last_checkin"] == today:
            remaining = "已在今天签过到"
            return (f"(met){uid}(met) 你今天已经签到过啦，明天 {today} 之后再来"
                    f"（连续 {u['streak']} 天，当前 {u['points']} {cur_label}）")
        # 计算连续天数
        if u["last_checkin"] == _yesterday(tz):
            streak = u["streak"] + 1
        else:
            streak = 1
        bonus = bonus_step * min(streak, bonus_cap)
        gain = base + bonus
        u["points"] += gain
        u["last_checkin"] = today
        u["streak"] = streak
        u["total"] = u["total"] + 1
        _save(data)

        parts = [f"✅ 签到成功，{name} 获得 {gain} {cur_label}！"]
        if bonus > 0:
            parts.append(f"（基础 {base} + 连续 {streak} 天加成 {bonus}）")
        parts.append(f"当前余额：{u['points']} {cur_label}，累计签到 {u['total']} 天")
        return "\n".join(parts)

    # ── 积分查询 / 维护 ────────────────────────────────
    async def _cmd_points(self, msg):
        uid = msg.get("author")
        args = [str(a) for a in (msg.get("args") or [])]
        cur_label = str(self.ctx.config("currency", "积分"))
        if uid is None:
            return "无法识别你的身份 ID。"

        # 管理员维护：加/减/设
        op = (args[0] if args else "").lower()
        if op in ("加", "add", "+", "奖励", "give"):
            return await self._points_adjust(uid, args, delta=True, set_val=False)
        if op in ("减", "sub", "-", "扣", "take"):
            return await self._points_adjust(uid, args, delta=True, set_val=False, negate=True)
        if op in ("设", "set", "=", "赋", "设置"):
            return await self._points_adjust(uid, args, delta=False, set_val=True)

        data = _load()
        u = _ensure_user(data, uid)
        return (f"(met){uid}(met) 你的余额：{u['points']} {cur_label}\n"
                f"连续签到 {u['streak']} 天 · 累计 {u['total']} 天\n"
                f"发 `积分加/减/设 <ID> <数量>` 可给他人调整（仅管理员）")

    async def _points_adjust(self, uid, args, delta: bool, set_val: bool, negate: bool = False):
        cur_label = str(self.ctx.config("currency", "积分"))
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
        data = _load()
        t = _ensure_user(data, target)
        if set_val:
            t["points"] = amount
        else:
            t["points"] = max(0, t["points"] + amount)
        _save(data)
        verb = "已设" if set_val else ("已扣减" if amount < 0 else "已增加")
        return f"✅ 已对 `{target}` {verb} {abs(amount)} {cur_label}，当前 {t['points']} {cur_label}。"

    # ── 商店 ────────────────────────────────────────────
    async def _cmd_shop(self, msg):
        uid = msg.get("author")
        args = [str(a) for a in (msg.get("args") or [])]
        cur_label = str(self.ctx.config("currency", "积分"))
        data = _load()
        shop = data["shop"]

        if not args or args[0].lower() in ("list", "列表", "全部"):
            return self._shop_list(uid, shop, cur_label)

        action = args[0].lower()
        if action in ("buy", "购买", "换"):
            return self._shop_buy(msg, shop, cur_label)
        if action in ("add", "新", "新增"):
            return self._shop_add(uid, args, shop, cur_label)
        if action in ("del", "remove", "删", "删除"):
            return self._shop_del(uid, args, shop)
        if action in ("price", "改价", "set"):
            return self._shop_price(uid, args, shop, cur_label)
        return ("用法：\n`.商店` 商品列表\n"
                "`.商店 buy <商品名>` 兑换\n"
                "管理员：`.商店 add <名> <价格> [描述]` / `del <名>` / `price <名> <新价>`")

    def _shop_list(self, uid, shop, cur_label) -> str:
        if not shop:
            return "🏪 商店还没有商品，等管理员 `.商店 add` 上架吧。"
        lines = [f"🏪 积分商店（{cur_label}兑换）："]
        for i, it in enumerate(shop, 1):
            desc = f" - {it['desc']}" if it.get("desc") else ""
            lines.append(f"{i}. `{it['name']}`　{it['price']} {cur_label}{desc}")
        lines.append("兑换：`.商店 buy <商品名>`")
        return "\n".join(lines)

    def _shop_buy(self, msg, shop, cur_label) -> str:
        uid = msg.get("author")
        name = " ".join(str(a) for a in (msg.get("args") or [])[1:]).strip()
        if not name:
            return "用法：`.商店 buy <商品名>`"
        item = next((it for it in shop if it["name"].lower() == name.lower()), None)
        if item is None:
            return f"❌ 商店里没有「{name}」，用 `.商店` 查看现有商品。"
        data = _load()
        u = _ensure_user(data, uid)
        price = int(item["price"])
        if u["points"] < price:
            return (f"❌ 余额不足 兑换「{item['name']}」需 {price} {cur_label}，"
                    f"你当前有 {u['points']} {cur_label}，还差 {price - u['points']}。")
        u["points"] -= price
        _save(data)
        desc = f"（{item['desc']}）" if item.get("desc") else ""
        return (f"🏪 兑换成功！你获得了「{item['name']}」{desc}\n"
                f"已扣除 {price} {cur_label}，剩余 {u['points']} {cur_label}。")

    def _shop_add(self, uid, args, shop, cur_label) -> str:
        if not _is_admin(uid, self.ctx.config("admins", [])):
            return "❌ 仅管理员可上架商品。"
        if len(args) < 3:
            return "用法：`.商店 add <商品名> <价格> [描述]`"
        it_name = args[1]
        try:
            price = int(args[2])
        except ValueError:
            return "❌ 价格必须是整数。"
        if any(it["name"].lower() == it_name.lower() for it in shop):
            return f"❌ 已存在同名商品「{it_name}」，如需改价用 `.商店 price`、删除用 `.商店 del`。"
        desc = " ".join(args[3:]).strip()
        shop.append({"name": it_name, "price": price, "desc": desc})
        _save(self._cur_data(shop))
        return f"✅ 已上架「{it_name}」定价 {price} {cur_label}。用 `.商店` 查看。"

    def _shop_del(self, uid, args, shop) -> str:
        if not _is_admin(uid, self.ctx.config("admins", [])):
            return "❌ 仅管理员可下架商品。"
        if len(args) < 2:
            return "用法：`.商店 del <商品名>`"
        it_name = args[1]
        for it in shop[:]:
            if it["name"].lower() == it_name.lower():
                shop.remove(it)
                _save(self._cur_data(shop))
                return f"✅ 已下架「{it['name']}」。"
        return f"❌ 商店里没有「{it_name}」。"

    def _shop_price(self, uid, args, shop, cur_label) -> str:
        if not _is_admin(uid, self.ctx.config("admins", [])):
            return "❌ 仅管理员可改价。"
        if len(args) < 3:
            return "用法：`.商店 price <商品名> <新价格>`"
        it_name = args[1]
        try:
            price = int(args[2])
        except ValueError:
            return "❌ 价格必须是整数。"
        for it in shop:
            if it["name"].lower() == it_name.lower():
                it["price"] = price
                _save(self._cur_data(shop))
                return f"✅ 已将「{it_name}」改价为 {price} {cur_label}。"
        return f"❌ 商店里没有「{it_name}」。"

    @staticmethod
    def _cur_data(shop) -> dict:
        # 仅改 shop 时保留 users 原样
        with _LOCK:
            d = _load()
        d["shop"] = shop
        return d