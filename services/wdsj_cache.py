"""
WDSJ 战绩图片缓存：bot 发出的战绩图被引用时，直接返回数据而非视觉识别。
key: snapshot (图片文件名中的标识)
value: {"player": str, "template": str, "summary": str}
"""
from __future__ import annotations

import time

_cache: dict[str, dict] = {}
_MAX_AGE = 3600  # 1 小时后过期


def store(snapshot: str, player: str, template: str, summary: str):
    """存储 WDSJ 查询结果"""
    _cache[snapshot] = {
        "player": player,
        "template": template,
        "summary": summary,
        "time": time.time(),
    }
    # 定期清理过期条目
    if len(_cache) > 50:
        _cleanup()


def get(snapshot: str) -> dict | None:
    """获取缓存的 WDSJ 数据，过期返回 None"""
    entry = _cache.get(snapshot)
    if entry and time.time() - entry["time"] < _MAX_AGE:
        return entry
    if entry:
        del _cache[snapshot]
    return None


def _cleanup():
    now = time.time()
    expired = [k for k, v in _cache.items() if now - v["time"] > _MAX_AGE]
    for k in expired:
        del _cache[k]
