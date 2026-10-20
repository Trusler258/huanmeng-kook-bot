"""
Phase 13 Plugin API（Huanmeng 2.0）

插件的唯一公共入口。插件只能通过 ctx 访问公开能力，禁止直接 import Core 内部实现、
直接访问数据库或修改内部 Runtime 对象。

暴露的能力（全部可选，按需使用）：
- message   : 发送 / 回复消息（走 Response Delivery）
- memory    : 记忆写入 / 检索（走 Memory Engine，异步不阻塞）
- event     : 订阅 / 发布事件（走 EventBus）
- timer     : 注册周期定时器（reload/unload 自动取消）
- capability: 注册 Command / Skill / Tool 能力（走 CapabilityRegistry）
- config    : 读取本插件 manifest 声明的静态配置
- economy   : 积分余额 / 权益库存读写（唯一锁 + 原子写，杜绝跨插件并发丢更新）
- image     : 图片提取（复用 dispatcher 附件/卡片/表情/引用提取器 + 按引用 ID 拉消息捞图）
- vision    : 图片识别（复用 services.image_api 视觉 LLM 描述）
- identity  : 权限判定（is_admin，走 core.config 全局权限）
- logger    : 本插件命名空间的日志器
- llm       : 文本生成（走 services.llm，默认 reply_model，惰性导入）
- approval  : 人工审批申请 / 回传（走 services.notify_system，惰性导入）
- sandbox   : 沙箱真实执行 py/cpp/shell + 产物收集 + 清理（走 core.sandbox，惰性导入）

约束：
- 不暴露 db、core 内部 Runtime、文件系统、网络、进程执行。
- 高风险权限（网络/文件/进程）由 Phase 17 Permission 统一裁决，本层不直接开放。
- 上述 Adapter 一律惰性 import 内部实现，插件经 ctx.* 调用即与内部实现解耦；
  Core 内部重构时只需保持这些公开方法签名不变，插件无需改动。
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.logger import get_logger
from core.eventbus import EventBus, Event, get_event_bus
from core.capability import (
    Capability, CATEGORY_COMMAND, CATEGORY_TOOL, RUNTIME_COMMAND, RUNTIME_FC,
    get_capability_registry,
)
from core import economy as _economy

logger = get_logger("plugin.api")

# ── 权限门控（v2：manifest.permissions 声明式裁决）────────────────
_HIGH_RISK_LEAVES = ("raw",)   # 通配不得覆盖的高危能力，必须显式声明


def _is_core_tool_name(tool_name: str) -> bool:
    """工具名是否已被核心占用（核心白名单 / 硬拒绝清单 / 风险模型里的已知能力）。

    ★ v2.3.101：插件**未声明**权限时不允许占用这些名字——否则插件静默替换核心
    handler，却继续沿用核心为该名字做的权限判定（沙箱、审批等），等于越权。
    显式声明 permissions 的插件仍可接管（如 sandbox 插件接管 run_code）。
    """
    try:
        from core.tool_runtime.config import ALLOWED_TOOLS, DENIED_TOOLS, TOOL_PERMISSIONS
        if tool_name in ALLOWED_TOOLS or tool_name in DENIED_TOOLS:
            return True
        if tool_name in TOOL_PERMISSIONS:
            return True
    except Exception:
        pass
    try:
        from core.tools import TOOL_SCHEMAS  # type: ignore
        for t in TOOL_SCHEMAS or []:
            if (t.get("function") or {}).get("name") == tool_name:
                return True
    except Exception:
        pass
    return False


def _perm_ok(manifest, perm: str) -> bool:
    """插件只能在 manifest.permissions 声明的能力内行事。

    支持三种写法：
      "message.send"          精确
      "napcat.read"           分组
      "napcat.*" / "*"        通配（前缀通配，如 napcat.* 允许 napcat.read）
    例外：前缀通配**不覆盖高危能力**（raw 必须显式声明），全量 "*" 仍可覆盖。
    manifest 为空（旧插件/未声明）时一律拒绝——安全默认。
    """
    perms = list(getattr(manifest, "permissions", None) or [])
    if not perms:
        return False
    if perm in perms or "*" in perms:
        return True
    leaf = perm.rsplit(".", 1)[-1]
    for pm in perms:
        if not (pm.endswith(".*") and perm.startswith(pm[:-1])):
            continue
        if leaf in _HIGH_RISK_LEAVES:
            continue
        return True
    return False


def _adapt_cmd_handler(handler):
    """把 **QQ 风格**的位置参数 handler 适配成 KOOK 的 (msg) 单参风格。

    背景：两端插件系统的指令 handler 签名不同——
      QQ  : async def h(args, user_id, group_id, sender_name, is_group, bot_qq)
      KOOK: async def h(msg)     # msg 是 _PluginMsg（dict + 属性访问）

    有了这一层，从 QQ 移植过来的插件**无需改一行代码**即可在 KOOK 上跑。
    已是 KOOK 风格（1 个参数）的 handler 原样返回，不包层、零开销。
    """
    try:
        import inspect
        sig = inspect.signature(handler)
        kinds = (inspect.Parameter.POSITIONAL_ONLY,
                 inspect.Parameter.POSITIONAL_OR_KEYWORD,
                 inspect.Parameter.VAR_POSITIONAL)
        pos = [k for k, v in sig.parameters.items()
               if k not in ("self", "cls") and v.kind in kinds]
        if len(pos) <= 1:
            return handler          # 已是 KOOK 风格
    except Exception:
        return handler              # 取不到签名 → 原样返回（保守，不破坏现有插件）

    def _g(msg, key, default=None):
        try:
            return msg.get(key, default)     # _PluginMsg 是 dict 子类
        except Exception:
            return getattr(msg, key, default)

    async def _wrapped(msg):
        try:
            from core.config import get_config
            bot_id = getattr(get_config(), "bot_kook", 0) or 0
        except Exception:
            bot_id = 0
        return await handler(
            _g(msg, "args", []),
            _g(msg, "author", 0),
            _g(msg, "chat_id", 0),
            _g(msg, "sender", ""),
            _g(msg, "is_group", False),
            bot_id,
        )
    return _wrapped


class PluginMessage:
    """消息能力：发送 / 回复。默认走 Response Delivery / Sender。"""

    def __init__(self, plugin_name: str, manifest=None):
        self._plugin = plugin_name
        self._manifest = manifest

    async def send(self, text: str, chat_id: int, is_group: bool = True) -> bool:
        """发送文本消息到频道/私聊。失败返回 False（不抛异常）。"""
        try:
            from services.sender import send_by_chat_type
            await send_by_chat_type(str(text), chat_id, is_group, None if is_group else None)
            return True
        except Exception as e:
            logger.warning("Plugin %s 发送失败: %s", self._plugin, e)
            return False

    async def send_file(self, file_path: str, chat_id: int, is_group: bool = True) -> bool:
        """发送文件（先上传为 asset）。失败返回 False（不抛异常）。"""
        try:
            from services.sender import send_file as _send_file
            return bool(await _send_file(file_path, chat_id, is_group))
        except Exception as e:
            logger.warning("Plugin %s 发送文件失败: %s", self._plugin, e)
            return False

    async def send_ex(self, text: str, chat_id: int, is_group: bool = True) -> Optional[dict]:
        """发送文本并返回响应 dict（KOOK 无 NapCat message_id，返回 {"status":"ok"}）。

        v2 新增：给从 QQ 移植来的插件用（它们期望 dict 返回值）。
        失败 / 未声明 message.send → None。
        """
        if not _perm_ok(self._manifest, "message.send"):
            logger.warning("Plugin %s 未声明 message.send，拒绝发送", self._plugin)
            return None
        try:
            from services.sender import send_group_msg, send_private_msg
            ok = await (send_group_msg(str(text), chat_id) if is_group
                        else send_private_msg(str(text), chat_id))
            return {"status": "ok", "data": {}} if ok else None
        except Exception as e:
            logger.warning("Plugin %s send_ex 失败: %s", self._plugin, e)
            return None

    async def send_file_ex(self, file_path: str, chat_id: int, is_group: bool = True) -> Optional[dict]:
        """发送文件并返回响应 dict（KOOK 无 message_id）。失败 / 未声明 → None。"""
        if not _perm_ok(self._manifest, "message.send"):
            logger.warning("Plugin %s 未声明 message.send，拒绝发送文件", self._plugin)
            return None
        try:
            from services.sender import send_file as _send_file
            ok = bool(await _send_file(file_path, chat_id, is_group))
            return {"status": "ok", "data": {}} if ok else None
        except Exception as e:
            logger.warning("Plugin %s send_file_ex 失败: %s", self._plugin, e)
            return None

    async def delete(self, message_id) -> bool:
        """撤回消息。⚠️ KOOK 平台不提供撤回 API → 记录并返回 False（不抛异常）。

        保留方法签名是为了从 QQ 移植的插件不缺属性；真要撤回需另行实现。
        """
        if not _perm_ok(self._manifest, "message.delete"):
            logger.warning("Plugin %s 未声明 message.delete，拒绝撤回", self._plugin)
            return False
        logger.warning("Plugin %s delete(msg_id=%s)：KOOK 平台不支持撤回，已忽略",
                       self._plugin, message_id)
        return False

    @staticmethod
    def message_id_of(resp: Optional[dict]):
        """从 send_ex/send_file_ex 的响应里取 message_id，取不到返回 None。"""
        if not isinstance(resp, dict):
            return None
        data = resp.get("data")
        if isinstance(data, dict) and data.get("message_id") is not None:
            return data.get("message_id")
        return None


class PluginMemory:
    """记忆能力：异步写入（不阻塞） + 检索。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def remember(self, content: str, memory_type: str = "knowledge",
                       chat_id: int = 0) -> None:
        """写入一条长期记忆（异步，不阻塞响应）。"""
        try:
            from core.memory_engine import get_memory_engine, normalize_memory_type
            engine = get_memory_engine()
            engine.observe(
                chat_id=chat_id, content=content, tag="plugin",
                author=f"plugin:{self._plugin}",
            )
            _ = normalize_memory_type  # keep import referenced
        except Exception as e:
            logger.warning("Plugin %s 记忆写入降级: %s", self._plugin, e)

    async def recall(self, query: str, chat_id: Optional[int] = None, limit: int = 5) -> list:
        """检索记忆，返回结构化列表（DB 不可用返回空）。"""
        try:
            from core.memory_engine import get_memory_engine
            return await get_memory_engine().retrieve_top(
                query, chat_id=chat_id, limit=limit)
        except Exception as e:
            logger.warning("Plugin %s 记忆检索降级: %s", self._plugin, e)
            return []


