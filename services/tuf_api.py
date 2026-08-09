"""
TUF API 服务模块
- 封装 The Universal Forums API 调用
- 支持搜索谱面、获取谱面详情
- 异步 HTTP 请求（httpx）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import httpx

from core.logger import get_logger
from core.config import get_config

logger = get_logger("tuf_api")

# ── 配置 ───────────────────────────────────────────────
_BASE_URL = "https://api.tuforums.com"


# ══════════════════════════════════════════════════════════
#  公共函数
# ══════════════════════════════════════════════════════════

async def search_levels(
    query: str,
    limit: int = 20,
    page: int = 1,
    sort: str = "relevance",
) -> dict:
    """
    搜索谱面
    
    Args:
        query: 搜索关键词（支持字段语法：song:xxx, artist:xxx, creator:xxx）
        limit: 每页数量（默认 20）
        page: 页码（默认 1）
        sort: 排序方式（relevance/bpm/time 等）
        
    Returns:
        API 响应的 data 部分（含 results, total, hasMore 等）
    """
    url = f"{_BASE_URL}/v2/database/levels"
    params = {
        "query": query,
        "limit": limit,
        "page": page,
    }
    
    logger.info("[TUFAPI] 搜索谱面: query=%s page=%d", query, page)
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.error("[TUFAPI] 搜索失败 HTTP %d: %s", resp.status_code, resp.text[:200])
                return {"results": [], "total": 0, "hasMore": False, "error": f"HTTP {resp.status_code}"}
            
            data = resp.json()
            results = data.get("results", [])
            total = data.get("total", 0)
            has_more = data.get("hasMore", False)
            
            logger.info("[TUFAPI] 搜索成功: 找到 %d/%d 个结果", len(results), total)
            return {
                "results": results,
                "total": total,
                "hasMore": has_more,
                "page": page,
                "limit": limit,
            }
            
    except Exception as e:
        logger.error("[TUFAPI] 搜索异常: %s", e, exc_info=True)
        return {"results": [], "total": 0, "hasMore": False, "error": str(e)}


async def get_level_by_slug(slug: str) -> Optional[dict]:
    """
    通过 slug（字符串 ID）获取谱面详情
    
    Args:
        slug: 谱面 slug（如 "hello-bpm-2021"）
        
    Returns:
        谱面详情 dict，未找到返回 None
    """
    url = f"{_BASE_URL}/v2/database/levels/{slug}"
    logger.info("[TUFAPI] 获取谱面详情: slug=%s", slug)
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.warning("[TUFAPI] 谱面不存在: slug=%s", slug)
                return None
            if resp.status_code != 200:
                logger.error("[TUFAPI] 获取详情失败 HTTP %d", resp.status_code)
                return None
            
            data = resp.json()
            logger.info("[TUFAPI] 获取详情成功: %s", data.get("song", "?"))
            return data
            
    except Exception as e:
        logger.error("[TUFAPI] 获取详情异常: %s", e, exc_info=True)
        return None


async def get_level_by_id(level_id: int) -> Optional[dict]:
    """
    通过数字 ID 获取谱面详情
    
    Args:
        level_id: 谱面数字 ID
        
    Returns:
        谱面详情 dict，未找到返回 None
    """
    url = f"{_BASE_URL}/v2/database/levels/byId/{level_id}"
    logger.info("[TUFAPI] 获取谱面详情: id=%d", level_id)
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.warning("[TUFAPI] 谱面不存在: id=%d", level_id)
                return None
            if resp.status_code != 200:
                logger.error("[TUFAPI] 获取详情失败 HTTP %d", resp.status_code)
                return None
            
            data = resp.json()
            logger.info("[TUFAPI] 获取详情成功: %s", data.get("song", "?"))
            return data
            
    except Exception as e:
        logger.error("[TUFAPI] 获取详情异常: %s", e, exc_info=True)
        return None


async def get_level_passes(level_id: int, limit: int = 5, offset: int = 0) -> dict:
    """
    获取关卡的通关记录
    
    Args:
        level_id: 关卡 ID
        limit: 返回记录数量（默认 5）
        offset: 偏移量（用于分页）
        
    Returns:
        包含 passes 列表和 total 的 dict
    """
    url = f"{_BASE_URL}/v2/database/passes/level/{level_id}"
    params = {
        "limit": limit,
        "offset": offset,
        "sort": "createdAt",
        "order": "desc",
    }
    
    logger.info("[TUFAPI] 获取通关记录: level=%d limit=%d", level_id, limit)
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.error("[TUFAPI] 获取通关记录失败 HTTP %d", resp.status_code)
                return {"passes": [], "total": 0}
            
            data = resp.json()
            # API 可能直接返回 list，也可能返回 {results: [], total: N}
            if isinstance(data, list):
                passes = data
                total = len(data)
            else:
                passes = data.get("results", data.get("passes", []))
                total = data.get("total", len(passes))
            
            logger.info("[TUFAPI] 获取通关记录成功: %d/%d 条", len(passes), total)
            return {
                "passes": passes,
                "total": total,
            }
            
    except Exception as e:
        logger.error("[TUFAPI] 获取通关记录异常: %s", e, exc_info=True)
        return {"passes": [], "total": 0}


async def download_image(url: str, save_path: Path) -> bool:
    """
    下载图片到本地（用于难度图标等）
    
    Args:
        url: 图片 URL
        save_path: 保存路径
        
    Returns:
        是否下载成功
    """
    logger.info("[TUFAPI] 下载图片: %s → %s", url[:60], save_path.name)
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.error("[TUFAPI] 下载失败 HTTP %d", resp.status_code)
                return False
            
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            
            logger.info("[TUFAPI] 图片下载成功: %s (%d bytes)", save_path.name, len(resp.content))
            return True
            
    except Exception as e:
        logger.error("[TUFAPI] 下载图片异常: %s", e, exc_info=True)
        return False


# ══════════════════════════════════════════════════════════
#  数据格式化辅助函数
# ══════════════════════════════════════════════════════════

def format_duration(ms: Optional[int]) -> str:
    """将毫秒转换为 MM:SS 格式"""
    if not ms:
        return "?.??"
    seconds = ms // 1000
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"


def format_bpm(bpm: Optional[float]) -> str:
    """格式化 BPM 显示"""
    if not bpm:
        return "?.?"
    return str(int(bpm)) if bpm == int(bpm) else f"{bpm:.1f}"


def get_difficulty_info(level_data: dict) -> dict:
    """
    从谱面数据中提取难度信息
    
    Returns:
        包含 difficulty_name, difficulty_color, difficulty_icon, base_score 的 dict
    """
    diff = level_data.get("difficulty", {})
    rating = level_data.get("rating", {})
    
    return {
        "name": diff.get("name", "?"),
        "type": diff.get("type", "PGU"),
        "color": diff.get("color", "#ffffff"),
        "icon_url": diff.get("icon", ""),
        "base_score": diff.get("baseScore", 0),
        "avg_difficulty_id": rating.get("averageDifficultyId", 0) if rating else 0,
    }


def format_creator(level_data: dict) -> str:
    """格式化创作者信息"""
    creator = level_data.get("creator", "")
    if creator:
        return creator
    
    # 从 levelCredits 提取
    credits = level_data.get("levelCredits", [])
    creators = [c.get("creator", {}).get("name", "") for c in credits if c.get("role") == "charter"]
    return " | ".join(creators) if creators else "未知"


# ══════════════════════════════════════════════════════════
#  测试函数
# ══════════════════════════════════════════════════════════

async def test_search():
    """测试搜索功能"""
    result = await search_levels("song:Hello (BPM) 2021", limit=3)
    print(f"找到 {result['total']} 个结果，显示前 {len(result['results'])} 个:")
    for level in result["results"]:
        print(f"  - {level.get('song')} ({level.get('artist')}) - {format_creator(level)}")


if __name__ == "__main__":
    asyncio.run(test_search())
