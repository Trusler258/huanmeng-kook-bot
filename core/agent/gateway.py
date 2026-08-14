"""
Phase 7 Agent 适配层 gateway（Huanmeng 2.0）

作用：在 pipeline 里建立"薄适配层"，不把 Agent 逻辑塞进 process_message。
流程：should_plan(规则) → plan(LLM) → execute(Execution Loop) → 发送。
- 简单聊天 / 简单 command → should_plan=False，直接返回 False，走原 Fast Path；
- 复杂任务 → 生成 Plan 并执行；规划失败(plan=None) → 返回 False，回退原 pipeline；
- 执行后生成 final_text 并发送，返回 True（表示已处理，pipeline 直接 return）。

任何异常都不会向上抛破坏 pipeline：内部捕获并返回 False / 发兜底回复。
"""
from __future__ import annotations

import time
from typing import Optional

from core.agent.config import AGENT_ENABLED
from core.agent.executor import AgentContext, get_executor
from core.agent.planner import get_planner
from core.agent.skill_registry import get_skill_registry
from core.logger import get_logger
from core.trace import set_plan_summary, get_trace_id

logger = get_logger("agent.gateway")


async def try_handle_with_agent(
    msg: str,
    *,
    user_id: int,
    chat_id: int,
    sender_name: str,
    is_group: bool,
    bot_qq: int,
    intent: str = "",
) -> bool:
    """尝试用 Agent 处理复杂任务。返回 True=已处理并发出回复；False=回退原 pipeline。

    仅当 Agent 全局开关开启、且规则判定需要规划时，才会进入规划/执行。
    规划失败 / 执行无回复 / 未开启 → False（调用方保持原 Fast Path 不丢消息）。
    """
    if not AGENT_ENABLED:
        return False
    if not msg or not msg.strip():
        return False

    planner = get_planner()
    # 规则判定：简单聊天/简单 command 直接放行，不触发任何 LLM 规划
    if not planner.should_plan(msg, intent, is_group):
        logger.debug("Agent: 无需规划(简单消息/intent=%s)，保持 Fast Path", intent)
        return False

    trace_id = get_trace_id()
    logger.info("Agent: 进入规划 pipeline trace=%s intent=%s msg='%s'",
                trace_id, intent, msg[:40])

    # 规划：失败 → fallback
    plan = await planner.plan(msg)
    if plan is None:
        logger.info("Agent: 规划失败，fallback 原 pipeline trace=%s", trace_id)
        set_plan_summary(planned=False, reason="plan_failed")
        return False

    # 执行：按 Plan 执行 Skill/Tool，得到最终回复
    ctx = AgentContext(
        user_id=int(user_id or 0),
        group_id=int(chat_id or 0) if is_group else 0,
        chat_id=int(chat_id or 0),
        sender_name=sender_name or "",
        is_group=bool(is_group),
        bot_qq=int(bot_qq or 0),
        original_msg=msg,
    )
    result = await get_executor().execute(plan, ctx)

    # 发送最终回复 + 写上下文
    if result.final_text:
        await _deliver(result.final_text, chat_id, is_group, user_id,
                       sender_name, bot_qq)
        logger.info("Agent: 已处理并回复 chat=%d status=%s final=%s",
                    chat_id, result.status, result.final_text[:40])
        return True

    # 执行无回复 → 回退原 pipeline（不丢消息）
    logger.warning("Agent: 执行后无回复，回退原 pipeline trace=%s", trace_id)
    return False


async def _deliver(final_text: str, chat_id: int, is_group: bool, user_id: int,
                   sender_name: str, bot_qq: int) -> None:
    """发送 Agent 最终回复；按句分段发送并回写上下文/缓冲。"""
    from core.context_manager import get_context_mgr
    from core.config import get_config
    from services.sender import send_sentences

    ctx = get_context_mgr()
    cfg = get_config()

    # 拆成可发送的句子（按换行/句号），避免一次性超长
    sentences = _split_sentences(final_text)
    if not sentences:
        sentences = [final_text]

    task = None
    try:
        task = __import__("asyncio").create_task(send_sentences(
            sentences, chat_id, is_group,
            user_id=user_id if not is_group else None,
        ))
        ctx.set_active_send_task(chat_id, task)
    except Exception as e:
        logger.warning("Agent 发送失败: %s", e)
        return

    # 回写上下文（简化标记）
    _context_reply = final_text
    import re as _re
    _context_reply = _re.sub(r'\[img:file:[^\]]*\]', '[图片]', _context_reply)
    _context_reply = _re.sub(r'\[img:https?://[^\]]*\]', '[图片]', _context_reply)
    _context_reply = _re.sub(r'\[CARD\].*?\[/CARD\]', '[卡片]', _context_reply,
                             flags=_re.DOTALL)
    _context_reply = _re.sub(r'\(met\)\w+\(met\)', '@', _context_reply)
    _context_reply = _re.sub(r'\[CQ:[^\]]*\]', '[消息]', _context_reply)
    ctx.append_to_context(chat_id, f"{cfg.bot_name}: {_context_reply}")
    for s in sentences:
        s_clean = s.strip()
        if s_clean:
            ctx.append_to_buffer(chat_id, f"{cfg.bot_name}: {s_clean}")


def _split_sentences(text: str, max_len: int = 3000) -> list[str]:
    """把长文本拆成可发送的多句（按换行优先，再按句号）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out: list[str] = []
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > max_len:
            if buf:
                out.append(buf)
            buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        out.append(buf)
    return out or [text[:max_len]]