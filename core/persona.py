"""
Phase 20 Part13：PersonaContext / ResponseStyle（Huanmeng 2.0）

统一所有"系统生成的"回复风格（fallback / error / permission / timeout / JSON 解析失败 /
DB 降级 / Agent 失败），避免散落硬编码另一套人格。

设计原则：
- 人格底色只来自 bot_config.toml [personality]（personality_core/side/identity）与
  skills/ 中已锁定的 persona_lock，这里不重新定义另一套人格。
- 本模块只提供"同一人格下的功能句式"，通过读取配置的称呼/语气标签，让所有
  系统回复与正常聊天共用同一套人设语气。
- 纯函数 / 无副作用，不调用 LLM，随时可独立测试。
"""
from __future__ import annotations

from typing import Optional

# 回复情景分类（与需求 Part13 对齐）
KIND_NORMAL = "normal"              # 正常回复（占位，实际走 LLM）
KIND_TOOL_FAILED = "tool_failed"    # 工具执行失败
KIND_PERMISSION = "permission"      # 权限拒绝
KIND_TIMEOUT = "timeout"            # 超时
KIND_JSON_PARSE = "json_parse"      # JSON 解析失败
KIND_DB_FALLBACK = "db_fallback"    # DB 降级
KIND_AGENT_FAILED = "agent_failed"  # Agent 失败
KIND_REPLY_FAILED = "reply_failed"  # 回复生成失败


def _config() -> Optional[dict]:
    """读取 bot_config 的 personality 配置（失败返回 None，不抛异常）。"""
    try:
        from core.config import get_config
        cfg = get_config()
        return {
            "bot_name": getattr(cfg, "bot_name", "幻梦") or "幻梦",
            "core": getattr(cfg, "personality_core", "") or "",
            "side": getattr(cfg, "personality_side", "") or "",
            "identity": getattr(cfg, "identity", "") or "",
        }
    except Exception:
        return None


def persona_uses_meow(core: str = "") -> bool:
    """判断配置人格是否带"喵结尾"（猫娘/软萌）。默认按幻梦设定为 True。"""
    if not core:
        return True
    return any(t in core for t in ("喵", "猫", "软萌", "可爱", "猫娘"))


def _tail(core: str) -> str:
    """按人格取句尾语气词。与核心人格一致，不额外引入未配置的语气。"""
    if persona_uses_meow(core):
        return "喵"
    return ""


def _me(cfg: dict) -> str:
    """称呼自己：默认 bot 名（幻梦）。"""
    return cfg.get("bot_name") or "幻梦"


def persona_message(kind: str, *, detail: str = "") -> str:
    """返回某情景下、统一人格风格的功能句式。

    所有句式都贴合 bot_config [personality] 的软萌猫娘人设，不引入另一套客服/高冷人格。
    detail 可选追加具体信息（如工具名/错误片段），拼在句尾。
    """
    cfg = _config() or {}
    core = cfg.get("core", "")
    tail = _tail(core)
    me = _me(cfg)

    t = tail and f"{tail}" or ""
    if kind == KIND_TOOL_FAILED:
        base = f"呜…这件事没办成{t}，我换个方式再试试？" if t else \
            "这件事没办成，我换个方式再试试。"
    elif kind == KIND_PERMISSION:
        base = f"这个操作只有主人或管理员能用{t}，不可以哦。" if t else \
            "这个操作只有主人或管理员能用。"
    elif kind == KIND_TIMEOUT:
        base = f"等太久啦，这件事暂时没完成{t}，稍后再试一次？" if t else \
            "等太久了，这件事暂时没完成，稍后再试一次。"
    elif kind == KIND_JSON_PARSE:
        base = f"{me}有点没听懂刚才的内容{t}，不过已经处理到一半了，稍等" if t else \
            f"{me}有点没听懂刚才的内容，不过已经处理到一半了，稍等。"
    elif kind == KIND_DB_FALLBACK:
        base = f"记录功能暂时在降级运行{t}，普通聊天不受影响" if t else \
            "记录功能暂时在降级运行，普通聊天不受影响。"
    elif kind == KIND_AGENT_FAILED:
        base = f"这个任务步骤有点复杂，我漏掉了某一步{t}，换个说法再说一次？" if t else \
            "这个任务步骤有点复杂，我漏掉了某一步，换个说法再说一次。"
    elif kind == KIND_REPLY_FAILED:
        base = f"呜…回复生成失败了{t}" if t else "呜…回复生成失败了。"
    else:  # KIND_NORMAL 及未知 → 空（正常回复走 LLM，不在此硬编码）
        base = ""

    if detail:
        return f"{base}\n{detail}"
    return base


# ── 单例便捷函数 ───────────────────────────────────────────

def tool_failed(detail: str = "") -> str:
    return persona_message(KIND_TOOL_FAILED, detail=detail)


def permission_denied(detail: str = "") -> str:
    return persona_message(KIND_PERMISSION, detail=detail)


def timeout_message(detail: str = "") -> str:
    return persona_message(KIND_TIMEOUT, detail=detail)


def json_parse_fallback(detail: str = "") -> str:
    return persona_message(KIND_JSON_PARSE, detail=detail)


def db_fallback(detail: str = "") -> str:
    return persona_message(KIND_DB_FALLBACK, detail=detail)


def agent_failed(detail: str = "") -> str:
    return persona_message(KIND_AGENT_FAILED, detail=detail)


def reply_failed(detail: str = "") -> str:
    return persona_message(KIND_REPLY_FAILED, detail=detail)