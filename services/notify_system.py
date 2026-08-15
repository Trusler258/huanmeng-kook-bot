"""
通知系统（KOOK Card 原生卡片）
- 性能监控：记录 LLM 调用耗时 → 窗口平均延迟超阈值 → 发"已降级"卡片；恢复 → 发"已恢复"卡片（状态机去重）
- GitHub 更新检测：git ls-remote 查远程 SHA，变化 → 发"有新版本"卡片
- 目标频道配置：管理员用 .notify 指令通过卡片按钮选择（.notifyset <channel_id> 回调保存）
- 卡片风格：KOOK Card 原生（color 背景色 + header + section 对齐 + context），无 emoji 依赖

配置持久化: data/notify_config.json
{
  "target_channels": [123, 456],   // 通知目标频道（字频道 ID，int）
  "perf_enabled": true,
  "perf_avg_ms": 2000,             // 窗口平均延迟阈值(ms)，超过触发降级
  "perf_timeout_rate": 0.5,        // 窗口超时率阈值(0~1)
  "perf_window": 30,               // 统计最近 N 次调用
  "update_enabled": true,
  "update_interval_s": 1800,       // 更新检查间隔(秒)
  "last_notified_sha": "",         // 上次通知过的远程 SHA（去重）
}
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Optional

from core.logger import get_logger

logger = get_logger("notify")

# ── 全局状态 ────────────────────────────────────────────────
_CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "notify_config.json"

# LLM 耗时窗口（线程安全：LLM 调用可能来自 executor 线程）
_latency_lock = Lock()
_latency_window: deque[float] = deque(maxlen=200)   # 每项: (elapsed_ms, success:bool)
_timeout_lock = Lock()
_last_error_ts = 0.0

# 性能状态机：None=正常 / "degraded"=已降级(已通知) 
_perf_state: str | None = None
_perf_state_lock = Lock()

# 上次性能检查时间（避免每次调用都触发检查）
_last_perf_check = 0.0

# Phase 20 Hotfix B：最近一次请求的性能归属（由 core.logger.log_trace_summary 上报），
# 用于在降级卡片里标注"慢的是哪一层（queue/llm/search_tool/delivery）"，避免笼统归因 LLM。
_last_stage: dict = {}  # {queue,llm,search_tool,delivery,other,total_wall,dominant}


def note_request_stage(stage: dict) -> None:
    """缓存最近一次请求的性能归属分解（供降级卡片展示）。"""
    global _last_stage
    if isinstance(stage, dict) and stage:
        _last_stage = dict(stage)


# ── 配置读写 ────────────────────────────────────────────────

def _default_cfg() -> dict:
    return {
        "target_channels": [],
        "perf_enabled": True,
        "perf_avg_ms": 2000,
        "perf_timeout_rate": 0.5,
        "perf_window": 30,
        "update_enabled": True,
        "update_interval_s": 1800,
        "last_notified_sha": "",
    }


def load_cfg() -> dict:
    try:
        if _CONFIG_FILE.exists():
            d = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = _default_cfg()
            cfg.update({k: v for k, v in d.items() if k in cfg})
            return cfg
    except Exception as e:
        logger.warning("notify 配置读取失败: %s", e)
    return _default_cfg()


def save_cfg(cfg: dict) -> None:
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("notify 配置写入失败: %s", e)


# ── LLM 耗时记录（由 llm.py 埋点调用）──────────────────────

def record_llm_call(elapsed_sec: float, success: bool = True) -> None:
    """记录一次 LLM 调用耗时（线程安全）。success=False 表示超时/异常"""
    global _last_error_ts
    with _latency_lock:
        _latency_window.append((elapsed_sec * 1000.0, success))
    if not success:
        with _timeout_lock:
            _last_error_ts = time.time()


# ── 卡片渲染 ────────────────────────────────────────────────
# KOOK Card 结构: [{"type":"card","theme":"secondary","color":"#RRGGBB","size":"lg","modules":[...]}]

def _card_obj(color: str, modules: list) -> list:
    """构造一张 KOOK Card 对象（list of card dict），供 send_raw_group/user 发送。

    原生卡片消息（type=10）才支持 action-group 交互按钮；
    [CARD] 标记字符串渲染的卡片是静态的，不放按钮。
    """
    return [{
        "type": "card",
        "theme": "secondary",
        "color": color,
        "size": "lg",
        "modules": modules,
    }]


def _card(color: str, modules: list) -> str:
    """渲染一张 KOOK Card 的 [CARD] 标记字符串（静态卡片用，无交互按钮）"""
    import json as _json
    return f"[CARD]{_json.dumps(_card_obj(color, modules), ensure_ascii=False)}[/CARD]"


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain-text", "content": text}}


def _section(kv: list[tuple[str, str]]) -> dict:
    """section 模块：kv 列表渲染成等宽对齐表格"""
    width = max((len(k) for k, _ in kv), default=4)
    lines = []
    for k, v in kv:
        lines.append(f"`{k}`　`{v}`")
    return {"type": "section", "text": {"type": "kmarkdown", "content": "\n".join(lines)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "kmarkdown", "content": text}]}


def _divider() -> dict:
    return {"type": "divider"}


# ── 性能降级卡片 ────────────────────────────────────────────

def _render_perf_degraded(avg_ms: float, p50_ms: float, p95_ms: float, p99_ms: float,
                          timeout_rate: float, window_n: int, threshold: float) -> str:
    color = "#d9534f"  # 红
    modules = [
        _header("KOOK BOT · 服务降级"),
        _section([
            ("服务", "LLM API"),
            ("状态", "已降级"),
            ("平均延迟", f"{avg_ms:.0f} ms"),
            ("P50 / P95 / P99", f"{p50_ms:.0f} / {p95_ms:.0f} / {p99_ms:.0f} ms"),
            ("正常阈值(P95)", f"{threshold:.0f} ms"),
            ("超时率", f"{timeout_rate * 100:.0f}%"),
            ("统计窗口", f"最近 {window_n} 次调用"),
        ]),
    ]
    # Phase 20 Hotfix B：标注慢的归属层，避免把 search/tool 慢误归因到 LLM。
    if _last_stage:
        _sb = _last_stage
        modules.append(_section([
            ("当前瓶颈归属", _sb.get("dominant", "unknown")),
            ("queue / llm", f"{_sb.get('queue', 0)} / {_sb.get('llm', 0)} ms"),
            ("search_tool", f"{_sb.get('search_tool', 0)} ms"),
            ("delivery / wall", f"{_sb.get('delivery', 0)} / {_sb.get('total_wall', 0)} ms"),
        ]))
    modules += [
        _divider(),
        _context("建议：回复 .sys 查看主机状态；.cost 查看 API 消耗；仍异常可 .restart 重启恢复。"),
        _context(f"检测时间 {time.strftime('%H:%M')} · 恢复后自动通知"),
    ]
    return _card(color, modules)


def _render_perf_recovered(avg_ms: float, p50_ms: float, p95_ms: float, window_n: int) -> str:
    color = "#2ecc71"  # 绿
    modules = [
        _header("KOOK BOT · LLM 服务已恢复"),
        _section([
            ("服务", "LLM API"),
            ("状态", "正常"),
            ("平均延迟", f"{avg_ms:.0f} ms"),
            ("P50 / P95", f"{p50_ms:.0f} / {p95_ms:.0f} ms"),
            ("统计窗口", f"最近 {window_n} 次调用"),
        ]),
        _divider(),
        _context(f"检测时间 {time.strftime('%H:%M')}"),
    ]
    return _card(color, modules)


# ── GitHub 更新卡片 ─────────────────────────────────────────

def _action_button(text: str, value: str, theme: str = "primary") -> dict:
    """KOOK action-group 按钮：click=return-val，value 作为指令回传。

    注意：KOOK 按钮必须带 value_type，否则不渲染。
    """
    return {
        "type": "button",
        "theme": theme,
        "value": value,
        "value_type": "string",
        "click": "return-val",
        "text": {"type": "plain-text", "content": text},
    }


def _render_update_card(local_sha: str | None, remote_sha: str, commits: list[str]) -> str:
    """普通更新提示卡片（保留了，但普通级现在默认不发，仅 P0 用）"""
    color = "#f0ad4e"  # 黄
    modules = [_header("KOOK BOT · 检测到新版本")]
    rows = [("远程版本", remote_sha[:7])]
    if local_sha:
        rows.append(("本地版本", local_sha[:7]))
    if commits:
        rows.append(("新增提交", f"{len(commits)} 个"))
    modules.append(_section(rows))
    if commits:
        modules.append(_divider())
        modules.append({"type": "section", "text": {"type": "kmarkdown", "content": "**更新内容**"}})
        for c in commits[:6]:
            modules.append(_context(f"`{c}`"))
        if len(commits) > 6:
            modules.append(_context(f"… 共 {len(commits)} 个提交"))
    modules.append(_divider())
    # 操作按钮：先查看更新，再确认应用（确认仅 admin 点击才生效）
    modules.append({
        "type": "action-group",
        "elements": [
            _action_button("查看更新", ".update check", theme="primary"),
            _action_button("确认更新", ".update", theme="danger"),
        ],
    })
    modules.append(_context("确认更新仅 admin 可触发生效"))
    modules.append(_context(f"检测时间 {time.strftime('%H:%M')}"))
    return _card_obj(color, modules)


def _render_p0_update_card(remote_sha: str, commits: list[str]) -> str:
    """P0 级核心漏洞修复提醒卡片：高优先级 + 操作按钮。

    按钮 value 按指令回传：
      .update check  → 先检查并展示待更新 diff
      .update        → 确认后应用更新
    符合"P0 是必须更新但非强制，检测到先 check 再确认"策略。
    """
    color = "#d9534f"  # 红（P0 高优先级）
    modules = [_header("KOOK BOT · P0 核心漏洞修复")]

    rows = [("远程版本", remote_sha[:7])]
    if commits:
        rows.append(("涉及提交", f"{len(commits)} 个"))
    modules.append(_section(rows))
    modules.append(_divider())

    modules.append({"type": "section", "text": {
        "type": "kmarkdown",
        "content": "**检测到 P0 级核心修复，建议尽快更新**\n"
                   "（非强制，但涉及安全/核心漏洞，请优先处理）",
    }})
    if commits:
        modules.append({"type": "section", "text": {"type": "kmarkdown", "content": "**更新内容**"}})
        for c in commits[:5]:
            modules.append(_context(f"`{c}`"))
        if len(commits) > 5:
            modules.append(_context(f"… 共 {len(commits)} 个提交"))
    modules.append(_divider())

    # 操作按钮：先查看更新，再确认应用
    modules.append({
        "type": "action-group",
        "elements": [
            _action_button("查看更新", ".update check", theme="primary"),
            _action_button("确认更新", ".update", theme="danger"),
        ],
    })
    modules.append(_context(f"检测时间 {time.strftime('%H:%M')}"))
    return _card_obj(color, modules)


def render_update_check_card(text: str) -> str:
    """把 .update check 的纯文本结果渲染成 KOOK 卡片。

    解析规则：
      - 摘要行（风险等级/依赖阻断说明等）→ context
      - 缩进的文件差异行（`[LOW] xx.py  +1 -2`）→ 文件差异列表
      - `更新日志:` 之后的提交行 → 更新日志列表
    卡片底部提供「应用更新」按钮（仅 admin 点击才生效）。
    """
    color = "#f0ad4e"
    modules = [_header("KOOK BOT · 更新检查")]
    lines = text.splitlines()

    if text.strip().startswith("已是最新"):
        modules.append(_context("当前已是最新版本，无需更新。"))
        modules.append(_context(f"检测时间 {time.strftime('%H:%M')}"))
        # 必须返回 card 对象（list），与下方分支一致。
        # 若返回 [CARD] 字符串，send_raw_group/_send_raw 会把它当 type=10 的 content，
        # KOOK 报 40000 json格式不正确。
        return _card_obj(color, modules)

    summary: list[str] = []
    file_lines: list[str] = []
    log_lines: list[str] = []
    in_log = False
    for ln in lines:
        if ln.startswith("更新日志:"):
            in_log = True
            continue
        if in_log:
            if ln.strip():
                log_lines.append(ln.strip())
            continue
        if ln.strip().startswith("  ") and "[" in ln:
            file_lines.append(ln.strip())
        elif ln.strip():
            summary.append(ln.strip())

    if summary:
        modules.append(_context("\n".join(summary)))
    if file_lines:
        modules.append(_divider())
        modules.append({"type": "section", "text": {"type": "kmarkdown", "content": "**文件差异**"}})
        for fl in file_lines[:20]:
            modules.append(_context(f"`{fl}`"))
    if log_lines:
        modules.append(_divider())
        modules.append({"type": "section", "text": {"type": "kmarkdown", "content": "**更新日志**"}})
        for lg in log_lines[:10]:
            modules.append(_context(f"`{lg}`"))

    # 有可用更新才给「应用更新」按钮（admin 点击才生效）
    if file_lines:
        modules.append(_divider())
        modules.append({
            "type": "action-group",
            "elements": [_action_button("应用更新", ".update", theme="danger")],
        })
    modules.append(_context(f"检测时间 {time.strftime('%H:%M')}"))
    return _card_obj(color, modules)


# ── 高风险人工审批 ──────────────────────────────────────────

# 待确认的审批：token -> asyncio.Future(bool)
_approval_pending: dict[str, asyncio.Future] = {}


def _render_approval_card(token: str, files: list[dict], assessment) -> list:
    """高风险更新人工确认卡片：展示涉及文件 + 确认/取消按钮。"""
    color = "#d9534f"  # 红
    modules = [_header("KOOK BOT · 高风险更新确认")]
    high_files = getattr(assessment, "high_files", None) or [
        f.get("filename", "") for f in files
    ]
    modules.append(_section([
        ("风险等级", f"**{assessment.level}**"),
        ("高风险文件", f"{len(high_files)} 个"),
    ]))
    modules.append(_divider())
    modules.append({"type": "section", "text": {
        "type": "kmarkdown", "content": "**涉及高风险文件**"}})
    for name in high_files[:20]:
        modules.append(_context(f"`{name}`"))
    if len(high_files) > 20:
        modules.append(_context(f"… 共 {len(high_files)} 个高风险文件"))
    modules.append(_divider())
    modules.append({
        "type": "action-group",
        "elements": [
            _action_button("确认更新", f".update approve {token}", theme="danger"),
            _action_button("取消更新", f".update deny {token}", theme="secondary"),
        ],
    })
    modules.append(_context(f"审批有效期 10 分钟 · 仅 admin 点击生效"))
    return _card_obj(color, modules)


async def request_update_approval(files: list[dict], assessment, timeout: float = 600.0) -> bool:
    """高风险更新人工审批回调（注入 safe_update 的 set_approve_callback）。

    发卡片到目标频道，等待 admin 点击「确认更新/取消更新」按钮，返回是否放行。
    超时（默认 10 分钟）未处理则默认拒绝。
    """
    import uuid
    token = uuid.uuid4().hex[:16]
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _approval_pending[token] = fut
    try:
        await _send_to_targets(_render_approval_card(token, files, assessment))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("高风险更新审批超时 token=%s", token)
            return False
    finally:
        _approval_pending.pop(token, None)


def resolve_approval(token: str, approved: bool) -> str:
    """点击审批按钮后调用：把结果回传给等待中的请求。"""
    fut = _approval_pending.get(token)
    if not fut or fut.done():
        return "审批已失效或已处理"
    fut.set_result(approved)
    return "已确认更新" if approved else "已取消更新"


# ── 发送 ────────────────────────────────────────────────────

async def _send_to_targets(content) -> None:
    """把消息发到所有配置的目标频道（字频道 ID）。

    content 可以是 [CARD] 字符串（静态卡片）或 card 对象列表（交互卡片，走原生 type=10）。
    """
    cfg = load_cfg()
    targets = cfg.get("target_channels", [])
    if not targets:
        logger.info("notify: 未配置目标频道，跳过发送")
        return
    from services.sender import send_group_msg, send_raw_group
    is_raw = isinstance(content, (dict, list))
    sent = 0
    for cid in targets:
        try:
            if is_raw:
                await send_raw_group(content, int(cid))
            else:
                await send_group_msg(content, int(cid))
            sent += 1
        except Exception as e:
            logger.warning("notify 发送失败 channel=%s: %s", cid, e)
    logger.info("notify 已发送 %d/%d 个频道", sent, len(targets))


# ── 性能检查 ────────────────────────────────────────────────

def _percentile(values: list[float], p: float) -> float:
    """计算百分位数（线性插值）。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (len(s) - 1) * (p / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


async def check_perf_and_notify() -> None:
    """检查性能窗口，超阈值发降级卡片；恢复后发恢复卡片（状态机去重）。

    Phase 20 Part1：降级判定改用 P95（历史分位数）而非均值，避免单次慢请求误报。
    只有当窗口内 P95 持续高于阈值（或超时率过高）才判定为 LLM 降级。
    """
    global _perf_state, _last_perf_check
    cfg = load_cfg()
    if not cfg.get("perf_enabled", True) or not cfg.get("target_channels"):
        return
    # 节流：距上次检查不足 60s 直接跳过
    now = time.time()
    if now - _last_perf_check < 60:
        return
    _last_perf_check = now

    with _latency_lock:
        window = list(_latency_window)
    if len(window) < 5:
        return  # 样本太少

    samples = sorted(w[0] for w in window)
    avg_ms = sum(samples) / len(samples)
    p50_ms = _percentile(samples, 50)
    p95_ms = _percentile(samples, 95)
    p99_ms = _percentile(samples, 99)
    timeout_n = sum(1 for w in window if not w[1])
    timeout_rate = timeout_n / len(window)
    threshold = float(cfg.get("perf_avg_ms", 2000))
    thr_rate = float(cfg.get("perf_timeout_rate", 0.5))

    # 用 P95 判定降级：单次慢请求不会抬高 P95，只有持续变慢才触发。
    degraded = (p95_ms > threshold) or (timeout_rate > thr_rate)

    with _perf_state_lock:
        if degraded and _perf_state != "degraded":
            _perf_state = "degraded"
            card = _render_perf_degraded(avg_ms, p50_ms, p95_ms, p99_ms,
                                         timeout_rate, len(window), threshold)
            await _send_to_targets(card)
            logger.info("性能降级通知已发: avg=%.0fms p50=%.0fms p95=%.0fms p99=%.0fms rate=%.0f%%",
                        avg_ms, p50_ms, p95_ms, p99_ms, timeout_rate * 100)
        elif (not degraded) and _perf_state == "degraded":
            _perf_state = None
            card = _render_perf_recovered(avg_ms, p50_ms, p95_ms, len(window))
            await _send_to_targets(card)
            logger.info("性能恢复通知已发: avg=%.0fms p95=%.0fms", avg_ms, p95_ms)


# ── GitHub 更新检查 ─────────────────────────────────────────

def _git_ls_remote_sha() -> Optional[str]:
    """返回远程仓库当前 HEAD SHA（git ls-remote），失败返回 None"""
    try:
        r = subprocess.run(
            ["git", "ls-remote", "origin", "HEAD"],
            capture_output=True, text=True, timeout=15,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.split()[0]
    except Exception as e:
        logger.debug("git ls-remote 失败: %s", e)
    return None


def _git_local_sha() -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _git_recent_commits(n: int = 6) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "log", "-n", str(n), "--oneline"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if r.returncode == 0:
            return [ln for ln in r.stdout.strip().splitlines() if ln][:n]
    except Exception:
        pass
    return []


def _git_pending_commits(local_sha: str, remote_sha: str) -> list[str]:
    """返回本地 HEAD 与远程 HEAD 之间新增的 commit message 纯文本（不含 SHA 前缀）。

    用于分级：需读取 commit 首行原始消息，而非 --oneline 的含 SHA 格式，
    以便 severity 模块识别 [FEAT]/[BUGFIX]/[CORE]/[P0] 前缀。
    """
    try:
        r = subprocess.run(
            ["git", "log", f"{local_sha}..{remote_sha}", "--format=%s"],
            capture_output=True, text=True, timeout=15,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if r.returncode == 0:
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:
        logger.debug("git log 获取 pending commits 失败: %s", e)
    return []


async def check_github_update() -> None:
    """检查远程 SHA，与本地不同则按更新分级处理。

    普通级（FEAT/BUGFIX/CORE）：仅记录检查状态，不主动发卡片；
    用户手动 .update 时才应用。
    P0 级：立即发高优先级卡片 + 按钮提醒（先 check 再确认）。
    去重：仅当 SHA 变化时处理。
    """
    cfg = load_cfg()
    if not cfg.get("update_enabled", True) or not cfg.get("target_channels"):
        return
    remote = _git_ls_remote_sha()
    local = _git_local_sha()
    if not remote or not local:
        return
    if remote == local:
        return  # 无新提交
    if cfg.get("last_notified_sha") == remote:
        return  # 已处理过这个版本

    from modules._auto_update.severity import classify_commits, is_p0
    pending = _git_pending_commits(local, remote)
    level = classify_commits(pending)

    if is_p0(level):
        card = _render_p0_update_card(remote, pending)
        await _send_to_targets(card)
        logger.warning("P0 更新通知已发: local=%s remote=%s commits=%d",
                       local[:7], remote[:7], len(pending))
    else:
        # 普通级：仅记录，不发卡片（用户 .update 时再应用）
        logger.info("检测到普通级更新(%s): local=%s remote=%s commits=%d，不主动通知",
                    level, local[:7], remote[:7], len(pending))

    cfg["last_notified_sha"] = remote
    save_cfg(cfg)


async def notify_github_update(remote_sha: str, commits: list[str] | None = None) -> bool:
    """GitHub Action webhook 触发的即时更新通知（推送即发，无需轮询）

    remote_sha: 远程新版本 SHA；commits: 新增提交的一行摘要列表（纯 message，不含作者）。
    按更新分级处理：
      - 普通级（FEAT/BUGFIX/CORE）：仅记录，不主动发卡片。
      - P0 级：发「P0 核心漏洞修复」卡片 + 按钮（先 check 再确认）。
    去重：last_notified_sha 相同则不重复处理。
    返回 True=已发送/已记录，False=跳过/失败。
    """
    cfg = load_cfg()
    if not cfg.get("update_enabled", True) or not cfg.get("target_channels"):
        return False
    if not remote_sha:
        return False
    if cfg.get("last_notified_sha") == remote_sha:
        return False  # 已处理过这个版本

    from modules._auto_update.severity import classify_commits, is_p0
    level = classify_commits(commits or [])
    if is_p0(level):
        card = _render_p0_update_card(remote_sha, commits or [])
        await _send_to_targets(card)
        logger.warning("P0 更新通知已发(webhook): remote=%s commits=%d",
                       remote_sha[:7], len(commits or []))
    else:
        logger.info("检测到普通级更新(%s)(webhook): remote=%s commits=%d，不主动通知",
                    level, remote_sha[:7], len(commits or []))

    cfg["last_notified_sha"] = remote_sha
    save_cfg(cfg)
    return True


async def _head_sha_api() -> str:
    """通过 GitHub API 获取远程分支 HEAD SHA（服务器非 git 仓库时 git 命令不可用）"""
    try:
        from modules._auto_update.engine import GITHUB_API, GITHUB_BRANCH
        import httpx
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.get(
                f"{GITHUB_API}/commits/{GITHUB_BRANCH}",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            return r.json().get("sha", "")
    except Exception:
        return ""


async def _fetch_commit_info(sha: str) -> dict | None:
    """通过 GitHub API 获取单个 commit 信息（短 hash 亦可）。

    返回 {"sha": 完整sha, "message": 首行message, "files": [{filename,status}]}
    """
    if not sha:
        return None
    try:
        from modules._auto_update.engine import GITHUB_API
        import httpx
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.get(
                f"{GITHUB_API}/commits/{sha}",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            d = r.json()
            files = [
                {"filename": f.get("filename", ""), "status": f.get("status", "")}
                for f in d.get("files", [])
            ]
            return {
                "sha": d.get("sha", ""),
                "message": d.get("commit", {}).get("message", "").split("\n")[0],
                "files": files,
            }
    except Exception:
        return None


def _render_resend_text(full_sha: str, message: str, files: list[dict], level: str) -> str:
    """构造补发通知的普通级更新检查文本（供 render_update_check_card 解析）"""
    lines = [f"远程版本：**{full_sha[:7]}**", f"更新内容:{message}（等级 [{level}]）"]
    if files:
        lines.append("")
        for f in files[:20]:
            lines.append(f"  [{level}] {f['filename']}  ({f['status']})")
        if len(files) > 20:
            lines.append(f"  … 共 {len(files)} 个文件")
    lines.append("")
    lines.append("更新日志:")
    lines.append(f"  {full_sha[:7]}  {message}")
    return "\n".join(lines)


async def resend_github_update(num: str = "", commits: list[str] | None = None) -> str:
    """补发 git 更新通知（忽略 last_notified_sha 去重）。

    - 无参数：默认补发远程最新版本（HEAD）的更新通知。
    - 带短 hash：查询并补发该 commit 的更新内容。
    用于 .update pull 后顶掉了 webhook/轮询更新通知时的手动补发：
      - P0 级 → 重发「P0 核心漏洞修复」红色卡片 + 查看/确认按钮
      - 普通级 → 重发「更新检查」卡片（含文件差异 + 应用更新按钮）
    返回面向用户的文本总结。
    """
    cfg = load_cfg()
    if not cfg.get("target_channels"):
        return "补发失败：尚未配置通知目标频道，先执行 .notify 选择频道"

    from modules._auto_update.severity import classify_commits, is_p0

    # 目标 commit：指定短 hash 则查它，否则取远程 HEAD（最新版本）
    target = num or await _head_sha_api()
    if not target:
        return "补发失败：无法获取远程版本 SHA"
    info = await _fetch_commit_info(target)
    if not info:
        return f"补发失败：无法获取 commit {target[:7]} 的信息"
    full_sha = info["sha"]
    message = info["message"] or "(无提交信息)"
    files = info["files"]

    level = classify_commits([message])
    if is_p0(level):
        card = _render_p0_update_card(full_sha, [message])
        await _send_to_targets(card)
        msg = f"已补发 P0 更新通知（{full_sha[:7]}：{message}）"
    else:
        text = _render_resend_text(full_sha, message, files, level)
        card = render_update_check_card(text)
        await _send_to_targets(card)
        msg = f"已补发更新通知（等级 {level}，{full_sha[:7]}：{message}）"

    cfg["last_notified_sha"] = full_sha
    save_cfg(cfg)
    return msg


# ── 指令支持 ────────────────────────────────────────────────

def get_target_channels() -> list[int]:
    return list(load_cfg().get("target_channels", []))


def set_target_channels(channel_ids: list[int]) -> None:
    cfg = load_cfg()
    cfg["target_channels"] = [int(c) for c in channel_ids]
    save_cfg(cfg)


def add_target_channel(channel_id) -> bool:
    cfg = load_cfg()
    lst = cfg.get("target_channels", [])
    if int(channel_id) in lst:
        return False
    lst.append(int(channel_id))
    cfg["target_channels"] = lst
    save_cfg(cfg)
    return True


def set_perf_enabled(enabled: bool) -> tuple[bool, bool]:
    """开/关 LLM 性能降级通知（perf_enabled）。返回 (成功与否, 当前开关状态)。

    关闭后 check_perf_and_notify 不再发送降级/恢复卡片；开启则恢复，并重置
    降级状态机，让下一次检查重新评估（避免沿用关闭前的 degraded 状态）。
    """
    global _perf_state
    cfg = load_cfg()
    cfg["perf_enabled"] = bool(enabled)
    save_cfg(cfg)
    if enabled:
        with _perf_state_lock:
            _perf_state = None
    return True, bool(enabled)


def get_perf_enabled() -> bool:
    return bool(load_cfg().get("perf_enabled", True))


# ── 后台任务循环 ────────────────────────────────────────────

async def notify_loop() -> None:
    """后台任务：性能每 60s 检查 + 更新每 interval 检查"""
    cfg = load_cfg()
    update_interval = float(cfg.get("update_interval_s", 1800))
    last_update_check = 0.0
    while True:
        try:
            await check_perf_and_notify()
            now = time.time()
            if now - last_update_check >= update_interval:
                await check_github_update()
                last_update_check = now
        except Exception as e:
            logger.warning("notify 后台任务异常: %s", e)
        await asyncio.sleep(60)
