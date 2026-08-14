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

def _card(color: str, modules: list) -> str:
    """渲染一张 KOOK Card 的 JSON 字符串（自动补 [CARD] 标记）"""
    import json as _json
    obj = [{
        "type": "card",
        "theme": "secondary",
        "color": color,
        "size": "lg",
        "modules": modules,
    }]
    return f"[CARD]{_json.dumps(obj, ensure_ascii=False)}[/CARD]"


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

def _render_perf_degraded(avg_ms: float, timeout_rate: float, window_n: int, threshold: float) -> str:
    color = "#d9534f"  # 红
    modules = [
        _header("KOOK BOT · LLM 服务降级"),
        _section([
            ("服务", "LLM API"),
            ("状态", "已降级"),
            ("平均延迟", f"{avg_ms:.0f} ms"),
            ("正常阈值", f"{threshold:.0f} ms"),
            ("超时率", f"{timeout_rate * 100:.0f}%"),
            ("统计窗口", f"最近 {window_n} 次调用"),
        ]),
        _divider(),
        _context("建议：回复 .sys 查看主机状态；.cost 查看 API 消耗；仍异常可 .restart 重启恢复。"),
        _context(f"检测时间 {time.strftime('%H:%M')} · 恢复后自动通知"),
    ]
    return _card(color, modules)


def _render_perf_recovered(avg_ms: float, window_n: int) -> str:
    color = "#2ecc71"  # 绿
    modules = [
        _header("KOOK BOT · LLM 服务已恢复"),
        _section([
            ("服务", "LLM API"),
            ("状态", "正常"),
            ("平均延迟", f"{avg_ms:.0f} ms"),
            ("统计窗口", f"最近 {window_n} 次调用"),
        ]),
        _divider(),
        _context(f"检测时间 {time.strftime('%H:%M')}"),
    ]
    return _card(color, modules)


# ── GitHub 更新卡片 ─────────────────────────────────────────

def _render_update_card(local_sha: str | None, remote_sha: str, commits: list[str]) -> str:
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
    modules.append(_context("回复 .update 拉取并应用更新 · .up 查看完整更新日志"))
    modules.append(_context(f"检测时间 {time.strftime('%H:%M')}"))
    return _card(color, modules)


# ── 发送 ────────────────────────────────────────────────────

async def _send_to_targets(content: str) -> None:
    """把消息发到所有配置的目标频道（字频道 ID）"""
    cfg = load_cfg()
    targets = cfg.get("target_channels", [])
    if not targets:
        logger.info("notify: 未配置目标频道，跳过发送")
        return
    from services.sender import send_group_msg
    sent = 0
    for cid in targets:
        try:
            await send_group_msg(content, int(cid))
            sent += 1
        except Exception as e:
            logger.warning("notify 发送失败 channel=%s: %s", cid, e)
    logger.info("notify 已发送 %d/%d 个频道", sent, len(targets))


# ── 性能检查 ────────────────────────────────────────────────

async def check_perf_and_notify() -> None:
    """检查性能窗口，超阈值发降级卡片；恢复后发恢复卡片（状态机去重）"""
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

    avg_ms = sum(w[0] for w in window) / len(window)
    timeout_n = sum(1 for w in window if not w[1])
    timeout_rate = timeout_n / len(window)
    threshold = float(cfg.get("perf_avg_ms", 2000))
    thr_rate = float(cfg.get("perf_timeout_rate", 0.5))

    degraded = (avg_ms > threshold) or (timeout_rate > thr_rate)

    with _perf_state_lock:
        if degraded and _perf_state != "degraded":
            _perf_state = "degraded"
            card = _render_perf_degraded(avg_ms, timeout_rate, len(window), threshold)
            await _send_to_targets(card)
            logger.info("性能降级通知已发: avg=%.0fms rate=%.0f%%", avg_ms, timeout_rate * 100)
        elif (not degraded) and _perf_state == "degraded":
            _perf_state = None
            card = _render_perf_recovered(avg_ms, len(window))
            await _send_to_targets(card)
            logger.info("性能恢复通知已发: avg=%.0fms", avg_ms)


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


async def check_github_update() -> None:
    """检查远程 SHA，与本地不同则发更新卡片（去重：仅当 SHA 变化时发）"""
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
        return  # 已通知过这个版本

    commits = _git_recent_commits()
    card = _render_update_card(local, remote, commits)
    await _send_to_targets(card)

    cfg["last_notified_sha"] = remote
    save_cfg(cfg)
    logger.info("更新通知已发: local=%s remote=%s", local[:7], remote[:7])


async def notify_github_update(remote_sha: str, commits: list[str] | None = None) -> bool:
    """GitHub Action webhook 触发的即时更新通知（推送即发，无需轮询）
    remote_sha: 远程新版本 SHA；commits: 新增提交的一行摘要列表
    去重：last_notified_sha 相同则不重复推送。
    返回 True=已发送，False=跳过/失败
    """
    cfg = load_cfg()
    if not cfg.get("update_enabled", True) or not cfg.get("target_channels"):
        return False
    if not remote_sha:
        return False
    if cfg.get("last_notified_sha") == remote_sha:
        return False  # 已通知过这个版本

    card = _render_update_card(None, remote_sha, commits or [])
    await _send_to_targets(card)

    cfg["last_notified_sha"] = remote_sha
    save_cfg(cfg)
    logger.info("更新通知已发(webhook): remote=%s commits=%d", remote_sha[:7], len(commits or []))
    return True


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
