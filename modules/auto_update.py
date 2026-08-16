"""
自动更新模块 v2 — Git Patch 行级增量合并（Phase 16 升级为安全代码级更新）

.update       手动触发增量更新（走安全流水线：Fetch→Diff→分析→评估→快照→应用→Health→回滚）
.update check 只检查不下载
.update force 强制全量对比（跳过 SHA 缓存）
.update resend  补发被顶掉的 git 更新通知（绕过去重，P0 发红色卡片 / 普通级发更新检查卡片）
.upd          同上（短别名）
.upd test     公开连通性测试（无需权限）
"""

from __future__ import annotations

import asyncio
import os

import httpx

from core.logger import get_logger
from modules._auto_update.engine import check_and_update, GITHUB_API, GITHUB_REPO, GITHUB_BRANCH
from modules._auto_update.safe_update import safe_check_and_update

logger = get_logger("auto_update")


async def _restart_after(delay: float):
    """延迟后强退进程。依赖 systemd Restart=always 自动拉起新进程加载新代码。"""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        logger.warning("自动重启任务被取消，请手动重启使更新生效")
        return
    except Exception:
        logger.exception("自动重启任务异常")
        return
    logger.info("更新完成，自动重启进程...")
    os._exit(0)


def _schedule_restart(delay: float = 5.0) -> bool:
    """安排延迟自动重启；失败返回 False（调用方回退为仅提示手动重启）。"""
    try:
        asyncio.create_task(_restart_after(delay))
        return True
    except Exception:
        logger.exception("安排自动重启失败")
        return False


def _parse_update_args(args):
    """解析 .update 的 bash 风格参数。

    支持 -y/--yes（跳过高风险二次确认直接装）、-q/--quiet（静默，只提示完成+重启），
    以及子命令 check/force/test/resend/approve/deny。
    返回 (yes, quiet, sub, rest)。
    """
    yes = quiet = False
    sub = None
    rest = []
    for a in (args or []):
        al = str(a).lower()
        if al in ("-y", "--yes", "-yes", "yes"):
            yes = True
        elif al in ("-q", "--quiet", "-quiet", "quiet"):
            quiet = True
        elif sub is None and al in ("check", "force", "test", "resend", "notify", "补发", "approve", "deny"):
            sub = al
        else:
            rest.append(a)
    return yes, quiet, sub, rest


async def cmd_update(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.update [check|force|test|resend] [-y] [-q]"""
    yes, quiet, sub, rest = _parse_update_args(args)

    # test 模式无需权限，任何人可用
    if sub == "test":
        return await _test_connectivity()

    try:
        from core.config import load_roles_config
        roles = load_roles_config()
        admin_qq = roles.get("admin_qq", 0)
        if admin_qq and user_id != admin_qq:
            return "权限不足喵~"
    except Exception:
        pass

    # 补发被顶掉的 git 更新通知（需权限，绕过去重）
    if sub in ("resend", "notify", "补发"):
        from services.notify_system import resend_github_update
        sha_arg = rest[0] if rest else ""
        return await resend_github_update(sha_arg)

    # 高风险更新人工审批：点击卡片「确认/取消更新」按钮后回传
    if sub in ("approve", "deny"):
        from services.notify_system import resolve_approval
        token = rest[0] if rest else ""
        return resolve_approval(token, sub == "approve")

    check_only = sub == "check"
    force = sub == "force"

    # Phase 16：默认走安全代码级更新流水线（含 Snapshot/Test/Health/Rollback）
    from core.eventbus import get_event_bus, EVENT_UPDATE_STARTED, EVENT_UPDATE_COMPLETED
    get_event_bus().emit(EVENT_UPDATE_STARTED, {
        "user_id": user_id, "check_only": check_only, "force": force,
        "yes": yes, "quiet": quiet,
    })

    # 注入进度回调：更新应用期间实时上报进度到原频道；-q 静默时不推送进度
    from services.sender import send_by_chat_type
    async def _progress(msg: str):
        await send_by_chat_type(f"**[更新进度]** {msg}", group_id, is_group,
                                user_id=user_id if not is_group else None)

    result = await safe_check_and_update(
        check_only=check_only, force=force, require_approval=not yes,
        progress=None if (check_only or quiet) else _progress,
    )

    get_event_bus().emit(EVENT_UPDATE_COMPLETED, {
        "user_id": user_id, "check_only": check_only, "force": force,
        "yes": yes, "quiet": quiet, "result": result[:200],
    })

    if check_only:
        # 结果以 KOOK 原生卡片形式发送（含文件差异 + 应用更新交互按钮）
        from services.notify_system import render_update_check_card
        from services.sender import send_raw_group, send_raw_user
        card = render_update_check_card(result)
        if is_group:
            await send_raw_group(card, group_id)
        else:
            await send_raw_user(card, user_id)
        return None
    if result and ("已更新" in result or "已安全更新" in result) and "个文件" in result:
        # 更新成功 → 延迟自动重启，由上层先发出提示消息，再让 systemd 拉起新进程
        if _schedule_restart(5.0):
            logger.info("更新成功，已安排 5 秒后自动重启")
            if quiet:
                return "✅ 更新完成，正在重启…"
            result += "\n\n✅ 更新成功，5 秒后自动重启机器人，请稍候…"
        else:
            result += "\n\n建议重启 bot 使更新生效"
    return result


async def _test_connectivity() -> str:
    """公开测试：检查 GitHub 连通性 + 仓库可达性"""
    import time
    t0 = time.time()
    lines = ["【自动更新连通性测试】\n"]

    # 1. DNS / 直连
    try:
        async with httpx.AsyncClient(timeout=8, verify=False) as c:
            r = await c.get(f"{GITHUB_API}/commits/{GITHUB_BRANCH}")
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha", "?")[:7]
            msg = (data.get("commit", {}).get("message", "?").split("\n")[0])[:40]
            lines.append(f"GitHub 连通: OK ({ms}ms)")
            lines.append(f"仓库: {GITHUB_REPO}@{GITHUB_BRANCH}")
            lines.append(f"最新提交: {sha} — {msg}")
        elif r.status_code == 403:
            lines.append(f"GitHub 连通: OK ({ms}ms) 但触发限流，稍后再试")
        else:
            lines.append(f"GitHub 响应异常: HTTP {r.status_code} ({ms}ms)")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        lines.append(f"GitHub 不通 ({ms}ms): {str(e)[:100]}")

    # 2. 本地 git 状态
    try:
        from pathlib import Path
        state = Path("data/auto_update_state.json")
        if state.exists():
            import json
            d = json.loads(state.read_text())
            lines.append(f"本地追踪文件: {len(d)} 个")
        else:
            lines.append("本地状态: 无缓存（首次更新将全量对比）")
    except Exception:
        lines.append("本地状态: 读取失败")

    lines.append("\n结论: 自动更新系统可正常工作" if "OK" in lines[1] else "\n结论: 网络不通，检查服务器防火墙或代理")
    return "\n".join(lines)
