"""
判断模块（原 judge.py）
- 三级判断：快速规则拒绝 → 廉价模型 → 精细兴趣度
- ✅ 搜索缓存改为内存存储 + 定时刷盘（消除 I/O 瓶颈）
- 搜索触发关键词管理
- 实时查询关键词检测
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from core.logger import get_logger
from core.config import get_config, BotConfig

logger = get_logger("judge")

# ── 缓存文件路径 ────────────────────────────────────────────
_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "search_cache.json"
_CACHE_MAX_ENTRIES = 200
_CACHE_TTL_SECONDS = 7 * 24 * 3600   # 7 天过期


# ── 默认关键词 ──────────────────────────────────────────────
DEFAULT_REALTIME_WORDS = [
    "天气", "现在时间", "现在几点", "气温", "当前", "实时",
    "新闻", "最新", "汇率", "股价", "期货", "比赛", "直播",
]

DEFAULT_SEARCH_TRIGGER_WORDS = [
    "搜索", "查一下", "什么是", "为什么", "怎么", "如何",
    "定义", "百科", "告诉我", "解释", "多少", "何时", "在哪", "介绍一下",
]


# ════════════════════════════════════════════════════════════
#  搜索缓存（内存优先，定时刷盘）
# ════════════════════════════════════════════════════════════

_mem_cache: dict[str, dict] = {}      # {query: {result, timestamp, realtime}}
_cache_dirty: bool = False             # 是否需要刷盘


def _load_search_cache_from_disk():
    """启动时从磁盘加载搜索缓存到内存"""
    global _mem_cache
    if not _CACHE_FILE.exists():
        logger.debug("搜索缓存文件不存在，使用空缓存")
        return
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            if _CACHE_FILE.stat().st_size > 0:
                _mem_cache = json.load(f)
        logger.info("搜索缓存已从磁盘加载: %d 条", len(_mem_cache))
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("读取搜索缓存失败: %s，将使用空缓存", e)
        _mem_cache = {}


def _save_search_cache_to_disk():
    """将内存缓存写入磁盘（仅脏时写）"""
    global _cache_dirty
    if not _cache_dirty:
        return
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_mem_cache, f, ensure_ascii=False, indent=2)
        _cache_dirty = False
        logger.debug("搜索缓存已写入磁盘: %d 条", len(_mem_cache))
    except Exception as e:
        logger.error("写入搜索缓存失败: %s", e)


def flush_search_cache():
    """外部调用：强制刷盘"""
    _save_search_cache_to_disk()


def get_cached_search(query: str) -> str | None:
    """
    从内存缓存查询搜索结果。
    
    Returns:
        命中返回结果文本；未命中/过期/实时查询 返回 None
    """
    entry = _mem_cache.get(query)
    if not entry:
        logger.debug("搜索缓存未命中: '%s...'", query[:30])
        return None
    
    # 实时查询永远跳过缓存
    if entry.get("realtime"):
        logger.debug("实时查询跳过缓存: '%s...'", query[:30])
        return None
    
    # TTL 过期检查
    age = time.time() - entry.get("timestamp", 0)
    if age > _CACHE_TTL_SECONDS:
        del _mem_cache[query]
        logger.debug("缓存过期(%.0fh): '%s...'", age / 3600, query[:30])
        return None
    
    logger.info("搜索缓存命中: '%s...'", query[:30])
    return entry.get("result")


def set_search_cache(query: str, result: str, realtime: bool = False):
    """
    写入搜索缓存到内存。
    超过上限时自动淘汰最旧的条目。
    """
    global _cache_dirty
    _mem_cache[query] = {
        "result": result,
        "timestamp": time.time(),
        "realtime": realtime,
    }
    _cache_dirty = True
    
    # LRU 淘汰
    if len(_mem_cache) > _CACHE_MAX_ENTRIES:
        sorted_keys = sorted(
            _mem_cache.items(),
            key=lambda x: x[1].get("timestamp", 0),
        )
        remove_count = len(_mem_cache) - _CACHE_MAX_ENTRIES
        for k, _ in sorted_keys[:remove_count]:
            del _mem_cache[k]
        logger.debug("搜索缓存淘汰 %d 条（当前 %d 条）", remove_count, len(_mem_cache))

    logger.debug("搜索缓存已更新: '%s...' (实时=%s)", query[:30], realtime)


def get_cache_stats() -> dict:
    """获取缓存统计信息（用于调试）"""
    return {
        "entries": len(_mem_cache),
        "dirty": _cache_dirty,
        "max_entries": _CACHE_MAX_ENTRIES,
    }


# ════════════════════════════════════════════════════════════
#  关键词管理
# ════════════════════════════════════════════════════════════

def _load_keywords(key_name: str, default: list[str]) -> list[str]:
    """从 bot_config.toml 加载关键词列表，未配置则用默认值"""
    cfg = get_config()
    judge_cfg_raw = {}
    try:
        # 直接读 toml 获取 judge section
        import toml
        config_path = Path(__file__).resolve().parent.parent / "config" / "bot_config.toml"
        with open(config_path, "r", encoding="utf-8") as f:
            bot_toml = toml.load(f)
        judge_cfg_raw = bot_toml.get("judge", {})
    except Exception:
        pass
    
    words = judge_cfg_raw.get(key_name, None)
    if words is None:
        return default
    return list(words)  # 确保 copy


# 全局关键词列表（模块加载时初始化）
REALTIME_WORDS: list[str] = []
SEARCH_TRIGGER_WORDS: list[str] = []


def init_keywords():
    """初始化关键词列表（程序启动时调用）"""
    global REALTIME_WORDS, SEARCH_TRIGGER_WORDS
    REALTIME_WORDS = _load_keywords("realtime_words", DEFAULT_REALTIME_WORDS)
    SEARCH_TRIGGER_WORDS = _load_keywords("search_trigger_words", DEFAULT_SEARCH_TRIGGER_WORDS)
    logger.info("关键词初始化完成 | realtime=%d个 search_trigger=%d个",
               len(REALTIME_WORDS), len(SEARCH_TRIGGER_WORDS))


def reload_keywords():
    """重新加载关键词（reload 配置后调用）"""
    init_keywords()


def is_realtime_query(query: str) -> bool:
    """判断查询是否具有实时性（命中实时关键词即返回 True）"""
    if not REALTIME_WORDS:
        return False
    q_lower = query.lower()
    return any(word in q_lower for word in REALTIME_WORDS)


def keyword_need_search(msg: str) -> bool:
    """消息是否命中搜索触发关键词"""
    if not SEARCH_TRIGGER_WORDS:
        return False
    msg_lower = msg.lower()
    return any(word in msg_lower for word in SEARCH_TRIGGER_WORDS)


# ════════════════════════════════════════════════════════════
#  三级判断逻辑
# ════════════════════════════════════════════════════════════

def should_quick_reject(msg: str, context_lines: list[str], bot_name: str) -> bool:
    """
    第一级：快速规则拒绝（策略：激进放松，仅拦截极端情况）。
    
    原策略过于保守（上下文无 bot 名即拒绝），导致群友正常对话中提及
    的话题 bot 完全不应答。现改为只拦截以下情况：
    - 消息太短（<3字）→ 拒绝
    - 机器人已在最近 3 条中连续回复 3 次且当前消息未提及它 → 防止刷屏
    - 消息是纯指令/纯数字/纯表情 → 拒绝
    
    其余全部放行到 LLM 判断。
    """
    text = msg.strip()
    
    # 消息太短
    if len(text) < 3:
        logger.debug("快速拒绝: 消息过短 (%d字)", len(text))
        return True

    # 机器人最近 3 条全是它自己 → 防刷屏（放宽到 3 次才拦截）
    recent_bot_count = sum(
        1 for line in context_lines[-3:] if line.startswith(f"{bot_name}:")
    )
    if recent_bot_count >= 3 and bot_name not in text:
        logger.debug("快速拒绝: 机器人已连续回复%d次（防刷屏）", recent_bot_count)
        return True

    # 纯数字 / 大概率无意义内容
    if text.isdigit() or all(c in "1234567890+-*/=<>!@#$%^&*()_+[]{}|;:',.<>?.`" for c in text):
        logger.debug("快速拒绝: 纯数字或纯符号消息")
        return True

    return False


async def should_respond(
    msg: str,
    msg_type: str,
    sender_name: str,
    group_id: int,
    context: list[str],
    bot_name: str,
    bot_qq: int,
    reply_threshold_override: int | None = None,
) -> bool:
    """完整的三级判断流程。注意：is_arch 已由 pipeline.py 从 call_judgment_pipeline 直接获取。"""
    cfg = get_config()

    if re.search(rf'@{bot_qq}(?!\d)', msg):
        logger.info("@机器人检测 → 直接回复")
        return True

    if should_quick_reject(msg, context, bot_name):
        logger.debug("快速规则拒绝: msg='%s...'", msg[:30])
        return False

    context_str = "\n".join(context[-10:])
    personality = cfg.personality_core

    from services.llm import call_judgment_pipeline

    interest_score, cheap_should_reply, _ = await call_judgment_pipeline(
        msg=msg,
        sender_name=sender_name,
        context_str=context_str,
        personality=personality,
        cheap_model=cfg.cheap_model if cfg.cheap_model.url and cfg.cheap_model.key else None,
        judge_model=cfg.judge_model if cfg.judge_model.url and cfg.judge_model.key else None,
    )

    reply_threshold = reply_threshold_override if reply_threshold_override is not None else cfg.reply_interest
    if cfg.cheap_model.url and cfg.cheap_model.key and cfg.cheap_model.name:
        if not cheap_should_reply:
            logger.debug("最终决策: 廉价模型建议不回复")
            return False
        if cfg.judge_model.url and cfg.judge_model.key and cfg.judge_model.name:
            result = interest_score >= reply_threshold
            logger.debug("最终决策: 兴趣度=%d 阈值=%d → %s",
                        interest_score, reply_threshold, "REPLY" if result else "SKIP")
            return result
        else:
            logger.warning("判断模型未配置，但廉价模型通过了 → 回复")
            return True
    else:
        if cfg.judge_model.url and cfg.judge_model.key and cfg.judge_model.name:
            result = interest_score >= reply_threshold
            logger.debug("最终决策(无廉价): 兴趣度=%d → %s",
                       interest_score, "REPLY" if result else "SKIP")
            return result
        else:
            logger.warning("无可用的判断模型，默认不回复")
            return False


# ════════════════════════════════════════════════════════════
#  搜索判断
# ════════════════════════════════════════════════════════════

async def needs_search(msg: str, context_str: str) -> bool:
    """判断消息是否需要联网搜索"""
    # 先尝试关键词
    if keyword_need_search(msg):
        logger.info("搜索触发: 关键词命中 → '%s...'", msg[:30])
        return True
    
    # 回退到模型判断
    cfg = get_config()
    if cfg.cheap_model.url and cfg.cheap_model.key and cfg.cheap_model.name:
        from services.llm import judge_need_search
        need = await judge_need_search(msg, context_str, cfg.cheap_model)
        return need
    
    return False


# ── 启动时自动初始化 ────────────────────────────────────────
# 不再加载磁盘缓存，每次启动清空保证时效性
_search_cache = {}
init_keywords()
