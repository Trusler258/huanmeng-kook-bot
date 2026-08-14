"""
附件服务（Huanmeng 2.0 Phase 5）

职责：
- 将本地文件 / 外部 URL 上传为 KOOK asset。
- 外部 HTTP 图片下载：带超时、浏览器 User-Agent（绕过 KOOK CDN 403），下载后上传。
- 图片/文件类型判定（决定 IMG 或 FILE 发送类型）。

所有外部 I/O 均有超时，超时后抛异常交由 response_delivery 降级。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp

from core.logger import get_logger

logger = get_logger("sender.attachment")

# 图片后缀 → IMG(type=2)；其余文件 → FILE(type=4)
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".jfif"}

# KOOK CDN 需要浏览器 UA 才能下载外部图片，否则 403
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_TMP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "img_temp"


class AttachmentService:
    """附件上传服务。依赖 kook_transport 提供 create_asset。"""

    def __init__(self, transport):
        self._transport = transport

    @staticmethod
    def is_image(path_or_name) -> bool:
        return Path(path_or_name).suffix.lower() in IMG_EXT

    async def upload(self, path: str) -> str:
        """将本地文件上传为 KOOK asset，返回 URL。"""
        return await self._transport.create_asset(path)

    async def download_and_upload(self, url: str, timeout: float = 10.0) -> str:
        """下载外部 HTTP 图片（带超时 + 浏览器 UA）后上传为 KOOK asset，返回 URL。

        失败时抛出异常（由 response_delivery 决定降级策略）。
        """
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": _BROWSER_UA}
        async with aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    raise IOError(f"下载外部图片失败: HTTP {resp.status}")
                data = await resp.read()
                suffix = os.path.splitext(url.split('?')[0])[-1] or '.png'
                # 同步磁盘写入放到线程池，避免阻塞 event loop
                tmp_name = await asyncio.to_thread(self._write_temp, data, suffix)
                try:
                    return await self._transport.create_asset(tmp_name)
                finally:
                    await asyncio.to_thread(self._remove_temp, tmp_name)

    @staticmethod
    def _write_temp(data: bytes, suffix: str) -> str:
        """同步写入临时文件，返回路径（在线程池中执行）。"""
        tmp = tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, dir=str(_TMP_DIR))
        try:
            tmp.write(data)
        finally:
            tmp.close()
        return tmp.name

    @staticmethod
    def _remove_temp(path: str):
        """同步删除临时文件（在线程池中执行）。"""
        try:
            os.unlink(path)
        except OSError:
            pass