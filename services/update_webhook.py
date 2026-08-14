"""
GitHub 更新 Webhook 接收端
- GitHub Action 在 push 后 POST JSON 到 http://<host>:62004/github-update
  body: {"sha": "<新版本SHA>", "commits": ["commit消息1", ...], ...}
- 收到后立即渲染更新卡片并发送（推送即通知，无需等 30 分钟轮询）
- 去重由 notify_system 的 last_notified_sha 保证（相同 SHA 不重复推送）
- 简单鉴权：Header X-Bot-Key 需匹配 BOT_UPDATE_KEY（默认 huanmeng_update_2026）
"""
from __future__ import annotations

import asyncio
import json
import os

from aiohttp import web

from core.logger import get_logger
from services.notify_system import notify_github_update

logger = get_logger("update_webhook")

_AUTH_KEY = os.environ.get("BOT_UPDATE_KEY", "huanmeng_update_2026")
_PORT = int(os.environ.get("BOT_UPDATE_PORT", "62004"))


def _extract_commits(body: dict) -> list[str]:
    """从 GitHub push payload 提取提交摘要（每行一条）"""
    out: list[str] = []
    for c in body.get("commits", []) or []:
        if not isinstance(c, dict):
            continue
        msg = (c.get("message") or "").strip().splitlines()
        first = msg[0].strip() if msg else ""
        author = (c.get("author") or {}).get("name", "") if isinstance(c.get("author"), dict) else ""
        if first:
            out.append(f"{first[:60]}" + (f"（{author}）" if author else ""))
    if not out:
        # 兼容只有 sha 的简化通知
        msg = (body.get("message") or "").strip().splitlines()
        if msg:
            out.append(msg[0].strip()[:60])
    return out


async def _handle_github_update(request: web.Request) -> web.Response:
    key = request.headers.get("X-Bot-Key", "")
    if key != _AUTH_KEY:
        logger.warning("webhook 鉴权失败: key=%s", key[:20] or "(空)")
        return web.Response(status=401, text="unauthorized")

    try:
        body = await request.json()
    except Exception:
        body = {}

    sha = str(body.get("sha") or "").strip()
    if not sha:
        logger.warning("webhook body 缺少 sha")
        return web.Response(status=400, text="missing sha")

    commits = _extract_commits(body)
    # 立即触发即时通知（async 任务，不等完成即返回 200）
    asyncio.ensure_future(notify_github_update(sha, commits))
    logger.info("收到 GitHub push 通知: sha=%s commits=%d", sha[:7], len(commits))
    return web.Response(status=200, text="ok")


async def start_update_webhook(port: int = _PORT) -> None:
    """启动 webhook HTTP 服务（常驻，直到事件循环关闭）"""
    app = web.Application()
    app.router.add_post("/github-update", _handle_github_update)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("GitHub 更新 Webhook 已监听 0.0.0.0:%d", port)
    # 常驻：被取消时由事件循环关闭 runner
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()
        raise
