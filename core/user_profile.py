"""
用户画像系统（v2.3.34 重做）

架构
────
    每条消息   → bump_activity()   只计数、不调 LLM（零成本）
    每天 00:05 → run_daily()       回看**前一天**的 msglog，按 (会话, 用户) 聚合，
                                   发言足够的组合交给 LLM 增量更新画像

为什么改成这样（v2.3.33 的教训）
──────────────────────────────
旧实现是「每条消息都跑一遍关键词/LLM 提取」，跑一段时间后实测 52 个用户：
昵称只有 12 个对（9 个是 bot 自己的名字）、334 条「已知信息」里 20% 是疑问句、
剩下大量「我是你主人」「我就是神」这类角色扮演玩梗 —— 因为单条消息**没有上下文**，
「我是主人」在私聊和在群里含义完全相反，靠单句规则判断必然出错。

改成每天批量回看后：
  1. 看的是**一整天**的发言 → 有上下文，能分出玩梗和真实表达
  2. 判断依据变成「反复出现的稳定特征」，而不是某一句
  3. 每人每天 1 次 LLM 调用（实测约 25 次/天），比每条都调便宜得多
  4. 增量更新：在旧画像上补充/淘汰，不是每次重写

分域存储（用户明确要求）
──────────────────────
    群聊  g<群号>:<QQ>     例 g1053523927:3483585417
    私聊  p<QQ>           例 p3483585417

同一人在不同群、以及私聊，各有独立画像 —— 群里的身份/语气和私聊未必一致，
混成一份会让 bot 在 A 群说 B 群的事。键格式与 fav.json 保持一致（g<群>:<QQ>）。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from core.logger import get_logger

logger = get_logger("profile")

_ROOT = Path(__file__).resolve().parent.parent
_DATA_FILE = _ROOT / "data" / "user_profiles.json"
_STATE_FILE = _ROOT / "data" / "profile_job_state.json"
_MSGLOG_DIR = _ROOT / "data" / "msglog"
_GROUP_NICK_FILE = _ROOT / "data" / "group_nicknames.json"

# ── 阈值（实测数据定：按 5 条算，每天约 25 人触发，成本可忽略）──
MIN_MSGS = 5              # 当天最少发言条数才建档/更新
MIN_MSGS_EXISTING = 2     # 已有画像的用户，少一些也值得更新
MAX_MSGS = 60             # 单次送 LLM 的消息条数上限（取最近的）
MAX_CHARS = 2200          # 单次送 LLM 的文本上限（超了从最早裁）
LLM_TIMEOUT = 45.0

# 字段定义：key → 注入时的显示名
#
# ★ v2.3.41 改造：原来拆成 身份/常问/偏好/习惯/注意 五个固定字段 ——
#   实践下来问题很明显：LLM 为了"填满字段"会硬凑，甚至把玩梗当身份写进去，
#   读起来也像表格不像人话。
#   改成 **nick + text 两个字段**：text 是一段自然语言，不分点、不带标签。
#   nick 单独保留是因为 .权力 set nick 等逻辑依赖它。
FIELD_LABELS = {
    "nick": "昵称",
    "text": "画像",
}
_FIELDS = tuple(FIELD_LABELS.keys())
_FIELD_MAX = {"nick": 16, "text": 220}
# 旧格式的五个字段（2026-09 之前的数据）。读取时兼容，新写入一律走 text
_LEGACY_FIELDS = ("role", "demands", "preference", "habit", "warning")
_LEGACY_LABELS = {"role": "身份", "demands": "常问", "preference": "偏好",
                  "habit": "习惯", "warning": "注意"}

# LLM 可能用来表示「没信息」的写法，一律当空
_PLACEHOLDER = {"", "未提及", "未知", "无", "暂无", "不清楚", "不确定", "未知晓",
                "none", "null", "n/a", "na", "-", "—", "无信息", "没有提及"}

# 隐私兜底：即使 LLM 违反指令写了这些，也不落盘
_SENSITIVE_RE = re.compile(
    r'(密码|password|token|secret|api.?key|手机号|手机号码|身份证|银行卡|住址|家庭住址|'
    r'真实姓名|微信号|支付宝)',
    re.IGNORECASE,
)
# 疑问句特征（LLM 偶尔会把用户的提问抄进画像）
_QUESTION_RE = re.compile(r'[?？]|谁|啥|什么|哪|吗|呢|多少|怎么|咋|为何|为什么|是不是|有没有|如何')

# 媒体类型占位（保留行为信号，但不送原文）
_MEDIA_TPL = {"图片": "[图片]", "文件": "[文件]", "视频": "[视频]", "语音": "[语音]",
              "转发": "[转发]", "表情": "[表情]"}
_TEXT_TYPES = {"text", "文字", "group"}   # ⚠️ KOOK 的用户消息 type 是 "group"（QQ 是 "text"）


# ══════════════════════════════════════════════════════════
#  分域键
# ══════════════════════════════════════════════════════════

def scope_key(chat_id, user_id, is_group: bool) -> str:
    """生成画像存储键：群聊 g<群号>:<QQ>，私聊 p<QQ>"""
    uid = str(user_id)
    return f"g{chat_id}:{uid}" if is_group else f"p{uid}"


def parse_scope(key: str) -> dict:
    """拆解存储键 → {scope, group, qq}"""
    k = str(key)
    if k.startswith("g") and ":" in k:
        gid, qq = k[1:].split(":", 1)
        return {"scope": "group", "group": gid, "qq": qq}
    if k.startswith("p"):
        return {"scope": "private", "group": "", "qq": k[1:]}
    # 兼容旧格式（裸 QQ 号，全局画像）—— 当作私聊处理
    return {"scope": "private", "group": "", "qq": k}


_group_cache: tuple[float, set[str]] = (0.0, set())


def group_ids() -> set[str]:
    """已知群号集合。用于判断某个 chat 是群还是私聊。

    来源两处并集：group_nicknames.json（nickname_sync 自动写入）
    + adapter_config 的 group_settings。带 mtime 缓存，文件没变就不重复读。
    """
    global _group_cache
    ts, cached = _group_cache
    try:
        mtime = _GROUP_NICK_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime and mtime == ts and cached:
        return cached

    ids: set[str] = set()
    try:
        ids |= {str(k) for k in json.loads(_GROUP_NICK_FILE.read_text(encoding="utf-8"))}
    except Exception:
        pass
    try:
        from core.config import get_config
        ids |= {str(k) for k in (get_config().group_settings or {})}
    except Exception:
        pass
    _group_cache = (mtime, ids)
    return ids


def is_group_chat(chat_id) -> bool:
    return str(chat_id) in group_ids()


# ══════════════════════════════════════════════════════════
#  存储
# ══════════════════════════════════════════════════════════

def _load_all() -> dict[str, dict]:
    if not _DATA_FILE.exists():
        return {}
    try:
        data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("画像文件损坏，重置")
        return {}


def _save_all(data: dict[str, dict]) -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _DATA_FILE)      # 原子替换，避免写一半被读到


def get_profile(chat_id, user_id, is_group: bool) -> dict:
    """取某个分域的画像，不存在返回空骨架"""
    key = scope_key(chat_id, user_id, is_group)
    return _load_all().get(key) or _new_profile(chat_id, user_id, is_group)


def _new_profile(chat_id, user_id, is_group: bool) -> dict:
    now = int(time.time())
    return {
        "qq": str(user_id),
        "scope": "group" if is_group else "private",
        "group": str(chat_id) if is_group else "",
        **{f: "" for f in _FIELDS},
        "days": 0,            # 累计有画像的天数
        "msg_count": 0,       # 累计发言条数
        "first_seen": now,
        "last_msg_at": 0,
        "updated_at": 0,
    }


def bump_activity(chat_id, user_id, is_group: bool) -> None:
    """记一次发言（每条消息调用，纯时间戳更新、零成本、不调 LLM）。

    这里**只更新 last_msg_at**，不累加 msg_count。原因：
      「发言数」由每日任务累加 —— 每条消息恰好会被一个日批次分析一次，
      这样回填历史与日常运行的口径一致，也不会和实时计数重复累加。
      而「当天发言是否够 5 条」是直接数当天消息条数的，不依赖计数器。
    """
    try:
        key = scope_key(chat_id, user_id, is_group)
        data = _load_all()
        p = data.get(key)
        if p is None:
            p = _new_profile(chat_id, user_id, is_group)
            data[key] = p
        p["last_msg_at"] = int(time.time())
        _save_all(data)
    except Exception:
        logger.exception("记录活跃度失败")   # 不能影响主流程


# ══════════════════════════════════════════════════════════
#  清洗与解析
# ══════════════════════════════════════════════════════════

def _clean_value(v: Any, field: str) -> str:
    """清洗 LLM 给出的单个字段值。

    这里的每一条都是踩过的坑：
      - 换行/多余空格 → 注入时会破坏提示词结构
      - 占位符「未提及」→ 不能注入（等于没信息）
      - 疑问句 → LLM 会把用户的提问抄进来
      - 隐私 → 即使 LLM 违反指令也要兜住
    """
    s = str(v or "").strip().strip('"').strip("'")
    s = re.sub(r'\s+', ' ', s)
    s = s.strip('。.,，;；')
    if s.lower() in _PLACEHOLDER:
        return ""
    if _SENSITIVE_RE.search(s):
        return ""
    if _QUESTION_RE.search(s):
        return ""
    limit = _FIELD_MAX.get(field, 70)
    if len(s) > limit:
        s = s[:limit].rstrip()
    return s


def _valid_nick(nick: Any, qq: str, limit: int = 16) -> str:
    """昵称校验：不能是纯 QQ 号 / 代词 / bot 名 / 疑问词

    limit=0 表示不截断。来自配置的分群昵称是权威值，**不该截断**
    （实测 `sleepy_snail_#笨蛋小蜗牛❤️🐸` 被截成 `sleepy_snail_#笨蛋`）；
    只有 LLM 猜的昵称才限长。
    """
    n = str(nick or "").strip().strip('@').strip()
    n = re.sub(r'\s+', ' ', n)
    if not n or n.isdigit():
        return ""
    if n in ("你", "我", "他", "她", "它", "您", "大家", "自己", "未知", "未提及"):
        return ""
    if _QUESTION_RE.search(n):
        return ""
    try:
        from core.config import get_config as _gc
        bn = (getattr(_gc(), "bot_name", "") or "").strip().lower()
        if bn and n.lower() in {bn, bn + "bot", bn.replace("bot", "").strip()}:
            return ""
    except Exception:
        pass
    if limit and len(n) > limit:
        n = n[:limit].rstrip()
    return n


def _parse_fields(raw: str) -> dict[str, str]:
    """从 LLM 输出里取第一个 JSON 对象的 6 个字段（容忍代码块包裹）"""
    if not raw:
        return {}
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        # 常见问题：值里有裸换行 / 尾逗号，退一步做单字段正则捞
        obj = {}
        for f in _FIELDS:
            mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % f, raw)
            if mm:
                obj[f] = mm.group(1)
        if not obj:
            return {}
    return {f: str(obj.get(f, "")) for f in _FIELDS}


# ══════════════════════════════════════════════════════════
#  LLM 提示词
# ══════════════════════════════════════════════════════════

_PROMPT = """你在为 QQ 机器人维护一份用户画像。请根据【旧画像】和【新聊天记录】输出更新后的画像。

