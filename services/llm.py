"""
LLM 调用服务（原 send_message.py 中的 LLM 部分 + judge/if_module）
- 统一 OpenAI 兼容接口封装
- ✅ 支持并行调用多个模型（asyncio.gather）减少串行延迟
- 多句回复生成 + 好感度提取
- 兴趣度判断（三级判断模型调用）
- 搜索判断模型调用
- v1.1.2: 提示词统一管理 → data/main_skill.md，群聊/私聊分离
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

# ── 回复 JSON schema ──────────────────────────────────────
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "reply_schema.json"
_REPLY_SCHEMA: dict | None = None


def _load_reply_schema() -> dict:
    global _REPLY_SCHEMA
    if _REPLY_SCHEMA is None:
        _REPLY_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _REPLY_SCHEMA


def _normalize_reply_json(data: dict) -> dict:
    """按 schema 自动补缺、clamp 范围、校验类型"""
    s = _load_reply_schema().get("fields", {})
    out = {}
    for key, field in s.items():
        val = data.get(key)
        dtype = field.get("type", "str")

        # null 处理
        is_nullable = "null" in dtype
        if val is None and is_nullable:
            out[key] = None
            continue
        if val is None:
            val = field.get("default")

        # 类型转换
        if dtype.startswith("str"):
            out[key] = str(val) if val else field.get("default", "")
            max_len = field.get("max_len")
            if max_len and len(out[key]) > max_len:
                out[key] = out[key][:max_len]
        elif dtype.startswith("int"):
            try:
                out[key] = int(val)
            except (ValueError, TypeError):
                out[key] = int(field.get("default", 0))
            mn, mx = field.get("min"), field.get("max")
            if mn is not None and out[key] < mn:
                out[key] = mn
            if mx is not None and out[key] > mx:
                out[key] = mx
            if is_nullable and not val:
                out[key] = None
        elif dtype.startswith("list"):
            out[key] = val if isinstance(val, list) else field.get("default", [])
        elif dtype.startswith("dict"):
            if isinstance(val, dict):
                sub = {}
                for sk, sf in field.get("keys", {}).items():
                    sv = val.get(sk, sf.get("default"))
                    if sf.get("type") == "str":
                        sub[sk] = str(sv) if sv else ""
                    else:
                        try:
                            sub[sk] = int(sv)
                        except (ValueError, TypeError):
                            sub[sk] = 0
                out[key] = sub
            elif is_nullable:
                out[key] = None
            else:
                out[key] = field.get("default", {})
        elif dtype.startswith("int|null"):
            if val is None:
                out[key] = None
            else:
                try:
                    out[key] = int(val)
                except (ValueError, TypeError):
                    out[key] = None
        elif dtype.startswith("str|null"):
            out[key] = str(val) if val else None
        else:
            out[key] = val if val is not None else field.get("default")
    return out
import re
import random
from pathlib import Path
from typing import Optional

from openai import OpenAI

from core.logger import get_logger
from core.config import BotConfig, ModelConfig, get_config
from utils.format_lang import format_lang

logger = get_logger("llm")

# ── 多句回复提示词（从 main_skill.md 加载）──────────────────
# 设计原则：system 消息只放不随对话变化的内容（人设+格式规则），
# 动态内容（历史/记忆/搜索）放在 user/assistant 多轮消息中。
# 这样 API 端的 KV cache 在连续对话中命中率最高。

# ★ 锚点消息：插入在 system 和对话历史之间。内容永远不变，
#    确保即使 FIFO 裁剪历史消息，system+锚点这段前缀始终缓存命中。
_MULTI_REPLY_ANCHOR = "【以下是最新的聊天记录，请结合你的人设和上述规则参与对话】"

# ── main_skill.md 加载 ────────────────────────────────────

_skill_sections: dict[str, str] = {}
_skill_loaded = False


def _load_skill_sections() -> dict[str, str]:
    """解析 skills/ 目录下所有 .md 文件，返回 {节名: 内容}"""
    global _skill_sections, _skill_loaded
    if _skill_loaded:
        return _skill_sections

    import re as _re
    skills_dir = Path(__file__).resolve().parent.parent / "skills"
    sections: dict[str, str] = {}
    if skills_dir.is_dir():
        for sf in sorted(skills_dir.glob("*.md")):
            try:
                text = sf.read_text(encoding="utf-8").strip()
                # 提取 ## section_name 行（支持 @scope 后缀）
                m = _re.match(r'^##\s+(\S+)', text)
                if not m:
                    continue
                key = m.group(1).strip()
                # 去掉第一行（标题行），剩余为内容
                content = text[text.index("\n") + 1:].strip() if "\n" in text else ""
                if content:
                    sections[key] = content
            except Exception:
                pass
    _skill_loaded = True
    _skill_sections = sections

    _skill_sections = sections
    _skill_loaded = True
    logger.info("main_skill.md 已加载: %d 个章节", len(sections))
    return sections


def reload_skill_cache():
    """清除技能文件缓存（reload 时调用）"""
    global _skill_sections, _skill_loaded
    _skill_sections = {}
    _skill_loaded = False


def build_system_sections(bot_name: str, personality: str, is_group: bool,
                          custom_persona: dict | None = None,
                          capabilities: Optional[list] = None) -> dict[str, str]:
    """组装 system prompt 的各语义节（Phase 9：供 Context Engine 分类 Stable/Dynamic）。

    返回 {节名: 内容}，节名 ∈ {header, format_rules, command_tools, cmd_list,
    face_lib, private_tone, anti_repeat, fav_format, fav_tiers, play_mode}。
    保留原 _build_system_text 的拼接逻辑，仅拆成 dict 便于分类与预算。

    Phase 10：capabilities 非空时，cmd_list 只列出「当前请求相关」的指令能力
    （CapabilityRouter 选中），不再每次注入全部 COMMAND_MAP，避免上下文膨胀。
    capabilities 为空（默认）时保持旧行为：全量指令列表。
    """
    sec = _load_skill_sections()

    if custom_persona:
        # per-user persona：用三段构造 header
        parts = []
        core = custom_persona.get("core", "")
        side = custom_persona.get("side", "")
        identity = custom_persona.get("identity", "")
        if core:
            parts.append(f"# 核心人格\n{core}")
        if side:
            parts.append(f"# 侧面人格\n{side}")
        if identity:
            parts.append(f"# 固定身份\n{identity}")
        header = "\n---\n".join(parts) if parts else f"# 角色设定\n{personality}"
    else:
        header = sec.get("prompt_header", "你是{bot_name}").format(
            bot_name=bot_name, personality=personality
        )

    fmt_key = "group_format" if is_group else "private_format"
    format_rules = sec.get(fmt_key, "")

    fav_format = sec.get("fav_format", "")
    fav_tiers = sec.get("fav_tiers", "")
    anti_repeat = sec.get("anti_repeat", "")
    play_mode = sec.get("play_mode", "") if not custom_persona else ""
    command_tools = sec.get("command_tools", "")
    face_lib = sec.get("face_lib", "") if not custom_persona else ""
    private_tone = sec.get("private_tone", "") if (not is_group and not custom_persona) else ""

    # 动态注入 COMMAND_MAP 全部指令（Phase 10：capabilities 非空时只注入相关指令）
    if capabilities:
        from core.capability.loader import build_command_usage
        cmd_list = build_command_usage(list(capabilities))
    else:
        cmd_list = _build_dynamic_command_list()

    return {
        "header": header,
        "format_rules": format_rules,
        "command_tools": command_tools,
        "cmd_list": cmd_list,
        "face_lib": face_lib,
        "private_tone": private_tone,
        "anti_repeat": anti_repeat,
        "fav_format": fav_format,
        "fav_tiers": fav_tiers,
        "play_mode": play_mode,
    }


def _build_system_text(bot_name: str, personality: str, is_group: bool, custom_persona: dict | None = None) -> str:
    """根据群聊/私聊组装 system prompt

    custom_persona dict {core, side, identity} 非空时：用三段构造 header（全替换人设），
    禁用 face_lib/private_tone/play_mode（表情/语气/玩模式），保留功能段（format/command/fav/anti_repeat）。
    """
    sections = build_system_sections(bot_name, personality, is_group, custom_persona)

    # 稳定的注入顺序（Stable 在前，Dynamic Capability 在后）
    order = [
        "header", "format_rules", "command_tools", "cmd_list",
        "face_lib", "private_tone", "anti_repeat", "fav_format", "fav_tiers", "play_mode",
    ]
    return "\n\n".join(p for p in (sections[k] for k in order) if p)


def _build_dynamic_command_list() -> str:
    """从 COMMAND_MAP 动态生成全部指令用法列表（含中文说明）"""
    try:
        from modules.commands import COMMAND_MAP
    except ImportError:
        return ""
    lines = ["【全部可调用指令】"]
    for name in sorted(set(COMMAND_MAP)):
        desc = _CMD_DESC.get(name)
        if desc:
            lines.append(f"  .{name}: {desc}")
        else:
            lines.append(f"  .{name}")
    return "\n".join(lines)

# ── 指令说明（精简，面向 LLM）─────────────────────────────
_CMD_DESC = {
    # 数据查询
    "balance": "查 DeepSeek API 余额（还剩多少钱）",
    "cost":    "查今日 Token 消耗统计（调了多少次、花了多少钱）",
    "tokens":  "查今日各模型 Token 用量明细",
    "stats":   "查自身统计（回复次数/好感度/被@次数）",
    "setstats":"设置自身统计数据（主人用）",
    "unstats": "管理员用",
    # PC状态
    "sys":     "查看主人电脑状态（当前窗口、在听什么歌、歌词）",
    "pc":      "同 sys，查看主人电脑状态",
    # 好感度 / 关系
    "favlist": "查看本群好感度排行榜",
    "resetfav":"重置某人的好感度（管理员）",
    "添加关系":"添加用户的预设身份（管理员）",
    # 天气 / 地震 / 新闻
    "weather": "查指定城市天气",
    "天气":    "同 weather，查天气",
    "eq":      "查最近地震信息",
    "地震":    "同 eq，查地震",
    "wzq":     "查五子棋战绩排行榜",
    # 搜索 / 阅读
    "search":  "搜索互联网获取信息，返回总结",
    "read":    "阅读网页内容，返回总结",
    # 记忆 / 上下文
    "memory":  "显示当前群聊的记忆（管理员）",
    "recall":  "召回历史聊天中与当前话题相关的记忆",
    "persona": "查看机器人的性格描述/adoptable persona",
    # 群管理
    "op":      "移交特权/管理员",
    "owner":   "移交拥有者权限",
    "leave":   "退出群聊（管理员）",
    "nickname":"给群友设置备注名",
    # 模式 / 预设
    "preset":  "切换群聊 preset（管理员）",
    "sleep":   "切换群聊到休眠模式",
    "叙事":    "切换群聊到叙事模式",
    "主人":    "切换群聊到主人互动模式",
    "含蓄":    "切换群聊到含蓄模式",
    # 提醒 / 倒计时 / 抽签
    "remind":  "设置定时提醒",
    "countdown":"设置倒计时",
    "倒计时":   "同 countdown",
    "luck":    "抽签/运势占卜",
    "抽":      "同 luck，抽签",
    # 信息 / 更新
    "info":    "查看个人信息/群信息",
    "updateinfo":"更新个人备注信息",
    "up":      "同 updateinfo",
    "reload":  "热重载配置（管理员）",
    "update":  "从 git 拉取最新代码并更新 bot",
    "upd":     "同 update",
    "gh":      "从 git 拉取指定分支代码更新 bot",
    "md":      "用 LLM 渲染 Markdown 为图片卡片",
    # 绘画 / 视频 / 语音
    "draw":    "AI 绘画生成图片",
    "绘画":    "同 draw",
    "video":   "AI 生成视频",
    "视频":    "同 video",
    "img2video":"图片转视频",
    "图生视频": "同 img2video",
    "voice":   "文本转语音播报",
    "语音":    "同 voice",
    "box":     "查看或加入小游戏盒子",
    # 五子棋 / 象棋 / 翻译
    "五子棋":   "发起五子棋对战",
    "象棋":    "发起象棋对战",
    "tr":      "翻译文本到指定语言",
    "翻译":    "同 tr，翻译文本",
    "xq":      "查看大群在线信息",
    # 战绩 / TUFD
    "wdsj":    "查我的数据",
    "tufd":    "查 TUFD 信息",
    "tuflevel":"查 TUFD 难度信息",
    "tufpage":"查看 TUFD 关卡页面",
    "tufsearch":"搜索 TUFD 谱面",
    "tuf谱面":  "同 tufsearch",
    "analyze": "分析谱面数据",
    # 系统
    "help":    "显示帮助信息",
    "ping":    "检查机器人是否在线",
    "restart": "重启机器人（管理员）",
    "reload":  "热重载配置文件（管理员）",
    # 调试/测试
    "jsonraw": "查看原始 LLM 输出 JSON（调试用）",
    "testok":  "测试用",
    "testsys": "测试用",
}


# ── 底层调用：同步 OpenAI → 异步执行器 ────────────────────

def _create_client(model_cfg: ModelConfig) -> OpenAI:
    """创建 OpenAI 客户端实例"""
    return OpenAI(base_url=model_cfg.url, api_key=model_cfg.key, timeout=60.0)


def _is_deepseek(model_cfg: ModelConfig) -> bool:
    """判断是否为 DeepSeek 模型（用于关闭 thinking 省延迟/降本）"""
    return "deepseek" in (model_cfg.url or "").lower()


def _thinking_disabled_body(model_cfg: ModelConfig) -> dict | None:
    """DeepSeek 默认思考模式 enabled，且思考模式下 temperature 无效。
    返回 extra_body 关闭 thinking，让 temperature 生效并降低延迟/成本。"""
    return {"thinking": {"type": "disabled"}} if _is_deepseek(model_cfg) else None


def _to_strict_tools(tools: list[dict]) -> list[dict]:
    """把工具 schema 转换为 DeepSeek strict 模式（Beta）要求：
    - 每个 function 设 strict=true
    - object 添加 additionalProperties=false
    - 所有 properties 键加入 required
    不修改原 TOOLS 定义，仅用于本次请求。"""
    from copy import deepcopy
    out = []
    for t in tools:
        t = deepcopy(t)
        fn = t.get("function", {})
        fn["strict"] = True
        params = fn.get("parameters")
        if isinstance(params, dict) and params.get("type") == "object":
            props = params.get("properties", {})
            params["additionalProperties"] = False
            keys = list(props.keys())
            if keys:
                reqs = list(dict.fromkeys([*(params.get("required") or []), *keys]))
                params["required"] = reqs
        out.append(t)
    return out


async def call_llm(
    model_cfg: ModelConfig,
    messages: list[dict],
    max_tokens: int | None = None,
    temperature: float = 0.7,
    timeout: float = 60.0,
    json_mode: bool = False,
) -> str:
    """
    调用 LLM 并返回原始文本内容。
    内部使用 run_in_executor 避免阻塞事件循环。
    
    Args:
        model_cfg: 模型配置（含 url / key / name）
        messages: 对话消息列表 [{"role":"system","content":...}, ...]
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        timeout: 超时时间（秒）
        
    Returns:
        模型返回的文本内容；出错返回空字符串
    """
    client = _create_client(model_cfg)
    loop = asyncio.get_running_loop()
    
    # Phase 6：记录本次 LLM 调用（供 trace 统计 llm_call_count）
    from core.trace import record_llm
    record_llm()
    
    logger.info("调用LLM [%s] url=%s tokens=%s temp=%.1f timeout=%.1fs",
                 model_cfg.name[:20], model_cfg.url.split('/')[-2] if '/' in model_cfg.url else model_cfg.url[:20],
                 str(max_tokens), temperature, timeout)
    
    start_time = loop.time()
    try:
        # ★ max_tokens=None 时不传递该参数，让 API 用默认值
        req_params = {
            "model": model_cfg.model if hasattr(model_cfg, 'model') else model_cfg.name,
            "temperature": temperature,
            "messages": messages,
        }
        if max_tokens is not None:
            req_params["max_tokens"] = max_tokens
        if json_mode:
            req_params["response_format"] = {"type": "json_object"}
        # DeepSeek 关闭 thinking（默认 enabled），省延迟/降本，并让 temperature 生效
        _td = _thinking_disabled_body(model_cfg)
        if _td:
            req_params["extra_body"] = _td
        
        completion = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.chat.completions.create(**req_params)),
            timeout=timeout,
        )
        elapsed = loop.time() - start_time
        text = completion.choices[0].message.content
        text = text.strip() if text else ""
        finish = completion.choices[0].finish_reason or "unknown"
        
        if not text:
            logger.warning("LLM [%s] 返回空内容 | finish_reason=%s | 耗时%.1fs",
                         model_cfg.name[:20], finish, elapsed)
            if json_mode and finish == "stop":
                logger.warning("JSON模式返回空，关闭json_mode重试...")
                return await call_llm(model_cfg, messages, max_tokens, temperature, timeout, json_mode=False)
            if finish == "length" and max_tokens < 2000:
                boosted = min(max_tokens * 2, 2000)
                logger.warning("finish_reason=length, 扩大上限 %d→%d 重试...", max_tokens, boosted)
                return await call_llm(model_cfg, messages, boosted, temperature, timeout, json_mode)

        # 记录 token 消耗
        try:
            usage = completion.usage
            if usage:
                from core.token_tracker import record_usage
                # DeepSeek 在 usage 顶层返回 prompt_cache_hit_tokens（非 OpenAI 的 prompt_tokens_details.cached_tokens）
                cached_tokens = getattr(usage, 'prompt_cache_hit_tokens', None) or 0
                if not cached_tokens:
                    _cached = getattr(usage, 'prompt_tokens_details', None)
                    cached_tokens = getattr(_cached, 'cached_tokens', 0) or 0
                record_usage(
                    model=model_cfg.name,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cached_tokens=cached_tokens,
                )
        except Exception:
            pass
        
        preview = text[:50] + ("..." if len(text) > 50 else "")
        logger.info("LLM [%s] 返回 %d字符 耗时%.1fs → \"%s\"",
                   model_cfg.name[:20], len(text), elapsed, preview.replace("\n", "\\n"))
        # 性能监控埋点（记录成功耗时）
        try:
            from services.notify_system import record_llm_call
            record_llm_call(elapsed, success=True)
        except Exception:
            pass
        # Phase 20：把单次 LLM 耗时采样写入当前 trace（供 P50/P95/P99 统计）
        try:
            from core.trace import record_llm_duration
            record_llm_duration(elapsed * 1000.0)
        except Exception:
            pass
        return text
    except asyncio.TimeoutError:
        elapsed = loop.time() - start_time
        logger.error("LLM [%s] 调用超时 (%.1fs/%.1fs)", model_cfg.name[:20], elapsed, timeout)
        try:
            from services.notify_system import record_llm_call
            record_llm_call(elapsed, success=False)
        except Exception:
            pass
        try:
            from core.trace import record_llm_duration
            record_llm_duration(elapsed * 1000.0)
        except Exception:
            pass
        return ""
    except Exception as e:
        elapsed = loop.time() - start_time
        logger.error("LLM [%s] 调用失败 (%.1fs): %s", model_cfg.name[:20], elapsed, e)
        try:
            from services.notify_system import record_llm_call
            record_llm_call(elapsed, success=False)
        except Exception:
            pass
        try:
            from core.trace import record_llm_duration
            record_llm_duration(elapsed * 1000.0)
        except Exception:
            pass
        return ""


# ── Function Calling ─────────────────────────────────────

class ToolCallResult:
    """工具调用结果"""
    def __init__(self, content: str, tool_calls: list[dict] | None):
        self.content = content or ""
        self.tool_calls = tool_calls or []

async def call_llm_with_tools(
    model_cfg: "ModelConfig",
    messages: list[dict],
    tools: list[dict],
    max_tokens: int | None = None,
    temperature: float = 0.7,
    timeout: float = 60.0,
) -> ToolCallResult:
    """
    调用 LLM，支持 Function Calling。
    返回 ToolCallResult，包含可能的 tool_calls。
    """

    client = _create_client(model_cfg)
    loop = asyncio.get_running_loop()
    start_time = loop.time()

    # Phase 6：记录本次 LLM 调用（供 trace 统计 llm_call_count）
    from core.trace import record_llm
    record_llm()

    # 工具/strict 模式 & thinking 关闭（仅 DeepSeek）
    _td = _thinking_disabled_body(model_cfg)
    _use_strict = _td is not None  # DeepSeek 支持 strict 模式（Beta，需 /beta base_url）
    _strict_tools = _to_strict_tools(tools) if _use_strict else tools

    req_params = {
        "model": model_cfg.model if hasattr(model_cfg, 'model') else model_cfg.name,
        "temperature": temperature,
        "messages": messages,
    }
    # Phase 10：tools 为空（能力按需路由未命中工具）时不发 tools，避免 provider 拒绝
    if tools:
        req_params["tools"] = tools
        req_params["tool_choice"] = "auto"
    if max_tokens is not None:
        req_params["max_tokens"] = max_tokens
    if _td:
        req_params["extra_body"] = _td

    # strict 模式需要 beta base_url；失败则回退普通调用
    _strict_client = None
    if _use_strict:
        _strict_client = OpenAI(
            base_url=model_cfg.url.rstrip("/") + "/beta",
            api_key=model_cfg.key, timeout=60.0,
        )

    try:
        try:
            _client = _strict_client if _strict_client is not None else client
            completion = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _client.chat.completions.create(**req_params)),
                timeout=timeout,
            )
        except Exception as se:
            # strict/Beta 失败 → 回退普通客户端 + 原始 tools（去掉 strict）
            if _use_strict:
                logger.warning("LLM FC strict 模式失败，回退普通调用: %s", se)
                if tools:
                    req_params["tools"] = tools
                completion = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: client.chat.completions.create(**req_params)),
                    timeout=timeout,
                )
            else:
                raise
        msg = completion.choices[0].message
        content = msg.content or ""
        tc_list: list[dict] = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tc_list.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })
            logger.info("LLM [%s] → tool_calls: %s",
                       model_cfg.name[:20],
                       [(t["name"], str(t["arguments"])[:60]) for t in tc_list])

        # 性能监控埋点（记录成功耗时）
        try:
            from services.notify_system import record_llm_call
            record_llm_call(loop.time() - start_time, success=True)
        except Exception:
            pass
        try:
            from core.trace import record_llm_duration
            record_llm_duration((loop.time() - start_time) * 1000.0)
        except Exception:
            pass
        return ToolCallResult(content=content.strip(), tool_calls=tc_list)

    except asyncio.TimeoutError:
        logger.error("LLM FC [%s] 超时", model_cfg.name[:20])
        try:
            from services.notify_system import record_llm_call
            record_llm_call(loop.time() - start_time, success=False)
        except Exception:
            pass
        try:
            from core.trace import record_llm_duration
            record_llm_duration((loop.time() - start_time) * 1000.0)
        except Exception:
            pass
        return ToolCallResult(content="", tool_calls=[])
    except Exception as e:
        logger.error("LLM FC [%s] 失败: %s", model_cfg.name[:20], e)
        try:
            from services.notify_system import record_llm_call
            record_llm_call(loop.time() - start_time, success=False)
        except Exception:
            pass
        try:
            from core.trace import record_llm_duration
            record_llm_duration((loop.time() - start_time) * 1000.0)
        except Exception:
            pass
        return ToolCallResult(content="", tool_calls=[])


# ── 高级功能：多句回复生成 ─────────────────────────────────

def _extract_fav_change(raw: str) -> tuple[str, int]:
    """
    从模型输出中提取好感度变化值和清洗后的文本。
    
    Returns:
        (清洗后的原始文本, fav变化值)
    """
    fav_change = 0
    fav_match = re.search(r'[\[［]fav:\s*([+-]?\d+)[\]］]', raw)
    if fav_match:
        try:
            fav_change = int(fav_match.group(1))
            fav_change = max(-5, min(5, fav_change))
            raw = raw.replace(fav_match.group(0), "")
        except Exception:
            pass
    return raw, fav_change


def _clean_sentences(raw: str) -> list[str]:
    """
    清洗模型输出的原始文本为句子列表。
    - 按 ||| 分割
    - 如果没有 ||| 分隔符，尝试按空行段落分割
    - 移除残留竖线和角色标签
    - 过滤过短的句子
    - 上限 5 句
    """
    # 先检查是否有 ||| 分隔符
    if "|||" in raw:
        raw = raw.replace("\n", " ").replace("\r", " ")
        sentences = [s.strip() for s in raw.split("|||") if s.strip()]
    elif "||" in raw:
        raw = raw.replace("\n", " ").replace("\r", " ")
        sentences = [s.strip() for s in raw.split("||") if s.strip()]
    else:
        # 没有分隔符 → 按双换行段落切分（适配长文回复如错误报告分析）
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', raw) if p.strip()]
        if len(paragraphs) <= 1:
            sentences = [raw.strip()]
        else:
            sentences = paragraphs
        # 不合并换行，保留段落格式
        sentences = [s.replace("\r", "") for s in sentences]
        return [s for s in sentences if len(s) >= 2][:5]
    
    # 清理残留
    sentences = [
        s.replace("||", "").replace("|", "").strip()
        for s in sentences
    ]
    sentences = [
        re.sub(r'[\[［](admin|friend|群友)[\]］]', '', s)
        for s in sentences
    ]
    sentences = [s for s in sentences if len(s) >= 2]
    return sentences[:5]


def _build_history_messages(msg_history: list[str], bot_name: str) -> list[dict]:
    """
    将扁平上下文列表拆分为多轮 user/assistant 消息。

    拆分规则：
    - 以 "{bot_name}:" 或 "{bot_name}：" 开头的行 → assistant
    - 其他所有行 → user
    - 连续相同角色的消息合并为一条（减少轮次）
    - assistant 消息中的回复分隔符保持原样（不做替换），确保 prefix 稳定
    """
    if not msg_history:
        return []

    turns: list[dict] = []

    for line in msg_history:
        line = line.strip()
        if not line:
            continue

        is_bot = False
        content = line

        # 检测是否为 bot 回复（兼容半角/全角冒号）
        for sep in [": ", "："]:
            if line.startswith(f"{bot_name}{sep}"):
                is_bot = True
                content = line[len(bot_name) + len(sep):].strip()
                break

        role = "assistant" if is_bot else "user"

        # ★ 每条消息独立成 turn，不合并——让 LLM 清楚区分每个人的发言
        turns.append({"role": role, "content": content})

    return turns


# ── Function Calling 增强版生成 ──────────────────────────

async def generate_multi_reply_with_tools(
    msg_history: list[str],
    speaker_name: str,
    current_msg: str,
    bot_name: str,
    system_prompt: str,
    reply_model: "ModelConfig",
    is_group: bool = True,
    extra_info: str = "",
    max_tokens: int = 8000,
    user_id: int = 0,
    group_id: int = 0,
    bot_qq: int = 0,
    system_text_override: str | None = None,
    tools_override: Optional[list] = None,
) -> tuple[list[str], int, list, str, str, list | None, str, int | None, str | None, str, dict]:
    """
    跟 generate_multi_reply 一样，但先走 FC 工具调用。
    如果 LLM 选择调用工具，执行后把结果喂回去，再生成最终回复。

    Phase 10：tools_override 非空时，只把「当前请求相关」的工具 Schema 给模型，
    不再每次注入全部工具（CapabilityLoader 按需加载）。
    """
    if tools_override is not None:
        tools = tools_override
    else:
        from core.tools import get_tool_schemas
        tools = get_tool_schemas()
    msgs = _build_messages(msg_history, speaker_name, current_msg, bot_name, system_prompt, is_group, extra_info, system_text_override)

    # 长消息（题目/长文/网页阅读）→ 扩大输出 token
    has_long_context = len(current_msg) > 2000
    if max_tokens and has_long_context:
        max_tokens = max(max_tokens, 8000)

    # 多轮 FC Agent 循环：最多 5 轮，LLM 可以连续调多个工具
    MAX_ROUNDS = 2
    errors = []
    data_results = []
    action_results = []

    for round_idx in range(MAX_ROUNDS):
        result = await call_llm_with_tools(reply_model, msgs, tools, max_tokens=max_tokens, temperature=0.4)
        raw_preview = (result.content or "")[:200].replace("\n", "\\n")
        logger.info("LLM原始输出 [轮%d]: content=%s | tool_calls=%d", round_idx + 1, raw_preview, len(result.tool_calls))

        if not result.tool_calls:
            if not (result.content or "").strip():
                if round_idx == 0:
                    logger.warning("LLM 返回空内容，重试...")
                    continue
                # 第二轮仍空 → json_mode 兜底（FC 路径未开 json_mode，这里补一次）
                logger.warning("LLM 连续返回空内容，用 json_mode 兜底...")
                json_raw = await call_llm(
                    reply_model, msgs,
                    max_tokens=min(max_tokens or 800, 800),
                    temperature=0.3, json_mode=True,
                )
                if json_raw and json_raw.strip():
                    result.content = json_raw
                    logger.info("json_mode 兜底成功: %s...", json_raw[:80])
                else:
                    logger.error("json_mode 兜底仍失败，放弃")
                    break
            # 非 JSON 且首次 → 强制 json_mode 重试一次
            raw = (result.content or "").strip()
            if round_idx == 0 and raw and not raw.startswith("{"):
                # 先尝试从混合文本中提取 JSON（LLM 有时会先吐自然语言再吐 JSON）
                json_pos = raw.find('{"replies"') if '"replies"' in raw else raw.find('{"')
                if json_pos >= 0:
                    extracted = raw[json_pos:]
                    logger.info("从混合文本提取 JSON: pos=%d", json_pos)
                    result.content = extracted
                    # 从 JSON 解析 calls 转为原生 tool_calls 格式（触发FC循环执行）
                    try:
                        parsed = json.loads(extracted)
                        calls = parsed.get("calls", [])
                        if calls and isinstance(calls, list):
                            result.tool_calls = [
                                {"id": f"call_{i}", "name": c.get("name", ""), "arguments": {"args": c.get("args", "")}}
                                for i, c in enumerate(calls)
                            ]
                            logger.info("混合文本中提取到 %d 个工具调用", len(result.tool_calls))
                    except Exception:
                        pass
                    # 提取成功但无 calls → json_mode 确保 JSON 完整
                    if not result.tool_calls:
                        json_raw = await call_llm(reply_model, msgs, max_tokens=max(max_tokens or 0, 4000), temperature=0.4, json_mode=True)
                        if json_raw and json_raw.startswith("{"):
                            result.content = json_raw
                else:
                    logger.info("LLM 输出非 JSON，强制重试...")
                    json_raw = await call_llm(reply_model, msgs, max_tokens=min(max_tokens or 800, 800), temperature=0.3, json_mode=True)
                    if json_raw and json_raw.startswith("{"):
                        result.content = json_raw
                        logger.info("json_mode 重试成功: %s...", json_raw[:80])
            break

        logger.info("FC: 轮%d 检测到 %d 个工具调用", round_idx + 1, len(result.tool_calls))
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in result.tool_calls
            ],
        })

        # 并行执行本轮所有工具（Phase 8：统一走 ToolRuntime，解耦 Model Tool Request 与执行）
        from core.tool_runtime import ToolRequest, get_tool_runtime
        from core.trace import get_trace_id

        async def run_one(tc):
            req = ToolRequest.from_tool_call(
                tc, trace_id=get_trace_id(),
                user_id=user_id, group_id=group_id,
                chat_id=group_id if is_group else user_id,
                sender_name=speaker_name, is_group=is_group, bot_qq=bot_qq,
                original_msg=current_msg,
            )
            res = await get_tool_runtime().execute(req)
            return tc, res.to_context()

        tool_results = await asyncio.gather(*(run_one(tc) for tc in result.tool_calls))
        for tc, tool_text in tool_results:
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_text})
            logger.info("FC: 工具 %s 返回 %d 字符", tc["name"], len(tool_text))
            # 工具返回长内容时扩大 max_tokens
            if len(tool_text) > 500:
                max_tokens = max(max_tokens or 0, 8000)

            if "未绑定" in tool_text or "失败" in tool_text or "出错" in tool_text:
                errors.append(tool_text)
            elif tc["name"] in ("wdsj_query", "weather", "search_web", "earthquake", "sys", "pc"):
                data_results.append(tool_text)
            elif tool_text:
                action_results.append(tool_text)

        # 遇到错误或数据结果 → 停止循环
        if errors or data_results:
            break
        # 已执行过工具 → 跳出，走 json_mode 生成最终回复
        if action_results:
            break

    # ── 如果工具已执行，强制 json_mode 回复 ──
    if errors or data_results or action_results:
        # 工具已成功执行 → 记录 tool_success，绝不让最终 LLM 失败覆盖工具成功。
        if action_results:
            try:
                from core.trace import record_final_flag
                record_final_flag("tool_success")
            except Exception:
                pass
        json_raw = await call_llm(
            reply_model, msgs,
            max_tokens=max(max_tokens or 0, 8000),
            temperature=0.4, json_mode=True,
        )
        if json_raw and json_raw.strip():
            parsed = _parse_reply(json_raw, speaker_name, quiet=True)
            if parsed[0]:
                try:
                    from core.trace import record_final_flag
                    record_final_flag("final_reply_ok")
                except Exception:
                    pass
                return parsed
            # 工具已成功，但最终 LLM JSON 解析失败 → 用工具结果兜底，
            # 禁止让用户"再说一遍"覆盖已成功的工具执行。
            logger.warning("工具已成功执行，但最终 LLM JSON 解析失败，回退工具结果")
            try:
                from core.trace import record_final_flag
                record_final_flag("final_llm_failed")
                record_final_flag("final_reply_fallback")
            except Exception:
                pass
            if action_results:
                return _parse_reply(
                    json.dumps({"replies": [action_results[0]], "fav": 0, "calls": [],
                                "face": None, "mood": "开心", "action": "", "at": None,
                                "mode": None, "origin": "user", "actor": {}},
                               ensure_ascii=False),
                    speaker_name,
                )

    # ── 处理结果 ──
    if errors:
        # Phase 20 Part7：工具被拒绝/出错时，禁止硬编码 fallback 话术。
        # 让 LLM 基于已有的 persona system（msgs[0]）自然、符合人设地说明，
        # 保证「Permission DENY → 结构化 ToolResult → LLM → Persona 风格回复」。
        return await _reply_from_error(msgs, errors[0], reply_model, speaker_name)
    if data_results:
        wrap_msgs = [
            {"role": "system", "content": "你是幻梦，一个可爱的QQ机器人。用自然语气回复，加个喵或颜文字。1句，≤40字。"},
            {"role": "user", "content": f"用户说「{current_msg}」\n查到数据：{data_results[0]}\n请用一句话自然回复。"},
        ]
        raw = await call_llm(reply_model, wrap_msgs, max_tokens=80, temperature=0.4)
        if raw:
            return _parse_reply(
                json.dumps({"replies": [raw.strip()], "fav": 0, "calls": [], "face": None, "mood": "好奇", "action": "", "at": None, "mode": None, "origin": "user", "actor": {}}, ensure_ascii=False),
                speaker_name,
            )
        return _parse_reply(
            json.dumps({"replies": [data_results[0]], "fav": 0, "calls": [], "face": None, "mood": "好奇", "action": "", "at": None, "mode": None, "origin": "user", "actor": {}}, ensure_ascii=False),
            speaker_name,
        )

    if action_results:
        reply_text = action_results[0]
        return _parse_reply(
            json.dumps({"replies": [reply_text], "fav": 0, "calls": [], "face": None, "mood": "开心", "action": "", "at": None, "mode": None, "origin": "user", "actor": {}}),
            speaker_name,
        )

    # 无工具调用 → 优先尝试 JSON，失败则按纯文本处理
    raw = result.content or ""
    if not raw.strip():
        return [], 0, [], "", "", None, "", None, None, "user", {}, None

    # 尝试 JSON 解析（LLM 有时返回 JSON 但不调工具）
    parsed = _parse_reply(raw, speaker_name, quiet=True)
    if parsed[0]:
        return parsed

    # 纯文本：按句号拆开发送
    sentences = [s.strip() + "。" for s in raw.replace("\n", " ").split("。") if s.strip()]
    if not sentences:
        sentences = [raw.strip()[:120]]
    return sentences, 0, [], "", "", None, "", None, None, "user", {}, None



async def _reply_from_error(msgs: list[dict], err_text: str,
                            reply_model: str, speaker_name: str) -> tuple:
    """Phase 20 Part7：工具被拒绝或出错时，让 LLM 基于 persona 自然回复。

    msgs 已包含 persona system（由调用方用 ContextBuilder 注入），此处追加一句
    用户指令，让 LLM 用自己的语气向用户说明（不暴露内部细节），保证人设一致：
    Permission DENY → 结构化 ToolResult → LLM → Persona 风格回复。
    禁止 Permission 层直接生成最终用户话术。
    仅当 LLM 彻底失败时才返回中性兜底（Graceful Degradation，不吐崩溃堆栈）。
    """
    try:
        from core.trace import record_llm_duration
        _st = time.monotonic()
        follow = list(msgs) + [{
            "role": "user",
            "content": (
                "刚才的某个操作没有成功完成。请用你一贯的语气，自然、简短地告诉用户"
                "这件事（不要复述内部报错细节，也不要暴露权限/系统内部信息）："
                f"{err_text}"
            ),
        }]
        raw = await call_llm(reply_model, follow, max_tokens=200, temperature=0.4)
        try:
            record_llm_duration((time.monotonic() - _st) * 1000.0)
        except Exception:
            pass
        if raw and raw.strip():
            return _parse_reply(
                json.dumps({
                    "replies": [raw.strip()], "fav": 0, "calls": [], "face": None,
                    "mood": "无奈", "action": "", "at": None, "mode": None,
                    "origin": "user", "actor": {},
                }, ensure_ascii=False),
                speaker_name,
            )
    except Exception:
        pass
    # 兜底：LLM 不可用时，仅给中性致歉（不暴露内部细节）
    # Phase 20 Part13：统一走 persona.tool_failed，避免硬编码另一套人格。
    try:
        from core.persona import tool_failed as _persona_tool_failed
        _fallback_msg = _persona_tool_failed()
    except Exception:
        _fallback_msg = "抱歉，这个操作我没能完成，换个说法再试试。"
    return [], 0, [], _fallback_msg, "", None, "", None, None, "user", {}, None


def _parse_reply(
    raw: str,
    speaker_name: str = "",
    quiet: bool = False,
) -> tuple:
    """解析 LLM JSON 回复，返回标准 11 元组。quiet=True 时 JSON 失败不告警"""
    if not raw:
        return [], 0, [], "", "", None, "", None, None, "user", {}, None

    try:
        from core.trace import span as _trace_span
        with _trace_span("json_parse"):
            raw = raw.lstrip('\ufeff')  # 去掉 BOM
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
            raw = re.sub(r'\s*```$', '', raw)
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                raw = raw[start:end + 1]
            raw = re.sub(r'//[^\n]*', '', raw)
            data = json.loads(raw)
            data = _normalize_reply_json(data)
            replies = data.get("replies", [])
        if not isinstance(replies, list) or not replies:
            raw_cleaned, fav_change = _extract_fav_change(raw)
            replies = _clean_sentences(raw_cleaned)
            return replies, fav_change, [], "", "", None, "", None, None, "user", {}, None
        if isinstance(replies[0], list):
            replies = [str(r) for r in replies[0]]
        else:
            replies = [str(r) for r in replies]

        fav_change = data.get("fav", 0)
        calls = data.get("calls", [])
        face_cq = ""
        face = data.get("face")
        if face:
            try:
                from modules.face_lib import get_face, make_cq
                fp = get_face(str(face).strip())
                if fp:
                    face_cq = make_cq(fp)
            except Exception:
                pass

        mood = data.get("mood", "")
        mood_detail = data.get("mood_detail")
        action = data.get("action", "")
        at_qq = data.get("at")
        mode_switch = data.get("mode")
        origin = data.get("origin", "user")
        actor = data.get("actor") or {}
        instructs = data.get("instructs")

        logger.info("JSON回复解析: %d句 fav=%+d calls=%d mood=%s",
                   len(replies), fav_change, len(calls), mood)
        return replies, fav_change, calls, face_cq, mood, mood_detail, action, at_qq, mode_switch, origin, actor, instructs

    except json.JSONDecodeError:
        if not quiet:
            logger.warning("JSON解析失败，尝试修复: %s...", raw[:80])
        try:
            # 先尝试补全截断的 JSON（加缺失的 }]）
            fixed = raw.rstrip()
            # 找到最后一个完整的字符串结尾
            last_complete = fixed.rfind('",')
            if last_complete > 0:
                fixed = fixed[:last_complete] + '"],"fav":0,"calls":[]}'
                try:
                    data = json.loads(fixed)
                    replies = data.get("replies", [])
                    if isinstance(replies, list) and replies:
                        fav_change = data.get("fav", 0)  # 与主成功路径一致，避免未定义 safe_fav
                        calls = data.get("calls") or []
                        face_cq = data.get("face")
                        mood = data.get("mood", "")
                        mood_detail = data.get("mood_detail")
                        action = data.get("action", "")
                        at_qq = data.get("at")
                        mode_switch = data.get("mode")
                        origin = data.get("origin", "user")
                        actor = data.get("actor") or {}
                        instructs = data.get("instructs")
                        logger.info("JSON截断修复: %d句", len(replies))
                        return replies, fav_change, calls, face_cq, mood, mood_detail, action, at_qq, mode_switch, origin, actor, instructs
                except Exception:
                    pass

            m = re.search(r'"replies"\s*:\s*\[', raw)
            if m:
                inner = raw[m.end():]
                depth = 0
                end_pos = -1
                for i, ch in enumerate(inner):
                    if ch == '[': depth += 1
                    elif ch == ']':
                        if depth == 0:
                            end_pos = i
                            break
                        depth -= 1
                if end_pos >= 0:
                    parts = []
                    in_str = False
                    cur = ""
                    repl_inner = inner[:end_pos]
                    for i, ch in enumerate(repl_inner):
                        if ch == '"':
                            in_str = not in_str
                        elif ch == ',' and not in_str:
                            parts.append(cur.strip().strip('"').strip("'"))
                            cur = ""
                            continue
                        cur += ch
                    if cur.strip():
                        parts.append(cur.strip().strip('"').strip("'"))
                    parts = [p for p in parts if p]
                    if parts:
                        fm = re.search(r'"fav"\s*:\s*(-?\d+)', raw)
                        fv = int(fm.group(1)) if fm else 0
                        return parts, fv, [], "", "", None, "", None, None, "user", {}, None
        except Exception:
            pass
        return [], 0, [], "", "", None, "", None, None, "user", {}, None


def normalize_final_reply(raw: str) -> Optional[str]:
    """Phase 20 Hotfix B：统一的「最终回复安全边界」归一化入口。

    LLM output → normalize → Reply → delivery。任何情况下都不允许把原始 JSON /
    ```json 围栏原文直接送进发送层：

    - 入参为空 → 返回 None（调用方需提供 fallback）。
    - 纯文本（非 JSON）→ 原样返回（正常 Agent 回复放行）。
    - JSON / ```json 包裹 → 复用 _parse_reply 提取 replies（含截断修复路径）；
      提取成功 → 拼接 replies 返回；提取失败 → 返回 None。
    - 返回 None 一律表示「无法得到有效回复」，由调用方走 persona fallback 或
      工具成功结果兜底，绝不把原始 JSON 泄漏给用户。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip().lstrip('\ufeff')
    # 去掉 ```json 围栏后判断是否看起来是 JSON
    stripped = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE).strip()
    stripped = re.sub(r'\s*```$', '', stripped)
    # 纯文本：直接放行（不强制解析，避免误伤正常 Agent 回复）
    if not (stripped.startswith('{') or stripped.startswith('[')):
        return text
    replies, *_ = _parse_reply(text, quiet=True)
    out = "\n".join(str(r) for r in replies if str(r).strip()).strip()
    return out or None


