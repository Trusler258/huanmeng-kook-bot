"""Phase 20 Hotfix B 测试（Huanmeng 2.0.1fix）

覆盖：JSON 安全边界、Search retry 优化、Trace 父子 Span 统计、性能归属分解。

运行：python _test_phase20_hotfix_b.py
"""
import sys, os, asyncio, json, types, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0

def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")

# ── 让真实 services.llm 可导入（stub openai） ───────────────
# services/llm.py 顶层 `from openai import OpenAI`。本地未装 openai。
# 注入一个仅含 OpenAI 类的 stub 模块，使真实 llm.py 成功 import，
# 从而能用真实 _parse_reply / normalize_final_reply 跑 JSON 安全边界用例。
# OpenAI 类只在真正调用 LLM 时才会被实例化使用，stub 会抛出，但这些用例不触网。
try:
    import services.llm as _real_llm   # 已存在的真实环境（服务器端）
    _LLM_OK = True
except Exception:
    _F_OPENAI = types.ModuleType("openai")
    class _StubOpenAI:
        def __init__(self, *a, **kw):
            raise RuntimeError("stub openai 不应被实例化（测试不触网）")
    _F_OPENAI.OpenAI = _StubOpenAI
    sys.modules["openai"] = _F_OPENAI
    try:
        import services.llm as _real_llm
        _LLM_OK = True
    except Exception as _e:
        _LLM_OK = False
        print(f"[WARN] services.llm 仍无法导入: {_e}")

# 导入被测试模块
from core.trace import (
    RequestContext, span, record, trace_summary,
    stage_breakdown, metrics_snapshot,
)
from core.persona import json_parse_fallback
from core.agent.executor import _extract_tool_success_reminder


