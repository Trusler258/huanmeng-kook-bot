"""
长期记忆模块 v4 — DeepSeek 模板填充，零创作空间
- 记忆文件按对话 ID 隔离，支持旧格式混合读取
- 关键词匹配检索
- 时间窗口批处理 + 模板化提示词 → 高压缩率 + 不编造
"""

from __future__ import annotations

import re
import asyncio
import time
from datetime import datetime
from pathlib import Path

from core.logger import get_logger
from core.config import get_config
from utils.format_lang import format_lang

logger = get_logger("memory")

# ── 配置 ────────────────────────────────────────────────────
MEMORY_DIR = Path(__file__).resolve().parent.parent / "data"
_MSGLOG_DIR = MEMORY_DIR / "msglog"
MAX_MEMORY_LINES = 999999  # 永久保存
MEMORY_COOLDOWN_SECONDS = 600
BATCH_GAP_SECONDS = 60
# Phase 6 Part3：单次记忆检索的最大扫描行数与注入上限（收敛检索成本，避免全量扫描）
MEMORY_SCAN_CAP = 3000   # 只扫描最近 N 条记忆做关键词打分
MEMORY_TOP_K = 10        # 命中后最多注入条数
MEMORY_TOKEN_BUDGET = 2000  # 记忆注入字符上限

_last_memory_attempt: dict[int, float] = {}
_overflow_buffers: dict[int, list[dict]] = {}
_OVERFLOW_THRESHOLD = 10  # 累积 10 条后触发压缩


# 私聊 persona 记忆覆盖：user_id → persona 专属 memory_id
# pipeline 私聊有 persona 时调用 set_persona_override 设置
_persona_overrides: dict[int, str] = {}


def set_persona_override(user_id: int, memory_id: str | None):
    """设置私聊 persona 的记忆覆盖。memory_id 为 None 时清除覆盖"""
    if memory_id:
        _persona_overrides[user_id] = memory_id
    else:
        _persona_overrides.pop(user_id, None)


def clear_memory_by_id(memory_id: str) -> bool:
    """删除指定 memory_id 的记忆文件"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    file = MEMORY_DIR / f"memory_{memory_id}.md"
    if file.exists():
        file.unlink()
        logger.info("已清空记忆文件: %s", file.name)
        return True
    return False


def _get_memory_file(chat_id) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    # 私聊 persona 覆盖：chat_id 是 user_id 且有 override 时用 persona 专属 ID
    if isinstance(chat_id, int) and chat_id in _persona_overrides:
        return MEMORY_DIR / f"memory_{_persona_overrides[chat_id]}.md"
    return MEMORY_DIR / f"memory_{chat_id}.md"


# ════════════════════════════════════════════════════════════
#  模板化摘要提示词（只做提取，不做创作）
# ════════════════════════════════════════════════════════════

SUMMARIZE_TEMPLATE = """将以下聊天记录压缩为一行记忆。严格按模板输出，只使用原文信息，不添加任何原文不存在的内容。

输出模板（严格照此格式，每行一条）：
[时间] 参与者: 谁说了什么、回复了什么 (指令; 图片; 文件)
关键词: 用逗号分割的关键词列表

规则（必须遵守）：
1. 只输出模板内容，不要任何额外说明
2. 名字必须来自原文，不许编造
3. 如果原文没有某项信息，填"无"而不是编造
4. 每行压缩 3-10 条消息，但保留所有具体细节（名字、数字、指令、决定）
5. "谁说了什么" 用逗号连接多个动作，如 "A说了X, B回复Y, C表示Z"
6. 关键词选 3-5 个最核心的，便于后续检索

聊天记录：
{conversation}

