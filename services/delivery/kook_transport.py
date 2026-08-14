"""
KOOK 传输层（Huanmeng 2.0 Phase 5）

职责：
- 持有 khl.py Bot 实例与频道对象缓存。
- 提供最底层的 KOOK 发送原语（KMD / CARD / IMG / FILE / TEXT）与 asset 上传。
- 不包含任何格式化 / 策略 / fallback 逻辑，只负责"把一种消息类型发到一个频道"。

注意：所有对外发送原语在 khl 库默认超时基础上不再额外重试，
重试 / 降级统一由 response_delivery 编排。
"""
from __future__ import annotations

from khl import MessageTypes

from core.logger import get_logger

logger = get_logger("sender.transport")

# ── 全局状态（单一数据源，sender.py 兼容门面动态转发）──
_bot = None                                   # khl.py Bot 实例
_channel_cache: dict[str, object] = {}        # chat_id(str) → Channel 对象


def init(bot):
    """初始化传输层（注入 khl.py Bot 实例）"""
    global _bot
    _bot = bot


def cache_channel(chat_id, channel):
    """缓存频道对象（dispatcher 收到消息时调用）"""
    _channel_cache[str(chat_id)] = channel


def close():
    """关闭传输层，清空频道缓存"""
    _channel_cache.clear()


async def get_channel(chat_id, is_group: bool):
    """获取频道对象（优先缓存，否则 fetch）。
    私聊：只能依赖 dispatcher 已缓存的频道（用户需先发起对话）。
    """
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
        logger.warning("私聊频道未缓存，无法主动发送: %s", chat_id)
        return None


# ── 发送原语 ────────────────────────────────────────────────

async def send_kmarkdown(channel, text: str):
    """KMarkdown 文本（支持 **加粗**、(met)@(met)、[链接](url) 等）"""
    await _bot.client.send(channel, text, type=MessageTypes.KMD)


async def send_text(channel, text: str):
    """纯文本（KMarkdown 解析失败时的降级目标）"""
    await _bot.client.send(channel, text, type=MessageTypes.TEXT)


async def send_card(channel, card_json: str):
    """卡片消息"""
    await _bot.client.send(channel, card_json, type=MessageTypes.CARD)


async def send_img(channel, url: str):
    """图片消息"""
    await _bot.client.send(channel, url, type=MessageTypes.IMG)


async def send_file(channel, url: str):
    """文件消息"""
    await _bot.client.send(channel, url, type=MessageTypes.FILE)


async def create_asset(path: str) -> str:
    """将本地文件上传为 KOOK asset，返回可发送的 URL"""
    return await _bot.client.create_asset(path)