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
    from core.logger import info, set_debug_mode as _set_log_debug

    if args.debug:
        _set_log_debug(True)
        info("Debug 模式已启用（命令行参数 --debug）")

    bot = HuanmengBot()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        sig_name = sig.name if hasattr(sig, 'name') else sig
        info("\n接收到中断信号 (signal=%s)，正在关闭...", sig_name)
        bot.stop()

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
        loop.run_until_complete(bot.initialize())
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        info("\n键盘中断 (Ctrl+C)")
    finally:
        loop.run_until_complete(bot.shutdown())
        loop.close()
        info("应用程序已退出")


if __name__ == "__main__":
    print("=" * 55)
    print("  幻梦 KOOK Bot  v2.0.0")
    print("  Powered by khl.py")
    print("  私聊 · 字频道 · 频道")
    print("=" * 55)
    main()
