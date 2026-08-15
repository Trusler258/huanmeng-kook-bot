"""
KOOK 机器人"正在听/正在玩"状态设置（Huanmeng 2.0.1 Phase 20）

通过 KOOK HTTP API `/api/v3/game/activity` 设置机器人自己的动态状态：
- data_type=2  → "正在听 xxx"（音乐），需 singer + music_name
- data_type=1  → "正在玩 xxx"（游戏），需合法的游戏 id

结束状态走 `/api/v3/game/delete-activity`。

khl.py 未暴露该接口，这里直接复用 bot 的 token 走 HTTP（与 services/pc_status 一致）。
"""
from __future__ import annotations

import time
from typing import Optional

from core.logger import get_logger

logger = get_logger("music_status")

API = "https://www.kookapp.cn/api/v3/game/activity"
API_DELETE = "https://www.kookapp.cn/api/v3/game/delete-activity"

# 最近一次设置的 payload（供 status 查询）
_last: dict = {}


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


async def set_music(music_name: str, singer: str = "", software: str = "cloudmusic") -> tuple[bool, str]:
    """设置"正在听 <music_name>"。music_name 必填。"""
    if not music_name or not music_name.strip():
        return False, "歌曲名不能为空"
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