class PluginEvent:
    """事件能力：订阅 / 发布。reload/unload 时自动清理。"""

    def __init__(self, plugin_name: str, bus: EventBus):
        self._plugin = plugin_name
        self._bus = bus
        self._handlers: list[tuple[str, Callable]] = []

    def on(self, event_name: str):
        """装饰器订阅事件。"""
        def deco(fn):
            self._handlers.append((event_name, fn))
            self._bus.subscribe(event_name, fn)
            return fn
        return deco

    def subscribe(self, event_name: str, handler: Callable) -> None:
        self._handlers.append((event_name, handler))
        self._bus.subscribe(event_name, handler)

    async def publish(self, event_name: str, data: Optional[dict] = None) -> None:
        await self._bus.publish(event_name, data)

    def clear(self) -> None:
        for name, h in self._handlers:
            self._bus.unsubscribe(name, h)
        self._handlers.clear()


class PluginTimer:
    """定时器能力：注册周期任务。reload/unload 自动取消，防止热更新后重复执行。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name
        self._tasks: list[asyncio.Task] = []

    def every(self, seconds: float):
        """装饰器：每 seconds 秒执行一次。"""
        def deco(fn):
            async def _loop():
                while True:
                    try:
                        await asyncio.sleep(seconds)
                        await fn()
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.warning("Plugin %s 定时器任务异常: %s", self._plugin, e)
            task = asyncio.create_task(_loop())
            self._tasks.append(task)
            return fn
        return deco

    def cancel_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


class PluginCapability:
    """能力注册：让插件向 Core 注册 Command/Skill/Tool 能力，供 CapabilityRouter 使用。"""

    def __init__(self, plugin_name: str, manifest=None):
        self._plugin = plugin_name
        self._manifest = manifest
        self._registry = get_capability_registry()
        self._registered: list[str] = []

    def _bind_command(self, name: str, handler: Callable) -> None:
        """把插件指令 handler 绑到核心指令表（v2：让 .<name> 能直接命中插件）。

        与 register_command 的区别：本方法直接操作 COMMAND_MAP，
        用于"插件拥有指令名"的迁移场景（内置指令被插件接管）。
        """
        try:
            from modules.commands import COMMAND_MAP
            COMMAND_MAP[name] = handler
            self._registered.append(f"command.{name}")
        except Exception as e:
            logger.warning("Plugin %s _bind_command(%s) 失败: %s", self._plugin, name, e)

    def register_command(self, name: str, description: str = "",
                         handler: Optional[Callable] = None,
                         permissions: Optional[list[str]] = None) -> None:
        """注册一个命令能力。handler 为 async (msg) -> str|None。"""
        cap = Capability(
            id=f"plugin.{self._plugin}.{name}",
            name=name,
            description=description,
            category=CATEGORY_COMMAND,
            runtime=RUNTIME_COMMAND,
            permissions=permissions or ["message.read", "message.send"],
            source=f"plugin:{self._plugin}",
        )
        self._registry.register(cap)
        if handler is not None:
            # ★ 签名适配：QQ 风格的位置参数 handler 自动包一层转成 KOOK 的 (msg) 风格
            self._registry.bind_handler(cap.id, _adapt_cmd_handler(handler))
        self._registered.append(cap.id)

    def register_tool(self, name: str, description: str = "",
                      schema: Optional[dict] = None,
                      handler: Optional[Callable] = None,
                      permissions: Optional[list[str]] = None,
                      always_on: bool = False) -> None:
        """注册一个 Function Calling 工具能力，供 LLM 对话时自动发现并调用（无需指令）。

        - schema : 完整 OpenAI 工具定义 {"type":"function","function":{...}}。
          缺省时按 name/description 自动生成一个无参 schema。
        - handler: async (arguments: dict, user_id, group_id, sender_name,
                    is_group, bot_kook) -> str | None，返回自然语言结果文本。
          缺省时只注册 schema 不绑定 handler（如仅想暴露给 LLM 语义）。
        - description 里的触发关键词会被 CapabilityRouter 用来精准路由：
          只在用户消息命中相关语义时才把该工具 Schema 交给 LLM。
        - always_on: True 时该工具成为核心常驻能力，普通聊天也会把它的 Schema
          交给 LLM，让模型始终知道插件拥有这项能力（如"能运行代码"）。
        """
        if not schema:
            schema = {"type": "function", "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            }}
        # ★ v2.3.101：权限声明收紧（对齐 tool_runtime 的权限模型）
        #   原则：注册资格 / 能力声明 / 执行授权是三件事，不能因为"注册成功"就自动获得执行权。
        #   ① 声明了 permissions → 必须是 manifest.permissions 的子集，否则拒绝注册
        #   ② 没声明 + 工具名与核心工具重名 → 拒绝注册（否则插件静默替换核心 handler，
        #      却继续继承核心为该工具做的权限判定）
        #   ③ 没声明 + 新工具名 → 放行但告警（迁移期，便于批量补齐）
        owner_perms = list(getattr(self._manifest, "permissions", None) or [])
        declared = permissions is not None and len(permissions) > 0
        if declared:
            need = [str(p) for p in permissions]
            missing = [p for p in need if p not in owner_perms]
            if missing:
                logger.error(
                    "插件 %s 拒绝注册工具 %s：所需权限 %s 不在 manifest.permissions 内（已授权: %s）",
                    self._plugin, name, missing, owner_perms,
                )
                return
        else:
            need = ["message.read", "message.send"]
            if _is_core_tool_name(name):
                logger.error(
                    "插件 %s 拒绝注册工具 %s：未声明 permissions 却占用了核心工具名，"
                    "如需接管必须显式声明所需权限",
                    self._plugin, name,
                )
                return
            logger.warning(
                "插件 %s 注册工具 %s 未声明 permissions（迁移期按 %s 放行，请补声明）",
                self._plugin, name, need,
            )
        cap = Capability(
            id=f"plugin.{self._plugin}.{name}",
            name=name,
            description=description or schema.get("function", {}).get("description", ""),
            category=CATEGORY_TOOL,
            runtime=RUNTIME_FC,
            permissions=need,
            source=f"plugin:{self._plugin}",
            always_on=always_on,
            perm_declared=declared,
            owner_permissions=owner_perms,
        )
        self._registry.register(cap)
        self._registry.bind_tool_schema(cap.id, schema)
        if handler is not None:
            self._registry.bind_handler(cap.id, handler)
        self._registered.append(cap.id)

    def unregister_all(self) -> None:
        for cid in self._registered:
            self._registry.unregister(cid)
        self._registered.clear()


class PluginImage:
    """图片提取能力：从消息/卡片/引用/文本里拿图片 URL。

    提取器全部委托 core.dispatcher.EventDispatcher 的成熟实现
    （_extract_image_urls / _extract_card_images / _extract_emote_urls /
    _extract_quote_info），插件无需自造重复逻辑。
    """

    @staticmethod
    def extract_attachments(attachments) -> list[str]:
        """从 khl 附件列表（list[dict] / list[对象]）抽所有图片 URL。"""
        from core.dispatcher import EventDispatcher
        return EventDispatcher._extract_image_urls(attachments)

    @staticmethod
    def extract_card(payload) -> list[str]:
        """从 KOOK 卡片 JSON（list / dict / JSON 字符串）递归提取所有图片 URL。"""
        from core.dispatcher import EventDispatcher
        return EventDispatcher._extract_card_images(payload)

    @staticmethod
    def extract_emotes(content) -> list[str]:
        """从 KMarkdown 文本提取 KOOK 服务器自定义表情（图片型表情包）URL。"""
        from core.dispatcher import EventDispatcher
        return EventDispatcher._extract_emote_urls(content)

    @staticmethod
    def extract_quote(msg) -> tuple[str, list[str]]:
        """从 khl 消息的 quote 提取 (引用正文, 引用图片 URL 列表)。"""
        from core.dispatcher import EventDispatcher
        return EventDispatcher._extract_quote_info(msg)

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """从文本提取裸 http(s) URL（兼容反引号 / Markdown [text](url) 包裹）。"""
        import re as _re
        return [m.rstrip(")") for m in _re.findall(r"https?://[^\s)\]>\uFF09]+", text or "")]

    @staticmethod
    async def fetch_quote_images(quote_id: str) -> list[str]:
        """按引用消息 ID 拉取被引用消息，返回其中所有图片 URL（无则空列表）。

        KOOK 引用消息事件里 quote 常不带 content/attachments（图在别处），
        此处走 khl MessageAPI.view 按 id 取回完整消息再递归捞图。
        """
        if not quote_id:
            return []
        try:
            from khl import api
            from modules.plugin_share import _walk_strings
            from services.delivery import kook_transport
            bot = kook_transport._bot
            gate = getattr(getattr(bot, "client", None), "gate", None)
            if not gate:
                return []
            body = await gate.exec_req(api.Message.view(msg_id=quote_id))
            candidates: list[str] = []
            _walk_strings(body, out=candidates)
            seen: set[str] = set()
            urls: list[str] = []
            for c in candidates:
                s = str(c)
                low = s.lower()
                if low.startswith(("http://", "https://")) and (
                    "img.kookapp.cn" in low
                    or "img.kaiheila.cn" in low
                    or low.rstrip(")").endswith(
                        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
                ):
                    u = s.rstrip(")")
                    if u not in seen:
                        seen.add(u)
                        urls.append(u)
            return urls
        except Exception as e:
            logger.warning("PluginImage 引用消息取图失败: %s", e)
        return []

    @staticmethod
    async def fetch_user_avatar(user_id: str) -> str:
        """按 KOOK 用户 ID 拉取其头像 URL（失败返回空串）。

        供插件按需使用，如 `.摸头 @人` 用被 @ 用户的头像生成 GIF。
        """
        if not user_id:
            return ""
        try:
            from khl import api
            from services.delivery import kook_transport
            bot = kook_transport._bot
            gate = getattr(getattr(bot, "client", None), "gate", None)
            if not gate:
                return ""
            body = await gate.exec_req(api.User.view(user_id=str(user_id)))
            if isinstance(body, dict):
                return str(body.get("avatar") or "")
        except Exception as e:
            logger.warning("PluginImage 拉取用户头像失败 %s: %s", user_id, e)
        return ""

    async def render_html(self, full_html: str, filename: Optional[str] = None,
                          width: int = 480, height: int = 400,
                          wait_ms: int = 500, wait_selector: Optional[str] = None,
                          timeout_ms: int = 10000) -> Optional[str]:
        """把 HTML/SVG 渲染为 PNG，返回图片绝对路径；失败返回 None。

        ★ 兼容层：QQ 侧的 render_html 在 PluginImage（签名带 filename），
          KOOK 侧实现在 PluginCard（参数叫 output_filename）。此处委托过去，
          让从 QQ 移植的插件 `ctx.image.render_html(html, filename, width=...)`
          无需改代码即可工作。
        """
        try:
            return await PluginCard("image_shim").render_html(
                full_html, width=width, height=height,
                wait_ms=wait_ms, wait_selector=wait_selector,
                timeout_ms=timeout_ms, output_filename=filename)
        except Exception as e:
            logger.warning("PluginImage.render_html 失败: %s", e)
            return None


class PluginVision:
    """图片识别能力：用视觉 LLM 把图片内容描述成文字。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def describe(self, image_url: str, chat_id: int = 0) -> str:
        """识别图片内容，返回文字描述（视觉模型关闭/失败时返回空串）。"""
        try:
            from core.config import get_config
            from services.image_api import recognize_image
            cfg = get_config()
            im = getattr(cfg, "image_model", None)
            if im is None or not getattr(im, "switch", False):
                return ""
            return (await recognize_image(image_url, im, chat_id=chat_id) or "").strip()
        except Exception as e:
            logger.warning("Plugin %s 图片识别失败: %s", self._plugin, e)
        return ""


