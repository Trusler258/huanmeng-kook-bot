"""
地震信息插件（一个命令一个插件）

迁移自 Core 静态命令 .eq/.地震，注册为插件命令。
复用 modules.earthquake.cmd_eq（独立模块，功能一致）。
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger("plugin.eq")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("eq", "地震"):
            self.ctx.capability.register_command(
                name=name, description="最新地震信息：.eq",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        from modules.earthquake import cmd_eq as _eq

        args = msg.get("args") or []
        return await _eq(args, msg.get("author"), msg.get("chat_id"),
                         msg.get("sender"), msg.get("is_group"),
                         msg.get("bot_qq") or 0)
