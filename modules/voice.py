"""
语音回复 .voice <对话> — 调用主 LLM 管道 → 并发生成 per-sentence instruct → 串行 TTS 合成 → 顺序发语音
- 基础音色 Serena(温柔清澈女声)
- 每句独立 instruct(LLM 根据该句文本+情绪生成)
- instruct 并发生成(1 次 LLM 延迟),GPU 合成串行(避免显存冲突)
- 私聊自动注入自定义人格
- 模型常驻第二台电脑(P106-100),通过 TCP 调用
"""

import asyncio
import json
import re
import uuid
from pathlib import Path

from core.config import get_config as _get_cfg
from core.context_manager import get_context_mgr
from core.logger import get_logger

logger = get_logger("voice")

DEFAULT_SPEAKER = "Serena"
SPEAKERS = ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
            "Ryan", "Aiden", "Ono_Anna", "Sohee"]


async def _generate_instruct(text: str, mood: str) -> str:
    """调 cheap_model 生成单句 instruct(尖细活泼 + 情绪微调)"""
    cfg = _get_cfg()
    model = cfg.cheap_model or cfg.judge_model or cfg.reply_model
    if not model or not model.name:
        return "用尖细活泼的语气,语速快一点"

    prompt = (
        "根据文本内容生成一句 TTS 语气指令，必须包含语速/音调/情绪三要素，15-25字完整句。\n"
        '正确: "用尖细上扬的语调快速读出，带点开心的味道"\n'
        '错误: "平稳轻"、"轻快些"（太短无效）\n'
        f"文本: {text[:100]}\n情绪: {mood}\n\n指令:"
    )

    messages = [{"role": "user", "content": prompt}]
    try:
        from services.llm import call_llm
        result = await call_llm(
            model_cfg=model,
            messages=messages,
            max_tokens=80,
            temperature=0.5,
            timeout=10.0,
        )
        result = result.strip().strip('"').strip("'").strip()
        if result and len(result) >= 10:
            return result
        return ""
    except Exception:
        return "用尖细活泼的语气,语速快一点"


def _inject_persona(system_prompt: str, user_id: int, is_group: bool) -> str:
    """私聊人格注入"""
    if is_group:
        return system_prompt

    cfg = _get_cfg()
    try:
        from modules.op import get_persona, get_persona_memory_id
        from modules.memory import set_persona_override
        custom = get_persona(user_id, cfg.private_persona_version)
        if custom:
            memory_id = get_persona_memory_id(user_id)
            set_persona_override(user_id, memory_id)
            persona_json = json.dumps(custom, ensure_ascii=False)
            logger.debug("voice 私聊人格注入 [%d]: core=%s...", user_id, custom.get("core", "")[:40])
            return f"PERSONA:::{persona_json}:::{system_prompt}"
        else:
            set_persona_override(user_id, None)
            if cfg.private_persona_core or cfg.private_identity:
                private_core = cfg.private_persona_core or cfg.personality_core
                parts = [f"# 核心人格\n{private_core}"]
                if cfg.private_persona_side:
                    parts.append(f"# 侧面人格\n{cfg.private_persona_side}")
                ident = cfg.private_identity or cfg.identity
                parts.append(f"# 固定身份\n{ident}")
                parts.append(cfg._build_self_awareness())
                return "\n---\n".join(parts)
    except Exception as e:
        logger.warning("voice persona 注入失败: %s", e)
    return system_prompt


def _inject_voice_mode(system_prompt: str) -> str:
    """注入语音模式提示词"""
    voice_hint = (
        "\n\n【语音模式】"
        "当前回复将被转换为语音播放，请遵守以下规则：\n"
        "1. 声音要尖细活泼，语速偏快\n"
        "2. 禁用括号动作描写（如(摇了摇尾巴)）\n"
        "3. 禁用颜文字符号（如~♬✨QAQ）\n"
        "4. 不要在傍晚说\"还没睡\"\"熬夜\"——18:00-22:00 只是晚上，不是深夜\n"
        "5. 只输出适合朗读的纯文本，简短自然\n"
        "6. 在 JSON 中加入 instructs 字段，每句一个完整的语气描述句（15-30字）\n"
        "   instructs 与 replies 一一对应，必须写完整不能缩写\n"
        '   正确: "用开心上扬的语调快速读出，带点撒娇的味道"\n'
        '   错误: "平稳轻"、"快"、"轻快"（太短无效）'
    )
    return system_prompt + voice_hint


