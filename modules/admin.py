"""
管理员配置管理模块 — v2
- 统一读写 bot_config.toml / adapter_config.toml
- 管理 data/*.json（fav, luck, countdown, reminders 等）
- 热重载 + 文件操作
"""
from __future__ import annotations

import json
import toml
from pathlib import Path

from core.logger import get_logger
from core.config import reload_config

logger = get_logger("admin")

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _ROOT / "config"


def _trigger_reload():
    """通过控制文件触发 bot 热重载（避免直接调用 reload_config 导致状态不一致）"""
    from pathlib import Path
    ctrl = Path(__file__).resolve().parent.parent / "data" / "control.txt"
    ctrl.write_text("reload\n", encoding="utf-8")
_DATA_DIR = _ROOT / "data"

# ════════════════════════════════════════════════════════════
#  TOAD 通用读写
# ════════════════════════════════════════════════════════════

def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    return toml.loads(path.read_text(encoding="utf-8"))


def _write_toml(path: Path, data: dict):
    path.write_text(toml.dumps(data), encoding="utf-8")


# ════════════════════════════════════════════════════════════
#  JSON 通用读写
# ════════════════════════════════════════════════════════════

def _read_json(path: Path) -> dict | list:
    if not path.exists():
        return {} if path.suffix == ".json" else []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _reload():
    try:
        reload_config()
        logger.info("配置热重载完成")
    except Exception as e:
        logger.error("配置重载失败: %s", e)


# ════════════════════════════════════════════════════════════
#  通用 config get/set（点分隔路径: "chat.group_list"）
# ════════════════════════════════════════════════════════════

_CONFIG_FILES = {
    "bot": _CONFIG_DIR / "bot_config.toml",
    "adapter": _CONFIG_DIR / "adapter_config.toml",
    "roles": _CONFIG_DIR / "roles.toml",
}


def config_get(path: str) -> str:
    """读取配置: config_get("bot.model") 或 config_get("adapter.chat.group_list")"""
    parts = path.split(".", 1)
    if parts[0] not in _CONFIG_FILES:
        return f"未知配置域: {parts[0]}，可用: {', '.join(_CONFIG_FILES)}"

    data = _read_toml(_CONFIG_FILES[parts[0]])
    cur = data
    keys = parts[1].split(".") if len(parts) > 1 else []
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return f"键不存在: {path}"
    return json.dumps(cur, ensure_ascii=False, indent=2)


def config_set(path: str, value: str) -> str:
    """设置配置: config_set("bot.reply_threshold", "5") 支持 str/int/float/bool/list"""
    parts = path.split(".", 1)
    if parts[0] not in _CONFIG_FILES:
        return f"未知配置域: {parts[0]}"

    data = _read_toml(_CONFIG_FILES[parts[0]])
    keys = parts[1].split(".") if len(parts) > 1 else []
    if not keys:
        return "请指定键路径，如 bot.reply_threshold"

    # 解析值
    parsed = _parse_value(value)

    # 沿着路径设置
    cur = data
    for k in keys[:-1]:
        if k not in cur:
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = parsed

    _write_toml(_CONFIG_FILES[parts[0]], data)
    _reload()
    logger.info("配置修改: %s = %s", path, value)
    return f"✅ {path} = {parsed}"


def config_list(section: str) -> str:
    """列出配置分区键"""
    if section not in _CONFIG_FILES:
        return f"未知域: {section}，可用: {', '.join(_CONFIG_FILES)}"

    data = _read_toml(_CONFIG_FILES[section])

    def _flatten(d, prefix=""):
        lines = []
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                lines.append(f"  [{p}]")
                lines.extend(_flatten(v, p))
            else:
                lines.append(f"  {p} = {json.dumps(v, ensure_ascii=False)}")
        return lines

    lines = [f"【{section} 配置】"]
    lines.extend(_flatten(data))
    return "\n".join(lines)


def _parse_value(s: str):
    """解析字符串值为 Python 对象"""
    s = s.strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() == "null" or s.lower() == "none":
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
        return [x.strip() for x in inner.split(",") if x.strip()]
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)
    return s


# ════════════════════════════════════════════════════════════
#  群/私聊白名单（快捷操作）
# ════════════════════════════════════════════════════════════

def _adapter_edit(list_key: str, action: str, value: int) -> str:
    """通用白名单编辑"""
    data = _read_toml(_CONFIG_DIR / "adapter_config.toml")
    chat = data.setdefault("chat", {})
    lst = list(chat.get(list_key, []))

    if action == "add":
        if value in lst:
            return f"已在 {list_key} 中: {value}"
        lst.append(value)
    elif action == "remove":
        if value not in lst:
            return f"不在 {list_key} 中: {value}"
        lst.remove(value)
    else:
        return f"未知操作: {action}"

    chat[list_key] = lst
    _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
    _reload()
    logger.info("白名单 %s %s %d", list_key, action, value)
    return f"✅ {list_key} {action}: {value}"


