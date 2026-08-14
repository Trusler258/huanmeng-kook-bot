"""
自动更新模块 v2 — Git Patch 行级增量合并（Phase 16 升级为安全代码级更新）

.update       手动触发增量更新（走安全流水线：Fetch→Diff→分析→评估→快照→应用→Health→回滚）
.update check 只检查不下载
.update force 强制全量对比（跳过 SHA 缓存）
.upd          同上（短别名）
.upd test     公开连通性测试（无需权限）
"""

from __future__ import annotations

import httpx

from modules._auto_update.engine import check_and_update, GITHUB_API, GITHUB_REPO, GITHUB_BRANCH
from modules._auto_update.safe_update import safe_check_and_update


async def cmd_update(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.update [check|force|test]"""
    # test 模式无需权限，任何人可用
    if args and args[0].lower() == "test":
        return await _test_connectivity()

    try:
        from core.config import load_roles_config
        roles = load_roles_config()
        admin_qq = roles.get("admin_qq", 0)
        if admin_qq and user_id != admin_qq:
            return "权限不足喵~"
    except Exception:
        pass

    check_only = args and args[0].lower() == "check"
    force = args and args[0].lower() == "force"

    # Phase 16：默认走安全代码级更新流水线（含 Snapshot/Test/Health/Rollback）
    from core.eventbus import get_event_bus, EVENT_UPDATE_STARTED, EVENT_UPDATE_COMPLETED
    get_event_bus().emit(EVENT_UPDATE_STARTED, {
        "user_id": user_id, "check_only": check_only, "force": force,
    })

    result = await safe_check_and_update(check_only=check_only, force=force)

    get_event_bus().emit(EVENT_UPDATE_COMPLETED, {
        "user_id": user_id, "check_only": check_only, "force": force,
        "result": result[:200],
    })

    if check_only:
        # 结果过长时 KOOK 单条消息会发送失败并触发 fallback，需分包发送
        MAX_CHUNK = 1700
        if len(result) <= MAX_CHUNK:
            return result
        from services.sender import send_by_chat_type
        for chunk in _split_chunks(result, MAX_CHUNK):
            await send_by_chat_type(chunk, group_id, is_group, user_id=user_id)
        return None
    if result and ("已更新" in result or "已安全更新" in result) and "个文件" in result:
        result += "\n\n建议重启 bot 使更新生效"
    return result


def _split_chunks(text: str, max_len: int) -> list[str]:
    """按行聚合切分长文本，尽量不切断单行，保证每块不超过 max_len。"""
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > max_len and cur:
            chunks.append(cur)
            cur = ""
        if len(line) > max_len:
            # 单行超长，硬切
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len])
            continue
        cur += line
    if cur:
        chunks.append(cur)
    return chunks or [text]


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
