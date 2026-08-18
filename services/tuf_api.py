"""
TUF API 服务模块（重写版 v2）
- 封装 The Universal Forums (ADOFai) 官方 API: https://api.tuforums.com
- 覆盖: 谱面 / 玩家 / 排行榜 / 统计 / 歌曲 / 艺术家 / 关卡包
- 全部端点公开可用（无需认证）
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("tuf_api")

_BASE_URL = "https://api.tuforums.com"
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


async def _get(path: str, params: dict | None = None) -> tuple[int, any]:
    """GET 请求，返回 (status_code, json)。"""
    url = f"{_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                logger.warning("[TUFAPI] %s HTTP %d: %s", path, r.status_code, r.text[:150])
                return r.status_code, None
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, None
    except Exception as e:
        logger.error("[TUFAPI] %s 异常: %s", path, e)
        return -1, {"error": str(e)}


# ══════════════════════════════════════════════════════════
#  谱面 Levels
# ══════════════════════════════════════════════════════════

async def search_levels(query: str, limit: int = 10, page: int = 1,
                        sort: str = "relevance") -> dict:
    """搜索谱面。query 支持 song:/artist:/creator: 语法。"""
    _, data = await _get("/v2/database/levels", {
        "query": query, "limit": limit, "page": page, "sort": sort,
    })
    if not data:
        return {"results": [], "total": 0, "hasMore": False}
    return {
        "results": data.get("results", []),
        "total": data.get("total", 0),
        "hasMore": data.get("hasMore", False),
        "page": data.get("page", page),
    }


async def get_level(level_id: int | str) -> Optional[dict]:
    """获取谱面详情（数字 ID 或 slug）。返回 {level, rerateHistory} 或 None。"""
    path = f"/v2/database/levels/byId/{level_id}" if str(level_id).isdigit() \
        else f"/v2/database/levels/{level_id}"
    code, data = await _get(path)
    return data if code == 200 and data else None


async def get_level_ratings(level_id: int) -> list:
    """获取谱面评级。"""
    code, data = await _get(f"/v2/database/levels/{level_id}/ratings")
    if code != 200 or not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("results", data.get("ratings", []))


async def get_level_passes(level_id: int, limit: int = 5) -> dict:
    """获取谱面通关记录。"""
    code, data = await _get(f"/v2/database/passes/level/{level_id}",
                            {"limit": limit, "sort": "createdAt", "order": "desc"})
    if code != 200 or not data:
        return {"passes": [], "total": 0}
    if isinstance(data, list):
        return {"passes": data, "total": len(data)}
    return {"passes": data.get("results", data.get("passes", [])),
            "total": data.get("total", 0)}


# ══════════════════════════════════════════════════════════
#  玩家 Players
# ══════════════════════════════════════════════════════════

async def search_players(name: str, limit: int = 5) -> list[dict]:
    """按名称搜索玩家（用于 bind 绑定）。"""
    code, data = await _get(f"/v2/database/players/search/{name}", {"limit": limit})
    if code != 200 or not data:
        return []
    return data.get("results", data.get("players", []))


async def get_player(player_id: int) -> Optional[dict]:
    """获取玩家详情（v3，含分数/Pass/WF 等）。"""
    code, data = await _get(f"/v3/players/{player_id}")
    return data if code == 200 and data else None


async def get_player_rank_history(player_id: int, limit: int = 30) -> list[dict]:
    """玩家排名历史（周度采样）。"""
    code, data = await _get(f"/v3/players/{player_id}/rank-history", {"limit": limit})
    if code != 200 or not data:
        return []
    return data.get("series", [])


async def get_player_passes(player_id: int, limit: int = 10) -> list[dict]:
    """玩家通关记录。"""
    code, data = await _get(f"/v3/players/{player_id}/passes", {"limit": limit})
    if code != 200 or not data:
        return []
    return data.get("passes", data.get("results", []))


# ══════════════════════════════════════════════════════════
#  排行榜 Leaderboard
# ══════════════════════════════════════════════════════════

_RANK_FIELDS = {
    "ranked": "rankedScore",      # 排名分（默认）
    "general": "generalScore",    # 综合分
    "pp": "ppScore",              # PP
    "wf": "wfScore",              # 世界第一分数
    "wfpp": "wfPPScore",
    "12k": "score12K",
    "xacc": "averageXacc",        # 平均精度
    "passes": "universalPassCount",  # 通关数
}

_RANK_LABELS = {
    "ranked": "排名分", "general": "综合分", "pp": "PP", "wf": "世界第一分",
    "wfpp": "WF PP", "12k": "12K 分", "xacc": "平均精度", "passes": "通关数",
}


def resolve_rank_field(name: str) -> str | None:
    return _RANK_FIELDS.get(name)


async def get_leaderboard(field: str = "rankedScore", page: int = 1, limit: int = 10) -> dict:
    """排行榜。field 见 _RANK_FIELDS。

    用 v3 端点（/v3/players/leaderboard），results 自带 name/country，
    避免 v2 只有 id 无名字的问题。
    """
    code, data = await _get("/v3/players/leaderboard", {
        "field": field, "page": page, "limit": limit,
    })
    if code != 200 or not data:
        return {"results": [], "count": 0}
    return {"results": data.get("results", []), "count": data.get("count", 0),
            "page": data.get("page", page)}


async def get_creators_leaderboard(page: int = 1, limit: int = 10) -> dict:
    """创作者排行榜。"""
    code, data = await _get("/v3/creators/leaderboard", {"page": page, "limit": limit})
    if code != 200 or not data:
        return {"results": [], "count": 0}
    return {"results": data.get("results", []), "count": data.get("count", 0),
            "page": data.get("page", page)}


# ══════════════════════════════════════════════════════════
#  统计 Statistics
# ══════════════════════════════════════════════════════════

async def get_statistics() -> Optional[dict]:
    """全局统计：overview / difficulties / submissions。"""
    code, data = await _get("/v2/database/statistics")
    return data if code == 200 and data else None


async def get_country_stats() -> list[dict]:
    """国家/地区玩家分布。"""
    code, data = await _get("/v2/database/statistics/players")
    if code != 200 or not data:
        return []
    return data.get("countryStats", [])


# ══════════════════════════════════════════════════════════
#  歌曲 / 艺术家 / 关卡包
# ══════════════════════════════════════════════════════════

async def search_songs(query: str, limit: int = 5) -> list[dict]:
    """搜索歌曲。"""
    code, data = await _get("/v2/database/songs", {"query": query, "limit": limit})
    if code != 200 or not data:
        return []
    return data.get("songs", data.get("results", []))


async def search_artists(query: str, limit: int = 5) -> list[dict]:
    """搜索艺术家。"""
    code, data = await _get("/v2/database/artists", {"query": query, "limit": limit})
    if code != 200 or not data:
        return []
    return data.get("artists", data.get("results", []))


async def get_packs(page: int = 1, limit: int = 10) -> dict:
    """关卡包列表。"""
    code, data = await _get("/v2/database/levels/packs", {"page": page, "limit": limit})
    if code != 200 or not data:
        return {"packs": [], "total": 0}
    return {"packs": data.get("packs", data.get("results", [])),
            "total": data.get("total", 0), "page": data.get("page", page)}


# ══════════════════════════════════════════════════════════
#  格式化辅助
# ══════════════════════════════════════════════════════════

def fmt_duration(ms) -> str:
    if not ms:
        return "??:??"
    s = int(float(ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def fmt_bpm(bpm) -> str:
    if not bpm:
        return "?"
    return str(int(bpm)) if float(bpm) == int(float(bpm)) else f"{float(bpm):.1f}"


def fmt_num(n, digits: int = 0) -> str:
    """数字千分位格式化。"""
    if n is None:
        return "0"
    try:
        return f"{float(n):,.{digits}f}"
    except (TypeError, ValueError):
        return str(n)


def fmt_pct(x, digits: int = 2) -> str:
    if x is None:
        return "-"
    try:
        return f"{float(x) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def get_creator_name(level: dict) -> str:
    """从关卡数据提取创作者名。"""
    c = level.get("creator")
    if isinstance(c, dict):
        return c.get("name", "")
    if c:
        return str(c)
    credits = level.get("levelCredits", []) or []
    names = [x.get("creator", {}).get("name", "") for x in credits
             if isinstance(x.get("creator"), dict)]
    return " | ".join(n for n in names if n) or "未知"


def get_diff_name(level: dict) -> str:
    d = level.get("difficulty", {})
    if isinstance(d, dict):
        return d.get("name", "?")
    return "?"
