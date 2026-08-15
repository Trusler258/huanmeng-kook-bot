"""
统一 RequestContext / Trace 系统（Huanmeng 2.0 Phase 1）

目标：任何一次消息请求都能通过 trace_id 还原"为什么慢"。
- 每个请求绑定一个 RequestContext，至少含：
    trace_id, conversation_id, user_id, channel_id, message_id
  Agent 长任务额外有 task_id；Tool 有 tool_call_id；Plugin 有 plugin_id。
- 基于 contextvars 传播：asyncio.create_task / ensure_future / 子协程自动继承，
  因此跨 Dispatcher → Queue → Worker → Pipeline → Memory → Search → Tool → LLM
  → Response Delivery → 后台 asyncio Task 都能拿到同一 trace_id。
- 统一阶段计时器：记录 dispatcher / queue_wait / judge / memory / message_retrieval
  / context_build / skill_resolution / search / tool / llm / json_parse /
  response_policy / kook_send / message_store 等阶段耗时，保留原始样本，
  可输出 P50 / P95 / P99。
- 不做 Token Streaming 强制改造，仅计价，不改变生成方式。

用法：
    from core.trace import new_request, current, span, get_trace_id
    async def on_msg(...):
        req = new_request(conversation_id=..., user_id=..., channel_id=..., message_id=...)
        with span("judge"):
            ...
        log_phase("memory", 12.3)          # 手动记录一段耗时
        trace_id = get_trace_id()          # 全局唯一标识，贯穿日志
"""
from __future__ import annotations

import contextvars
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

# ── contextvars：当前请求上下文 ─────────────────────────────
_current_ctx: contextvars.ContextVar[Optional["RequestContext"]] = contextvars.ContextVar(
    "huanmeng_request_ctx", default=None
)


def _gen_trace_id() -> str:
    """生成 16 位十六进制 trace_id（跨进程唯一）"""
    return uuid.uuid4().hex[:16]


