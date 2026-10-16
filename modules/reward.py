"""
赞赏 / 赞助模块

指令：
    .赞赏                     发赞赏码图 + 说明（所有人可用）
    .赞赏 list                列出赞助过的人
    .赞赏 add <人> <金额> [备注]   记录一笔赞助（仅管理员）
    .赞赏 del <编号>           删除一条记录（仅管理员）

数据：data/rewards.json
    {"sponsors": [{"name": "张三", "amount": "30", "count": 2,
                   "date": "2026-09-14", "by": "10001", "note": ""}]}

**赞助名单常驻注入 system prompt**（services/llm.py::_build_system_text 调 sponsors_hint()），
让 bot 一直记得谁支持过它——这是用户明确要求的"以表感谢"。
名单为空时不注入（省 token）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from core.logger import get_logger

logger = get_logger("reward")

_ROOT = Path(__file__).resolve().parent.parent
SPONSORS_FILE = _ROOT / "data" / "rewards.json"
QRCODE_FILE = _ROOT / "data" / "images" / "reward_qrcode.png"

MAX_SPONSORS = 200          # 名单上限，防无限膨胀（常驻提示词会跟着变大）
MAX_HINT_ITEMS = 20         # 常驻提示词里最多列多少个名字


# ── 存储 ─────────────────────────────────────────────────────────

def _load() -> dict:
    if not SPONSORS_FILE.exists():
        return {"sponsors": []}
    try:
        with open(SPONSORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"sponsors": []}
        items = data.get("sponsors")
        if not isinstance(items, list):
            data["sponsors"] = []
        return data
    except Exception as e:
        logger.error("读取赞助名单失败: %s", e)
        return {"sponsors": []}


def _save(data: dict) -> None:
    """原子写：临时文件 + os.replace，避免写一半被读。"""
    SPONSORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SPONSORS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SPONSORS_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_sponsors() -> list[dict]:
    return _load().get("sponsors", [])


def _fmt_amount(amount: str) -> str:
    """金额展示：纯数字带 ¥，其他原样（支持"一杯奶茶"这类）。"""
    a = str(amount or "").strip()
    if not a:
        return ""
    try:
        num = float(a.replace("￥", "").replace("¥", "").strip())
        s = f"{num:.0f}" if num == int(num) else f"{num:.2f}"
        return f"¥{s}"
    except Exception:
        return a


def add_sponsor(name: str, amount: str, by: str = "", note: str = "") -> tuple[bool, str]:
    """记录一笔赞助。同名累加金额（能解析数字时），并累加次数。"""
    name = (name or "").strip()
    amount = (amount or "").strip()
    if not name:
        return False, "得告诉我是谁赞赏的喵～用法：`.赞赏 add <称呼> <金额>`"
    if len(name) > 30:
        name = name[:30]
    if len(amount) > 20:
        amount = amount[:20]

    data = _load()
    items = data["sponsors"]
    today = datetime.now().strftime("%Y-%m-%d")

    for it in items:
        if str(it.get("name", "")).strip().lower() == name.lower():
            # 同名：金额可相加就相加，否则覆盖为最新描述
            old_a, new_a = str(it.get("amount", "")), amount
            try:
                merged = float(old_a.replace("￥", "").replace("¥", "").strip()) + \
                         float(new_a.replace("￥", "").replace("¥", "").strip())
                it["amount"] = f"{merged:.0f}" if merged == int(merged) else f"{merged:.2f}"
            except Exception:
                it["amount"] = new_a
            it["count"] = int(it.get("count", 1)) + 1
            it["date"] = today
            it["by"] = str(by)
            if note:
                it["note"] = note[:50]
            _save(data)
            return True, (f"已更新「{name}」的赞助记录～"
                          f"累计 {_fmt_amount(it['amount'])}（{it['count']} 次）")

    if len(items) >= MAX_SPONSORS:
        return False, f"名单已经到上限（{MAX_SPONSORS} 人）了喵，先删几条旧记录吧"

    items.append({
        "name": name,
        "amount": amount,
        "count": 1,
        "date": today,
        "by": str(by),
        "note": note[:50] if note else "",
    })
    _save(data)
    return True, f"记住了！谢谢「{name}」{_fmt_amount(amount) or ''} 的支持～"


def remove_sponsor(index: int) -> tuple[bool, str]:
    data = _load()
    items = data["sponsors"]
    if index < 1 or index > len(items):
        return False, f"编号超出范围（当前 1~{len(items)}）"
    gone = items.pop(index - 1)
    _save(data)
    return True, f"已从名单移除「{gone.get('name', '')}」"


def sponsors_hint() -> str:
    """常驻 system 用的赞助名单片段。名单为空返回空串。"""
    items = list_sponsors()
    if not items:
        return ""
    lines = []
    for it in items[:MAX_HINT_ITEMS]:
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        amt = _fmt_amount(it.get("amount", ""))
        cnt = int(it.get("count", 1))
        tail = f"（{cnt} 次）" if cnt > 1 else ""
        lines.append(f"· {name} {amt}{tail}".rstrip())
    if not lines:
        return ""
    more = f"\n（另有 {len(items) - MAX_HINT_ITEMS} 位，名单见 .赞赏 list）" \
        if len(items) > MAX_HINT_ITEMS else ""
    return (
        "【赞赏名单】这些朋友赞助过我的开发，是让我能继续跑下去的人——"
        "遇到他们要格外热情，被问到/聊到时主动道谢，别提金额大小、也不要在群里反复念叨：\n"
        + "\n".join(lines) + more
    )


# ── 指令 ─────────────────────────────────────────────────────────

def _qrcode_cq() -> str:
    """赞赏码图片 CQ 码（复用 sender 的路径规范化，避免 file:/// 四斜杠坑）。"""
    try:
        from services.sender import build_local_image_cq
        return build_local_image_cq(str(QRCODE_FILE))
    except Exception:
        # ★ KOOK 适配：返回内联图片标记（sender 会拦截并先发图再发文字）
        return build_local_image_cq(str(QRCODE_FILE))


