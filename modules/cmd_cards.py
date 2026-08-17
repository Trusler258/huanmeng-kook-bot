"""
指令原生 KOOK 卡片构建模块（Huanmeng Phase 21）

背景：大量指令返回纯文本，可读性差且样式混乱。这里把“指令纯文字输出”封装成
KOOK 原生卡片（card JSON），并按指令分别写 builder，内容字段用变量占位。

用法：
  builder 接收结构化数据变量，返回 KOOK card 数组（list[dict]）。
  指令函数收集数据 → 调 build_xxx(data) → 返回 "__CARD__:" + json.dumps(card)。
  路由层识别 __CARD__: 前缀后走 send_raw 发送，失败时回退纯文本。

通用组件：
  card()    : 卡片壳
  header()  : 顶栏标题
  kmd()     : KMarkdown 段落
  section() : 段落（可含缩略图/内联元素）
  div()     : 分隔线
  fields()  : 键值对列表（最多4列）
  btn()     : 按钮（需原生卡片消息才渲染）
"""
from __future__ import annotations

from typing import Iterable, Optional

CARD_PREFIX = "__CARD__:"


def _card(theme: str = "secondary", size: str = "lg") -> dict:
    return {"type": "card", "theme": theme, "size": size, "modules": []}


def card(theme: str = "secondary", size: str = "lg", modules: Optional[list] = None) -> list[dict]:
    """构造完整 KOOK 卡片数组 [ {type:card, ...} ]，供 send_raw 直接发送。

    theme: primary/secondary/success/danger/warning/info
    """
    c = _card(theme, size)
    if modules:
        c["modules"] = modules
    return [c]


def header(content: str) -> dict:
    """顶栏标题（纯文本，红色强调）。"""
    return {"type": "header", "text": {"type": "plain-text", "content": content}}


def kmd(content: str) -> dict:
    """KMarkdown 段落，支持加粗/链接/代码/引用等。"""
    return {"type": "section", "text": {"type": "kmarkdown", "content": content}}


def section(content: str, accessory: Optional[dict] = None) -> dict:
    """段落，可选 accessory（如一个小 image 缩略图）。"""
    mod = {"type": "section", "text": {"type": "kmarkdown", "content": content}}
    if accessory:
        mod["accessory"] = accessory
    return mod


def text_section(content: str, accessory: Optional[dict] = None) -> dict:
    """纯文本段落（plain-text）。"""
    mod = {"type": "section", "text": {"type": "plain-text", "content": content}}
    if accessory:
        mod["accessory"] = accessory
    return mod


def div() -> dict:
    """分隔线。"""
    return {"type": "divider"}


def fields(pairs: Iterable[tuple[str, str]]) -> dict:
    """键值对字段组（KOOK 1-4 列）。pairs: [(键, 值), ...]。"""
    elems = []
    for k, v in pairs:
        elems.append({
            "type": "field",
            "text": {"type": "kmarkdown", "content": f"**{k}**\n{v}"},
        })
    return {"type": "section", "modules": elems}


def button(value: str, text: str, theme: str = "primary") -> dict:
    """按钮元素（需放在 action-group 中才渲染）。"""
    return {
        "type": "button",
        "theme": theme,
        "value": value,
        "click": "return-val",
        "text": {"type": "plain-text", "content": text},
    }


def action_group(*btns: dict) -> dict:
    """按钮组。"""
    return {"type": "action-group", "elements": list(btns)}


def img(src_url: str) -> dict:
    """图片元素（URL 形式）。"""
    return {"type": "image", "src": src_url}


# ════════════════════════════════════════════════════════════
#  各指令 builder（分别写，接收结构化数据变量）
# ════════════════════════════════════════════════════════════

