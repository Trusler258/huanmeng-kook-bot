"""
专用翻译能力模块
- 识别翻译意图（"翻译"、"你翻译"、"帮我翻"、"翻译一下"、"translate" 等）
- 优先翻译引用消息（quoted_msg），无引用则提取句内待翻译文本
- 一次 LLM 调用给出简明译文，不啰嗦、不泄漏 JSON
- 命中后由 pipeline 直接回复并结束，不进入泛聊/Agent 流程
"""

from __future__ import annotations

import re

from core.logger import get_logger

logger = get_logger("translate")

# ── 语言别名 → 翻译目标（英文名）────────────────────────────
_LANG_MAP = {
    "en": "English", "英文": "English", "英语": "English", "english": "English",
    "zh": "Chinese", "中文": "Chinese", "汉语": "Chinese", "chinese": "Chinese",
    "jp": "Japanese", "ja": "Japanese", "日文": "Japanese", "日语": "Japanese", "japanese": "Japanese",
    "kr": "Korean", "ko": "Korean", "韩文": "Korean", "韩语": "Korean", "korean": "Korean",
    "fr": "French", "法文": "French", "法语": "French", "french": "French",
    "de": "German", "德文": "German", "德语": "German", "german": "German",
}

# ── 翻译动词触发词 ───────────────────────────────────────────
_TRIGGER_RE = re.compile(
    r"(翻译|translate|翻成|翻一下|翻下|帮我翻|给翻译|翻译翻译|译一下|译成)",
    re.IGNORECASE,
)

# ── 从消息中提取目标语言（如 "翻译成英文" / "翻成日语" / "英文翻译" / "翻译 英文 xxx"）─
_LANG_IN_MSG_RE = re.compile(
    r"(?:翻译成|翻成|翻译为|译为|译成|翻译)(?:成|为|到|个)?\s*"
    r"(中文|汉语|英文|英语|日文|日语|韩文|韩语|法文|法语|德文|德语|en|zh|jp|ja|kr|ko|fr|de)"
    r"|(?:中文|汉语|英文|英语|日文|日语|韩文|韩语|法文|法语|德文|德语)\s*(?:翻译|译文)",
    re.IGNORECASE,
)


def _extract_lang(text: str) -> str | None:
    """从消息文本中提取目标语言（返回英文名），无则 None。"""
    for m in _LANG_IN_MSG_RE.finditer(text):
        for g in m.groups():
            if g and g.lower() in _LANG_MAP:
                return _LANG_MAP[g.lower()]
    return None


# 语言指示词（中文全称，用于从内联文本里剔除，避免混进待翻内容）
_LANG_WORD_RE = re.compile(
    r"(?:中文|汉语|英文|英语|日文|日语|韩文|韩语|法文|法语|德文|德语)\s*"
)


def _clean_translation_text(s: str) -> str:
    """去掉触发词与语言指示词，剩下即为待翻译文本。"""
    t = _TRIGGER_RE.sub("", s)
    t = _LANG_WORD_RE.sub("", t)
    t = re.sub(r"^(?:成|为|到|个|一下)\s*", "", t)  # 去掉残留的介词/语气词
    return t.strip()


def _strip_mentions(text: str) -> str:
    """去掉 KOOK 提及标记 (met)id(/met)。"""
    return re.sub(r"\(met\)[^)]*\(/met\)", "", text).strip()


def detect_translate_request(msg_content: str, has_quote: bool) -> dict | None:
    """检测是否为翻译请求。

    返回 {"lang": str|None, "text": str|None}；非翻译请求返回 None。
    - lang: 指定的目标语言（英文名），未指定为 None（调用方决定默认中文）
    - text: 句内内联待翻译文本（去掉触发词后），有引用消息时一律为 None（用引用内容）
    """
    s = _strip_mentions(msg_content or "")
    if not s:
        return None

    lang = _extract_lang(s)
    has_verb = bool(_TRIGGER_RE.search(s))

    if has_quote:
        # 有引用消息：出现"翻译"字样或点名了目标语言即视为翻译请求，内容取引用消息
        if has_verb or lang:
            return {"lang": lang, "text": None}
        return None

    # 无引用：需出现翻译动词，且去除动词/语言词后仍有内联待翻文本才触发
    if not has_verb:
        return None
    text = _clean_translation_text(s)
    if not text:
        return None
    return {"lang": lang, "text": text}


async def _do_translate(text: str, target_lang: str) -> str:
    """一次 LLM 调用返回简明译文；失败返回空串。"""
    from core.config import get_config
    from services.llm import call_llm
    try:
        result = await call_llm(
            model_cfg=get_config().judge_model,
            messages=[{
                "role": "user",
                "content": (
                    f"Translate the following text to {target_lang}. "
                    "Output ONLY the translation. No explanation, no quotation marks, "
                    "no additional text.\n\n"
                    f"{text}"
                ),
            }],
            max_tokens=1200,
            temperature=0.1,
        )
        return (result or "").strip().strip('"').strip("'").strip()
    except Exception as e:  # noqa: BLE001
        logger.error("翻译失败: %s", e)
        return ""


async def handle_translate_request(
    msg_content: str,
    quoted_msg: str,
    chat_id,
    user_id,
    is_group: bool,
) -> str | None:
    """pipeline 入口：处理翻译请求，返回应发送的译文文本。

    非翻译请求返回 None（调用方继续正常流程）；
    翻译请求返回格式化后的译文（含无法处理的提示）。
    """
    has_quote = bool(quoted_msg and quoted_msg.strip())
    req = detect_translate_request(msg_content, has_quote)
    if req is None:
        return None

    # 确定待翻译文本：优先引用消息，其次句内文本
    text = (quoted_msg or "").strip() if has_quote else (req.get("text") or "").strip()
    if not text:
        return "要翻译的内容在哪亚？可以引用一条消息，或直接把内容丢给我喵~"

    target = req.get("lang") or "Chinese"  # 默认译为中文
    translation = await _do_translate(text, target)
    if not translation:
        return "翻译失败了喵~ 要不换个时间再试试？"

    logger.info("翻译完成 [chat=%d] %s→%s: '%s'", chat_id, text[:20], target, translation[:30])
    return translation