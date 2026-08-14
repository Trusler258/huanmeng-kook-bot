"""
KOOK 消息发送服务
- 使用 khl.py Bot 实例发送消息
- 支持频道(group)/私聊(person)/字频道(channel)
- 图片用 asset/URL 发送，文本用 KMarkdown 类型发送
- 频道对象缓存（避免每次发送都 fetch）
"""

from __future__ import annotations

import asyncio
import json
import re
import random
from pathlib import Path
from typing import Optional

from khl import MessageTypes

from core.logger import get_logger
from core.config import get_config
from utils.format_lang import format_lang

logger = get_logger("sender")

# ── 全局状态 ────────────────────────────────────────────────
_bot = None                        # khl.py Bot 实例
_channel_cache: dict[str, object] = {}  # chat_id(str) → Channel 对象


def init_sender(bot):
    """初始化发送器（传入 khl.py Bot 实例）"""
    global _bot
    _bot = bot


def cache_channel(chat_id, channel):
    """缓存频道对象（dispatcher 收到消息时调用）"""
    _channel_cache[str(chat_id)] = channel


async def close_sender():
    """关闭发送器"""
    _channel_cache.clear()


# ── 卡片 JSON 预校验与修复 ─────────────────────────────────

def _validate_and_repair_card_json(raw: str):
    """验证并修复卡片 JSON。返回 (ok: bool, fixed_json: str, error_detail: str)"""
    raw = raw.strip()
    detail = ""

    # ---- 解析 ----
    try:
        cards = json.loads(raw)
    except json.JSONDecodeError as e:
        detail = f"JSON解析失败: {e}"
        # 尝试修复：去掉末尾多余的 ]
        fixed = raw
        for _ in range(5):
            if fixed.rstrip().endswith("]]"):
                fixed = fixed.rstrip()[:-1]
            else:
                break
        # 补全缺失的括号
        open_brace = fixed.count("{") - fixed.count("}")
        open_bracket = fixed.count("[") - fixed.count("]")
        fixed += "}" * max(0, open_brace) + "]" * max(0, open_bracket)
        try:
            cards = json.loads(fixed)
            logger.info("卡片 JSON 括号修复成功 (补了%d个} %d个])", max(0, open_brace), max(0, open_bracket))
        except json.JSONDecodeError:
            return False, raw, detail

    # ---- 类型检查 ----
    if not isinstance(cards, list):
        cards = [cards]
    if not cards:
        return False, raw, "卡片数组为空"

    # ---- 结构验证与修复 ----
    fixed_count = 0
    for i, card in enumerate(cards):
        if not isinstance(card, dict):
            return False, raw, f"card[{i}] 不是 JSON 对象"
        if card.get("type") != "card":
            return False, raw, f"card[{i}].type 不是 'card'"
        if "modules" not in card:
            return False, raw, f"card[{i}] 缺少 modules 字段"
        if not isinstance(card["modules"], list):
            return False, raw, f"card[{i}].modules 不是数组"

        for j, mod in enumerate(card["modules"]):
            if not isinstance(mod, dict):
                return False, raw, f"card[{i}].modules[{j}] 不是对象"

            mtype = mod.get("type", "")
            # section 有 accessory 但缺 mode → 自动补 right
            if mtype == "section" and isinstance(mod.get("accessory"), dict) and "mode" not in mod:
                mod["mode"] = "right"
                fixed_count += 1
                logger.info("卡片修复: section[%d][%d] 自动补 mode=right", i, j)

            # accessory 内容校验：button 必须有 type/text/value
            acc = mod.get("accessory")
            if isinstance(acc, dict):
                atype = acc.get("type", "")
                if atype == "button":
                    # 自动修复：button.text 是字符串 → 转为 plain-text 对象
                    btn_text = acc.get("text")
                    if isinstance(btn_text, str):
                        acc["text"] = {"type": "plain-text", "content": btn_text}
                        fixed_count += 1
                        logger.info("卡片修复: button.text 字符串自动转对象: '%s'", btn_text[:50])
                    if "text" not in acc:
                        return False, raw, f"card[{i}].modules[{j}].accessory.button 缺少 text"
                    if "theme" not in acc:
                        acc["theme"] = "primary"
                        fixed_count += 1
                    if "value" not in acc:
                        acc["value"] = "click"
                        fixed_count += 1
                elif atype == "image":
                    if "src" not in acc:
                        return False, raw, f"card[{i}].modules[{j}].accessory.image 缺少 src"
                elif atype:
                    return False, raw, f"card[{i}].modules[{j}].accessory 未知类型: {atype}"

    if fixed_count:
        logger.info("卡片 JSON 修复了 %d 处，重新序列化", fixed_count)

    try:
        fixed_json = json.dumps(cards, ensure_ascii=False)
        return True, fixed_json, detail
    except Exception as e:
        return False, raw, f"修复后序列化失败: {e}"


