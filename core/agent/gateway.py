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

from core.agent.config import (
    AGENT_ENABLED,
    PLANNER_CONFIRM_MODEL,
    PLANNER_CONFIRM_MAX_LEN,
    PLANNER_CONFIRM_TIMEOUT,
)
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


async def _send_progress(text: str, chat_id: int, is_group: bool, user_id: int) -> None:
    """轻量进度提示：单条消息直接发送，失败静默（不抛进主流程）。"""
    try:
        from services.sender import send_by_chat_type
        await send_by_chat_type(text, chat_id, is_group, user_id)
    except Exception as e:
        logger.warning("Agent 进度提示发送失败: %s", e)


def _agent_entry_text(task: str) -> str:
    """Agent 模式入口提示文案：先正常陈述一句，再斜体输出 [Agent Mode] 任务。"""
    task = (task or "").strip()
    if len(task) > 50:
        task = task[:50] + "…"
    return f"正在处理任务，进入 Agent 模式...\n*[Agent Mode] 任务：{task or '（空）'}*"


# ── Phase 20 Hotfix F：Agent 入口 LLM 门卫 ──────────────────────────
# 明确强任务标记：命中即跳过门卫直接进 Agent（长任务、多步骤、产出物），不打扰。
_STRONG_TASK_MARKERS: tuple[str, ...] = (
    "帮我写", "帮我做", "帮我查", "帮我找", "帮我分析", "帮我整理", "帮我配置",
    "帮我调查", "帮我检查", "帮我看看", "帮我算", "帮我搜索", "帮我部署",
    "写一个", "写一个程序", "写代码", "运行", "执行一下", "分析一下", "整理一下",
    "总结一下", "对比一下", "修改一下", "翻译一下", "生成", "创建", "打包",
    "部署", "搭建", "实现", "研究一下", "介绍一下", "详细", "展开", "完整",
)


async def _should_confirm_agent_entry(msg: str, intent: str) -> bool:
    """是否需要对"是否进入 Agent"做一次轻量 LLM 二次确认。

    仅边界情况触发：规则已放行（should_plan=True），但消息较短（≤ 边界长度）且
    未命中明确强任务标记 → 回落 LLM 判断"是真复杂任务，还是普通聊天/承接上文"。
    明确复杂任务 / 长消息直接进入 Agent，不额外调 LLM，避免拖慢正常任务。
    """
    if intent == "command":
        return False
    text = (msg or "").strip()
    if len(text) > PLANNER_CONFIRM_MAX_LEN:
        return False
    low = text.lower()
    return not any(m in low for m in _STRONG_TASK_MARKERS)


async def _llm_confirms_agent(msg: str, intent: str) -> bool:
    """用一次极低成本 LLM（max_tokens≈5，只回 0/1）确认"用户这句话是否真需要进 Agent"。

    返回 True → 是复杂任务，应进 Agent；False → 普通聊天/承接上文，回退 Fast Path。
    任何异常/超时/解析失败都保守返回 True（不阻断已放行的规则判定，避免丢真实任务）。
    """
    try:
        from core.config import get_config
        from services.llm import call_llm
        cfg = get_config()
        model_cfg = None
        if PLANNER_CONFIRM_MODEL:
            try:
                model_cfg = cfg.get_model(PLANNER_CONFIRM_MODEL)
            except Exception:
                model_cfg = None
        if model_cfg is None:
            model_cfg = getattr(cfg, "reply_model", None)
        if model_cfg is None:
            return True

        prompt = (
            "判断用户这句话是否需要进入'Agent 模式'（Agent 会调用工具/搜索/执行代码"
            "来完成任务）。\n"
            "只回一个数字：0 表示这只是普通聊天/闲聊/承接上文的简单提问，不需要 Agent；"
            "1 表示这是需要调用工具、联网、执行代码、多步骤处理的任务。\n"
            "不要输出任何其他内容，只要 0 或 1。\n\n"
            f"用户消息：{msg[:100]}\n"
        )
        raw = await call_llm(
            model_cfg,
            [{"role": "user", "content": prompt}],
            max_tokens=5, temperature=0.0, timeout=PLANNER_CONFIRM_TIMEOUT,
        )
        raw = (raw or "").strip()
        logger.info("Agent 门卫判定 msg=%r raw=%r", msg[:40], raw)
        return "1" in raw and "0" not in raw
    except Exception as e:
        logger.warning("Agent 门卫失败，保守放行: %s", e)
        return True


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
        # Issue1：续说也进入 Agent → 先发入口提示再执行。
        await _send_progress(_agent_entry_text(msg), chat_id, is_group, user_id)
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

    # Phase 20 Hotfix F：规则放行后，"短句/含糊"边界消息再经一次轻量 LLM 确认，
    # 避免 "你会做吗" 这类承接上文的闲聊被规则(task)强拉进 Agent 后 LLM 编元认知废话。
    if await _should_confirm_agent_entry(msg, intent):
        if not await _llm_confirms_agent(msg, intent):
            logger.info("Agent: 门卫判定为普通聊天(%r)，回退 Fast Path", msg[:30])
            return False

    trace_id = get_trace_id()
    logger.info("Agent: 进入规划 pipeline trace=%s intent=%s msg='%s'",
                trace_id, intent, msg[:40])

    # Issue1：确认进入 Agent 模式后，先发一句入口提示再规划，避免"无感知直接进 agent"。
    await _send_progress(_agent_entry_text(msg), chat_id, is_group, user_id)

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


def _split_sentences(text: str, max_len: int = 3000, max_items: int = 10) -> list[str]:
    """把长文本拆成可发送的多句。

    Phase 20 Hotfix E（Issue2）：
    - 优先按"空行（段落 \n\n）"天然切分，各段独立成句发送。配合句间随机延迟，
      让多条短回复按自然节奏逐条发出，而不是粘成一条长消息；
    - 代码块围栏（``` ... ```）内部一律不切分，整体归属同一条消息；
    - 单个段落仍超长时，再按行做长度拆分。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 1) 按空行切段，围栏内部不切。
    paras: list[str] = []
    buf = ""
    in_fence = False
    for ln in text.split("\n"):
        stripped = ln.strip()
        is_fence_line = stripped.startswith("```")
        if is_fence_line:
            # 围栏标记行永远并入当前段（避免把 ``` 单独切成一段）
            in_fence = not in_fence
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if in_fence:
            # 代码块内部：完整累积，绝不按空行/长度切分
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if stripped == "":
            # 空行 = 段落分隔（仅在围栏外生效）
            if buf:
                paras.append(buf)
                buf = ""
            continue
        buf = (buf + "\n" + ln) if buf else ln
    if buf:
        paras.append(buf)

    # 2) 展开每个段落；超长段落再做行级拆分。
    out: list[str] = []
    for p in paras:
        if len(p) <= max_len:
            out.append(p)
        else:
            out.extend(_split_long_lines(p, max_len))
    return out[:max_items] or [text[:max_len]]


def _split_long_lines(text: str, max_len: int = 3000) -> list[str]:
    """按行 + 长度拆分（不破坏代码块围栏），供超长段落降级使用。"""
    lines = text.split("\n")
    out: list[str] = []
    buf = ""
    in_fence = False
    for ln in lines:
        stripped = ln.strip()
        is_fence_line = stripped.startswith("```")
        if is_fence_line:
            in_fence = not in_fence
            buf = (buf + "\n" + ln) if buf else ln
            continue
        if in_fence:
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