def _build_messages(
    msg_history: list[str],
    speaker_name: str,
    current_msg: str,
    bot_name: str,
    system_prompt: str,
    is_group: bool,
    extra_info: str,
    system_text_override: str | None = None,
) -> list[dict]:
    """构建 messages 列表（与 generate_multi_reply 相同的格式）

    system_text_override 非空时：直接用该文本作为 system 消息内容（Phase 9
    Context Engine 已按 Stable/Dynamic + 预算组装好的 system 上下文），跳过
    内部 _build_system_text 重建，避免重复注入。
    """
    # 拆 PERSONA:::{json}:::{原system} 标记（与 generate_multi_reply 一致）
    _custom_persona = None
    _personality = system_prompt
    if system_prompt.startswith("PERSONA:::"):
        parts = system_prompt.split(":::", 2)
        if len(parts) == 3:
            try:
                _custom_persona = json.loads(parts[1])
            except Exception:
                _custom_persona = None
            _personality = parts[2]
    if system_text_override is not None:
        msgs = [{"role": "system", "content": system_text_override}]
    else:
        msgs = [{"role": "system", "content": _build_system_text(bot_name, _personality, is_group, custom_persona=_custom_persona)}]
    # 按 bot_name: 前缀判断角色（与 _build_history_messages 一致）
    # 群聊中多个用户连续发言时，奇偶索引会把第 2/4/6 条 user 消息错误标为 assistant，
    # 导致 LLM 看到混乱的对话历史，返回空内容或非 JSON 输出
    for line in msg_history:
        line = line.strip()
        if not line:
            continue
        is_bot = False
        content = line
        for sep in [f"{bot_name}: ", f"{bot_name}："]:
            if line.startswith(f"{bot_name}{sep}"):
                is_bot = True
                content = line[len(bot_name) + len(sep):].strip()
                break
        role = "assistant" if is_bot else "user"
        msgs.append({"role": role, "content": content})

    user_parts = []
    # 大消息（题目/长文）→ 去记忆，给 LLM 省上下文
    is_long = len(current_msg) > 1500
    if extra_info and not is_long:
        user_parts.append(f"【上下文】\n{extra_info}")
        ctx_hint = "优先用上下文+自身知识回答，上下文够用就别搜。"
    else:
        ctx_hint = "如果你不了解，可以调用搜索工具查一下。"
    max_chars = "40" if is_group else "12"
    fmt_reminder = (
        "★★★ 最重要规则：你的全部回复必须是 JSON 格式，绝不允许输出纯文本 ★★★\n"
        f"{ctx_hint}\n"
        "用户让你写代码/做游戏/做网页/写脚本时，必须调用 write_code 工具，不要口头承诺。出题/写文章/答疑等直接文字回答。"
        "★ 搜索规则：用户没说'搜/查/找/介绍一下'就绝对不要搜，用你自己的知识回答。不用工具就直接输出 JSON。\n"
        f'回复格式: {{"replies":["回复"],"fav":0,"calls":[],"face":null,"mood":"开心","action":"","at":null,"mode":null,"origin":"user","actor":{{"name":"{speaker_name}","qq":0}}}}\n'
        f"回复 1~3 句，每句≤{max_chars}字。fav -5~+5。严格按照这个 JSON 格式输出！"
    )
    user_parts.append(fmt_reminder)
    # 长消息截断：保留前 2500 字（够题目描述+要求），防止 flash 模型吃不下
    msg_text = current_msg[:2500] + ("…[截断]" if len(current_msg) > 2500 else "")
    user_parts.append(f"{speaker_name} 发消息：「{msg_text}」")
    msgs.append({"role": "user", "content": "\n".join(user_parts)})
    return msgs