class PluginIdentity:
    """身份 / 权限判定：is_admin 走 core.config 全局权限。"""

    @staticmethod
    def is_admin(user_id, group_id: int = 0) -> bool:
        try:
            from core.config import get_config
            return bool(get_config().is_admin(user_id, group_id))
        except Exception as e:
            logger.warning("PluginIdentity.is_admin 失败: %s", e)
            return False

    @staticmethod
    def is_op(user_id) -> bool:
        """是否 bot 主人（OP）。"""
        try:
            from core.config import get_config
            return bool(get_config().is_op(user_id))
        except Exception as e:
            logger.warning("PluginIdentity.is_op 失败: %s", e)
            return False


class PluginI18n:
    """i18n 能力：读 config/lang.toml（与内置模块同一份），插件不直接 import utils.format_lang。

    · get("weather.searching")            → lang.toml 里的文案（缺失返回 default 或 [LANG:key]）
    · get("box.header", cp_name="顺丰")   → 同时替换 ${cp_name} 占位符
    · has("weather.searching")            → 判断某 key 是否存在
    """

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    def get(self, key_path: str, default: Optional[str] = None, **fmt) -> str:
        try:
            from utils.format_lang import format_lang
            data = format_lang(key_path)          # 缺失时 format_lang 返回 "[LANG:key]"
            if data.startswith("[LANG:") and default is not None:
                data = default
            for k, v in fmt.items():
                data = data.replace("${" + k + "}", str(v))
            return data
        except Exception as e:
            logger.warning("Plugin %s i18n.get(%s) 失败: %s", self._plugin, key_path, e)
            return default if default is not None else ""

    def has(self, key_path: str) -> bool:
        try:
            from utils.format_lang import has_key
            return bool(has_key(key_path))
        except Exception:
            return False