def whitelist_add(what: str, value: int) -> str:
    key = "private_whitelist" if what in ("private", "private_whitelist") else "group_list"
    return _adapter_edit(key, "add", value)


def whitelist_remove(what: str, value: int) -> str:
    key = "private_whitelist" if what in ("private", "private_whitelist") else "group_list"
    return _adapter_edit(key, "remove", value)


# ════════════════════════════════════════════════════════════
#  Group set（@仅模式等）
# ════════════════════════════════════════════════════════════

def group_set(group_id: int, key: str, value: str) -> str:
    """设置群属性: at_only, reply_threshold"""
    data = _read_toml(_CONFIG_DIR / "adapter_config.toml")
    gs = data.setdefault("group_settings", {})
    gid = str(group_id)
    if gid not in gs:
        gs[gid] = {}

    parsed = _parse_value(value)
    gs[gid][key] = parsed
    _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
    _reload()
    return f"✅ 群 {group_id} {key} = {parsed}"


# ════════════════════════════════════════════════════════════
#  Luck 专用（日期分层格式: {"date": {"qq": value}}）
# ════════════════════════════════════════════════════════════

def luck_list() -> str:
    """列出所有 luck 记录（按日期分层）"""
    data = _read_json(_DATA_DIR / "luck.json")
    if not isinstance(data, dict) or not data:
        return "暂无运气记录"

    from datetime import date as _date
    today = _date.today().isoformat()
    lines = [f"【运气记录 ({len(data)} 天)】"]

    for day, users in sorted(data.items(), reverse=True)[:10]:
        flag = " ←今天" if day == today else ""
        u = ", ".join(f"{qq}={val}" for qq, val in sorted(users.items()))
        lines.append(f"  {day}{flag}: {u}")
    return "\n".join(lines)


def luck_set(qq: str, value: str) -> str:
    """设置今天的 luck 值: luck_set("3483585417", "85")"""
    from datetime import date as _date
    today = _date.today().isoformat()
    data = _read_json(_DATA_DIR / "luck.json")
    if not isinstance(data, dict):
        data = {}
    if today not in data:
        data[today] = {}

    parsed = _parse_value(value)
    data[today][qq] = parsed
    _write_json(_DATA_DIR / "luck.json", data)
    return f"✅ 今天 {qq} 的运气 → {parsed}"


def luck_del(qq: str) -> str:
    """删除今天的 luck 值: luck_del("3483585417")"""
    from datetime import date as _date
    today = _date.today().isoformat()
    data = _read_json(_DATA_DIR / "luck.json")
    if not isinstance(data, dict) or today not in data:
        return "今天没有运气记录"
    if qq not in data[today]:
        return f"今天没有 {qq} 的运气记录"
    del data[today][qq]
    if not data[today]:
        del data[today]
    _write_json(_DATA_DIR / "luck.json", data)
    return f"✅ 已删除今天 {qq} 的运气值"

_DATA_FILES = {
    "fav": _DATA_DIR / "fav.json",
    "luck": _DATA_DIR / "luck.json",
    "countdown": _DATA_DIR / "countdown.json",
    "reminders": _DATA_DIR / "reminders.json",
    "recall": _DATA_DIR / "recall.json",
    "wzq": _DATA_DIR / "wzq_results.json",
}


def data_get(name: str, key: str = "") -> str:
    """读取数据文件: data_get("fav") 或 data_get("fav", "g_123_456")"""
    if name not in _DATA_FILES:
        return f"未知数据: {name}，可用: {', '.join(_DATA_FILES)}"

    data = _read_json(_DATA_FILES[name])
    if key:
        if isinstance(data, dict) and key in data:
            return f"{name}.{key} = {data[key]}"
        if isinstance(data, list):
            try:
                idx = int(key) - 1
                if 0 <= idx < len(data):
                    return json.dumps(data[idx], ensure_ascii=False, indent=2)
                return f"索引超出: {key}/{len(data)}"
            except ValueError:
                pass
            return f"键不存在: {name}.{key}"
        return f"键不存在: {name}.{key}"

    # 返回摘要
    if isinstance(data, dict):
        return f"{name} ({len(data)} 条): " + json.dumps(data, ensure_ascii=False)[:200]
    if isinstance(data, list):
        return f"{name} ({len(data)} 条): " + json.dumps(data, ensure_ascii=False)[:200]
    return str(data)


