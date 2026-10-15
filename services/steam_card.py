# -*- coding: utf-8 -*-
"""Steam 资料卡：数据组装 + 两种渲染器

  · `build_payload()`     取数据（唯一数据来源，两个渲染器共用）
  · `render_html()`       注入模板 → 自包含 HTML（设计稿对照 / 调试用）
  · `render_card()`       委托 services/steam_card_pillow（生产路径，Pillow 直绘）

为什么生产走 Pillow：服务器是 i3-2130，Chromium 单张卡约 900ms 且常驻近 400MB；
Pillow 约 20~80ms、内存几十 MB。实测见 docs（wdsj 卡片同一结论）。

模板：data/templates/steam_profile_card.html（视觉基准，改样子先改它，再改 Pillow）

注意：模板里的注入标记必须唯一 —— **注释里也不要写那两个标记的字面量**，
   否则 str.replace(..., 1) 会替换到注释那一处，整个数据 JSON 被塞进注释、
   真注入点空着（踩过，表现为"卡片上字段全是 —"）。下面有 assert 防呆。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path

from core.logger import get_logger
from services import steam_api as S

logger = get_logger("steam_card")

ROOT = Path(__file__).resolve().parent.parent
TPL_FILE = ROOT / "data" / "templates" / "steam_profile_card.html"
FONT_FILE = ROOT / "data" / "steam_assets" / "font" / "harmonyos_medium_latin.woff2"

# 展示哪款游戏的成就（先用 ADOFAI；将来可改成"成就最多的那款"）
ACH_APPID = 977950


def _data_uri(p) -> str:
    try:
        p = Path(p)
        if p.exists() and p.stat().st_size > 0:
            return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
    except Exception:
        pass
    return ""


def _png_uri(p) -> str:
    """PNG 版（logo 需要透明通道，不能按 jpeg 标 MIME）"""
    try:
        p = Path(p)
        if p.exists() and p.stat().st_size > 0:
            return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
    except Exception:
        pass
    return ""


BRAND_DIR = ROOT / "data" / "steam_assets" / "brand"


def brand_assets() -> dict:
    """Steam / Valve 官方 logo（浅色透明 PNG，抓自 store.akamai.steamstatic.com）"""
    return {
        "steam_logo": _png_uri(BRAND_DIR / "steam_header_logo.png"),
        "valve_logo": _png_uri(BRAND_DIR / "logo_valve.png"),
    }


async def _enrich(g: dict, key: str) -> dict:
    """一行游戏数据：中文名 + 时长 + 横幅图（并发取）"""
    aid = g.get("appid")
    name, img = await asyncio.gather(
        S.zh_name(aid, g.get("name", "")),
        S.game_header(aid),
    )
    return {
        "name": name or g.get("name", "") or ("appid " + str(aid)),
        "hours": round(g.get(key, 0) / 60.0, 1),
        "appid": aid,
        "img": _data_uri(img),
    }


async def _fetch_payload(steamid: str, top_n: int, recent_n: int, ach_n: int) -> dict:
    """真正去各处取数（任何一步抛异常都由 build_payload 兜住）"""
    prof = await S.player_summary(steamid)
    if not prof:
        return {}

    owned, recent_raw, level, badges, friends = await asyncio.gather(
        S.owned_games(steamid),
        S.recent_games(steamid, count=recent_n),
        S.steam_level(steamid),
        S.profile_badges(steamid),
        S.friend_count(steamid),
    )
    ach = await S.achievements(steamid, ACH_APPID)
    # 传入 ach 复用：recent_achievements 内部本会再查一次（跨境白花一趟）
    ach_recent = await S.recent_achievements(steamid, ACH_APPID, ach_n, ach_data=ach)
    # 成就图标（小图，并发下载并落盘缓存 → 转 data URI）
    if ach_recent:
        icons = await asyncio.gather(*[
            S.ach_icon(ACH_APPID, a.get("apiname", ""), a.get("icon_url", ""))
            for a in ach_recent
        ])
        for a, p in zip(ach_recent, icons):
            a["icon"] = _data_uri(p)

    games = owned.get("games") or []
    top = sorted(games, key=lambda g: g.get("playtime_forever", 0), reverse=True)[:top_n]
    recent = sorted(recent_raw, key=lambda g: g.get("playtime_2weeks", 0),
                    reverse=True)[:recent_n]

    # 先批量预取这十几款的中文名（一次请求搞定）——否则下面 _enrich 会逐个查，
    # 走代理时每趟跨境 1~2s，冷启动要多花十几秒
    await S.prefetch_zh_names([g.get("appid") for g in (top + recent)])

    # 库存价值（分批查 store，带 24h 缓存；免费/锁区游戏计入 unpriced）
    # 注意：必须在 games 定义**之后**调用（曾在定义前引用，报 UnboundLocalError）
    value = await S.library_value([g.get("appid") for g in games])

    top_items = list(await asyncio.gather(*[_enrich(g, "playtime_forever") for g in top]))
    recent_items = list(await asyncio.gather(*[_enrich(g, "playtime_2weeks") for g in recent]))
    # 头像只取一次（原来在 profile 里又 await 了一次 avatar_file）
    avatar_path = await S.avatar_file(steamid, prof.get("avatar", ""))
    avatar = _data_uri(avatar_path)

    created = ""
    if prof.get("created"):
        try:
            created = datetime.fromtimestamp(prof["created"]).strftime("%Y-%m-%d")
        except Exception:
            created = ""

    total_hours = round(sum(g.get("playtime_forever", 0) for g in games) / 60.0)
    # 游戏库概览（填满左栏用；全部本地可算，不额外请求）
    played = sum(1 for g in games if g.get("playtime_forever", 0) > 0)
    top_hours = (top[0].get("playtime_forever", 0) / 60.0) if top else 0.0
    return {
        "profile": {
            "name": prof.get("name", ""),
            "steamid": steamid,
            "level": level,
            "created": created,
            "online": prof.get("online", False),
            "state_kind": prof.get("state_kind", ""),
            "state_text": prof.get("state_text", ""),
            "game_now": prof.get("in_game", ""),
            "avatar": avatar,
            "avatar_path": str(avatar_path),
        },
        "stats": {
            "games": owned.get("count", len(games)),
            "hours": total_hours,
            "ach_got": ach.get("got", 0),
            "ach_total": ach.get("total", 0),
            "badges": badges.get("count"),
            "friends": friends,
            "xp": badges.get("xp"),
            "xp_cur": badges.get("xp_cur"),
            "xp_need": badges.get("xp_need"),
        },
        "recent": recent_items,
        "recent_sum": round(sum(g.get("playtime_2weeks", 0) for g in recent) / 60.0, 1),
        "top": top_items,
        "library": {
            "played": played,
            "unplayed": max(0, len(games) - played),
            "avg": round(total_hours / played, 1) if played else 0.0,
            "top_share": round(top_hours * 100.0 / total_hours, 1) if total_hours else 0.0,
        },
        "achievements": ach_recent,
        "value": value,
        # 完整 appid 列表也存进快照：这样以后 api 域断了、store 还通时，
        # 仍能重新查价格刷新"库存价值"（两个域名经常一个通一个不通）
        "appids": [g.get("appid") for g in games if g.get("appid")],
        "brand": brand_assets(),
        "ach_game": "冰与火之舞",
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ── 缓存快照（Steam 在国内时通时断，实测整段 API 会连续超时）──────
CACHE_DIR = ROOT / "data" / "steam_cache"
CACHE_MAX_AGE = 86400 * 3        # 3 天内的快照还能用
FRESH_TTL = 90.0                 # 秒：这么新的快照**直接出图**，同时后台刷新
                                 #   （stale-while-revalidate —— 重复查询毫秒级返回，
                                 #    代价是「正在玩」最多滞后 90 秒）
_refreshing: set = set()


def _cache_file(steamid: str) -> Path:
    return CACHE_DIR / ("%s.json" % steamid)


def _save_cache(steamid: str, payload: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_file(steamid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.debug("卡片缓存写入失败: %s", e)


def _load_cache(steamid: str) -> dict:
    """读上次成功的快照；过期或损坏返回 {}"""
    try:
        p = _cache_file(steamid)
        d = json.loads(p.read_text(encoding="utf-8"))
        ts = float(d.get("_cached_at") or 0)
        if not ts or (time.time() - ts) > CACHE_MAX_AGE:
            return {}
        d["stale"] = True
        d["stale_at"] = d.get("updated", "")
        d["stale_age_h"] = round((time.time() - ts) / 3600.0, 1)
        return d
    except Exception:
        return {}


def _load_raw(steamid: str):
    """读快照原始内容（**不**标 stale），返回 (payload, 距今年龄秒)"""
    try:
        d = json.loads(_cache_file(steamid).read_text(encoding="utf-8"))
        ts = float(d.get("_cached_at") or 0)
        return (d, time.time() - ts) if ts else (None, 1e9)
    except Exception:
        return None, 1e9


def _spawn_refresh(steamid: str, top_n: int, recent_n: int, ach_n: int) -> None:
    """后台刷新快照（不阻塞本次出图）；同一 steamid 同时只跑一个"""
    if steamid in _refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refreshing.add(steamid)

    async def _job():
        try:
            p = await _fetch_payload(steamid, top_n, recent_n, ach_n)
            if p:
                p["_cached_at"] = time.time()
                _save_cache(steamid, p)
                logger.debug("后台刷新快照完成 steamid=%s", steamid)
        except Exception as e:
            logger.debug("后台刷新快照失败: %s: %r", type(e).__name__, e)
        finally:
            _refreshing.discard(steamid)

    loop.create_task(_job())


async def build_payload(steamid: str, top_n: int = 10, recent_n: int = 8,
                        ach_n: int = 4) -> dict:
    """取全部要展示的数据。

    三级策略（按代价从低到高）：
      1. 快照在 FRESH_TTL 内 → **直接返回**（毫秒级出图），同时后台静默刷新
      2. 真取数：成功 → 落盘快照
      3. 取数失败 → 回退 3 天内的快照（payload["stale"]=True，
         渲染层在页脚标注"缓存数据 X 小时前"）；两者都不可用才返回 {}
    """
    raw, age = _load_raw(steamid)
    if raw and raw.get("profile") and age <= FRESH_TTL:
        raw["stale"] = False
        _spawn_refresh(steamid, top_n, recent_n, ach_n)
        logger.debug("命中新鲜快照（%.0fs 前），直接出图并后台刷新", age)
        return raw

    payload = {}
    # 先花几秒探测连通性：不通就直接走快照，别让用户干等 25s 超时
    if not await S.reachable():
        cached = _load_cache(steamid)
        if cached:
            logger.info("Steam api 域不可达，直接用缓存快照 steamid=%s（%.1f 小时前）",
                        steamid, cached.get("stale_age_h", 0))
            return cached
    try:
        payload = await _fetch_payload(steamid, top_n, recent_n, ach_n)
    except Exception as e:
        # 打全 traceback：这里曾经只打 str(e)，遇到消息为空的异常（超时类）
        # 完全看不出是哪一步断的
        logger.warning("Steam 取数失败（将尝试缓存）: %s: %r", type(e).__name__, e,
                       exc_info=True)
    if payload:
        payload["_cached_at"] = time.time()
        _save_cache(steamid, payload)
        return payload

    cached = _load_cache(steamid)
    if cached:
        logger.info("Steam 不可用，改用缓存快照 steamid=%s（%.1f 小时前）",
                    steamid, cached.get("stale_age_h", 0))
        return cached
    return {}


def render_html(payload: dict) -> str:
    """把 payload 注入自包含 HTML（字体与图片全部内联）—— 设计稿 / 调试用"""
    if not payload:
        return ""
    try:
        html = TPL_FILE.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("卡片模板读取失败: %s", e)
        return ""

    try:
        font_b64 = base64.b64encode(FONT_FILE.read_bytes()).decode()
        html = html.replace("__FONT_B64__", font_b64, 1)
    except Exception as e:
        logger.warning("字体内联失败（中文仍可回退渲染）: %s", e)

    js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("/*__CARD_DATA__*/null", js, 1)

    # 防呆：标记若还在，说明替换被别处的字面量抢走了
    if "__CARD_DATA__" in html or "__FONT_B64__" in html:
        logger.error("卡片注入失败：占位符仍在（检查模板注释里是否写了字面量）")
        return ""

    S.flush_zh()          # 中文名缓存落盘
    return html


async def build_profile_html(steamid: str, top_n: int = 10, recent_n: int = 8) -> str:
    """取数 + 生成 HTML（保留旧接口，供设计稿对照与探针使用）"""
    return render_html(await build_payload(steamid, top_n=top_n, recent_n=recent_n))


async def render_card(steamid: str, out_path, top_n: int = 10, recent_n: int = 8):
    """【生产路径】取数 + Pillow 直绘 → 保存图片，返回 Path（失败返回 None）"""
    from services import steam_card_pillow as CP

    payload = await build_payload(steamid, top_n=top_n, recent_n=recent_n)
    if not payload:
        return None
    try:
        img = CP.render_steam_card(payload, root=ROOT)
        S.flush_zh()
        return CP.save_steam_card(img, out_path)
    except Exception as e:
        logger.error("卡片渲染失败: %s", e, exc_info=True)
        return None
