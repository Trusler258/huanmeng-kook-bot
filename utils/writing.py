"""
Agent 式写作管道
- 检测写作请求 → 独立系统提示词 → 生成完整内容 → 写入文件发送
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from core.logger import get_logger

logger = get_logger("writing")

# ★ 写作请求关键词（不含代码）
_WRITE_PATTERNS = [
    r"写[一]?[篇封首次条]",           # 写一篇/封/首/次/条
    r"写作文", r"写文章", r"写[封信]", r"写诗",
    r"写(个|一下|一段|一份)",         # 写个/一下 (会被代码过滤)
    r"帮我写", r"给你写", r"请写",
    r"不少于\d+字",                   # 不少于800字
    r"阅读.(?:下面|以下).*材料",     # 阅读下面的材料
    r"根据要求写作", r"根据以下要求",
    r"自拟标题", r"明确文体",         # 高考作文关键词
    r"字数.{0,3}\d+.{0,3}字",        # 字数800字左右
]

_WRITE_RE = re.compile("|".join(_WRITE_PATTERNS))

# 代码关键词：匹配到则不是写作请求
_CODE_KEYWORDS = re.compile(
    r"python|java|c\+\+|c#|golang|rust|typescript|javascript|html|css"
    r"|代码|程序|tkinter|flask|django|react|vue|node|api|贪吃蛇|窗口"
    r"|2048|游戏|小游戏|网页|写个|页面",
    re.IGNORECASE,
)


async def is_writing_request(msg: str, chat_history: list[str] | None = None) -> bool:
    """LLM 判断是写作/作文请求还是代码/聊天（排除代码生成和普通对话）"""
    # 快速过滤：完全不像写作请求的直接跳过
    if not _WRITE_RE.search(msg):
        return False

    from services.llm import call_llm
    from core.config import get_config
    cfg = get_config()

    history_text = ""
    if chat_history:
        recent = chat_history[-6:]  # 最近 6 条
        history_text = "\n".join(recent)

    prompt = (
        "判断用户意图类型。仅回复一个词：\n"
        "writing — 要求写文章/作文/诗歌/故事/邮件等长文本\n"
        "code — 要求写代码/程序/网页/脚本/小游戏\n"
        "chat — 闲聊、提问、命令等非创作类\n\n"
        f"最近对话:\n{history_text}\n\n"
        f"用户消息: {msg}\n\n"
        "类型:"
    )

    try:
        result = await call_llm(
            cfg.reply_model, [{"role": "user", "content": prompt}],
            max_tokens=10, temperature=0.1, timeout=5.0,
        )
        label = (result or "").strip().lower()
        logger.info("意图判断: '%s' → %s", msg[:40], label)
        return "writing" in label
    except Exception as e:
        logger.debug("意图判断失败, 回退关键词: %s", e)
        return not _CODE_KEYWORDS.search(msg)


# ★ 写作专用系统提示词（无 ||| 格式约束）
WRITING_SYSTEM_PROMPT = """你是一个写作助手。请根据用户的要求写完整的文章/信件/代码。
输出格式：第一行是标题。第二行空行。第三行开始是正文。
不要用|||分隔符，不要加任何解释或聊天语气。"""


async def generate_and_send_file(
    msg: str,
    msg_history: list[str],
    speaker_name: str,
    chat_id: int,
    is_group: bool,
    user_id: int = 0,
):
    """
    检测到写作请求后：
    1. 用写作专用 prompt 调用 LLM
    2. 输出直接写为 txt 文件
    3. 发送文件
    返回 True 表示已处理
    """
    from core.config import get_config
    from services.llm import call_llm

    cfg = get_config()

    # ★ 构建写作专用 messages
    history_text = "\n".join(msg_history[-5:]) if msg_history else ""
    full_prompt = f"最近聊天记录:\n{history_text}\n\n当前请求:\n{msg}"

    messages = [
        {"role": "system", "content": WRITING_SYSTEM_PROMPT},
        {"role": "user", "content": full_prompt},
    ]

    logger.info("写作管道启动: from=%s chat=%d len=%d", speaker_name, chat_id, len(msg))
    try:
        raw = await call_llm(cfg.reply_model, messages, max_tokens=4000, temperature=0.8, timeout=120.0)
    except Exception as e:
        logger.warning("写作管道 LLM 调用失败: %s", e)
        return False

    if not raw or len(raw.strip()) < 20:
        logger.warning("写作管道: LLM 返回内容过短 (%d 字)", len(raw) if raw else 0)
        return False

    # ★ 提取标题作为文件名
    lines = raw.strip().split("\n")
    title = lines[0].strip() if lines else ""
    # 清理标题中的非法文件名字符
    safe_title = title
    for ch in r'<>:"/\|?*':
        safe_title = safe_title.replace(ch, "")
    safe_title = safe_title.strip()[:40]  # 截断过长标题
    if not safe_title:
        import time
        safe_title = f"reply_{time.strftime('%Y%m%d_%H%M%S')}"

    fname = f"{safe_title}.txt"

    # 检测编程语言，给正确后缀
    lang_ext = {
        "python": "py", "py": "py", "python3": "py",
        "javascript": "js", "js": "js",
        "typescript": "ts", "ts": "ts",
        "html": "html", "htm": "html",
        "css": "css",
        "java": "java",
        "c++": "cpp", "cpp": "cpp", "cxx": "cpp",
        "c#": "cs", "csharp": "cs",
        "go": "go", "golang": "go",
        "rust": "rs", "rs": "rs",
    }
    import re
    lower = (title + raw[:200]).lower()
    for kw, ext in lang_ext.items():
        if re.search(rf'\b{re.escape(kw)}\b', lower):
            fname = f"{safe_title}.{ext}"
            break
    fpath = Path(tempfile.gettempdir()) / fname
    fpath.write_text(raw.strip(), encoding="utf-8")
    logger.info("写作管道: %s (%d 字)", fname, len(raw))

    # ★ 注入上下文：让 bot 知道自己写了什么
    body = lines[2:] if len(lines) > 2 else lines[1:] if len(lines) > 1 else []
    summary = " ".join(body[:3])[:120]  # 取正文前几句做摘要
    from core.context_manager import get_context_mgr
    ctx = get_context_mgr()
    ctx.append_to_context(chat_id,
        f"[系统] 你刚刚帮{speaker_name}写了一篇《{title}》，内容摘要: {summary}...")

    # ★ 发送
    from services.sender import send_by_chat_type
    fpath_str = str(fpath).replace("\\", "/")
    file_cq = f"[CQ:file,file=file:///{fpath_str},name={fname}]"
    tip = f"《{title}》写了 {len(raw)} 字，文件来了喵~"

    if is_group:
        await send_by_chat_type(tip, chat_id, is_group=True)
        await asyncio.sleep(0.5)
        await send_by_chat_type(file_cq, chat_id, is_group=True)
    else:
        await send_by_chat_type(tip, chat_id, is_group=False, user_id=user_id)
        await asyncio.sleep(0.5)
        await send_by_chat_type(file_cq, chat_id, is_group=False, user_id=user_id)

    # ★ 收尾回复（按你配置的人设定制）
    from services.llm import call_llm
    followup_prompt = (
        f"你刚刚帮{speaker_name}写了一篇《{title}》并发给了ta。"
        f"现在用1句简短自然的收尾，符合你一贯的说话风格，20字以内。"
    )
    try:
        followup = await call_llm(cfg.reply_model, [
            {"role": "system", "content": cfg.system_prompt},
            {"role": "user", "content": followup_prompt},
        ], max_tokens=60, temperature=0.8, timeout=15.0)
        if followup and followup.strip():
            await asyncio.sleep(0.5)
            if is_group:
                await send_by_chat_type(followup.strip(), chat_id, is_group=True)
            else:
                await send_by_chat_type(followup.strip(), chat_id, is_group=False, user_id=user_id)
            # 注入上下文
            ctx.append_to_context(chat_id, f"{cfg.bot_name}: {followup.strip()}")
    except Exception as e:
        logger.debug("写作收尾LLM调用失败: %s", e)

    # ★ 后台清理
    async def _clean():
        await asyncio.sleep(30)
        try: fpath.unlink()
        except: pass
    asyncio.create_task(_clean())

    return True