def build_info(d: dict) -> list[dict]:
    """.info 运行状态卡片。

    d: 由 cmd_info 收集的结构化数据变量（system/_runtime/connection/config/models/token）。
    """
    sys_ = d.get("system", {})
    rt = d.get("runtime", {})
    conn = d.get("connection", {})
    cfg = d.get("config", {})
    models = d.get("models", {})
    tok = d.get("token", {})

    modules = [header(f"🤖 {d.get('name', '')} 运行状态")]

    # 系统
    modules.append(kmd("**系统**"))
    sys_rows = [
        ("系统", f"{sys_.get('os', '?')}"),
        ("Python", f"{sys_.get('python', '?')}"),
    ]
    if sys_.get("memory"):
        sys_rows.append(("内存", sys_["memory"]))
    if sys_.get("cpu"):
        sys_rows.append(("CPU", sys_["cpu"]))
    if sys_.get("disk"):
        sys_rows.append(("磁盘", sys_["disk"]))
    modules.append(_two_col_fields(sys_rows))

    # 运行
    if rt:
        modules.append(div())
        modules.append(kmd("**运行**"))
        modules.append(_two_col_fields([
            ("运行时长", rt.get("uptime", "?")),
            ("消息数", rt.get("msgs", "?")),
            ("活跃会话", rt.get("chats", "?")),
            ("待办任务", rt.get("tasks", "?")),
        ]))

    # 连接
    if conn:
        modules.append(div())
        modules.append(kmd("**连接**"))
        modules.append(_two_col_fields([
            ("KOOK", conn.get("kook", "?")),
            ("频道缓存", f"{conn.get('cache', 0)} 个"),
        ]))

    # 配置
    modules.append(div())
    modules.append(kmd("**配置**"))
    modules.append(_two_col_fields([
        ("回复阈值", f"{cfg.get('reply_threshold', '?')}"),
        ("上下文长度", f"{cfg.get('context_length', '?')}"),
        ("私聊", "开" if cfg.get("private_chat") else "关"),
        ("图片识别", "开" if cfg.get("image_recog") else "关"),
        ("白名单群", f"{cfg.get('groups', 0)} 个"),
        ("调试", "开" if cfg.get("debug") else "关"),
    ]))

    # 模型
    modules.append(div())
    modules.append(kmd("**模型**"))
    mod_rows = []
    for label in ("回复", "快", "仲裁"):
        key = {"回复": "reply", "快": "cheap", "仲裁": "judge"}[label]
        if key in models:
            m = models[key]
            mod_rows.append((label, f"{m.get('name', '?')} @ {m.get('provider', '?')}"))
    if "vision" in models:
        v = models["vision"]
        mod_rows.append(("视觉", f"{v.get('name', '?')} ({'开' if v.get('enabled') else '关'})"))
    modules.append(_two_col_fields(mod_rows))

    # Token
    if tok:
        modules.append(div())
        modules.append(kmd("**Token 消耗**"))
        modules.append(_two_col_fields([
            ("今日", f"{tok.get('today_calls', 0)} 次 · {tok.get('today_tokens', 0):,} tok · ¥{tok.get('today_cost', 0):.4f}"),
            ("累计", f"{tok.get('total_calls', 0)} 次 · {tok.get('total_tokens', 0):,} tok · ¥{tok.get('total_cost', 0):.2f}"),
        ]))

    return card("secondary", "lg", modules)


def build_cost(d: dict) -> list[dict]:
    """.cost Token 消耗卡片。

    d: from cmd_cost（today/total 各含 calls/prompt/completion/cached/cost）。
    """
    t = d.get("today", {})
    total = d.get("total", {})
    modules = [
        header("💰 Token 消耗"),
        fields([
            ("今日", f"{t.get('calls', 0)} 次调用\n{t.get('prompt', 0):,} + {t.get('completion', 0):,} tokens\n¥{t.get('cost', 0):.4f}"),
            ("累计", f"{total.get('calls', 0)} 次调用\n{total.get('prompt', 0):,} + {total.get('completion', 0):,} tokens\n¥{total.get('cost', 0):.2f}"),
        ]),
    ]
    return card("secondary", "lg", modules)


def build_favlist(rows: list[tuple[str, str, int]], header: str = "💗 好感度列表") -> list[dict]:
    """.favlist 好感度列表卡片。

    rows: [(name, uid, value), ...] 已按 value 降序。
    """
    modules = [{"type": "header", "text": {"type": "plain-text", "content": header}}]
    if not rows:
        modules.append(kmd("当前没有好感度记录喵~"))
        return card("secondary", "lg", modules)

    lines = "\n".join(
        f"**{i + 1}. {name}** · {value}" for i, (name, uid, value) in enumerate(rows)
    )
    modules.append(kmd(lines))
    return card("secondary", "lg", modules)


def _two_col_fields(rows: list[tuple[str, str]]) -> dict:
    """两列键值对（info 内部用，兼容空值）。"""
    return fields(rows)