async def generate_multi_reply(
    msg_history: list[str],
    speaker_name: str,
    current_msg: str,
    bot_name: str,
    system_prompt: str,
    reply_model: ModelConfig,
    is_group: bool = True,
    extra_info: str = "",
    max_tokens: int = 8000,
) -> tuple[list[str], int]:
    """
    调用主回复模型，生成多句回复 + 好感度变化。

    采用多轮对话格式，利用 API 前缀缓存：
    - system: 静态人设 + 格式规则（每次相同，缓存命中率最高）
    - 历史: user/assistant 多轮（前面不变的部分也能命中缓存）
    - 最后一条 user: 当前消息 + 动态记忆/搜索信息（变化部分集中在末尾）

    v1.1.2: 群聊/私聊使用不同格式规则。群聊最多5句、允许颜文字；
            私聊最多8句、简短、只用文字表情(QAQ/OuO/QwQ)、用你我他称呼。

    Args:
        msg_history: 上下文历史消息列表
        speaker_name: 说话者名称
        current_msg: 当前消息文本
        bot_name: 机器人名字
        system_prompt: 系统提示词（人设）
        reply_model: 回复模型配置
        is_group: 是否群聊
        extra_info: 额外信息（记忆/搜索结果等）
        max_tokens: 最大 token 数

    Returns:
        (句子列表, 好感度变化值, CALL列表, FACE_CQ码)
    """
    # 1. 从 main_skill.md 组装 system 消息（群聊/私聊不同）
    #    system_prompt 形如 "PERSONA:::{json}:::{原system}" 时拆出 custom_persona dict 注入
    _custom_persona = None
    _personality = system_prompt
    if system_prompt.startswith("PERSONA:::"):
        parts = system_prompt.split(":::", 2)
        if len(parts) == 3:
            try:
                _custom_persona = json.loads(parts[1])
            except Exception:
                _custom_persona = None
            _personality = parts[2]

    system_text = _build_system_text(
        bot_name=bot_name,
        personality=_personality,
        is_group=is_group,
        custom_persona=_custom_persona,
    )

    # 2. 解析历史为多轮 user/assistant 消息（排除最后一条，因为它就是当前消息，
    #    会作为独立的最后一条 user 消息追加）
    history = msg_history[:-1] if msg_history else []
    turns = _build_history_messages(history, bot_name)

    # 3. 构造最后一条 user 消息：动态信息 + 格式提醒 + 当前发言
    user_parts = []
    if extra_info:
        user_parts.append(f"【当前可用的搜索/记忆信息】\n{extra_info}\n请参考以上信息回答，如果信息不相关可忽略。")
    # ★ 格式提醒：JSON 输出
    max_chars = "40" if is_group else "12"
    fmt_reminder = (
        "【格式规则：严格输出 JSON，不要任何额外文字】"
        "\n"
        '{"replies":["完整的第一句话","自然的第二句话"],"mood_detail":["开心","好奇"],"fav":2,"calls":[],"face":null,"mood":"开心","action":"摇了摇尾巴","at":null,"mode":null,"origin":"user","actor":{"name":"当前发言者","qq":发言人QQ号}}'
        "\n"
        f"日常回复 1~3 句，每句≤{max_chars}字。复杂问题 3~6 句，每句≤150 字，展开说透。fav -5~+5。"
            "\n"
                        "mood: 当前整体情绪。mood_detail: 每句话对应情绪数组(和replies一一对应)。action: 动作描写。at: @的QQ号，不@就null。mode: 模式切换。face: 极少用，通常null。"
            "\n"
            "origin: 谁发起操作(user/bot)。actor: 替谁执行({name,qq})，bot发起时actor=null。"
            "\n"
            '【origin和actor必须填！每句JSON都要包含这两个字段，origin不填默认当user，actor填发言者名字和QQ】'
            "\n"
            '不要输出残缺URL（如单独的"https:"），不知道完整链接就说不知道！'
            "\n"
            '【指令调用规则：如果有人要求你执行一个操作（如发战报/查天气/搜百度/发群统计等），必须通过calls执行对应指令，不要只回文字假装做了。calls里填指令名和参数，replies里说一句简短的"好的喵~"即可。】'
            "\n"
            '【致命规则：replies 内必须用标准 JSON，英文引号必须转义为 \\" ，或用中文引号「」替代！】'
            "\n"
            "【禁止：JSON之后严禁加任何注释、说明、//、/*、```、换行文字！】"
        )
    user_parts.append(fmt_reminder)
    user_parts.append(f"{speaker_name}说：{current_msg}")
    turns.append({"role": "user", "content": "\n\n".join(user_parts)})

    # 4. 组装 messages：system + 固定锚点 + 多轮历史 + 当前消息
    #    锚点消息永远不变，保证前缀缓存命中（即使历史被 FIFO 裁剪）
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": _MULTI_REPLY_ANCHOR},
    ]
    # 上下文超 60 轮时裁到最近 40，防止 JSON 模式拒答
    if len(turns) > 60:
        logger.warning("上下文过长(%d轮)，裁剪至最近40轮", len(turns))
        turns = turns[-40:]
    messages.extend(turns)

    logger.info("开始多句回复生成 | speaker=%s | history_turns=%d | extra=%d字",
               speaker_name, len(turns) - 1, len(extra_info))

    raw = await call_llm(reply_model, messages, max_tokens=max_tokens, temperature=0.4, json_mode=True)
    if not raw:
        logger.warning("多句回复生成为空，将使用 fallback")
        return [], 0, [], "", "", "", None, None, "user", {}, [], []

    logger.debug("多句生成原始输出: %s", raw[:200])

    # ------JSON解析（含防幻觉清洗）------
    try:
        # 0. 去掉 BOM
        raw = raw.lstrip('\ufeff')
        # 1. 去掉 markdown 代码块
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw)
        # 2. 截取第一个 { 到最后一个 }，扔掉前后垃圾
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        # 3. 去掉行尾 // 注释
        raw = re.sub(r'//[^\n]*', '', raw)
        data = json.loads(raw)
        # ── schema 校验 + 自动补缺 ──
        data = _normalize_reply_json(data)
        replies = data.get("replies", [])
        if not isinstance(replies, list) or not replies:
            raw_cleaned, fav_change = _extract_fav_change(raw)
            replies = _clean_sentences(raw_cleaned)
            logger.info("回退旧格式: %d句", len(replies))
            return replies, fav_change, [], "", "", "", None, None, None, "user", {}, []
        if isinstance(replies[0], list):
            replies = [str(r) for r in replies[0]]
        else:
            replies = [str(r) for r in replies]

        fav_change = data.get("fav", 0)
        calls = data.get("calls", [])

        face_cq = ""
        face = data.get("face")
        if face:
            try:
                from modules.face_lib import get_face, make_cq
                fp = get_face(str(face).strip())
                if fp:
                    face_cq = make_cq(fp)
            except Exception:
                pass

        mood = data.get("mood", "")
        mood_detail = data.get("mood_detail")  # 每句情绪数组
        instructs = data.get("instructs") or []  # 每句语气指令（语音模式）
        action = data.get("action", "")
        at_qq = data.get("at")
        mode_switch = data.get("mode")
        origin = data.get("origin", "user")
        actor = data.get("actor") or {}

        logger.info("JSON回复解析: %d句 fav=%+d calls=%d mood=%s action=%s",
                   len(replies), fav_change, len(calls), mood)
        return replies, fav_change, calls, face_cq, mood, mood_detail, action, at_qq, mode_switch, origin, actor, instructs
    except json.JSONDecodeError:
        logger.warning("JSON解析失败，尝试修复: %s...", raw[:80])
        # 修复 JSON 中最常见错误：replies 数组里未转义的双引号
        try:
            # 1. 用正则定位 replies 数组
            m = re.search(r'"replies"\s*:\s*\[', raw)
            if m:
                bracket_start = m.end() - 1  # [
                # 找到对应的 ]
                depth = 0
                bracket_end = -1
                for i in range(bracket_start, len(raw)):
                    if raw[i] == '[':
                        depth += 1
                    elif raw[i] == ']':
                        depth -= 1
                        if depth == 0:
                            bracket_end = i
                            break
                    if bracket_end > bracket_start:
                        arr_text = raw[bracket_start + 1:bracket_end]
                        # 按 "," 切分（JSON 数组元素分隔符）
                        # 这样内部的引号不会被误切
                        entries = arr_text.split('","')
                        parts = []
                        for i, e in enumerate(entries):
                            if i == 0:
                                e = e.lstrip('"')
                            if i == len(entries) - 1:
                                e = e.rstrip('"')
                            # 去掉首尾可能残留的引号
                            e = e.strip().strip('"').strip()
                            if e:
                                parts.append(e)
                        if parts:
                            fav_m = re.search(r'"fav"\s*:\s*(-?\d+)', raw)
                            fav_change = max(-5, min(5, int(fav_m.group(1)) if fav_m else 0))
                            logger.info("修复提取: %d句 fav=%+d", len(parts), fav_change)
                            return parts, fav_change, [], "", "", None, "", None, None, "user", {}
        except Exception:
            pass
        logger.warning("修复也失败，回退旧格式: %s...", raw[:80])
        raw = raw.replace("\\n", "\n")
        raw_cleaned, fav_change = _extract_fav_change(raw)
        sentences = _clean_sentences(raw_cleaned)
        logger.info("多句回复生成完成(旧格式): %d句 fav=%+d", len(sentences), fav_change)
        return sentences, fav_change, [], "", "", None, "", None, None, "user", {}


