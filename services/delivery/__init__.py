"""
Response Delivery 模块（Huanmeng 2.0 Phase 5）

将原 services/sender.py 单一职责拆分为：
  - kook_transport      传输层：Bot 实例 / 频道缓存 / 发送原语 / asset 上传
  - message_formatter   消息格式化与路由（文本 / 卡片 / 图片 / 文件/ CQ 解析）
  - card_formatter      卡片 JSON 校验与修复、倒计时占位符
  - attachment_service  附件上传（本地 / 外部 URL，带超时）
  - message_store       消息落盘（异步队列，不阻塞 KOOK 发送主路径）
  - response_policy     句间延迟策略（max_sentences / max_total_delay / delay_policy）
  - response_delivery   响应投递编排，统一返回 DeliveryResult

默认响应模式保持"LLM 完整生成 → ResponsePolicy → 一次性发送"，
不要求 KOOK Token Streaming。send_sentences 作为可选句子分段模式，
其人为延迟由 ResponsePolicy 管理，普通聊天默认不产生明显延迟。
"""
from services.delivery.kook_transport import (
    init as kook_transport_init,
    cache_channel as kook_transport_cache_channel,
    close as kook_transport_close,
    get_channel as kook_transport_get_channel,
    _bot,
    _channel_cache,
)
from services.delivery.response_delivery import ResponseDelivery, DeliveryResult
from services.delivery.response_policy import ResponsePolicy, ResponsePolicyConfig
from services.delivery.message_store import MessageStore
from services.delivery.attachment_service import AttachmentService

__all__ = [
    "kook_transport_init",
    "kook_transport_cache_channel",
    "kook_transport_close",
    "kook_transport_get_channel",
    "ResponseDelivery",
    "DeliveryResult",
    "ResponsePolicy",
    "ResponsePolicyConfig",
    "MessageStore",
    "AttachmentService",
]