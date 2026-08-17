"""
KOOK 机器人"正在听/正在玩"状态设置（Huanmeng 2.0.1 Phase 20）

通过 KOOK HTTP API `/api/v3/game/activity` 设置机器人自己的动态状态：
- data_type=2  → "正在听 xxx"（音乐），需 singer + music_name
- data_type=1  → "正在玩 xxx"（游戏），需合法的游戏 id

结束状态走 `/api/v3/game/delete-activity`。

khl.py 未暴露该接口，这里直接复用 bot 的 token 走 HTTP（与 services/pc_status 一致）。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from core.logger import get_logger

logger = get_logger("music_status")

API = "https://www.kookapp.cn/api/v3/game/activity"
API_DELETE = "https://www.kookapp.cn/api/v3/game/delete-activity"

# "正在玩"（游戏）相关端点
API_GAME = "https://www.kookapp.cn/api/v3/game"
API_GAME_CREATE = "https://www.kookapp.cn/api/v3/game/create"

# 最近一次设置的 payload（供 status 查询）· 音乐
_last: dict = {}
# 最近一次设置的"正在玩"状态 · 游戏
_last_game: dict = {}

# KOOK software 字段的别名表（写别名 → 归一化后的正式值）。用户可能打 kugoumusic /
# kugou / 酷狗 / 网易云 / netease / qq等，统统归一化到 KOOK 认识的标识符。
SOFTWARE_ALIASES: dict[str, set[str]] = {
    "cloudmusic": {
        "cloudmusic", "netease", "163", "网易", "网易云", "网易云音乐", "网抑云",
        "wangyi", "wangyiyun", "netease_cloud", "cloud",
    },
    "qqmusic": {"qqmusic", "qq", "qq音乐"},
    "kugou": {"kugou", "kugoumusic", "kugou_music", "kg", "酷狗", "酷狗音乐"},
}


def normalize_software(raw: str) -> str:
    """把用户输入的软件名归一化为 KOOK 认识的标识符；无法识别时原样回传。"""
    s = (raw or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    for canonical, keys in SOFTWARE_ALIASES.items():
        if s in keys:
            return canonical
    return (raw or "").strip()


def _get_token() -> str:
    """从 khl.py Bot 实例取 token，失败回退启动配置。"""
    try:
        import services.sender as _s
        if _s._bot:
            return _s._bot.client.token
    except Exception:
        pass
    try:
        from core.config import get_config
        cfg = get_config()
        if cfg.kook_token:
            return cfg.kook_token
    except Exception:
        pass
    try:
        import toml
        from pathlib import Path
        cfg = toml.load(Path(__file__).resolve().parent.parent / "config" / "bot_config.toml")
        return cfg.get("kook", {}).get("token", "")
    except Exception:
        return ""


def _post(url: str, payload: dict) -> tuple[bool, str]:
    """POST 到 KOOK API，返回 (是否成功, 提示信息)。"""
    import requests
    token = _get_token()
    if not token:
        return False, "KOOK token 未找到"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=6,
        )
        data = resp.json() if resp.content else {}
        code = data.get("code", -1)
        if resp.status_code == 200 and code == 0:
            return True, data.get("message", "操作成功")
        return False, f"HTTP {resp.status_code} code={code} {data.get('message', resp.text[:100])}"
    except Exception as e:
        return False, f"请求异常: {e}"


def _get_json(url: str, params: dict) -> tuple[Optional[dict], str]:
    """GET 请求 KOOK 接口，返回 (data, err)。err 为空表示成功。"""
    import requests
    token = _get_token()
    if not token:
        return None, "KOOK token 未找到"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bot {token}"},
            params=params,
            timeout=6,
        )
        data = resp.json() if resp.content else {}
        code = data.get("code", -1)
        if resp.status_code == 200 and code == 0:
            return data.get("data"), ""
        return None, f"HTTP {resp.status_code} code={code} {data.get('message', resp.text[:100])}"
    except Exception as e:
        return None, f"请求异常: {e}"


def find_game_id(game_name: str) -> tuple[Optional[int], str]:
    """在 KOOK 游戏库里按名称查游戏 id；找不到返回 (None, "")。

    游戏列表接口无 name 搜索参数，只能拉全部后在本地匹配。"""
    data, err = _get_json(API_GAME, {"type": 0})
    if err:
        return None, err
    for g in (data or {}).get("items", []):
        if (g.get("name") or "").strip() == (game_name or "").strip():
            try:
                return int(g.get("id")), ""
            except (TypeError, ValueError):
                return None, f"游戏 {game_name} 的 id 非法"
    return None, ""


async def set_game(game_name: str) -> tuple[bool, str]:
    """设置"正在玩 <game_name>"（data_type=1）。

    游戏库中找不到同名游戏时自动创建（KOOK 单日最多创建 5 个）。"""
    name = (game_name or "").strip()
    if not name:
        return False, "游戏名不能为空"
    gid, err = find_game_id(name)
    if err:
        return False, f"查询游戏失败: {err}"
    if gid is None:
        ok, msg = _post(API_GAME_CREATE, {"name": name})
        if not ok:
            return False, f"创建游戏失败: {msg}"
        gid, err = find_game_id(name)
        if err or gid is None:
            return False, "已创建游戏但未能取到游戏 id"
    ok, msg = _post(API, {"id": gid, "data_type": 1})
    if ok:
        _last_game.clear()
        _last_game.update({"name": name, "game_id": gid, "set_at": time.time()})
        logger.info("已设置正在玩: %s", name)
    return ok, msg


def clear_game() -> tuple[bool, str]:
    """结束"正在玩"状态。"""
    ok, msg = _post(API_DELETE, {"data_type": 1})
    if ok:
        _last_game.clear()
        logger.info("已结束正在玩状态")
    return ok, msg


def current_game() -> dict:
    """返回最近一次设置的"正在玩"状态（未设置返回空 dict）。"""
    return dict(_last_game)
    """设置"正在听 <music_name>"。music_name 必填。"""
    if not music_name or not music_name.strip():
        return False, "歌曲名不能为空"
    software = normalize_software(software) or "cloudmusic"
    payload = {
        "id": 0,                 # 音乐类型 id 填 0
        "data_type": 2,
        "software": software or "cloudmusic",
        "singer": (singer or "").strip(),
    }
    payload["music_name"] = music_name.strip()
    ok, msg = _post(API, payload)
    if ok:
        _last.clear()
        _last.update(payload)
        _last["set_at"] = time.time()
        logger.info("已设置正在听: %s - %s", payload["music_name"], payload["singer"])
    return ok, msg


def clear_music() -> tuple[bool, str]:
    """结束"正在听"状态。"""
    payload = {"data_type": 2}
    ok, msg = _post(API_DELETE, payload)
    if ok:
        _last.clear()
        logger.info("已结束正在听状态")
    return ok, msg


def current_status() -> dict:
    """返回最近一次设置的音乐状态（未设置返回空 dict）。"""
    return dict(_last)