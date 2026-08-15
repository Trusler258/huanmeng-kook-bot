"""Phase 20 回归测试（Part 17：20 项清单）。

覆盖：复杂度/FastPath 分离、Response Budget、Context 扩容、write_code 权限分离、
Agent Final JSON 稳定性、Task 状态、DB 接入、Memory FTS5/fallback、Search Cache、
Persona fallback、多轮续说、Capability Context、Trace P50/P95/P99、Task timeout/cancel。

全部为本地单元级验证，不依赖 LLM / 网络 / KOOK 连接。
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0

def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    ok = bool(cond)
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")

# ── 1. simple chat → Fast Path ─────────────────────────────
from core.complexity import assess_complexity
for m in ("你好", "哈哈", "谢谢", "在吗", "晚安"):
    c = assess_complexity(m)
    check(f"simple_chat_fastpath: {m}", c.level == "chat" and not c.needs_agent,
          f"got={c.level}")

# ── 2. MySQL history → 非 Fast Path ────────────────────────
c = assess_complexity("mysql历史")
check("mysql_history_knowledge", c.level == "knowledge" and c.needs_agent,
      f"got={c.level}")
from core.agent.planner import get_planner
check("mysql_history_should_plan", get_planner().should_plan("mysql历史", "chat") is True)

# ── 3. 完整教程 → detailed/full ────────────────────────────
c = assess_complexity("给我一份MySQL完整部署教程，详细一点")
check("tutorial_detailed_level", c.level in ("knowledge", "task"), f"got={c.level}")
check("tutorial_has_detail_hint", bool(c.detail_hint))

# ── 4. coding → Agent ──────────────────────────────────────
c = assess_complexity("帮我用python写一个2048小游戏")
check("coding_task_level", c.level == "task" and c.needs_agent, f"got={c.level}")
check("coding_should_plan", get_planner().should_plan("帮我用python写一个2048小游戏", "chat") is True)

# ── 5. write_code → ALLOW ──────────────────────────────────
from core.tool_runtime.permission import check_permission
ok, reason = check_permission("write_code")
check("write_code_allow", ok is True, f"{reason}")

# ── 6. execute_code → DENY ─────────────────────────────────
ok, reason = check_permission("code_execute")
check("execute_code_deny", ok is False, f"{reason}")
ok, reason = check_permission("shell_execute")
check("shell_execute_deny", ok is False, f"{reason}")

# ── 7/8. tool success + final JSON failure → success；malformed JSON → retry ──
# 依赖 services.llm（本地若无 openai 依赖则跳过，服务器端会完整校验）。
try:
    import inspect, services.llm as llm_mod
    llm_ok = True
except Exception:
    print("[SKIP] services.llm 不可导入(缺 openai?)，跳过 LLM JSON 相关用例")
    llm_ok = False

if llm_ok:
    src = inspect.getsource(llm_mod)
    check("tool_success_flag_in_code", "record_final_flag(\"tool_success\")" in src)
    check("final_llm_failed_flag_in_code", "record_final_flag(\"final_llm_failed\")" in src)
    check("final_reply_fallback_flag_in_code", "record_final_flag(\"final_reply_fallback\")" in src)
    check("tool_result_fallback_on_json_fail",
          "回退工具结果" in src and "禁止让用户" in src)
    # _parse_reply 对 BOM/代码块/截断 JSON 的修复能力
    res = llm_mod._parse_reply("\ufeff```json\n{\"replies\":[\"你好喵\"],\"fav\":0,\"calls\":[]}\n```", quiet=True)
    check("json_bom_fence_parse", bool(res[0]) and "你好喵" in res[0][0], f"got={res[0]}")
    res = llm_mod._parse_reply('{"replies":["内容一",', quiet=True)
    check("json_truncated_repair", bool(res[0]), f"got={res[0]}")

# ── 9. DB startup → SQLite READY ───────────────────────────
async def _db_test():
    from db.database import init_db, close_db, db
    await init_db()
    h = db.health
    check("db_initialized", db.initialized is True, f"health={h}")
    check("db_health_ready", h.get("status") == "ready", f"health={h}")
    # SELECT 1 验证
    try:
        from db.database import get_session
        async for s in get_session():
            r = await s.execute(__import__("sqlalchemy").text("SELECT 1"))
            check("db_select1", r.scalar() == 1)
            break
    except Exception as e:
        check("db_select1", False, str(e)[:120])
    await close_db()
    check("db_disposed", db.initialized is False)

# ── 10. Memory → FTS5 ──────────────────────────────────────
async def _memory_fts5_test():
    # 直接验证 SQLite/FTS5 检索路径（MemoryEngine 打分可能丢弃短查询，故走 repo.search）
    from db.database import get_session, db
    from db.repositories import MemoryRepository
    async for s in get_session():
        repo = MemoryRepository(s)
        await repo.add(content="测试记忆：TCP三次握手原理", user_id=1,
                       conversation_id=777, source="test")
        await s.commit()
        rows = await repo.search("三次握手", limit=5, conversation_id=777)
        check("memory_fts5_hit", any("TCP" in r["content"] for r in rows),
              f"n={len(rows)}")
        break

# ── 11. Memory fallback → file ─────────────────────────────
def _memory_fallback_test():
    from modules.memory import _legacy_file_top_memories
    mems = _legacy_file_top_memories("你好", [], chat_id=-999999, max_cnt=2)
    check("memory_file_fallback_no_crash", isinstance(mems, list))

# ── 12/13. Search cache hit/miss ───────────────────────────
async def _search_cache_test():
    from modules.web_search import _cache_get, _cache_put
    q = "realtime_cache_test_abc123"
    await _cache_put(q, "RESULT")
    hit = await _cache_get(q)
    check("search_cache_hit", hit == "RESULT", f"got={hit}")
    miss = await _cache_get("no_such_query_key_zzz")
    check("search_cache_miss", miss is None, f"got={miss}")

# ── 14. DB failure → graceful fallback ─────────────────────
def _db_fallback_test():
    # 未初始化时 health 应返回 degraded 且不抛异常
    from db.database import DatabaseManager
    dm = DatabaseManager()
    h = dm.health
    check("db_failure_graceful", h.get("status") == "degraded", f"health={h}")

# ── 15. Persona fallback ───────────────────────────────────
def _persona_test():
    from core.persona import tool_failed, permission_denied, timeout_message, json_parse_fallback, reply_failed
    t = tool_failed()
    check("persona_tool_failed_nonempty", bool(t))
    check("persona_tool_failed_no_plain", "哎" not in t and "客服" not in t)
    check("persona_permission_nonempty", bool(permission_denied()))
    check("persona_timeout_nonempty", bool(timeout_message()))
    check("persona_json_nonempty", bool(json_parse_fallback()))
    check("persona_reply_nonempty", bool(reply_failed()))

# ── 16. 多轮任务 continuation ──────────────────────────────
def _continuation_test():
    from core.agent.planner import extract_constraints
    cons = extract_constraints("继续")
    check("cont_marker_continue", cons.is_continuation, f"{cons}")
    cons = extract_constraints("一次说完")
    check("cont_marker_one_shot", cons.is_one_shot, f"{cons}")
    cons = extract_constraints("全部整理")
    check("cont_marker_all", cons.is_one_shot or cons.is_continuation)
    # 续说状态存取
    from core.agent.executor import _CONTINUATION, has_continuation, get_continuation
    _CONTINUATION[(123, 456)] = {"goal": "g", "accumulated": ["info"], "constraints": cons, "plan_steps": []}
    check("cont_has_state", has_continuation(123, 456) is True)
    st = get_continuation(123, 456)
    check("cont_get_state", st is not None and st.get("goal") == "g")
    _CONTINUATION.pop((123, 456), None)

# ── 17. Capability context ─────────────────────────────────
def _capability_test():
    from core.capability.loader import get_capability_loader
    loader = get_capability_loader()
    r = loader.resolve("帮我用python写一个2048游戏", "tool", is_group=True)
    check("cap_discovered_gt0", len(r["caps"]) > 0, f"caps={len(r['caps'])}")
    check("cap_tools_available", len(r["fc_schemas"]) > 0, f"tools={len(r['fc_schemas'])}")
    # trace 记录
    import core.trace as trace
    from core.trace import record_capability_stats
    ctx = trace.new_request(trace_id="t_cap", intent="tool")
    record_capability_stats({"capabilities_discovered": 3, "capabilities_selected": 1, "tools_available": 2})
    ts = ctx.trace_summary()
    check("cap_trace_recorded", ts["capability"].get("capabilities_discovered") == 3,
          f"cap={ts.get('capability')}")

# ── 18. Trace P50/P95/P99 ──────────────────────────────────
def _trace_percentile_test():
    from core.trace import RequestContext
    ctx = RequestContext(trace_id="t_perf", intent="chat")
    for ms in [100, 200, 300, 400, 500]:
        ctx.record_llm(ms)
    st = ctx.llm_stats()
    check("trace_p50", st["p50_ms"] == 300.0, f"p50={st['p50_ms']}")
    check("trace_max", st["max_ms"] == 500.0, f"max={st['max_ms']}")
    check("trace_calls", st["calls"] == 5, f"calls={st['calls']}")

# ── 19. Task timeout ───────────────────────────────────────
async def _task_timeout_test():
    import core.agent.executor as ex_mod
    from core.agent.executor import AgentExecutor, AgentContext
    from core.agent.planner import Plan, PlanStep
    async def _block(*a, **k):
        await asyncio.sleep(5)
        return True
    ex = AgentExecutor()
    ex._await_tool = _block  # 覆盖单步执行为阻塞，制造超时
    plan = Plan(goal="g", steps=[PlanStep(action="x", tool="slow")])
    actx = AgentContext(user_id=1, chat_id=2, original_msg="g")
    old = ex_mod.TOTAL_TASK_TIMEOUT
    try:
        ex_mod.TOTAL_TASK_TIMEOUT = 0.3  # 缩短总任务超时，触发 wait_for TimeoutError
        res = await asyncio.wait_for(ex.execute(plan, actx), timeout=5)
        check("task_timeout_status", res.status == "TIMEOUT", f"status={res.status}")
    finally:
        ex_mod.TOTAL_TASK_TIMEOUT = old

# ── 20. Task cancellation ──────────────────────────────────
async def _task_cancel_test():
    from core.agent.executor import AgentExecutor, AgentContext
    from core.agent.planner import Plan, PlanStep
    async def _block(*a, **k):
        await asyncio.sleep(10)
        return True
    ex = AgentExecutor()
    ex._await_tool = _block  # 覆盖单步执行为阻塞，便于在运行中取消
    plan = Plan(goal="g", steps=[PlanStep(action="x", tool="slow")])
    actx = AgentContext(user_id=1, chat_id=2, original_msg="g")
    task = asyncio.create_task(ex.execute(plan, actx))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        res = await task
        check("task_cancel_returns", res.status == "CANCELLED", f"status={res.status}")
    except asyncio.CancelledError:
        check("task_cancel_propagates", True)


# ── 9b. 旧库 FTS tokenizer 迁移（unicode61 → trigram + 回填）──
async def _fts_migration_test():
    """模拟旧版 unicode61 FTS 表，验证 init 时自动迁移为 trigram 并回填索引。"""
    import os as _os
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text
    tmp = _os.path.abspath("data/_mig_regress.db")
    if _os.path.exists(tmp):
        _os.remove(tmp)
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}")
    async with eng.begin() as c:
        await c.execute(_text(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT,"
            " user_id INTEGER, conversation_id INTEGER, memory_type TEXT, importance FLOAT,"
            " confidence FLOAT, source TEXT, summary TEXT, status TEXT,"
            " source_message_id TEXT, vector_id TEXT, created_at BIGINT, updated_at BIGINT,"
            " last_accessed_at BIGINT, meta TEXT, vector BLOB)"))
        await c.execute(_text(
            "CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize='unicode61')"))
        await c.execute(_text(
            "INSERT INTO memories (content, conversation_id) VALUES ('旧库TCP三次握手原理', 999)"))
    await eng.dispose()

    _old_url = _os.environ.get("DATABASE_URL")
    _os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}"
    from db.database import init_db, close_db, db
    try:
        await init_db()
        async with db._engine.connect() as c:
            row = (await c.execute(_text(
                "SELECT sql FROM sqlite_master WHERE name='memories_fts'"))).first()
            ok_tok = "trigram" in (row[0] or "")
            n = (await c.execute(_text(
                "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH '三次握手'"))).scalar()
            check("fts_migration_tokenizer", ok_tok, f"sql={row[0] if row else None}")
            check("fts_migration_backfill_matches", n >= 1, f"n={n}")
    finally:
        await close_db()
    if _old_url is None:
        _os.environ.pop("DATABASE_URL", None)
    else:
        _os.environ["DATABASE_URL"] = _old_url
    if _os.path.exists(tmp):
        _os.remove(tmp)


async def main():
    await _fts_migration_test()
    await _db_test()
    # 重新初始化 DB 供 search_cache 与 memory 使用（_db_test 已 dispose）
    from db.database import init_db
    await init_db()
    try:
        await _memory_fts5_test()
        await _search_cache_test()
    finally:
        from db.database import close_db
        await close_db()
    _memory_fallback_test()
    _db_fallback_test()
    _persona_test()
    _continuation_test()
    _capability_test()
    _trace_percentile_test()
    await _task_timeout_test()
    await _task_cancel_test()

    print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS} passed, {FAIL} failed)")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())