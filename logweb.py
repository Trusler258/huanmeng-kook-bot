#!/usr/bin/env python3
"""
幻梦 KOOK Bot — 独立日志控制台服务
监听 62000 端口，通过文件 watch 读取 logs/huanmeng.log 新增行并广播给浏览器。

部署：作为独立 systemd 服务（kook-logweb.service）运行，与 bot 进程解耦。
"""

import asyncio
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from core.log_server import start

    port = 62000
    print("=" * 55)
    print("  幻梦 KOOK Bot · 日志控制台")
    print(f"  独立模式 · 端口 {port}")
    print("  监听文件: logs/huanmeng.log")
    print("=" * 55)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(start(port=port, standalone=True))
    except KeyboardInterrupt:
        print("\n键盘中断，退出")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
