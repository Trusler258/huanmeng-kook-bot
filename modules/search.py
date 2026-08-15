"""
搜索模块（从 commands.py 抽离）
- DeepSeek Responses API 原生搜索（优先）
- Agent 多源搜索（百度+Bing+深度抓取，回退）
- 本地搜索器（终版回退）
- 搜索缓存集成
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import httpx

from core.logger import get_logger
from modules.judge import get_cached_search, set_search_cache, is_realtime_query, keyword_need_search
from utils.format_lang import format_lang

logger = get_logger("search")

# ── 搜索提示去重：同一(chat, query)在短时间内只发一次"正在搜"/"找到 N 个结果"，
# ── 避免 pipeline 自动搜索与 Agent 工具搜索两条路径对同一 query 重复推送提示。 ──
import threading
_TIP_DEDUP_TTL = 30.0  # 秒
_tip_lock = threading.Lock()
_tip_sent: dict[tuple, float] = {}  # (chat_id, query) -> monotonic time


def _tip_should_send(key: tuple) -> bool:
    """返回 True 表示本次应发送提示；去重窗口内重复 key 返回 False。"""
    now = time.monotonic()
    with _tip_lock:
        last = _tip_sent.get(key)
        if last is not None and (now - last) < _TIP_DEDUP_TTL:
            return False
        _tip_sent[key] = now
        return True


def _count_search_results(text: str) -> int:
    """估算搜索结果条数：优先统计列表项行（- / * / • / 1. / 1、），否则按段落数近似。"""
    import re
    items = re.findall(r'(?m)^\s*(?:[-*•]|\d{1,2}[\.、])\s+', text)
    if items:
        return len(items)
    return max(1, text.count("\n\n") + 1)


async def perform_search(
    query: str,
    sender_name: str = "",
    user_id: int = 0,
    chat_id: int = 0,
    limit: int = 4,
    source: str = "all",
    is_group: bool = False,
) -> Optional[str]:
    """
    执行搜索：缓存 → Agent级搜索（百度+bing+百科并行）→ 格式化 → 写缓存/记忆。
    """
    # ── Step 1: 缓存命中检查 ──
    cached = get_cached_search(query)
    if cached:
        logger.info("搜索缓存命中: '%s...'", query[:30])
        return cached

    from services.sender import send_by_chat_type
    search_tip = f"🔍 DeepSeek搜索: {query[:60]}{'…' if len(query)>60 else ''}"

    # ── 搜索前即时提示：先发一条"正在搜"，避免用户长时间无反馈（卡好久）──
    if chat_id and _tip_should_send(("tip", chat_id, query)):
        try:
            asyncio.create_task(send_by_chat_type(search_tip, chat_id, is_group, user_id=user_id))
        except Exception:
            pass

    # ── Step 2: DeepSeek Responses API 原生搜索（优先）──
    logger.info("执行 DeepSeek 原生搜索: '%s...' (user=%s)", query[:40], sender_name)
    result_text = None
    _search_start = asyncio.get_event_loop().time()
    try:
        from modules.web_search import ds_native_search
        # 外层 wait_for 上限与 DS 大幅调高的单次超时匹配（默认 60s×2 次尝试 + 退避）
        result_text = await asyncio.wait_for(ds_native_search(query), timeout=90.0)
    except asyncio.TimeoutError:
        logger.warning("DeepSeek 原生搜索超时，回退 Agent 搜索")
    except Exception as e:
        logger.warning("DeepSeek 原生搜索异常: %s，回退 Agent 搜索", e)
    from core.trace import record as _trace_record
    _trace_record("search_total", (asyncio.get_event_loop().time() - _search_start) * 1000.0)

    # ── 找到结果后提示条数：先于最终组成的答案发出，保持"搜→找到→回答"顺序 ──
    if chat_id and result_text and _tip_should_send(("found", chat_id, query)):
        try:
            n = _count_search_results(result_text)
            asyncio.create_task(send_by_chat_type(
                f"✅ 找到 {n} 个结果", chat_id, is_group, user_id=user_id))
        except Exception:
            pass

    # ── Step 3: 无结果回退 ──
    if result_text is None:
        set_search_cache(query, result_text)
        logger.info("DeepSeek 搜索无结果: '%s...'", query[:40])
        return "暂无搜索结果。"

    if not result_text:
        logger.info("搜索无结果: '%s...'", query[:30])
        return None

    # 截断保护
    max_len = min(limit * 400, 3000)
    if len(result_text) > max_len:
        result_text = result_text[:max_len] + "..."

    logger.info("搜索完成: '%s...' → %d字符\n%s", query[:30], len(result_text), result_text[:800])

    # ── Step 3: 写入缓存 + 记忆 ──
    realtime = is_realtime_query(query)
    set_search_cache(query, result_text, realtime=realtime)

    if sender_name and chat_id:
        try:
            from modules.memory import save_search_memory
            await asyncio.to_thread(
                save_search_memory, query, result_text, sender_name, user_id, chat_id,
            )
        except Exception as e:
            logger.warning("写入搜索记忆失败: %s", e)

    return result_text

async def auto_search_if_needed(
    msg: str,
    sender_name: str,
    user_id: int,
    chat_id: int,
    is_group: bool = False,
) -> Optional[str]:
    """
    自动搜索：根据关键词和实时性判断是否需要搜索。
    仅对非 @ 消息生效。
    
    Returns:
        搜索结果文本；不需要搜索时返回 None
    """
    from modules.judge import needs_search as _needs_search_judge
    
    msg_lower = msg.lower()

    # ★ 预检：用户拒搜 → 跳过（与 core/tools.py 保持一致）
    no_search_patterns = [
        r'不[要需用]搜', r'别搜', r'不[要需用]查', r'不用搜索',
        r'不[是要能].*[搜查找]', r'别[去再].*[搜查找]',
        r'你[自己]的(?:回答|想法|看法|意见|思考)',
        r'(?:不要|不想|不需要).*搜索', r'(?:而)?不是.*搜索',
        r'搜[索到]了?[什么啥]', r'不要.*[网上去]?找',
    ]
    if any(re.search(p, msg_lower) for p in no_search_patterns):
        logger.info("自动搜索跳过（用户拒搜）: '%s...'", msg[:30])
        return None

    # 快速关键词匹配
    if keyword_need_search(msg):
        logger.info("自动搜索触发（关键词）: '%s...'", msg[:30])
        return await perform_search(msg, sender_name=sender_name, user_id=user_id, chat_id=chat_id, is_group=is_group)

    # 实时话题判断
    if is_realtime_query(msg):
        logger.info("自动搜索触发（实时话题）: '%s...'", msg[:30])
        return await perform_search(msg, sender_name=sender_name, user_id=user_id, chat_id=chat_id, is_group=is_group)

    # 回退到模型判断（较慢，仅前面都没命中时）
    context_str = ""  # 调用方应传入上下文
    need_model = await _needs_search_judge(msg, context_str)
    if need_model:
        logger.info("自动搜索触发（模型判断）: '%s...'", msg[:30])
        return await perform_search(msg, sender_name=sender_name, user_id=user_id, chat_id=chat_id, is_group=is_group)

    return None
