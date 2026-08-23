from __future__ import annotations

import asyncio
import re
from pathlib import Path

# ── 回复后处理：去重括号动作、修正语气分裂 ──
_PARREN_ACTION = re.compile(r'[(（][^)）]*[)）]')

def _clean_reply(text: str) -> str:
    """修复语气分裂：连续多个括号动作描述只保留第一个"""
    # 找末尾连续括号动作
    matches = list(_PARREN_ACTION.finditer(text))
    if len(matches) >= 2:
        # 检查是否连续（无文字间隔）
        consecutive = True
        for i in range(1, len(matches)):
            between = text[matches[i-1].end():matches[i].start()]
            if between.strip():
                consecutive = False
                break
        if consecutive:
            # 只保留第一个
            text = text[:matches[0].start()] + text[matches[0].start():matches[0].end()].strip()
    return text

from core.logger import get_logger
from core.config import get_config
from core.reply_split import split_reply_for_send
from utils.format_lang import format_lang
from modules.judge import should_respond
from modules.memory import (
    get_top_memories,
    maybe_save_memory,
    load_memories as _load_memories_for_context,
    search_msglog,
)
from modules.fav import update_fav, get_fav
from modules.commands import handle_command
from modules.search import auto_search_if_needed
from services.llm import generate_multi_reply, generate_multi_reply_with_tools
from services.sender import send_sentences, send_by_chat_type, send_raw_group, send_raw_user

logger = get_logger("pipeline")

# ------工具函数------
def _clean_name(name):
    return re.sub(r'[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]+', '', str(name))


