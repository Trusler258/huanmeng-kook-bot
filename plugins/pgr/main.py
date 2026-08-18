"""
Phigros 查询插件（.pgr）

迁移自 Core 静态命令 modules/pgr（COMMAND_MAP），改为插件注册命令。
功能与原命令一致：.pgr login/me/top/song/new/b{n}。

API Key 配置：编辑本插件目录下的 config.json，填入 api_key：
  {
    "api_key": "你的PGR开放平台Key"
  }
未配置时命令会提示去插件文件夹下配置。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.logger import get_logger

logger = get_logger("plugin.pgr")

_CFG_FILE = Path(__file__).resolve().parent / "config.json"


def _api_key_configured() -> bool:
    """检测 API Key 是否已配置（config.json 或环境变量）。"""
    try:
        if _CFG_FILE.exists():
            cfg = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
            if str(cfg.get("api_key", "")).strip():
                return True
    except Exception:
        pass
    import os
    return bool(os.getenv("PGR_API_KEY", ""))


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        self.ctx.capability.register_command(
            name="pgr", description="Phigros 查询：.pgr help 查看用法",
            handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    async def _cmd(self, msg):
        from modules.pgr import cmd_pgr as _impl

        args = msg.get("args") or []
        # Key 未配置 → 提示去插件文件夹配置
        if not _api_key_configured():
            return ("❌ PGR API Key 未配置\n"
                    "请编辑插件文件夹 plugins/pgr/config.json：\n"
                    "```json\n{\"api_key\": \"你的Key\"}\n```\n"
                    "或设置环境变量 PGR_API_KEY 后重启")
        return await _impl(
            args, msg.get("author"), msg.get("chat_id"),
            msg.get("sender"), bool(msg.get("is_group")),
            msg.get("bot_qq") or 0,
        )
