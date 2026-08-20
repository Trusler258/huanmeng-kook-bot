"""
经济能力：积分余额与权益库存的**唯一读写入口**。

设计目的（对应插件隔离指南）：
- 积分/库存数据（plugins/points/data.json）不应被各插件各自直读直改。
- 本模块持有一把核心级唯一锁（RLock），points/shop/sandbox 等所有写方
  统一收敛到这里完成读改写，从根上避免"各插件各拿一把不同的锁写同一文件"的丢更新。
- 写入采用临时文件 + os.replace 原子落盘，降低半写损坏风险。

对外暴露函数（同步、短、无 await，在 asyncio 单线程下天然不被协程打断）：
- get_points / add_points / set_points
- get_inventory / add_inventory / consume_inventory

插件通过 PluginContext.economy 访问本模块，禁止再直接 import 或改数据文件。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from core.logger import get_logger

logger = get_logger("economy")

# 核心级唯一锁：所有读写同一数据文件的地方都必须经本模块，共用这一把。
_LOCK = threading.RLock()

# 积分余额 + 权益库存的唯一数据源（与 points 插件共享，插件目录不上传更新，
# 仅服务器本地维护该文件；此处只负责读写它，路径保持与既有实现一致）。
_DATA_FILE = Path(__file__).resolve().parent.parent / "plugins" / "points" / "data.json"


# ── 内部：读 / 写（均持核心唯一锁）────────────────────────

def _load() -> dict:
    with _LOCK:
        if _DATA_FILE.exists():
            try:
                d = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("users"), dict):
                    return d
            except Exception as e:
                logger.warning("economy 读取 %s 失败: %s", _DATA_FILE, e)
        return {"users": {}}


def _save(data: dict) -> None:
    with _LOCK:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DATA_FILE.with_name(_DATA_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_DATA_FILE)


def _ensure_user(data: dict, uid) -> dict:
    u = data["users"].setdefault(str(uid), {})
    u.setdefault("points", 0)
    u.setdefault("inventory", {})
    return u


# ── 公开 API：积分 ──────────────────────────────────────

def get_points(uid) -> int:
    with _LOCK:
        return int(_ensure_user(_load(), uid)["points"])


def add_points(uid, delta: int, min_zero: bool = True) -> int:
    """给用户增减积分，返回新余额。min_zero=True 时余额不为负。"""
    with _LOCK:
        data = _load()
        u = _ensure_user(data, uid)
        u["points"] = max(0, u["points"] + delta) if min_zero else u["points"] + delta
        _save(data)
        return u["points"]


def set_points(uid, value: int) -> int:
    """设用户积分为指定值（最小 0），返回新余额。"""
    with _LOCK:
        data = _load()
        u = _ensure_user(data, uid)
        u["points"] = max(0, int(value))
        _save(data)
        return u["points"]


# ── 公开 API：权益库存 ──────────────────────────────────

def get_inventory(uid) -> dict:
    with _LOCK:
        return dict(_ensure_user(_load(), uid).get("inventory") or {})


def add_inventory(uid, effect: str, qty: int = 1) -> int:
    """给用户增加 N 次权益，返回当前库存。"""
    with _LOCK:
        data = _load()
        u = _ensure_user(data, uid)
        inv = u.setdefault("inventory", {})
        inv[effect] = int(inv.get(effect, 0)) + max(1, int(qty))
        _save(data)
        return inv[effect]


def consume_inventory(uid, effect: str) -> bool:
    """消耗 1 次权益；库存不足返回 False，成功扣除并返回 True。"""
    with _LOCK:
        data = _load()
        u = _ensure_user(data, uid)
        inv = u.setdefault("inventory", {})
        n = int(inv.get(effect, 0))
        if n <= 0:
            return False
        if n == 1:
            inv.pop(effect, None)
        else:
            inv[effect] = n - 1
        _save(data)
        return True