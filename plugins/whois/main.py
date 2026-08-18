"""
域名 WHOIS 查询插件（一个命令一个插件）

迁移自 Core 静态命令 .whois，注册为插件命令。
功能与原命令一致：.whois <域名> → 注册商/注册时间/到期时间/NS/状态
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger("plugin.whois")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("whois", "域名"):
            self.ctx.capability.register_command(
                name=name, description="域名 WHOIS 查询：.whois <域名>",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        import asyncio

        from modules.whois_lookup import lookup_domain
        args = msg.get("args") or []
        if not args:
            return "用法: .whois <域名>  例如 .whois 01240820.xyz"
        domain = " ".join(args)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lookup_domain, domain)
