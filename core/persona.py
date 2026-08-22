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

from utils.format_lang import format_lang

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


def _me(cfg: dict) -> str:
    """称呼自己：默认 bot 名（幻梦）。"""
    return cfg.get("bot_name") or "幻梦"


# 每种情景对应的 lang.toml key（喵系 / 平实 两个版本）
_KIND_KEYS: dict[str, tuple[str, str]] = {
    KIND_TOOL_FAILED: ("tool_failed_meow", "tool_failed_plain"),
    KIND_PERMISSION: ("permission_meow", "permission_plain"),
    KIND_TIMEOUT: ("timeout_meow", "timeout_plain"),
    KIND_JSON_PARSE: ("json_parse_meow", "json_parse_plain"),
    KIND_DB_FALLBACK: ("db_fallback_meow", "db_fallback_plain"),
    KIND_AGENT_FAILED: ("agent_failed_meow", "agent_failed_plain"),
    KIND_REPLY_FAILED: ("reply_failed_meow", "reply_failed_plain"),
}


def persona_message(kind: str, *, detail: str = "") -> str:
    """返回某情景下、统一人格风格的功能句式。

    所有句式都贴合 bot_config [personality] 的软萌猫娘人设，不引入另一套客服/高冷人格。
    detail 可选追加具体信息（如工具名/错误片段），拼在句尾。
    """
    cfg = _config() or {}
    core = cfg.get("core", "")
    me = _me(cfg)

    keys = _KIND_KEYS.get(kind)
    if keys is None:  # KIND_NORMAL 及未知 → 空（正常回复走 LLM，不在此硬编码）
        base = ""
    else:
        key = keys[0] if persona_uses_meow(core) else keys[1]
        base = format_lang(f"persona.message.{key}", me=me)

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