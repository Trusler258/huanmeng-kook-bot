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

    # 请求开始时间（单调时钟）
    start_monotonic: float = field(default_factory=time.perf_counter)

    # 阶段耗时样本（线程安全度低，仅单请求内使用；多 async 任务共享时用锁）
    _phases: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    # 关键阶段打点顺序，便于还原时间线
    events: list[tuple[str, float]] = field(default_factory=list)

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

    def phases(self) -> dict[str, list[float]]:
        """返回各阶段耗时样本（只读视图）。"""
        return {k: list(v) for k, v in self._phases.items()}

    def summary(self) -> dict:
        """各阶段耗时汇总（样本数 + 总和 + P50/P95/P99 毫秒）。"""
        out = {}
        for phase, samples in self._phases.items():
            out[phase] = {
                "count": len(samples),
                "total_ms": round(sum(samples), 2),
                "p50_ms": round(_percentile(samples, 50), 2),
                "p95_ms": round(_percentile(samples, 95), 2),
                "p99_ms": round(_percentile(samples, 99), 2),
            }
        return out

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.start_monotonic) * 1000.0, 2)


class Span:
    """阶段计时上下文管理器：with span('llm') as s: ..."""

    def __init__(self, ctx: RequestContext, phase: str):
        self._ctx = ctx
        self._phase = phase
        self._start = 0.0

    def __enter__(self) -> "Span":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        self._ctx.record(self._phase, elapsed)
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