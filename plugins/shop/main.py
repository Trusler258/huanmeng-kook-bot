"""
积分商店 插件（KOOK，独立于 points 插件运行）

功能（插件内注册命令，前缀 .）：
  商店   : .商店 / .shop                        商品列表
          兑换：.商店 buy <商品名> / .商店 购买 <商品名>（扣积分）
          维护(仅 admin)：
            .商店 add <名> <价格> [描述] | .商店 del <名> | .商店 price <名> <新价>

数据：
  - 商品清单   : plugins/shop/data.json      （本插件自己管理）
  - 积分余额   : plugins/points/data.json     （读/写 points 插件共享的余额数据源）

依赖：需要 points 插件先启用并产生余额，否则购买按 0 积分处理。
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

# 本插件数据文件
_SHOP_FILE = Path(__file__).resolve().parent / "data.json"
# points 插件共享的积分余额数据文件
_POINTS_FILE = Path(__file__).resolve().parent.parent / "points" / "data.json"
_LOCK = RLock()


# ── 商品读写 ──────────────────────────────────────────────

def _load_shop() -> list:
    with _LOCK:
        if _SHOP_FILE.exists():
            try:
                d = json.loads(_SHOP_FILE.read_text(encoding="utf-8"))
                if isinstance(d.get("shop"), list):
                    return d["shop"]
            except Exception:
                pass
        return []


def _save_shop(shop: list) -> None:
    with _LOCK:
        _SHOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SHOP_FILE.write_text(
            json.dumps({"shop": shop}, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 积分余额（共享 points 数据）──────────────────────────

def _load_points() -> dict:
    with _LOCK:
        if _POINTS_FILE.exists():
            try:
                d = json.loads(_POINTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                d = None
            if isinstance(d, dict) and isinstance(d.get("users"), dict):
                return d
        return {"users": {}}


def _save_points(data: dict) -> None:
    with _LOCK:
        _POINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POINTS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_user(data: dict, uid) -> dict:
    u = data["users"].setdefault(str(uid), {})
    u.setdefault("points", 0)
    return u


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

    async def _cmd_shop(self, msg):
        uid = msg.get("author")
        args = [str(a) for a in (msg.get("args") or [])]
        cur = str(self.ctx.config("currency", "积分"))
        shop = _load_shop()

        if not args or args[0].lower() in ("list", "列表", "全部"):
            return self._shop_list(shop, cur)

        action = args[0].lower()
        if action in ("buy", "购买", "换"):
            return self._shop_buy(msg, shop, cur)
        if action in ("add", "新", "新增"):
            return self._shop_add(uid, args, shop, cur)
        if action in ("del", "remove", "删", "删除"):
            return self._shop_del(uid, args, shop)
        if action in ("price", "改价", "set"):
            return self._shop_price(uid, args, shop, cur)
        return ("用法：\n`.商店` 商品列表\n"
                "`.商店 buy <商品名>` 兑换\n"
                "管理员：`.商店 add <名> <价格> [描述]` / `del <名>` / `price <名> <新价>`")

    def _shop_list(self, shop, cur) -> str:
        if not shop:
            return "🏪 商店还没有商品，等管理员 `.商店 add` 上架吧。"
        lines = [f"🏪 积分商店（{cur}兑换）："]
        for i, it in enumerate(shop, 1):
            desc = f" - {it['desc']}" if it.get("desc") else ""
            lines.append(f"{i}. `{it['name']}`　{it['price']} {cur}{desc}")
        lines.append("兑换：`.商店 buy <商品名>`")
        return "\n".join(lines)

    def _shop_buy(self, msg, shop, cur) -> str:
        uid = msg.get("author")
        name = " ".join(str(a) for a in (msg.get("args") or [])[1:]).strip()
        if not name:
            return "用法：`.商店 buy <商品名>`"
        item = next((it for it in shop if it["name"].lower() == name.lower()), None)
        if item is None:
            return f"❌ 商店里没有「{name}」，用 `.商店` 查看现有商品。"
        data = _load_points()
        u = _ensure_user(data, uid)
        price = int(item["price"])
        if u["points"] < price:
            return (f"❌ 积分不足 兑换「{item['name']}」需 {price} {cur}，"
                    f"你当前有 {u['points']} {cur}，还差 {price - u['points']}（先 `.签到` 攒积分）。")
        u["points"] -= price
        _save_points(data)
        desc = f"（{item['desc']}）" if item.get("desc") else ""
        return (f"🏪 兑换成功！你获得了「{item['name']}」{desc}\n"
                f"已扣除 {price} {cur}，剩余 {u['points']} {cur}。")

    def _shop_add(self, uid, args, shop, cur) -> str:
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
            return f"❌ 已存在同名商品「{it_name}」，改价用 `.商店 price`、删除用 `.商店 del`。"
        shop.append({"name": it_name, "price": price, "desc": " ".join(args[3:]).strip()})
        _save_shop(shop)
        return f"✅ 已上架「{it_name}」定价 {price} {cur}。用 `.商店` 查看。"

    def _shop_del(self, uid, args, shop) -> str:
        if not _is_admin(uid, self.ctx.config("admins", [])):
            return "❌ 仅管理员可下架商品。"
        if len(args) < 2:
            return "用法：`.商店 del <商品名>`"
        it_name = args[1]
        for it in shop[:]:
            if it["name"].lower() == it_name.lower():
                shop.remove(it)
                _save_shop(shop)
                return f"✅ 已下架「{it['name']}」。"
        return f"❌ 商店里没有「{it_name}」。"

    def _shop_price(self, uid, args, shop, cur) -> str:
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
                _save_shop(shop)
                return f"✅ 已将「{it_name}」改价为 {price} {cur}。"
        return f"❌ 商店里没有「{it_name}」。"