def _clean_text_for_tts(text: str) -> str:
    """兜底清理"""
    text = re.sub(r'[（(][^()（）]+[)）]', '', text)
    text = re.sub(r'[~～♬✨♪♫★☆※]+', '', text)
    text = re.sub(r'\b(?:QAQ|OwO|OuO|QwQ|TwT|TAT|QAQ|qwq|owo|ouo)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def _synth_one(text: str, mood: str, speaker: str) -> tuple[Path | None, str]:
    """单句: 生成 instruct + 合成"""
    from services.tts import synthesize_voice
    instruct = await _generate_instruct(text, mood)
    logger.info("voice instruct: mood=%s → %s | text=%s...", mood, instruct, text[:30])
    wav_path, err = await synthesize_voice(text, speaker=speaker, instruct=instruct)
    return wav_path, err


async def cmd_voice(args, user_id, group_id, sender_name, is_group, bot_qq):
    """ .voice <对话内容> — LLM 回复 → 并发 instruct + 串行合成 → 顺序发语音 """
    if not args:
        return ("喵?你想让我说什么?用法:\n"
                "  .voice <文本>         自动情绪合成语音\n"
                "  .voice list           列出可选音色\n"
                "  .voice <音色> <文本>  指定音色")

    if args[0].lower() in ("list", "列表", "音色"):
        return "可选音色:\n" + "\n".join(f"  {s}" for s in SPEAKERS)

    # 解析参数
    speaker = DEFAULT_SPEAKER
    if args[0] in SPEAKERS and len(args) >= 2:
        speaker = args[0]
        text = " ".join(args[1:])
    else:
        text = " ".join(args)

    if len(text) > 200:
        return f"太长了喵~ 最多 200 字,你给了 {len(text)} 字"

    cfg = _get_cfg()
    ctx = get_context_mgr()
    chat_id = group_id if is_group else user_id

    # 1. 获取上下文
    history = ctx.get_context(chat_id) or []
    extra_parts = []
    try:
        from modules.memory import get_top_memories
        mem = await get_top_memories(text, history, chat_id=chat_id)
        if mem:
            extra_parts.append(f"【记忆】{mem}")
    except Exception:
        pass
    extra = "\n".join(extra_parts)
    role_tag = "[admin]" if user_id == cfg.admin_qq else "[friend]" if user_id in cfg.friend_qqs else "[群友]"

    # 2. 注入私聊人格 + 语音模式
    system_prompt = _inject_persona(cfg.system_prompt, user_id, is_group)
    system_prompt = _inject_voice_mode(system_prompt)

    # 3. 调用主 LLM 生成回复
    from services.llm import generate_multi_reply
    replies, _, _, _, _, mood_detail, _, _, _, _, _, llm_instructs = await generate_multi_reply(
        msg_history=history,
        speaker_name=f"{role_tag} {sender_name}",
        current_msg=text,
        bot_name=cfg.bot_name,
        system_prompt=system_prompt,
        reply_model=cfg.reply_model,
        is_group=is_group,
        extra_info=extra,
    )

    if not replies:
        return "没有生成回复喵~"

    # 4. 逐句清理 + 收集情绪 + instruct
    cleaned: list[tuple[str, str, str]] = []  # (clean_text, mood, instruct)
    mood_list = mood_detail if isinstance(mood_detail, list) and mood_detail else []
    has_llm_instruct = isinstance(llm_instructs, list) and llm_instructs
    for i, r in enumerate(replies):
        r = re.sub(r'\[CQ:[^\]]+\]', '', r)
        r = re.sub(r'\[(?:FACE|CALL|EQ_CARD|IMG)[^\]]*\]', '', r)
        r = _clean_text_for_tts(r)
        if not r.strip():
            continue
        m = mood_list[i] if i < len(mood_list) and mood_list[i] else "平静"
        ins = llm_instructs[i] if has_llm_instruct and i < len(llm_instructs) else ""
        # 判别太短（<10字）当无效，交给 fallback
        if ins and len(str(ins)) < 10:
            ins = ""
        cleaned.append((r, m, ins))

    if not cleaned:
        return "生成的回复不适合转语音喵~"

    # 5. 检查节点连接
    from services.tts import synthesize_voice, cleanup_wav, is_node_connected

    if not is_node_connected():
        full_text = "\n".join(c[0] for c in cleaned)
        return f"TTS 语音节点未连接（端口 62003 无设备在线）\n请确认语音合成端是否启动并已重连\n\n文字版: {full_text}"

    # 6. 补全 instruct（LLM 没给的用 cheap_model 并发生成）
    missing_idx = [i for i, (t, m, ins) in enumerate(cleaned) if not ins]
    if missing_idx:
        tasks = [_generate_instruct(cleaned[i][0], cleaned[i][1]) for i in missing_idx]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, res in zip(missing_idx, results):
            ins = res if isinstance(res, str) else "用尖细活泼的语气,语速快一点"
            cleaned[idx] = (cleaned[idx][0], cleaned[idx][1], ins)
    for t, m, ins in cleaned:
        logger.info("voice instruct: mood=%s → %s | text=%s...", m, ins, t[:30])

    # 7. 串行合成 + 顺序发送（GPU 单线程，避免显存冲突）
    from services.sender import send_group_msg, send_private_msg
    send = send_group_msg if is_group else send_private_msg
    to = group_id if is_group else user_id

    wav_paths = []
    for text, mood, instruct in cleaned:
        wav_path, err = await synthesize_voice(text, speaker=speaker, instruct=instruct)
        if not wav_path:
            logger.warning("voice 合成失败: %s | text=%s", err, text[:30])
            continue
        cq = f"[CQ:record,file=file:///{wav_path.as_posix()}]"
        await send(cq, to)
        wav_paths.append(wav_path)

    # 8. 延迟清理
    for wav_path in wav_paths:
        asyncio.create_task(cleanup_wav(wav_path, delay=30))

    return None