# ── 判断模型调用 ────────────────────────────────────────────

async def judge_interest(
    msg: str,
    sender_name: str,
    context_str: str,
    judge_model: ModelConfig,
    personality_core: str,
) -> int:
    """
    调用精细兴趣度判断模型（第三级），返回 0~10 的兴趣度分数。
    """
    system = (
        f"你叫{get_config().bot_name}，消息记录:{context_str} "
        f"请输出你对'{msg}'的感兴趣的程度(0~10)，只输出数字，不能带有其他内容。"
        f"你的人设：{personality_core}，如果消息与你无关可输出2，非常相关输出10。"
        "如果消息是纯表情、无意义或私人对话，不应回复（可输出0）。"
    )
    result = await call_llm(
        judge_model,
        [{"role": "system", "content": system}],
        max_tokens=2,
        temperature=0.5,
        timeout=15.0,
    )
    
    digits = "".join(c for c in result if c.isdigit())
    if not digits:
        return 0
    interest = int(digits)
    interest = min(10, max(0, interest))
    logger.debug("精细兴趣度判断: msg='%s...' → %d/10", msg[:30], interest)
    return interest


async def judge_should_reply_cheap(
    msg: str,
    context_str: str,
    cheap_model: ModelConfig,
) -> bool:
    """
    调用廉价判断模型（第二级），返回是否应该回复（bool）。
    """
    bot_name = get_config().bot_name
    prompt = (
        f"你是群聊管家。机器人叫{bot_name}。根据聊天记录判断机器人是否应回复新消息。\n"
        f"【最近对话】\n{context_str}\n"
        f"【新消息】{msg}\n"
        "如果机器人需要回应（被直接提及、问题可解答、话题高度相关），输出1；否则输出0。只输出数字。"
    )
    result = await call_llm(
        cheap_model,
        [{"role": "user", "content": prompt}],
        max_tokens=1,
        temperature=0,
        timeout=10.0,
    )
    should = result.strip() == "1"
    logger.debug("廉价判断模型: msg='%s...' → %s", msg[:30], "REPLY" if should else "SKIP")
    return should