输出要求：
1. 只输出一个 JSON 对象，不要解释、不要 markdown 代码块。字段固定为：nick, text
   - nick: 他希望你怎么称呼他；看不出来就填空字符串 ""
   - text: 一段自然语言描述。**不要分点、不要标签、不要换行**，就是一两句通顺的话
2. text 的写法：像跟熟人介绍这个人那样自然地说出来，不要套模板。
   好例子："他常在群里问电脑硬件怎么选，喜欢直接要结论、不爱铺垫，晚上十点后基本不说话。"
   坏例子（不要这样写）："身份：学生 常问：硬件 偏好：精简 注意：别催他"
3. **只写聊天记录里能明确看出的内容，没有依据的不要编** —— 宁可少写也不要凑。
4. 在旧画像的基础上**更新**：保留仍然成立的、补充新发现的、淘汰过时或已被推翻的。

特别注意（这类内容**不算**用户信息，不要写进 text）：
- 玩梗与角色扮演：例如"我是你主人""我就是神""我是废物"这类，是玩笑不是身份
- 反问与疑问：例如"我是谁""我的好感度是多少"，这是他在提问，不是他的信息
- 一次性的闲聊：某天恰好聊到的技术名词、新闻、临时任务，不是长期兴趣
- **他给别人提的建议**：他劝别人"晚上别练"是观点不是他自己的习惯，别写成他的偏好或注意事项
- 隐私：真实姓名、住址、联系方式、账号密码一律不写

