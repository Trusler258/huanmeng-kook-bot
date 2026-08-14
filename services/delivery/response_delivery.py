"""
响应投递编排（Huanmeng 2.0 Phase 5）

职责：
- 编排 格式化(message_formatter) → 策略(response_policy) → 传输(kook_transport)
  → 附件(attachment_service) → 落盘(message_store)。
- 所有发送失败统一返回 DeliveryResult，避免 Card/KMarkdown/Text fallback
  导致重复发送：降级只发生一次，且不重复发送已成功的内容。
- 对外保持与 1.x 兼容的 bool 返回（send_group_msg 等），内部统一 DeliveryResult。
  新代码可通过 deliver() / send_to() 拿到完整 DeliveryResult。

默认响应模式：LLM 完整生成 → ResponsePolicy → 一次性发送（不要求 Token Streaming）。
send_sentences 作为可选句子分段模式，延迟由 ResponsePolicy 管理。
复杂 Agent 可使用 PROGRESS → FINAL（多条 send_to 顺序调用）。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.config import get_config
from core.logger import get_logger
from utils.format_lang import format_lang

from services.delivery import kook_transport as transport
from services.delivery.message_formatter import OutgoingMessage, parse
from services.delivery.card_formatter import replace_countdown, validate_and_repair_card_json
from services.delivery.attachment_service import AttachmentService
from services.delivery.message_store import MessageStore
from services.delivery.response_policy import ResponsePolicy, ResponsePolicyConfig

logger = get_logger("sender.delivery")


@dataclass
class DeliveryResult:
    """一次发送的完整结果。"""
    ok: bool
    method: str = ""        # "kmarkdown" | "card" | "img" | "file" | "channel" | "card_fallback" | ...
    error: str = ""
    attempts: int = 0
    delivered: bool = True  # 是否已向频道投递（False 表示频道不可达等未发送场景）

    def __bool__(self):
        return self.ok


class ResponseDelivery:
    """响应投递编排器。"""

    def __init__(
        self,
        policy: Optional[ResponsePolicy] = None,
        attachments: Optional[AttachmentService] = None,
        store: Optional[MessageStore] = None,
    ):
        self.policy = policy or ResponsePolicy()
        self.store = store or MessageStore()
        self.attachments = attachments or AttachmentService(transport)
        # 注意：worker 不在此处启动（模块导入期可能无 event loop）。
        # 由 sender.init_sender 显式 start()，或首次 log() 时懒启动。

    # ── 内部：单条特殊内容段发送 ─────────────────────────────
    async def _send_segment(self, channel, seg) -> DeliveryResult:
        try:
            if seg.kind == "card":
                card_json = replace_countdown(seg.content)
                ok, card_json, detail = validate_and_repair_card_json(card_json)
                if not ok:
                    logger.error("卡片 JSON 校验失败 (无法修复): %s | 原始: %s", detail, seg.content[:500])
                    await transport.send_kmarkdown(
                        channel,
                        format_lang("bot.card_fallback", bot_name=get_config().bot_name))
                    return DeliveryResult(True, "card_fallback", detail, 1)
                try:
                    await transport.send_card(channel, card_json)
                    return DeliveryResult(True, "card", attempts=1)
                except Exception as ce:
                    logger.error("卡片发送失败 (JSON已校验通过但仍被KOOK拒绝): %s | JSON=%s", ce, card_json[:500])
                    await transport.send_kmarkdown(
                        channel,
                        format_lang("bot.card_fallback", bot_name=get_config().bot_name))
                    return DeliveryResult(False, "card", str(ce), 2)

            elif seg.kind == "img_url":
                url = seg.content
                try:
                    asset = await self.attachments.download_and_upload(url)
                    await transport.send_img(channel, asset)
                    return DeliveryResult(True, "img", attempts=1)
                except Exception as _e:
                    logger.warning("下载外部图片失败 %s: %s，尝试直接发送 URL", url, _e)
                    await transport.send_img(channel, url)
                    return DeliveryResult(True, "img", attempts=1)

            elif seg.kind == "img_file":
                path = seg.content
                if not Path(path).exists():
                    logger.warning("本地图片不存在: %s", path)
                    return DeliveryResult(False, "img_file", "file-not-found", 1)
                asset = await self.attachments.upload(path)
                await transport.send_img(channel, asset)
                return DeliveryResult(True, "img", attempts=1)

            elif seg.kind == "file":
                path = seg.content
                if not Path(path).exists():
                    logger.warning("CQ:file 路径不存在: %s", path)
                    return DeliveryResult(False, "file", "file-not-found", 1)
                asset = await self.attachments.upload(path)
                if self.attachments.is_image(path):
                    await transport.send_img(channel, asset)
                else:
                    await transport.send_file(channel, asset)
                logger.info("CQ:file 已发送: %s", path)
                return DeliveryResult(True, "file", attempts=1)

            else:
                return DeliveryResult(False, seg.kind, f"未知段类型: {seg.kind}", 1)
        except Exception as e:
            return DeliveryResult(False, seg.kind, str(e), 1)

    # ── 内部：发送一条 OutgoingMessage ───────────────────────
    async def _send_outgoing(self, channel, om: OutgoingMessage) -> DeliveryResult:
        attempts = 0
        # 1) 先发文本段（KMD，失败降级一次为 TEXT）
        if om.text_before:
            attempts += 1
            try:
                await transport.send_kmarkdown(channel, om.text_before)
            except Exception as e:
                try:
                    await transport.send_text(channel, om.text_before)
                    attempts += 1
                except Exception as e2:
                    return DeliveryResult(False, "kmarkdown", f"{e} / {e2}", attempts)

        # 2) 发送特殊内容段（仅支持单段，parsing 保证只有一段）
        if om.segments:
            result = await self._send_segment(channel, om.segments[0])
            result.attempts += attempts
            return result

        return DeliveryResult(True, "kmarkdown", attempts=attempts)

    # ── 核心：格式化 + 投递 ──────────────────────────────────
    async def send_to(self, message: str, chat_id, is_group: bool, user_id=None) -> DeliveryResult:
        """把一条字符串投递到指定频道/私聊，返回 DeliveryResult。"""
        channel = await transport.get_channel(chat_id, is_group)
        if channel is None:
            return DeliveryResult(False, "channel", "channel-not-found", 0, delivered=False)
        om = parse(message)
        return await self._send_outgoing(channel, om)

    async def deliver(self, message: str, chat_id, is_group: bool, user_id=None) -> DeliveryResult:
        """send_to 的别名（语义更贴合"投递"）。"""
        return await self.send_to(message, chat_id, is_group, user_id)

    # ── 兼容公开接口（保持 bool 返回）────────────────────────
    async def send_group_msg(self, message: str, group_id) -> bool:
        result = await self.send_to(message, group_id, is_group=True)
        if not result.delivered:
            return False
        if not result.ok:
            cfg = get_config()
            fallback = format_lang("bot.fallback_reply", name=cfg.bot_name)
            await self.send_to(fallback, group_id, is_group=True)
        if result.ok:
            self._log_bot_sent(group_id, message)
        return result.ok

    async def send_private_msg(self, message: str, user_id) -> bool:
        result = await self.send_to(message, user_id, is_group=False)
        if not result.delivered:
            return False
        if not result.ok:
            cfg = get_config()
            fallback = format_lang("bot.fallback_reply", name=cfg.bot_name)
            await self.send_to(fallback, user_id, is_group=False)
        if result.ok:
            self._log_bot_sent(user_id, message)
        return result.ok

    async def send_by_chat_type(self, message: str, chat_id, is_group: bool, user_id=None) -> bool:
        if is_group:
            return await self.send_group_msg(message, chat_id)
        assert user_id is not None, "私聊发送必须提供 user_id"
        return await self.send_private_msg(message, user_id)

    async def send_sentences(
        self,
        sentences: list[str],
        chat_id,
        is_group: bool,
        user_id=None,
        min_interval: float = 0.5,
        max_interval: float = 1.5,
    ):
        """逐条发送句子列表，句间延迟由 ResponsePolicy 管理（不隐藏在内部）。"""
        logger.info("开始分批发送 %d 条句子 → chat=%s is_group=%s",
                    len(sentences), chat_id, is_group)

        from core.trace import span
        policy = ResponsePolicy(ResponsePolicyConfig(
            min_interval=min_interval, max_interval=max_interval))
        sentences = policy.cap_sentences(sentences)
        delays = policy.compute_delays(len(sentences))

        with span("kook_send"):
            for i, sentence in enumerate(sentences):
                if i > 0 and delays[i] > 0:
                    logger.debug("句间等待 %.2fs (#%d/%d)", delays[i], i + 1, len(sentences))
                    await asyncio.sleep(delays[i])
                await self.send_by_chat_type(sentence, chat_id, is_group, user_id)
                logger.debug("已发送第 %d/%d 条: %s...", i + 1, len(sentences), sentence[:30])

        logger.info("分批发送完成: 共 %d 条 → chat=%s", len(sentences), chat_id)

    async def send_file(self, file_path: str, chat_id, is_group: bool) -> bool:
        path = Path(file_path)
        if not path.exists():
            logger.warning("文件不存在: %s", file_path)
            return False
        try:
            channel = await transport.get_channel(chat_id, is_group)
            if channel is None:
                return False
            asset_url = await self.attachments.upload(str(path))
            if self.attachments.is_image(path):
                await transport.send_img(channel, asset_url)
            else:
                await transport.send_file(channel, asset_url)
            logger.info("文件已发送: %s", path.name)
            return True
        except Exception as e:
            logger.error("文件发送失败: %s", e)
            return False

    async def send_raw_group(self, raw_obj, group_id) -> bool:
        return await self._send_raw(raw_obj, group_id, is_group=True)

    async def send_raw_user(self, raw_obj, user_id) -> bool:
        return await self._send_raw(raw_obj, user_id, is_group=False)

    async def _send_raw(self, raw_obj, target, is_group: bool) -> bool:
        channel = await transport.get_channel(target, is_group)
        if channel is None:
            return False
        try:
            content = json.dumps(raw_obj, ensure_ascii=False) if isinstance(raw_obj, (dict, list)) else str(raw_obj)
            await transport.send_card(channel, content)
            return True
        except Exception as e:
            logger.error("send_raw 失败: %s", e)
            return False

    # ── 消息落盘 ────────────────────────────────────────────
    def _log_bot_sent(self, chat_id, content: str):
        try:
            self.store.log_bot(chat_id, get_config().bot_qq, content)
        except Exception as e:
            logger.warning("_log_bot_sent 失败: %s", e)

    def log_user_message(self, chat_id, user_id, content: str):
        self.store.log_user(chat_id, user_id, content)