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

import re
import time
from typing import Optional

from core.agent.config import AGENT_ENABLED
from core.agent.executor import (
    AgentContext, get_executor, has_continuation, get_continuation,
)
from core.agent.planner import (
    get_planner, extract_constraints, TaskConstraints,
)
from core.agent.skill_registry import get_skill_registry
from core.logger import get_logger
from core.trace import set_plan_summary, get_trace_id

logger = get_logger("agent.gateway")

# Phase 20 Hotfix D：续说意图的"纯命令词"。这些词单独出现（或只带极短修饰）
# 时才继承上一话题；若消息里还有实质内容（如"详细说说缓存命中率"），
# 说明用户明确给出了新主题，应优先绑定新主题而不是旧 continuation。
_CONTINUATION_MARKERS: tuple[str, ...] = (
    "继续", "再详细", "讲完", "说全", "说完整", "接着讲", "接着说",
    "继续说", "全部整完", "继续说完", "详细说说", "详细讲",
    "展开讲", "展开说说", "说详细点", "详细一点说", "多讲讲",
    "再展开", "详细展开", "展开一下", "一次说完", "一次给我",
    "全部整理", "全部给我", "一步到位", "全部一次", "一次说",
    "一次讲完", "一次性", "再讲讲", "详细点", "具体点", "展开来",
)


def _has_new_topic(msg: str) -> bool:
    """去掉续说意图词后，消息里是否还剩下实质内容（视为显式新主题）。

    例："详细说说缓存命中率" → 去掉"详细说说"剩"缓存命中率" → True；
        "继续" / "详细说说" → 剩空 → False（应继承旧话题）。
    """
    text = msg or ""
    for marker in sorted(_CONTINUATION_MARKERS, key=len, reverse=True):
        text = text.replace(marker, "")
    text = re.sub(r"[\s，。！？、,.!?~～…\-—_【】（）()<>《》\"'\"'「」]+", "", text)
    return len(text) >= 2


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

    constraints = extract_constraints(msg)

    # Phase 20 Part8：续说（"继续/再详细/一次说完/全部整理"）→ 基于上次已收集信息补全，
    # 不重复规划/工具，仅一次 LLM 总结，避免重复 LLM/Tool。
    # Phase 20 Hotfix C：detail_level=high（"详细说说/展开讲"）也视为续说意图——
    # 继承上一任务上下文并修改输出深度，而不是当作新问题重新规划。
    # Phase 20 Hotfix D：仅当消息是**纯续说词**（去掉"详细说说/继续/展开讲"等后
    # 无实质内容）才继承旧话题；若用户明确给出新主题（如"详细说说缓存命中率"），
    # 不继承旧 continuation，走正常规划绑定新主题，避免跳回更早的话题。
    if (constraints.is_continuation or constraints.is_one_shot or constraints.detail_level == "high") \
            and has_continuation(chat_id, user_id) \
            and not _has_new_topic(msg):
        continuation_ctx = _build_ctx(user_id, chat_id, sender_name, is_group, bot_qq, msg)
        result = await get_executor().try_continue_task(continuation_ctx,
                                                        constraints=constraints)
        if result is not None and result.final_text:
            await _deliver(result.final_text, chat_id, is_group, user_id,
                           sender_name, bot_qq)
            logger.info("Agent: 续说已处理 chat=%d final=%s",
                        chat_id, result.final_text[:40])
            return True
        # 无续说状态 → 回退原 pipeline

    planner = get_planner()
    # 规则判定：简单聊天/简单 command 直接放行，不触发任何 LLM 规划
    if not planner.should_plan(msg, intent, is_group):
        logger.debug("Agent: 无需规划(简单消息/intent=%s)，保持 Fast Path", intent)
        return False

    trace_id = get_trace_id()
    logger.info("Agent: 进入规划 pipeline trace=%s intent=%s msg='%s'",
                trace_id, intent, msg[:40])

    # 规划：失败 → fallback
    plan = await planner.plan(msg, constraints=constraints)
    if plan is None:
        logger.info("Agent: 规划失败，fallback 原 pipeline trace=%s", trace_id)
        set_plan_summary(planned=False, reason="plan_failed")
        return False

    # 执行：按 Plan 执行 Skill/Tool，得到最终回复
    ctx = _build_ctx(user_id, chat_id, sender_name, is_group, bot_qq, msg)
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


def _build_ctx(user_id: int, chat_id: int, sender_name: str, is_group: bool,
               bot_qq: int, msg: str) -> AgentContext:
    return AgentContext(
        user_id=int(user_id or 0),
        group_id=int(chat_id or 0) if is_group else 0,
        chat_id=int(chat_id or 0),
        sender_name=sender_name or "",
        is_group=bool(is_group),
        bot_qq=int(bot_qq or 0),
        original_msg=msg,
    )


async def _deliver(final_text: str, chat_id: int, is_group: bool, user_id: int,
                   sender_name: str, bot_qq: int) -> None:
    """发送 Agent 最终回复；按句分段发送并回写上下文/缓冲。"""
    from core.context_manager import get_context_mgr
    from core.config import get_config
    from services.sender import send_sentences

    ctx = get_context_mgr()
    cfg = get_config()

    # Phase 20 Hotfix B：二道防线 —— 即使上游传入的 final_text 含 JSON/```json 原文，
    # 也在此归一化为纯文本，绝不把原始 JSON 送进发送层。
    from services.llm import normalize_final_reply
    normalized = normalize_final_reply(final_text)
    if normalized is None:
        from core.persona import json_parse_fallback
        normalized = json_parse_fallback()
    final_text = normalized

    # Phase 20 Hotfix D：Agent 回复同样走 KMD 归一化（# 标题→**加粗**，代码块内原样），
    # 与 Fast Path 保持一致的"真正 KMD"行为。
    try:
        from utils.kmd import normalize_kmd_text
        final_text = normalize_kmd_text(final_text)
    except Exception:
        pass  # KMD 归一化失败不阻塞发送

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
    """把长文本拆成可发送的多句（按换行优先，再按句号）。

    Phase 20 Hotfix D：保留行内缩进（不再 strip 行），且不在 ``` 代码块
    围栏内部切分——代码块整体归属同一条消息，保证代码结构不被拆碎。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    lines = text.split("\n")
    out: list[str] = []
    buf = ""
    in_fence = False
    for ln in lines:
        stripped = ln.strip()
        is_fence_line = stripped.startswith("```")
        if is_fence_line:
            # 围栏标记行永远并入当前 buf（避免把 ``` 单独切成一段）
            in_fence = not in_fence
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if in_fence:
            # 代码块内部：完整累积，绝不按长度切分
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if len(buf) + len(ln) + 1 > max_len:
            if buf:
                out.append(buf)
            buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        out.append(buf)
    return out or [text[:max_len]]