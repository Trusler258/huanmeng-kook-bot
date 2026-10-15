"""决策模型客户端（OpenRouter Decisions API / Inception Mercury Decide）。

为什么单独做一层：把"结构化判定"交给**专用决策模型**，而不是让回复模型自己守规矩、
或用关键词清单去猜。决策模型不生成文本，只回答类型化问题、返回**校准概率**，
代码直接拿概率做分流 —— 没有解析胶水。

方案（2026-10-14 实测选定，**免费**）：
    端点 POST https://openrouter.ai/api/alpha/decisions
    模型 inception/mercury-decide:free
    ⚠️ 该模型不能用 chat/completions，必须走 /api/alpha/decisions。
    与 Jev / System One 同构：state + questions，题型 noul / choice / score。
    实测 6/6 判定正确、延迟 1.7~3.6s、usage.cost=0。

设计铁律（用户 2026-10-14 明确的三条顾虑）：
  1. **fail-open**：任何异常 / 超时 / 非 200 / 解析失败 → 返回 None，**绝不阻断主流程**。
     架构上让决策层"没有能力"影响回复的发出。
  2. **不新增串行延迟**：调用方应**并发**发起，或先用便宜预筛挡掉大部分消息。
  3. **默认关闭**：是否启用由上层开关决定（见 `enabled()`），不开时行为与从前完全一致。

用法：
    from services import decision
    need, prob = await decision.need_search("恒大现状")   # (True, 0.998) / (None, None)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.logger import get_logger

logger = get_logger("service.decision")

_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "bot_config.toml"

_URL_DEFAULT = "https://openrouter.ai/api/alpha/decisions"
_MODEL_DEFAULT = "inception/mercury-decide:free"
_TIMEOUT = 8.0

# ── 提问模板（改这里即可调整判定口径）────────────────────────────────
_Q_NEED_SEARCH = {
    "type": "noul",
    "instructions": "这条消息是否需要联网查询实时/最新信息才能准确回答？",
    "criteria": {
        "true": "涉及具体实体的当前状态、进展、价格、股价、人事变动、近期事件等会随时间变化的事",
        "false": "常识、技术原理、闲聊、情绪表达、机器人自身信息等答案不随时间变化的内容",
    },
}


def _key() -> str:
    return os.environ.get("OPENROUTER_KEY", "") or os.environ.get("DECISION_KEY", "")


def _url() -> str:
    return os.environ.get("DECISION_URL", "") or _URL_DEFAULT


def _model() -> str:
    return os.environ.get("DECISION_MODEL", "") or _MODEL_DEFAULT


# ── 启用开关与参数（读 bot_config.toml [decision]）────────────────────
def _cfg() -> dict:
    try:
        import toml
        return toml.load(_CFG_PATH).get("decision", {}) or {}
    except Exception:
        return {}


def threshold() -> float:
    try:
        return float(_cfg().get("threshold", 0.5))
    except Exception:
        return 0.5


def call_timeout() -> float:
    try:
        return float(_cfg().get("timeout", 6.0))
    except Exception:
        return 6.0


def enabled() -> bool:
    """是否启用决策模型：**配置了 key** 且未在 [decision] enabled=false 里显式关闭。

    未配置 → False，调用方自动回退原判定逻辑（关键词 _detect_skill_needs / LLM judge）。
    """
    if not _key():
        return False
    return bool(_cfg().get("enabled", True))


def available() -> bool:
    """是否具备调用条件（有 key）。不代表网络一定通。"""
    return bool(_key())


def startup_notice() -> str:
    """启动时打印的一行状态提示（给运维看，不影响逻辑）。"""
    if not _key():
        return ("未配置决策模型（config/.env 缺 OPENROUTER_KEY）→ 自动使用原判定逻辑"
                "（关键词 _detect_skill_needs + LLM judge）")
    if not _cfg().get("enabled", True):
        return "决策模型已配置但 [decision] enabled=false → 使用原判定逻辑"
    return (f"决策模型已启用: {_model()} | 阈值 {threshold()} | 超时 {call_timeout()}s"
            "（fail-open：不可用时自动回退原逻辑）")


async def decide(state, questions: dict, *, timeout: float = _TIMEOUT):
    """调用决策模型，返回 `answers` dict；任何失败返回 None（fail-open）。

    state: 待判定内容（字符串；复杂场景可传 dict/list）
    questions: {问题ID: {"type": "noul|choice|score", "instructions": "...", "criteria": ...}}
    """
    key = _key()
    if not key:
        logger.debug("决策模型未配置 key，跳过")
        return None

    payload = {"model": _model(), "state": state, "questions": questions}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(
                _url(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://qqbot.local",
                    "X-Title": "huanmeng-bot",
                },
                json=payload,
            )
        if r.status_code != 200:
            logger.warning("决策模型 HTTP %s（fail-open）: %s", r.status_code, (r.text or "")[:120])
            return None
        data = r.json() or {}
        ans = data.get("answers")
        if not isinstance(ans, dict):
            logger.warning("决策模型响应无 answers（fail-open）: %s", str(data)[:120])
            return None
        return ans
    except Exception as e:  # noqa: BLE001 —— fail-open：绝不把异常抛给主流程
        logger.warning("决策模型调用失败（fail-open）: %s: %s", type(e).__name__, str(e)[:120])
        return None


async def need_search(state, *, threshold: float = 0.5, timeout: float = _TIMEOUT):
    """判定「是否需要联网」。

    返回 (need, prob)：
      - (True,  p)  → p >= threshold
      - (False, p)  → p <  threshold
      - (None, None) → 不可用 / 失败（调用方应按"不强制搜索"处理）
    """
    ans = await decide(state, {"realtime": _Q_NEED_SEARCH}, timeout=timeout)
    if not ans:
        return None, None
    q = ans.get("realtime") or {}
    p = q.get("noul")
    if not isinstance(p, (int, float)):
        return None, None
    return (p >= threshold), float(p)


# ── 通用路由判定（替代关键词版 _detect_skill_needs）──────────────────
# 一次调用同时问多个问题（决策模型支持并行多题，加题几乎不增延迟）。
# 命中项命名与 _detect_skill_needs 的 needs 对齐（tools/face/play/deep）+ 新增 search/code/memory。
_ROUTE_QUESTIONS = {
    "search": {
        "type": "noul",
        "instructions": "要准确回答这条消息，是否需要联网查询实时/最新信息？",
        "criteria": {
            "true": "涉及具体实体的当前状态、进展、价格、股价、人事、近期事件等会随时间变化的事",
            "false": "常识、技术原理、闲聊、情绪、机器人自身信息等不随时间变化的内容",
        },
    },
    "code": {
        "type": "noul",
        "instructions": "这条消息是否要求写代码 / 跑代码 / 调试程序？",
        "criteria": {
            "true": "明确要写、改、解释或运行一段代码，或排程序 bug",
            "false": "只是普通聊天、问概念、或与编程无关",
        },
    },
    "deep": {
        "type": "noul",
        "instructions": "这条消息是否需要较长篇幅的深度讲解（而非一两句就够）？",
        "criteria": {
            "true": "要求详细解释、原理、步骤、对比分析、教程等需要展开的内容",
            "false": "寒暄、简单确认、一句话能答完的内容",
        },
    },
    "tools": {
        "type": "noul",
        "instructions": "这条消息是否需要用外部工具/指令才能答（如查天气、查战绩、查快递、查谱面、画像等）？",
        "criteria": {
            "true": "需要查具体数据或调用某个专门功能",
            "false": "聊天即可回答，无需外部数据",
        },
    },
    "face": {
        "type": "noul",
        "instructions": "这条消息是否在要表情包 / 颜文字 / 表情？",
        "criteria": {"true": "明确在要表情类内容", "false": "不是"},
    },
    "play": {
        "type": "noul",
        "instructions": "这条消息是否要求角色扮演 / 整活 / 玩梗？",
        "criteria": {"true": "在要求扮演某个角色或整活", "false": "不是"},
    },
    "memory": {
        "type": "noul",
        "instructions": "这条消息是否在要求记住某事 / 回忆某事 / 修改记忆？",
        "criteria": {"true": "明确要求记、查或改记忆", "false": "不是"},
    },
}


# ── 工具选择（B：让决策模型指定该调哪个工具）────────────────────────
# 选项**从真实工具表动态派生** → 插件 register_tool 新增的工具自动纳入，无需改这里。

def tool_options(cap: int = 40) -> dict:
    """{工具名: 一句话说明}，取自 core.tools 的工具表（内置 + 插件动态注册）。"""
    opts: dict[str, str] = {}
    try:
        from core.tools import get_tool_schemas
        for s in get_tool_schemas():
            fn = (s or {}).get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            desc = (fn.get("description") or "").replace("\n", " ").strip()
            opts[name] = desc[:cap] or name
    except Exception as e:
        logger.debug("取工具表失败（工具选择题降级）: %s", e)
    opts["无"] = "不需要任何工具"
    return opts


def tool_force_threshold() -> float:
    """工具选择置信度阈值：达到才强制调用。

    设阈值是为了**防决策模型判错时硬调错工具**（实测 draw_card 两条全判错）。
    """
    try:
        return float(_cfg().get("tool_force_threshold", 0.7))
    except Exception:
        return 0.7


def think_on_tools() -> bool:
    """`tools` 维度命中时是否也开思考模式（默认开）。

    实测依据：关思考时工具调用率 0/20，开思考 7/20。开思考会增加耗时，故留开关。
    """
    try:
        return bool(_cfg().get("think_on_tools", True))
    except Exception:
        return True


async def route(message, *, threshold: float = 0.5, timeout: float = _TIMEOUT,
               speaker_name: str = "", group_nicknames=None):
    """一次调用判定本轮需要哪些能力 + 最该调用哪个工具。

    返回 (needs: set[str], probs: dict[str, float], tool: dict | None)：
      - needs  命中项（>= threshold），命名与 _detect_skill_needs 对齐 + search/code/memory
      - tool   {"name": ..., "confidence": ...}；仅当最高概率 >= tool_force_threshold()
               且不是「无」时给出，否则 None（调用方按 tool_choice="auto" 处理）
      - 失败/不可用 → (None, None, None)，调用方**回退原逻辑**（fail-open）
    """
    questions = dict(_ROUTE_QUESTIONS)
    questions["which_tool"] = {
        "type": "choice",
        "instructions": "这条消息最该调用下面哪个工具？（无需工具时选「无」）",
        "criteria": tool_options(),
    }
    ans = await decide(_compose_state(message, speaker_name, group_nicknames),
                       questions, timeout=timeout)
    if not ans:
        return None, None, None
    needs, probs = set(), {}
    for k in _ROUTE_QUESTIONS:
        q = ans.get(k) or {}
        p = q.get("noul")
        if isinstance(p, (int, float)):
            probs[k] = round(float(p), 4)
            if p >= threshold:
                needs.add(k)
    tool = None
    wp = (ans.get("which_tool") or {}).get("probabilities") or {}
    if wp:
        name, conf = max(wp.items(), key=lambda kv: kv[1])
        if name != "无" and float(conf) >= tool_force_threshold():
            tool = {"name": name, "confidence": round(float(conf), 4)}
    return needs, probs, tool


# ── 回复判定（替代原 LLM judge / _judge_combined）────────────────────
# ⚠️ score 的 criteria 上限 10 档（11 档会 422）；score 返回的是**期望档位值**（0..n-1）。
_INTEREST_LEVELS = ["完全无关", "几乎无关", "略相关", "有一点相关", "中等相关",
                    "较相关", "相关", "很相关", "非常相关", "极其相关"]

_Q_JUDGE = {
    # ★ 关键一题：**是否在跟幻梦说话**。带上下文一起判，能抓到隐晦代称（猫娘/梦梦/小猫咪）。
    #   实测（50 例 × 上下文 3/10/15/20/30 条）100% 正确：
    #   - 被指向 0.98~1.000 ／ 无指向 0.004~0.107 —— 中间是巨大空档，几乎没有模糊地带。
    "addressed": {
        "type": "noul",
        "instructions": ("这条新消息是不是在跟幻梦说话（在叫它、问它、或明确指向它）？\n"
                         "注意：『本群成员』名单里列出的都是**真人/用户**，"
                         "即使有人昵称像机器人名（如叫'初梦bot'），@ 他也**不算**在叫幻梦。"),
        "criteria": {
            "true": "消息明确指向幻梦：@它、叫它名字（幻梦 / 梦梦）、或用它的代称（猫娘 / 小猫咪）",
            "false": "消息是群友之间的对话、向全体提问、或单纯闲聊，没有指向幻梦",
        },
    },
    "reply": {
        "type": "noul",
        "instructions": ("（前提：已确认这条消息是在跟幻梦说话；它可能被称为 幻梦 / 梦梦 / 猫娘 / 小猫咪，"
                         "都算在叫它）它是否应该回复？"),
        "criteria": {
            "true": "正常的问题、请求、对话、打招呼或闲聊——只要在跟它说话就该回应",
            "false": "只有纯刷屏、无意义重复、明显误触/测试，或内容不适合它回应时才为否",
        },
    },
    "interest": {
        "type": "score",
        "instructions": "对这条新消息的兴趣度（0=完全无关，9=极其相关）",
        "criteria": _INTEREST_LEVELS,
    },
    "arch": {
        "type": "noul",
        "instructions": "消息是否在问机器人自身的架构 / 版本 / 模型 / 能力？",
        "criteria": {"true": "在问它用什么模型、什么版本、怎么实现", "false": "不是"},
    },
}


_GROUP_NICKS_PATH = Path(__file__).resolve().parent.parent / "data" / "group_nicknames.json"


def group_nicknames(group_id, exclude=None, cap: int = 120) -> list[str]:
    """本群成员昵称（读 `data/group_nicknames.json` 本地缓存，**不发 API**）。

    为什么要给：群里有真人昵称撞了机器人名（如"初梦bot"）时，模型容易把
    "@初梦bot 你好"误判成在叫幻梦。把成员名单摆出来，模型就能区分"这是群友"。
    exclude: 要排除的名字集合（传 bot_name，避免名单里出现自己造成歧义）。
    """
    if group_id in (None, "", 0):
        return []
    try:
        raw = json.loads(_GROUP_NICKS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    d = raw.get(str(group_id)) or {}
    if not isinstance(d, dict):
        return []
    ex = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    seen, out = set(), []
    for v in d.values():
        n = str(v or "").strip()
        if not n or n in ex or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= cap:
            break
    return out


def _compose_state(msg, speaker_name: str = "", group_nicknames=None) -> str:
    """把「本群成员名单 + 说话人」拼进待判定内容（模型靠它区分真人与机器人）。"""
    parts = []
    if group_nicknames:
        parts.append("本群成员（都是真人/用户，不是机器人）：" + "、".join(group_nicknames))
    if speaker_name:
        parts.append(f"正在说话的人：{speaker_name}")
    parts.append(f"消息：{msg}")
    return "\n".join(parts)
    """给判定用的上下文条数。实测 3/10/15/20/30 条准确率均为 100%、延迟基本持平，
    所以可以放心放大；但在 bot_config.toml [decision] judge_context 可调。"""
    try:
        return max(0, int(_cfg().get("judge_context", 20)))
    except Exception:
        return 20


def judge_context_lines() -> int:
    """给判定用的上下文条数。实测 3/10/15/20/30 条准确率均为 100%、延迟基本持平，
    所以可以放心放大；但在 bot_config.toml [decision] judge_context 可调。"""
    try:
        return max(0, int(_cfg().get("judge_context", 20)))
    except Exception:
        return 20


async def judge(msg, context_str: str = "", personality_core: str = "", *,
                reply_to_bot: bool = False, timeout: float | None = None,
                speaker_name: str = "", group_nicknames=None):
    """一次调用判定 (是否指向 / 是否回复 / 兴趣度 / 是否问架构)。

    返回 `(interest:int 0~10, should_reply:bool, is_arch:bool, addressed:bool)`；
    **失败/不可用 → None**（调用方回退原 judge，fail-open）。

    ⚠️ 合成判定是 `addressed AND reply` —— 只看 `reply` 会误判（无指向的消息 reply 也会很高）。
    reply_to_bot（回复链）为 True 时，会把这一事实写进 state 让模型知情，调用方通常直接视为已指向。
    group_nicknames：本群成员昵称名单，用来区分"真人群友"和"机器人被呼叫"。
    """
    head = ""
    if group_nicknames:
        head += "本群成员（都是真人/用户，不是机器人）：" + "、".join(group_nicknames) + "\n"
    if speaker_name:
        head += f"正在说话的人：{speaker_name}\n"
    if reply_to_bot:
        head += "⚠️ 这条消息是**直接回复/引用**了幻梦的消息（回复链）。\n"
    tail = f"最近对话：\n{context_str}\n新消息：{msg}" if context_str else f"新消息：{msg}"
    ans = await decide(head + tail, _Q_JUDGE, timeout=timeout or call_timeout())
    if not ans:
        return None
    rp = (ans.get("reply") or {}).get("noul")
    if not isinstance(rp, (int, float)):
        return None
    ad = (ans.get("addressed") or {}).get("noul")
    sc = (ans.get("interest") or {}).get("score")
    n = len(_INTEREST_LEVELS)
    interest = round(float(sc) / (n - 1) * 10) if isinstance(sc, (int, float)) and n > 1 else 0
    interest = max(0, min(10, int(interest)))
    arch = float((ans.get("arch") or {}).get("noul") or 0) >= threshold()
    if reply_to_bot:
        ad = 1.0   # 回复链是确定性信号，直接视为已指向
    return (interest, bool(rp >= threshold()), arch,
            bool(ad >= threshold()) if isinstance(ad, (int, float)) else False)


# ── 自测：python3 -m services.decision ──────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import time

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

    CASES = [
        ("恒大现状", True),
        ("今天A股大盘怎么样", True),
        ("红石中继器是什么", False),
        ("你几岁", False),
        ("帮我把这句话翻译成英文", False),
        ("幻梦你用的什么模型", False),
    ]

    async def _main():
        print(f"key 配置: {available()} | 模型: {_model()}")
        ok = 0
        for msg, expect in CASES:
            t = time.time()
            need, prob = await need_search(msg)
            dt = round(time.time() - t, 2)
            verdict = "OK" if need == expect else ("FAIL" if need is not None else "N/A")
            ok += 1 if need == expect else 0
            print(f"  [{dt}s] {verdict:<4} {msg!r:<24} need={need} prob={None if prob is None else round(prob, 4)}")
        print(f"结果: {ok}/{len(CASES)} 判定正确")

        print("\n=== route() 多路判定（一次调用）===")
        ROUTE_CASES = [
            "恒大现状",
            "帮我写个冒泡排序",
            "详细讲讲红石中继器的原理",
            "今天天气怎么样",
            "晚安啦",
        ]
        for msg in ROUTE_CASES:
            t = time.time()
            needs, probs, tool = await route(msg)
            dt = round(time.time() - t, 2)
            if needs is None:
                print(f"  [{dt}s] N/A  {msg!r}（判定失败）")
            else:
                print(f"  [{dt}s] {msg!r:<22} -> {sorted(needs)}  强制工具={tool}  {probs}")

        print("\n=== judge() 回复判定 ===")
        for msg, ctx in [("晚安啦", ""), ("幻梦你用的什么模型", ""),
                         ("你们俩明天几点见", "A: 明天几点见\nB: 十点"),
                         ("今天A股大盘怎么样", "群友在聊股票")]:
            t = time.time()
            jd = await judge(msg, ctx)
            dt = round(time.time() - t, 2)
            print(f"  [{dt}s] {msg!r:<20} -> {jd}")

    asyncio.run(_main())