async def judge_need_search(
    msg: str,
    context_str: str,
    cheap_model: ModelConfig,
) -> bool:
    """判断消息是否需要联网搜索"""
    bot_name = get_config().bot_name
    prompt = (
        f"你是一个搜索判断助手。机器人叫{bot_name}，正在群聊中。\n"
        f"最近对话：{context_str}\n"
        f"新消息：{msg}\n"
        "这条消息是否需要联网查询实时信息或未知知识才能准确回答？"
        "如果需要，输出1；否则输出0。只输出数字。"
    )
    result = await call_llm(
        cheap_model,
        [{"role": "user", "content": prompt}],
        max_tokens=1,
        temperature=0,
        timeout=10.0,
    )
    need = result.strip() == "1"
    logger.debug("搜索判断: msg='%s...' → %s", msg[:30], "SEARCH" if need else "NO_SEARCH")
    return need


# ── 并行调用工具 ────────────────────────────────────────────

async def call_judgment_pipeline(
    msg: str,
    sender_name: str,
    context_str: str,
    personality: str,
    cheap_model: ModelConfig | None,
    judge_model: ModelConfig | None,
) -> tuple[int, bool, bool]:
    """
    判断管道 — 相同模型时合并为一次调用
    Returns: (兴趣度分数 0~10, 廉价判断 bool, 是否在问架构 bool)
    """
    # 相同模型 → 合并为一次调用
    has_cheap = cheap_model and cheap_model.url and cheap_model.key and cheap_model.name
    has_judge = judge_model and judge_model.url and judge_model.key and judge_model.name

    if has_cheap and has_judge and cheap_model.name == judge_model.name:
        return await _judge_combined(msg, sender_name, context_str, cheap_model, personality)

    # 不同模型或仅一个 → 并行调用
    tasks = {}
    if has_cheap:
        tasks["cheap"] = asyncio.create_task(judge_should_reply_cheap(msg, context_str, cheap_model))
    if has_judge:
        tasks["interest"] = asyncio.create_task(judge_interest(msg, sender_name, context_str, judge_model, personality))

    interest_score = 0
    should_reply_cheap = False
    is_arch = False
    for name, task in tasks.items():
        try:
            result = await task
            if name == "cheap":
                should_reply_cheap = bool(result)
            elif name == "interest":
                interest_score = int(result) if result else 0
        except Exception as e:
            logger.warning("判断 '%s' 出错: %s", name, e)

    logger.debug("并行判断完成: cheap=%s interest=%d arch=N/A", should_reply_cheap, interest_score)
    return interest_score, should_reply_cheap, is_arch


