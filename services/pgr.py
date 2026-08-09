"""
Phigros Query 开放平台 API 客户端
=================================
Base: https://r0semi.xtower.site/api/v1/open
Auth: X-OpenApi-Token header
"""

import json, os, urllib.request
from typing import Optional

BASE = "https://r0semi.xtower.site/api/v1/open"

def _api_key() -> str:
    key = os.getenv("PGR_API_KEY", "")
    if not key:
        raise RuntimeError("PGR_API_KEY 未在 .env 中配置")
    return key


def _post(path: str, body: dict | None = None) -> dict:
    """POST 请求，返回 JSON"""
    headers = {
        "X-OpenApi-Token": _api_key(),
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_raw(path: str, body: dict | None = None) -> str:
    """POST 返回原始文本"""
    headers = {
        "X-OpenApi-Token": _api_key(),
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.request.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {err_body[:300]}") from e


def _get(path: str, params: dict | None = None) -> dict:
    """GET 请求，返回 JSON"""
    headers = {"X-OpenApi-Token": _api_key()}
    url = f"{BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 登录 ──────────────────────────────────────────────

def generate_qrcode(taptap_version: str = "cn") -> dict:
    """生成 TapTap 登录二维码 → {qrId, verificationUrl, qrcodeBase64}"""
    return _post(f"/auth/qrcode?taptapVersion={taptap_version}")


def poll_qrcode(qr_id: str) -> dict:
    """轮询二维码登录状态 → {status, sessionToken?, retryAfter?}"""
    return _get(f"/auth/qrcode/{qr_id}/status")


# ── 图片生成 ────────────────────────────────────────────

def get_bestn_image(session_token: str, n: int = 30, theme: str = "black", taptap_version: str = "cn") -> str:
    """获取 BestN SVG 图片 → 返回 SVG 文本"""
    return _post_raw(f"/image/bn?format=svg", {
        "sessionToken": session_token,
        "taptapVersion": taptap_version,
        "n": n,
        "theme": theme,
    })


# ── 存档 ──────────────────────────────────────────────

def get_profile(session_token: str, taptap_version: str = "cn", calculate_rks: bool = True) -> dict:
    """获取玩家存档 & RKS"""
    params = ""
    if calculate_rks:
        params = "?calculate_rks=true"
    return _post(f"/save{params}", {
        "sessionToken": session_token,
        "taptapVersion": taptap_version,
    })


# ── 排行榜/曲目 ──────────────────────────────────────

def get_leaderboard(top: int = 10) -> dict:
    """获取 RKS 排行榜"""
    return _get("/leaderboard", {"limit": top})


def search_song(keyword: str) -> dict:
    """搜索曲目"""
    return _get("/song/search", {"keyword": keyword})


def get_new_songs() -> dict:
    """新曲速递"""
    return _get("/song/new")


# ── 格式化 ────────────────────────────────────────────

def format_profile(data: dict) -> str:
    """将存档 JSON 格式化为可读文本"""
    save = data.get("save", {})
    rks = data.get("rks", {})
    stats = data.get("gradeCounts", {})
    user = save.get("user", {})

    lines = ["===== Phigros 玩家存档 ====="]
    if user:
        intro = user.get("selfIntro", "")
        if intro:
            lines.append(f"简介: {intro}")

    total_rks = rks.get("totalRks", 0)
    lines.append(f"RKS: {total_rks:.2f}")

    lines.append("")
    lines.append("--- 评级统计 ---")
    for diff in ("IN", "AT", "HD", "EZ"):
        g = stats.get(diff, {})
        if g:
            parts = []
            for grade in ("P", "FC", "C"):
                if grade in g:
                    parts.append(f"{grade}={g[grade]}")
            lines.append(f"  {diff}: {', '.join(parts)}")

    lines.append("")
    lines.append("--- Best 30 ---")
    b30 = rks.get("b30Charts", [])
    for i, chart in enumerate(b30[:15], 1):
        sid = chart.get("songId", "?")
        diff = chart.get("difficulty", "?")
        crks = chart.get("rks", 0)
        lines.append(f"  #{i} {sid} [{diff}] RKS={crks:.3f}")

    lines.append("")
    game_progress = save.get("game_progress", {})
    rank = game_progress.get("challengeModeRank", 0)
    if rank:
        lines.append(f"课题段位: {rank}")

    return "\n".join(lines)
