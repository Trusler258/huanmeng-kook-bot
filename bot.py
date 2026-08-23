"""
Bot 核心类（KOOK 版）
- 基于 khl.py Bot 实例
- 事件循环主入口
- 配置热加载
- 优雅关闭
"""

import asyncio
import sys
import time
from typing import TYPE_CHECKING, Optional

from khl import Bot, MessageTypes, Message

from core.logger import get_logger, info, error, warning
from core.config import get_config, load_bot_config
from utils.format_lang import load_lang, format_lang
from services.sender import init_sender, close_sender
from core.dispatcher import EventDispatcher
from core.context_manager import init_context, get_context_mgr

if TYPE_CHECKING:
    pass

logger = get_logger("bot")

# 卡片按钮防重复点击：key = f"{msg_id}:{value}" → 最近处理时间
# 同一张卡片同一按钮在窗口内只响应一次，避免 update 等长任务被重复触发
_BTN_HANDLED: dict[str, float] = {}
_BTN_DEDUP_WINDOW = 600  # 秒；覆盖 update 长任务窗口


def _btn_dedup(key: str) -> bool:
    """返回 False 表示应忽略本次点击（重复），True 表示放行。"""
    if not key:
        return True
    now = time.time()
    last = _BTN_HANDLED.get(key)
    if last and now - last < _BTN_DEDUP_WINDOW:
        return False
    _BTN_HANDLED[key] = now
    # 清理过期条目，防止无界增长
    if len(_BTN_HANDLED) > 500:
        for k in [k for k, t in _BTN_HANDLED.items() if now - t >= _BTN_DEDUP_WINDOW]:
            _BTN_HANDLED.pop(k, None)
    return True


