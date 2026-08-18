"""
TUF 谱面查询插件（ADOFai 节奏游戏社区）

迁移自 Core 静态命令（tuflevel/tufsearch/tufd/tufpage），合并为单插件。
数据源：https://api.tuforums.com 官方 API（services.tuf_api / modules.tuf_commands）。

功能：
  .tuflevel <名称/ID>  谱面详情卡片（截图发送）
  .tufsearch <关键词> [页码]  搜索谱面（截图发送）
  .tufd <编号>         下载搜索结果中的谱面
  .tufpage <页码>      搜索结果翻页
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger("plugin.tuf")


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("tuflevel", "tuf谱面"):
            self.ctx.capability.register_command(
                name=name, description="TUF 谱面详情：.tuflevel <名称/ID>",
                handler=self._cmd_tuflevel)
        self.ctx.capability.register_command(
            name="tufsearch", description="TUF 谱面搜索：.tufsearch <关键词> [页码]",
            handler=self._cmd_tufsearch)
        self.ctx.capability.register_command(
            name="tufd", description="下载搜索结果中的谱面：.tufd <编号>",
            handler=self._cmd_tufd)
        self.ctx.capability.register_command(
            name="tufpage", description="搜索结果翻页：.tufpage <页码>",
            handler=self._cmd_tufpage)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    def _base(self, msg):
        """构造与原命令一致的参数元组。"""
        return (
            msg.get("args") or [],
            msg.get("author"),
            msg.get("chat_id"),
            msg.get("sender"),
            bool(msg.get("is_group")),
            msg.get("bot_qq") or 0,
        )

    async def _cmd_tuflevel(self, msg):
        from modules.tuf_commands import cmd_tuflevel as _impl
        return await _impl(*self._base(msg))

    async def _cmd_tufsearch(self, msg):
        from modules.tuf_commands import cmd_tuf_search as _impl
        return await _impl(*self._base(msg))

    async def _cmd_tufd(self, msg):
        from modules.tuf_commands import cmd_tufd as _impl
        return await _impl(*self._base(msg))

    async def _cmd_tufpage(self, msg):
        from modules.tuf_commands import cmd_tufpage as _impl
        return await _impl(*self._base(msg))
