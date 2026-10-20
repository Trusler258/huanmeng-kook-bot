"""
KOOK 消息发送服务（Huanmeng 2.0 Phase 5 兼容门面）

Huanmeng 2.0 已将发送逻辑拆分为 services/delivery 下的独立模块：
  - kook_transport      传输层（Bot 实例 / 频道缓存 / 发送原语 / asset 上传）
  - message_formatter   消息格式化与路由
  - card_formatter      卡片校验与修复
  - attachment_service  附件上传
  - message_store       消息落盘（异步）
  - response_policy     句间延迟策略
  - response_delivery   响应投递编排（统一返回 DeliveryResult）

本文件作为兼容门面，保留 1.x 的全部公开接口与内部状态
（_bot / _channel_cache / _get_channel），
确保 commands / pipeline / pc_status / spam_guard 等旧调用方无需改动即可继续工作。

新代码应直接使用 services.delivery 下的模块。
"""
from __future__ import annotations

import re
from pathlib import Path

from core.logger import get_logger
from services.delivery import kook_transport
from services.delivery.response_delivery import ResponseDelivery

logger = get_logger("sender")

# ── 兼容门面条目 ────────────────────────────────────────────
_delivery = ResponseDelivery()

# ── 文本内联本地图片（兼容 QQ 移植插件）────────────────────────
# QQ 侧插件返回 [CQ:image,file=file:///...]，由 NapCat 平台解析；
# KOOK 没有文本内联图片，故统一约定 [图片:<路径>] 标记，在这里拦截：
# 有标记就先发文件，再把纯文本发出去。无标记时零开销（直接返回原文）。
_IMG_TAG = re.compile(r"\[图片:([^\]]+)\]")


def build_local_image_cq(path: str) -> str:
    """生成 KOOK 的内联图片标记（QQ 侧同名函数返回 CQ 码，此处返回 [图片:路径]）。

    从 QQ 移植的模块（如 modules/reward.py）直接调它即可，无需改代码。
    """
    try:
        return f"[图片:{Path(str(path)).resolve()}]"
    except Exception:
        return f"[图片:{path}]"


async def _split_inline_images(message: str, chat_id, is_group: bool) -> str:
    """把文本里的 [图片:路径] 拆出来：先发图，返回剩余纯文本。

    剩余为空（整条只有图）时返回空串——调用方据此跳过文本发送。
    """
    if not message or "[图片:" not in message:
        return message
    paths = [x.strip() for x in _IMG_TAG.findall(message) if x.strip()]
    rest = _IMG_TAG.sub("", message).strip()
    for pth in paths:
        try:
            await _delivery.send_file(pth, chat_id, is_group)
        except Exception as e:
            logger.warning("内联图片发送失败 %s: %s", pth, e)
    return rest


# 兼容内部状态：动态转发到传输层（保持旧调用方 from services.sender import _bot 可用）
def __getattr__(name):
    if name == "_bot":
        return kook_transport._bot
    if name == "_channel_cache":
        return kook_transport._channel_cache
    if name == "_get_channel":
        return kook_transport.get_channel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def init_sender(bot):
    """初始化发送器（传入 khl.py Bot 实例）"""
    kook_transport.init(bot)
    _delivery.store.start()
    logger.info("发送器已初始化 (Phase5 兼容层)")


def cache_channel(chat_id, channel):
    """缓存频道对象（dispatcher 收到消息时调用）"""
    kook_transport.cache_channel(chat_id, channel)


async def close_sender():
    """关闭发送器"""
    kook_transport.close()
    await _delivery.store.close()


# ── 公开发送接口（保持 bool 返回）───────────────────────────

async def send_group_msg(message: str, group_id) -> bool:
    """发送频道文本消息（group_id = KOOK channel_id）"""
    message = await _split_inline_images(message, group_id, True)
    if not message:
        return True
    return await _delivery.send_group_msg(message, group_id)


async def send_private_msg(message: str, user_id) -> bool:
    """发送私聊文本消息（user_id = KOOK user_id）"""
    message = await _split_inline_images(message, user_id, False)
    if not message:
        return True
    return await _delivery.send_private_msg(message, user_id)


async def send_by_chat_type(
    message: str,
    chat_id,
    is_group: bool,
    user_id=None,
) -> bool:
    """根据聊天类型选择频道/私聊发送"""
    message = await _split_inline_images(message, chat_id, is_group)
    if not message:
        return True
    ok = await _delivery.send_by_chat_type(message, chat_id, is_group, user_id)
    # ★ v2.3.102：只有发送层明确返回成功才发 sent；失败发 failed（不伪造成功）
    try:
        from core.eventbus import emit_event
        emit_event("message.sent" if ok else "message.failed", {
            "chat_id": chat_id, "is_group": bool(is_group),
            "chars": len(message or ""), "ok": bool(ok),
        }, source="core.sender")
    except Exception:
        pass
    return ok


async def send_sentences(
    sentences: list[str],
    chat_id,
    is_group: bool,
    user_id=None,
    min_interval: float = 0.5,
    max_interval: float = 1.5,
):
    """逐条发送句子列表，每条之间随机间隔（延迟由 ResponsePolicy 管理）"""
    # ★ v2.3.102：replied = 已交给发送层（不宣称平台确认成功）
    try:
        from core.eventbus import emit_event
        emit_event("message.replied", {
            "chat_id": chat_id, "is_group": bool(is_group),
            "count": len(sentences or []),
            "chars": sum(len(s or "") for s in (sentences or [])),
        }, source="core.sender")
    except Exception:
        pass
    await _delivery.send_sentences(
        sentences, chat_id, is_group, user_id, min_interval, max_interval)


async def send_file(file_path: str, chat_id, is_group: bool) -> bool:
    """发送文件（先上传为 asset）"""
    return await _delivery.send_file(file_path, chat_id, is_group)


async def send_raw_group(raw_obj: dict, group_id) -> bool:
    """发送自定义消息（如卡片消息）到频道"""
    return await _delivery.send_raw_group(raw_obj, group_id)


async def send_raw_user(raw_obj: dict, user_id) -> bool:
    """发送自定义消息到私聊"""
    return await _delivery.send_raw_user(raw_obj, user_id)


# ── 消息落盘（转交 MessageStore）────────────────────────────

def log_user_message(chat_id, user_id, content: str):
    """记录用户消息到 msglog，供长时记忆回溯用户历史对话"""
    _delivery.log_user_message(chat_id, user_id, content)


def _log_bot_sent(chat_id, content: str):
    """记录 bot 发送的消息到 msglog"""
    _delivery._log_bot_sent(chat_id, content)


# ── 兼容旧接口 ──────────────────────────────────────────────

def get_ws_manager():
    """兼容旧接口，KOOK 不使用 WS 管理器"""
    return None