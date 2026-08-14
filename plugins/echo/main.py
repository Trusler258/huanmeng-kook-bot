"""示例插件：注册 .pecho 命令，回显文本。"""


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        # 注册命令能力（通过公开 Plugin API）
        self.ctx.capability.register_command(
            name="pecho",
            description="回显文本：.pecho <内容>",
            handler=self._handle_echo,
        )

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _handle_echo(self, msg):
        args = (msg.get("args") or [])
        text = " ".join(str(a) for a in args).strip()
        if not text:
            return "用法：.pecho <内容>"
        return f"{self.ctx.config('prefix', '')}{text}"