@dataclass
class RequestContext:
    """
    一次消息请求的完整上下文。所有可选的追踪标识都集成在此。
    通过 contextvars 在当前 async 任务及其子任务中透明传播。
    """
    trace_id: str = field(default_factory=_gen_trace_id)

    # 请求归属标识
    conversation_id: Optional[int] = None
    user_id: Optional[int] = None
    channel_id: Optional[str] = None
    message_id: Optional[str] = None

    # 长任务 / 工具 / 插件 标识（可选，按需填充）
    task_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    plugin_id: Optional[str] = None

    # 是否群聊
    is_group: bool = False

    # Phase 6 Part2：请求意图分类（chat/command/tool/search/plugin/system）
    intent: str = ""

    # 请求开始时间（单调时钟）
    start_monotonic: float = field(default_factory=time.perf_counter)

    # 阶段耗时样本（线程安全度低，仅单请求内使用；多 async 任务共享时用锁）
    _phases: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    # ── Phase 20 Hotfix B：区分 wall duration 与嵌套 child duration ──
    # _phases[phase] 的 total_ms 是「该阶段所有样本之和」，会包含嵌套子阶段重复累加
    #（例如 plan_step 包裹 tool → tool 又被单独记录一次）。为让 trace_summary 不被误解，
    # 额外记录每个 phase 的「最外层 span 墙钟耗时」：
    #   _phase_wall[phase] = 该 phase 作为最外层 span 时的一次实际持续时间。
    # 嵌套 span（栈内已有其他 span）不写 _phase_wall，避免把子阶段当 wall。
    _span_stack: list[tuple[str, float]] = field(default_factory=list)
    _phase_wall: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    # 关键阶段打点顺序，便于还原时间线
    events: list[tuple[str, float]] = field(default_factory=list)

    # ── Phase 6 增强：工具调用明细 + LLM 调用计数 ──
    # 每个元素: {"tool_name", "start_ms", "end_ms", "duration_ms", "status"}
    #   status ∈ OK / FAILED / TIMEOUT / CANCELLED
    tool_calls: list[dict] = field(default_factory=list)
    # 本次请求内部发出的 LLM 调用（含 judge / 主生成 / 工具后总结 / 搜索判断等）
    llm_call_count: int = 0
    # 每次 LLM 调用的耗时样本（毫秒），用于输出 avg/max/P50/P95/P99。
    # 与 llm_call_count 一一对应（每次调用 push 一个耗时样本）。
    llm_durations: list[float] = field(default_factory=list)

    # ── Phase 7 增强：Agent 规划摘要 + Skill 使用 ──
    # 记录"为什么规划、执行了什么、调用了几次 LLM"等 trace_summary 信息。
    plan_summary: dict = field(default_factory=dict)
    # 每个元素: {"name", "selected": bool, "loaded": bool}
    skills_used: list[dict] = field(default_factory=list)

    # ── Phase 9 增强：上下文 Token 消耗（system/history/memory/skill/tool/search/task）──
    # 由 core.context_builder.ContextEngine 产出后写入，供 trace_summary 审计上下文占用。
    context_tokens: dict = field(default_factory=dict)

    # ── Phase 20 Part7：最终回复状态标记 ──
    # 取值：tool_success / final_llm_failed / final_reply_fallback / final_reply_ok。
    # 用于审计"是否因最终 LLM 失败而覆盖了已成功的工具结果"。
    final_flags: list[str] = field(default_factory=list)

    # ── Phase 20 Part5：Capability 解析统计 ──
    # capabilities_discovered / capabilities_selected / tools_available。
    capability_stats: dict = field(default_factory=dict)

    def record_capability_stats(self, stats: dict) -> None:
        if stats:
            self.capability_stats = dict(stats)

    def record_final_flag(self, flag: str) -> None:
        if flag and flag not in self.final_flags:
            self.final_flags.append(flag)

    def record_context_tokens(self, stats: dict) -> None:
        """记录本次请求各类上下文的 Token 消耗（key 形如 system_tokens / history_tokens…）。"""
        if not stats:
            return
        for k, v in stats.items():
            if isinstance(v, int):
                self.context_tokens[k] = v
        self.context_tokens["has_context_tokens"] = True

    # 慢请求阈值（毫秒），用于分类
    SLOW_MS: float = 3000.0
    VERY_SLOW_MS: float = 10000.0

    # ── 日志注入辅助 ──
    def set_task(self, task_id: str):
        self.task_id = task_id

    def set_tool_call(self, tool_call_id: str):
        self.tool_call_id = tool_call_id

    def set_plugin(self, plugin_id: str):
        self.plugin_id = plugin_id

    def span(self, phase: str) -> "Span":
        """返回一个计时上下文管理器，离开时自动记录 phase 耗时。"""
        return Span(self, phase)

    def record(self, phase: str, elapsed_ms: float):
        """手动记录一段阶段耗时（毫秒）。"""
        self._phases[phase].append(elapsed_ms)
        self.events.append((phase, elapsed_ms))

    def record_phase_wall(self, phase: str, elapsed_ms: float) -> None:
        """记录该 phase 作为最外层 span 的墙钟耗时（用于区分 wall 与嵌套 child）。"""
        self._phase_wall[phase].append(elapsed_ms)

    def phases(self) -> dict[str, list[float]]:
        """返回各阶段耗时样本（只读视图）。"""
        return {k: list(v) for k, v in self._phases.items()}

    def summary(self) -> dict:
        """各阶段耗时汇总（样本数 + 总和 + wall + P50/P95/P99 毫秒）。

        Phase 20 Hotfix B：`total_ms` 是「该阶段所有样本之和（含嵌套子阶段重复累加）」，
        `wall_ms` 是「该阶段作为最外层 span 的实际墙钟时间」。两者语义不同，不可直接相加。
        请求整体墙钟以 trace_summary 的 `total_ms` 为准。
        """
        out = {}
        for phase, samples in self._phases.items():
            wall = self._phase_wall.get(phase, [])
            out[phase] = {
                "count": len(samples),
                "total_ms": round(sum(samples), 2),
                "wall_ms": round(sum(wall), 2),
                "p50_ms": round(_percentile(samples, 50), 2),
                "p95_ms": round(_percentile(samples, 95), 2),
                "p99_ms": round(_percentile(samples, 99), 2),
            }
        return out

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.start_monotonic) * 1000.0, 2)

    # ── Phase 6：工具调用 / LLM 计数 / 慢请求 ──
    def record_tool_call(self, tool_name: str, duration_ms: float, status: str,
                         start_ms: float | None = None, end_ms: float | None = None,
                         tool_call_id: str | None = None,
                         retry_count: int = 0,
                         error: str | None = None) -> None:
        """记录一次工具调用的耗时与结果状态。

        status ∈ OK / FAILED / TIMEOUT / CANCELLED / DENIED，不记录入参/结果等敏感内容。
        Phase 8：额外记录 tool_call_id / retry_count / error，便于与 Model Tool Request 对齐。
        """
        entry = {
            "tool_name": tool_name,
            "start_ms": round(start_ms, 2) if start_ms is not None else None,
            "end_ms": round(end_ms, 2) if end_ms is not None else None,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        }
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        if retry_count:
            entry["retry_count"] = retry_count
        if error:
            entry["error"] = str(error)[:200]
        self.tool_calls.append(entry)
        self._phases.setdefault("tool", []).append(duration_ms)
        self.events.append(("tool_call", duration_ms))

    def record_llm(self, elapsed_ms: float | None = None) -> None:
        """记录一次 LLM 调用发起（用于统计本次请求的 llm_call_count）。

        elapsed_ms: 该次调用耗时（毫秒）。传入则同时计入 llm_durations 样本，
        用于输出 LLM 的 avg/max/P50/P95/P99。
        """
        self.llm_call_count += 1
        if elapsed_ms is not None:
            self.llm_durations.append(elapsed_ms)

    def record_llm_duration(self, elapsed_ms: float) -> None:
        """为一次已发起的 LLM 调用补记耗时样本（毫秒）。"""
        self.llm_durations.append(elapsed_ms)

    def llm_stats(self) -> dict:
        """LLM 调用统计：calls / total / avg / max / P50 / P95 / P99（毫秒）。"""
        samples = self.llm_durations
        if not samples:
            return {
                "calls": self.llm_call_count, "total_ms": 0.0, "avg_ms": 0.0,
                "max_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0,
            }
        return {
            "calls": self.llm_call_count,
            "total_ms": round(sum(samples), 2),
            "avg_ms": round(sum(samples) / len(samples), 2),
            "max_ms": round(max(samples), 2),
            "p50_ms": round(_percentile(samples, 50), 2),
            "p95_ms": round(_percentile(samples, 95), 2),
            "p99_ms": round(_percentile(samples, 99), 2),
        }

    # ── Phase 7：Agent 规划 / Skill 使用记录 ──
    def set_plan_summary(self, **kwargs) -> None:
        """记录 Agent 规划摘要（planned/reason/steps/tools/skills/replans 等）。"""
        self.plan_summary.update({k: v for k, v in kwargs.items()})

    def record_skill(self, name: str, selected: bool = False, loaded: bool = False) -> None:
        """记录一次 Skill 的选中/加载（用于 trace_summary 审计）。"""
        self.skills_used.append({"name": name, "selected": bool(selected), "loaded": bool(loaded)})

    def trace_summary(self) -> dict:
        """输出本次请求的完整摘要：阶段耗时 / 工具调用 / LLM 次数 / 规划 / Skill。

        供 trace_summary 审计与日志使用，回答"为什么规划、执行了什么、耗时多少、
        调用了几次 LLM"。
        """
        return {
            "trace_id": self.trace_id,
            "intent": self.intent,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "total_ms": self.total_ms(),
            "llm": self.llm_stats(),
            "tool_calls": list(self.tool_calls),
            "skills": list(self.skills_used),
            "plan": dict(self.plan_summary),
            "context_tokens": dict(self.context_tokens),
            "final_flags": list(self.final_flags),
            "capability": dict(self.capability_stats),
            "phases": self.summary(),
        }

    def severity(self) -> str:
        """按总耗时分类：normal / slow_request / very_slow_request。"""
        total = self.total_ms()
        if total >= self.VERY_SLOW_MS:
            return "very_slow_request"
        if total >= self.SLOW_MS:
            return "slow_request"
        return "normal"

    def stage_breakdown(self) -> dict:
        """Phase 20 Hotfix B：把各阶段归类到「性能归属」并给出整体 wall。

        归类的目的是让性能日志能明确说清"慢在哪一层"，而不是笼统说"LLM 性能下降"：
          queue       ← queue_wait（排队）
          llm         ← llm（主生成 / 各阶段 span llm）
          search_tool ← search / tool / tool_call（搜索与工具）
          delivery    ← kook_send / delivery / response_policy（发送）
          other       ← 其余阶段
          total_wall  ← 请求整体墙钟（唯一可相加的权威值）
        """
        summary = self.summary()
        buckets = {"queue": 0.0, "llm": 0.0, "search_tool": 0.0,
                   "delivery": 0.0, "other": 0.0}
        search_tool_keys = {"search", "tool", "tool_call"}
        llm_keys = {"llm", "json_parse"}
        delivery_keys = {"kook_send", "delivery", "response_policy"}
        queue_keys = {"queue_wait"}
        for phase, s in summary.items():
            # 优先用墙钟（最外层 span），没有墙钟（手动 record）再用累计 total。
            v = s.get("wall_ms") or s.get("total_ms") or 0.0
            if phase in queue_keys:
                buckets["queue"] += v
            elif phase in llm_keys:
                buckets["llm"] += v
            elif phase in search_tool_keys:
                buckets["search_tool"] += v
            elif phase in delivery_keys:
                buckets["delivery"] += v
            else:
                buckets["other"] += v
        buckets["total_wall"] = self.total_ms()
        # 定位「最慢归属」：忽略 other 与 total_wall，取五个真实归属中最大者。
        cands = {k: buckets[k] for k in ("queue", "llm", "search_tool", "delivery", "other")}
        dominant = max(cands, key=lambda k: cands[k]) if any(cands.values()) else "other"
        buckets["dominant"] = dominant if cands.get(dominant, 0) >= 1.0 else "other"
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in buckets.items()}


