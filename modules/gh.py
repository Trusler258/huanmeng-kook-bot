"""
公会登记模块 .gh
- 存储: data/gh.json (群→{opts:[], members:{QQ→{name: 公会名, nick: 昵称}}})
- 指令: add / del / list / fix / new / op / help
- .gh op <qq>  授权他人使用 new 指令
- .gh new <qq> <公会名>  帮别人登记（需 op 权限）
"""

from __future__ import annotations
import json
from pathlib import Path

from core.logger import get_logger

logger = get_logger("guild")


def _load(gh_path: Path) -> dict:
    if gh_path.exists():
        try:
            data = json.loads(gh_path.read_text(encoding="utf-8"))
            # 旧格式兼容: members 曾直接挂在 group 下，迁移到 members 字段
            for gid, gd in list(data.items()):
                if isinstance(gd, dict) and "members" not in gd:
                    members = {}
                    opts = []
                    for k, v in list(gd.items()):
                        if k == "opts":
                            opts = v if isinstance(v, list) else []
                        elif isinstance(v, (dict, str)):
                            members[k] = v
                    data[gid] = {"opts": opts, "members": members}
            return data
        except Exception:
            return {}
    return {}


def _save(gh_path: Path, data: dict):
    gh_path.parent.mkdir(parents=True, exist_ok=True)
    gh_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _gkey(group_id: int) -> str:
    return str(group_id)


async def _resolve_nick(qq: int) -> str:
    """通过 khl.py 获取 KOOK 用户昵称"""
    try:
        from utils.username import get_or_resolve_username
        return await get_or_resolve_username(qq)
    except Exception:
        pass
    return str(qq)


def _is_admin(user_id: int) -> bool:
    """检查是否是 bot 管理员（主人）"""
    from core.config import get_config
    return user_id == get_config().admin_qq


def _can_op(user_id: int, group_data: dict) -> bool:
    """检查是否有 op/new 权限"""
    return _is_admin(user_id) or str(user_id) in group_data.get("opts", [])


async def cmd_gh(args, user_id, group_id, sender_name, is_group, bot_qq):
    """公会登记 .gh [add|del|list|fix|new|op]"""
    if not is_group:
        return "公会登记仅限群聊使用喵~"

    gh_path = Path(__file__).resolve().parent.parent / "data" / "gh.json"
    data = _load(gh_path)
    gid = _gkey(group_id)
    group_data = data.setdefault(gid, {"opts": [], "members": {}})
    members = group_data.setdefault("members", {})

    if not args or (len(args) == 1 and args[0].lower() in ("help",)):
        lines = [
            "【公会登记 .gh】",
            "  .gh add <公会名>        登记你的公会",
            "  .gh del                 删除你的记录",
            "  .gh list                查看本群已登记公会",
            "  .gh fix <公会名>        修改你的公会名",
            "  .gh new <用户ID> <公会名>  帮别人登记（需op/admin）",
            "  .gh op <用户ID>          授权别人使用new（仅admin）",
            "  .gh help                 此帮助",
        ]
        return "\n".join(lines)

    sub = args[0].lower()
    uk = str(user_id)

    # ── new: op/admin 帮别人登记 ──
    if sub == "new":
        if not _can_op(user_id, group_data):
            return "你没有权限使用此操作喵~ 需要 bot 管理员或 op 授权"
        if len(args) < 3:
            return "用法: .gh new <用户ID> <公会名>\n例: .gh new 3483585417 秋日小镇"
        try:
            target_qq = int(args[1])
        except ValueError:
            return f"QQ号格式不对喵: {args[1]}"
        name = " ".join(args[2:]).strip()
        if not name:
            return "公会名不能为空喵~"
        nick = await _resolve_nick(target_qq)
        members[str(target_qq)] = {"name": name, "nick": nick}
        _save(gh_path, data)
        logger.info("公会登记(new): group=%s op=%s target=%s name=%s nick=%s",
                   gid, uk, target_qq, name, nick)
        return f"已登记: {name} ← {nick}({target_qq})  (由 {sender_name} 操作)"

    # ── op: 授权某人使用 new ──
    if sub == "op":
        if not _is_admin(user_id):
            return "仅 bot 管理员可以授权 op 喵~"
        if len(args) < 2:
            return "用法: .gh op <用户ID>"
        try:
            target_qq = int(args[1])
        except ValueError:
            return f"QQ号格式不对喵: {args[1]}"
        opts = group_data.setdefault("opts", [])
        tks = str(target_qq)
        if tks in opts:
            opts.remove(tks)
            _save(gh_path, data)
            return f"已取消 {target_qq} 的 op 权限"
        opts.append(tks)
        _save(gh_path, data)
        return f"已授权 {target_qq} 使用 .gh new"

    # ── add: 自己登记 ──
    if sub == "add":
        if len(args) < 2:
            return "用法: .gh add <公会名>"
        name = " ".join(args[1:]).strip()
        if not name:
            return "公会名不能为空喵~"
        members[uk] = {"name": name, "nick": sender_name}
        _save(gh_path, data)
        logger.info("公会登记: group=%s user=%s name=%s nick=%s", gid, uk, name, sender_name)
        return f"已登记: {name} ← {sender_name}"

    # ── del ──
    if sub == "del":
        if uk in members:
            old = members.pop(uk)
            _save(gh_path, data)
            old_name = old if isinstance(old, str) else old.get("name", "?")
            logger.info("公会删除: group=%s user=%s", gid, uk)
            return f"已删除 {sender_name} 的公会记录: {old_name}"
        return "你还没有登记公会喵~ 用 .gh add <公会名> 登记"

    # ── list ──
    if sub in ("list",):
        if not members:
            return "本群还没有人登记公会喵~"
        lines = [f"【本群已登记公会 {len(members)}】"]
        for qq, info in members.items():
            if isinstance(info, str):
                lines.append(f"  {qq}: {info}")
            else:
                lines.append(f"  {info.get('nick', qq)}({qq}): {info.get('name', '?')}")
        return "\n".join(lines)

    # ── fix: 修改自己的 ──
    if sub == "fix":
        if len(args) < 2:
            return "用法: .gh fix <新公会名>"
        if uk not in members:
            return "你还没有登记公会喵~ 先用 .gh add 登记"
        name = " ".join(args[1:]).strip()
        if not name:
            return "公会名不能为空喵~"
        old_info = members[uk]
        old_name = old_info if isinstance(old_info, str) else old_info.get("name", "?")
        nick = old_info.get("nick", sender_name) if isinstance(old_info, dict) else sender_name
        members[uk] = {"name": name, "nick": nick}
        _save(gh_path, data)
        logger.info("公会修改: group=%s user=%s %s→%s", gid, uk, old_name, name)
        return f"已修改: {old_name} → {name}"

    return f"未知操作: {sub}\n.gh 查看帮助"
