"""
Agnes AI 多模态生成模块
- .draw <prompt>    文生图
- .video <prompt>   文生视频
- .img2video        图生视频 (需引用/回复图片)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.logger import get_logger

logger = get_logger("agnes")

AGNES_BASE = "https://apihub.agnes-ai.com/v1"
CST = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "agnes_output"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_api_key() -> str:
    return os.environ.get("AGNES_KEY", "") or os.environ.get("AGNES_API_KEY", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


# ── 配额管理 ────────────────────────────────────────────

_QUOTA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DRAW_LIMIT = 10  # 每人每天画图次数
DEFAULT_VIDEO_LIMIT = 4  # 每人每天视频次数


def _load_quota(filename: str) -> dict:
    p = _QUOTA_DIR / filename
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"daily_reset": "", "users": {}}


def _save_quota(filename: str, data: dict):
    _QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    (_QUOTA_DIR / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_daily_reset(data: dict) -> dict:
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if data.get("daily_reset") != today:
        data["daily_reset"] = today
        for uid in data.get("users", {}):
            data["users"][uid]["used"] = 0
    return data


def _get_user_quota(data: dict, user_id: int, default_limit: int) -> tuple[int, int]:
    """返回 (剩余, 上限)"""
    uid = str(user_id)
    u = data.setdefault("users", {}).setdefault(uid, {"used": 0, "limit": default_limit})
    u.setdefault("limit", default_limit)
    left = u["limit"] - u.get("used", 0)
    return left, u["limit"]


def _use_quota(data: dict, user_id: int, default_limit: int = DEFAULT_DRAW_LIMIT) -> int:
    """扣除一次用量，返回剩余次数"""
    uid = str(user_id)
    data.setdefault("users", {}).setdefault(uid, {"used": 0, "limit": default_limit})
    data["users"][uid]["used"] = data["users"][uid].get("used", 0) + 1
    return data["users"][uid]["limit"] - data["users"][uid]["used"]


def check_and_use_draw(user_id: int) -> tuple[bool, int, int]:
    """返回 (允许, 剩余, 上限) — 不扣量，只检查"""
    data = _load_quota("draw.json")
    data = _check_daily_reset(data)
    left, limit = _get_user_quota(data, user_id, DEFAULT_DRAW_LIMIT)
    return left > 0, left, limit


def commit_draw(user_id: int) -> int:
    """画图成功后扣量，返回剩余次数"""
    data = _load_quota("draw.json")
    data = _check_daily_reset(data)
    remaining = _use_quota(data, user_id, DEFAULT_DRAW_LIMIT)
    _save_quota("draw.json", data)
    return remaining


def check_and_use_video(user_id: int) -> tuple[bool, int, int]:
    """返回 (允许, 剩余, 上限) — 不扣量，只检查"""
    data = _load_quota("video.json")
    data = _check_daily_reset(data)
    left, limit = _get_user_quota(data, user_id, DEFAULT_VIDEO_LIMIT)
    return left > 0, left, limit


def commit_video(user_id: int) -> int:
    data = _load_quota("video.json")
    data = _check_daily_reset(data)
    remaining = _use_quota(data, user_id, DEFAULT_VIDEO_LIMIT)
    _save_quota("video.json", data)
    return remaining


def owner_quota_get(kind: str, qq: str = "") -> str:
    """.owner draw|video get [QQ]"""
    fn = f"{kind}.json"
    data = _check_daily_reset(_load_quota(fn))
    lim = DEFAULT_DRAW_LIMIT if kind == "draw" else DEFAULT_VIDEO_LIMIT
    if qq:
        left, limit = _get_user_quota(data, int(qq), lim)
        return f"QQ {qq}: 已用 {limit-left}/{limit} (剩余 {left})"
    lines = [f"今日{kind}用量:"]
    for uid, u in data.get("users", {}).items():
        left = u.get("limit", lim) - u.get("used", 0)
        lines.append(f"  {uid}: {left}/{u.get('limit',lim)}")
    return "\n".join(lines) if len(lines) > 1 else f"今日暂无{kind}用量记录"


def owner_quota_set(kind: str, qq: str, value: int):
    """.owner draw|video set <QQ> <值>"""
    fn = f"{kind}.json"
    data = _check_daily_reset(_load_quota(fn))
    data.setdefault("users", {}).setdefault(qq, {"used": 0, "limit": value})
    data["users"][qq]["limit"] = value
    _save_quota(fn, data)


def owner_quota_reset(kind: str = ""):
    """.owner draw|video reset [kind]  无 kind=全部"""
    kinds = [kind] if kind else ["draw", "video"]
    for k in kinds:
        fn = f"{k}.json"
        _save_quota(fn, {"daily_reset": "", "users": {}})


# ── API 调用 ────────────────────────────────────────────

async def _gen_image(prompt: str, size: str = "1024x1024") -> dict | None:
    """文生图 → 返回 {url, local_path}"""
    body = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    try:
        async with httpx.AsyncClient(timeout=120, verify=False) as c:
            r = await c.post(f"{AGNES_BASE}/images/generations", json=body, headers=_headers())
            r.raise_for_status()
            data = r.json()
            img = data.get("data", [{}])[0]
            url = img.get("url", "")
            if not url and img.get("b64_json"):
                raw = base64.b64decode(img["b64_json"])
                path = DATA_DIR / f"draw_{uuid.uuid4().hex[:8]}.png"
                path.write_bytes(raw)
                return {"local_path": str(path)}
            if url:
                path = DATA_DIR / f"draw_{uuid.uuid4().hex[:8]}.png"
                async with httpx.AsyncClient(timeout=30, verify=False) as c2:
                    r2 = await c2.get(url)
                    path.write_bytes(r2.content)
                return {"url": url, "local_path": str(path)}
            return None
    except Exception as e:
        logger.warning("文生图失败: %s", e)
        return None


async def _gen_video(prompt: str, image_url: str = "",
                     width: int = 1152, height: int = 768,
                     num_frames: int = 241, frame_rate: int = 24) -> dict | None:
    """文生视频 / 图生视频 → 返回 {url, local_path}，503 自动重试"""
    body = {
        "model": "agnes-video-v2.0",
        "prompt": prompt or "make this image into a short video",
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    if image_url:
        body["image"] = image_url

    # 创建任务（503 重试 3 次）
    data = None
    for retry in range(3):
        try:
            async with httpx.AsyncClient(timeout=60, verify=False) as c:
                r = await c.post(f"{AGNES_BASE}/videos", json=body, headers=_headers())
                if r.status_code in (503, 502, 500, 429):
                    await asyncio.sleep(5)
                    continue
                r.raise_for_status()
                data = r.json()
                break
        except Exception:
            await asyncio.sleep(5)
    if not data:
        return None

    video_id = data.get("video_id", "") or data.get("id", "")

    # 轮询结果（最多 60 次 × 3s = 3 分钟）
    for attempt in range(60):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as c:
                r2 = await c.get(
                    f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}",
                    headers=_headers(),
                )
                if r2.status_code == 503:
                    continue
                r2.raise_for_status()
                d2 = r2.json()
                status = d2.get("status", "")
                if status == "completed":
                    video_url = d2.get("remixed_from_video_id", "")
                    if video_url:
                        path = DATA_DIR / f"video_{uuid.uuid4().hex[:8]}.mp4"
                        async with httpx.AsyncClient(timeout=120, verify=False) as c3:
                            r3 = await c3.get(video_url)
                            path.write_bytes(r3.content)
                        return {"url": video_url, "local_path": str(path)}
                    return None
                if status == "failed":
                    logger.warning("视频任务失败: %s", video_id)
                    return None
        except Exception:
            pass
    logger.warning("视频任务超时: %s", video_id)
    return None


# ── 发送 ────────────────────────────────────────────────

async def _send_media(local_path: str, group_id: int, is_group: bool, user_id: int, media_type: str):
    """发送图片/视频到 KOOK（视频需先上传为 asset）"""
    from services.sender import send_by_chat_type, send_file
    if media_type == "image":
        normalized = local_path.replace("\\", "/")
        cq_msg = f"[img:file:{normalized}]"
        await send_by_chat_type(cq_msg, group_id if is_group else user_id, is_group, user_id=user_id)
    else:
        # 视频/其他文件 → 上传为 asset
        await send_file(local_path, group_id if is_group else user_id, is_group)


async def _bg_gen_video(user_id, group_id, is_group, prompt, image_url="",
                         width=1152, height=768, num_frames=241, frame_rate=24):
    """后台视频生成——不阻塞聊天"""
    try:
        result = await _gen_video(prompt, image_url=image_url,
                                  width=width, height=height,
                                  num_frames=num_frames, frame_rate=frame_rate)
        if result:
            remaining = commit_video(user_id)
            await _send_media(result["local_path"], group_id, is_group, user_id, "video")
            tip = f"(met){user_id}(met) 视频好了喵~ (今日剩余 {remaining}/{DEFAULT_VIDEO_LIMIT})"
        else:
            tip = f"(met){user_id}(met) 视频生成失败喵~ 换一种描述试试？(不扣次数)"
    except Exception as e:
        logger.warning("后台视频生成异常: %s", e)
        tip = f"(met){user_id}(met) 视频生成失败喵~ 换一种描述试试？(不扣次数)"

    from services.sender import send_by_chat_type
    chat_id = group_id if is_group else user_id
    await send_by_chat_type(tip, chat_id, is_group, user_id)


# ── 命令 ────────────────────────────────────────────────

async def cmd_draw(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    .draw <提示词>  文生图
    .draw 512x512 <提示词>  指定尺寸
    .draw help       查看详细帮助
    """
    from utils.format_lang import format_lang
    if args and args[0].lower() == "help":
        return format_lang("help.detail.draw")
    if not args:
        return "用法: .draw <提示词>  如 .draw 一只坐在樱花树下的猫娘\n.draw help 查看完整参数"

    # 配额检查（不扣量）
    ok, left, limit = check_and_use_draw(user_id)
    if not ok:
        return f"今日画图次数已用完喵~ ({limit}/{limit})，明天再来吧！"

    size = "1024x1024"
    prompt_parts = []
    for a in args:
        if a.replace("x", "").isdigit() and "x" in a:
            size = a
        else:
            prompt_parts.append(a)
    prompt = " ".join(prompt_parts) if prompt_parts else " ".join(args)

    # 发送进度提示
    chat_id = group_id if is_group else user_id
    tip = f"正在画画喵~ 请稍等 (今日剩余 {left}/{limit})..."
    from services.sender import send_by_chat_type
    await send_by_chat_type(tip, chat_id, is_group, user_id)

    result = await _gen_image(prompt, size)
    if not result:
        return f"(met){user_id}(met) 图片生成失败喵~ 换一种描述试试？(不扣次数)"

    # 成功后扣量
    remaining = commit_draw(user_id)
    await _send_media(result["local_path"], group_id, is_group, user_id, "image")
    return f"(met){user_id}(met) 画好了喵~ (今日剩余 {remaining}/{limit})"