# ------戳一戳------
async def handle_poke_event(sender_name, user_id, chat_id, is_group):
    from core.context_manager import get_context_mgr
    cfg = get_config()
    ctx = get_context_mgr()

    sender_name = _clean_name(sender_name)

    from modules.fav import ensure_fav
    ensure_fav(chat_id, user_id, is_group)

    system_msg = format_lang("poke.message", name=sender_name, bot_name=cfg.bot_name)
    ctx.append_to_context(chat_id, f"[系统] {system_msg}")
    logger.info("🐾 戳一戳回复流程启动: from=%s chat=%d", sender_name, chat_id)

    related_memories = await get_top_memories(system_msg, ctx.get_context(chat_id), chat_id=chat_id)
    fav_val = get_fav(chat_id, user_id, is_group)
    fav_info = f"当前{sender_name}对你的好感度：{fav_val}/100"

    extra_parts = []
    from datetime import datetime
    now = datetime.now()
    now_str = now.strftime("%Y年%m月%d日 %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    weekdays = "日一二三四五六"
    now_str += f" 周{weekdays[int(now.strftime('%w'))]}"
    extra_parts.append(f"当前时间：{now_str}")

    from modules.preset import get_preset
    active_preset = get_preset(chat_id)
    if active_preset:
        extra_parts.append(f"【系统注入指令 — 你必须严格遵守，优先级高于人设】\n{active_preset}")
    if related_memories:
        extra_parts.append(related_memories)
    extra_parts.append(fav_info)

    poke_rules = [
        "【戳一戳规则：只用 1 句简短回应，不要展开话题，不要超过 20 字】",
        "【禁止重复：绝对不要说摸头很舒服、摸摸头、被摸了之类的前一次用过的句式，每次必须想全新的回应】",
        "【随机语气：可以从疑惑、开心、害羞、吓一跳、嫌弃、淡定中随机选一种情绪回应】",
        "【禁止调用任何工具/指令/搜索，只输出纯文本回复】",
    ]

    try:
        from modules.op import get_mode, get_sleep_prompt_rule, get_narrative_prompt_rule
        mode = get_mode(chat_id)
        if mode == "sleeping":
            poke_rules = [get_sleep_prompt_rule(chat_id)]
        elif mode == "narrative":
            poke_rules = [get_narrative_prompt_rule()]
    except ImportError:
        pass
    extra_parts.extend(poke_rules)

    buffer_snapshot = list(ctx.get_buffer(chat_id))

    sentences, fav_change, llm_calls, face_cq, mood, mood_detail, action, at_qq, mode_switch, origin, actor, _ = await generate_multi_reply_with_tools(
        msg_history=ctx.get_context(chat_id),
        speaker_name=sender_name,
        current_msg=f"[系统] {system_msg}",
        bot_name=cfg.bot_name,
        system_prompt=cfg.system_prompt,
        reply_model=cfg.reply_model,
        is_group=is_group,
        extra_info="\n".join(extra_parts),
        max_tokens=None,
        user_id=user_id, group_id=chat_id if is_group else 0, bot_kook=cfg.bot_kook,
    )

    if sentences:
        # 静默去除 [FACE:xxx] 残留文本，LLM 不该输出这个
        sentences = [re.sub(r'\[FACE:[^\]]*\]?', '', s).strip() for s in sentences]
        sentences = [_clean_reply(s) for s in sentences]
        sentences = [s for s in sentences if s]
        if not sentences:
            sentences = ["喵~"]

        task = asyncio.create_task(send_sentences(
            sentences, chat_id, is_group,
            user_id=user_id if not is_group else None,
        ))
        ctx.set_active_send_task(chat_id, task)

        update_fav(chat_id, user_id, 1, is_group)
        logger.info("戳一戳回复完成: %d句 fav+1", len(sentences))

        await maybe_save_memory(system_msg, sentences[0], sender_name, chat_id, user_id, buffer_snapshot)


# ------消息处理主入口------
async def process_message(msg_type, msg_content, chat_id, sender_name, user_id, is_group, bot_kook,
                          raw_event=None, raw_message="", quoted_msg="", error_report=None,
                          is_mentioned=False, image_urls=None, quoted_image_urls=None,
                          **extra_kwargs):
    """消息处理主管道。
    注：image_urls / quoted_image_urls 由 dispatcher 提取 KOOK 附件后传入。
    **extra_kwargs 用于前向兼容：避免未来 dispatcher/enqueue 新增参数时
    process_message 签名不匹配直接抛出 TypeError 导致全量消息静默（P0 事故）。
    """
    from core.context_manager import get_context_mgr
    from core.plugin import get_pipeline_hooks
    cfg = get_config()
    ctx = get_context_mgr()

    sender_name = _clean_name(sender_name)

    # ── 图片/引用图片占位注入（dispatcher 已在入队前同步完成识图，此处兜底挂上下文标签）
    img_urls = image_urls or []
    qimg_urls = quoted_image_urls or []
    if img_urls:
        ctx.append_to_context(chat_id, f"[本条消息附带图片 {len(img_urls)} 张]")
        logger.info("附带图片已记录 [chat=%d] count=%d", chat_id, len(img_urls))
    if qimg_urls:
        ctx.append_to_context(chat_id, f"[被引用消息附带图片 {len(qimg_urls)} 张]")
        logger.info("引用附带图片已记录 [chat=%d] count=%d", chat_id, len(qimg_urls))
    if extra_kwargs:
        logger.debug("process_message 收到未识别扩展参数: %s", list(extra_kwargs.keys()))

    # 首次对话自动注册好感度
    from modules.fav import ensure_fav
    ensure_fav(chat_id if is_group else user_id, user_id, is_group)

    # ------清洗不可见字符------
    import re as _re
    if msg_type == "文字" and msg_content:
        _invisible = _re.compile(
            r'[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e'
            r'\u2060-\u2064\u2066-\u2069\ufeff]+'
        )
        cleaned = _invisible.sub('', msg_content)
        if cleaned != msg_content:
            logger.info("[chat=%d] 清洗不可见字符: %d → %d 字符", chat_id, len(msg_content), len(cleaned))
            msg_content = cleaned
        if not msg_content.strip():
            logger.info("[chat=%d] 消息全为不可见字符，跳过处理", chat_id)
            return

    # ------管理员提示词注入------
    if msg_type == "文字" and "{{" in msg_content:
        role_tag = cfg.get_user_tag(user_id)
        if role_tag == "admin":
            from modules.preset import extract_preset_from_message, set_preset, clear_preset
            preset_text, cleaned = extract_preset_from_message(msg_content)
            if preset_text:
                if preset_text.lower() == "reset":
                    cleared = clear_preset(chat_id)
                    reply = "提示词已重置喵~ (｡･ω･｡)" if cleared else "当前没有注入的提示词喵~"
                else:
                    set_preset(chat_id, preset_text)
                    reply = f"提示词已注入喵~ ({len(preset_text)}字) 🔒"
                if is_group:
                    await send_by_chat_type(reply, chat_id, is_group=True)
                else:
                    await send_by_chat_type(reply, chat_id, is_group=False, user_id=user_id)
                if cleaned:
                    msg_content = cleaned
                else:
                    return

    # ------插件管道预钩子（on_message）------
    _pre_hooks = get_pipeline_hooks().all_pre_hooks()
    if _pre_hooks:
        _msg_dict = {
            "msg_type": msg_type, "msg_content": msg_content,
            "chat_id": chat_id, "sender_name": sender_name,
            "user_id": user_id, "is_group": is_group,
            "bot_kook": bot_kook, "raw_message": raw_message,
            "quoted_msg": quoted_msg, "is_mentioned": is_mentioned,
            "image_urls": image_urls, "quoted_image_urls": quoted_image_urls,
        }
        for hook in _pre_hooks:
            try:
                result = await hook(_msg_dict)
                if isinstance(result, str):
                    await send_by_chat_type(result, chat_id, is_group=is_group,
                                           user_id=user_id if not is_group else None)
                    return
            except Exception:
                pass

    # ------引用消息注入------
    if quoted_msg:
        quote_line = f"[引用原文] {quoted_msg}"
        ctx.append_to_context(chat_id, quote_line)
        logger.info("📎 引用消息已注入上下文 [%d]: '%s'...", chat_id, quoted_msg[:50])

    # ------上下文记录------
    role_tag = cfg.get_user_tag(user_id, chat_id if is_group else 0)
    if not is_group and role_tag not in ("admin",):
        try:
            from modules.op import is_private_master
            if is_private_master(user_id):
                role_tag = "admin"
        except ImportError:
            pass
    display_name = cfg.get_display_name(user_id, chat_id)
    fav_val = get_fav(chat_id, user_id, is_group)

    # ★ 好感度 -100 → 直接忽略
    if fav_val <= -100:
        logger.warning("好感度-100忽略: uid=%d(%s) chat=%d fav=%d", user_id, sender_name, chat_id, fav_val)
        return

    context_line = f"[{role_tag}] {display_name}[fav={fav_val}]: {msg_content}"

    buffer_text = f"{sender_name}: {msg_content}"
    if quoted_msg:
        buffer_text = f"{sender_name} (回复「{quoted_msg[:80]}」): {msg_content}"
    ctx.append_to_buffer(chat_id, buffer_text)

    from modules.stm import add_entry as stm_add
    stm_add(chat_id, role_tag, f"{sender_name}: {msg_content}", sender_name)

    # Phase 11：记录到工作记忆，溢出时异步提炼为长期记忆（不阻塞响应）
    try:
        from core.memory_engine import get_memory_engine
        get_memory_engine().observe(
            chat_id, msg_content, author=sender_name, user_id=user_id,
            tag="bot" if msg_type == "bot" else "user",
        )
    except Exception:
        pass

    # ------指令拦截------
    # KOOK 统一前缀: . 触发指令（管理员权限在各 handler 内部用 cfg.is_admin 校验）
    import re as _re
    # KOOK KMarkdown 转义点归一化（用户发 `.luck` 可能收到 `\.luck`/`\\.luck`）
    _m = _re.match(r'^\\+\.', msg_content)
    if _m:
        msg_content = msg_content[len(_m.group(0)) - 1:]
    if msg_content.startswith("."):
        # 纯标点/省略号（.、...、。。、？？等）直接忽略
        if _re.match(r'^[.。．｡?？！\uff01~]+$', msg_content):
            return
        else:
            full_cmd = msg_content
            logger.info("指令拦截: '%s' from=%s", full_cmd[:40], sender_name)
            await _handle_command_route(full_cmd, user_id, chat_id, sender_name, is_group, bot_kook, raw_message=raw_message, raw_event=raw_event)
            return

    # 消息不以 . 开头，但文本中嵌入了已注册指令（如"你能不能执行 .plugin status"）
    # → 提取并真实执行，避免把命令意图交给 LLM 产生"假装执行"的幻觉
    from modules.commands import _extract_embedded_command
    embedded = _extract_embedded_command(msg_content)
    if embedded:
        full_cmd = embedded
        logger.info("指令拦截(嵌入): '%s' from=%s", full_cmd[:40], sender_name)
        await _handle_command_route(full_cmd, user_id, chat_id, sender_name, is_group, bot_kook, raw_message=raw_message, raw_event=raw_event)
        return

    if msg_type != "文字":
        logger.debug("非文字消息(type=%s)，不进入回复管道", msg_type)
        return

    # ★ 指令和非文字消息不写入 LLM 上下文（避免污染）
    # 只有进入回复管道的消息才写入 group_context
    ctx.append_to_context(chat_id, context_line)

    # ------回复判断------
    should_reply = False
    import re as _re
    # KOOK: is_mentioned 由 dispatcher 通过 (met)id(met) / mention 列表检测后传入
    # 兜底: 再检查文本中的 @bot_name / @bot_kook
    if not is_mentioned:
        is_mentioned = bool(_re.search(rf'@{bot_kook}(?!\d)', msg_content)) or \
                       bool(_re.search(rf'@{cfg.bot_name}', msg_content))
    logger.debug("回复判断: is_mentioned=%s is_group=%s bot_kook=%s content='%s'",
                 is_mentioned, is_group, bot_kook, msg_content[:40])
    group_setting = cfg.group_settings.get(chat_id, {}) if is_group else {}
    # KOOK: 全局默认 at_only（仅被 @ 时回复），可在 group_settings 中按频道关闭
    at_only = group_setting.get("at_only", True)
    custom_threshold = group_setting.get("reply_threshold")

    if not is_group:
        should_reply = True
        logger.debug("私聊消息 → 直接回复")
    elif at_only:
        should_reply = is_mentioned
        if is_mentioned:
            logger.info("@机器人检测(at_only) → 直接回复")
    elif is_mentioned:
        should_reply = True
        logger.info("@机器人检测 → 直接回复")
    elif is_group and re.search(r"@\S+", msg_content):
        logger.debug("@他人消息 [群%d] → SKIP", chat_id)
    else:
        from core.trace import span as _trace_span
        with _trace_span("judge"):
            should_reply = await should_respond(
                msg_content, msg_type, sender_name, chat_id,
                ctx.get_context(chat_id), cfg.bot_name, bot_kook,
                reply_threshold_override=custom_threshold,
            )

    if not should_reply:
        return

    # ------专用翻译能力：识别翻译意图 → 优先翻引用消息 → 一次简明译文------
    # 命中即直接回复并结束，不进入泛聊/Agent（避免只问不译、回复啰嗦）。
    try:
        from modules.translate import handle_translate_request
        _tr_reply = await handle_translate_request(
            msg_content, quoted_msg, chat_id, user_id, is_group)
        if _tr_reply is not None:
            await send_by_chat_type(_tr_reply, chat_id, is_group=is_group, user_id=user_id)
            return
    except Exception:
        pass  # 翻译模块异常不阻断正常流程

    # ------Phase 6 Part2: 轻量意图分类（非 AI）------
    # 走完整 Agent 路径前先记录意图；普通聊天可据此跳过多余 search 判断。
    try:
        from core.router import resolve_intent, needs_search_heuristic
        intent = resolve_intent(msg_content, is_group=is_group, is_mentioned=is_mentioned, role_tag=role_tag)
        _fast_search = intent in ("search", "realtime") or needs_search_heuristic(msg_content)
    except Exception:
        intent = ""
        _fast_search = True  # router 出错 → 走完整 pipeline（含搜索判断），不丢消息
    logger.debug("请求意图: intent=%s fast_search=%s msg='%s'", intent, _fast_search, msg_content[:30])

    # ------Phase 20 P0: 复杂度评估（与意图拆开）------
    # 决定回答深度/是否需要 Agent/输出长度/上下文预算。
    # 纯规则，不调用 LLM；失败保守回落 chat，不丢消息。
    try:
        from core.complexity import assess_complexity
        _complexity = assess_complexity(msg_content)
    except Exception:
        _complexity = None
    _cx_level = _complexity.level if _complexity else "chat"
    _context_scale = _complexity.context_scale if _complexity else 1.0
    _detail_hint = _complexity.detail_hint if _complexity else ""
    # Phase 20 Hotfix E：命令行/指令查询——即使未被归为 knowledge，也注入
    # "一轮直接给出具体命令"提示，避免模型只回"让我先看看格式"却从不交命令。
    # 搜索已自动触发并注入结果（见 _fast_search 逻辑），这里强制模型直接实答。
    try:
        from core.router import is_command_lookup
        if is_command_lookup(msg_content) and not _detail_hint:
            _detail_hint = (
                "\n【本次是'要指令/命令/写法'的查询，必须在一轮内直接给出具体可用的命令/代码/步骤】"
                "1. 若上下文已有搜索结果，直接基于它引用并给出最终命令，不要只说要'先看看/去找找'。"
                "2. 不要只说一句'我来帮主人找'然后停住——请在本条消息里就把完整命令/写法/参数贴出来。"
                "3. 若确实搜不到，给一个最合理的版本并说明版本差异，明确告知这是估计值而非编造。"
                "4. 保持人格语气（如猫娘的'喵~'），命令本身要有代码块标注，方便主人直接复制。"
            )
    except Exception:
        pass
    if _complexity and not _complexity.is_chat:
        logger.info("复杂度: level=%s score=%d scale=%.1f", _cx_level,
                    _complexity.score, _context_scale)

    # ------Phase 7 Agent 薄适配层------
    # 复杂任务走 Agent Planner+Executor（返回 True 表示已处理并回复，直接结束）；
    # 简单聊天 / 简单 command / 规划失败 → 返回 False，继续原 Fast Path，不改原逻辑。
    if intent != "command":
        try:
            from core.agent.gateway import try_handle_with_agent
            # Agent 传入最近对话历史，让"去查查/再搜下/是么"这类承接句能结合前文定主题，
            # 避免孤立消息 plan/搜索跑偏（如把"你去查查"误解成查用户本人）。
            recent_history = ctx.get_context(chat_id)[-8:] or []
            if await try_handle_with_agent(
                msg_content, user_id=user_id, chat_id=chat_id,
                sender_name=sender_name, is_group=is_group, bot_kook=bot_kook,
                intent=intent, recent_history=recent_history,
            ):
                return
        except Exception as _ae:
            logger.warning("Agent 适配层异常，回退原 pipeline: %s", _ae)

    # ------自忽略机制------
    from services.self_ignore import is_ignored, ignore_user, remaining_seconds
    if is_group and is_ignored(user_id):
        logger.info("用户%d在忽略列表中，跳过回复 (%ds后解除)", user_id, remaining_seconds(user_id))
        return


    # ------记忆检索+好感度+上下文组装------
    full_msg = f"[{role_tag}] {sender_name}发了: {msg_content}"
    from core.trace import span as _trace_span
    with _trace_span("memory"):
        related_memories = await get_top_memories(msg_content, ctx.get_context(chat_id), chat_id=chat_id)
    fav_val = get_fav(chat_id, user_id, is_group)

    arch_context = ""
    arch_keywords = ["版本", "更新", "更新日志", "架构", "能力", "配置", "changelog", "version", "模型", "model"]
    msg_for_arch = msg_content[-200:].lower()
    if any(kw in msg_for_arch for kw in arch_keywords):
        try:
            from core.config import get_architecture_context
            arch_context = get_architecture_context()
            logger.debug("架构上下文已注入 (%d chars)", len(arch_context))
        except Exception:
            pass

    from datetime import datetime
    now = datetime.now()
    now_str = now.strftime("%Y年%m月%d日 %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    weekdays = "日一二三四五六"
    now_str += f" 周{weekdays[int(now.strftime('%w'))]}"
    # Phase 9：上下文按类型分桶，交给 Context Engine 仲裁优先级与预算
    extra_buckets: dict[str, list[str]] = {
        "system": [], "task": [], "memory": [], "search": [], "conversation": [],
    }

    def _bx(kind: str, text: str) -> None:
        if text and kind in extra_buckets:
            extra_buckets[kind].append(text)

    _bx("system", f"当前时间：{now_str}")

    # 节假日信息
    try:
        from modules.holiday import get_today_holiday_text
        holiday_text = get_today_holiday_text()
        if holiday_text:
            _bx("system", holiday_text)
    except Exception:
        pass

    from modules.preset import get_preset
    active_preset = get_preset(chat_id)
    if active_preset:
        _bx("task", f"【系统注入指令 — 你必须严格遵守，优先级高于人设】\n{active_preset}")
    if related_memories:
        _bx("memory", related_memories)

    # Phase 6 Part4：msglog 回溯结果按 conversation 预算截断，防止把上下文挤爆
    if not related_memories or len(related_memories) < 300:
        try:
            with _trace_span("message_retrieval"):
                msglog_context = get_msglog_context(msg_content, ctx.get_context(chat_id), chat_id)
            if msglog_context:
                _bx("memory", msglog_context)
        except Exception:
            pass

    try:
        img_keywords = ["图", "照片", "截图", "表情", "什么", "是谁", "这是"]
        has_img_ref = any(k in msg_content[-20:] for k in img_keywords) or msg_content == "[图片]"
        has_img_in_recent = any("[图片]" in l for l in ctx.get_context(chat_id)[-5:] if isinstance(l, str))
        if has_img_ref or has_img_in_recent:
            from services.image_api import get_recent_image_descriptions
            recent_imgs = get_recent_image_descriptions(chat_id=chat_id, limit=2)
            if recent_imgs:
                img_lines = ["【本群最近图片描述（如果还没识别完则为空，不知道就直说不知道）】"]
                for img in recent_imgs:
                    img_lines.append(f"- {img.get('desc', '?')[:100]} (来自 {img.get('author', '?')})")
                _bx("memory", "\n".join(img_lines))
    except Exception:
        pass

    _bx("system", f"当前{sender_name}对你的好感度：{fav_val}/100")

    if is_group:
        at_list = _build_at_list(ctx.get_context(chat_id), cfg)
        if at_list:
            _bx("conversation", at_list)

    if arch_context:
        _bx("system", arch_context)

    if is_group:
        # ★ 认主修复：只有真正的主人(admin_qq)才以"主人"称呼；
        #    OP/群管理员是次要身份，绝不能当作"主人"对待。
        #    且"身份提示"只在当前发言人是主人或本群 OP 时才注入，
        #    避免误注入到普通用户对话、误导 LLM 把普通用户/OP 当主人称呼。
        group_ops = cfg.group_owners.get(chat_id, [])
        master_name = cfg.get_display_name(cfg.admin_qq)
        if user_id == cfg.admin_qq:
            _bx("system",
                f"【身份提示】当前与你对话的是你的主人{master_name}，"
                f"请用对主人的语气与态度。")
        elif group_ops and user_id in group_ops:
            op_name = cfg.get_display_name(user_id, chat_id)
            _bx("system",
                f"【身份提示】当前与你对话的是本群管理员/OP {op_name}，"
                f"请给予管理员应有的礼貌与尊重；但只有你的主人{master_name}才算'主人'，"
                f"不要称呼管理员为'主人'。")

    try:
        from modules.op import get_mode, get_sleep_prompt_rule, get_narrative_prompt_rule
        mode = get_mode()
        if mode == "sleeping":
            _bx("task", get_sleep_prompt_rule(chat_id))
        elif mode == "narrative":
            _bx("task", get_narrative_prompt_rule())
    except ImportError:
        pass

    # ── 用户画像注入 ──
    try:
        from core.user_profile import build_profile_text
        profile_text = build_profile_text(user_id)
        if profile_text:
            _bx("system", f"\n\n【发言者画像】\n{profile_text}")
    except ImportError:
        pass

    # Phase 9：把分桶的额外上下文交给 Context Engine 仲裁
    # （Retrieve→Rank→Compress→Budget→Inject），产出带预算的 extra_info，
    # 并记录各类 Token 消耗到 trace。
    from core.context_builder import build_context_from_parts, DEFAULT_TOKEN_BUDGETS
    from core.trace import record_context_tokens as _record_ctx_tokens
    buckets_joined = {k: "\n\n".join(v) for k, v in extra_buckets.items() if v}
    # Phase 20 P0：Context 按复杂度动态扩容（2200 不是硬上限）。
    # 复杂任务/知识问题放大各段预算，避免关键 context 被截断导致回答被压短。
    _ctx_budgets = None
    if _context_scale > 1.0:
        _ctx_budgets = {k: int(v * _context_scale) for k, v in DEFAULT_TOKEN_BUDGETS.items()}
    built_extra = build_context_from_parts(buckets_joined, budgets=_ctx_budgets,
                                           with_headers=False)
    extra_info = built_extra.text
    # 注入复杂度详细提醒（推到 extra_info 末尾，让 LLM 知道要展开回答）
    if _detail_hint:
        extra_info = (extra_info + _detail_hint) if extra_info else _detail_hint
    if built_extra.dropped:
        logger.debug("extra_info 按预算丢弃 %d 项: %s",
                     len(built_extra.dropped), built_extra.dropped[:5])
    _record_ctx_tokens(built_extra.stats.to_dict())
    if extra_info:
        logger.info("额外信息: %d字 (memory=%d search=%d)",
                    len(extra_info), built_extra.stats.memory_tokens,
                    built_extra.stats.search_tokens)

    # ------错误报告处理------
    if error_report:
        logger.info("🔧 检测到错误报告，临时隔离上下文...")
        from modules.error_report import build_error_report_prompt
        full_msg = build_error_report_prompt(sender_name=sender_name, log_content=error_report, original_msg=msg_content)
        msg_history_for_llm = []
        extra_info_for_llm = ""
        ctx.append_to_context(chat_id, f"[错误报告] {sender_name} 上传了 Minecraft 错误报告，请求分析")
        logger.info("🔧 上下文已隔离（旧上下文保留，LLM 调用暂不使用）")
    else:
        msg_history_for_llm = ctx.get_context(chat_id)
        extra_info_for_llm = extra_info

    # ------系统提示词+工具代理------
    # cfg.system_prompt 结构: # 核心人格\n{core}\n---\n# 侧面人格\n{side}\n---\n# 固定身份\n{identity}\n---\n{self_awareness}
    # persona 注入策略（全替换模式）：
    #   - per-user persona dict {core, side, identity} 非空 → JSON 编码后用 PERSONA::: 标记，
    #     _build_system_text 检测到后用三段构造 header，禁用 face_lib/private_tone/play_mode
    #   - 保留 format_rules/command_tools/fav/anti_repeat（功能段）
    #   - 同步设置记忆 override：私聊有 persona 时用专属记忆文件
    #   - 无 per-user persona 时回退 [private_persona] 基底，再回退默认
    system_prompt_for_llm = cfg.system_prompt
    if not is_group:
        try:
            from modules.op import get_persona, get_persona_memory_id
            from modules.memory import set_persona_override
            custom = get_persona(user_id, cfg.private_persona_version)
            if custom:
                # 设置记忆覆盖（persona 专属记忆文件）
                memory_id = get_persona_memory_id(user_id)
                set_persona_override(user_id, memory_id)
                # persona JSON 编码后用 PERSONA::: 标记注入
                import json as _json
                persona_json = _json.dumps(custom, ensure_ascii=False)
                system_prompt_for_llm = f"PERSONA:::{persona_json}:::{cfg.system_prompt}"
                logger.debug("私聊人格注入(全替换) [%d]: core=%s...", user_id, custom.get("core", "")[:40])
            else:
                # 无 persona：清除记忆覆盖
                set_persona_override(user_id, None)
                if cfg.private_persona_core or cfg.private_identity:
                    # 全局 [private_persona] 基底：替换 core/side/identity，保留 self_awareness
                    private_core = cfg.private_persona_core or cfg.personality_core
                    parts = [f"# 核心人格\n{private_core}"]
                    if cfg.private_persona_side:
                        parts.append(f"# 侧面人格\n{cfg.private_persona_side}")
                    ident = cfg.private_identity or cfg.identity
                    parts.append(f"# 固定身份\n{ident}")
                    parts.append(cfg._build_self_awareness())
                    system_prompt_for_llm = "\n---\n".join(parts)
                    logger.debug("私聊使用 [private_persona] 基底")
        except ImportError:
            pass

    # ── Phase 9：System Context（Stable + Dynamic Capability）──
    # 把 system prompt 拆为 Stable（人格/格式/语气/好感度/玩模式）与
    # Dynamic Capability（指令列表 + 选中的 Skill），交给 Context Engine 仲裁。
    # 仅当检测到与消息相关的 Skill 时才加载其正文（Fast Path 简单聊天不触发）。
    from services.llm import build_system_sections as _build_sys_sections
    from core.context_builder import classify_system_sections as _classify_sections
    from core.context_builder import build_context_from_parts as _build_ctx
    from core.trace import record_skill as _record_skill
    import json as _json

    _custom_persona = None
    _personality = system_prompt_for_llm
    if system_prompt_for_llm.startswith("PERSONA:::"):
        _pparts = system_prompt_for_llm.split(":::", 2)
        if len(_pparts) == 3:
            try:
                _custom_persona = _json.loads(_pparts[1])
            except Exception:
                _custom_persona = None
            _personality = _pparts[2]

    # ── Phase 10：Capability 按需解析 ─────────────────────────
    # 用 CapabilityRouter 选出「当前请求相关」的能力，只注入相关的指令列表、
    # 加载命中的 Skill 正文，并把命中的 Tool Schema 交给 FC（不再全量塞给模型）。
    # 普通聊天（intent=chat/空）或 router 失败时回退到最小核心能力，避免上下文膨胀。
    from core.capability.loader import get_capability_loader as _get_cap_loader
    _cap_loader = _get_cap_loader()
    try:
        _cap_resolved = _cap_loader.resolve(msg_content, intent, is_group=is_group)
        _caps = _cap_resolved["caps"]
        skill_text = _cap_resolved["skill_text"]
        fc_schemas = _cap_resolved["fc_schemas"]
        for _c in _cap_resolved["caps"]:
            if _c.category == "skill":
                _record_skill(_c.name, selected=True, loaded=True)
        # Phase 20 Part5：把 Capability 解析统计写入 trace，供审计 caps=0/tools=0。
        # 无能力匹配时正常记录 0，不静默失败。
        try:
            from core.trace import record_capability_stats as _record_cap_stats
            _record_cap_stats({
                "capabilities_discovered": len(_caps),
                "capabilities_selected": len([c for c in _caps if getattr(c, "selected", False)]),
                "tools_available": len(fc_schemas),
            })
        except Exception:
            pass
    except Exception as _ce:
        logger.warning("Capability 解析失败，回退核心能力: %s", _ce)
        _caps = []
        skill_text = ""
        fc_schemas = []
        try:
            from core.trace import record_capability_stats as _record_cap_stats
            _record_cap_stats({"capabilities_discovered": 0, "capabilities_selected": 0,
                               "tools_available": 0, "error": str(_ce)[:120]})
        except Exception:
            pass

    sys_sections = _build_sys_sections(cfg.bot_name, _personality, is_group, _custom_persona,
                                       capabilities=_caps)
    stable_sec, dynamic_sec = _classify_sections(sys_sections)

    # 动态能力：按路由选中的指令列表 + 命中的 Skill（预算内，可丢弃）
    cmd_list = dynamic_sec.get("cmd_list", "")

    _stable_part = "\n\n".join(v for v in stable_sec.values() if v)
    _essential = "\n\n".join(p for p in (_stable_part, cmd_list, dynamic_sec.get("command_tools", "")) if p)
    _sys_parts = {"system": _essential}
    if skill_text:
        _sys_parts["skill"] = skill_text
    # Phase 20 P0：system 段预算也按复杂度扩容，避免 Stable 人格被截断
    _sys_budgets = None
    if _context_scale > 1.0:
        _sys_budgets = {k: int(v * _context_scale) for k, v in DEFAULT_TOKEN_BUDGETS.items()}
    sys_built = _build_ctx(_sys_parts, budgets=_sys_budgets, with_headers=False)
    system_text_override = sys_built.text
    logger.info("system_context: %d字 caps=%d tools=%d (system=%d skill=%d)",
                len(system_text_override), len(_caps), len(fc_schemas),
                sys_built.stats.system_tokens, sys_built.stats.skill_tokens)
    _record_ctx_tokens(sys_built.stats.to_dict())

    # 工具预选/执行代理已删除：inject_tool_system / try_tool_select / get_tool_status
    # 三个函数在 core/tools.py 中不存在，ImportError 被静默吞掉，整段是死代码。
    # 工具选择/执行由 generate_multi_reply_with_tools 中的 FC Agent 全权处理。

    # ------Agent写作路由------
    try:
        from utils.writing import is_writing_request, generate_and_send_file
        if await is_writing_request(msg_content, msg_history_for_llm):
            logger.info("写作请求检测: from=%s", sender_name)
            handled = await generate_and_send_file(
                msg=full_msg if not is_group else msg_content,
                msg_history=msg_history_for_llm,
                speaker_name=sender_name,
                chat_id=chat_id,
                is_group=is_group,
                user_id=user_id,
            )
            if handled:
                logger.info("写作管道处理完成: chat=%d", chat_id)
                return
            logger.info("写作管道回退 → 走正常生成")
    except ImportError:
        pass

    # ------编程路由（长代码题直接走 write_code，不经过 FC）------
    import re as _re
    _CODE_HINTS = [
        r"使用\s*c\+\+", r"编程解决", r"写(个|代码|程序).*题",
        r"编写程序", r"#include", r"\.cpp", r"交互题",
        r"时间复杂度", r"std::", r"using namespace",
    ]
    _code_detected = any(_re.search(p, msg_content) for p in _CODE_HINTS)
    if _code_detected and len(msg_content) > 500:
        # 太长的题直接认怂，16岁看不懂
        if len(msg_content) > 2000:
            logger.info("编程请求过长 %d字，认怂", len(msg_content))
            from services.sender import send_group_msg, send_private_msg
            msg = "呜…这题好难喵，我看不懂~( ＞﹏＜ )"
            if is_group:
                await send_group_msg(msg, chat_id)
            else:
                await send_private_msg(msg, user_id)
            return
        logger.info("编程请求检测: from=%s len=%d → 走代码生成管道", sender_name, len(msg_content))
        try:
            from core.tools import _write_code
            lang = "c++" if any(k in msg_content.lower() for k in ("c++", "cpp", "#include")) else "python"
            if "javascript" in msg_content.lower() or "js" in msg_content.lower():
                lang = "javascript"
            result = await _write_code(
                language=lang, description=msg_content[:3000],
                user_id=user_id, group_id=chat_id, sender_name=sender_name,
                is_group=is_group, bot_kook=bot_kook,
            )
            logger.info("代码生成管道完成: chat=%d result=%s", chat_id, result[:80])
            return
        except Exception as e:
            logger.warning("代码生成管道失败: %s，回退正常生成", e)

    # ------文件创建/打包类请求 → 沙箱真实执行（根治"假装已打包"幻觉）------
    # 用户要求"创建/生成 N 个文件并打包/压缩"时，直接走沙箱执行管道：
    # LLM 生成脚本 → 沙箱真实运行 → 产物 zip 作为附件返回，绝不口头声称成功。
    # 非管理员触发会走 _run_code_tool 内的审批卡片。
    _FILE_OP_RES = [
        _re.compile(r'创建\s*[0-9一-十]+\s*个?.*(文件|\.md|\.txt|\.json|\.csv|\.py)'),
        _re.compile(r'生成\s*[0-9一-十]+\s*个?.*(文件|\.md|\.txt|\.json|\.csv|\.py)'),
        _re.compile(r'(创建|生成|写).*(文件|\.md|\.txt|\.json|\.csv).*(打包|压缩|zip)'),
        _re.compile(r'(打包|压缩).*\.(md|txt|json|csv|zip)'),
    ]
    if any(_r.search(msg_content) for _r in _FILE_OP_RES):
        logger.info("文件操作检测: from=%s len=%d → 走沙箱执行管道", sender_name, len(msg_content))
        try:
            from core.tools import _run_code_tool
            result = await _run_code_tool(
                {"language": "python", "code": "", "description": msg_content[:3000]},
                user_id, chat_id, sender_name, is_group, bot_kook,
            )
            from services.sender import send_group_msg, send_private_msg
            if is_group:
                await send_group_msg(result, chat_id)
            else:
                await send_private_msg(result, user_id)
            logger.info("文件操作沙箱管道完成: chat=%d", chat_id)
            return
        except Exception as e:
            logger.warning("文件操作沙箱管道失败: %s，回退正常生成", e)

    # ------自动搜索（用户说"搜索/查/搜"等关键词时，先搜再答）------
    # Phase 6 Part2 Fast Path：普通聊天（无搜索意图）跳过自动搜索判断，
    # 避免触发额外的模型判断 LLM 调用（这是简单聊天 5~10s 延迟的来源之一）。
    if _fast_search:
        try:
            with _trace_span("search"):
                # 承接句（"搜搜看吧?"无实体词）需结合前文确定搜索主题。
                _search_ctx = ""
                try:
                    _recent = ctx.get_context(chat_id)[-6:] or []
                    _search_ctx = "\n".join(
                        h for h in _recent if isinstance(h, str) and h.strip()
                    )[-400:]
                except Exception:
                    _search_ctx = ""
                search_result = await auto_search_if_needed(
                    msg_content, sender_name, user_id, chat_id, is_group,
                    context=_search_ctx,
                )
            if search_result:
                # Phase 9：搜索结果注入 search 桶并按预算重建 extra_info
                # （search 优先级最低、可丢弃；错误报告隔离时跳过注入）
                if error_report:
                    logger.debug("错误报告隔离，跳过搜索结果注入")
                else:
                    _bx("search", f"【搜索结果（必须基于此回答，不要编造）】\n{search_result}")
                    _rebuilt = build_context_from_parts(
                        {k: "\n\n".join(v) for k, v in extra_buckets.items() if v},
                        with_headers=False,
                    )
                    extra_info = _rebuilt.text
                    _record_ctx_tokens(_rebuilt.stats.to_dict())
                    extra_info_for_llm = extra_info
                    logger.info("自动搜索结果已注入 (%d字)", len(search_result))
        except Exception as _se:
            logger.warning("自动搜索失败: %s", _se)
    else:
        logger.debug("Fast Path: 普通聊天跳过自动搜索 (intent=%s)", intent)

    # ------JSON LLM生成------
    logger.info("开始生成回复: speaker=%s chat=%d", sender_name, chat_id)
    with _trace_span("llm"):
        sentences, fav_change, llm_calls, face_cq, mood, mood_detail, action, at_qq, mode_switch, origin, actor, _ = await generate_multi_reply_with_tools(
            msg_history=msg_history_for_llm, speaker_name=sender_name, current_msg=full_msg,
            bot_name=cfg.bot_name, system_prompt=system_prompt_for_llm, reply_model=cfg.reply_model,
            is_group=is_group, extra_info=extra_info_for_llm,
            max_tokens=(_complexity.output_max_tokens if _complexity else None),
            user_id=user_id, group_id=chat_id if is_group else 0, bot_kook=bot_kook,
            system_text_override=system_text_override,
            tools_override=fc_schemas,
            detail_hint=_detail_hint or (_complexity.detail_hint if _complexity else ""),
        )

    if not sentences:
        # Phase 20 Part13：回复失败提示统一走 persona，不硬编码另一套人格。
        try:
            from core.persona import reply_failed
            _fail_head = reply_failed()
        except Exception:
            _fail_head = "回复生成失败。"
        error_lines = [
            _fail_head,
            f"错误: LLM返回空内容",
            f"时间: {now.strftime('%H:%M:%S')}",
            f"对话者: {sender_name}",
            f"上下文: {len(msg_history_for_llm)}轮",
            "请联系服务器管理员解决。",
        ]
        await send_by_chat_type("\n".join(error_lines), chat_id if is_group else chat_id,
                               is_group=True if is_group else False,
                               user_id=user_id if not is_group else None)
        logger.warning("LLM 未返回有效句子，已发送失败提示")
        return

    # ------垃圾过滤------
    _ctx_pattern = re.compile(r'^\[(admin|friend|群友)\]\s+\S+:\s')
    filtered = []
    for s in sentences:
        if _ctx_pattern.match(s):
            logger.warning("LLM 回显上下文格式，已过滤: '%s'", s[:60])
            continue
        filtered.append(s)
    if not filtered:
        filtered.append(format_lang("bot.fallback_reply", name=cfg.bot_name))
    sentences = filtered
    sentences = [_clean_reply(s) for s in sentences]

    # actor 始终以真实发送者为准，不信任 LLM 输出
    if origin == "user" and (not actor or actor.get("qq") != user_id):
        actor = {"name": sender_name, "qq": user_id}

    # ------FILE文件处理------
    _file_re = re.compile(r'\[FILE:(.+?)\](.*?)\[/FILE\]', re.DOTALL)
    for i, s in enumerate(sentences):
        m = _file_re.search(s)
        if m:
            fname = m.group(1).strip()
            content = m.group(2).strip()
            if not fname.endswith('.txt'):
                fname += '.txt'
            import tempfile
            fpath = Path(tempfile.gettempdir()) / fname
            fpath.write_text(content, encoding='utf-8')
            logger.info("[FILE] 创建: %s (%d字)", fname, len(content))
            fpath_str = str(fpath).replace('\\', '/')
            sentences[i] = f"[CQ:file,file=file:///{fpath_str},name={fname}]"
            async def _clean():
                await asyncio.sleep(30)
                try: fpath.unlink()
                except: pass
            asyncio.create_task(_clean())

    # ------倒计时卡片兜底------
    # LLM 经常偷懒不生成卡片，检测用户消息有"倒计时X秒/分钟"但 LLM 没发 [CARD] 时自动补
    # 同时检查 LLM 回复（LLM 可能识别了倒计时意图但没生成卡片）
    _cd_re = _re.compile(r'倒计时\s*(\d+)\s*(秒|s|分钟|分|分钟|小时|h)', _re.IGNORECASE)
    # 更宽松的匹配：支持"60s""60秒""5分钟"等不带"倒计时"前缀的写法
    _cd_loose = _re.compile(r'(?<![\d:])(\d+)\s*(秒|s|分钟|分|小时|h)(?![\d])', _re.IGNORECASE)
    _cd_match = _cd_re.search(msg_content)
    _cd_source = "user_msg"
    if not _cd_match:
        # 从 LLM 回复里检测倒计时意图
        _llm_text = " ".join(sentences)
        _cd_match = _cd_re.search(_llm_text)
        if _cd_match:
            _cd_source = "llm_reply"
    if not _cd_match:
        # 从用户消息检测纯数字+单位（如"60s"），但要求消息很短（避免误匹配）
        _stripped_msg = msg_content.strip()
        # 去掉 @提及 后的内容
        _stripped_msg = _re.sub(r'\(met\)\w+\(met\)', '', _stripped_msg).strip()
        if len(_stripped_msg) <= 20:
            _cd_match = _cd_loose.search(_stripped_msg)
            if _cd_match:
                _cd_source = "user_loose"
    _has_card = any('[CARD]' in s for s in sentences)
    if _cd_match and not _has_card:
        num = int(_cd_match.group(1))
        unit = _cd_match.group(2).lower()
        if unit in ('秒', 's'):
            seconds = num
            mode = 'second'
        elif unit in ('分钟', '分'):
            seconds = num * 60
            mode = 'second'
        else:  # 小时/h
            seconds = num * 3600
            mode = 'hour'
        import json as _json
        _cd_obj = [{"type": "card", "theme": "secondary", "size": "lg", "modules": [
            {"type": "header", "text": {"type": "plain-text", "content": f"⏳ 倒计时 {num}{unit}"}},
            {"type": "countdown", "mode": mode, "endTime": f"__COUNTDOWN__:{seconds}"}
        ]}]
        _cd_card = f"[CARD]{_json.dumps(_cd_obj, ensure_ascii=False)}[/CARD]"
        # 清除 LLM 可能生成的裸 JSON / "给你发卡片" 等废话
        sentences = [s for s in sentences if '"type":"card"' not in s and '给你发' not in s and '卡片模块' not in s]
        if not sentences:
            sentences = [f"开始倒计时喵~"]
        sentences.append(_cd_card)
        logger.info("倒计时卡片兜底: %d%s → %ds mode=%s (source=%s)", num, unit, seconds, mode, _cd_source)
    elif _cd_match and _has_card:
        # LLM 生成了卡片但可能用了裸时间戳（没遵循占位符规则），修复
        num = int(_cd_match.group(1))
        unit = _cd_match.group(2).lower()
        if unit in ('秒', 's'):
            seconds = num
        elif unit in ('分钟', '分'):
            seconds = num * 60
        else:
            seconds = num * 3600
        _bare_ts_re = _re.compile(r'"endTime"\s*:\s*(\d{10,})\s*}')
        _fixed = False
        new_sentences = []
        for s in sentences:
            if '[CARD]' in s and _bare_ts_re.search(s):
                s = _bare_ts_re.sub(f'"endTime":"__COUNTDOWN__:{seconds}"}}', s)
                _fixed = True
            new_sentences.append(s)
        if _fixed:
            sentences = new_sentences
            logger.info("倒计时卡片修复: 裸时间戳 → __COUNTDOWN__:%ds", seconds)
        # 同时修复末尾多余的 ]（LLM 常写成 }]] 而非 }]）
        sentences = [s.replace('}][/CARD]', '[/CARD]').replace('}]][/CARD]', '[/CARD]') + '[/CARD]' if '[CARD]' in s and not s.rstrip().endswith('[/CARD]') else s for s in sentences]
        # 更精确：确保 [CARD]...}][/CARD]（单 card 数组正确闭合）
        _bracket_fix = _re.compile(r'(\}\])\s*\]\s*\[/CARD\]')
        sentences = [_bracket_fix.sub(r'\1[/CARD]', s) if '[CARD]' in s else s for s in sentences]

    # ------scan replies for inline .commands ------
    if sentences:
        new_lines = []
        _cmd_re = _re.compile(r'(?:^|\s)\.(\w+)(?:\s+\[?([^\]]*)\]?)?')
        for _line in sentences:
            _line = str(_line) if _line else ""
            _added = 0
            for _m in _cmd_re.finditer(_line):
                _cn = _m.group(1).strip()
                _ca = (_m.group(2) or "").strip()
                if _cn:
                    # Only intercept real commands, not random text matching the pattern
                    from modules.commands import COMMAND_MAP as _CM
                    if _cn in _CM:
                        llm_calls.append({"name": _cn, "args": _ca})
                        _added += 1
                        logger.info("从回复中自动提取CALL: .%s %s", _cn, _ca)
            if _added:
                # Strip out the command text to avoid sending it as literal text
                _cleaned = _cmd_re.sub("", _line).strip()
                if _cleaned:
                    new_lines.append(_cleaned)
            else:
                new_lines.append(_line)
        sentences = new_lines

    # ------CALL执行------
    executed_calls = []
    call_results = []
    call_texts = []  # 延迟执行的发文件类 CALL
    if llm_calls:
        for call in llm_calls:
            if not isinstance(call, dict):
                continue
            cmd_name = str(call.get("name", "")).strip().lstrip(".~")
            cmd_args = str(call.get("args", "")).strip()
            if not cmd_name:
                continue
            from modules.commands import COMMAND_MAP
            if cmd_name not in COMMAND_MAP:
                err_msg = f"指令 .{cmd_name} 不存在。\n请联系服务器管理员。"
                await send_by_chat_type(err_msg, chat_id if is_group else chat_id,
                                       is_group=True, user_id=None)
                logger.warning("JSON CALL 无效: %s", cmd_name)
                continue
            # 追踪：有人叫bot执行 → 用actor的QQ；bot自己执行 → 用bot_kook
            caller_id = user_id
            caller_name = sender_name
            if isinstance(actor, dict) and actor.get("qq"):
                caller_id = int(actor["qq"])
                caller_name = actor.get("name", sender_name)
            elif origin == "bot":
                caller_id = bot_kook
                caller_name = cfg.bot_name

            call_text = f".{cmd_name} {cmd_args}".strip()
            logger.info("JSON CALL: %s (by=%s origin=%s)", call_text, caller_name, origin)
            # write_code 类发文件指令延迟执行，等文字先发送
            _send_file_cmds = ("write_code", "search", "search_web", "s")
            if cmd_name in _send_file_cmds:
                call_results.append(None)  # 占位，稍后填充
                call_texts.append((call_text, len(call_results) - 1, caller_id, caller_name))
            else:
                try:
                    result = await handle_command(call_text, caller_id, chat_id, caller_name, is_group, bot_kook, raw_message)
                    call_results.append(result)
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    logger.warning("JSON CALL执行失败 [%s]: %s\n%s", cmd_name, e, tb)
                    err_msg = f"指令 .{cmd_name} 执行失败。\n错误: {str(e)[:200]}\n请联系服务器管理员。"
                    await send_by_chat_type(err_msg, chat_id if is_group else chat_id,
                                           is_group=True, user_id=None)
                    call_results.append(f"[CALL错误] {e}\n\n堆栈:\n{tb}")
            executed_calls.append((cmd_name, cmd_args))

    is_at_me = raw_message and (f"(met){bot_kook}(met)" in raw_message or f"[CQ:at,qq={bot_kook}]" in raw_message)
    if executed_calls and is_group and is_at_me:
        first_call_idx = -1
        for i, s in enumerate(sentences):
            if re.search(r'\[CALL:', s):
                first_call_idx = i
                break
        if first_call_idx > 0:
            logger.debug("丢弃 CALL 前的 %d 句闲聊（@优先）", first_call_idx)
            sentences = sentences[first_call_idx:]
    # CALL/FACE 标记清理直接在列表元素上做，保持 sentences 为 JSON 多句数组，
    # 不经过 " || ".join 字符串往返（消除竖线泄漏的历史遗留）。
    sentences = [re.sub(r'\[CALL:[^\]]+\]', '', s).strip() for s in sentences]
    if executed_calls:
        hints = []
        for c in set(name for name, _ in executed_calls):
            hints.append(c)
        call_hint = "、".join(hints)
        logger.info("CALL执行: %s", call_hint)
        ctx.append_to_context(chat_id, f"[系统] 已调用: {call_hint}")

    # ------表情处理------
    if not face_cq:
        # 静默去除 [FACE:xxx] 残留
        sentences = [re.sub(r'\[FACE:[^\]]*\]?', '', s).strip() for s in sentences]

    sentences = [s for s in sentences if s.strip()]
    # 通用分句：结构化标题优先（知识类回答按阶段分条），闲聊长句按自然段落兜底拆分，
    # 让 Fast Path 与 Agent 路径的分句行为一致（Phase20 HotfixF: 门卫把闲聊降回 Fast Path
    # 后仍能分句发送，不再粘成一条长消息）。
    sentences = list(split_reply_for_send("\n\n".join(sentences)))
    if not sentences:
        sentences = ["喵~"]
    _face_cq_for_later = face_cq

    # ------上下文回写------
    _context_reply = "\n".join(sentences)
    # ★ 标记简化写入上下文（避免图片标记污染 LLM 上下文）
    _context_reply = re.sub(r'\[img:file:[^\]]*\]', '[图片]', _context_reply)
    _context_reply = re.sub(r'\[img:https?://[^\]]*\]', '[图片]', _context_reply)
    _context_reply = re.sub(r'\[CARD\].*?\[/CARD\]', '[卡片]', _context_reply, flags=re.DOTALL)
    _context_reply = re.sub(r'\(met\)\w+\(met\)', '@', _context_reply)
    _context_reply = re.sub(r'\[CQ:[^\]]*\]', '[消息]', _context_reply)
    ctx.append_to_context(chat_id, f"{cfg.bot_name}: {_context_reply}")

    for s in sentences:
        _s_clean = re.sub(r'\s*\[系统\]\s*已调用:\s*\S+', '', s).strip()
        if _s_clean:
            ctx.append_to_buffer(chat_id, f"{cfg.bot_name}: {_s_clean}")

    # ------action动作------
    if action:
        action_text = f"({action})"
        if sentences:
            sentences[-1] = sentences[-1] + action_text
        else:
            sentences.append(action_text)

    # ------@处理：把 @QQ号 替换为 (met)ID(met) KMarkdown 格式------
    if at_qq and is_group:
        at_met = f"(met){at_qq}(met)"
        at_text = f"@{at_qq}"
        for i in range(len(sentences)):
            sentences[i] = sentences[i].replace(at_text, at_met)

    # ------Phase 20 Hotfix D：真正走 KMD 链路------
    # 发送前把残留 Markdown 结构归一化为 KOOK KMD 可渲染形式（# 标题→**加粗**，
    # 代码块围栏内原样保留）。确保用户要求 KMD/结构化排版时内容真的以 KMD 语法
    # 发出，而不是 LLM 口头承诺"我会用KMD"。
    try:
        from utils.kmd import normalize_kmd_text
        sentences = [normalize_kmd_text(s) for s in sentences]
    except Exception:
        pass  # KMD 归一化失败不阻塞发送（原样发出）

    # ------mode切换------
    if mode_switch and mode_switch in ("normal", "sleeping", "narrative"):
        try:
            from modules.op import _load_modes, _save_modes
            modes = _load_modes()
            if mode_switch == "normal":
                modes.pop(str(chat_id), None)
            else:
                modes[str(chat_id)] = {"mode": mode_switch, "since": __import__("time").time()}
            _save_modes(modes)
            logger.info("LLM主动切换模式: chat=%d → %s", chat_id, mode_switch)
        except Exception:
            pass

    # ------插件管道后钩子（on_reply）------
    _post_hooks = get_pipeline_hooks().all_post_hooks()
    if _post_hooks and sentences:
        _msg_dict = {
            "msg_type": msg_type, "msg_content": msg_content,
            "chat_id": chat_id, "sender_name": sender_name,
            "user_id": user_id, "is_group": is_group,
            "bot_kook": bot_kook, "raw_message": raw_message,
            "quoted_msg": quoted_msg, "is_mentioned": is_mentioned,
            "image_urls": image_urls, "quoted_image_urls": quoted_image_urls,
        }
        _filtered = []
        for s in sentences:
            for hook in _post_hooks:
                try:
                    modified = await hook(s, _msg_dict)
                    if modified is None:
                        continue
                    if modified == "":
                        s = None
                        break
                    s = modified
                except Exception:
                    pass
            if s is not None:
                _filtered.append(s)
        sentences = _filtered

    if not sentences:
        logger.info("插件后钩子拦截了全部回复 [chat=%d]", chat_id)
        return

    # ------发送------
    old_task = ctx.cancel_old_task(chat_id)
    if old_task:
        logger.debug("取消旧发送任务 chat=%d", chat_id)

    # 工具调用通知 → 追加到最后
    if executed_calls:
        call_hints = []
        for name, args_str in executed_calls:
            call_hints.append(f"[工具调用: {name} {args_str}]")
        sentences.append("\n".join(call_hints))

    task = asyncio.create_task(send_sentences(
        sentences, chat_id, is_group,
        user_id=user_id if not is_group else None,
    ))
    ctx.set_active_send_task(chat_id, task)

    if _face_cq_for_later:
        async def _send_face_after():
            await task
            await send_by_chat_type(_face_cq_for_later, chat_id if is_group else chat_id,
                                   is_group=True if is_group else False,
                                   user_id=user_id if not is_group else None)
        asyncio.create_task(_send_face_after())

    # ------CALL结果回发------
    if call_results:
        async def _send_call_results():
            await task
            # 等待文字全部发出后，再执行发文件类 CALL
            for call_text, idx, caller_id, caller_name in call_texts:
                try:
                    result = await handle_command(call_text, caller_id, chat_id, caller_name, is_group, bot_kook, raw_message)
                    call_results[idx] = result
                except Exception as e:
                    logger.warning("延迟CALL执行失败: %s", e)
                    call_results[idx] = f"[CALL错误] {e}"
            is_search_or_read = any(name in ("search", "read") for name, _ in executed_calls)
            for i, r in enumerate(call_results):
                if not r:
                    continue
                if isinstance(r, str) and r.startswith("__EQ_CARD__:"):
                    png = r.split(":", 1)[1]
                    cq = f"[img:file:{png.replace(chr(92), '/')}]"
                    await send_by_chat_type(cq, chat_id if is_group else chat_id,
                                           is_group=True if is_group else False,
                                           user_id=user_id if not is_group else None)
                elif not is_search_or_read:
                    if isinstance(r, str) and r.startswith("[CALL错误]"):
                        continue
                    short = r[:200] + "..." if len(r) > 200 else r
                    logger.info("CALL结果: %s", short[:80])

            if call_results[0]:
                effective_result = call_results[0]
                is_call_error = isinstance(effective_result, str) and effective_result.startswith("[CALL错误]")
                ctx_text = effective_result[:200] if not is_call_error else f"[执行失败] {effective_result[:200]}"
                ctx.append_to_context(chat_id, f"[系统] 调用结果: {ctx_text}")
                try:
                    from services.llm import call_llm as raw_llm
                    if is_search_or_read:
                        # Phase 20 Hotfix C：不再硬编码"你是幻梦，一只猫娘助手"，
                        # 复用当前 persona system（含 PERSONA::: 私聊人格注入），
                        # 保持人格与格式约束一致。
                        from services.llm import _build_system_text
                        _follow_persona = system_prompt_for_llm
                        _follow_custom = None
                        if _follow_persona.startswith("PERSONA:::"):
                            _pp = _follow_persona.split(":::", 2)
                            if len(_pp) == 3:
                                import json as _j
                                try:
                                    _follow_custom = _j.loads(_pp[1])
                                except Exception:
                                    _follow_custom = None
                                _follow_persona = _pp[2]
                        follow_sys = _build_system_text(cfg.bot_name, _follow_persona, is_group,
                                                       custom_persona=_follow_custom)
                    if is_call_error:
                        # Phase 20 Hotfix C：错误分支同样复用 persona system，避免 NameError
                        from services.llm import _build_system_text
                        # ★ 私聊 follow-up 也要注入 persona（修复人设割裂）
                        custom_persona = None
                        if not is_group:
                            try:
                                from modules.op import get_persona
                                custom_persona = get_persona(user_id, cfg.private_persona_version)
                            except ImportError:
                                pass
                        follow_sys = _build_system_text(cfg.bot_name, cfg.system_prompt, is_group,
                                                        custom_persona=custom_persona)
                        err_detail = effective_result.replace("[CALL错误]", "").strip()
                        prompt = (
                            "系统调用的功能执行失败。请用你的语气告诉用户操作失败，然后**原样附上**下面的报错信息（含堆栈）。\n"
                            "不要总结、不要改写堆栈，直接贴。\n"
                            f"报错信息:\n{err_detail[:2000]}"
                        )
                        max_t = 120
                    elif is_search_or_read:
                        prompt = (
                            f"上下文——用户 {sender_name} 刚才问：「{msg_content}」\n"
                            "下面是你搜索到的资料。请**直接回答用户的问题**，列出具体信息（地名、数字、时间等），不要模糊概括。\n"
                            "按 reply_schema JSON 格式输出（replies/fav/calls/face/mood/action）。\n"
                            "replies 3-5 句，每句一个事实要点。禁止说'搜索结果显示'、'根据资料'这类套话。\n"
                            "用你的正常语气和人设回复，加喵~或颜文字。\n"
                            f"搜索结果:\n{effective_result[:4000]}"
                        )
                        max_t = None  # 不限 token
                    else:
                        from services.llm import _build_system_text
                        # ★ 私聊 follow-up 也要注入 persona（修复人设割裂）
                        custom_persona = None
                        if not is_group:
                            try:
                                from modules.op import get_persona
                                custom_persona = get_persona(user_id, cfg.private_persona_version)
                            except ImportError:
                                pass
                        follow_sys = _build_system_text(cfg.bot_name, cfg.system_prompt, is_group,
                                                        custom_persona=custom_persona)
                        prompt = (
                            "上面是调用结果，用一句话自然回应。纯文本，不要JSON。\n"
                            f"结果: {effective_result[:500]}"
                        )
                        max_t = 200
                    follow = await raw_llm(cfg.reply_model, [
                        {"role": "system", "content": follow_sys},
                        {"role": "user", "content": prompt},
                    ], max_tokens=max_t, temperature=0.7, timeout=15.0)
                    if follow and follow.strip():
                        f_text = follow.strip()
                        # JSON 回复 → 解析并分段发送
                        if f_text.startswith('{') and '"replies"' in f_text:
                            try:
                                import json
                                parsed = json.loads(f_text)
                                if isinstance(parsed, dict) and "replies" in parsed:
                                    for sentence in parsed["replies"]:
                                        sentence = sentence[:3000].strip()
                                        if sentence:
                                            ctx.append_to_context(chat_id, f"{cfg.bot_name}: {sentence[:200]}")
                                            await send_by_chat_type(sentence, chat_id if is_group else chat_id,
                                                                   is_group=True if is_group else False,
                                                                   user_id=user_id if not is_group else None)
                                    return  # 已处理，跳过下面
                                else:
                                    f_text = str(parsed)
                            except Exception:
                                pass
                        # 纯文本回退
                        f_text = f_text[:3000]
                        f_text = re.sub(r'[\[［]fav:\s*[+-]?\d+[\]］]', '', f_text).strip()
                        ctx.append_to_context(chat_id, f"{cfg.bot_name}: {f_text[:200]}")
                        await send_by_chat_type(f_text, chat_id if is_group else chat_id,
                                               is_group=True if is_group else False,
                                               user_id=user_id if not is_group else None)
                    else:
                        # Phase 20 Hotfix C：最终总结 LLM 失败/为空时，
                        # 若工具本身已成功（如 write_code 已发送文件），
                        # 直接把工具的成功结果反馈给用户，绝不静默，
                        # 也不让用户误以为任务失败。
                        logger.warning("CALL follow LLM 为空，工具结果直发: %s", effective_result[:60])
                        _ok_text = str(effective_result).strip()
                        if _ok_text and not _ok_text.startswith("[CALL错误]"):
                            ctx.append_to_context(chat_id, f"{cfg.bot_name}: {_ok_text[:200]}")
                            await send_by_chat_type(_ok_text[:3000], chat_id if is_group else chat_id,
                                                   is_group=True if is_group else False,
                                                   user_id=user_id if not is_group else None)
                except Exception as e:
                    import traceback
                    logger.error("追加回复失败:\n%s", traceback.format_exc())
                    # Phase 20 Hotfix C：follow 抛异常（超时/网络等）时同样直发工具成功结果，
                    # 避免"文件已发出但用户只看到沉默/失败提示"。
                    try:
                        _ok_text = str(effective_result).strip()
                        if _ok_text and not _ok_text.startswith("[CALL错误]") \
                                and not any(k in _ok_text for k in ("失败", "出错", "超时")):
                            await send_by_chat_type(_ok_text[:3000], chat_id if is_group else chat_id,
                                                   is_group=True if is_group else False,
                                                   user_id=user_id if not is_group else None)
                            ctx.append_to_context(chat_id, f"{cfg.bot_name}: {_ok_text[:200]}")
                    except Exception:
                        pass
        asyncio.create_task(_send_call_results())

    # ------好感度------
    if fav_change != 0:
        update_fav(chat_id, user_id, fav_change, is_group)
        logger.info("好感度调整: %s %+d (chat=%d)", sender_name, fav_change, chat_id)

    # ------自动记忆------
    buffer_snapshot = list(ctx.get_buffer(chat_id))
    await maybe_save_memory(msg_content, sentences[0] if sentences else "", sender_name, chat_id, user_id, buffer_snapshot)

    logger.info("✅ 管道处理完成: %d句 sent chat=%d", len(sentences), chat_id)

    # ------Phase 20 Hotfix D: Fast Path 主题记录（供裸续说继承）------
    # 普通聊天（未走 Agent）讨论的主题也写入 continuation，这样用户随后说
    # "详细说说/继续/再详细点"（纯续说词、无新主题）时能继承**最近一次讨论的话题**，
    # 而不是更早的 Agent 任务。
    # 过滤：纯闲聊（哈哈/谢谢/嗯）不成为话题；短于 2 字不记录；指令/续说词本身不记录。
    try:
        from core.agent.executor import set_continuation
        from core.agent.planner import TaskConstraints
        from core.agent.gateway import _CONTINUATION_MARKERS as _cont_markers
        from core.complexity import _is_mostly_casual as _mostly_casual
        _topic = msg_content.strip()
        _compact = re.sub(r"[\s，。！？、,.!?~～…\-—_]+", "", _topic).lower()
        _looks_cont = any(m in _topic for m in _cont_markers) and len(_topic) <= 14
        if _topic and len(_topic) >= 2 and not _mostly_casual(_compact) and not _looks_cont:
            set_continuation(chat_id, user_id, {
                "goal": _topic,
                "accumulated": [],
                "constraints": TaskConstraints(),
                "plan_steps": [],
            })
            logger.debug("Fast Path 主题已记录: chat=%d topic='%s'",
                         chat_id, _topic[:30])
    except Exception:
        pass  # 主题记录失败不影响主流程

    # ── 后台提取用户画像（不阻塞）──
    asyncio.ensure_future(_async_extract_profile(user_id, sender_name, msg_content))


# ------用户画像后台提取------
async def _async_extract_profile(user_id: int, sender_name: str, msg: str):
    """后台异步提取用户画像，不阻塞主流程"""
    try:
        from core.user_profile import extract_from_message, update_profile
        extracted = await extract_from_message(user_id, sender_name, msg)
        if extracted:
            # 防幻觉：用户名/facts 不可能是长句子或指令
            for field in ("name", "facts"):
                val = extracted.get(field, "")
                if isinstance(val, list):
                    filtered = [v for v in val if isinstance(v, str) and len(v) < 20 and not any(
                        kw in v for kw in ("查询", "帮我", "我叫", "战绩", "域名", "搜索", "什么", "怎么", ".")
                    )]
                    if not filtered:
                        extracted.pop(field, None)
                    else:
                        extracted[field] = filtered
                elif isinstance(val, str) and val:
                    if len(val) > 15 or any(kw in val for kw in ("查询", "帮我", "域名", "搜索", "什么", "怎么")):
                        extracted.pop(field, None)
            if not extracted:
                return
            update_profile(user_id, extracted)
            from core.logger import get_logger
            get_logger("pipeline").info("画像更新: uid=%d new=%s", user_id,
                                       {k: extracted[k] for k in sorted(extracted.keys())[:3]})
    except Exception:
        pass


# ------指令路由------
async def _handle_command_route(text, user_id, group_id, sender_name, is_group, bot_kook, raw_message="", raw_event=None):
    from core.config import get_config
    cfg = get_config()
    result = await handle_command(text, user_id, group_id, sender_name, is_group, bot_kook, raw_message=raw_message, raw_event=raw_event)
    if result is None:
        return
    if result == "__SYS_TEST_CARD__":
        await _send_test_card(group_id if is_group else user_id, is_group, user_id)
        return
    if isinstance(result, str) and result.startswith("__EQ_CARD__:"):
        png_path = result.split(":", 1)[1]
        cq = f"[img:file:{png_path.replace(chr(92), '/')}]"
        if is_group:
            await send_by_chat_type(cq, group_id, is_group=True)
        else:
            await send_by_chat_type(cq, group_id, is_group=False, user_id=user_id)
        return
    if isinstance(result, str) and result.startswith("__CARD__:"):
        # 原生 KOOK 卡片（指令 builder 产出），失败回退纯文本
        await _send_cmd_card(result[len("__CARD__:"):], group_id if is_group else user_id,
                             is_group, user_id, fallback=None)
        return
    clean_result = re.sub(r'\[(admin|friend|群友)\]', '', result)
    if is_group:
        await send_by_chat_type(clean_result, group_id, is_group=True)
    else:
        await send_by_chat_type(clean_result, group_id, is_group=False, user_id=user_id)


# ------指令原生卡片发送------
async def _send_cmd_card(card_json: str, chat_id, is_group: bool, user_id, fallback=None):
    """发送指令 builder 产出的原生 KOOK 卡片。

    解析 card JSON，校验通过后走 send_raw；失败时若给出 fallback 纯文本则发送之，
    否则静默（不阻塞进程）。
    """
    try:
        import json as _json
        from services.delivery.card_formatter import validate_and_repair_card_json
        card_json = card_json.strip()
        ok, fixed_json, detail = validate_and_repair_card_json(card_json)
        if not ok:
            logger.warning("指令卡片 JSON 不合法: %s", detail)
            if fallback:
                await send_by_chat_type(fallback, chat_id, is_group, user_id if not is_group else None)
            return
        cards = _json.loads(fixed_json)
        if is_group:
            await send_raw_group(cards, chat_id)
        else:
            await send_raw_user(cards, user_id)
    except Exception as e:
        logger.warning("指令卡片发送异常: %s", e, exc_info=True)
        if fallback:
            try:
                await send_by_chat_type(fallback, chat_id, is_group, user_id if not is_group else None)
            except Exception:
                pass


# ------测试卡片------
async def _send_test_card(chat_id, is_group, user_id):
    from utils.format_lang import format_lang
    md = format_lang("testsys.card_markdown")
    card = [{
        "type": "card",
        "theme": "secondary",
        "size": "lg",
        "modules": [
            {"type": "section", "text": {"type": "kmarkdown", "content": md}},
            {"type": "action-group", "elements": [{
                "type": "button",
                "theme": "primary",
                "value": ".testok",
                "click": "return-val",
                "text": {"type": "plain-text", "content": format_lang("testsys.button_label")},
            }]},
        ],
    }]
    logger.info("发送测试卡片: chat=%d is_group=%s", chat_id, is_group)
    if is_group:
        await send_raw_group(card, chat_id)
    else:
        await send_raw_user(card, user_id)


# ------msglog搜索------
def get_msglog_context(current_msg, context, chat_id):
    try:
        if len(current_msg) < 3:
            return ""
        query_parts = [current_msg]
        for line in context[-3:]:
            content = line.split(": ", 1)[-1] if ": " in line else line
            if len(content) > 6:
                query_parts.append(content)
        query = " ".join(query_parts[-3:])
        # 回溯范围从 300 条扩大到 5000 条，覆盖几周/一个月前的相关对话
        return search_msglog(chat_id, query, limit=8, max_scan=5000)
    except Exception:
        return ""


def _build_at_list(context, cfg):
    import re
    seen = {}
    for line in reversed(context[-30:]):
        m = re.match(r'\[(admin|friend|群友)\]\s+(.+?):', line)
        if m:
            name = m.group(2).strip()
            for uid, n in cfg.qq_name_map.items():
                if n == name and uid not in seen:
                    seen[str(uid)] = name
                    break
        if len(seen) >= 8:
            break
    if not seen:
        return ""
    lines = ["【可 @ 的用户（用 (met)用户ID(met) 格式）】"]
    for uid, name in seen.items():
        lines.append(f"  {name}: ID={uid}")
    return "\n".join(lines)