# ── 内部工具 ────────────────────────────────────────────────

async def _get_channel(chat_id, is_group: bool):
    """获取频道对象（优先缓存，否则 fetch）"""
    key = str(chat_id)
    if key in _channel_cache:
        return _channel_cache[key]
    if is_group:
        try:
            ch = await _bot.client.fetch_public_channel(key)
            _channel_cache[key] = ch
            return ch
        except Exception as e:
            logger.error("fetch_public_channel 失败 %s: %s", key, e)
            return None
    else:
        # 私聊：需要用户先发起过对话（缓存在 dispatcher 里）
        logger.warning("私聊频道未缓存，无法主动发送: %s", chat_id)
        return None


# 图片 URL 标记格式: [img:url]  本地路径标记: [img:file:path]
_IMG_URL_RE = re.compile(r'\[img:(https?://[^\]]+)\]')
_IMG_FILE_RE = re.compile(r'\[img:file:([^\]]+)\]')
# 卡片标记: [CARD]KOOK Card JSON[/CARD]
_CARD_RE = re.compile(r'\[CARD\](.*?)\[/CARD\]', re.DOTALL)
# CQ 文件标记: [CQ:file,file=file:///本地路径,name=文件名]（对齐 QQ onebot 发文件）
_CQ_FILE_RE = re.compile(r'\[CQ:file,file=file:///([^\],>]+),name=([^\]]+)\]')
# 倒计时占位符: __COUNTDOWN__:秒数
_COUNTDOWN_RE = re.compile(r'"__COUNTDOWN__:(\d+)"')


