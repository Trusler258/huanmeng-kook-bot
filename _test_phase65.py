"""
Phase 6.5 测试：
- Memory：SQLite/FTS5 主源 + 短词 LIKE fallback + DB不可用/无数据时文件 fallback
- Search：SQLite search_cache hit/miss/TTL；缓存异常不影响搜索
- 回归：普通聊天 Fast Path、Search timeout、LLM 单次调用、Tool timeout/cancel
运行: python _test_phase65.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# 用临时 SQLite，避免污染真实 data/huanmeng.db
_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_p65_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.trace import new_request, record_llm, get_llm_call_count, get_tool_calls
from core.router import resolve_intent, needs_search_heuristic

from modules.memory import get_top_memories, _legacy_file_top_memories
import modules.web_search as ws

_MEM_DIR = Path(__file__).resolve().parent / "data"
_MEM_CHAT = 99991
_mem_file = _MEM_DIR / f"memory_{_MEM_CHAT}.md"


def _write_file_memory(content: str):
    _MEM_DIR.mkdir(parents=True, exist_ok=True)
    _mem_file.write_text("# 记忆\n" + content + "\n", encoding="utf-8")


# ── 数据库准备 ─────────────────────────────────────────────
async def _setup_db():
    from db import init_db
    from db.database import db
    await init_db()
    from db.repositories import MemoryRepository
    async with db.session()() as s:
        repo = MemoryRepository(s)
        await repo.add("用户喜欢蓝色水彩画笔画画记录", conversation_id=1001, user_id=1)
        await repo.add("用户讨厌红色", conversation_id=1001, user_id=1)
        await repo.add("天气很好适合外出", conversation_id=1002, user_id=2)
        await s.commit()
    return db


# ── Memory: FTS5 ───────────────────────────────────────────
async def test_memory_fts5(db):
    out = await get_top_memories("蓝色水彩画笔", [], 1001)
    assert out and "蓝色水彩画笔" in out and "用户喜欢蓝色水彩画笔画画记录" in out, out
    print("OK test_memory_fts5 (FTS5 命中, source=sqlite)")


# ── Memory: LIKE fallback（短词 <3 字）────────────────────
async def test_memory_like(db):
    out = await get_top_memories("红色", [], 1001)
    assert out and "用户讨厌红色" in out, out
    print("OK test_memory_like (短词自动回退 LIKE, source=sqlite)")


# ── Memory: conversation 过滤 ──────────────────────────────
async def test_memory_conversation_filter(db):
    # conv=1001 无该记忆，应 miss（不跨会话返回）
    out = await get_top_memories("天气很好适合外出", [], 1001)
    assert out == "", out
    out2 = await get_top_memories("天气很好适合外出", [], 1002)
    assert out2 and "天气很好适合外出" in out2, out2
    print("OK test_memory_conversation_filter")


# ── Memory: time(since_ms) 过滤 ────────────────────────────
async def test_memory_time_filter(db):
    from db.repositories import MemoryRepository
    async with db.session()() as s:
        repo = MemoryRepository(s)
        import time
        future = int(time.time() * 1000) + 10_000_000
        rows = await repo.search("蓝色水彩画笔", conversation_id=1001, since_ms=future)
    assert rows == [], rows
    print("OK test_memory_time_filter (since_ms 未来 → 空)")


# ── Memory: DB无数据 → 文件 fallback ───────────────────────
async def test_memory_file_fallback(db):
    _write_file_memory("- [admin] 小明喜欢蓝色 (2026-01-01)")
    try:
        out = await get_top_memories("小明喜欢什么颜色", [], _MEM_CHAT)
        assert out and "小明" in out, out
        # 直接验证 legacy 检索函数
        top = _legacy_file_top_memories("小明", [], _MEM_CHAT, 5)
        assert any("小明" in m for m in top), top
        print("OK test_memory_file_fallback (file legacy fallback)")
    finally:
        if _mem_file.exists():
            _mem_file.unlink()


# ── Search: cache miss / hit / TTL ─────────────────────────
async def test_search_cache_hit_miss():
    # miss
    assert await ws._cache_get("全新查询甲乙丙丁") is None, "首次应 miss"
    # put + hit
    await ws._cache_put("全新查询甲乙丙丁", "缓存文本XYZ")
    got = await ws._cache_get("全新查询甲乙丙丁")
    assert got == "缓存文本XYZ", got
    print("OK test_search_cache_hit_miss (miss→put→hit)")


async def test_search_cache_ttl():
    from db.database import db
    from db.repositories import SearchCacheRepository
    # 写入已过期条目（TTL=-1 → expires_at 在过去）
    async with db.session()() as s:
        await SearchCacheRepository(s).put(
            "过期查询甲乙丙丁", {"text": "old"}, engine="deepseek", ttl_seconds=-1)
        await s.commit()
    assert await ws._cache_get("过期查询甲乙丙丁") is None, "过期缓存应判 miss"
    print("OK test_search_cache_ttl (过期 → miss)")


async def test_search_cache_exception_ignored():
    # DB 未初始化时缓存访问应安全返回 None，不抛异常
    from db.database import db
    orig = db.initialized
    db._initialized = False
    try:
        assert await ws._cache_get("任意查询xyz") is None
        await ws._cache_put("任意查询xyz", "txt")  # 不抛
    finally:
        db._initialized = orig
    print("OK test_search_cache_exception_ignored")


async def test_ds_native_search_cache_no_second_call(db):
    os.environ["DEEPSEEK_KEY"] = "test-key"
    calls = {"n": 0}

    def _fake_ds(query, api_key):
        calls["n"] += 1
        return "DeepSeek 搜索结果"

    orig = ws._ds_call
    ws._ds_call = _fake_ds
    try:
        q = "缓存搜索测试查询123"
        r1 = await ws.ds_native_search(q)
        r2 = await ws.ds_native_search(q)
        assert r1 == r2 == "DeepSeek 搜索结果", (r1, r2)
        assert calls["n"] == 1, f"第二次应命中缓存，_ds_call 仅调用1次，实际{calls['n']}"
        print("OK test_ds_native_search_cache_no_second_call (miss→search→hit)")
    finally:
        ws._ds_call = orig


# ── 回归：普通聊天 Fast Path ───────────────────────────────
def test_fast_path_ordinary_chat():
    assert resolve_intent("晚上好呀 今天真开心", is_group=True) == "chat"
    assert needs_search_heuristic("晚上好呀 今天真开心") is False
    assert needs_search_heuristic("帮我查一下量子计算") is True
    print("OK test_fast_path_ordinary_chat (普通聊天不走 search)")


# ── 回归：Search timeout ───────────────────────────────────
async def test_search_timeout(db):
    os.environ["DEEPSEEK_KEY"] = "test-key"

    def _hanging_ds(query, api_key):
        # run_in_executor 中执行的同步阻塞函数（run_in_executor 无法调度协程）
        time.sleep(5)
        return "x"

    orig = ws._ds_call
    ws._ds_call = _hanging_ds
    try:
        try:
            await asyncio.wait_for(ws.ds_native_search("超时搜索测试123"), timeout=0.2)
            raise AssertionError("应当超时")
        except asyncio.TimeoutError:
            pass
        print("OK test_search_timeout (wait_for 超时)")
    finally:
        ws._ds_call = orig


# ── 回归：LLM 单次调用 ─────────────────────────────────────
def test_llm_single_call():
    new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
    record_llm()
    assert get_llm_call_count() == 1, get_llm_call_count()
    print("OK test_llm_single_call")


# ── 回归：Tool timeout / cancel ────────────────────────────
def test_tool_timeout_resolution():
    from core.tools import _tool_timeout, DEFAULT_TOOL_TIMEOUT, TOOL_TIMEOUTS
    assert _tool_timeout("search_web", 5.0) == 5.0
    assert _tool_timeout("search_web", None) == TOOL_TIMEOUTS["search_web"]
    assert _tool_timeout("unknown_tool", None) == DEFAULT_TOOL_TIMEOUT
    print("OK test_tool_timeout_resolution")


async def test_execute_tool_timeout():
    import core.tools as tools

    async def _slow_impl(*a, **k):
        await asyncio.sleep(5)

    orig = tools._execute_impl
    tools._execute_impl = _slow_impl
    try:
        new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        result = await tools.execute_tool(
            "search_web", {"query": "x"}, user_id=2, group_id=0,
            sender_name="t", is_group=False, bot_qq=0, timeout=0.1)
        assert "超时" in result, result
        calls = get_tool_calls()
        assert calls and calls[-1]["status"] == "TIMEOUT", calls
        print("OK test_execute_tool_timeout")
    finally:
        tools._execute_impl = orig


async def test_execute_tool_cancelled():
    import core.tools as tools

    async def _hanging_impl(*a, **k):
        await asyncio.sleep(10)

    orig = tools._execute_impl
    tools._execute_impl = _hanging_impl
    try:
        new_request(conversation_id=1, user_id=2, channel_id="c", message_id="m")
        task = asyncio.create_task(tools.execute_tool(
            "weather", {}, user_id=2, group_id=0, sender_name="t",
            is_group=False, bot_qq=0, timeout=30))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
            raise AssertionError("应当 CancelledError")
        except asyncio.CancelledError:
            pass
        calls = get_tool_calls()
        assert calls and calls[-1]["status"] == "CANCELLED", calls
        print("OK test_execute_tool_cancelled")
    finally:
        tools._execute_impl = orig


async def main():
    db = await _setup_db()
    await test_memory_fts5(db)
    await test_memory_like(db)
    await test_memory_conversation_filter(db)
    await test_memory_time_filter(db)
    await test_memory_file_fallback(db)

    await test_search_cache_hit_miss()
    await test_search_cache_ttl()
    await test_search_cache_exception_ignored()
    await test_ds_native_search_cache_no_second_call(db)

    test_fast_path_ordinary_chat()
    await test_search_timeout(db)
    test_llm_single_call()
    test_tool_timeout_resolution()
    await test_execute_tool_timeout()
    await test_execute_tool_cancelled()

    await db.dispose()
    print("\n=== ALL Phase6.5 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())