# ═══════════════════════════════════════════════════════════════
# 1. normalize_final_reply — 正常 JSON 解析（需真实 services.llm）
# ═══════════════════════════════════════════════════════════════
def test_normalize_ok():
    if not _LLM_OK:
        print("[SKIP] normalize_ok (缺 openai)")
        return
    from services.llm import normalize_final_reply
    raw = '{"replies":["MySQL 的历史可以分为几个阶段"],"fav":0,"calls":[]}'
    out = normalize_final_reply(raw)
    check("norm_ok_returns_text", isinstance(out, str) and len(out) > 0, f"out={out!r}")
    check("norm_ok_no_json_leak", not out.startswith("{"), f"out={out!r}")
    check("norm_ok_contains_reply", "MySQL" in (out or ""), f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 2. ```json 包裹解析
# ═══════════════════════════════════════════════════════════════
def test_normalize_json_fence():
    if not _LLM_OK:
        print("[SKIP] normalize_json_fence (缺 openai)")
        return
    from services.llm import normalize_final_reply
    raw = '```json\n{"replies":["你好喵"],"fav":0,"calls":[]}\n```'
    out = normalize_final_reply(raw)
    check("norm_fence_returns_text", isinstance(out, str) and len(out) > 0, f"out={out!r}")
    check("norm_fence_no_fence", "```" not in (out or ""), f"out={out!r}")
    check("norm_fence_contains", "你好喵" in (out or ""), f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 3. 截断 JSON 不得原样发送
# ═══════════════════════════════════════════════════════════════
def test_normalize_truncated():
    if not _LLM_OK:
        print("[SKIP] normalize_truncated (缺 openai)")
        return
    from services.llm import normalize_final_reply
    raw = '{"replies":["MySQL 的历史嘛，最早可以追溯到1970年代",'
    out = normalize_final_reply(raw)
    # _parse_reply 的截断修复路径可以提取 replies
    check("norm_truncated_returns_text", isinstance(out, str) and len(out) > 0, f"out={out!r}")
    check("norm_truncated_no_json_leak", not out.startswith("{"), f"out={out!r}")
    check("norm_truncated_contains", "MySQL" in (out or "") or "1970" in (out or ""),
          f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 4. 纯文本（非 JSON）保持原样
# ═══════════════════════════════════════════════════════════════
def test_normalize_plaintext():
    if not _LLM_OK:
        print("[SKIP] normalize_plaintext (缺 openai)")
        return
    from services.llm import normalize_final_reply
    raw = "MySQL 的历史可以分为几个阶段喵~"
    out = normalize_final_reply(raw)
    check("norm_plaintext_unchanged", out == raw, f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 5. 空/None → None
# ═══════════════════════════════════════════════════════════════
def test_normalize_empty():
    if not _LLM_OK:
        print("[SKIP] normalize_empty (缺 openai)")
        return
    from services.llm import normalize_final_reply
    check("norm_empty", normalize_final_reply("") is None)
    check("norm_none", normalize_final_reply(None) is None)
    check("norm_whitespace", normalize_final_reply("   ") is None)


# ═══════════════════════════════════════════════════════════════
# 6. _extract_tool_success_reminder — write_code 成功
# ═══════════════════════════════════════════════════════════════
def test_extract_tool_success_write_code():
    acc = ['[工具:write_code]\n已发送 game2048.py']
    out = _extract_tool_success_reminder(acc)
    check("extract_wc_success", out == "已发送 game2048.py", f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 7. _extract_tool_success_reminder — 失败工具忽略
# ═══════════════════════════════════════════════════════════════
def test_extract_tool_success_fail():
    acc = ['[工具:search_web]\n搜索失败，未绑定']
    out = _extract_tool_success_reminder(acc)
    check("extract_tool_fail", out is None, f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 8. _extract_tool_success_reminder — 混合（失败+成功，取成功）
# ═══════════════════════════════════════════════════════════════
def test_extract_tool_success_mixed():
    acc = [
        '[工具:search_web]\n搜索失败，超时',
        '[工具:write_code]\n已发送 app.py',
    ]
    out = _extract_tool_success_reminder(acc)
    check("extract_tool_mixed", out == "已发送 app.py", f"out={out!r}")


# ═══════════════════════════════════════════════════════════════
# 9. _extract_tool_success_reminder — 空列表
# ═══════════════════════════════════════════════════════════════
def test_extract_tool_success_empty():
    check("extract_tool_success_empty", _extract_tool_success_reminder([]) is None)


# ═══════════════════════════════════════════════════════════════
# 10. Trace 父子 Span → wall_ms 与 total_ms 正确区分
# ═══════════════════════════════════════════════════════════════
async def test_trace_span_nesting():
    """用真实 async 睡眠验证 Span 嵌套语义：
    - 最外层 span → 墙钟 wall_ms 被记录（≈ 实际睡眠时长）
    - 嵌套子 span → wall_ms = 0（栈内已有外层 span，不写入 wall，避免把子阶段当墙钟）
    - total_ms 是累计（含子阶段重复累加），wall_ms 是墙钟，两者语义不同，相加会误导。
    """
    ctx = RequestContext(trace_id="t_nest", intent="test")
    with ctx.span("outer"):
        await asyncio.sleep(0.02)   # 外层真实耗时 ≈20ms
        with ctx.span("inner"):
            await asyncio.sleep(0.01)   # 内层真实耗时 ≈10ms（嵌套，不记 wall）

    summ = ctx.summary()
    outer_s = summ.get("outer", {})
    inner_s = summ.get("inner", {})

    # outer 是最外层 → 有 wall，且 wall >= 睡眠时长（约>=15ms）
    check("nest_outer_has_wall", outer_s.get("wall_ms", 0) >= 15.0,
          f"wall={outer_s.get('wall_ms')}")
    # outer 的墙钟 = 自身睡眠(20ms) + 内层睡眠(10ms) + 事件循环开销。
    # 只校验它不是"重复累加"（如 60s 那样的异常放大）：wall 应远小于真实工作的数量级放大。
    check("nest_outer_wall_not_nested", outer_s.get("wall_ms", 0) < 100.0,
          f"wall={outer_s.get('wall_ms')}")

    # inner 是嵌套子 span → wall_ms == 0（未作为最外层）
    check("nest_inner_wall_zero", inner_s.get("wall_ms", -1) == 0.0,
          f"wall={inner_s.get('wall_ms')}")
    # inner 的 total_ms 仍被正常累计
    check("nest_inner_total_gt0", inner_s.get("total_ms", 0) >= 5.0,
          f"total={inner_s.get('total_ms')}")

    # 关键语义：total_ms（含嵌套累计）> wall_ms（墙钟），二者不可直接相加
    check("nest_total_gt_wall_inner", inner_s.get("total_ms", 0) > inner_s.get("wall_ms", 0))

    # 嵌套正确性：inner 的耗时被同时计入 outer 的 total（重复累加），但 inner 自身 wall 为 0
    # 说明 wall 与 total 语义不同，避免 trace_summary 误读成"tool=60s 就是请求花了 60s"。
    check("nest_different_semantics",
          outer_s.get("total_ms", 0) >= inner_s.get("total_ms", 0) and
          inner_s.get("wall_ms", 0) < inner_s.get("total_ms", 0))


# ═══════════════════════════════════════════════════════════════
# 11. stage_breakdown — 性能归属
# ═══════════════════════════════════════════════════════════════
def test_stage_breakdown():
    ctx = RequestContext(trace_id="t_sb", intent="test")
    # 模拟耗时场景：search 很慢，llm 和 delivery 正常
    ctx.record("search", 15000.0)
    ctx.record("llm", 5000.0)
    ctx.record("queue_wait", 200.0)
    ctx.record("kook_send", 300.0)
    ctx.record("judge", 50.0)

    sb = ctx.stage_breakdown()
    check("sb_search_tool_gt0", sb.get("search_tool", 0) >= 15000,
          f"search_tool={sb.get('search_tool')}")
    check("sb_dominant_is_search", sb.get("dominant") == "search_tool",
          f"dominant={sb.get('dominant')}")
    check("sb_queue_gt0", sb.get("queue", 0) >= 200, f"queue={sb.get('queue')}")
    check("sb_delivery_gt0", sb.get("delivery", 0) >= 300, f"delivery={sb.get('delivery')}")
    check("sb_total_wall_gt0", sb.get("total_wall", 0) > 0, f"wall={sb.get('total_wall')}")


# ═══════════════════════════════════════════════════════════════
# 12. 搜索词优化缓存 — 同一 query 不重复调 LLM
# ═══════════════════════════════════════════════════════════════
def test_opt_cache_hit():
    from core.tools import _opt_cache_get, _opt_cache_set, _OPT_CACHE
    _OPT_CACHE.clear()
    q = "mysql 历史 发展"
    check("opt_cache_miss_before", _opt_cache_get(q) is None)
    _opt_cache_set(q, "MySQL history development")
    check("opt_cache_hit_after", _opt_cache_get(q) == "MySQL history development")
    _OPT_CACHE.clear()


# ═══════════════════════════════════════════════════════════════
# 13. 搜索词优化缓存 — 空 query 不缓存
# ═══════════════════════════════════════════════════════════════
def test_opt_cache_empty():
    from core.tools import _opt_cache_get, _opt_cache_set, _OPT_CACHE
    _OPT_CACHE.clear()
    _opt_cache_set("", "xxx")
    check("opt_cache_empty_key", _opt_cache_get("") is None)


# ═══════════════════════════════════════════════════════════════
# 14. Persona JSON fallback 非空且不包含硬编码错误语
# ═══════════════════════════════════════════════════════════════
def test_persona_json_fallback():
    fb = json_parse_fallback()
    check("persona_json_fb_nonempty", bool(fb))
    check("persona_json_fb_no_hardcode", "系统错误" not in fb and "高风险" not in fb)


# ═══════════════════════════════════════════════════════════════
# 15. note_request_stage / _last_stage 上报
# ═══════════════════════════════════════════════════════════════
def test_note_request_stage():
    import services.notify_system as _ns
    # 先清空模块全局，再调用 note_request_stage。
    # 注意：note_request_stage 内部是 `global _last_stage; _last_stage = dict(...)`（重绑定新 dict），
    # 因此调用后必须通过模块属性重新读取，不能沿用调用前的局部引用（否则读到的是旧 dict）。
    _ns._last_stage.clear()
    _ns.note_request_stage({"queue": 10, "llm": 200, "search_tool": 15000,
                            "delivery": 50, "other": 30, "total_wall": 15300,
                            "dominant": "search_tool"})
    check("last_stage_updated", _ns._last_stage.get("dominant") == "search_tool",
          f"stage={_ns._last_stage}")
    check("last_stage_search_tool", _ns._last_stage.get("search_tool") == 15000)


# ═══════════════════════════════════════════════════════════════
# 16. metrics_snapshot 可用（不炸）
# ═══════════════════════════════════════════════════════════════
def test_metrics_snapshot():
    snap = metrics_snapshot()
    check("metrics_snapshot_is_dict", isinstance(snap, dict))
    # 至少有一个已知的阶段
    for k in ("llm", "search", "tool", "queue_wait", "kook_send", "judge", "json_parse"):
        if k in snap:
            check(f"metrics_{k}_p50", isinstance(snap[k].get("p50_ms"), (int, float)))
            break


# ═══════════════════════════════════════════════════════════════
# 17. FC/普通调用 max_tokens<=0 不得传给 API（chat 复杂度预算为 0 → 不设上限）
# ═══════════════════════════════════════════════════════════════
async def test_max_tokens_zero_omitted():
    """回归：DeepSeek 报 'Invalid max_tokens value, valid range is [1, 393216]'。
    根因：chat 复杂度 output_max_tokens=0 被直接塞进 req_params["max_tokens"]。
    修复：边界处 max_tokens<=0 视为"不设上限"，不写入请求参数。
    这里 mock _create_client 捕获 req_params，验证 max_tokens=0 时该键被省略。
    """
    if not _LLM_OK:
        print("[SKIP] max_tokens_zero (缺 openai)")
        return
    import services.llm as llm
    from core.config import ModelConfig
    _cfg = ModelConfig()   # 默认空配置；_create_client 已被 mock，不触网
    captured = {}

    class _FakeMsg:
        content = "hi"
        tool_calls = None
    class _FakeChoice:
        message = _FakeMsg()
    class _FakeComp:
        choices = [_FakeChoice()]
    class _FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            return _FakeComp()
    class _FakeChat:
        completions = _FakeCompletions()
    class _FakeClient:
        def __init__(self, *a, **kw):
            self.chat = _FakeChat()

    orig = llm._create_client
    llm._create_client = lambda *a, **kw: _FakeClient()
    try:
        # max_tokens=0 → 请求参数中不得出现 max_tokens（会触发 DeepSeek 400）
        r = await llm.call_llm_with_tools(_cfg, [], [], max_tokens=0)
        check("mtk_zero_omitted", "max_tokens" not in captured,
              f"captured={set(captured.keys())}")
        check("mtk_zero_ok", isinstance(r, llm.ToolCallResult))
        # max_tokens=None → 同样省略
        await llm.call_llm_with_tools(_cfg, [], [], max_tokens=None)
        check("mtk_none_omitted", "max_tokens" not in captured,
              f"captured={set(captured.keys())}")
        # 正值 → 保留
        await llm.call_llm_with_tools(_cfg, [], [], max_tokens=8000)
        check("mtk_positive_kept", captured.get("max_tokens") == 8000,
              f"max_tokens={captured.get('max_tokens')}")
    finally:
        llm._create_client = orig


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
async def main():
    test_normalize_ok()
    test_normalize_json_fence()
    test_normalize_truncated()
    test_normalize_plaintext()
    test_normalize_empty()
    test_extract_tool_success_write_code()
    test_extract_tool_success_fail()
    test_extract_tool_success_mixed()
    test_extract_tool_success_empty()
    await test_trace_span_nesting()
    test_stage_breakdown()
    test_opt_cache_hit()
    test_opt_cache_empty()
    test_persona_json_fallback()
    test_note_request_stage()
    test_metrics_snapshot()
    await test_max_tokens_zero_omitted()

    print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS} passed, {FAIL} failed)")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    asyncio.run(main())