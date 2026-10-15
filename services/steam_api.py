# -*- coding: utf-8 -*-
"""Steam Web API 数据层（/~steam 系列指令用）

职责：只管「拿数据」，不管展示。展示在 modules/steam.py，卡片在 services/steam_card.py。

指令入口：/~steam price|px|who|bd|help、/~在干嘛

注意：三条实测经验（都踩过，改这个文件前先读）：
  1. 查账号**必须**用 SteamID64 或资料链接，绝不用昵称猜 vanity ——
     曾用 ResolveVanityURL?vanityurl=trusler 解析到一个陌生人（他抢注了这个自定义 URL），
     白排查一小时并误导用户去改本来没问题的隐私设置。
  2. `communityvisibilitystate == 3`（资料页公开）**不等于**游戏库可见，两者是独立门槛，
     判断库是否可见只能实测 GetOwnedGames（返回 15 字节 `{"response":{}}` = 不可见）。
  3. `store.steampowered.com` 在国内时通时断（实测 WinError 10060），
     而服务器出口稳定 —— bot 本来就在服务器上跑，所以这里不需要特殊处理，
     但**别在本地开发机上期望它每次都通**。

key 位置：config/.env 的 STEAM_KEY（必需）；ITAD_KEY（可选，用于历史最低价）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("steam")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENV_FILE = ROOT / "config" / ".env"
ASSETS = DATA / "steam_assets"
BIND_FILE = DATA / "steam_bind.json"
NAMES_ZH = ASSETS / "games" / "names_zh.json"
PRICE_HIST = DATA / "steam_price_history.json"

API = "https://api.steampowered.com"
STORE = "https://store.steampowered.com"
CDN_HEADER = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
# 头像：avatars.steamstatic.com 在部分网络不通，fastly 镜像实测可用
AVATAR_HOSTS = ("avatars.fastly.steamstatic.com", "avatars.cloudflare.steamstatic.com")
UA = {"User-Agent": "Mozilla/5.0"}

STATE_TXT = {0: "离线", 1: "在线", 2: "忙碌", 3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏"}


# ── 配置 ───────────────────────────────────────────────────────

def _env(name: str) -> str:
    """先看环境变量，再兜底解析 config/.env（bot 主进程不一定 load_dotenv）"""
    import os
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def steam_key() -> str:
    return _env("STEAM_KEY")


def itad_key() -> str:
    return _env("ITAD_KEY")


def has_key() -> bool:
    return bool(steam_key())


# ── HTTP ──────────────────────────────────────────────────────
#
# 两条取数通道（配了 STEAM_PROXY 走 Worker，否则一切照旧直连）：
#   直连：服务器 → Steam（国内时通时断，不通时每个请求干等 25~30s 超时）
#   代理：服务器 → CF Worker（境外边缘）→ Steam；Worker 源码 deploy/cf-steam-proxy/
#
# 实测收益（92 款游戏价格）：直连 75,013 ms → 代理 3,873 ms，
# 且有价覆盖 37/92 → 72/92（Worker 侧到 Steam 更稳，拿得全）。

_PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 共享 AsyncClient：跨境每趟 TLS 握手要 1~2s，复用连接后同一批请求能省掉大部分。
# 注意 httpx.AsyncClient 绑定 event loop —— 探针里多次 asyncio.run 会换 loop，
# 所以检测到 loop 变化就重建（否则报 "attached to a different loop"）。
_client = None
_client_loop = None


def _http_client():
    import asyncio
    import httpx
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(
            timeout=30.0, trust_env=False, verify=False, headers=UA,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16,
                                keepalive_expiry=90.0))
        _client_loop = loop
    return _client


def proxy_url() -> str:
    return _env("STEAM_PROXY").rstrip("/")


def proxy_token() -> str:
    return _env("STEAM_PROXY_TOKEN")


def proxy_on() -> bool:
    return bool(proxy_url())


def _split_url(url: str):
    """整条 Steam URL → Worker 的 (kind, path)；非 api/store 域名返回 (None, "")"""
    if url.startswith(API):
        return "api", url[len(API):].lstrip("/")
    if url.startswith(STORE):
        return "store", url[len(STORE):].lstrip("/")
    return None, ""


async def _proxy_call(ops: list, timeout: float = 60.0) -> list:
    """一次 POST 打多个 op 到 Worker（Worker 内部并发执行），按序返回结果

    合并请求是这里的关键：跨境每趟 1~2s，若一个 Steam 请求占一趟，
    一张卡片几十趟反而比直连更慢。
    """
    headers = {"Content-Type": "application/json", "User-Agent": _PROXY_UA}
    tk = proxy_token()
    if tk:
        headers["X-Proxy-Token"] = tk
    c = _http_client()
    r = await c.post(proxy_url(), json={"ops": ops}, headers=headers, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError("proxy 拒绝: %s" % d.get("error"))
    return d.get("r") or []


async def _get_json(url: str, params: Optional[dict] = None, timeout: float = 25.0):
    """GET → JSON

    配了 STEAM_PROXY 时改走 Worker —— **key 交给 Worker 注入，服务器不再外传**；
    否则直连 Steam（trust_env=False：绕过机器上的 http_proxy）。
    """
    if proxy_on():
        kind, path = _split_url(url)
        if kind:
            q = dict(params or {})
            q.pop("key", None)          # Worker 侧持 key，不必往外传
            rs = await _proxy_call([{"k": kind, "p": path, "q": q}],
                                   timeout=max(20.0, timeout + 10))
            r0 = rs[0] if rs else {}
            if r0.get("ok") and r0.get("data") is not None:
                return r0["data"]
            raise RuntimeError("proxy op 失败: %s"
                               % (r0.get("error") or r0.get("status")))
    r = await _http_client().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def _get_bytes(url: str, timeout: float = 30.0) -> bytes:
    r = await _http_client().get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


async def _api(path: str, **params):
    params["key"] = steam_key()
    return await _get_json(f"{API}/{path}", params)


async def reachable(timeout: float = 5.0) -> bool:
    """轻量探测取数通道是否可用。

    为什么需要它：Steam 对国内断的时候，第一个业务请求要干等 25s 超时才失败，
    整条「取数 → 渲染」白等半分钟。先花几秒探一下，不通就直接走缓存快照。
    配了代理就探 Worker 的健康检查（它自带到 Steam 的通路）。
    """
    try:
        if proxy_on():
            r = await _http_client().get(proxy_url() + "/", timeout=timeout)
            r.raise_for_status()
            return bool((r.json() or {}).get("has_steam_key"))
        await _get_json(f"{API}/ISteamWebAPIUtil/GetServerInfo/v1/", timeout=timeout)
        return True
    except Exception as e:
        logger.debug("Steam 取数通道不可用: %s: %r", type(e).__name__, e)
        return False


# ── 绑定（QQ ↔ SteamID）──────────────────────────────────────

def _load_bind() -> dict:
    try:
        return json.loads(BIND_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_bind(m: dict) -> None:
    BIND_FILE.parent.mkdir(parents=True, exist_ok=True)
    BIND_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def get_bind(qq) -> str:
    return _load_bind().get(str(qq), "")


def set_bind(qq, steamid: str) -> None:
    m = _load_bind()
    m[str(qq)] = str(steamid)
    _save_bind(m)


def del_bind(qq) -> bool:
    m = _load_bind()
    if str(qq) in m:
        m.pop(str(qq))
        _save_bind(m)
        return True
    return False


# ── 解析用户输入的账号 ────────────────────────────────────────

_RE_STEAMID64 = re.compile(r"\b(7656119\d{10})\b")
_RE_PROFILES = re.compile(r"steamcommunity\.com/profiles/(\d+)")
_RE_ID = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_\-\.]+)")
_RE_APP = re.compile(r"/app/(\d+)")


def extract_appid(text: str) -> str:
    """从商店链接抠 appid（/~steam price <链接>）"""
    m = _RE_APP.search(text or "")
    return m.group(1) if m else ""


# ── 账号标识换算 ───────────────────────────────────────────────
# SteamID64 = 76561197960265728 + account_id，而 **好友代码就是 account_id**
# （Steam 客户端「好友代码」显示的那串数字）。
# ⚠️ 该公式在本机无法联网核实（服务器到 steamcommunity.com 不通，CF Worker 也只放行
#    steamapi/store）。因此 resolve_steamid 会对换算结果**真的查一次**
#    （GetPlayerSummaries），查不到就明确报错、绝不返回错数据。
_STEAM64_BASE = 76561197960265728

_RE_STEAM2 = re.compile(r"\bSTEAM_([0-5]):([01]):(\d+)\b", re.I)
_RE_STEAM3 = re.compile(r"^\[?U:1:(\d+)\]?$", re.I)
_RE_DIGITS = re.compile(r"^\d{1,10}$")

# ── 好友代码（字母形式）────────────────────────────────────────
# Steam 客户端「好友代码」/ `steamcommunity.com/user/<code>` 里那串**字母**，
# 它**不是** account_id 的十进制，而是 account_id 的**十六进制**再按固定表替换字符：
#   hex → letter: 0=b 1=c 2=d 3=f 4=g 5=h 6=j 7=k 8=m 9=n a=p b=q c=r d=t e=v f=w
# 来源：Dr. McKay《Translate new /user/ links》；已实测验证：
#   Trusler 的 account_id 1467315295 → hex 5775745f → hkkhkghw
#   取 https://steamcommunity.com/user/hkkhkghw 返回 <title>Steam Community :: Trusler</title>
# ⚠️ 第一版误以为好友代码就是 account_id 十进制，拿 /user/1467315295 去试 → Steam 返回 Error 页。
_FC_ALPHABET = "bcdfghjkmnpqrtvw"                 # 下标 = hex 值 0..f
_FC_LETTER2HEX = {ch: format(i, "x") for i, ch in enumerate(_FC_ALPHABET)}
_FC_HEX2LETTER = {format(i, "x"): ch for i, ch in enumerate(_FC_ALPHABET)}
_RE_FRIEND_CODE = re.compile(r"^[bcdfghjkmnpqrtvw\-]{4,12}$", re.I)


def friend_code_to_account_id(code: str) -> str:
    """字母好友代码 → account_id（十进制字符串）；非法返回 "" """
    t = re.sub(r"[\s\-]", "", (code or "")).lower()
    if not t or len(t) > 8:
        return ""
    hexs = ""
    for ch in t:
        if ch not in _FC_LETTER2HEX:
            return ""
        hexs += _FC_LETTER2HEX[ch]
    try:
        return str(int(hexs, 16))
    except Exception:
        return ""


def account_id_to_friend_code(acc) -> str:
    """account_id（十进制）→ 字母好友代码"""
    try:
        return "".join(_FC_HEX2LETTER[c] for c in format(int(acc), "x"))
    except Exception:
        return ""


def steamid64_to_account_id(steamid) -> str:
    try:
        v = int(str(steamid)) - _STEAM64_BASE
        return str(v) if v > 0 else ""
    except Exception:
        return ""


def friend_code_to_steamid64(code) -> str:
    """好友代码（= account_id）→ SteamID64"""
    return str(_STEAM64_BASE + int(code))


def steamid64_to_friend_code(steamid) -> str:
    """SteamID64 → 好友代码（个人资料页/客户端能直接用的那串短数字）"""
    try:
        return str(int(str(steamid)) - _STEAM64_BASE)
    except Exception:
        return ""


def extract_steamid(text: str) -> str:
    """从输入里抠 SteamID64（保留旧名）：profiles 链接 / 17 位 SteamID64"""
    t = (text or "").strip()
    m = _RE_PROFILES.search(t) or _RE_STEAMID64.search(t)
    return m.group(1) if m else ""


def extract_identity(text: str) -> tuple:
    """把输入解析成 (steamid64, 来源说明)；认不出返回 ("", "")。

    支持（纯本地换算，不需要网络）：
      · SteamID64            76561199427581023
      · 好友代码（字母）     hkkhkghw / hkk-hkghw  ← 本次新增（Steam 客户端显示的那串）
      · 数字部分             1467315295            ← account_id（SteamID3 的数字部分）
      · SteamID2             STEAM_0:1:731657647
      · SteamID3             [U:1:1467315295] / U:1:1467315295
      · 资料链接             https://steamcommunity.com/profiles/76561199427581023
    """
    t = (text or "").strip()
    if not t:
        return "", ""

    m = _RE_PROFILES.search(t)
    if m:
        return m.group(1), "资料链接"

    m = _RE_STEAMID64.search(t)
    if m:
        return m.group(1), "SteamID64"

    # SteamID2: STEAM_X:Y:Z → account_id = Z*2 + Y
    m = _RE_STEAM2.search(t)
    if m:
        acc = int(m.group(3)) * 2 + int(m.group(2))
        return str(_STEAM64_BASE + acc), "SteamID2"

    # SteamID3: [U:1:account_id]
    m = _RE_STEAM3.search(t)
    if m:
        return str(_STEAM64_BASE + int(m.group(1))), "SteamID3"

    # 字母形式的好友代码（Steam 客户端显示的那串，如 hkkhkghw / hkk-hkghw）
    if _RE_FRIEND_CODE.match(t):
        acc = friend_code_to_account_id(t)
        if acc:
            return str(_STEAM64_BASE + int(acc)), "好友代码"

    # 纯数字（≤10 位且不是 17 位 SteamID64）→ account_id（SteamID3 的数字部分）
    if _RE_DIGITS.match(t):
        return str(_STEAM64_BASE + int(t)), "SteamID 数字部分"

    return "", ""


def extract_vanity(text: str) -> str:
    """从 steamcommunity.com/id/xxx 链接里抠自定义 URL"""
    m = _RE_ID.search(text or "")
    return m.group(1) if m else ""


async def resolve_steamid(text: str) -> tuple:
    """把用户输入解析成 SteamID64

    返回 (steamid, 错误提示)。支持：好友代码 / SteamID64 / SteamID2 / SteamID3 /
    profiles 链接 / id/xxx 自定义 URL。
    注意：不猜昵称 —— 那是踩过的坑。
    """
    if not steam_key():
        return "", "未配置 STEAM_KEY"
    t = (text or "").strip()
    if not t:
        return "", "没给账号"
    sid, _src = extract_identity(t)
    if sid:
        return sid, ""
    vanity = extract_vanity(t)
    if vanity:
        try:
            r = await _api("ISteamUser/ResolveVanityURL/v1/", vanityurl=vanity)
            res = r.get("response", {})
            if res.get("success") == 1:
                return res.get("steamid", ""), ""
            return "", f"这个自定义 URL「{vanity}」在 Steam 上不存在"
        except Exception as e:
            logger.warning("ResolveVanityURL 失败: %s", e)
            return "", "解析自定义 URL 失败（网络问题？）"
    if re.fullmatch(r"[A-Za-z0-9_\-\.]{2,32}", t):
        # 看着像自定义 URL：只在用户明确给的就是这个名字时尝试（结果会标注来源）
        try:
            r = await _api("ISteamUser/ResolveVanityURL/v1/", vanityurl=t)
            res = r.get("response", {})
            if res.get("success") == 1:
                return res.get("steamid", ""), ""
        except Exception:
            pass
        return "", (f"把「{t}」当成自定义 URL 查了，没找到对应账号\n"
                    "  也可以直接给 SteamID64 或好友代码")
    # ⚠️ 旧文案把好友代码写成"那串数字，如 1467315295"——错的：
    #   好友代码是**字母**（account_id 的十六进制 + 固定字符替换），
    #   1467315295 是 account_id 本身。别再混为一谈。
    return "", ("认不出这是账号喵~ 可以给这几种：\n"
                "  · 好友代码（Steam 好友列表里那串字母，如 hkkhkghw）\n"
                "  · account_id（9~10 位数字，如 1467315295）\n"
                "  · SteamID64（17 位数字，个人资料页链接里那串）\n"
                "  · SteamID2/3（STEAM_1:1:733657647 · [U:1:1467315295]）\n"
                "  · 个人资料链接（steamcommunity.com/profiles/... 或 /id/...）")


# ── 玩家数据 ──────────────────────────────────────────────────

async def player_summary(steamid: str) -> dict:
    """昵称 / 头像 / 在线状态 / 正在玩什么（私有账号也能拿到基本字段）"""
    r = await _api("ISteamUser/GetPlayerSummaries/v2/", steamids=steamid)
    players = (r.get("response") or {}).get("players") or []
    if not players:
        return {}
    p = players[0]
    game = p.get("gameextrainfo") or ""
    online = bool(p.get("personastate", 0)) or bool(game)
    return {
        "steamid": p.get("steamid", steamid),
        "name": p.get("personaname", ""),
        "avatar": p.get("avatarfull") or p.get("avatarmedium") or "",
        "online": online,
        "in_game": game,
        "state_text": ("正在玩 " + game) if game else STATE_TXT.get(p.get("personastate"), "离线"),
        "state_kind": "ingame" if game else ("" if online else "off"),
        "visible": p.get("communityvisibilitystate") == 3,
        "created": p.get("timecreated", 0),
        "lastlogoff": p.get("lastlogoff", 0),
    }


async def owned_games(steamid: str) -> dict:
    """游戏库 + 时长

    注意：不加 include_appinfo：那个响应大 10 倍（实测 15KB 要 100s），
    游戏名走 zh_name() 单独查（反正中文名也要查 store）。
    """
    r = await _api("IPlayerService/GetOwnedGames/v1/", steamid=steamid,
                   include_played_free_games=1)
    resp = r.get("response") or {}
    return {"count": resp.get("game_count", len(resp.get("games") or [])),
            "games": resp.get("games") or [],
            "visible": bool(resp.get("games"))}


async def recent_games(steamid: str, count: int = 6) -> list:
    r = await _api("IPlayerService/GetRecentlyPlayedGames/v1/", steamid=steamid,
                   count=count)
    return (r.get("response") or {}).get("games") or []


async def steam_level(steamid: str) -> Optional[int]:
    try:
        r = await _api("IPlayerService/GetSteamLevel/v1/", steamid=steamid)
        return (r.get("response") or {}).get("player_level")
    except Exception:
        return None


async def achievements(steamid: str, appid: int) -> dict:
    """玩家成就。资料不公开时 Valve 返回 403 + "Profile is not public" """
    try:
        r = await _api("ISteamUserStats/GetPlayerAchievements/v1/",
                       steamid=steamid, appid=appid)
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            return {"error": "profile_private", "got": 0, "total": 0, "list": []}
        return {"error": str(e), "got": 0, "total": 0, "list": []}
    ach = (r.get("playerstats") or {}).get("achievements") or []
    return {"error": "", "got": sum(1 for a in ach if a.get("achieved")),
            "total": len(ach), "list": ach}


async def friend_count(steamid: str) -> Optional[int]:
    """好友数。资料/好友列表不公开时返回 None（展示成 —，不是 0）"""
    try:
        r = await _api("ISteamUser/GetFriendList/v1/", steamid=steamid,
                       relationship="friend")
    except Exception as e:
        logger.debug("好友列表不可用 %s: %s", steamid, e)
        return None
    fl = (r.get("friendslist") or {}).get("friends")
    return len(fl) if fl is not None else None


async def profile_badges(steamid: str) -> dict:
    """徽章数 + 等级 XP 进度。

    XP 三个字段的语义（容易搞混）：
      player_xp                       总 XP
      player_xp_needed_current_level  当前等级起点所需 XP
      player_xp_needed_to_level_up    距下一级还需多少 XP
    → 本级的已得 XP = player_xp - player_xp_needed_current_level
    """
    try:
        r = await _api("IPlayerService/GetBadges/v1/", steamid=steamid)
    except Exception as e:
        logger.debug("徽章数据不可用 %s: %s", steamid, e)
        return {}
    b = r.get("response") or {}
    if not b:
        return {}
    total = b.get("player_xp")
    base = b.get("player_xp_needed_current_level")
    need = b.get("player_xp_needed_to_level_up")
    cur = None
    if isinstance(total, int) and isinstance(base, int):
        cur = max(0, total - base)
    return {"count": len(b.get("badges") or []), "xp": total, "level": b.get("player_level"),
            "xp_cur": cur, "xp_need": need}


# 成就定义缓存：schema 一天都不会变，进程内存缓存足够（重启即失效）
_schema_cache: dict = {}
ACH_DIR = DATA / "steam_assets" / "ach"


async def ach_schema(appid) -> dict:
    """成就定义 apiname -> {name, desc, hidden, icon}，中文优先（l=schinese）"""
    key = str(appid)
    if key in _schema_cache:
        return _schema_cache[key]
    m: dict = {}
    try:
        r = await _api("ISteamUserStats/GetSchemaForGame/v2/", appid=appid, l="schinese")
        st = ((r.get("game") or {}).get("availableGameStats") or {})
        for a in st.get("achievements") or []:
            an = a.get("name")
            if an:
                m[an] = {"name": a.get("displayName") or an,
                         "desc": a.get("description") or "",
                         "hidden": bool(a.get("hidden")),
                         "icon": a.get("icon") or ""}
    except Exception as e:
        logger.warning("成就定义获取失败 appid=%s: %s", appid, e)
    _schema_cache[key] = m
    return m


async def ach_icon(appid, apiname: str, url: str) -> Optional[Path]:
    """成就图标（小图，缓存到 data/steam_assets/ach/）

    schema 给的域名是 steamcdn-a.akamaihd.net，国内不稳；
    换 cloudflare / fastly 两个 CDN 镜像试（头像那边也是同一套路）。
    """
    if not url:
        return None
    try:
        ACH_DIR.mkdir(parents=True, exist_ok=True)
        out = ACH_DIR / ("%s_%s.jpg" % (appid, apiname))
        if out.exists() and out.stat().st_size > 0:
            return out
        for host in ("cdn.cloudflare.steamstatic.com", "cdn.akamai.steamstatic.com"):
            try:
                u = re.sub(r"^https?://[^/]+", "https://" + host, url)
                raw = await _get_bytes(u, timeout=20)
                if raw:
                    out.write_bytes(raw)
                    return out
            except Exception as e:
                logger.debug("成就图标下载失败 %s %s: %s", host, apiname, e)
    except Exception as e:
        logger.debug("成就图标缓存失败 %s: %s", apiname, e)
    return None


async def recent_achievements(steamid: str, appid, limit: int = 4, ach_data=None) -> list:
    """最近解锁的成就（按 unlocktime 倒序）。拿不到返回空列表

    `ach_data` 可传入已有的 achievements() 结果复用 —— 否则这里会**再查一次**
    （卡片取数时上游刚查过，重复一趟跨境请求白花 1~4s）。
    """
    res = ach_data if ach_data else await achievements(steamid, appid)
    lst = [a for a in (res.get("list") or []) if a.get("achieved")]
    if not lst:
        return []
    lst.sort(key=lambda x: x.get("unlocktime", 0), reverse=True)
    sch = await ach_schema(appid)
    out = []
    for a in lst[:limit]:
        apiname = a.get("apiname") or ""
        info = sch.get(apiname) or {}
        ts = a.get("unlocktime") or 0
        out.append({
            "apiname": apiname,
            "name": info.get("name") or apiname,
            "date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
            "ts": ts,
            "icon_url": info.get("icon") or "",
        })
    return out


# ── 游戏库价值（仓库价值）────────────────────────────────────
PRICE_CACHE = DATA / "steam_assets" / "prices.json"
_PRICE_TTL = 86400          # 价格一天变不了太多，缓存 24h（省掉每次 4 批请求）


def _price_cache() -> dict:
    try:
        return json.loads(PRICE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _price_save(m: dict) -> None:
    try:
        PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PRICE_CACHE.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("价格缓存写入失败: %s", e)


def _price_accum(out: dict, cache: dict, keys, data: dict, now: float) -> None:
    """把一批 appdetails 结果累加进 out 并写缓存（直连/代理两路共用）"""
    for k in keys:
        dd = (data.get(k) or {}).get("data") or {}
        po = dd.get("price_overview") or {}
        if po:
            o = int(po.get("initial") or 0)
            f = int(po.get("final") or 0)
            c = po.get("currency") or ""
            out["original"] += o
            out["final"] += f
            out["currency"] = c or out["currency"]
            out["priced"] += 1
            cache[k] = {"o": o, "f": f, "c": c, "p": 1, "t": now}
        else:
            out["unpriced"] += 1
            cache[k] = {"o": 0, "f": 0, "c": "", "p": 0, "t": now}


async def library_value(appids, cc: str = "cn", batch: int = 10,
                        budget: float = 75.0) -> dict:
    """游戏库总价值：分批查 store 的 price_overview，累加原价与现价。

    ⚠️ 三个实测点：
      1. **必须带 `filters=price_overview`** —— 多 appid 时配 `filters=basic` 会 400
         （单 appid 时 basic 正常，所以这个坑只在批量时才暴露）。
      2. 批量别贪大：实测 100 个一批必超时，25 个一批在 store 慢时也整批失败
         → 默认 10 个一批，失败自动重试一次。
      3. 免费游戏 / 该区未售的没有 price_overview，计入 unpriced（不是 0 元），
         否则总价值会被严重低估。
    结果按 appid 缓存 24h，且**每批都落盘**（整轮几分钟，中途断了不该全白跑）。
    """
    ids = [str(a) for a in appids if a]
    if not ids:
        return {}

    cache = _price_cache()
    now = time.time()
    out = {"original": 0, "final": 0, "priced": 0, "unpriced": 0,
           "currency": "", "count": len(ids)}

    miss = []
    for k in ids:
        c = cache.get(k)
        if c and (now - float(c.get("t") or 0)) < _PRICE_TTL:
            out["original"] += int(c.get("o") or 0)
            out["final"] += int(c.get("f") or 0)
            out["currency"] = c.get("c") or out["currency"]
            if c.get("p"):
                out["priced"] += 1
            else:
                out["unpriced"] += 1
        else:
            miss.append(k)

    if not miss:
        out["saved"] = max(0, out["original"] - out["final"])
        return out

    # ── 优先走代理：一次请求把缺的全查回来 ──
    # Worker 内部按 15 个一批并发再合并。实测 92 款 3.9s；
    # 而直连分批要 75s，且 store 一慢就整批失败、价格覆盖率只有一半（37/92 → 72/92）。
    if proxy_on():
        try:
            rs = await _proxy_call(
                [{"k": "store", "p": "api/appdetails",
                  "q": {"appids": ",".join(miss), "filters": "price_overview",
                        "cc": cc, "l": "schinese"}}],
                timeout=min(150.0, max(40.0, budget)))
            d = (rs[0].get("data") if rs else None) or {}
        except Exception as e:
            logger.warning("代理查价失败（退回直连分批）: %s: %r", type(e).__name__, e)
            d = {}
        if isinstance(d, dict) and d:
            _price_accum(out, cache, miss, d, now)
            _price_save(cache)
            out["saved"] = max(0, out["original"] - out["final"])
            return out

    chunks = [miss[i:i + batch] for i in range(0, len(miss), batch)]
    # 并发发批次：串行时 92 款要十几分钟（store 慢，每批失败还要重试一次），
    # 并发 4 批后一轮降到 2~3 分钟。别开太大，Steam 会限流。
    sem = asyncio.Semaphore(4)
    lock = asyncio.Lock()

    async def _fetch(chunk):
        async with sem:
            for attempt in (1, 2):
                try:
                    d = await _get_json(f"{STORE}/api/appdetails",
                                        {"appids": ",".join(chunk),
                                         "filters": "price_overview",
                                         "cc": cc, "l": "schinese"}, timeout=30)
                    if isinstance(d, dict):
                        return chunk, d
                except Exception as e:
                    logger.debug("批量查价失败（%d 个, 第 %d 次）: %s: %r",
                                 len(chunk), attempt, type(e).__name__, e)
                    await asyncio.sleep(1.0)
            return chunk, None

    # 总时间预算：查价不能把整张卡拖死（Steam 断的时候每批都要等到超时）。
    # 超预算就只收已完成的批次，剩下的留给下次（缓存会逐轮补全）。
    tasks = [asyncio.create_task(_fetch(c)) for c in chunks]
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for tk in pending:
        tk.cancel()
    results = []
    for tk in done:
        if tk.cancelled() or tk.exception() is not None:
            continue
        results.append(tk.result())
    if pending:
        logger.info("价格查询超预算 %.0fs，本轮只完成 %d/%d 批（其余下次补）",
                    budget, len(results), len(chunks))

    for chunk, d in results:
        if not isinstance(d, dict):
            out["unpriced"] += len(chunk)
            continue
        _price_accum(out, cache, chunk, d, now)
    # 并发结束后统一落盘（并发里逐批写会互相覆盖）
    _price_save(cache)
    out["saved"] = max(0, out["original"] - out["final"])
    return out


# ── 游戏名（中文优先，带缓存）────────────────────────────────

_zh_cache: Optional[dict] = None
_zh_dirty = False


def _load_zh() -> dict:
    global _zh_cache
    if _zh_cache is None:
        try:
            _zh_cache = json.loads(NAMES_ZH.read_text(encoding="utf-8"))
        except Exception:
            _zh_cache = {}
    return _zh_cache


def flush_zh() -> None:
    global _zh_dirty
    if not _zh_dirty:
        return
    try:
        NAMES_ZH.parent.mkdir(parents=True, exist_ok=True)
        NAMES_ZH.write_text(json.dumps(_load_zh(), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        _zh_dirty = False
    except Exception as e:
        logger.debug("中文名缓存写入失败: %s", e)


async def prefetch_zh_names(appids) -> int:
    """批量预取中文名（只补缺失的），返回新增条数

    为什么需要它：`zh_name()` 是逐个查的，卡片要 18 款就是 18 趟请求 ——
    走代理时每趟跨境 1~2s，冷启动能吃掉十几秒。这里合并成**一次** Worker 请求
    （多 op，Worker 内部并发查）后再逐个读缓存，冷启动降到 1s 上下。
    注意用 `filters=basic` 必须**单个 appid 一 op**：多 appid + basic 会 400。
    """
    global _zh_dirty
    cache = _load_zh()
    miss = []
    for a in appids:
        k = str(a) if a else ""
        if k and not cache.get(k) and k not in miss:
            miss.append(k)
    if not miss:
        return 0
    added = 0
    if proxy_on():
        try:
            ops = [{"k": "store", "p": "api/appdetails",
                    "q": {"appids": k, "filters": "basic",
                          "cc": "cn", "l": "schinese"}} for k in miss]
            rs = await _proxy_call(ops, timeout=60)
            for k, r in zip(miss, rs):
                nm = ((((r.get("data") or {}).get(k) or {}).get("data") or {})
                      .get("name") or "").strip()
                if nm:
                    cache[k] = nm
                    added += 1
        except Exception as e:
            logger.debug("中文名批量预取失败: %s: %r", type(e).__name__, e)
    else:
        res = await asyncio.gather(*[zh_name(k) for k in miss],
                                   return_exceptions=True)
        added = sum(1 for x in res if isinstance(x, str) and x and not x.startswith("appid "))
    if added:
        _zh_dirty = True
        flush_zh()
    return added


async def zh_name(appid, fallback: str = "") -> str:
    """游戏名（中文优先，查不到退英文）

    注意两点（都实测过）：
      1. 查不到时**不写缓存** —— store 时通时断，写空值会导致以后永远拿不到。
      2. `l=schinese` 对没有中文名的游戏会返回**英文名**（Steam 的行为）；
         真的返回空时再试一次 `l=english`，避免卡片上出现 "appid 1167630" 这种兜底。
    """
    global _zh_dirty
    cache = _load_zh()
    k = str(appid)
    if cache.get(k):
        return cache[k]
    name = ""
    for lang in ("schinese", "english"):
        try:
            # 超时给到 30s：国内到 store 时通时断，20s 会偶发丢名字
            # （表现为卡片上出现 "appid 1167630"，实际 store 是查得到的）
            d = await _get_json(f"{STORE}/api/appdetails",
                                {"appids": appid, "filters": "basic",
                                 "cc": "cn", "l": lang}, timeout=30)
            name = ((d.get(k) or {}).get("data") or {}).get("name") or ""
        except Exception as e:
            logger.debug("游戏名查询失败 appid=%s lang=%s: %s", appid, lang, e)
        if name:
            break
    if name:
        cache[k] = name
        _zh_dirty = True
    return name or fallback


# ── 搜索 / 价格 ───────────────────────────────────────────────

async def search_app(term: str, cc: str = "cn") -> list:
    """按名字搜游戏 → [{appid, name}]（storesearch）"""
    d = await _get_json(f"{STORE}/api/storesearch/",
                        {"term": term, "cc": cc, "l": "schinese"}, timeout=20)
    out = []
    for it in (d.get("items") or []):
        out.append({"appid": it.get("id"), "name": it.get("name") or "",
                    "is_free": bool(it.get("is_free"))})
    return out


async def app_details(appid, cc: str = "cn", lang: str = "schinese") -> dict:
    d = await _get_json(f"{STORE}/api/appdetails",
                        {"appids": appid, "cc": cc, "l": lang}, timeout=25)
    return ((d.get(str(appid)) or {}).get("data")) or {}


async def app_price(appid, cc: str = "cn") -> dict:
    """当前价 + 折扣（免费游戏没有 price_overview，用 is_free 判断）"""
    d = await app_details(appid, cc=cc)
    po = d.get("price_overview") or {}
    return {
        "name": d.get("name") or "",
        "is_free": bool(d.get("is_free")),
        "type": d.get("type") or "",
        "release": (d.get("release_date") or {}).get("date") or "",
        "coming_soon": bool(d.get("release_date", {}).get("coming_soon")),
        "currency": po.get("currency", ""),
        "initial": po.get("initial", 0),
        "final": po.get("final", 0),
        "initial_fmt": po.get("initial_formatted", ""),
        "final_fmt": po.get("final_formatted", ""),
        "discount": po.get("discount_percent", 0),
    }


async def price_multi_region(appid, regions: tuple = ("cn", "us", "ar", "ru")) -> list:
    """地区比价（并发请求，失败的区域跳过）"""
    async def one(cc):
        try:
            p = await app_price(appid, cc=cc)
            return {"cc": cc, "currency": p["currency"], "final_fmt": p["final_fmt"],
                    "is_free": p["is_free"], "discount": p["discount"]}
        except Exception:
            return None
    res = await asyncio.gather(*(one(cc) for cc in regions))
    # 有些区域查得到但没价格（俄罗斯区已停售等）→ 跳过，别在卡片上显示 "-"
    return [r for r in res if r and (r.get("final_fmt") or r.get("is_free"))]


# ── 历史最低价（ITAD，可选）──────────────────────────────────

async def history_low(appid, cc: str = "cn") -> dict:
    """历史最低价 —— Steam 官方 API **不提供**，只能走 IsThereAnyDeal

    需要 ITAD_KEY（免费申请：isthereanydeal.com/apps）。没配就返回 {}，
    调用方降级成"暂无史低数据"，不影响其它信息。

    两步：lookup（appid → ITAD id，GET）→ prices/v3（POST，body 是 id 数组）
    注意：prices/v3 必须 POST，写 GET 会 405（此段未实测，缺 key —— 拿到 key 后校验）
    """
    key = itad_key()
    if not key:
        return {}
    import httpx
    country = (cc or "cn").upper()
    try:
        s = await _get_json("https://api.isthereanydeal.com/games/lookup/v1",
                            {"key": key, "appid": appid}, timeout=20)
        gid = ((s or {}).get("game") or {}).get("id")
        if not gid:
            return {}
        async with httpx.AsyncClient(timeout=20, trust_env=False, verify=False) as c:
            r = await c.post("https://api.isthereanydeal.com/games/prices/v3",
                             params={"key": key, "country": country}, json=[gid])
            data = r.json()
        if not data:
            return {}
        item = data[0] if isinstance(data, list) else {}
        low = ((item.get("historyLow") or {}).get("all") or {})
        deal = ((item.get("deals") or [{}])[0].get("price") or {})
        return {"low": low.get("amount"), "currency": low.get("currency", ""),
                "current": deal.get("amount"), "country": country}
    except Exception as e:
        logger.debug("ITAD 查询失败 appid=%s: %s", appid, e)
        return {}


# ── 素材缓存 ──────────────────────────────────────────────────

def _shrink(raw: bytes, wh, quality: int = 85) -> bytes:
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail(wh, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


async def game_header(appid) -> Path:
    """游戏横幅（本地缓存优先；缺则下载 + 压缩）。失败返回不存在的 Path"""
    cache = ASSETS / "games" / f"{appid}.jpg"
    if not cache.exists():
        try:
            raw = await _get_bytes(CDN_HEADER.format(appid=appid))
            if raw and len(raw) > 500:
                cache.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(cache.write_bytes, _shrink(raw, (360, 170)))
        except Exception as e:
            logger.debug("横幅下载失败 appid=%s: %s", appid, e)
    return cache


async def avatar_file(steamid: str, url: str) -> Path:
    """头像缓存（avatars.steamstatic.com 不通时换 fastly 镜像）"""
    cache = ASSETS / "avatar" / f"{steamid}.jpg"
    if not cache.exists() and url:
        for host in AVATAR_HOSTS:
            try:
                raw = await _get_bytes(url.replace("avatars.steamstatic.com", host))
                if raw and len(raw) > 500:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(cache.write_bytes, _shrink(raw, (200, 200), 88))
                    break
            except Exception:
                continue
    return cache


# ── 自记价格（Steam 官方没有史低，至少攒自己的记录）────────────

def record_price(appid, final: int, currency: str) -> Optional[int]:
    """记录本次看到的价格

    返回：破纪录时返回**旧的**最低价（用于"比你上次见到的还低"提示），否则 None。
    Steam 官方没有史低接口，这是自记数据 —— 用得越久越有价值。
    """
    if not final:
        return None
    k = f"{appid}:{currency}"
    try:
        m = json.loads(PRICE_HIST.read_text(encoding="utf-8"))
    except Exception:
        m = {}
    prev = (m.get(k) or {}).get("low")
    if prev is None:
        m[k] = {"low": final, "at": int(time.time())}
        _write_hist(m)
        return None
    if final < prev:
        m[k] = {"low": final, "at": int(time.time())}
        _write_hist(m)
        return prev
    return None


def _write_hist(m: dict) -> None:
    try:
        PRICE_HIST.parent.mkdir(parents=True, exist_ok=True)
        PRICE_HIST.write_text(json.dumps(m, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception as e:
        logger.debug("价格历史写入失败: %s", e)
