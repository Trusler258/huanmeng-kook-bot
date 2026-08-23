#!/usr/bin/env python3
"""
幻梦 KOOK Bot — 入口文件
用法: python main.py [--debug]
      echo reload > data/control.txt  热重载配置（不重启）
"""

import asyncio
import argparse
import signal
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    """程序入口"""
    # 命令行参数
    parser = argparse.ArgumentParser(description="幻梦 KOOK Bot")
    parser.add_argument("--debug", action="store_true", help="启用 debug 模式")
    args = parser.parse_args()

    from bot import HuanmengBot
    from core.logger import info, error, set_debug_mode as _set_log_debug

    if args.debug:
        _set_log_debug(True)
        info("Debug 模式已启用（命令行参数 --debug）")

    bot = HuanmengBot()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _amain():
        await bot.initialize()
        await bot.run()

    main_task = loop.create_task(_amain())

    def signal_handler(sig, frame):
        sig_name = sig.name if hasattr(sig, 'name') else sig
        info("\n接收到中断信号 (signal=%s)，正在关闭...", sig_name)
        # 取消主任务：khl_bot.start() 是常驻 gather（handle_pkg + gate.run），
        # 不取消的话 run_until_complete 永远阻塞，SIGTERM 后无法进入 shutdown，
        # systemd 只能等超时强杀（表现为关闭卡十几秒）。
        main_task.cancel()

    def reload_handler(sig, frame):
        """SIGUSR1: 热重载配置，不重启进程"""
        info("\n收到 SIGUSR1 信号，热重载配置...")
        bot.handle_reload()
        info("热重载完成")

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGUSR1, reload_handler)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        info("主任务已取消，进入优雅关闭")
    except KeyboardInterrupt:
        info("\n键盘中断 (Ctrl+C)")
    except Exception as e:
        error("主流程异常: %s", e, exc_info=True)
    finally:
        # 优雅关闭：带超时兜底，任何一步卡住都不阻塞退出
        try:
            loop.run_until_complete(asyncio.wait_for(bot.shutdown(), timeout=15))
        except asyncio.TimeoutError:
            info("优雅关闭超时，强制清理剩余资源")
        except Exception as e:
            info("优雅关闭异常（已忽略）: %s", e)
        # 清理残留任务（插件后台协程 / 渲染队列等），避免 loop.close() 挂起
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        loop.close()
        info("应用程序已退出")


if __name__ == "__main__":
    print("=" * 55)
    print("  幻梦 KOOK Bot  v2.0.0")
    print("  Powered by khl.py")
    print("  私聊 · 字频道 · 频道")
    print("=" * 55)
    main()