def data_set(name: str, key: str, value: str) -> str:
    """设置数据值: data_set("fav", "g_123_456", "100")"""
    if name not in _DATA_FILES:
        return f"未知数据: {name}"

    data = _read_json(_DATA_FILES[name])
    if not isinstance(data, dict):
        data = {}

    parsed = _parse_value(value)
    # ★ fav 值 clamp 到 0~100
    if name == "fav" and isinstance(parsed, (int, float)):
        parsed = max(0, min(100, int(parsed)))
    data[key] = parsed
    _write_json(_DATA_FILES[name], data)
    return f"✅ {name}.{key} = {parsed}"


def data_delete(name: str, key: str) -> str:
    """删除数据: data_delete("fav", "g_123_456")"""
    if name not in _DATA_FILES:
        return f"未知数据: {name}"

    data = _read_json(_DATA_FILES[name])
    if isinstance(data, dict):
        if key in data:
            del data[key]
            _write_json(_DATA_FILES[name], data)
            return f"✅ 已删除 {name}.{key}"
        return f"键不存在: {name}.{key}"
    if isinstance(data, list):
        try:
            idx = int(key) - 1
            if 0 <= idx < len(data):
                del data[idx]
                _write_json(_DATA_FILES[name], data)
                return f"✅ 已删除 {name}[{key}]"
        except ValueError:
            pass
    return f"不支持的操作: {name}.{key}"


def data_reset(name: str) -> str:
    """重置数据文件: data_reset("fav") 或 data_reset("all")"""
    if name == "all":
        for n, p in _DATA_FILES.items():
            if p.exists():
                p.unlink()
                logger.warning("数据已删除: %s", n)
        return "✅ 所有数据已清空（fav/luck/countdown/reminders/recall/wzq）"

    if name not in _DATA_FILES:
        return f"未知数据: {name}"

    p = _DATA_FILES[name]
    if p.exists():
        p.unlink()
        logger.warning("数据已删除: %s", name)
        return f"✅ {name} 数据已清空"
    return f"{name} 没有数据文件"


# ════════════════════════════════════════════════════════════
#  白名单一览
# ════════════════════════════════════════════════════════════

def show_whitelists() -> str:
    data = _read_toml(_CONFIG_DIR / "adapter_config.toml")
    chat = data.get("chat", {})
    glist = chat.get("group_list", [])
    plist = chat.get("private_whitelist", [])

    lines = ["【白名单一览】"]
    lines.append(f"群 ({len(glist)}): {', '.join(str(g) for g in glist)}")
    lines.append(f"私聊 ({len(plist)}): {', '.join(str(q) for q in plist)}")
    gs = data.get("group_settings", {})
    if gs:
        lines.append("")
        lines.append("群自定义设置:")
        for gid, s in gs.items():
            parts = [f"群{gid}: "]
            if "reply_threshold" in s:
                parts.append(f"阈值={s['reply_threshold']}")
            if s.get("at_only"):
                parts.append("@仅")
            lines.append("  " + " ".join(parts))
    return "\n".join(lines)


