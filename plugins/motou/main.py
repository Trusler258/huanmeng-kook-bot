"""
摸头 GIF 插件（UAPI post-image-motou）

用任意图片生成「摸摸头」动态 GIF：
  .摸头 <图片URL>           指定图片来源（URL 可带反引号/Markdown 包裹）
  .摸头                     自动取本消息里的图片（KOOK (met) 图片）
  .摸头 引用一张图片消息    从被引用消息里取图片
  .摸头 <URL> bg=透明色      自定义背景，如 bg=transparent / bg=%23ffffff

图片提取复用插件 API ctx.image（dispatcher 成熟实现）：
  参数 URL / 引用消息 / 本消息文本逐级兜底，无需自造重复逻辑。

实现：POST https://uapis.cn/api/v1/image/motou（multipart: image_url / bg_color）
成功返回 image/gif 二进制，保存临时文件后经 send_file 上传 KOOK asset 发送。
若 manifest.config.uapi_key 配置了 key，自动带上 Bearer 鉴权。
"""
from __future__ import annotations

import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("plugin.motou")

_API_URL = "https://uapis.cn/api/v1/image/motou"
_TMP_DIR = Path(__file__).resolve().parent / "tmp"


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        for name in ("摸头", "motou"):
            self.ctx.capability.register_command(
                name=name, description="用图片生成摸头 GIF：.摸头 <图片URL>，或引用一张图片消息",
                handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    # ── 命令处理 ────────────────────────────────────────
    async def _cmd(self, msg):
        args = [str(a) for a in (msg.get("args") or [])]
        chat_id = msg.get("chat_id")
        is_group = bool(msg.get("is_group"))
        image = self.ctx.image

        # 1) 解析参数：bg_color + 可能的 URL（允许被反引号/Markdown 包裹）
        image_url = ""
        bg = ""
        for a in args:
            low = a.lower()
            if low.startswith("bg="):
                bg = a.split("=", 1)[1].strip()
            else:
                urls = image.extract_urls(a)
                if urls:
                    image_url = urls[0]

        # 2) 参数没给 URL → 引用消息里的图片
        if not image_url:
            imgs = await image.fetch_quote_images(str(msg.get("quote_id") or ""))
            if imgs:
                image_url = imgs[0]

        # 3) 还没有 → 从本消息文本里提取图片
        if not image_url:
            text = str(msg.get("text") or "")
            for u in image.extract_urls(text):
                if u and "uapis.cn" not in u:
                    image_url = u
                    break

        if not image_url:
            return ("找不到图片来源。用法：`.摸头 <图片URL>`，"
                    "或 `.摸头` 并引用一张图片消息。")

        # 4) 调用 UAPI 生成摸头 GIF
        try:
            import httpx
            headers = {}
            key = str(self.ctx.config("uapi_key", "") or "").strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            data = {"image_url": image_url}
            if bg:
                data["bg_color"] = bg
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.post(_API_URL, data=data, headers=headers)
            if resp.status_code != 200:
                return f"❌ 摸头生成失败（HTTP {resp.status_code}）：{resp.text[:120]}"
            gif = resp.content
        except Exception as e:
            logger.error("摸头接口异常: %s", e)
            return f"❌ 摸头生成失败：{e}"

        # 5) 保存临时文件并发送（.gif 会被识别为图片消息）
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        path = _TMP_DIR / f"motou_{int(time.time() * 1000)}.gif"
        try:
            path.write_bytes(gif)
            from services.sender import send_file
            ok = await send_file(str(path), chat_id, is_group)
            if not ok:
                return "❌ GIF 已生成但发送失败。"
            return None  # 已自行发送图片
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