class Span:
    """阶段计时上下文管理器：with span('llm') as s: ..."""

    def __init__(self, ctx: RequestContext, phase: str):
        self._ctx = ctx
        self._phase = phase
        self._start = 0.0
        self._nested = False

    def __enter__(self) -> "Span":
        self._start = time.perf_counter()
        # Phase 20 Hotfix B：栈里已有打开的 span → 说明本 span 是嵌套子阶段，
        # 写入 wall 会重复累加，故只在最外层 span 时记录 wall。
        self._nested = bool(self._ctx._span_stack)
        self._ctx._span_stack.append((self._phase, self._start))
        return self

    def __exit__(self, *exc) -> bool:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        if self._ctx._span_stack:
            self._ctx._span_stack.pop()
        self._ctx.record(self._phase, elapsed)
        if not self._nested:
            self._ctx.record_phase_wall(self._phase, elapsed)
        metrics_record(self._phase, elapsed)  # 同时写入全局统计
        return False


def _percentile(samples: list[float], p: float) -> float:
    """计算百分位数（线性插值）。"""
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    rank = (len(s) - 1) * (p / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ── 全局 API ────────────────────────────────────────────────

def current() -> Optional[RequestContext]:
    """获取当前请求上下文（无则返回 None）。"""
    return _current_ctx.get()


def new_request(**kwargs) -> RequestContext:
    """创建并 set 一个新的 RequestContext，返回它。"""
    ctx = RequestContext(**kwargs)
    _current_ctx.set(ctx)
    return ctx


def patch_request(**kwargs) -> RequestContext:
    """在已存在上下文上补充字段（不新建），无则新建。"""
    ctx = _current_ctx.get()
    if ctx is None:
        return new_request(**kwargs)
    for k, v in kwargs.items():
        if hasattr(ctx, k) and v is not None:
            setattr(ctx, k, v)
    return ctx


def get_trace_id() -> str:
    ctx = _current_ctx.get()
    return ctx.trace_id if ctx else "-"


def span(phase: str) -> "Span | _NoopSpan":
    """便捷：在任意 async/同步代码里记录一段耗时。无上下文时返回空操作。"""
    ctx = _current_ctx.get()
    if ctx is None:
        return _NoopSpan()
    return ctx.span(phase)


def record(phase: str, elapsed_ms: float):
    """便捷：手动记录一段耗时。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record(phase, elapsed_ms)


def record_tool_call(tool_name: str, duration_ms: float, status: str,
                     start_ms: float | None = None, end_ms: float | None = None,
                     tool_call_id: str | None = None,
                     retry_count: int = 0,
                     error: str | None = None):
    """便捷：记录一次工具调用耗时与状态（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_tool_call(tool_name, duration_ms, status, start_ms, end_ms,
                             tool_call_id=tool_call_id, retry_count=retry_count,
                             error=error)


def record_llm(elapsed_ms: float | None = None):
    """便捷：记录一次 LLM 调用发起（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_llm(elapsed_ms)


def record_llm_duration(elapsed_ms: float):
    """便捷：为已发起的 LLM 调用补记耗时样本（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_llm_duration(elapsed_ms)


def get_llm_call_count() -> int:
    """返回当前请求的 LLM 调用次数（无上下文返回 0）。"""
    ctx = _current_ctx.get()
    return ctx.llm_call_count if ctx is not None else 0


def get_tool_calls() -> list[dict]:
    """返回当前请求的工具调用明细（无上下文返回空列表）。"""
    ctx = _current_ctx.get()
    return list(ctx.tool_calls) if ctx is not None else []


def set_plan_summary(**kwargs) -> None:
    """记录 Agent 规划摘要（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.set_plan_summary(**kwargs)


def record_skill(name: str, selected: bool = False, loaded: bool = False) -> None:
    """记录一次 Skill 选中/加载（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_skill(name, selected, loaded)


def record_context_tokens(stats: dict) -> None:
    """记录本次请求各类上下文的 Token 消耗（无上下文时安全忽略）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_context_tokens(stats)


def record_final_flag(flag: str) -> None:
    """记录最终回复状态标记（tool_success / final_llm_failed / final_reply_fallback / final_reply_ok）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_final_flag(flag)


def record_capability_stats(stats: dict) -> None:
    """记录 Capability 解析统计（capabilities_discovered / selected / tools_available）。"""
    ctx = _current_ctx.get()
    if ctx is not None:
        ctx.record_capability_stats(stats)


def trace_summary() -> dict:
    """返回当前请求的完整 trace_summary（无上下文返回空 dict）。"""
    ctx = _current_ctx.get()
    return ctx.trace_summary() if ctx is not None else {}


def stage_breakdown() -> dict:
    """返回当前请求的性能归属分解（queue/llm/search_tool/delivery/other/total_wall/dominant）。"""
    ctx = _current_ctx.get()
    return ctx.stage_breakdown() if ctx is not None else {}


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


# ── 全局阶段耗时统计（跨请求聚合，输出 P50/P95/P99）──────
# 仅保留最近 N 个样本，避免无限增长。加锁支持并发写入。
import threading

_METRIC_LOCK = threading.Lock()
_METRIC_SAMPLES: dict[str, deque[float]] = {}
_METRIC_MAX = 2000


def metrics_record(phase: str, elapsed_ms: float):
    """跨请求聚合某阶段耗时样本（供 P50/P95/P99 汇总）。"""
    with _METRIC_LOCK:
        dq = _METRIC_SAMPLES.get(phase)
        if dq is None:
            dq = deque(maxlen=_METRIC_MAX)
            _METRIC_SAMPLES[phase] = dq
        dq.append(elapsed_ms)


def metrics_snapshot() -> dict:
    """输出各阶段 P50/P95/P99 汇总（毫秒）。"""
    with _METRIC_LOCK:
        out = {}
        for phase, dq in _METRIC_SAMPLES.items():
            samples = list(dq)
            out[phase] = {
                "count": len(samples),
                "p50_ms": round(_percentile(samples, 50), 2),
                "p95_ms": round(_percentile(samples, 95), 2),
                "p99_ms": round(_percentile(samples, 99), 2),
            }
        return out


def metrics_reset():
    """清空全局阶段统计（测试用）。"""
    global _METRIC_SAMPLES
    with _METRIC_LOCK:
        _METRIC_SAMPLES = {}