async def _judge_combined(
    msg: str, sender_name: str, context_str: str,
    model: ModelConfig, personality_core: str,
) -> tuple[int, bool, bool]:
    """合并判断：一次 API 返回 cheap+interest+arch"""
    bot_name = get_config().bot_name
    prompt = (
        f"你是群聊助手 {bot_name}。回答三个问题，只输出 X|Y|Z：\n"
        f"X=1 机器人应回复 / 0 不应回复\n"
        f"Y=对消息的兴趣度 0~10\n"
        f"Z=1 消息在问机器人的架构/版本/模型/能力 / 0 不是\n\n"
        f"【人设】{personality_core}\n"
        f"【上下文】{context_str}\n"
        f"【新消息】{msg}\n"
    )
    result = await call_llm(model, [{"role": "user", "content": prompt}], max_tokens=20, temperature=0.4, timeout=15.0)
    result = result.strip()
    logger.debug("合并判断: '%s...' → '%s'", msg[:30], result)
    try:
        parts = result.replace(" ", "").split("|")
        cheap = parts[0].strip() == "1" if len(parts) >= 1 else False
        interest = int("".join(c for c in parts[1] if c.isdigit())) if len(parts) >= 2 else 0
        is_arch = parts[2].strip() == "1" if len(parts) >= 3 else False
        return min(10, max(0, interest)), cheap, is_arch
    except Exception:
        return 0, False, False


async def call_summary_model(
    messages: list[str],
    summary_model: ModelConfig,
    max_tokens: int = 80,
) -> str:
    """
    调用摘要模型总结对话记录。
    """
    from utils.format_lang import format_lang
    
    prompt = format_lang("memory.summarize_prompt", conversation="\n".join(messages[-30:]))
    
    result = await call_llm(
        summary_model,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.4,
        timeout=30.0,
    )
    
    if result:
        logger.info("对话摘要生成: %s...", result[:50])
    else:
        logger.warning("对话摘要生成为空")
    
    return result or ""