async def _send_to_channel(channel, message: str) -> bool:
    """发送消息到频道。
    文本以 KMarkdown 类型发送。
    图片标记 [img:URL] / [img:file:本地路径] 会先发文字再发图片。
    """
    if channel is None:
        return False
    try:
        # 兜底：检测裸 JSON 泄漏（LLM 格式错误时 _parse_reply 回退会把原始 JSON 当文本发）
        # 如果消息以 { 开头且含 "replies"/"fav"/"calls" 等 schema 字段，说明是未解析的 LLM 输出
        stripped = message.strip()
        if stripped.startswith('{') and '"replies"' in stripped and ('"fav"' in stripped or '"calls"' in stripped):
            logger.warning("检测到裸 JSON 泄漏，替换为兜底消息: %s", stripped[:80])
            message = "呜…刚才脑子乱了一下喵，能再说一遍吗？(＞﹏＜)"

        # 兜底：裸卡片 JSON（含 "type":"card" 且以 [{ 开头）自动补标记
        stripped = message.strip()
        if stripped.startswith('[{') and '"type":"card"' in stripped and '[CARD]' not in stripped:
            # 尝试解析，修复末尾多余的 ]
            _frag = stripped
            for _ in range(3):
                try:
                    json.loads(_frag)
                    message = f'[CARD]{_frag}[/CARD]'
                    break
                except json.JSONDecodeError:
                    _frag = _frag.rstrip().rstrip(']').rstrip()
            else:
                logger.warning("裸卡片 JSON 解析失败: %s", stripped[:100])

        # 检测卡片消息 [CARD]json[/CARD]
        card_match = _CARD_RE.search(message)
        if card_match:
            card_json = card_match.group(1).strip()
            # 替换倒计时占位符 __COUNTDOWN__:秒数 → 毫秒时间戳
            def _replace_countdown(m):
                import time as _time
                seconds = int(m.group(1))
                return str(int(_time.time() * 1000) + seconds * 1000)
            card_json = _COUNTDOWN_RE.sub(_replace_countdown, card_json)
            text = message[:card_match.start()].strip()
            if text:
                await _bot.client.send(channel, text, type=MessageTypes.KMD)

            # 预校验 + 修复卡片 JSON
            ok, card_json, detail = _validate_and_repair_card_json(card_json)
            if not ok:
                logger.error("卡片 JSON 校验失败 (无法修复): %s | 原始: %s", detail, card_json[:500])
                # 回退：发一个友好的纯文本
                await _bot.client.send(
                    channel,
                    format_lang("bot.card_fallback", bot_name=get_config().bot_name),
                    type=MessageTypes.KMD
                )
                return True

            try:
                await _bot.client.send(channel, card_json, type=MessageTypes.CARD)
            except Exception as ce:
                logger.error("卡片发送失败 (JSON已校验通过但仍被KOOK拒绝): %s | JSON=%s", ce, card_json[:500])
                await _bot.client.send(
                    channel,
                    format_lang("bot.card_fallback", bot_name=get_config().bot_name),
                    type=MessageTypes.KMD
                )
            return True

        # 检测 CQ 文件标记 [CQ:file,file=file:///path,name=xxx]（对齐 QQ onebot，KOOK 走 asset 上传 + FILE 类型）
        cq_file_match = _CQ_FILE_RE.search(message)
        if cq_file_match:
            file_path = cq_file_match.group(1).strip()
            text = message[:cq_file_match.start()].strip()
            if text:
                await _bot.client.send(channel, text, type=MessageTypes.KMD)
            if file_path and Path(file_path).exists():
                asset_url = await _bot.client.create_asset(file_path)
                _cq_ext = Path(file_path).suffix.lower()
                _cq_img = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".jfif"}
                if _cq_ext in _cq_img:
                    await _bot.client.send(channel, asset_url, type=MessageTypes.IMG)
                else:
                    await _bot.client.send(channel, asset_url, type=MessageTypes.FILE)
                logger.info("CQ:file 已发送: %s", file_path)
            else:
                logger.warning("CQ:file 路径不存在: %s", file_path)
            return True

        # 检测本地文件图片
        file_match = _IMG_FILE_RE.search(message)
        if file_match:
            file_path = file_match.group(1)
            text = message[:file_match.start()].strip()
            if text:
                await _bot.client.send(channel, text, type=MessageTypes.KMD)
            asset_url = await _bot.client.create_asset(file_path)
            await _bot.client.send(channel, asset_url, type=MessageTypes.IMG)
            return True

        # 检测 HTTP 图片 URL
        url_match = _IMG_URL_RE.search(message)
        if url_match:
            img_url = url_match.group(1)
            text = message[:url_match.start()].strip()
            if text:
                await _bot.client.send(channel, text, type=MessageTypes.KMD)
            # KOOK 外部 URL 需先下载再 create_asset 上传，否则 IMG 类型不认
            import tempfile, os
            import aiohttp
            try:
                async with aiohttp.ClientSession() as _sess:
                    async with _sess.get(img_url) as _resp:
                        if _resp.status == 200:
                            _data = await _resp.read()
                            _suffix = os.path.splitext(img_url.split('?')[0])[-1] or '.png'
                            _tmp = tempfile.NamedTemporaryFile(suffix=_suffix, delete=False, dir=str(Path(__file__).resolve().parent.parent / "data" / "img_temp"))
                            _tmp.write(_data)
                            _tmp.close()
                            asset_url = await _bot.client.create_asset(_tmp.name)
                            await _bot.client.send(channel, asset_url, type=MessageTypes.IMG)
                            os.unlink(_tmp.name)
                            return True
            except Exception as _e:
                logger.warning("下载外部图片失败 %s: %s，尝试直接发送 URL", img_url, _e)
            # 回退：直接发 URL（KOOK 可能只认自家 asset URL，外部 URL 可能仍为文本）
            await _bot.client.send(channel, img_url, type=MessageTypes.IMG)
            return True

        # 纯文本 → 以 KMarkdown 类型发送（支持 (met)xxx(met) @提及、**粗体**、[链接](url) 等）
        await _bot.client.send(channel, message, type=MessageTypes.KMD)
        return True
    except Exception as e:
        logger.error("发送到频道失败: %s", e)
        # KMarkdown 解析失败时回退为纯文本
        try:
            await _bot.client.send(channel, message, type=MessageTypes.TEXT)
            return True
        except Exception:
            return False


# ── 公开接口 ────────────────────────────────────────────────

async def send_group_msg(message: str, group_id) -> bool:
    """发送频道文本消息（group_id = KOOK channel_id）"""
    if _bot is None:
        logger.error("发送器未初始化")
        return False
    channel = await _get_channel(group_id, is_group=True)
    success = await _send_to_channel(channel, message)
    if not success:
        cfg = get_config()
        fallback = format_lang("bot.fallback_reply", name=cfg.bot_name)
        await _send_to_channel(channel, fallback)
    _log_bot_sent(group_id, message if success else "")
    return success


async def send_private_msg(message: str, user_id) -> bool:
    """发送私聊文本消息（user_id = KOOK user_id）"""
    if _bot is None:
        logger.error("发送器未初始化")
        return False
    channel = await _get_channel(user_id, is_group=False)
    success = await _send_to_channel(channel, message)
    if not success:
        cfg = get_config()
        fallback = format_lang("bot.fallback_reply", name=cfg.bot_name)
        await _send_to_channel(channel, fallback)
    if success:
        _log_bot_sent(user_id, message)
    return success


async def send_by_chat_type(
    message: str,
    chat_id,
    is_group: bool,
    user_id=None,
) -> bool:
    """根据聊天类型选择频道/私聊发送"""
    if is_group:
        return await send_group_msg(message, chat_id)
    else:
        assert user_id is not None, "私聊发送必须提供 user_id"
        return await send_private_msg(message, user_id)