class HuanmengBot:
    """
    幻梦 KOOK Bot 核心类。

    负责：
    1. 启动时加载所有配置和初始化所有服务
    2. 构建 khl.py Bot 实例并注册消息处理器
    3. 启动后台任务（提醒/地震/日志/PC状态/TTS/战绩）
    4. 支持优雅关闭
    """

    VERSION = "2.0.2"

    def __init__(self):
        self.cfg: object = None          # type: ignore (BotConfig)
        self.khl_bot: Optional[Bot] = None
        self.dispatcher: Optional[EventDispatcher] = None
        self._running: bool = False

    async def initialize(self):
        """初始化：加载配置 → 初始化各服务 → 构建 khl.py Bot"""
        info("=" * 50)
        info("🐱 幻梦 KOOK Bot v%s 正在启动...", self.VERSION)
        info("=" * 50)

        # 1. 加载配置
        self.cfg = load_bot_config()
        from services.llm import _load_skill_sections
        _load_skill_sections()
        info("配置已加载 | bot=%s | debug=%s",
             self.cfg.bot_name, self.cfg.debug_mode)

        # 2. 初始化语言文件
        load_lang()
        info("语言文件已加载")

        # 3. 初始化日志系统
        from core.logger import init_logger
        init_logger(debug_mode=self.cfg.debug_mode)
        info("日志系统已初始化")

        # 自动更新：KOOK Bot 禁用（通过本地改代码 → scp 上传方式部署）
        info("自动更新已禁用（KOOK Bot 使用手动部署模式）")

        # 4. 构建 khl.py Bot 实例
        self.khl_bot = Bot(token=self.cfg.kook_token)
        info("khl.py Bot 实例已创建")

        # 5. 拉取 bot 自身信息，填充 bot_id_str / bot_qq
        try:
            me = await self.khl_bot.client.fetch_me()
            self.cfg.bot_id_str = str(me.id)
            try:
                self.cfg.bot_qq = int(me.id)
            except (ValueError, TypeError):
                self.cfg.bot_qq = hash(str(me.id))
            info("Bot 自身信息: id=%s name=%s", self.cfg.bot_id_str, getattr(me, 'username', ''))
        except Exception as e:
            warning("拉取 bot 自身信息失败: %s（启动后仍可工作，但 @检测可能受影响）", e)

        # 6. 初始化发送器（注入 khl.py Bot 实例）
        init_sender(self.khl_bot)
        info("消息发送器已初始化")

        # 6a. Phase 20 P0：初始化 SQLite/FTS5（正式接入生产热路径）。
        # 失败仅降级到 Legacy（文件 memory / 内存搜索缓存），绝不阻断 Bot 启动。
        try:
            from db.database import init_db
            await init_db()
            from db.database import db
            info("数据库已就绪 (url=%s)", db.url)
            # 存量 .md 记忆回填 SQLite（幂等；失败仅降级，不阻断启动）
            try:
                from modules.memory import backfill_memories_to_db
                n = await backfill_memories_to_db()
                if n:
                    info("存量记忆已回填 SQLite: %d 条", n)
            except Exception as e:
                warning("记忆回填失败（不影响启动）: %s", e)
        except Exception as e:
            warning("数据库初始化失败，进入 Legacy fallback: %s", e)

        # 6a. 初始化用户名查询模块（注入 khl.py Bot 实例）
        from utils.username import init_username
        init_username(self.khl_bot)
        info("用户名查询模块已初始化")

        # 7. 初始化上下文管理器
        init_context()
        info("上下文管理器已初始化")

        # 8. 恢复未完成的五子棋对局（持久化恢复）
        from modules.wzq import load_games
        load_games()

        # 9. 判断模块关键词初始化
        from modules.judge import init_keywords
        init_keywords()

        # 10. 构建事件分发器
        self.dispatcher = EventDispatcher(khl_bot=self.khl_bot)
        import core.dispatcher as _disp
        _disp._current_dispatcher = self.dispatcher
        info("事件分发器已就绪")

        # 11. 注册 khl.py 消息处理器
        self._register_handlers()
        info("消息处理器已注册（TEXT/KMD）")

        # 12. 启动 Plugin Runtime（非阻塞，单插件失败不影响 Core）
        try:
            from core.plugin import get_plugin_manager
            mgr = get_plugin_manager()
            ok_names = await mgr.load_all()
            info("Plugin Runtime 就绪: 加载 %d 个插件 %s", len(ok_names), ok_names)
        except Exception as e:
            warning("Plugin Runtime 初始化降级: %s", e)

        info("=" * 50)
        info("✅ 所有组件初始化完成，准备连接 KOOK...")
        info("=" * 50)

    def _register_handlers(self):
        """注册 khl.py 消息处理器
        on_message(*except_type) 的参数是排除列表，不传 = 监听所有非 SYS 消息
        """
        @self.khl_bot.on_message()  # 监听 TEXT/KMD/IMG/VIDEO/FILE/AUDIO/CARD
        async def on_msg(msg: Message):
            try:
                await self.dispatcher.dispatch(msg)
            except Exception as e:
                error("消息处理异常: %s", e, exc_info=True)

        # KOOK 卡片按钮点击回调（message_btn_click 系统事件）
        # 按钮 value 即指令字符串（.notifyset <cid> / .testok 等），
        # 点击后按普通指令执行并回复到原频道
        from khl import EventTypes

        @self.khl_bot.on_event(EventTypes.MESSAGE_BTN_CLICK)
        async def on_btn_click(bot, event):
            try:
                body = event.body or {}
                value = str(body.get("value", "")).strip()
                user_id = str(body.get("user_id", ""))
                target_id = str(body.get("target_id", ""))
                msg_id = str(body.get("msg_id", ""))
                if not value:
                    return

                # 防二次点击：同一消息同一按钮窗口内只执行一次
                dedup_key = f"{msg_id}:{value}"
                if not _btn_dedup(dedup_key):
                    info("按钮重复点击已忽略 key=%s value=%s", dedup_key, value[:40])
                    return

                from core.config import get_config
                from modules.commands import handle_command
                from services.sender import send_by_chat_type
                from utils.username import get_or_resolve_username
                cfg = get_config()
                is_group = bool(target_id)
                chat_id = int(target_id) if target_id.isdigit() else 0
                uid = int(user_id) if user_id.isdigit() else 0

                # 解析点击人昵称（失败则退回 user_id）
                nickname = user_id
                try:
                    resolved = await get_or_resolve_username(user_id)
                    if resolved:
                        nickname = resolved
                except Exception:
                    pass

                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                info("卡片按钮点击 value=%s user=%s(%s) target=%s", value[:40], user_id, nickname, target_id)

                result = await handle_command(value, uid, chat_id, "", is_group, cfg.bot_qq)

                # 点击记录：时间戳 / 点击人昵称 / 频道ID / 按钮值 / 按钮返回内容
                detail = (
                    f"**按钮点击记录**\n"
                    f"- 时间戳：`{ts}`\n"
                    f"- 点击人：`{nickname}`（ID `{user_id}`）\n"
                    f"- 频道 ID：`{target_id or '私聊'}`\n"
                    f"- 按钮值：`{value}`\n"
                    f"- 按钮返回内容：`{result or '(无)'}`"
                )
                # 原生卡片返回：优先发卡片，再发点击记录；否则照旧拼接发送
                if isinstance(result, str) and result.startswith("__CARD__:"):
                    from core.pipeline import _send_cmd_card
                    await _send_cmd_card(result[len("__CARD__:"):],
                                         chat_id, is_group, user_id if not is_group else None)
                    await send_by_chat_type(detail, chat_id, is_group, user_id if not is_group else None)
                elif result:
                    await send_by_chat_type(f"{detail}\n\n---\n{result}", chat_id, is_group, user_id if not is_group else None)
                else:
                    await send_by_chat_type(detail, chat_id, is_group, user_id if not is_group else None)
            except Exception as e:
                error("卡片按钮回调异常: %s", e, exc_info=True)

    async def run(self):
        """主循环：启动 khl.py Bot + 后台任务"""
        self._running = True

        # ★ 后台任务已迁移至插件 plugins/bg_tasks，由 Plugin Runtime 自动管理

        # 注入高风险更新人工审批回调（卡片确认后放行）
        try:
            from modules._auto_update.safe_update import set_approve_callback
            from services.notify_system import request_update_approval
            set_approve_callback(request_update_approval)
            info("已注入高风险更新人工审批回调")
        except Exception as e:
            warning("注入高风险更新审批回调失败: %s", e)

        info("后台任务: 已由插件 plugins/bg_tasks 接管")

        # ★ 预启动 Chromium 和渲染队列（不阻塞聊天）
        try:
            from core.queues import start_render_queue
            start_render_queue()
            from modules.changelog import _ensure_browser
            await _ensure_browser()
            info("Chromium 已预启动 + 渲染队列就绪")
        except Exception as e:
            warning("Chromium 预启动失败: %s (将在首次使用时懒加载)", e)

        # 启动 khl.py Bot（这是阻塞调用，直到 bot 停止）
        try:
            await self.khl_bot.start()
        except Exception as e:
            error("khl.py Bot 运行异常: %s", e, exc_info=True)
            if self._running:
                info("⏳ 5 秒后重试...")
                await asyncio.sleep(5)
                await self.khl_bot.start()

    def stop(self):
        """触发停止信号"""
        self._running = False
        info("停止信号已发出")
        if self.khl_bot is not None:
            try:
                # khl.py Bot 通过取消事件循环来停止
                info("正在关闭 khl.py Bot...")
            except Exception:
                pass

    async def shutdown(self):
        """优雅关闭所有资源"""
        info("🛑 正在关闭...")

        # 关闭 Plugin Runtime（卸载所有插件，清理定时器/事件/能力注册）
        try:
            from core.plugin import get_plugin_manager
            await get_plugin_manager().shutdown_all()
            info("Plugin Runtime 已关闭")
        except Exception as e:
            warning("Plugin Runtime 关闭降级: %s", e)

        # 关闭发送器
        await close_sender()

        # 输出统计
        ctx = get_context_mgr()
        stats = ctx.get_stats()
        info("运行统计: %s", stats)

        # 刷新搜索缓存
        try:
            from modules.judge import flush_search_cache
            flush_search_cache()
        except Exception:
            pass

        # 持久化瞬时上下文（重启不丢记忆）
        from core.context_manager import save_context
        save_context()

        # Phase 20 P0：优雅关闭数据库（dispose 引擎，避免连接泄漏）
        try:
            from db.database import close_db
            await close_db()
            info("数据库已关闭")
        except Exception as e:
            warning("数据库关闭降级: %s", e)

        info("👋 再见！")
        print("")  # 空行让日志更清晰

    def handle_reload(self):
        """处理 .reload 指令 / SIGUSR1：全量热重载（config+lang+keywords+skills）"""
        from core.config import full_reload
        self.cfg = full_reload()
        info("配置热加载完成（上下文保留）")

    async def _send_reload_done(self):
        """发送重载完成回执（如果是从 .reload 触发的重启）"""
        import json
        from pathlib import Path
        state_path = Path(__file__).resolve().parent / "data" / "reload_state.json"

        if not state_path.exists():
            return

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state_path.unlink()
        except Exception:
            return

        chat_id = state.get("chat_id")
        is_group = state.get("is_group")
        if not chat_id:
            return

        from services.sender import send_group_msg, send_private_msg

        msg = "✅ 重载完成喵~ done! (。-`ω´-)✧"
        try:
            if is_group:
                await send_group_msg(msg, chat_id)
            else:
                await send_private_msg(msg, chat_id)
            info("重载回执已发送: chat=%s is_group=%s", chat_id, is_group)
        except Exception as e:
            error("重载回执发送失败: %s", e)