判断依据是**反复出现的稳定特征**，而不是某一句话。
确实看不出来就写空字符串 "" —— 别把"未提及"当默认值，那会让 text 变成一堆占位符。

【旧画像】
{old}

【新聊天记录】{date}，他在本会话的昵称是「{nick}」，共 {n} 条
{msgs}

只输出 JSON："""


def _render_old(p: dict) -> str:
    """把旧画像渲染给 LLM 看（空的写「（无）」）"""
    if not p:
        return "（无，这是第一次建档）"
    lines = []
    body = _clean_value(p.get("text"), "text")
    if body:
        # 新格式：正文是一段自然语言，原样给 LLM，别再套标签
        lines.append(body)
    else:
        # 旧记录兼容：五个固定字段带标签拼出来
        for f in ("role", "demands", "preference", "habit", "warning"):
            v = _clean_value(p.get(f), f)
            if v:
                lines.append("%s(%s): %s" % (f, f, v))
    nick_v = _clean_value(p.get("nick"), "nick")
    if nick_v:
        lines.append("称呼(nick): %s" % nick_v)
    meta = []
    if p.get("days"):
        meta.append("已建档 %d 天" % p["days"])
    if p.get("msg_count"):
        meta.append("累计发言 %d 条" % p["msg_count"])
    if meta:
        lines.append("（%s）" % "，".join(meta))
    return "\n".join(lines) if lines else "（无，这是第一次建档）"


# ══════════════════════════════════════════════════════════
#  单次更新
# ══════════════════════════════════════════════════════════

async def update_one(key: str, msgs: list[str], date_str: str,
                     nick: str = "", timeout: float = LLM_TIMEOUT) -> bool:
    """对某个分域做一次增量更新。返回是否成功写入。"""
    if not msgs:
        return False
    info = parse_scope(key)
    data = _load_all()
    old = data.get(key) or {}

    body = _format_msgs(msgs)
    prompt = _PROMPT.format(
        fields=", ".join(_FIELDS), old=_render_old(old), date=date_str,
        nick=nick or "未知", n=len(msgs), msgs=body,
    )

    try:
        from services.llm import call_llm
        from core.config import get_config
        cfg = get_config()
        raw = await call_llm(cfg.cheap_model, [{"role": "user", "content": prompt}],
                             max_tokens=400, temperature=0.2, timeout=timeout)
    except Exception as e:
        logger.warning("画像更新调用失败 key=%s: %s", key, e)
        return False

    fields = _parse_fields(raw or "")
    if not fields:
        logger.warning("画像更新解析失败 key=%s raw=%r", key, (raw or "")[:120])
        return False

    # 昵称优先用配置里的（分群昵称比 LLM 猜的准）；配置拿不到才用 LLM 的
    #   配置来源不截断，LLM 来源限 16 字
    llm_nick = _valid_nick(fields.get("nick"), info["qq"])
    final_nick = _valid_nick(nick, info["qq"], limit=24) or llm_nick

    p = data.get(key) or _new_profile(
        info["group"] or info["qq"], info["qq"], info["scope"] == "group")
    # 字段写回（LLM 说"未提及"时保留旧值，避免好不容易攒的信息被清掉）
    for f in _FIELDS:
        if f == "nick":
            continue
        new_v = _clean_value(fields.get(f), f)
        if new_v:
            p[f] = new_v
    # 已经在写新格式就顺手清掉旧格式的五个字段 —— 留着会让面板详情里新旧并存，
    # 看着像两份画像。只在新格式有内容时才清（避免清空还没迁移成功的记录）
    if p.get("text"):
        for legacy in ("role", "demands", "preference", "habit", "warning"):
            p.pop(legacy, None)
    p["nick"] = final_nick
    p["days"] = int(p.get("days") or 0) + (0 if p.get("updated_at") == _date_ts(date_str) else 1)
    p["updated_at"] = int(time.time())
    p["last_profile_date"] = date_str
    p["msg_count"] = int(p.get("msg_count") or 0) + len(msgs)   # 累计已分析条数
    p["model"] = _model_name(cfg)
    data[key] = p
    _save_all(data)
    logger.info("画像已更新 %s 昵称=%s 天数=%s", key, final_nick or "(无)", p["days"])
    return True


def _date_ts(date_str: str) -> int:
    try:
        return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    except Exception:
        return 0


def _model_name(cfg) -> str:
    """取模型名用于记录。

    ⚠️ cfg.cheap_model 是 ModelConfig **对象**不是字符串，直接塞进 JSON 会
    `TypeError: Object of type ModelConfig is not JSON serializable`。
    """
    m = getattr(cfg, "cheap_model", "")
    name = getattr(m, "name", "") or (m if isinstance(m, str) else "")
    return str(name)[:40]


def _format_msgs(msgs: list[str]) -> str:
    """消息列表 → 提示词正文（控条数与总长）"""
    sel = msgs[-MAX_MSGS:]
    out: list[str] = []
    total = 0
    for line in reversed(sel):       # 从最近往回加，超长就丢最早的
        if total + len(line) > MAX_CHARS and out:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(reversed(out))


# ══════════════════════════════════════════════════════════
#  注入
# ══════════════════════════════════════════════════════════

def _usable(p: Optional[dict]) -> bool:
    """画像是否可用（有内容）。

    ★ 注意：不能只判 text —— 旧记录是五个固定字段（role/demands/…），
    若只判 text 会让旧画像全部变成"不可用"，群里第一次说话就认不出人了。
    """
    if not p:
        return False
    if _clean_value(p.get("text"), "text"):
        return True
    if _clean_value(p.get("nick"), "nick"):
        return True
    return any(_clean_value(p.get(f), f)
               for f in ("role", "demands", "preference", "habit", "warning"))


def build_profile_text(chat_id, user_id, is_group: bool,
                       allow_cross_scope: bool = True) -> str:
    """生成注入用的画像文本。

    优先用**当前会话**的画像（用户要求区分群与私聊）。
    当前会话还没建档时，回退到同一人在其它会话的画像，并标注来源 ——
    这样在群里第一次说话也能被认得，同时不混淆来源。
    """
    data = _load_all()
    key = scope_key(chat_id, user_id, is_group)
    p = data.get(key)
    source = "本群" if is_group else "私聊"

    if not _usable(p) and allow_cross_scope:
        uid = str(user_id)
        cands = [(k, v) for k, v in data.items()
                 if k != key and parse_scope(k)["qq"] == uid and _usable(v)]
        if cands:
            k, v = max(cands, key=lambda kv: kv[1].get("updated_at") or 0)
            p, si = v, parse_scope(k)
            source = "来自私聊" if si["scope"] == "private" else "来自群 %s" % si["group"]

    if not _usable(p):
        return ""

    body = _clean_value(p.get("text"), "text")
    nick = _clean_value(p.get("nick"), "nick")
    if not body:
        # text 没有时回退旧格式的五个字段（老记录没有 text，只有 role/demands/…）。
        # ⚠️ 这里**不能**写成 `if not body and not nick` —— 昵称非空时会把整个回退
        # 跳过，旧画像的 role/demands 就全丢了（实测踩过：注入只剩"称呼：小明"）。
        for f in _LEGACY_FIELDS:
            v = _clean_value(p.get(f), f)
            if v:
                body = (body + "；" if body else "") + "%s: %s" % (_LEGACY_LABELS[f], v)
    if not body and not nick:
        return ""
    head = "【发言者画像 · %s】" % source
    # text 本身是一段自然语言，不要再套"画像："前缀 —— 套了就又变回标签腔
    if body and nick:
        return "%s\n称呼：%s\n%s" % (head, nick, body)
    return "%s\n%s" % (head, body or ("称呼：%s" % nick))


# ══════════════════════════════════════════════════════════
#  每日批量
# ══════════════════════════════════════════════════════════

def collect_day(date_str: str) -> dict[str, dict]:
    """扫某天的 msglog，按分域聚合。

    返回 {scope_key: {"msgs": [...], "nick": str}}，msgs 已按时间排序。
    跳过 bot 自己的消息、已撤回的、以及无正文的。
    """
    gids = group_ids()
    out: dict[str, dict] = {}
    for f in sorted(_MSGLOG_DIR.glob("msglog_*.jsonl")):
        cid = f.name[len("msglog_"):-len(".jsonl")]
        is_group = cid in gids
        per: dict[str, list[tuple[int, str]]] = {}
        try:
            fh = f.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "bot" or d.get("recalled"):
                    continue
                t = d.get("time") or 0
                uid = d.get("user_id")
                if not uid or not t:
                    continue
                if datetime.fromtimestamp(t).strftime("%Y-%m-%d") != date_str:
                    continue
                text = _msg_text(d)
                if text:
                    per.setdefault(str(uid), []).append((int(t), text))
        for uid, arr in per.items():
            arr.sort(key=lambda x: x[0])
            key = ("g%s:%s" % (cid, uid)) if is_group else ("p%s" % uid)
            out[key] = {
                "msgs": ["[%s] %s" % (datetime.fromtimestamp(t).strftime("%H:%M"), s)
                         for t, s in arr],
                "nick": _resolve_nick(uid, cid if is_group else ""),
            }
    return out


def _msg_text(d: dict) -> str:
    """取消息正文（媒体转占位符）"""
    t = str(d.get("type") or "")
    if t in _TEXT_TYPES:
        s = str(d.get("content") or "").strip()
        if not s or s.startswith("[原文未录制]"):
            return ""
        return re.sub(r'\s+', ' ', s)
    return _MEDIA_TPL.get(t, "")


def _resolve_nick(qq: str, group_id: str) -> str:
    try:
        from core.config import get_config
        cfg = get_config()
        n = cfg.get_display_name(int(qq), int(group_id) if group_id else 0)
        n = str(n or "").strip()
        return n if n and not n.isdigit() else ""
    except Exception:
        return ""


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    st["done_dates"] = sorted(set(st.get("done_dates") or []))[-60:]   # 只留最近 60 天
    _STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_daily(target_date: Optional[str] = None, *, force: bool = False,
                    min_msgs: Optional[int] = None, max_users: Optional[int] = None,
                    dry_run: bool = False) -> dict:
    """处理某一天（默认昨天）的画像更新。

    幂等：处理过的日期记在 state 文件里，重启/重跑不会重复调 LLM。
    force=True 时忽略记录强制重跑（回填历史用）。
    """
    if not target_date:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    st = _load_state()
    if not force and target_date in (st.get("done_dates") or []):
        logger.info("画像任务：%s 已处理过，跳过", target_date)
        return {"date": target_date, "skipped": True}

    agg = collect_day(target_date)
    keys = []
    for k, v in agg.items():
        n = len(v["msgs"])
        base = MIN_MSGS if min_msgs is None else min_msgs
        # 已有画像的人门槛低一些：他的画像值得随新发言微调
        exist = bool(_load_all().get(k, {}).get("updated_at"))
        need = MIN_MSGS_EXISTING if exist else base
        if n >= need:
            keys.append(k)
    keys.sort(key=lambda k: -len(agg[k]["msgs"]))
    if max_users:
        keys = keys[:max_users]

    logger.info("画像任务 %s：活跃组合 %d 个，达标 %d 个%s",
                target_date, len(agg), len(keys), "（dry-run）" if dry_run else "")
    if dry_run:
        return {"date": target_date, "candidates": len(keys),
                "detail": [(k, len(agg[k]["msgs"]), agg[k]["nick"]) for k in keys[:20]]}

    ok = 0
    for i, k in enumerate(keys, 1):
        try:
            if await update_one(k, agg[k]["msgs"], target_date, agg[k]["nick"]):
                ok += 1
        except Exception as e:
            logger.warning("画像更新异常 %s: %s", k, e)
        if i % 10 == 0:
            logger.info("画像任务进度 %d/%d", i, len(keys))

    st = _load_state()
    dates = set(st.get("done_dates") or [])
    dates.add(target_date)
    st["done_dates"] = sorted(dates)
    st["last_run"] = int(time.time())
    st["last_result"] = {"date": target_date, "updated": ok, "total": len(keys)}
    _save_state(st)
    logger.info("画像任务 %s 完成：更新 %d/%d", target_date, ok, len(keys))
    return {"date": target_date, "updated": ok, "total": len(keys)}


async def profile_daily_loop():
    """常驻任务：每天 00:05 处理前一天。

    为什么是 00:05：00:00 是日报推送、00:01 是战绩采集，错开避免同一时刻抢资源。
    启动时如果前一天还没处理（例如机器半夜重启过），立即补跑一次。
    """
    import asyncio
    logger.info("画像每日任务已启动（每天 00:05 回看前一天）")

    # 启动补跑：进程可能是在 00:05 之后才起来的
    try:
        await asyncio.sleep(30)                      # 先让启动流程走完
        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if y not in (_load_state().get("done_dates") or []):
            logger.info("画像任务：%s 未处理，启动补跑", y)
            await run_daily(y)
    except Exception:
        logger.exception("画像启动补跑失败")

    while True:
        try:
            now = datetime.now()
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            if now.hour == 0 and now.minute < 5:
                nxt = now.replace(hour=0, minute=5, second=0, microsecond=0)
            await asyncio.sleep(max(30.0, (nxt - now).total_seconds()))
            y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            await run_daily(y)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("画像每日任务异常")
            await asyncio.sleep(300)


# ══════════════════════════════════════════════════════════
#  自检
# ══════════════════════════════════════════════════════════

def _test():
    global _DATA_FILE
    import tempfile
    old = _DATA_FILE
    tmp = Path(tempfile.mktemp(suffix=".json"))
    _DATA_FILE = tmp
    try:
        assert scope_key(1053523927, 3483585417, True) == "g1053523927:3483585417"
        assert scope_key(0, 3483585417, False) == "p3483585417"
        assert parse_scope("g1:2") == {"scope": "group", "group": "1", "qq": "2"}

        _save_all({})
        bump_activity(1, 999, True)
        bump_activity(1, 999, True)
        bump_activity(0, 999, False)
        d = _load_all()
        assert d["g1:999"]["msg_count"] == 2
        assert d["p999"]["msg_count"] == 1

        ok = _parse_fields('{"nick":"小明","role":"学生","demands":"写代码",'
                           '"preference":"精简","habit":"直接","warning":"未提及"}')
        assert ok["role"] == "学生" and ok["warning"] == "未提及"
        assert _clean_value("未提及", "role") == ""
        assert _clean_value("我是谁", "role") == ""
        assert _clean_value("他手机号是 138xxxx", "warning") == ""
        assert _valid_nick("3483585417", "3483585417") == ""
        assert _valid_nick("幻梦", "1") == ""
        assert _valid_nick("Trusler", "1") == "Trusler"
        print("自检通过")
        return True
    finally:
        _DATA_FILE = old
        if tmp.exists():
            tmp.unlink()


if __name__ == "__main__":
    _test()
