"""
NASA 每日一图插件（一个命令一个插件）

迁移自 Core 静态命令 .nasa，注册为插件命令。
复用 modules.nasa.cmd_nasa（独立模块，功能一致）。
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger("plugin.nasa")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        self.ctx.capability.register_command(
            name="nasa", description="NASA 每日一图：.nasa",
            handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        from modules.nasa import cmd_nasa as _nasa

        args = msg.get("args") or []
        return await _nasa(args, msg.get("author"), msg.get("chat_id"),
                           msg.get("sender"), msg.get("is_group"),
                           msg.get("bot_qq") or 0)
