"""
天气查询插件（一个命令一个插件）

迁移自 Core 静态命令 .天气/.weather，注册为插件命令。
优先输出精美卡片图片，失败回退纯文本。功能与原命令一致。
"""
from __future__ import annotations

import asyncio

from core.logger import get_logger

logger = get_logger("plugin.weather")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("天气", "weather"):
            self.ctx.capability.register_command(
                name=name, description="天气查询：.天气 <城市>",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        from modules.weather import query_weather, build_weather_report
        from modules.changelog import send_weather_card
        from services.sender import send_group_msg, send_private_msg
        from utils.format_lang import format_lang

        args = msg.get("args") or []
        user_id = msg.get("author")
        group_id = msg.get("chat_id") if msg.get("is_group") else None
        is_group = bool(msg.get("is_group"))

        if not args:
            return format_lang("weather.prompt_input")
        city = " ".join(args)

        tip = format_lang("weather.searching")
        await (send_group_msg(tip, group_id) if is_group
               else send_private_msg(tip, user_id))

        data = await query_weather(city)
        if data is None:
            return format_lang("weather.fallback_error")

        # 异步卡片生成（不阻塞）
        async def _bg_send_weather():
            try:
                card_result = await send_weather_card(
                    data=data,
                    group_id=group_id if is_group else None,
                    user_id=user_id if not is_group else None,
                    is_group=is_group,
                )
                if card_result is not None:
                    fallback = build_weather_report(data, user_id)
                    if is_group:
                        await send_group_msg(fallback, group_id)
                    else:
                        await send_private_msg(fallback, user_id)
            except Exception as e:
                logger.error("[BG] 天气卡片后台发送失败: %s", e, exc_info=True)

        asyncio.create_task(_bg_send_weather())
        return None  # 后台异步处理