async def send_sentences(
    sentences: list[str],
    chat_id,
    is_group: bool,
    user_id=None,
    min_interval: float = 0.5,
    max_interval: float = 1.5,
):
    """逐条发送句子列表，每条之间随机间隔"""
    logger.info("开始分批发送 %d 条句子 → chat=%s is_group=%s",
               len(sentences), chat_id, is_group)

    from core.trace import span
    with span("kook_send"):
        for i, sentence in enumerate(sentences):
            if i > 0:
                delay = random.uniform(min_interval, max_interval)
                logger.debug("句间等待 %.2fs (#%d/%d)", delay, i + 1, len(sentences))
                await asyncio.sleep(delay)
            await send_by_chat_type(sentence, chat_id, is_group, user_id)
            logger.debug("已发送第 %d/%d 条: %s...", i + 1, len(sentences), sentence[:30])

    logger.info("分批发送完成: 共 %d 条 → chat=%s", len(sentences), chat_id)


async def send_file(file_path: str, chat_id, is_group: bool) -> bool:
    """发送文件（先上传为 asset）"""
    path = Path(file_path)
    if not path.exists():
        logger.warning("文件不存在: %s", file_path)
        return False
    try:
        channel = await _get_channel(chat_id, is_group)
        if channel is None:
            return False
        asset_url = await _bot.client.create_asset(str(path))
        # 图片后缀用 IMG(type=2)，其余文件用 FILE(type=4) —— 之前一律 IMG 导致非图片文件发不出来
        _img_ext = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".jfif"}
        if path.suffix.lower() in _img_ext:
            await _bot.client.send(channel, asset_url, type=MessageTypes.IMG)
        else:
            await _bot.client.send(channel, asset_url, type=MessageTypes.FILE)
        logger.info("文件已发送: %s", path.name)
        return True
    except Exception as e:
        logger.error("文件发送失败: %s", e)
        return False


def _log_msglog(chat_id, user_id, msg_type: str, content: str):
    """通用：写一条消息到 msglog（bot 与用户消息共用）"""
    try:
        from time import time as _time
        entry = {
            "msg_id": 0,
            "time": int(_time()),
            "user_id": user_id,
            "type": msg_type,
            "content": content,
            "recalled": False,
        }
        log_dir = Path(__file__).resolve().parent.parent / "data" / "msglog"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"msglog_{chat_id}.jsonl"
        import json as _json
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        # Phase1 Trace：记录消息落盘耗时
        try:
            from core.trace import record
            record("message_store", 1.0)
        except Exception:
            pass
    except Exception as e:
        logger.warning("_log_msglog 失败: %s", e)


def log_user_message(chat_id, user_id, content: str):
    """记录用户消息到 msglog，供长时记忆回溯用户历史对话"""
    _log_msglog(chat_id, user_id, "group", content)


def _log_bot_sent(chat_id, content: str):
    """记录 bot 发送的消息到 msglog"""
    try:
        cfg = get_config()
        _log_msglog(chat_id, cfg.bot_qq, "bot", content)
    except Exception as e:
        logger.warning("_log_bot_sent 失败: %s", e)


# ── 兼容旧接口 ──────────────────────────────────────────────

def get_ws_manager():
    """兼容旧接口，KOOK 不使用 WS 管理器"""
    return None


# 消息发送便捷函数（供 commands.py 等模块调用）
async def send_raw_group(raw_obj: dict, group_id) -> bool:
    """发送自定义消息（如卡片消息）到频道"""
    if _bot is None:
        return False
    try:
        channel = await _get_channel(group_id, is_group=True)
        if channel is None:
            return False
        # raw_obj 可能是卡片消息（card 数组 / 单 dict），统一序列化为 JSON 字符串
        import json as _json
        content = _json.dumps(raw_obj, ensure_ascii=False) if isinstance(raw_obj, (dict, list)) else str(raw_obj)
        from khl import MessageTypes
        await _bot.client.send(channel, content, type=MessageTypes.CARD)
        return True
    except Exception as e:
        logger.error("send_raw_group 失败: %s", e)
        return False


async def send_raw_user(raw_obj: dict, user_id) -> bool:
    """发送自定义消息到私聊"""
    if _bot is None:
        return False
    try:
        channel = await _get_channel(user_id, is_group=False)
        if channel is None:
            return False
        import json as _json
        content = _json.dumps(raw_obj, ensure_ascii=False) if isinstance(raw_obj, (dict, list)) else str(raw_obj)
        from khl import MessageTypes
        await _bot.client.send(channel, content, type=MessageTypes.CARD)
        return True
    except Exception as e:
        logger.error("send_raw_user 失败: %s", e)
        return False
