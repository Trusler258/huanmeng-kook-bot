"""
Phigros 指令模块
.pgr help         查看帮助
.pgr login       生成 TapTap 登录二维码（自动保存 token）
.pgr me [token]  查询我的存档（不传 token 则用保存的）
.pgr bx [N]      展示最好的 N 个成绩（默认 10）
.pgr top [N]     RKS 排行榜
.pgr song <曲名>  搜索曲目
.pgr new         新曲速递
"""

import asyncio, time, json, os

_TOKEN_FILE = "data/pgr_tokens.json"
from services.pgr import (
    generate_qrcode, poll_qrcode, get_profile,
    get_leaderboard, search_song, get_new_songs,
    format_profile,
)

_POLL_INTERVAL = 3  # 轮询间隔（秒）
_POLL_TIMEOUT = 120  # 轮询超时（秒）

# ── Token 存储 ──────────────────────────────────────────

def _load_tokens() -> dict:
    try:
        if os.path.exists(_TOKEN_FILE):
            with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_tokens(data: dict):
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def _get_token(user_id: int) -> str | None:
    return _load_tokens().get(str(user_id))

def _set_token(user_id: int, token: str):
    data = _load_tokens()
    data[str(user_id)] = token
    _save_tokens(data)

def _del_token(user_id: int):
    data = _load_tokens()
    data.pop(str(user_id), None)
    _save_tokens(data)


async def cmd_pgr(args, user_id, group_id, sender_name, is_group, bot_kook):
    from core.logger import get_logger
    logger = get_logger("pgr")

    chat_id = group_id if is_group else user_id
    if not args:
        return _help()

    sub = args[0].lower()

    if sub == "help":
        return _help()

    elif sub == "login":
        return await _login(chat_id, is_group, user_id, logger)

    elif sub == "logout":
        _del_token(user_id)
        return "✅ 已解绑 sessionToken"

    elif sub == "me":
        token = args[1] if len(args) > 1 else _get_token(user_id)
        if not token:
            return "未找到你的 sessionToken。请先 .pgr login 登录，或者手动传入: .pgr me <token>"
        return await _query_profile(token, logger)

    elif sub.startswith("b") and sub[1:].isdigit():
        n = int(sub[1:])
        token = _get_token(user_id)
        if not token:
            return "未找到你的 sessionToken，请先 .pgr login"
        result = await _query_best(token, n, logger, chat_id, is_group, user_id)
        return result if result else ""

    elif sub == "top":
        n = int(args[1]) if len(args) > 1 else 10
        return await _leaderboard(n, logger)

    elif sub == "song":
        if len(args) < 2:
            return "用法: .pgr song <曲名关键词>"
        keyword = " ".join(args[1:])
        return await _search_song(keyword, logger)

    elif sub == "new":
        return await _new_songs(logger)

    else:
        # 可能是直接传 sessionToken
        return await _query_profile(args[0], logger)


async def _login(chat_id: int, is_group: bool, user_id: int, logger) -> str:
    """生成二维码 → 发送图片 → 轮询 → 返回 sessionToken"""
    from services.sender import send_by_chat_type

    try:
        qr = generate_qrcode()
    except Exception as e:
        logger.error("生成二维码失败: %s", e)
        return f"生成登录失败喵~ ({e})"

    qr_id = qr.get("qrId", "")
    qr_b64 = qr.get("qrcodeBase64", "")
    verify_url = qr.get("verificationUrl", "")

    if not qr_id or not verify_url:
        return "生成登录失败：API 返回数据不完整"

    # 用 verify_url 生成 PNG 二维码并发送（KOOK 中暂不支持撤回，二维码 2 分钟后自动失效）
    import tempfile, os as _os
    try:
        import qrcode, io as _io_
        img = qrcode.make(verify_url)
        buf = _io_.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)

        fd, img_path = tempfile.mkstemp(suffix=".png")
        _os.write(fd, buf.read())
        _os.close(fd)

        cq_img = f"[img:file:{img_path}]"
        tip = f"📱 请用 TapTap App 扫描二维码登录\n\n或访问: {verify_url}\n{cq_img}"

        await send_by_chat_type(tip, chat_id, is_group, user_id=user_id)
        logger.debug("QR 消息已发送")
    except ImportError:
        pass

    # 轮询登录状态
    start = time.time()
    last_status = ""
    while time.time() - start < _POLL_TIMEOUT:
        try:
            result = poll_qrcode(qr_id)
        except Exception as e:
            logger.debug("轮询异常: %s", e)
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        status = result.get("status", "")
        if status != last_status:
            logger.info("QR 登录状态: %s", status)
            last_status = status

        # 有 token 就算成功（不管 status 是 success/confirmed/completed）
        token = result.get("sessionToken", "")
        if token:
            _set_token(user_id, token)
            masked = token[:2] + "*" * (len(token) - 4) + token[-2:] if len(token) > 4 else "***"
            logger.info("PGR 登录成功: user=%d token=%s", user_id, masked)
            return f"✅ 登录成功！（token 已自动保存）\n\n使用: .pgr me 查看存档\n或: .pgr b30 查看最佳成绩"

        if status in ("failed", "expired", "cancelled"):
            return f"登录失败：{status}"

        # Confirmed 但没 token → 继续等
        await asyncio.sleep(_POLL_INTERVAL)

    return "登录超时（2分钟），请重新 .pgr login"


