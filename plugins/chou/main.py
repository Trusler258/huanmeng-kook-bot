"""
随机抽取插件（一个命令一个插件）

迁移自 Core 静态命令 .抽，注册为插件命令。
功能与原命令一致：.抽 A B C 或 .抽 A,B,C
"""
from __future__ import annotations

import random

from core.logger import get_logger

logger = get_logger("plugin.chou")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("抽", "chou"):
            self.ctx.capability.register_command(
                name=name, description="随机抽取：.抽 A B C",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        from utils.format_lang import format_lang
        args = msg.get("args") or []
        if not args:
            return format_lang("luck.prompt")
        full = " ".join(args)
        if "," in full:
            options = [o.strip() for o in full.split(",") if o.strip()]
        else:
            options = args
        if len(options) < 2:
            return format_lang("luck.min_options")
        pick = random.choice(options)
        reactions = [
            f"我帮你决定了喵~ 选「{pick}」！(。-`ω´-)✧",
            f"喵呜～ 命运指引着你走向「{pick}」！",
            f"尾巴晃了晃，指向了「{pick}」喵～",
            f"闭上眼睛默念三秒… 就决定是「{pick}」了！",
            f"不用纠结啦，当然是「{pick}」喵～",
        ]
        return random.choice(reactions)