class PluginLLM:
    """文本生成：走 services.llm.call_llm，默认用机器人回复主模型reply_model。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def generate(self, messages: list[dict], temperature: float = 0.3,
                       timeout: float = 60.0) -> str:
        """生成文本。messages 为 [{role,content},...]，缺省模型=reply_model，返回原始文本。"""
        try:
            from core.config import get_config
            from services.llm import call_llm
            model_cfg = getattr(get_config(), "reply_model", None)
            if model_cfg is None:
                logger.warning("Plugin %s llm.generate: 未配置 reply_model", self._plugin)
                return ""
            return (await call_llm(model_cfg, messages,
                                   temperature=temperature, timeout=timeout) or "").strip()
        except Exception as e:
            logger.warning("Plugin %s llm.generate 失败: %s", self._plugin, e)
            return ""


class PluginApproval:
    """人工审批：申请/回传走 services.notify_system（内部实现可替换）。"""

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def request(self, plan_desc: str, timeout: float = 120.0) -> bool:
        """发起审批卡片，等待管理员确认；放行返回 True，取消/超时返回 False。"""
        try:
            from services.notify_system import request_run_approval
            return bool(await request_run_approval(plan_desc, timeout=timeout))
        except Exception as e:
            logger.warning("Plugin %s approval.request 失败: %s", self._plugin, e)
            return False

    def resolve(self, token: str, approved: bool) -> str:
        """回传审批结果（按钮回调用）。"""
        from services.notify_system import resolve_run_approval
        return resolve_run_approval(token, approved)


class PluginSandbox:
    """沙箱真实执行：py/cpp/shell + 产物收集 + 清理，走 core.sandbox（惰性导入）。"""

    @staticmethod
    async def run_python(code: str, **kwargs) -> dict:
        from core import sandbox
        return await sandbox.run_python(code, **kwargs)

    @staticmethod
    async def run_cpp(files: dict, **kwargs) -> dict:
        from core import sandbox
        return await sandbox.compile_and_run_cpp(files, **kwargs)

    @staticmethod
    async def run_shell(command: str, **kwargs) -> dict:
        from core import sandbox
        return await sandbox.run_shell(command, **kwargs)

    @staticmethod
    def collect_artifacts(tmp_dir) -> list:
        from core import sandbox
        return sandbox.collect_artifacts(tmp_dir)

    @staticmethod
    def cleanup(tmp_dir) -> None:
        from core import sandbox
        sandbox.cleanup(tmp_dir)


class PluginCard:
    """卡片渲染能力：把 HTML 模板渲染成图片（惰性依赖核心渲染基础设施）。

    渲染基础设施（Playwright 全局浏览器 + 模板引擎）留在核心共享，
    插件只通过此能力生成卡片图片，再自行用 ctx.message.send_file 发送。
    """

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    async def render_weather_card(self, data: dict, output_filename: Optional[str] = None) -> Optional[str]:
        """生成天气卡片图片，返回图片绝对路径；失败返回 None（调用方回退纯文本）。"""
        try:
            from modules.changelog import generate_weather_card
            return await generate_weather_card(data, output_filename=output_filename)
        except Exception as e:
            logger.warning("Plugin %s 天气卡片渲染失败: %s", self._plugin, e)
            return None

    async def render_html(self, html: str, width: int = 480, height: int = 400,
                          wait_ms: int = 500, wait_selector: Optional[str] = None,
                          timeout_ms: int = 10000,
                          output_filename: Optional[str] = None) -> Optional[str]:
        """把 HTML/SVG 渲染为 PNG 图片，返回图片绝对路径；失败返回 None。

        Args:
            html: HTML 或 SVG 内容（页面 set_content）
            width / height: 视口尺寸
            wait_ms: 渲染稳定等待毫秒数
            wait_selector: 可选，等待指定选择器出现（如 Leaflet 瓦片）
            timeout_ms: set_content 超时
            output_filename: 可选输出文件名
        """
        try:
            from modules.changelog import _ensure_browser
            import tempfile, os as _os, asyncio as _asyncio

            browser = await _ensure_browser()
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.set_content(html, wait_until="load", timeout=timeout_ms)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=min(timeout_ms, 6000))
                except Exception:
                    pass
            if wait_ms:
                await _asyncio.sleep(wait_ms / 1000.0)
            fd, png_path = tempfile.mkstemp(suffix=".png")
            _os.close(fd)
            await page.screenshot(path=png_path, full_page=True)
            await page.close()
            return png_path
        except Exception as e:
            logger.warning("Plugin %s HTML 渲染失败: %s", self._plugin, e)
            return None


class PluginPipeline:
    """消息管道钩子：允许插件在消息处理流程中插入逻辑。

    Hook 类型：
    - on_message: 收到消息后、管道处理前调用。返回 str 可跳过后续管道直接回复。
    - on_reply: 管道生成回复后、发送前调用。可修改/替换/拦截回复。
    """

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name
        self._pre_hooks: list[Callable] = []
        self._post_hooks: list[Callable] = []
        _get_hook_registry()._register_plugin(plugin_name, self)

    def on_message(self, fn: Callable):
        """注册消息预处理钩子。fn(msg_dict) -> str|None。
        返回非 None 字符串则跳过管道直接作为回复发送。"""
        self._pre_hooks.append(fn)
        return fn

    def on_reply(self, fn: Callable):
        """注册回复后处理钩子。fn(reply_text, msg_dict) -> str|None。
        返回 None 保持原回复；返回空字符串拦截不发送；返回其他字符串替换原回复。"""
        self._post_hooks.append(fn)
        return fn

    def get_pre_hooks(self) -> list:
        return list(self._pre_hooks)

    def get_post_hooks(self) -> list:
        return list(self._post_hooks)

    def clear(self) -> None:
        self._pre_hooks.clear()
        self._post_hooks.clear()
        _get_hook_registry()._unregister_plugin(self._plugin)


class PluginBackground:
    """后台任务注册：允许插件注册长时间运行的后台协程。

    用法：在 on_enable 中调用 ctx.background.add(my_coro())，
    在 on_disable/on_unload 时自动取消所有已注册任务。
    """

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name
        self._tasks: list[asyncio.Task] = []

    def add(self, coro) -> asyncio.Task:
        """注册一个后台协程，返回 asyncio.Task。插件卸载时自动取消。"""
        async def _wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("Plugin %s 后台任务异常: %s", self._plugin, e)
        task = asyncio.create_task(_wrapper())
        self._tasks.append(task)
        return task

    def cancel_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


class _HookRegistry:
    """全局管道钩子注册表（单例）。pipeline.py 从此查询所有插件的钩子。"""

    def __init__(self):
        self._plugins: dict[str, "PluginPipeline"] = {}

    def _register_plugin(self, name: str, pipeline: "PluginPipeline") -> None:
        self._plugins[name] = pipeline

    def _unregister_plugin(self, name: str) -> None:
        self._plugins.pop(name, None)

    def all_pre_hooks(self) -> list[Callable]:
        hooks: list[Callable] = []
        for p in self._plugins.values():
            hooks.extend(p.get_pre_hooks())
        return hooks

    def all_post_hooks(self) -> list[Callable]:
        hooks: list[Callable] = []
        for p in self._plugins.values():
            hooks.extend(p.get_post_hooks())
        return hooks


_hook_registry: Optional[_HookRegistry] = None


def _get_hook_registry() -> _HookRegistry:
    global _hook_registry
    if _hook_registry is None:
        _hook_registry = _HookRegistry()
    return _hook_registry


def get_pipeline_hooks() -> _HookRegistry:
    """供 pipeline.py 获取全局钩子注册表。"""
    return _get_hook_registry()


class PluginFS:
    """受限文件系统：插件只能读写自己的数据目录 data/plugins/<插件名>/。

    · 路径越界（../、绝对路径指向根外、符号链接逃逸）一律拒绝
    · 写类操作（write/append/remove/mkdir）需要 manifest 声明 fs.write
    · 读类操作（read/list/exists/stat）需要声明 fs.read
    这是"Python 侧沙箱"的第一步：不做进程隔离，但把可触达范围收敛到插件自己的目录。
    """

    def __init__(self, plugin_name: str, manifest=None, root: Optional[str] = None):
        self._plugin = plugin_name
        self._manifest = manifest
        self._root = Path(root) if root else (Path("data") / "plugins" / plugin_name)

    @property
    def root(self) -> Path:
        return self._root.resolve()

    def path(self, rel: str = "") -> Path:
        """把插件给的相对路径解析到沙箱内；越界抛 ValueError。"""
        base = self.root
        p = (base / str(rel).lstrip("/\\")).resolve() if rel else base
        if p != base and base not in p.parents:
            raise ValueError(f"路径越界（仅允许插件数据目录内）：{rel}")
        return p

    def _need(self, perm: str) -> bool:
        if not _perm_ok(self._manifest, perm):
            logger.warning("Plugin %s 未声明 %s，拒绝文件操作", self._plugin, perm)
            return False
        return True

    def read_text(self, rel: str, encoding: str = "utf-8") -> Optional[str]:
        if not self._need("fs.read"):
            return None
        try:
            return self.path(rel).read_text(encoding=encoding)
        except Exception as e:
            logger.warning("Plugin %s fs.read_text(%s) 失败: %s", self._plugin, rel, e)
            return None

    def read_bytes(self, rel: str) -> Optional[bytes]:
        if not self._need("fs.read"):
            return None
        try:
            return self.path(rel).read_bytes()
        except Exception as e:
            logger.warning("Plugin %s fs.read_bytes(%s) 失败: %s", self._plugin, rel, e)
            return None

    def write_text(self, rel: str, data: str, encoding: str = "utf-8") -> bool:
        if not self._need("fs.write"):
            return False
        try:
            p = self.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data, encoding=encoding)
            return True
        except Exception as e:
            logger.warning("Plugin %s fs.write_text(%s) 失败: %s", self._plugin, rel, e)
            return False

    def write_bytes(self, rel: str, data: bytes) -> bool:
        if not self._need("fs.write"):
            return False
        try:
            p = self.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return True
        except Exception as e:
            logger.warning("Plugin %s fs.write_bytes(%s) 失败: %s", self._plugin, rel, e)
            return False

    def listdir(self, rel: str = "") -> list[str]:
        if not self._need("fs.read"):
            return []
        try:
            return sorted(p.name for p in self.path(rel).iterdir())
        except Exception as e:
            logger.warning("Plugin %s fs.listdir(%s) 失败: %s", self._plugin, rel, e)
            return []

    def exists(self, rel: str) -> bool:
        if not self._need("fs.read"):
            return False
        try:
            return self.path(rel).exists()
        except Exception:
            return False

    def remove(self, rel: str) -> bool:
        if not self._need("fs.write"):
            return False
        try:
            p = self.path(rel)
            if p.is_dir():
                import shutil as _sh
                _sh.rmtree(p)
            elif p.exists():
                p.unlink()
            return True
        except Exception as e:
            logger.warning("Plugin %s fs.remove(%s) 失败: %s", self._plugin, rel, e)
            return False


class PluginPlugins:
    """插件间协作：查询其它插件的加载状态。

    用于软依赖（manifest.requires.optional_plugins）：本插件可先
    `ctx.plugins.has("points")` 判断邻居在不在，再决定走哪条分支，
    而不是直接 import 对方代码（那会破坏"可拆"）。
    """

    def __init__(self, plugin_name: str):
        self._plugin = plugin_name

    def loaded(self) -> list[str]:
        try:
            from core.plugin.manager import get_plugin_manager
            return [r["name"] for r in get_plugin_manager().list()
                    if r.get("state") == "enabled"]
        except Exception as e:
            logger.warning("Plugin %s ctx.plugins.loaded 失败: %s", self._plugin, e)
            return []

    def has(self, name: str) -> bool:
        return str(name) in self.loaded()


class PluginContext:
    """插件上下文：插件唯一的 API 入口。"""

    def __init__(self, plugin_name: str, manifest, bus: Optional[EventBus] = None):
        self.name = plugin_name
        self.manifest = manifest
        self.bus = bus or get_event_bus()
        self.message = PluginMessage(plugin_name, manifest)
        self.memory = PluginMemory(plugin_name)
        self.event = PluginEvent(plugin_name, self.bus)
        self.timer = PluginTimer(plugin_name)
        self.capability = PluginCapability(plugin_name, manifest)
        self.pipeline = PluginPipeline(plugin_name)
        self.background = PluginBackground(plugin_name)
        self.image = PluginImage()
        self.vision = PluginVision(plugin_name)
        self.identity = PluginIdentity()
        self.llm = PluginLLM(plugin_name)
        self.i18n = PluginI18n(plugin_name)
        self.fs = PluginFS(plugin_name, manifest)
        self.plugins = PluginPlugins(plugin_name)
        self.approval = PluginApproval(plugin_name)
        self.sandbox = PluginSandbox()
        self.card = PluginCard(plugin_name)
        self.logger = get_logger(plugin_name)

    @property
    def economy(self):
        """经济系统（积分/库存）：惰性导入 core.economy，取不到返回 no-op stub。

        与 QQ 一致的做法：内置经济已迁移为插件（points/shop），core.economy
        可能不存在 → 返回空模块让旧插件不崩溃。
        """
        try:
            from core import economy as _m
            return _m
        except ImportError:
            _empty = types.ModuleType("economy_stub")
            def _noop(*a, **k):
                return None
            for fn in ("get_points", "add_points", "set_points", "transfer_points",
                       "get_inventory", "add_inventory", "consume_inventory",
                       "get_last_sign", "mark_signed", "get_top_points"):
                setattr(_empty, fn, _noop)
            _empty.ITEMS = {}
            return _empty

    def has(self, perm: str) -> bool:
        """查询本插件是否具备某项能力（依据 manifest.permissions）。

        例：ctx.has("message.delete") / ctx.has("fs.write") / ctx.has("network.http")
        """
        return _perm_ok(self.manifest, perm)

    def config(self, key: str, default: Any = None) -> Any:
        """读取本插件 manifest.config 里的静态配置。"""
        return self.manifest.config.get(key, default)

    def cleanup(self) -> None:
        """卸载时清理：事件订阅 + 定时器 + 能力注册 + 管道钩子 + 后台任务，防止热更新后重复执行。"""
        self.event.clear()
        self.timer.cancel_all()
        self.capability.unregister_all()
        self.pipeline.clear()
        self.background.cancel_all()