def wl_cmd_manage(args: list) -> str:
    """分群指令白名单: .owner wl cmd add|remove|list <群号> [指令]"""
    data = _read_toml(_CONFIG_DIR / "adapter_config.toml")
    gs = data.setdefault("group_settings", {})

    if not args:
        return "用法: wl cmd add|remove|list|clear <群号> [指令]\n  remove 不写指令 = 禁止所有, clear = 恢复全部"

    sub = args[0].lower()
    if sub == "clear":
        if len(args) < 2:
            return "用法: wl cmd clear <群号>"
        from core.config import reload_config
        gid = args[1]
        if gid in gs and "cmd_whitelist" in gs[gid]:
            del gs[gid]["cmd_whitelist"]
            data["group_settings"] = gs
            _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
            _trigger_reload()
            return f"群{gid} 指令限制已清除（所有指令可用）"
        return f"群{gid} 无指令限制"

    if sub in ("list", "show"):
        if len(args) < 2:
            active = [(g, s.get("cmd_whitelist")) for g, s in gs.items() if "cmd_whitelist" in s]
            if not active:
                return "无分群指令白名单"
            lines = ["分群指令白名单:"]
            for gid, cmds in active:
                if cmds:
                    lines.append(f"  群{gid}: {', '.join(cmds)}")
                else:
                    lines.append(f"  群{gid}: (全部禁止)")
            return "\n".join(lines)
        gid = args[1]
        gs_group = gs.get(gid, {})
        if "cmd_whitelist" not in gs_group:
            return f"群{gid} 无指令限制（所有指令可用）"
        cmds = gs_group["cmd_whitelist"]
        if not cmds:
            return f"群{gid} 指令白名单: (全部禁止)"
        return f"群{gid} 指令白名单: {', '.join(cmds)}"

    if sub in ("add", "remove") and len(args) < 2:
        return f"用法: wl cmd {sub} <群号> [指令名]"

    gid = args[1]
    gs_group = gs.get(gid, {})
    if not isinstance(gs_group, dict):
        gs_group = {}
    
    if sub == "add":
        if len(args) < 3:
            return "用法: wl cmd add <群号> <指令名>"
        cmd_name = args[2].lower().lstrip(".#")
        cmds = gs_group.get("cmd_whitelist", [])
        if not isinstance(cmds, list):
            cmds = []
        if cmd_name in cmds:
            return f"群{gid} 已有指令 {cmd_name}"
        cmds.append(cmd_name)
        gs_group["cmd_whitelist"] = cmds
    elif sub == "remove":
        if len(args) < 3:
            # 不写指令 → 清空白名单（全部禁止）
            gs_group["cmd_whitelist"] = []
        else:
            cmd_name = args[2].lower().lstrip(".#")
            cmds = gs_group.get("cmd_whitelist", [])
            if not isinstance(cmds, list):
                cmds = []
            if cmd_name not in cmds:
                return f"群{gid} 无指令 {cmd_name}"
            cmds.remove(cmd_name)
            gs_group["cmd_whitelist"] = cmds
    else:
        return f"未知操作: {sub}"

    gs[gid] = gs_group
    data["group_settings"] = gs
    _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
    _trigger_reload()
    cmds = gs_group.get("cmd_whitelist", [])
    return f"群{gid} 指令白名单已更新: {', '.join(cmds) if cmds else '(全部禁止)'}"


def wl_welcome_manage(args: list) -> str:
    """分群欢迎语: .owner wl welcome set|del|show <群号> [欢迎语]"""
    data = _read_toml(_CONFIG_DIR / "adapter_config.toml")
    gs = data.setdefault("group_settings", {})

    if not args:
        return "用法: wl welcome set|del|show <群号> [欢迎语]\n  模板: {user}=QQ号 {group}=群号"

    sub = args[0].lower()
    if sub in ("show",):
        if len(args) < 2:
            active = [(g, s.get("welcome_msg", "")) for g, s in gs.items() if s.get("welcome_msg")]
            if not active: return "无分群欢迎语"
            lines = ["分群欢迎语:"]
            for gid, msg in active:
                lines.append(f"  群{gid}: {msg}")
            return "\n".join(lines)
        gid = args[1]
        msg = gs.get(gid, {}).get("welcome_msg", "")
        return f"群{gid} 欢迎语: {msg}" if msg else f"群{gid} 无欢迎语"

    if len(args) < 2:
        return f"用法: wl welcome {sub} <群号> [欢迎语]"

    gid = args[1]
    gs_group = gs.get(gid, {})
    if not isinstance(gs_group, dict): gs_group = {}

    if sub == "set":
        if len(args) < 3: return "用法: wl welcome set <群号> <欢迎语>"
        msg = " ".join(args[2:])
        gs_group["welcome_msg"] = msg
    elif sub == "del":
        gs_group.pop("welcome_msg", None)
        gs[gid] = gs_group
        data["group_settings"] = gs
        _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
        _trigger_reload()
        return f"群{gid} 欢迎语已删除"
    else:
        return f"未知操作: {sub}"

    gs[gid] = gs_group
    data["group_settings"] = gs
    _write_toml(_CONFIG_DIR / "adapter_config.toml", data)
    _trigger_reload()
    msg = gs_group.get("welcome_msg", "")
    return f"群{gid} 欢迎语已更新: {msg}"


# 文件查看（过滤敏感信息）
def read_project_file(path):
    import os, re
    full = _ROOT / path
    try:
        full = full.resolve()
    except Exception:
        return "路径无效"
    if not str(full).startswith(str(_ROOT.resolve())):
        return "不允许"
    if not full.exists():
        return "文件不存在"
    if full.is_dir():
        return "\n".join(f"  {f.name}" for f in sorted(full.glob("*"))[:20])
    text = full.read_text(encoding="utf-8")
    for kw in ["key", "secret", "token", "password", "KEY", "SECRET", "TOKEN"]:
        text = re.sub(r"(?im)^(.*?" + re.escape(kw) + r"\s*[:=]\s*).+$", r"\1***", text)
    if len(text) > 3000:
        text = text[:3000] + "..."
    return text