def _show_card() -> str:
    if not QRCODE_FILE.exists():
        logger.error("赞赏码图片缺失: %s", QRCODE_FILE)
        return "赞赏码图片找不到了喵…请管理员检查 data/images/reward_qrcode.png"
    n = len(list_sponsors())
    tail = f"\n已经有 {n} 位朋友支持过我了，谢谢你们喵～" if n else ""
    return (f"【赞赏码】幻梦的服务器和模型开销全靠大家的心意撑着喵\n"
            f"{_qrcode_cq()}\n"
            f"金额随意，一分也是心意，都拿去充模型了\n"
            f"赞助过的朋友会被我记在感谢名单里，一直记着～{tail}")


def _show_image_only() -> str:
    """仅发图（.赞赏 仅发图）：只回一张赞赏码，不带任何说明文字。

    为什么要这个：有人只想顺手把码要过去，被一段介绍文字跟着念一遍很烦；
    只发一张图干净利落（v2.3.6 用户要求）。
    """
    if not QRCODE_FILE.exists():
        logger.error("赞赏码图片缺失: %s", QRCODE_FILE)
        return "赞赏码图片找不到了喵…请管理员检查 data/images/reward_qrcode.png"
    return _qrcode_cq()


def _show_list() -> str:
    items = list_sponsors()
    if not items:
        return "感谢名单还是空的喵～第一个支持我的人会是谁呢"
    lines = [f"【赞赏名单】共 {len(items)} 位"]
    for i, it in enumerate(items, 1):
        name = str(it.get("name", "")).strip() or "匿名"
        amt = _fmt_amount(it.get("amount", ""))
        cnt = int(it.get("count", 1))
        cnt_txt = f" ×{cnt}" if cnt > 1 else ""
        note = str(it.get("note", "")).strip()
        note_txt = f"（{note}）" if note else ""
        lines.append(f"{i}. {name} {amt}{cnt_txt}{note_txt}".rstrip())
    lines.append("谢谢每一位支持过幻梦的朋友，我会一直记得～")
    return "\n".join(lines)


async def cmd_reward(args, user_id, group_id, sender_name, is_group, bot_qq) -> str:
    """赞赏码与赞助名单

    用法:
      .赞赏                       发送赞赏码（图 + 一段说明）
      .赞赏 仅发图                 只发一张赞赏码图，不带任何文字
      .赞赏 list                  查看赞赏名单
      .赞赏 add <称呼> <金额> [备注]  记录一笔赞助（管理员）
      .赞赏 del <编号>            移除一条记录（管理员）
    """
    from core.config import get_config

    sub = args[0].lower() if args else ""
    if not sub or sub in ("码", "qr", "code", "show"):
        return _show_card()

    # 仅发图：不带说明文字，只回一张码（参数名支持 仅发图 / 图 / 图片 / 纯图 / img / pic / only）
    if sub in ("仅发图", "只发图", "纯图", "图", "图片", "img", "image", "pic", "only", "photo"):
        return _show_image_only()

    if sub in ("list", "列表", "名单", "all"):
        return _show_list()

    cfg = get_config()
    admin = cfg.is_admin(user_id, group_id)

    if sub in ("add", "添加", "记", "记录"):
        if not admin:
            return "只有管理员能记赞赏名单喵～"
        if len(args) < 3:
            return ("用法：`.赞赏 add <称呼> <金额> [备注]`\n"
                    "例如：`.赞赏 add 张三 30 服务器续费`")
        name, amount = args[1], args[2]
        note = " ".join(args[3:]) if len(args) > 3 else ""
        ok, msg = add_sponsor(name, amount, by=str(user_id), note=note)
        if ok:
            logger.info("赞助名单新增/更新: %s %s by=%s", name, amount, user_id)
        return msg

    if sub in ("del", "delete", "删除", "移除"):
        if not admin:
            return "只有管理员能改赞赏名单喵～"
        if len(args) < 2 or not args[1].isdigit():
            return "用法：`.赞赏 del <编号>`（编号见 `.赞赏 list`）"
        ok, msg = remove_sponsor(int(args[1]))
        if ok:
            logger.info("赞助名单移除: #%s by=%s", args[1], user_id)
        return msg

    return ("用法：\n"
            "- `.赞赏`  看赞赏码（图 + 说明）\n"
            "- `.赞赏 仅发图`  只要一张码，不带文字\n"
            "- `.赞赏 list`  看赞赏名单\n"
            "- `.赞赏 add <称呼> <金额> [备注]`  记录赞助（管理员）\n"
            "- `.赞赏 del <编号>`  移除记录（管理员）")
