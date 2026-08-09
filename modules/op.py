"""
OP 次级管理员模块
- admin > op > 用户 三级权限
- .op group set/del/list  — 群聊 OP 权限指派
- .persona <文本|reset|show> — 私聊人格切换
- .主人 — 私聊将当前用户设为主人
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("op")

# ── 数据文件路径 ──
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
# per-user persona 存到 config/persona_data.json（带版本戳，配置改动自动失效）
_PERSONA_FILE = _CONFIG_DIR / "persona_data.json"
_MASTER_FILE = _DATA_DIR / "private_masters.json"


# ════════════════════════════════════════════════════════════
#  私聊人格存储（config/persona_data.json）
#  格式: {"_version": 1, "users": {uid: {"persona": str, "version": int, "set_at": ts}}}
#  version 与 bot_config.toml [private_persona].version 对应，不匹配自动失效
# ════════════════════════════════════════════════════════════

def _load_persona_data() -> dict:
    """加载 persona_data.json，返回 {"_version": 0, "users": {}}"""
    if not _PERSONA_FILE.exists():
        return {"_version": 0, "users": {}}
    try:
        data = json.loads(_PERSONA_FILE.read_text(encoding="utf-8"))
        # 兼容旧格式（纯 {uid: str}）
        if "users" not in data:
            return {"_version": 0, "users": {}}
        return data
    except Exception as e:
        logger.warning("读取 persona_data 失败: %s", e)
        return {"_version": 0, "users": {}}


def _save_persona_data(data: dict):
    _PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PERSONA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_persona(user_id: int, current_version: int = 0) -> dict | None:
    """获取用户 persona dict {core, side, identity}。版本不匹配返回 None"""
    data = _load_persona_data()
    entry = data.get("users", {}).get(str(user_id))
    if not entry:
        return None
    # 兼容旧格式（纯字符串 → core）
    if isinstance(entry, str):
        return {"core": entry, "side": "", "identity": ""} if current_version == 0 else None
    if entry.get("version", 0) != current_version:
        return None
    # 新格式：三段
    if "core" in entry or "side" in entry or "identity" in entry:
        return {
            "core": entry.get("core", ""),
            "side": entry.get("side", ""),
            "identity": entry.get("identity", ""),
        }
    # 旧格式 {persona: str}
    p = entry.get("persona", "")
    return {"core": p, "side": "", "identity": ""} if p else None


def set_persona_field(user_id: int, field: str, value: str, version: int = 0):
    """设置 persona 单字段 (core/side/identity)"""
    data = _load_persona_data()
    uid = str(user_id)
    entry = data.get("users", {}).get(uid, {})
    # 兼容旧格式转新格式
    if isinstance(entry, str):
        entry = {"core": entry, "side": "", "identity": ""}
    elif "persona" in entry and "core" not in entry:
        entry = {"core": entry["persona"], "side": "", "identity": ""}
    entry[field] = value.strip()[:2000]
    entry["version"] = version
    entry["set_at"] = int(time.time())
    # 三段都空则删除
    if not any([entry.get("core"), entry.get("side"), entry.get("identity")]):
        data["users"].pop(uid, None)
    else:
        data["users"][uid] = entry
    _save_persona_data(data)


def clear_persona(user_id: int):
    """清除用户的私聊自定义人格"""
    data = _load_persona_data()
    data["users"].pop(str(user_id), None)
    _save_persona_data(data)


def get_persona_memory_id(user_id: int) -> str | None:
    """返回当前 persona 的记忆文件 ID（user_id_phash8），无 persona 返回 None"""
    data = _load_persona_data()
    entry = data.get("users", {}).get(str(user_id))
    if not entry:
        return None
    if isinstance(entry, str):
        core, side, identity = entry, "", ""
    elif "core" in entry or "side" in entry or "identity" in entry:
        core = entry.get("core", "")
        side = entry.get("side", "")
        identity = entry.get("identity", "")
    else:
        core = entry.get("persona", "")
        side, identity = "", ""
    if not any([core, side, identity]):
        return None
    import hashlib
    h = hashlib.md5(f"{core}|{side}|{identity}".encode()).hexdigest()[:8]
    return f"{user_id}_p{h}"


def clear_persona_memory(user_id: int) -> str | None:
    """清除当前 persona 的记忆文件，返回 memory_id 或 None"""
    from modules.memory import clear_memory_by_id
    memory_id = get_persona_memory_id(user_id)
    if not memory_id:
        return None
    clear_memory_by_id(memory_id)
    return memory_id


# ════════════════════════════════════════════════════════════
#  私聊主人指定
# ════════════════════════════════════════════════════════════

def is_private_master(user_id: int) -> bool:
    """检查用户是否被指定为私聊主人"""
    if not _MASTER_FILE.exists():
        return False
    try:
        data = json.loads(_MASTER_FILE.read_text(encoding="utf-8"))
        return str(user_id) in data
    except Exception as e:
        logger.warning("检查私聊主人失败: %s", e)
        return False


def set_private_master(user_id: int):
    """将用户设为主人（私聊场景）"""
    data = {}
    if _MASTER_FILE.exists():
        try:
            data = json.loads(_MASTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[str(user_id)] = True
    _MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def unset_private_master(user_id: int):
    """取消用户的私聊主人指定"""
    if not _MASTER_FILE.exists():
        return
    try:
        data = json.loads(_MASTER_FILE.read_text(encoding="utf-8"))
        data.pop(str(user_id), None)
        _MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  roles.toml 读写（群 OP 指派）
# ════════════════════════════════════════════════════════════

_ROLES_PATH = Path(__file__).resolve().parent.parent / "config" / "roles.toml"


def _read_roles() -> dict:
    """读取 roles.toml 为 dict，group_owners 键统一转为字符串"""
    import toml
    if not _ROLES_PATH.exists():
        return {}
    with open(_ROLES_PATH, "r", encoding="utf-8") as f:
        data = toml.load(f)
    # ★ 规范化 group_owners 键为字符串
    go = data.get("group_owners", {})
    if go:
        normalized = {}
        for k, v in go.items():
            key = str(k)
            if isinstance(v, list):
                normalized[key] = [int(q) for q in v]
            else:
                normalized[key] = [int(v)]
        data["group_owners"] = normalized
    return data


def _write_roles(data: dict):
    """写回 roles.toml"""
    _ROLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ROLES_PATH, "w", encoding="utf-8") as f:
        # 手动写以保持格式可读
        f.write("# ── 角色权限 ──\n")
        f.write(f"admin_qq = {data.get('admin_qq', 0)}\n\n")
        f.write("# ── OP 次级管理员 ──\n")
        op_qqs = data.get("op_qqs", [])
        f.write("op_qqs = [\n")
        for q in op_qqs:
            f.write(f"    {q},\n")
        f.write("]\n\n")
        f.write("# ── 好友列表 ──\n")
        f.write("friend_qqs = [\n")
        for q in data.get("friend_qqs", []):
            f.write(f"    {q},\n")
        f.write("]\n\n")
        f.write("# ── 群主权限指派 ──\n")
        group_owners = data.get("group_owners", {})
        if group_owners:
            f.write("[group_owners]\n")
            for gid, oqs in group_owners.items():
                if isinstance(oqs, list):
                    f.write(f"{gid} = [\n")
                    for oq in oqs:
                        f.write(f"    {oq},\n")
                    f.write("]\n")
                else:
                    f.write(f"{gid} = {oqs}\n")
            f.write("\n")
        f.write("# ── QQ → 昵称映射 ──\n")
        f.write("[qq_name_map]\n")
        for qq, name in data.get("qq_name_map", {}).items():
            f.write(f"{qq} = \"{name}\"\n")
    # ★ 热重载让 BotConfig 单例立即生效
    try:
        from core.config import reload_config
        reload_config()
    except Exception as e:
        logger.warning("配置热重载失败: %s", e)


def add_op_qq(qq: int, group_id: int = 0) -> str:
    """添加单个 OP QQ（保留兼容）"""
    return add_op_qqs([qq], group_id=group_id)


def add_op_qqs(qqs: list[int], group_id: int = 0) -> str:
    """批量添加 OP QQ，若在群内发起则自动指派该群"""
    data = _read_roles()
    op_qqs: list = data.get("op_qqs", [])
    group_owners: dict = data.get("group_owners", {})
    added = []
    skipped = []
    assigned = []
    for qq in qqs:
        if qq not in op_qqs:
            op_qqs.append(qq)
            added.append(qq)
        else:
            skipped.append(qq)
    # ★ 自动指派当前群（追加到列表）
    if group_id and group_id not in group_owners:
        group_owners[str(group_id)] = []
    if group_id and qq not in group_owners.get(str(group_id), []):
        group_owners[str(group_id)].append(qq)
        assigned.append(qq)

    if not added and skipped and not assigned:
        names = ",".join(str(q) for q in skipped)
        return f"QQ {names} 已经是 OP 了"

    data["op_qqs"] = op_qqs
    data["group_owners"] = group_owners
    _write_roles(data)

    msg_parts = []
    if added:
        names = ",".join(str(q) for q in added)
        msg_parts.append(f"已将 {names} 设为 OP")
        logger.info("批量添加 OP: %s", added)
    if assigned:
        from core.config import get_config
        cfg = get_config()
        for q in assigned:
            pname = cfg.get_display_name(q, group_id)
            msg_parts.append(f"群 {group_id} 的权限已指派给 OP [{pname}({q})]")
    if skipped:
        names = ",".join(str(q) for q in skipped)
        msg_parts.append(f"{names} 早就是 OP，跳过")
    return "；".join(msg_parts)


def del_op_qq(qq: int) -> str:
    """移除 OP QQ"""
    data = _read_roles()
    op_qqs: list = data.get("op_qqs", [])
    if qq not in op_qqs:
        return f"QQ {qq} 不是 OP"
    op_qqs.remove(qq)
    data["op_qqs"] = op_qqs
    # 同时清理该 OP 的所有群指派（从列表中移除）
    group_owners: dict = data.get("group_owners", {})
    removed_groups = []
    for gid, owners in list(group_owners.items()):
        if qq in owners:
            owners.remove(qq)
            if not owners:
                del group_owners[gid]
            removed_groups.append(gid)
    data["group_owners"] = group_owners
    _write_roles(data)
    logger.info("移除 OP: %d", qq)
    msg = f"已移除 QQ {qq} 的 OP 权限"
    if removed_groups:
        msg += f"，同时清理了 {len(removed_groups)} 个群的 OP 指派"
    return msg


def set_group_ops(group_id: int, op_qqs: list[int]) -> str:
    """将群主权限指派给多个 OP"""
    data = _read_roles()
    global_op_qqs: list = data.get("op_qqs", [])
    unknown = [q for q in op_qqs if q not in global_op_qqs]
    if unknown:
        return f"以下 QQ 不是 OP，请先用 .op add 添加: {unknown}"
    group_owners: dict = data.get("group_owners", {})
    group_owners[str(group_id)] = op_qqs
    data["group_owners"] = group_owners
    _write_roles(data)
    from core.config import get_config
    cfg = get_config()
    names = [f"【{cfg.get_display_name(q, group_id)}({q})】" for q in op_qqs]
    return f"群 {group_id} 的权限已指派给 OP: {', '.join(names)}"


def set_group_op(group_id: int, op_qq: int) -> str:
    """将群主权限指派给 OP"""
    data = _read_roles()
    op_qqs: list = data.get("op_qqs", [])
    if op_qq not in op_qqs:
        return f"QQ {op_qq} 不是 OP，请先用 .owner op add {op_qq} 添加"
    group_owners: dict = data.get("group_owners", {})
    if str(group_id) not in group_owners:
        group_owners[str(group_id)] = []
    if op_qq not in group_owners[str(group_id)]:
        group_owners[str(group_id)].append(op_qq)
    data["group_owners"] = group_owners
    _write_roles(data)
    logger.info("群 OP 指派: group=%d → op_qq=%d", group_id, op_qq)
    from core.config import get_config
    cfg = get_config()
    pname = cfg.get_display_name(op_qq, group_id)
    return f"群 {group_id} 的权限已指派给 OP [{pname}({op_qq})]"


def del_group_op(group_id: int, op_qq: int = 0) -> str:
    """撤销群的 OP 指派。op_qq=0 时清空整个群并移除全局 OP 注册"""
    data = _read_roles()
    group_owners: dict = data.get("group_owners", {})
    if str(group_id) not in group_owners:
        return f"群 {group_id} 没有 OP 指派"
    if op_qq:
        if op_qq not in group_owners[str(group_id)]:
            return f"群 {group_id} 没有指派给 QQ {op_qq}"
        group_owners[str(group_id)].remove(op_qq)
        if not group_owners[str(group_id)]:
            del group_owners[str(group_id)]
        msg = f"已撤销群 {group_id} 对 QQ {op_qq} 的指派"
    else:
        removed = group_owners.pop(str(group_id))
        # 同步清空全局花名册中该群涉及的 OP（若不再被任何群指派）
        global_ops: list = data.get("op_qqs", [])
        for q in removed:
            still_assigned = any(q in owners for owners in group_owners.values())
            if not still_assigned and q in global_ops:
                global_ops.remove(q)
        data["op_qqs"] = global_ops
        msg = f"已清空群 {group_id} 的全部 OP（{len(removed)} 人）"
    data["group_owners"] = group_owners
    _write_roles(data)
    logger.info("撤销群 OP: group=%d op_qq=%d cleared=%d", group_id, op_qq, len(removed) if not op_qq else 0)
    return msg


def list_group_ops() -> str:
    """列出所有群 OP 指派"""
    data = _read_roles()
    op_qqs: list = data.get("op_qqs", [])
    group_owners: dict = data.get("group_owners", {})
    from core.config import get_config
    cfg = get_config()

    lines = ["【OP 列表】"]
    if not op_qqs:
        lines.append("  (无)")
    else:
        for q in op_qqs:
            name = cfg.get_display_name(q)
            lines.append(f"  OP: {name}({q})")

    lines.append("")
    lines.append("【群 OP 指派】")
    if not group_owners:
        lines.append("  (无)")
    else:
        for gid, oqs in group_owners.items():
            names = [cfg.get_display_name(int(oq), int(gid)) for oq in oqs]
            qnames = [f"{n}({oq})" for n, oq in zip(names, oqs)]
            lines.append(f"  群 {gid} → {', '.join(qnames)}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#  指令入口
# ════════════════════════════════════════════════════════════

async def cmd_op(args, user_id, group_id, sender_name, is_group, bot_qq):
    """OP 权限管理 .op [add|del <QQ> | group set|del|list]"""
    from core.config import get_config
    cfg = get_config()

    is_owner = user_id == cfg.admin_qq
    is_group_op = group_id and user_id in cfg.group_owners.get(group_id, [])

    # 全局 OP 增删 → 仅主人
    # 群指派 → 主人或该群 OP

    if not args:
        return list_group_ops()

    sub = args[0].lower()

    if sub in ("add", "del"):
        if not is_owner:
            return "只有主人能增删全局 OP 喵~"
        if sub == "add":
            if len(args) < 2:
                return "用法: .op add <QQ号> [QQ号2 QQ号3 ...]"
            qqs = [int(a) for a in args[1:]]
            return add_op_qqs(qqs)
        else:
            if len(args) < 2:
                return "用法: .op del <QQ号>"
            return del_op_qq(int(args[1]))

    elif sub == "group":
        if len(args) < 2:
            return "用法: .op group set <群号> <OP_QQ> | del <群号> | list"
        action = args[1].lower()

        if action == "set":
            if not is_owner and not is_group_op:
                return "只有主人或本群 OP 能指派群权限喵~"
            if len(args) < 4:
                return "用法: .op group set <群号> <OP_QQ号> [OP_QQ2 ...]"
            gid = int(args[2])
            # OP 只能管理自己所在的群
            if not is_owner and is_group_op and gid != group_id:
                return f"你只能管理群 {group_id} 的权限喵~"
            oqs = [int(a) for a in args[3:]]
            return set_group_ops(gid, oqs)

        elif action == "del":
            if not is_owner and not is_group_op:
                return "只有主人或本群 OP 能删除群权限喵~"
            if len(args) < 3:
                return "用法: .op group del <群号> [OP_QQ]（省略 OP_QQ 则清空整个群）"
            gid = int(args[2])
            if not is_owner and is_group_op and gid != group_id:
                return f"你只能管理群 {group_id} 的权限喵~"
            oq = int(args[3]) if len(args) > 3 else 0
            return del_group_op(gid, oq)

        elif action == "list":
            return list_group_ops()

        else:
            return "未知操作，支持: set/del/list"

    else:
        return "用法: .op [add|del <QQ> | group set|del|list]"


def _format_persona_display(p: dict) -> str:
    """格式化显示 persona 三段"""
    lines = ["当前人格:"]
    if p.get("core"):
        lines.append(f"\n[核心人格]\n{p['core']}")
    if p.get("side"):
        lines.append(f"\n[侧面人格]\n{p['side']}")
    if p.get("identity"):
        lines.append(f"\n[固定身份]\n{p['identity']}")
    if not any(p.values()):
        lines.append("(空)")
    return "\n".join(lines)


_PERSONA_HELP = (
    "【人格切换用法】\n"
    "  .persona                    — 查看当前\n"
    "  .persona help               — 显示本帮助\n"
    "  .persona <文本>             — 设置核心人格（兼容旧用法）\n"
    "  .persona core <文本>        — 设置核心人格\n"
    "  .persona side <文本>        — 设置侧面人格\n"
    "  .persona identity <文本>    — 设置固定身份\n"
    "  .persona show               — 查看当前\n"
    "  .persona reset              — 清除整个 persona（恢复默认人格）\n"
    "  .persona memory reset       — 清空当前人格的专属记忆\n\n"
    "三段说明:\n"
    "  core     — 性格核心，语气/口癖/说话方式（必填，最能影响回复风格）\n"
    "  side     — 侧面人格，情感倾向/情绪反应（可选，补充细节）\n"
    "  identity — 固定身份，名字/年龄/职业/与你的关系（可选，背景设定）\n\n"
    "建议: 详细描述性格/语气/口癖/与你的关系，每段上限2000字。\n"
    "例如: .persona core 你是高冷的御姐，说话简洁带刺，但会偷偷关心我"
)


async def cmd_persona(args, user_id, group_id, sender_name, is_group, bot_qq):
    """私聊人格切换"""
    if is_group:
        return "人格切换仅支持私聊喵~"

    from core.config import get_config
    cfg = get_config()
    cur_ver = cfg.private_persona_version

    # 无参数：显示当前
    if not args:
        current = get_persona(user_id, cur_ver)
        if current:
            return _format_persona_display(current) + "\n\n使用 .persona reset 恢复默认\n使用 .persona help 查看完整用法"
        return _PERSONA_HELP

    sub = args[0].lower()

    # help 子命令
    if sub == "help":
        return _PERSONA_HELP

    # memory reset 子命令
    if sub == "memory" and len(args) >= 2 and args[1].lower() == "reset":
        memory_id = clear_persona_memory(user_id)
        if memory_id:
            return f"已清空当前人格的专属记忆喵~ (id: {memory_id})"
        return "当前未设置自定义人格，无独立记忆可清空"

    # reset 子命令
    if sub == "reset":
        clear_persona(user_id)
        return "已恢复默认人格喵~（记忆文件保留，下次设回同人格仍可召回）"

    # show 子命令
    if sub == "show":
        current = get_persona(user_id, cur_ver)
        if current:
            return _format_persona_display(current) + "\n\n使用 .persona reset 恢复默认"
        return "当前使用默认人格"

    # core/side/identity 子命令
    if sub in ("core", "side", "identity") and len(args) >= 2:
        value = " ".join(args[1:])
        if len(value) > 2000:
            return f"{sub} 描述过长（上限2000字），请精简后重试喵~"
        set_persona_field(user_id, sub, value, cur_ver)
        logger.info("用户 %d 设置 persona.%s: %s...", user_id, sub, value[:40])
        return f"{sub} 已设置喵～\n\n如需恢复默认: .persona reset\n如需清空记忆: .persona memory reset"

    # 默认：单文本设 core（兼容旧用法 .persona <text>）
    persona = " ".join(args)
    if len(persona) > 2000:
        return "人格描述过长（上限2000字），请精简后重试喵~"
    set_persona_field(user_id, "core", persona, cur_ver)
    logger.info("用户 %d 设置 persona.core: %s...", user_id, persona[:40])
    return f"核心人格已设置喵～\n\n当前人格:\n{persona[:2000]}\n\n如需恢复默认: .persona reset"


async def cmd_master(args, user_id, group_id, sender_name, is_group, bot_qq):
    """私聊主人指定 .主人"""
    if is_group:
        return "主人指定仅支持私聊喵~"

    from core.config import get_config
    cfg = get_config()

    # admin/OP 也可以用
    if cfg.is_admin(user_id, group_id):
        set_private_master(user_id)
        return "主人随时都在喵~（私聊已设为 admin）"

    if is_private_master(user_id):
        return "你已经是主人了喵~"

    set_private_master(user_id)
    logger.info("私聊主人指定: user=%d", user_id)
    return f"好的喵！{sender_name}，你以后就是我在私聊里的主人了～"


async def cmd_op_list(args, user_id, group_id, sender_name, is_group, bot_qq):
    """查看 OP 状态"""
    return list_group_ops()


# ════════════════════════════════════════════════════════════
#  机器人模式：普通 / 含蓄叙述 / 睡觉
# ════════════════════════════════════════════════════════════

_MODE_FILE = _DATA_DIR / "bot_mode.json"


def _load_modes() -> dict:
    """加载分聊天模式表 {chat_id: {"mode": str, "since": float}}"""
    if not _MODE_FILE.exists():
        return {}
    try:
        data = json.loads(_MODE_FILE.read_text(encoding="utf-8"))
        # ★ 兼容旧格式: "247478659": "sleeping" → {"mode":"sleeping","since":0}
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = {"mode": v, "since": 0}
        return data
    except Exception as e:
        logger.warning("读取bot模式失败: %s", e)
        return {}


def get_mode(chat_id: int = 0) -> str:
    """返回指定聊天的模式（默认 normal）"""
    if not chat_id:
        return "normal"
    entry = _load_modes().get(str(chat_id))
    if not entry:
        return "normal"
    return entry.get("mode", "normal") if isinstance(entry, dict) else entry


def _save_modes(data: dict):
    _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MODE_FILE.write_text(json.dumps(data, ensure_ascii=False))


def _get_sleep_duration(chat_id: int) -> float:
    """返回已睡眠秒数（0=刚躺下、正数=睡着时长）"""
    entry = _load_modes().get(str(chat_id))
    if not entry or not isinstance(entry, dict):
        return 0
    since = entry.get("since", 0)
    if not since:
        return 0
    return time.time() - since


def is_sleeping(chat_id: int = 0) -> bool:
    return get_mode(chat_id) == "sleeping" if chat_id else False


def is_narrative(chat_id: int = 0) -> bool:
    if not chat_id:
        return False
    return get_mode(chat_id) in ("narrative", "sleeping")


def get_sleep_prompt_rule(chat_id: int = 0) -> str:
    """返回睡觉模式的回复规则（根据已睡时长变化）"""
    dur = _get_sleep_duration(chat_id)
    if dur < 30:
        return (
            "你现在刚刚关上灯躺到床上，还处于「准备入睡」阶段。"
            "所有回复用第三人称括号叙述，像迷糊的自语。例如：(灯刚关，还没睡着…)、(翻了个身，枕头凉凉的)。"
            "绝对不要用第一人称。10字左右。"
        )
    elif dur < 300:
        return (
            "你刚入睡不久，处于「浅眠」状态。"
            "所有回复用第三人称括号叙述，带着睡意。例如：(听到有人说话但不想醒…)、(往被子里缩了缩)。"
            "绝对不要用第一人称。10字左右。"
        )
    elif dur < 3600:
        return (
            "你正在熟睡中。"
            "所有回复用第三人称括号，含混不清。例如：(zzzZZZ…嗯…)、(迷迷糊糊哼了一声)。"
            "绝对不要用第一人称。10字左右。"
        )
    else:
        return (
            "你已经睡了很久，快自然醒了。"
            "所有回复用第三人称括号，开始有点清醒但赖床。例如：(揉了揉眼睛)天亮了吗…、(不想起床但好像该起了)。"
            "绝对不要用第一人称。15字左右。"
        )


def get_narrative_prompt_rule() -> str:
    """返回含蓄叙述模式的回复规则（无睡意，日常可用）"""
    return (
        "你当前采用「含蓄叙述」风格。所有回复都用第三人称括号叙述，"
        "像轻声的内心独白，包裹在圆括号里。例如：(悄悄看了他一眼)、(嘴角微微上扬)、(默默记下了这件事)。"
        "绝对不要用第一人称直接对话，不要说'我'，全部用括号内的第三方叙述。"
        "字数15字左右，含蓄、克制，像在写日记或轻声自语。"
        "可以表达情绪但不过分外露——温暖但不粘腻，关心但不刻意。"
    )


async def cmd_hanxu(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.含蓄 — 切换含蓄叙述风格（仅 admin），仅影响当前聊天"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "只有主人能切换我的说话风格喵~"

    chat_id = group_id if is_group else user_id
    current = get_mode(chat_id)
    if current == "narrative":
        _save_modes({k: v for k, v in _load_modes().items() if k != str(chat_id)})
        logger.info("含蓄模式关闭 chat=%d", chat_id)
        return "好，恢复正常说话方式喵~"
    else:
        modes = _load_modes()
        modes[str(chat_id)] = {"mode": "narrative", "since": time.time()}
        _save_modes(modes)
        logger.info("含蓄模式开启 chat=%d", chat_id)
        return "(轻声) 好的…从现在开始会这样说话。像写日记一样，你知道我在陪着你。"


async def cmd_sleep(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.sleep — 切换睡觉模式（仅 admin），仅影响当前聊天"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "只有主人能让我睡觉喵~"

    chat_id = group_id if is_group else user_id
    current = get_mode(chat_id)
    if current == "sleeping":
        modes = _load_modes()
        modes.pop(str(chat_id), None)
        _save_modes(modes)
        logger.info("睡觉模式关闭 chat=%d", chat_id)
        return "睡醒了喵！精神满满～"
    else:
        modes = _load_modes()
        modes[str(chat_id)] = {"mode": "sleeping", "since": time.time()}
        _save_modes(modes)
        logger.info("睡觉模式开启 chat=%d", chat_id)
        return "(zzzZZZ…已经睡着了，现在说什么都只会得到迷糊的呓语…)"
