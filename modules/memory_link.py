"""
跨聊天记忆关联（v2.3.11）

【核心原则】共享相关记忆，但**不合并聊天身份与聊天历史**。
两个人/两个会话各自保持独立边界，只是允许对方"看到"最近的交流内容，
并且 LLM 必须清楚每条记忆来自哪个会话。

指令：
    /~mlink add <目标>      建立关联（目标见下）
    /~mlink del [目标]      断开指定关联；不带目标 = 断开全部（任何一方都可断开）
    /~mlink list            列出当前聊天已关联的会话
    /~mlink yes / no        同意 / 拒绝"别人请求关联我的私聊"
    （统一入口 cmd_mlink；本文件的 cmd_memory_link/unlink/agree/deny 是它的子实现）

目标写法：
    p<QQ号>   私聊（如 p10001）
    g<群号>   群聊（如 g10001）
    <纯数字>  在群里当群号、在私聊里当对方 QQ 号（按常识默认）

权限规则：
    1. 关联**自己的私聊**（p + 自己的 QQ）→ 直接建立，无需他人同意
       —— 这是最常见场景：在群里说过的事，私聊也记得，反之亦然
    2. 关联**别人的私聊** → 必须对方同意：bot 私聊对方请求确认，
       对方回 `/~记忆同意` 才建立；拒绝则 → 不建立，且**不泄露任何内容**
    3. 关联**群聊** → 需要管理员权限（群记忆涉及他人隐私，不能谁都能拉）

数据：data/memory_links.json
    {"links": [["g10001", "p10001"]], "pending": {"p10001": {"from": "g10002", "at": "..."}}}
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from core.logger import get_logger

logger = get_logger("mlink")

LINK_FILE = Path(__file__).resolve().parent.parent / "data" / "memory_links.json"

PER_CHAT_LINES = 12      # 每个关联会话注入多少条近期记录
MAX_LINKS = 8            # 单个会话最多关联几个（防注入膨胀）


# ── 存储 ─────────────────────────────────────────────────────────

def _load() -> dict:
    if not LINK_FILE.exists():
        return {"links": [], "pending": {}}
    try:
        with open(LINK_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"links": [], "pending": {}}
        d.setdefault("links", [])
        d.setdefault("pending", {})
        return d
    except Exception as e:
        logger.error("读取关联表失败: %s", e)
        return {"links": [], "pending": {}}


def _save(d: dict) -> None:
    LINK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LINK_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LINK_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── key 工具 ─────────────────────────────────────────────────────

def chat_key(chat_id: int, is_group: bool) -> str:
    """会话标识：群 `g<群号>` / 私聊 `p<QQ>`（与 fav 的 key 风格一致）"""
    return f"g{chat_id}" if is_group else f"p{chat_id}"


def key_to_chat_id(key: str) -> int | None:
    try:
        return int(str(key).lstrip("gp"))
    except Exception:
        return None


def key_label(key: str) -> str:
    """给人看的来源名：群聊 123 / 私聊 456"""
    k = str(key)
    if k.startswith("g"):
        return f"群聊 {k[1:]}"
    if k.startswith("p"):
        return f"私聊 {k[1:]}"
    return k


def _pair(a: str, b: str) -> list[str]:
    """无序对的规范形式（去重）"""
    return sorted([str(a), str(b)])


def parse_target(arg: str, current_is_group: bool) -> tuple[str | None, str]:
    """解析目标参数 → (key, 错误说明)

    支持：p123 / g123 / 纯数字（群里=群号、私聊里=对方QQ）
    """
    s = (arg or "").strip().lower().replace("＃", "").lstrip("#")
    if not s:
        return None, "没写目标喵～例：/~mlink add p10001（私聊）或 g10001（群聊）"
    if s[0] == "p":
        num = s[1:].strip()
        if not num.isdigit():
            return None, "p 后面要跟 QQ 号喵，例：p10001"
        return f"p{num}", ""
    if s[0] == "g":
        num = s[1:].strip()
        if not num.isdigit():
            return None, "g 后面要跟群号喵，例：g10001"
        return f"g{num}", ""
    if s.isdigit():
        # 纯数字：群里当群号，私聊里当对方 QQ
        return (f"g{s}" if current_is_group else f"p{s}"), ""
    return None, "只认 p<QQ号> / g<群号> / 纯数字 这三种写法喵"


def links_of(key: str) -> list[str]:
    d = _load()
    out = []
    for a, b in d.get("links", []):
        if a == key:
            out.append(b)
        elif b == key:
            out.append(a)
    return out


def is_linked(a: str, b: str) -> bool:
    return b in links_of(a)


def add_link(a: str, b: str) -> bool:
    if a == b:
        return False
    d = _load()
    p = _pair(a, b)
    if p in d["links"]:
        return False
    if len(links_of(a)) >= MAX_LINKS or len(links_of(b)) >= MAX_LINKS:
        return False
    d["links"].append(p)
    _save(d)
    return True


def remove_link(a: str, b: str) -> bool:
    d = _load()
    p = _pair(a, b)
    if p not in d["links"]:
        return False
    d["links"].remove(p)
    _save(d)
    return True


# ── 待确认（别人请求关联我的私聊）───────────────────────────────

def set_pending(target_key: str, from_key: str) -> None:
    d = _load()
    d.setdefault("pending", {})[target_key] = {
        "from": from_key,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ts": int(time.time()),
    }
    _save(d)


def get_pending(my_key: str) -> dict | None:
    return _load().get("pending", {}).get(my_key)


def clear_pending(my_key: str) -> dict | None:
    d = _load()
    p = d.get("pending", {}).pop(my_key, None)
    _save(d)
    return p


# ── 注入：把关联会话的近期内容带来源标识拼出来 ───────────────────

def build_linked_context(current_key: str, ctx_mgr, per_chat: int = PER_CHAT_LINES) -> str:
    """生成注入给 LLM 的跨聊天记忆文本（自带来源标签与会话边界说明）

    v2.3.12 隐私加强（用户要求）：
      · **群聊来源只注入我自己（bot）的发言** —— 群成员的原话一律不外传，
        避免"某人在群里说的话"被搬进别人的私聊；话题脉络靠长期记忆摘要传递
      · 每个来源**额外附带长期记忆摘要**（data/memory_<chat_id>.md），
        让跨会话可用的是"长期沉淀 + 我自己说过的话"，而不只是最近几句
    """
    linked = links_of(current_key)
    if not linked:
        return ""

    try:
        from core.config import get_config
        bot_name = get_config().bot_name or ""
    except Exception:
        bot_name = ""

    blocks = []
    for key in linked:
        cid = key_to_chat_id(key)
        if cid is None:
            continue
        is_group_src = str(key).startswith("g")

        # ① 近期会话历史
        raw: list = []
        try:
            raw = list(ctx_mgr.get_context(cid))[-per_chat:]
        except Exception:
            raw = []

        if raw:
            if is_group_src and bot_name:
                kept = [x for x in raw
                        if str(x).startswith(f"{bot_name}:") or str(x).startswith(f"{bot_name}：")]
                if kept:
                    body = "\n".join(str(x)[:200] for x in kept[-per_chat:])
                    blocks.append(f"—— 来源：{key_label(key)}（仅我自己的发言，群成员原话不外传）——\n{body}")
            else:
                body = "\n".join(str(x)[:200] for x in raw)
                blocks.append(f"—— 来源：{key_label(key)}（最近 {len(raw)} 条）——\n{body}")

        # ② 长期记忆摘要（两个来源都带；本身是压缩概述，不含他人原话）
        try:
            from modules.memory import read_long_memory
            lm = (read_long_memory(cid, limit=8) or "").strip()
            # 跳过空占位（如"暂无长期记忆"），避免往上下文塞废话
            if lm and "暂无" not in lm and len(lm) > 12:
                blocks.append(f"—— 来源：{key_label(key)} · 长期记忆摘要 ——\n{lm[:600]}")
        except Exception:
            pass

    if not blocks:
        return ""
    return (
        "【跨聊天记忆 · 来自其他会话（不是当前这条对话）】\n"
        "下面是**其他聊天里**的近期交流与长期记忆，供你参考。铁律：\n"
        "1. 这些内容**不属于当前会话**——不要当成当前对话里发生过的事，不要把两边的人混成一个人；\n"
        "2. 每条都标了来源（群聊/私聊 + 号），需要时按来源区分着用；\n"
        "3. 不要主动复述「你上次在群里/私聊说过…」，除非对方先提起——自然用上就行；\n"
        "4. 私聊里的内容不要在群里传述，反之也一样；**群聊来源只给了我自己说过的话**，\n"
        "   群成员的发言一概不知道、也不要臆测，别提「群里某某说过」。\n\n"
        + "\n\n".join(blocks)
    )


# ── 指令实现 ─────────────────────────────────────────────────────

async def cmd_mlink(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """跨聊天记忆关联 —— **单一入口**（v2.3.12 整合，英文指令）

    用法:
      /~mlink add <目标>    建立关联
      /~mlink del [目标]    断开指定关联；不带目标 = 断开全部
      /~mlink list          查看已关联的会话
      /~mlink yes / no      同意 / 拒绝别人的关联请求（私聊）
      /~mlink <目标>        省略子命令时等同于 add
    目标写法: p<QQ号> 私聊 / g<群号> 群聊 / 纯数字（群里=群号、私聊=对方QQ）
    """
    first = (args[0].lower() if args else "")

    # 同意 / 拒绝
    if first in ("yes", "agree", "ok", "y", "同意"):
        return await cmd_memory_agree([], user_id, group_id, sender_name, is_group, bot_qq)
    if first in ("no", "deny", "reject", "refuse", "n", "拒绝"):
        return await cmd_memory_deny([], user_id, group_id, sender_name, is_group, bot_qq)

    # 断开
    if first in ("del", "delete", "remove", "rm", "unlink", "-", "断", "断开", "解除"):
        return await cmd_memory_unlink(args[1:], user_id, group_id, sender_name, is_group, bot_qq)

    # 列表 / 无参（无参时展示用法 + 当前关联）
    if first in ("list", "ls", "all", "列表", "查看", ""):
        out = await cmd_memory_link(["list"], user_id, group_id, sender_name, is_group, bot_qq)
        if not args:
            out += ("\n\n用法：\n"
                    "/~mlink add <目标> — 建立关联\n"
                    "/~mlink del [目标] — 断开（不带目标=全部断开）\n"
                    "/~mlink yes | no — 同意/拒绝别人的请求\n"
                    "目标：p<QQ号> 私聊 · g<群号> 群聊 · 纯数字")
        return out

    # add（显式或省略子命令直接给目标）
    target_args = args[1:] if first in ("add", "加", "+") else args
    if not target_args:
        return "要关联谁呀？例：/~mlink add p10001（私聊）或 g10001（群聊）"
    return await cmd_memory_link(target_args, user_id, group_id, sender_name, is_group, bot_qq)


async def cmd_memory_link(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """跨聊天记忆关联

    用法:
      （由 /~mlink 入口调用）
      /~mlink add <目标>      建立关联（p<QQ> 私聊 / g<群号> 群聊 / 纯数字）
      /~mlink list            查看已关联的会话
      /~mlink del <目标>      断开关联
    """
    from core.config import get_config
    cfg = get_config()

    my_key = chat_key(group_id if is_group else user_id, is_group)
    sub = (args[0].lower() if args else "")

    # 无参 → 用法
    if not sub:
        return ("用法喵：\n"
                "/~mlink add p<QQ号> — 关联某个私聊\n"
                "/~mlink add g<群号> — 关联某个群聊（需管理员）\n"
                "/~mlink add <纯数字> — 群里当群号、私聊里当对方 QQ\n"
                "/~mlink list — 看已关联的会话\n"
                "/~mlink del <目标> — 断开关联\n"
                "／ 关联自己的私聊直接建立；关联别人的私聊要对方回 /~mlink yes")

    if sub in ("list", "列表", "查看"):
        ks = links_of(my_key)
        if not ks:
            return "当前聊天还没有关联任何会话喵～发 /~mlink add p<QQ号> 试试"
        lines = [f"【记忆关联】当前会话（{key_label(my_key)}）已关联 {len(ks)} 个："]
        for i, k in enumerate(ks, 1):
            cid = key_to_chat_id(k)
            n = len(get_context_mgr_safe().get_context(cid)) if cid else 0
            lines.append(f"{i}. {key_label(k)}（{n} 条记录）")
        lines.append("解除：/~mlink del <目标>（不带目标 = 全部断开）")
        return "\n".join(lines)

    if sub in ("del", "delete", "取消", "解除", "remove"):
        if len(args) < 2:
            return "要指明断开哪个喵：/~mlink del p<QQ号>"
        target, err = parse_target(args[1], is_group)
        if not target:
            return err
        if remove_link(my_key, target):
            logger.info("解除记忆关联: %s ↔ %s", my_key, target)
            return f"已解除与 {key_label(target)} 的记忆关联喵"
        return f"没有和 {key_label(target)} 建立过关联哦"

    # 建立关联
    target, err = parse_target(sub, is_group)
    if not target:
        return err
    if target == my_key:
        return "这就是当前聊天呀，不用关联自己喵"

    # 权限判定
    if target.startswith("p"):
        target_qq = key_to_chat_id(target)
        if target_qq == user_id:
            # 自己的私聊 → 直接建立（最常见：群 ↔ 自己的私聊）
            if add_link(my_key, target):
                logger.info("记忆关联建立(自己): %s ↔ %s by=%s", my_key, target, user_id)
                return (f"已把当前聊天和你的私聊关联起来啦～\n"
                        f"以后在{key_label(target)}里说过的事，我在这边也会记得（不会混成同一场对话）")
            return "已经关联过啦，或者关联数到上限了喵（最多 %d 个）" % MAX_LINKS

        # 别人的私聊 → 需要对方同意
        if is_linked(my_key, target):
            return f"已经和 {key_label(target)} 关联着喵"
        if get_pending(target):
            return f"已经给 {key_label(target)} 发过请求了喵，等对方回 /~mlink yes"
        set_pending(target, my_key)
        # 私聊对方请求确认
        try:
            from services.sender import send_private_msg
            ok = await send_private_msg(
                f"有人想和你共享记忆喵～\n"
                f"来源：{key_label(my_key)}\n"
                f"同意后，我在那边的交流内容（最近几条）可以被这边参考，"
                f"但两边仍是独立的对话，不会合并。\n"
                f"同意就回：/~记忆同意\n"
                f"不想的话回：/~记忆拒绝（我不会告诉对方你拒绝了）",
                target_qq,
            )
            if not ok:
                clear_pending(target)
                return "给对方发确认消息失败了喵，稍后再试吧"
        except Exception as e:
            clear_pending(target)
            logger.warning("发送关联确认失败: %s", e)
            return "给对方发确认消息失败了喵，稍后再试吧"
        logger.info("记忆关联请求已发出: %s → %s by=%s", my_key, target, user_id)
        return (f"已经私聊 {key_label(target)} 征求同意啦～\n"
                f"对方回 /~记忆同意 之后就会生效（拒绝也不会告诉你，保护对方隐私）")

    # 群聊目标 → 需管理员
    if not cfg.is_admin(user_id, group_id):
        return "关联群聊记忆要管理员权限喵（群里的记忆涉及其他人隐私）"
    if add_link(my_key, target):
        logger.info("记忆关联建立(群): %s ↔ %s by=%s", my_key, target, user_id)
        return f"已把当前聊天和 {key_label(target)} 关联起来啦～两边的内容会带来源标识互相参考"
    return "已经关联过啦，或者关联数到上限了喵（最多 %d 个）" % MAX_LINKS


async def cmd_memory_unlink(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """断开关联（v2.3.12）

    用法:
      /~断开关联             断开当前聊天的**全部**记忆关联
      /~断开关联 <目标>       断开与指定会话的关联（p<QQ> / g<群号> / 纯数字）

    权限：关联的**任何一方**都可以随时断开（收回自己的共享），无需对方同意；
    断开后两边互不可见，且**不通知对方**（与拒绝请求同样处理，保护隐私）。
    """
    my_key = chat_key(group_id if is_group else user_id, is_group)
    sub = (args[0].lower() if args else "")

    # 无参 / all → 全部断开
    if not sub or sub in ("all", "全部", "所有", "clear", "*"):
        ks = links_of(my_key)
        if not ks:
            return "当前聊天没有关联任何会话喵"
        for k in ks:
            remove_link(my_key, k)
        logger.info("断开全部记忆关联: %s（%d 个）", my_key, len(ks))
        lines = [f"已断开 {len(ks)} 个记忆关联喵："]
        lines += [f"· {key_label(k)}" for k in ks]
        lines.append("（不通知对方，之后两边互不可见）")
        return "\n".join(lines)

    target, err = parse_target(sub, is_group)
    if not target:
        return err
    if remove_link(my_key, target):
        logger.info("解除记忆关联: %s ↔ %s by=%s", my_key, target, user_id)
        return f"已断开与 {key_label(target)} 的记忆关联喵（不通知对方）"
    return f"当前聊天没有和 {key_label(target)} 建立过关联哦"


async def cmd_memory_agree(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """同意别人发来的记忆关联请求（仅私聊有效）"""
    if is_group:
        return "这个是私聊里用的喵～"
    my_key = chat_key(user_id, False)
    p = get_pending(my_key)
    if not p:
        return "现在没有待确认的记忆关联请求喵"
    from_key = p.get("from", "")
    clear_pending(my_key)
    if add_link(my_key, from_key):
        logger.info("记忆关联经同意建立: %s ↔ %s", my_key, from_key)
        return f"好～已经同意和 {key_label(from_key)} 共享记忆啦"
    return "关联失败喵（可能已达上限），或者你们本来就关联着"


async def cmd_memory_deny(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """拒绝记忆关联请求（仅私聊有效，不通知请求方）"""
    if is_group:
        return "这个是私聊里用的喵～"
    my_key = chat_key(user_id, False)
    if not get_pending(my_key):
        return "现在没有待确认的记忆关联请求喵"
    clear_pending(my_key)
    logger.info("记忆关联被拒绝: %s", my_key)
    return "好的，已经拒绝啦，我不会告诉对方，也不会共享任何内容"


def get_context_mgr_safe():
    from core.context_manager import get_context_mgr
    return get_context_mgr()