输出："""


# ════════════════════════════════════════════════════════════
#  文件 I/O
# ════════════════════════════════════════════════════════════

def load_memories(chat_id: int) -> list[str]:
    file_path = _get_memory_file(chat_id)
    if not file_path.exists():
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        memories = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        return memories
    except Exception as e:
        logger.warning("读取记忆文件失败 [%s]: %e", chat_id, e)
        return []


def save_memories_to_file(chat_id: int, memories: list[str]):
    file_path = _get_memory_file(chat_id)
    cfg = get_config()
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(format_lang("memory.file_header", name=cfg.bot_name, chat_id=chat_id))
            f.write("\n".join(memories))
    except Exception as e:
        logger.error("保存记忆文件失败 [%d]: %s", chat_id, e)


def append_memory(chat_id: int, new_line: str):
    memories = load_memories(chat_id)
    if any(new_line.strip() == mem.strip() for mem in memories):
        return
    if len(memories) >= MAX_MEMORY_LINES:
        memories.pop(0)
    memories.append(new_line)
    save_memories_to_file(chat_id, memories)
    # Phase 20 P0：同步写 SQLite/FTS5（fire-and-forget；失败静默，不影响文件 legacy）
    _append_memory_sqlite_async(chat_id, new_line)


def _append_memory_sqlite_async(chat_id: int, line: str) -> None:
    """异步写一条记忆到 SQLite（不阻塞、不抛异常）。无 running loop 时跳过。"""
    try:
        import asyncio as _asyncio
        _asyncio.get_running_loop().create_task(_append_memory_sqlite(chat_id, line))
    except RuntimeError:
        pass  # 无事件循环（如同步脚本）→ 跳过 DB 写入


async def _append_memory_sqlite(chat_id: int, line: str) -> None:
    """写一条记忆到 SQLite（Phase 20 P0）。DB 未初始化/异常 → 静默回退文件。"""
    try:
        from db.database import db
        if not db.initialized:
            return
        from db.repositories import MemoryRepository
        async with db.session() as s:
            repo = MemoryRepository(s)
            await repo.add(
                content=line,
                conversation_id=int(chat_id or 0),
                memory_type="auto",
                source="append",
            )
    except Exception:
        pass  # 记忆仍保留在文件中，DB 写入失败不影响主流程


# ════════════════════════════════════════════════════════════
#  DeepSeek 压缩调用
# ════════════════════════════════════════════════════════════

async def _compress_with_deepseek(chat_id: int, entries: list[dict]):
    """
    用 DeepSeek 模板填充压缩一批消息。
    每条压缩结果写入一行。不阻塞主流程。
    """
    import time as _time
    if not entries:
        return

    # 构建原文（带时间戳和名字）
    lines = []
    for e in entries:
        ts = _time.strftime("%H:%M:%S", _time.localtime(e.get("time", 0)))
        author = str(e.get("author", "?"))[:15]
        content = str(e.get("content", ""))[:200]
        lines.append(f"[{ts}] {author}: {content}")
    conversation = "\n".join(lines[-50:])

    prompt = SUMMARIZE_TEMPLATE.format(conversation=conversation)

    try:
        from services.llm import call_llm
        cfg = get_config()
        result = await call_llm(
            model_cfg=cfg.reply_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.2,
            timeout=15.0,
        )
        if not result:
            logger.warning("[%d] 压缩结果为空", chat_id)
            return

        # 解析输出，每行一条记忆
        for line in result.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 清理可能的前缀格式错误
            line = re.sub(r'^[-*]\s*', '', line)
            if len(line) > 5:
                append_memory(chat_id, f"- {line}")
        logger.info("[%d] 压缩完成: %d条消息 → 保存", chat_id, len(entries))

    except Exception as e:
        logger.error("[%d] DeepSeek 压缩失败: %s", chat_id, e)
        # 降级：写入最后 3 条原文，不丢数据
        for e in entries[-3:]:
            ts = _time.strftime("%m-%d %H:%M", _time.localtime(e.get("time", 0)))
            author = str(e.get("author", "?"))[:15]
            content = str(e.get("content", ""))[:100]
            append_memory(chat_id, f"- [{ts}] {author}: {content}")


# ════════════════════════════════════════════════════════════
#  角色 / 写入
# ════════════════════════════════════════════════════════════

def get_role_label(user_id: int) -> str:
    return get_config().get_user_tag(user_id)


def save_memory(text: str, sender_name: str, user_id: int, chat_id: int):
    label = get_role_label(user_id)
    new_line = f"- [{label}] {text} ({datetime.now().strftime('%Y-%m-%d')})"
    append_memory(chat_id, new_line)
    logger.info("[%d] 新增长期记忆: %s...", chat_id, text[:50])


def save_search_memory(query: str, result: str, sender_name: str, user_id: int, chat_id: int):
    label = get_role_label(user_id)
    date = datetime.now().strftime("%Y-%m-%d")
    short_result = result.replace("\n", " ")[:80]
    line = f"- [{label}] [搜索] {sender_name} 问了'{query[:30]}' → {short_result} ({date})"
    append_memory(chat_id, line)


# ════════════════════════════════════════════════════════════
#  记忆检索（关键词匹配）
# ════════════════════════════════════════════════════════════

def extract_keywords(text: str) -> set[str]:
    """提取关键词 + 2字 N-gram（提升中文匹配召回率）"""
    base = set(re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]{2,}', text))
    # 中文 2-gram：把"今天天气"拆成"今天""天天""天气"，提升模糊匹配召回率
    chinese = re.findall(r'[\u4e00-\u9fa5]+', text)
    for seg in chinese:
        for i in range(len(seg) - 1):
            base.add(seg[i:i+2])
    return base


async def _sqlite_search_memories(query: str, chat_id: int, max_cnt: int,
                                  user_id: int = 0, since_ms: int = 0) -> list[str] | None:
    """从 SQLite/FTS5 检索记忆（Phase 6.5 主检索入口）。

    支持 conversation/user/time 过滤；FTS5 短词自动回退 LIKE（见 db.fts）。
    DB 未初始化 / 异常 / 无数据时返回 None，由调用方回退文件 legacy。
    """
    try:
        from db.database import db
        if not db.initialized:
            return None
        from db.repositories import MemoryRepository
        async with db.session()() as s:
            repo = MemoryRepository(s)
            rows = await repo.search(
                query, limit=max_cnt,
                conversation_id=(chat_id if chat_id else None),
                user_id=(user_id or None),
                since_ms=(since_ms or None),
            )
        if not rows:
            return None
        return [r["content"] for r in rows]
    except Exception as e:
        logger.warning("SQLite 记忆检索不可用，回退文件: %s", e)
        return None


def _legacy_file_top_memories(current_msg: str, context_lines: list[str],
                              chat_id: int, max_cnt: int) -> list[str]:
    """文件 legacy 检索（memory_*.md，仅作 fallback）：关键词打分排序。"""
    all_memories = load_memories(chat_id)
    if not all_memories:
        return []
    # Phase 6 Part3：只扫描最近 MEMORY_SCAN_CAP 条，避免超大记忆文件全量打分
    if len(all_memories) > MEMORY_SCAN_CAP:
        all_memories = all_memories[-MEMORY_SCAN_CAP:]

    keywords = extract_keywords(current_msg)
    for line in (context_lines or [])[-5:]:
        keywords.update(extract_keywords(line))

    scored: list[tuple[int, str]] = []
    for mem in all_memories:
        mem_keywords = extract_keywords(mem)
        score = len(keywords & mem_keywords) if keywords else 0
        if score > 0:
            scored.append((score, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:max_cnt]]


def _record_memory_trace(t0: float, hits: int, source: str, status: str) -> None:
    """记录记忆检索耗时 / 命中数 / 来源到 trace 与日志。"""
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    try:
        from core.trace import record
        record("memory", elapsed_ms)
        logger.debug("记忆检索: source=%s status=%s 命中注入=%d 耗时=%.1fms",
                     source, status, hits, elapsed_ms)
    except Exception:
        pass


async def get_top_memories(current_msg: str, context_lines: list[str], chat_id: int,
                           max_cnt: int = MEMORY_TOP_K, *, user_id: int = 0,
                           since_ms: int = 0) -> str:
    """记忆检索（Phase 6.5）：SQLite/FTS5 为主源，memory_*.md 仅作 legacy/fallback。

    结果受 MEMORY_TOKEN_BUDGET（Context Builder budget）限制；
    记录 source / 命中数 / 耗时。禁止删除旧数据。

    Phase 11：优先走 MemoryEngine 的 Filter→Candidate→Score→Rerank→Budget 流程；
    失败/无数据时回退旧 SQLite 与文件 legacy。
    """
    _t0 = time.perf_counter()

    # 组装检索文本：当前消息 + 最近上下文
    query_parts = [current_msg] + [l for l in (context_lines or [])[-5:] if l]
    query_text = " ".join(query_parts)

    # ── 主源：MemoryEngine（Filter/Candidate/Score/Rerank/Budget，底层 SQLite/FTS5）──
    source = "legacy_file"
    top: list[str] = []
    try:
        from core.memory_engine import get_memory_engine
        engine_hits = await get_memory_engine().retrieve(
            query_text, chat_id=chat_id, user_id=user_id or None,
            since_ms=since_ms or None, budget=MEMORY_TOKEN_BUDGET,
        )
        if engine_hits:
            source = "sqlite_fts5"
            top = engine_hits.split("\n")[1:]  # 去掉标题行
    except Exception as e:
        logger.warning("MemoryEngine 检索不可用，回退: %s", e)

    # ── fallback：旧 SQLite / FTS5 ──
    if not top:
        sqlite_hits = await _sqlite_search_memories(query_text, chat_id, max_cnt,
                                                    user_id=user_id, since_ms=since_ms)
        if sqlite_hits is not None:
            source = "sqlite_fts5"
            top = sqlite_hits

    # ── fallback：DB 不可用 / 异常 / 无数据时读文件 ──
    if not top:
        top = _legacy_file_top_memories(current_msg, context_lines, chat_id, max_cnt)
        source = "legacy_file"

    if not top:
        _record_memory_trace(_t0, 0, source, "miss")
        return ""

    result = format_lang("memory.recall_header") + "\n" + "\n".join(top)
    if len(result) > MEMORY_TOKEN_BUDGET:
        result = result[:MEMORY_TOKEN_BUDGET] + "\n..."

    _record_memory_trace(_t0, len(top), source, "hit")
    return result


# ════════════════════════════════════════════════════════════
#  定时记忆 + STM 溢出
# ════════════════════════════════════════════════════════════

async def maybe_save_memory(
    msg: str, reply: str, sender_name: str,
    chat_id: int, user_id: int, memory_buffer: list[str],
):
    """定时自动记忆（10 分钟冷却）"""
    now = time.time()
    last_time = _last_memory_attempt.get(chat_id, 0)
    if now - last_time < MEMORY_COOLDOWN_SECONDS:
        return

    _last_memory_attempt[chat_id] = now
    buffer_snapshot = list(memory_buffer)
    memory_buffer.clear()
    if not buffer_snapshot:
        return

    logger.info("触发自动记忆 [%d]: %d 条", chat_id, len(buffer_snapshot))

    entries: list[dict] = []
    for line in buffer_snapshot:
        parts = line.split(": ", 1)
        author = parts[0].replace("[admin] ", "").replace("[friend] ", "").strip() if parts else "?"
        content = parts[1] if len(parts) > 1 else ""
        tag = "admin" if "[admin]" in line else ("friend" if "[friend]" in line else "群友")
        entries.append({"time": time.time(), "author": author, "tag": tag, "content": content[:200]})

    asyncio.create_task(_compress_with_deepseek(chat_id, entries))


def merge_overflow_memory(chat_id: int, overflow: list[dict]):
    """STM 溢出 → 缓冲 → 累积够阈值后异步压缩"""
    buf = _overflow_buffers.get(chat_id, [])
    buf.extend(overflow)
    _overflow_buffers[chat_id] = buf
    logger.info("长时记忆溢出缓冲: chat=%d +%d = %d条 (阈值=%d)",
               chat_id, len(overflow), len(buf), _OVERFLOW_THRESHOLD)

    if len(buf) >= _OVERFLOW_THRESHOLD:
        snapshot = list(buf)
        buf.clear()
        asyncio.create_task(_compress_with_deepseek(chat_id, snapshot))


# ════════════════════════════════════════════════════════════
#  读取 / 搜索
# ════════════════════════════════════════════════════════════

def read_long_memory(chat_id: int, limit: int = 20) -> str:
    file = _get_memory_file(chat_id)
    if not file.exists():
        return "暂无长期记忆"
    lines = file.read_text(encoding="utf-8").strip().split("\n")
    return "\n".join(lines[-limit:]) if lines else "暂无长期记忆"


def search_long_memory(chat_id: int, keyword: str, limit: int = 5) -> str:
    file = _get_memory_file(chat_id)
    if not file.exists():
        return "暂无长期记忆"
    lines = file.read_text(encoding="utf-8").strip().split("\n")
    matches = [l for l in lines if keyword.lower() in l.lower()]
    return "\n".join(matches[-limit:]) if matches else f"未找到含「{keyword}」的记忆"


# ════════════════════════════════════════════════════════════
#  msglog 回溯检索 — 从近期聊天记录中搜索相关消息
# ════════════════════════════════════════════════════════════

_MSGLOG_DIR = MEMORY_DIR / "msglog"


def _read_tail_lines(path, n: int) -> list[str]:
    """高效读取文件末尾 n 行（用 seek 从尾部倒读，避免大文件全量读入内存）"""
    lines: list[str] = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            pos = size
            collected = b""
            while pos > 0 and collected.count(b"\n") < n:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                collected = f.read(read_size) + collected
            text = collected.decode("utf-8", errors="ignore")
            lines = text.splitlines()
    except Exception:
        return []
    return lines[-n:]


def search_msglog(chat_id: int, query: str, limit: int = 10, max_scan: int = 5000) -> str:
    """
    从 msglog JSONL 中搜索与 query 相关的聊天记录（可回溯到几周/一个月前）。

    Args:
        chat_id: 群号/私聊 ID
        query: 搜索关键词（如当前消息内容）
        limit: 最多返回条数
        max_scan: 最多扫描最近 N 条消息

    Returns:
        格式化的聊天记录文本，或空字符串
    """
    path = _MSGLOG_DIR / f"msglog_{chat_id}.jsonl"
    if not path.exists():
        return ""

    _t0 = time.perf_counter()
    try:
        import json
        lines = _read_tail_lines(path, max_scan)
        if not lines:
            return ""

        # 只取最近 max_scan 条
        recent = lines[-max_scan:]

        # 提取关键词（中文按字符切，英文按空格）
        keywords = _extract_keywords(query)

        scored = []
        for line in recent:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            # 跳过纯 CQ 码
            if entry.get("type") == "bot":
                pass  # ★ bot 消息也参与匹配，帮助定位相关对话
            content = entry.get("content", "")
            if not content or content.startswith("[CQ:"):
                continue

            score = _match_score(content, keywords)
            if score > 0:
                scored.append((score, entry))

        # 按分数降序，去重 + 用户消息优先 + bot 最多 2 条
        scored.sort(key=lambda x: -x[0])
        seen = set()
        user_results = []
        bot_results = []
        for score, entry in scored:
            uid = str(entry.get("user_id", ""))
            content = entry.get("content", "")
            dedup_key = f"{uid}:{content[:30]}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if entry.get("type") == "bot":
                if len(bot_results) < 2:
                    bot_results.append(entry)
            else:
                user_results.append(entry)
            if len(user_results) + len(bot_results) >= limit + 2:
                break

        # 合并：用户消息在前，bot 消息在后
        result = user_results[:limit] + bot_results[:2]
        result = result[:limit]

        if not result:
            return ""

        # 格式化输出：映射 user_id → 名字
        try:
            from core.trace import record
            record("message_retrieval", (time.perf_counter() - _t0) * 1000.0)
        except Exception:
            pass
        return _format_msglog_entries(result, chat_id)

    except Exception:
        return ""


def _extract_keywords(query: str) -> list[str]:
    """从查询文本提取有效关键词"""
    import re
    # 去掉标点和常见废话词
    stop = {"什么", "怎么", "为什么", "是啥", "帮我", "一下", "一个", "这个", "那个",
            "有没有", "能不能", "可以", "吗", "呢", "啊", "吧", "呀", "喵", "～", "~"}
    # 中文按单字/词切，英文按空格
    words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+', query)
    return [w for w in words if w.lower() not in stop and len(w) >= 1]


def _match_score(content: str, keywords: list[str]) -> int:
    """计算内容与关键词的匹配分数"""
    if not keywords:
        return 0
    content_lower = content.lower()
    score = 0
    for kw in keywords:
        kw_lower = kw.lower()
        count = content_lower.count(kw_lower)
        if count > 0:
            # 长关键词权重更高
            score += count * len(kw)
    return score


def _format_msglog_entries(entries: list[dict], chat_id: int) -> str:
    """将 msglog 条目格式化为 LLM 可读的聊天记录行"""
    from core.config import get_config
    cfg = get_config()
    lines = []
    for e in entries:
        uid = str(e.get("user_id", ""))
        is_bot = e.get("type") == "bot"
        name = cfg.qq_name_map.get(uid, uid)
        role = ""
        if is_bot and name != cfg.bot_name:
            role = " [bot]"
        content = e.get("content", "")[:100].replace("\n", " ")
        lines.append(f"{name}{role}: {content}")
    return "【最近聊天记录（来自消息回溯）】\n" + "\n".join(lines)
