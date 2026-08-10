"""
KOOK 事件分发器
- 接收 khl.py Message 对象
- 转换为 pipeline 可处理的格式
- 路由到：指令处理 / 普通消息管道
- 指令前缀: .
"""

from __future__ import annotations

import re
import asyncio
import logging

from core.logger import get_logger
from core.config import get_config
from core.pipeline import process_message

logger = get_logger("dispatcher")


class EventDispatcher:
    """
    KOOK 事件分发器。
    接收 khl.py Message 对象，提取字段后路由到 pipeline。
    """

    def __init__(self, khl_bot=None):
        self.khl_bot = khl_bot
        self._msg_count = 0
        # 消息去重
        self._seen_ids: dict[str, None] = {}
        self._seen_max = 500

    @property
    def msg_count(self) -> int:
        return self._msg_count

    async def dispatch(self, msg) -> None:
        """处理一条 khl.py Message"""
        try:
            _raw_type = getattr(msg, 'type', None)
            _raw_content = getattr(msg, 'content', None)
            logger.debug("dispatch 入口: type=%s content=%r", _raw_type, _raw_content[:80] if _raw_content else None)
            await self._dispatch_inner(msg)
        except Exception as e:
            import traceback
            logger.error("dispatch 异常(前%d): %s\n%s",
                self._msg_count, e, traceback.format_exc())

    async def _dispatch_inner(self, msg) -> None:
        # ══ 注意：这里必须在方法内部直接取 msg.type / msg.content
        #    之前曾错误地引用了 dispatch() 方法里的局部变量 _raw_type，
        #    导致 NameError: name '_raw_type' is not defined
        _raw_type = getattr(msg, 'type', None)
        _raw_content = getattr(msg, 'content', None)

        cfg = get_config()

        # ── 消息去重 ──
        msg_id = str(msg.id) if hasattr(msg, 'id') else ""
        if msg_id:
            if msg_id in self._seen_ids:
                logger.debug("去重跳过: msg_id=%s", msg_id)
                return
            self._seen_ids[msg_id] = None
            if len(self._seen_ids) > self._seen_max:
                keep = list(self._seen_ids.keys())[-(self._seen_max // 2):]
                self._seen_ids = {k: None for k in keep}

        # ── 提取基本字段 ──
        # khl.py 有两类消息: PublicMessage(频道/群聊) 和 PrivateMessage(私聊)
        from khl.message import PublicMessage, PrivateMessage
        is_group = isinstance(msg, PublicMessage)
        channel_type = "GROUP" if is_group else "PERSON"

        # 获取 channel_id 和 user_id
        channel_id = ""
        if hasattr(msg, 'ctx') and hasattr(msg.ctx, 'channel'):
            channel_id = str(msg.ctx.channel.id)

        user_id_str = str(msg.author_id) if hasattr(msg, 'author_id') else ""

        # ── 过滤 bot 消息（防止 bot 互相回复形成循环）──
        # 1. 过滤自己发的消息
        if user_id_str and user_id_str == str(cfg.bot_qq):
            return
        if user_id_str and user_id_str == cfg.bot_id_str and cfg.bot_id_str:
            return
        # 2. 过滤其他 bot 发的消息（KOOK bot 用户有 bot 标识）
        if hasattr(msg, 'author') and msg.author:
            is_bot_author = getattr(msg.author, 'bot', False) or \
                            getattr(msg.author, 'is_bot', False)
            if is_bot_author:
                logger.debug("跳过 bot 消息: uid=%s", user_id_str)
                return

        logger.debug("KMD 调试: is_group=%s uid=%s channel=%s", is_group, user_id_str, channel_id)

        # chat_id: 群聊=channel_id, 私聊=user_id
        chat_id = channel_id if is_group else user_id_str
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            # KOOK ID 理论上都是纯数字，兜底用 crc32 保证跨重启稳定
            import zlib as _zlib
            chat_id_int = _zlib.crc32(chat_id.encode("utf-8"))

        try:
            user_id_int = int(user_id_str)
        except ValueError:
            import zlib as _zlib
            user_id_int = _zlib.crc32(user_id_str.encode("utf-8"))

        # 获取发送者昵称
        sender_name = user_id_str
        if hasattr(msg, 'author') and msg.author:
            if hasattr(msg.author, 'nickname'):
                sender_name = msg.author.nickname or sender_name
            elif hasattr(msg.author, 'username'):
                sender_name = msg.author.username or sender_name

        # bot_id
        bot_id = cfg.bot_qq
        try:
            bot_id_str = str(bot_id)
        except Exception:
            bot_id_str = ""

        # ── 提取消息内容 ──
        content = msg.content or "" if hasattr(msg, 'content') else ""

        # 检测 @机器人
        is_mentioned = False
        # 方法1: 检查 msg.mention 列表
        if hasattr(msg, 'mention') and msg.mention:
            if bot_id_str in [str(m) for m in msg.mention]:
                is_mentioned = True
        # 方法2: 检查 content 中的 (met)bot_id(met)
        if not is_mentioned and bot_id_str:
            if f'(met){bot_id_str}(met)' in content:
                is_mentioned = True

        # ── 1) 当前消息 attachments 抽所有图片（兼容 dict/对象两种格式）──
        image_urls: list[str] = []
        attachments_raw = getattr(msg, 'attachments', None) or []
        if attachments_raw:
            image_urls = self._extract_image_urls(attachments_raw)

        # ══ P0 修复（v1.1 / 2026-08-11）：KOOK 用户端发图片时，平台有时不走 attachments
        #    而是包装成一条 MessageTypes.CARD 消息，content = '[{"theme":"invisible",...}]'
        #    卡片内部用 image / image-group / container 模块嵌图片，旧版完全不解析。
        _raw_type_str = str(_raw_type) if _raw_type is not None else ""
        _looks_like_card = isinstance(content, str) and content.lstrip().startswith("[{")
        if ("CARD" in _raw_type_str.upper()) or _looks_like_card:
            try:
                card_imgs = EventDispatcher._extract_card_images(content)
            except Exception:
                card_imgs = []
            if card_imgs:
                logger.info("🧩 CARD 消息解析到 %d 张图片 (attachments=%d, theme_json=%s)",
                            len(card_imgs), len(image_urls), content[:40].replace("\n", " "))
            # 去重合并保持顺序
            _seen: set[str] = set()
            merged: list[str] = []
            for u in [*image_urls, *card_imgs]:
                if u and u not in _seen:
                    _seen.add(u)
                    merged.append(u)
            image_urls = merged

        msg_type = "图文" if (image_urls and content.strip() and not _looks_like_card) else (
            "图片" if image_urls else "文字"
        )

        # ── 2) 引用消息提取（正文 + 附件图片，KOOK引用一条图片消息时图在这里）──
        quoted_text, quoted_image_urls = self._extract_quote_info(msg)
        if quoted_text:
            logger.info("📎 引用消息原文 (%d字)", len(quoted_text))
        if quoted_image_urls:
            logger.info("📎 引用消息含 %d 张图片", len(quoted_image_urls))

        # ── 白名单检查 ──
        # 指令检测（前缀 .）：图文/纯文字都可能是指令
        has_txt = bool(content.strip())
        is_command = has_txt and content.lstrip().startswith(".")

        if is_group:
            # group_list 为空 = 不限制（允许所有频道）；非空才做白名单校验
            if cfg.group_list and chat_id_int not in cfg.group_list and not is_command:
                return  # 非白名单频道 → 静默
        else:
            if not cfg.enable_private and not is_command:
                return  # 私聊未启用
            if cfg.private_whitelist and user_id_int not in cfg.private_whitelist and not is_command:
                return  # 不在私聊白名单

        # ── 忽略用户检查 ──
        try:
            from modules.ignore_users import is_ignored
            if is_ignored(user_id_int) and user_id_int != cfg.admin_qq:
                logger.debug("忽略用户: uid=%s", user_id_str)
                return
        except Exception:
            pass  # ignore_users 模块可能不存在

        self._msg_count += 1

        logger.info(
            "📩 消息 #%d | type=%s | imgs=%d | quoted_imgs=%d | from=%s(%s) | chat=%s | group=%s | content='%s...'",
            self._msg_count, msg_type, len(image_urls), len(quoted_image_urls),
            sender_name, user_id_str, chat_id, is_group,
            content[:30].replace("\n", " "),
        )

        # ── 预设昵称覆盖 ──
        preset_name = cfg.qq_name_map.get(user_id_str)
        if preset_name:
            sender_name = preset_name

        # ── 自动记录 user_id → 昵称（后台异步，不阻塞）──
        if sender_name and sender_name != user_id_str:
            try:
                from utils.username import record_user_name
                asyncio.ensure_future(record_user_name(user_id_str, sender_name))
            except Exception:
                pass

        # ── 先判定：是否需要进入管道（即：机器人是否需要看这条消息并可能回复）──
        #   =TRUE 的情况：有文字 / 是指令 / 被@ / 有引用文字 / 有引用图片 → 必须先同步等图片识别完再入队
        #   =FALSE 的情况：纯图片（无文、非@、非指令、无引用）→ 不进管道，图片识别可纯后台异步
        need_pipeline_reply = bool(has_txt or is_command or is_mentioned
                                   or quoted_text or quoted_image_urls)

        # ── 图片识别：当前消息图 + 引用消息图 收集 ──
        all_img_urls_to_recognize: list[str] = []
        if image_urls:
            all_img_urls_to_recognize.extend(image_urls)
        if quoted_image_urls:
            all_img_urls_to_recognize.extend(quoted_image_urls)

        # ═══ 双路径识别策略 ═══
        if need_pipeline_reply and all_img_urls_to_recognize:
            # ── 路径A（同步等待）：要进管道回复 → 先识别完，描述注入上下文 → 再入队
            #    解决：之前 ensure_future 后台跑，LLM 先回复完，图片描述后到，等于白识别
            logger.info("🖼️  [chat=%s] 需回复的图文/引用图消息 → 同步识别 %d 张（最多15s/张）",
                        chat_id_int, len(all_img_urls_to_recognize))
            for idx, _img_url in enumerate(all_img_urls_to_recognize, 1):
                try:
                    _desc = await asyncio.wait_for(
                        self._do_recognize_image(
                            _img_url, cfg, chat_id_int, sender_name, mark=f"#{idx}/{len(all_img_urls_to_recognize)}"
                        ),
                        timeout=15.0,  # 单张15秒硬超时，识别失败不阻塞整体
                    )
                    if _desc and _desc.strip():
                        from core.context_manager import get_context_mgr
                        _ctx = get_context_mgr()
                        _short = _desc[:80].replace("\n", " ")
                        # ✅ 先注入上下文（在 enqueue_message process_message 之前）
                        _ctx.append_to_context(chat_id_int, f'[图片描述{idx}]"{_short}" {sender_name}')
                        logger.info("🖼️  [chat=%s] 图%d 识别完成并注入上下文: '%s...'",
                                    chat_id_int, idx, _short[:50])
                except asyncio.TimeoutError:
                    logger.warning("🖼️  [chat=%s] 图%d 识别超时(>15s)，跳过阻塞: %s",
                                   chat_id_int, idx, str(_img_url)[:60].replace("\n", " "))
                except Exception as _e:
                    logger.warning("🖼️  [chat=%s] 图%d 识别失败跳过: %s", chat_id_int, idx, _e)
        elif all_img_urls_to_recognize:
            # ── 路径B（纯后台）：纯图片静默不进管道 → 异步跑不阻塞（旧行为，但数量改成全识别）
            for _img_url in all_img_urls_to_recognize:
                asyncio.ensure_future(self._bg_recognize_image(
                    _img_url, cfg, chat_id_int, is_group, user_id_int, sender_name, is_mentioned
                ))

        # ── 图+文混合消息 / @图文 ：保留文字内容进入管道，不 return ──
        #   场景修复：
        #   1) 用户发"这是什么 [图]" → 之前纯图且非@直接 return，文字丢了
        #   2) 用户引用一张图片问"帮我描述一下" → 之前引用图片没识别，文字也没进管道
        #   3) 引用图+@bot → 文字+图描述都进上下文
        if msg_type in ("图片", "图文"):
            if need_pipeline_reply:
                # 有文字/指令/@/引用 都进管道
                if msg_type == "图片":
                    content = content or "[图片]"
                msg_type = "文字"
            else:
                # 纯图片（无文、非@、非指令、无引用）：静默不进管道
                return

        # ── @ 替换：KMarkdown (met)id(met) → @昵称 ──
        content = self._replace_mentions(content, cfg)

        # ── 缓存频道对象（供 sender 使用）──
        if hasattr(msg, 'ctx') and hasattr(msg.ctx, 'channel'):
            from services.sender import cache_channel
            cache_channel(chat_id, msg.ctx.channel)

        # ── 调用消息处理管道 ──
        from core.queues import enqueue_message
        await enqueue_message(
            msg_type=msg_type,
            msg_content=content,
            chat_id=chat_id_int,
            sender_name=sender_name,
            user_id=user_id_int,
            is_group=is_group,
            bot_qq=bot_id,
            raw_event=msg,
            raw_message=content,
            quoted_msg=quoted_text,
            is_mentioned=is_mentioned,
            image_urls=image_urls,
            quoted_image_urls=quoted_image_urls,
        )

    @staticmethod
    def _extract_image_urls(attachments) -> list[str]:
        """从 khl Message.attachments（list[dict] 或 list[对象]）抽所有图片 URL。"""
        urls: list[str] = []
        if not attachments:
            return urls
        for att in attachments:
            try:
                url = ""
                if isinstance(att, dict):
                    t = att.get("type", "")
                    if t == "image" or (isinstance(t, str) and "image" in t.lower()):
                        url = att.get("url", "") or att.get("src", "")
                elif hasattr(att, "type"):
                    t_str = str(getattr(att, "type", "")).lower()
                    if "image" in t_str or t_str == "2":  # khl.py 图片 type 有时是字符串 "image" 或枚举值 2
                        url = (getattr(att, "url", None) or ""
                               or getattr(att, "src", None) or "")
                # 有些版本 khl.py 附件没 type 字段，直接看 url/file_type 后缀
                if not url and hasattr(att, "url"):
                    u = getattr(att, "url", "") or ""
                    ft = str(getattr(att, "file_type", "")).lower()
                    name = str(getattr(att, "name", "")).lower()
                    if (ft in ("png", "jpg", "jpeg", "gif", "webp", "bmp")
                            or name.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp", "bmp")
                            or any(u.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
                            or any(ext in u.lower().split("?", 1)[0] for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))):
                        url = u
                if url and isinstance(url, str) and (url.startswith("http") or url.startswith("https")):
                    urls.append(url)
            except Exception:
                continue
        return urls

    @staticmethod
    def _extract_card_images(card_payload) -> list[str]:
        """
        从 KOOK 卡片 JSON（list[card] / dict card / JSON 字符串）里递归提取所有图片 URL。
        覆盖 12 个 KOOK 卡片模块中含图的场景：
          - image:            src
          - image-group:      elements[].src
          - container:        modules[] 递归
          - section:          accessory (若 type=image 则取 src)
          - context:          elements[] 里可能嵌套 image 元素（type=image/src 字段）
        纯文本类模块（header/divider/action-group/file/audio/video/countdown/invite）无独立图片，跳过。
        """
        urls: list[str] = []
        import json as _json

        def _push(u: str) -> None:
            if u and isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
                urls.append(u)

        def _walk(node) -> None:
            if node is None:
                return
            if isinstance(node, list):
                for item in node:
                    _walk(item)
                return
            if not isinstance(node, dict):
                return
            t = node.get("type", "") or ""
            if t == "image":
                _push(node.get("src", "") or node.get("url", ""))
            elif t == "image-group":
                for el in node.get("elements", []) or []:
                    if isinstance(el, dict):
                        _push(el.get("src", "") or el.get("url", ""))
            elif t == "section":
                acc = node.get("accessory")
                if isinstance(acc, dict) and (acc.get("type") == "image"):
                    _push(acc.get("src", "") or acc.get("url", ""))
                # section.text 是 paragraph，其字段里无图片
            elif t == "context":
                for el in node.get("elements", []) or []:
                    if isinstance(el, dict):
                        if el.get("type") == "image":
                            _push(el.get("src", "") or el.get("url", ""))
                        elif isinstance(el.get("src"), str):
                            _push(el["src"])  # KMarkdown 元素没有 type，但有时带 src
            # ══ 统一递归任何含 modules 字段的节点：
            #    - 顶层 card dict（theme/color 等，**无 type 字段**，KOOK 包装图片消息的默认格式）
            #    - container 模块（type="container"，含 modules）
            #    - 其他带 modules 的未知扩展模块
            #    避免只在 container 分支单独 walk 导致漏顶层 / 重复遍历。
            #    纯文本类模块（header/divider/action-group/file/audio/video/countdown/invite）
            #    无 modules 字段，会自然跳过。
            if "modules" in node:
                _walk(node.get("modules", []))

        # 入口：可能是 JSON 字符串（CARD 消息 content）、单个 card dict、或 card list
        if isinstance(card_payload, str):
            s = (card_payload or "").strip()
            if not (s.startswith("[") or s.startswith("{")):
                return []
            try:
                parsed = _json.loads(s)
            except Exception:
                return []
            _walk(parsed)
        else:
            _walk(card_payload)
        return urls

    @staticmethod
    def _extract_quote_info(msg) -> tuple[str, list[str]]:
        """
        从 khl Message.quote 中提取：
          (引用消息正文文字, 引用消息附件里的图片 URL 列表)
        兼容：dict、对象、嵌套 attachments 字段名（attachments / images / 直接带 url 字段）。
        """
        quoted_text = ""
        quoted_images: list[str] = []
        quote = getattr(msg, "quote", None) if hasattr(msg, "quote") else None
        if not quote:
            return quoted_text, quoted_images
        try:
            if isinstance(quote, dict):
                quoted_text = quote.get("content", "") or ""
                for k in ("attachments", "images", "image_list"):
                    if k in quote and quote[k]:
                        quoted_images.extend(EventDispatcher._extract_image_urls(quote[k]))
            else:
                quoted_text = getattr(quote, "content", "") or ""
                for k in ("attachments", "images", "image_list"):
                    v = getattr(quote, k, None)
                    if v:
                        quoted_images.extend(EventDispatcher._extract_image_urls(v))
            # ══ 引用的消息本身可能是 CARD 类型（用户引用一条卡片包装的图片消息），
            #    attachments 可能为空，图片嵌在 quoted_text=卡片 JSON 里
            if quoted_text and isinstance(quoted_text, str) and quoted_text.lstrip().startswith("[{"):
                try:
                    q_card_imgs = EventDispatcher._extract_card_images(quoted_text)
                    if q_card_imgs:
                        quoted_images.extend(q_card_imgs)
                except Exception:
                    pass
        except Exception:
            pass
        # 去重保持顺序
        if quoted_images:
            _seen_q: set[str] = set()
            _uniq: list[str] = []
            for _u in quoted_images:
                if _u and _u not in _seen_q:
                    _seen_q.add(_u)
                    _uniq.append(_u)
            quoted_images = _uniq
        return quoted_text or "", quoted_images

    def _replace_mentions(self, content: str, cfg) -> str:
        """将 KMarkdown (met)id(met) 替换为 @昵称"""
        def _replace_mention(match):
            uid = match.group(1)
            if uid == 'all' or uid == 'here':
                return '@全体 '
            name = cfg.qq_name_map.get(uid, uid)
            return f'@{name} '

        content = re.sub(r'\(met\)(\w+)\(met\)', _replace_mention, content)
        return content

    async def _do_recognize_image(self, image_url, cfg, chat_id, sender_name, mark: str = "") -> str:
        """
        【同步路径用】识别单张图片，返回描述字符串（空串=失败/关闭）。不自己写上下文，调用方决定何时注入。
        mark: 可选标记（如 "#1/3"），用于日志区分多张图。
        """
        if not cfg.image_model.switch:
            return ""
        try:
            from services.image_api import recognize_image
            description = await recognize_image(image_url, cfg.image_model, chat_id=chat_id)
            if description and description.strip():
                short_desc = description[:80].replace("\n", " ")
                logger.info("🖼️  [chat=%s] 图%s 识别完成: '%s...' (%d字) url=%s",
                            chat_id, mark or "", short_desc, len(description),
                            image_url[:60].replace("\n", " "))
                return description.strip()
        except Exception as e:
            logger.warning("🖼️  [chat=%s] 图%s 识别失败 url=%s: %s",
                           chat_id, mark or "",
                           str(image_url)[:80].replace("\n", " "), e)
        return ""

    async def _bg_recognize_image(self, image_url, cfg, chat_id, is_group, user_id, sender_name, is_mentioned):
        """【纯后台路径用】异步识别一张图片，成功后描述注入上下文（纯图片不进管道的兜底记录）。"""
        if not cfg.image_model.switch:
            return
        desc = await self._do_recognize_image(image_url, cfg, chat_id, sender_name, mark="(bg)")
        if desc:
            try:
                from core.context_manager import get_context_mgr
                ctx = get_context_mgr()
                short_desc = desc[:80].replace("\n", " ")
                ctx.append_to_context(chat_id, f'[图片描述]"{short_desc}" {sender_name}')
            except Exception as _e:
                logger.warning("🖼️  [chat=%s] 后台识别写上下文失败: %s", chat_id, _e)


# ── 模块级引用 ──────────────────────────────────────────────
_current_dispatcher: EventDispatcher | None = None


def get_current_dispatcher() -> "EventDispatcher | None":
    return _current_dispatcher