async def cmd_video(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    .video <提示词>  文生视频
    .video <提示词> 512x512  指定尺寸
    .video help       查看详细帮助
    """
    from utils.format_lang import format_lang
    if args and args[0].lower() == "help":
        return format_lang("help.detail.video")
    if not args:
        return "用法: .video <提示词>  如 .video 樱花飘落的街道\n.video help 查看完整参数"

    # 配额检查（不扣量）
    ok, left, limit = check_and_use_video(user_id)
    if not ok:
        return f"今日视频次数已用完喵~ ({limit}/{limit})，明天再来吧！"

    chat_id = group_id if is_group else user_id
    tip = f"正在生成视频喵~ 需要几分钟 (今日剩余 {left}/{limit})..."
    from services.sender import send_by_chat_type
    await send_by_chat_type(tip, chat_id, is_group, user_id)

    # 解析参数
    prompt_parts = []
    size_w, size_h = 1152, 768
    num_frames = 241
    frame_rate = 24
    for a in args:
        al = a.lower()
        if "x" in a and al.replace("x", "").isdigit():
            parts = a.split("x")
            size_w, size_h = int(parts[0]), int(parts[1])
        elif al.isdigit():
            val = int(al)
            if val > 50:
                num_frames = val
            else:
                frame_rate = val
        else:
            prompt_parts.append(a)
    prompt = " ".join(prompt_parts) if prompt_parts else " ".join(args)

    # 后台生成，不阻塞聊天
    asyncio.create_task(_bg_gen_video(
        user_id, group_id, is_group, prompt,
        width=size_w, height=size_h,
        num_frames=num_frames, frame_rate=frame_rate,
    ))
    return f"视频生成任务已提交喵~ (今日剩余 {left}/{limit}) 好了会 @你"


async def cmd_img2video(args, user_id, group_id, sender_name, is_group, bot_qq, raw_message="", raw_event=None):
    """
    .img2video <图片URL>  将图片转为视频
    .img2video <图片URL> 512x512  指定尺寸
    .img2video help       查看详细帮助
    KOOK 平台支持：引用/回复一张图片 → 然后直接打 .img2video [尺寸] [提示词]
    """
    from utils.format_lang import format_lang
    if args and args[0].lower() == "help":
        return format_lang("help.detail.img2video")
    if not args:
        return ("用法: .img2video <图片URL> [描述] [尺寸]\n"
                "KOOK: 也可以【引用/回复一张图片】后直接打: .img2video [描述] [尺寸]\n"
                ".img2video help 查看完整参数")

    # 配额检查（不扣量）
    ok, left, limit = check_and_use_video(user_id)
    if not ok:
        return f"今日视频次数已用完喵~ ({limit}/{limit})，明天再来吧！"

    chat_id = group_id if is_group else user_id
    tip = f"正在图生视频喵~ 需要几分钟 (今日剩余 {left}/{limit})..."
    from services.sender import send_by_chat_type
    await send_by_chat_type(tip, chat_id, is_group, user_id)

    prompt_parts = []
    img_url = ""
    size_w, size_h = 1152, 768
    for a in args:
        al = a.lower()
        if a.startswith("http"):
            img_url = a
        elif "x" in a and al.replace("x", "").isdigit():
            parts = a.split("x")
            size_w, size_h = int(parts[0]), int(parts[1])
        else:
            prompt_parts.append(a)
    prompt = " ".join(prompt_parts)

    # ====== KOOK 优先：从 raw_event（khl Message）当前 attachments / 引用 quote.attachments 抽图 ======
    if not img_url and raw_event is not None:
        try:
            # 1) 当前消息 attachments 里的图片
            cur_atts = getattr(raw_event, "attachments", None) or []
            cur_imgs = _agnes_extract_image_urls(cur_atts)
            if cur_imgs:
                img_url = cur_imgs[0]
        except Exception:
            pass
        if not img_url:
            try:
                # 2) 引用消息 quote 的 attachments 里的图片（KOOK引用一条图片消息时图在这里）
                q = getattr(raw_event, "quote", None)
                if q is not None:
                    q_atts = None
                    if isinstance(q, dict):
                        for k in ("attachments", "images", "image_list"):
                            if k in q and q[k]:
                                q_atts = q[k]
                                break
                    else:
                        for k in ("attachments", "images", "image_list"):
                            v = getattr(q, k, None)
                            if v:
                                q_atts = v
                                break
                    if q_atts:
                        q_imgs = _agnes_extract_image_urls(q_atts)
                        if q_imgs:
                            img_url = q_imgs[0]
            except Exception:
                pass

    # ====== fallback：QQ OneBot [img:URL] / [CQ:image] / 裸 URL ======
    if not img_url and raw_message:
        import re as _re
        m = _re.search(r'\[img:(https?://[^\]]+)\]', raw_message)
        if not m:
            m = _re.search(r'\[CQ:image[^\]]*url=([^,\]]+)', raw_message)
        if not m:
            m = _re.search(r'http[^\s,\]]+\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s,\]]*)?', raw_message, _re.I)
        if m:
            img_url = m.group(1) if m.lastindex else m.group(0)

    if not img_url:
        return ("需要提供图片URL喵~\n"
                "用法: .img2video <图片URL> [描述] [尺寸]\n"
                "KOOK: 也可以【引用/回复一张图片】后直接打: .img2video [描述] [尺寸]\n"
                ".img2video help 查看完整参数")

    asyncio.create_task(_bg_gen_video(
        user_id, group_id, is_group, prompt,
        image_url=img_url, width=size_w, height=size_h,
    ))
    return f"图生视频任务已提交喵~ (今日剩余 {left}/{limit}) 好了会 @你"


def _agnes_extract_image_urls(attachments) -> list[str]:
    """与 dispatcher._extract_image_urls 逻辑一致：从khl attachments list抽所有图片URL。"""
    urls: list[str] = []
    if not attachments:
        return urls
    for att in attachments:
        try:
            url = ""
            if isinstance(att, dict):
                t = att.get("type", "")
                if t == "image" or (isinstance(t, str) and "image" in t.lower()):
                    url = att.get("url", "") or att.get("src", "")
            elif hasattr(att, "type"):
                t_str = str(getattr(att, "type", "")).lower()
                if "image" in t_str or t_str == "2":
                    url = (getattr(att, "url", None) or ""
                           or getattr(att, "src", None) or "")
            if not url and hasattr(att, "url"):
                u = getattr(att, "url", "") or ""
                ft = str(getattr(att, "file_type", "")).lower()
                name = str(getattr(att, "name", "")).lower()
                if (ft in ("png", "jpg", "jpeg", "gif", "webp", "bmp")
                        or (name and name.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp", "bmp"))
                        or any(u.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
                        or any(ext in u.lower().split("?", 1)[0] for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))):
                    url = u
            if url and isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
                urls.append(url)
        except Exception:
            continue
    return urls