async def _query_profile(token: str, logger) -> str:
    """查询玩家存档"""
    try:
        data = get_profile(token)
    except Exception as e:
        logger.error("查询存档失败: %s", e)
        return f"查询存档失败: {e}"

    return format_profile(data)


async def _query_best(token: str, n: int, logger, chat_id, is_group, user_id) -> str:
    """发送 BestN 可视化卡片（SVG→PNG via cairosvg）"""
    from services.pgr import get_bestn_image
    from services.sender import send_group_msg, send_private_msg

    try:
        svg = get_bestn_image(token, n=n)
    except Exception as e:
        logger.error("获取 Best%d 图片失败: %s", n, e)
        return f"获取 Best{n} 图片失败: {e}"

    # SVG → PNG via Playwright（复用 changelog 的单例浏览器）
    try:
        from modules.changelog import _ensure_browser
        import tempfile, os as _os

        browser = await _ensure_browser()
        page = await browser.new_page(viewport={"width": 900, "height": 1400})
        await page.set_content(svg)
        await page.wait_for_timeout(500)
        img_bytes = await page.screenshot(full_page=True, type="png")
        await page.close()

        fd, img_path = tempfile.mkstemp(suffix=".png", dir="/tmp")
        _os.write(fd, img_bytes)
        _os.close(fd)

        cq_img = f"[img:file:{img_path}]"
        if is_group:
            await send_group_msg(cq_img, group_id=chat_id)
        else:
            await send_private_msg(cq_img, user_id)
    except Exception as e:
        logger.error("Best%d 图片渲染失败: %s", n, e)
        return f"Best{n} 图片渲染失败: {e}"
    return None


async def _leaderboard(n: int, logger) -> str:
    try:
        data = get_leaderboard(n)
    except Exception as e:
        return f"获取排行榜失败: {e}"

    items = data.get("data", [])[:n]
    if not items:
        return "暂无排行榜数据"

    lines = [f"===== Phigros RKS 排行榜 Top {n} ====="]
    for i, item in enumerate(items, 1):
        name = item.get("name", "?")
        rks = item.get("rks", 0)
        lines.append(f"  #{i} {name}  RKS={rks:.2f}")
    return "\n".join(lines)


async def _search_song(keyword: str, logger) -> str:
    try:
        data = search_song(keyword)
    except Exception as e:
        return f"搜索失败: {e}"

    items = data.get("data", [])[:10]
    if not items:
        return f"未找到与 '{keyword}' 相关的曲目"

    lines = [f"===== 搜索 '{keyword}' ====="]
    for item in items:
        name = item.get("name", "?")
        artist = item.get("artist", "?")
        bpm = item.get("bpm", "?")
        lines.append(f"  {name} / {artist}  BPM={bpm}")
    return "\n".join(lines)


async def _new_songs(logger) -> str:
    try:
        data = get_new_songs()
    except Exception as e:
        return f"获取新曲失败: {e}"

    items = data.get("data", [])[:15]
    if not items:
        return "暂无新曲数据"

    lines = ["===== 新曲速递 ====="]
    for item in items:
        name = item.get("name", "?")
        artist = item.get("artist", "?")
        lines.append(f"  {name} / {artist}")
    return "\n".join(lines)


def _help() -> str:
    return (
        "【Phigros 查询 .pgr】\n"
        "  pgr login          生成登录二维码（token 自动保存）\n"
        "  pgr logout         解绑 sessionToken\n"
        "  pgr me             查看我的存档（RKS+评级+Best30）\n"
        "  pgr b<N>          展示最好的 N 个成绩（如 b30, b10）\n"
        "  pgr top [N]        RKS 排行榜 Top N（默认10）\n"
        "  pgr song <曲名>    搜索曲目\n"
        "  pgr new            新曲速递\n"
        "\n"
        "  → 先 .pgr login 扫码登录（只需一次）\n"
        "  → 然后 .pgr me 或 .pgr bx 直接查"
    )
