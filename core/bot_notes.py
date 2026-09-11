"""
幻梦的笔记本 — LLM 主动维护的长期记忆

与 modules/memory.py 的区别：
- memory.py 是被动摘要（自动从群聊提取压缩）
- 本模块是主动笔记（LLM 判断值得记才写，如群内关系/称呼/约定/黑话）

存储：data/notes/<chat_id>.md，一行一条 `- [MM-DD] 内容`
上限 MAX_NOTES 条，超出自动淘汰最旧（保留文件不无限膨胀）

用法：
- LLM 通过 calls 调 note 指令写入（见 skills/08_command_tools.md 规则 5b）
- 用户可用 .note 查看/增删
- 上下文注入见 core/pipeline.py 的 extra_info 组装
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from core.logger import get_logger

logger = get_logger("notes")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTES_DIR = DATA_DIR / "notes"

MAX_NOTES = 100          # 单群笔记上限，超出淘汰最旧
INJECT_LIMIT = 40        # 注入上下文的最大条数（省 token，最新的优先）
LINE_MAX = 200           # 单条笔记最大长度


def _note_file(chat_id) -> Path:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return NOTES_DIR / f"{chat_id}.md"


def _load_lines(chat_id) -> list[str]:
    f = _note_file(chat_id)
    if not f.exists():
        return []
    try:
        return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as e:
        logger.warning("读取笔记失败 %s: %s", chat_id, e)
        return []


def _save_lines(chat_id, lines: list[str]) -> None:
    try:
        _note_file(chat_id).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("写入笔记失败 %s: %s", chat_id, e)


def add_note(chat_id, text: str) -> str:
    """追加一条笔记，返回给调用方（LLM/用户）的提示语"""
    text = (text or "").strip().replace("\n", " ")
    if not text:
        return "笔记内容为空，没记"
    if len(text) > LINE_MAX:
        text = text[:LINE_MAX] + "…"

    lines = _load_lines(chat_id)
    tag = datetime.now().strftime("%m-%d")
    entry = f"- [{tag}] {text}"

    # 完全重复的不重复记
    if any(text in ln for ln in lines):
        return f"这条已经记过了：{text}"

    lines.append(entry)
    if len(lines) > MAX_NOTES:
        lines = lines[-MAX_NOTES:]
    _save_lines(chat_id, lines)
    logger.info("新增笔记 chat=%s: %s", chat_id, text[:60])
    return f"已记下：{text}"


def load_notes(chat_id, limit: int = INJECT_LIMIT) -> str:
    """加载笔记文本用于注入上下文（带序号，方便 LLM 用 fix/del 精确操作）；无笔记返回空串"""
    lines = _load_lines(chat_id)
    if not lines:
        return ""
    total = len(lines)
    start = max(1, total - limit + 1)
    shown = lines[start - 1:]
    header = (
        f"【你的笔记本·本群专属】共 {total} 条群内信息（只在当前群生效，其它群看不到）。"
        "新增直接写内容；改某条写 \"fix <序号> <新内容>\"；删某条写 \"del <序号>\"（都通过 calls 调 note，args 填这些）："
    )
    body = "\n".join(f"{i}. {ln.lstrip('- ')}" for i, ln in enumerate(shown, start=start))
    return header + "\n" + body


def list_notes(chat_id, scope: str = "") -> str:
    """给用户看的笔记列表（scope 用于标明是哪个群的笔记，直观确认隔离）"""
    lines = _load_lines(chat_id)
    title = f"笔记本·{scope}" if scope else "笔记本"
    if not lines:
        return f"【{title}】还是空的~ 说「记一下xxx」我就会记下来"
    out = [f"【{title}】共 {len(lines)} 条："]
    out.extend(f"{i}. {ln.lstrip('- ')}" for i, ln in enumerate(lines, 1))
    return "\n".join(out)


def delete_note(chat_id, index: int) -> str:
    """按序号删除（序号来自 list_notes，从 1 开始）"""
    lines = _load_lines(chat_id)
    if not lines:
        return "笔记本是空的"
    if index < 1 or index > len(lines):
        return f"序号超出范围（当前 {len(lines)} 条）"
    removed = lines.pop(index - 1)
    _save_lines(chat_id, lines)
    return f"已删除：{removed.lstrip('- ')}"


def update_note(chat_id, index: int, new_text: str) -> str:
    """修改第 index 条笔记（序号来自 load_notes/list_notes，从 1 开始）"""
    new_text = (new_text or "").strip().replace("\n", " ")
    if not new_text:
        return "新内容为空，没改"
    if len(new_text) > LINE_MAX:
        new_text = new_text[:LINE_MAX] + "…"

    lines = _load_lines(chat_id)
    if not lines:
        return "笔记本是空的"
    if index < 1 or index > len(lines):
        return f"序号超出范围（当前 {len(lines)} 条，用 .note 查看）"
    old = lines[index - 1].lstrip("- ")
    tag = datetime.now().strftime("%m-%d")
    lines[index - 1] = f"- [{tag}] {new_text}"
    _save_lines(chat_id, lines)
    logger.info("修改笔记 chat=%s #%d: %s → %s", chat_id, index, old[:30], new_text[:60])
    return f"已改第{index}条：{old} → {new_text}"


def clear_notes(chat_id) -> str:
    lines = _load_lines(chat_id)
    n = len(lines)
    _save_lines(chat_id, [])
    return f"已清空 {n} 条笔记"


def note_count(chat_id) -> int:
    return len(_load_lines(chat_id))


async def cmd_note(args, user_id, group_id, sender_name, is_group, bot_kook):
    """笔记本指令 .note

    .note              查看笔记
    .note <内容>       手动记一条
    .note fix <序号> <新内容>  修改某条
    .note del <序号>   删除某条
    .note clear        清空（仅管理员）
    """
    chat_id = group_id if is_group else user_id
    scope = "本群" if is_group else "私聊"
    a = list(args or [])

    if not a:
        return list_notes(chat_id, scope=scope)

    head = a[0].lower()
    if head in ("del", "delete", "删除", "-"):
        if len(a) < 2 or not a[1].isdigit():
            return "用法: .note del <序号>（序号用 .note 查看）"
        return delete_note(chat_id, int(a[1]))
    if head in ("fix", "edit", "改", "修改"):
        if len(a) < 3 or not a[1].isdigit():
            return "用法: .note fix <序号> <新内容>（序号用 .note 查看）"
        return update_note(chat_id, int(a[1]), " ".join(a[2:]))
    if head in ("clear", "清空"):
        from core.config import get_config
        cfg = get_config()
        try:
            _uid = int(user_id)
            _gid = int(group_id) if group_id else 0
        except (ValueError, TypeError):
            _uid, _gid = 0, 0
        if not cfg.is_admin(_uid, _gid):
            return "清空笔记本需要管理员权限"
        return clear_notes(chat_id)

    # 其余情况：手动记一条
    return add_note(chat_id, " ".join(a))
