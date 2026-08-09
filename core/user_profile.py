"""
按人用户画像系统
- JSON 文件存储: data/user_profiles.json
- 每次发言提取信息（增量更新）
- 生成回复时注入用户画像到上下文
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger("profile")

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "user_profiles.json"

# 默认新用户画像
_DEFAULT_PROFILE: dict[str, Any] = {
    "name": "",
    "tags": [],          # 身份/特征标签: ["学生", "程序员", "夜猫子"]
    "interests": [],     # 兴趣: ["游戏", "编程", "音乐"]
    "dislikes": [],      # 雷点
    "tone": "",          # 偏好语气: 幽默/温柔/直接
    "events": [],        # 重要事件: [{"date":"2026-10-24","text":"生日"}]
    "status": "",        # 当前状态: "备考中" / "刚买了新电脑"
    "facts": [],         # 事实: ["女朋友叫小红", "喜欢熬夜"]
    "last_seen": 0,      # 最后活跃时间戳
    "message_count": 0,  # 累计发言数
}

# 敏感信息过滤正则
_SENSITIVE_RE = re.compile(
    r'(密码|password|token|secret|api.?key|手机号|身份证|银行卡)',
    re.IGNORECASE,
)


def _load_all() -> dict[str, dict]:
    """加载全部用户画像"""
    if not _DATA_FILE.exists():
        return {}
    try:
        return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("画像文件损坏，重置")
        return {}


def _save_all(data: dict[str, dict]):
    """保存全部用户画像"""
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_profile(user_id: int) -> dict[str, Any]:
    """获取用户画像，不存在返回默认"""
    uid = str(user_id)
    data = _load_all()
    if uid not in data:
        data[uid] = dict(_DEFAULT_PROFILE)
    return data[uid]


def update_profile(user_id: int, updates: dict[str, Any]):
    """增量更新用户画像"""
    uid = str(user_id)
    data = _load_all()
    if uid not in data:
        data[uid] = dict(_DEFAULT_PROFILE)
    p = data[uid]

    # 列表字段合并去重
    for key in ("tags", "interests", "dislikes", "facts"):
        if key in updates and isinstance(updates[key], list):
            existing = set(p.get(key, []))
            existing.update(updates[key])
            p[key] = sorted(existing)

    # 标量字段覆盖（非空才写）
    for key in ("name", "tone", "status"):
        if key in updates and updates[key] and isinstance(updates[key], str):
            p[key] = updates[key]

    # events 追加
    if "events" in updates and isinstance(updates["events"], list):
        p.setdefault("events", []).extend(updates["events"])

    p["last_seen"] = int(time.time())
    p["message_count"] = p.get("message_count", 0) + updates.get("message_count", 0)

    _save_all(data)


def _is_sensitive(msg: str) -> bool:
    """检查是否包含敏感信息，避免存到画像"""
    return bool(_SENSITIVE_RE.search(msg))


async def extract_from_message(
    user_id: int, sender_name: str, msg: str,
) -> dict[str, Any] | None:
    """
    用 cheap LLM 从发言提取用户信息。
    返回增量更新 dict，无收获返回 None。
    """
    if not msg or len(msg) < 8 or _is_sensitive(msg):
        return None

    # 简单关键词提取先（零成本），兜底再调 LLM
    quick = _quick_extract(msg)
    if quick:
        return quick

    # LLM 提取：只对≥20字的长消息调用，避免浪费
    if len(msg) < 20 or not re.search(r'[\u4e00-\u9fa5a-zA-Z]{4}', msg):
        return None

    try:
        from services.llm import call_llm
        from core.config import get_config
        cfg = get_config()
        prompt = (
            f"从发言中提取用户信息，返回JSON。键: name,tags,interests,tone,status,facts\n"
            f"只提取明确提及的信息，不要臆测。无信息返回{{}}\n"
            f"发言: {msg[:200]}"
        )
        result = await call_llm(cfg.cheap_model, [{"role": "user", "content": prompt}],
                                max_tokens=150, temperature=0.1, timeout=8.0)
        if result:
            return _parse_llm_result(result)
    except Exception:
        pass
    return None


def _quick_extract(msg: str) -> dict[str, Any] | None:
    """零成本关键词快速提取，有收获直接返回，不做 LLM"""
    result: dict[str, Any] = {}

    # 姓名提取（严格：只取 2-3 字中文名，前后有分隔）
    _NOISE_NAMES = {"谁", "什么", "哪个", "哪里", "怎么样", "为啥", "你", "我", "他", "它", "你们", "我们", "他们",
                     "这个", "那个", "一个", "两个", "真的", "假的", "可以", "没问题", "不知道",
                     "采购", "销售", "学生", "同学", "你好", "好的", "嗯", "哦", "啊", "哈", "是", "不是",
                     "习惯了", "你同学", "最强小学生", "fv", "神", "god", "admin", "root"}
    m = re.search(r'(?:^|[，。！？\s])我(?:叫|是)([\u4e00-\u9fa5]{2,3})(?:[，。！？\s]|$)', msg)
    if not m:
        m = re.search(r'(?:^|[，。！？\s])I\'?m\s+([a-zA-Z]{2,8})(?:[，。！？\s]|$)', msg, re.IGNORECASE)
    if m:
        name = m.group(1)
        if name not in _NOISE_NAMES:
            result["name"] = name

    # 身份标签
    identities = {
        "大学生": r'(?:大学|大一|大二|大三|大四|本科)',
        "高中生": r'(?:高中|高二|高三|高考)',
        "初中生": r'(?:初中|初三|中考)',
        "中职生": r'(?:中职|职校|技校)',
        "程序员": r'(?:程序员|码农|写代码|编程|开发)',
        "上班族": r'(?:上班|工作|公司|老板)',
        "夜猫子": r'(?:熬夜|通宵|凌晨|睡不着)',
        "学生": r'(?:作业|考试|开学|老师|成绩|复习|备考)',
    }
    for tag, pat in identities.items():
        if re.search(pat, msg):
            result.setdefault("tags", []).append(tag)

    # 兴趣
    interests_map = {
        "游戏": r'(?:打游戏|游戏|王者|原神|LOL|吃鸡|Minecraft|我的世界|MC|音游|ADOFAI)',
        "编程": r'(?:编程|写代码|Python|Java|C\+\+|前端|后端|bug)',
        "音乐": r'(?:音乐|听歌|唱歌|网易云|QQ音乐|钢琴|吉他)',
        "动漫": r'(?:动漫|番|二次元|cos)',
        "科技": r'(?:科技|数码|手机|电脑|硬件|显卡)',
        "运动": r'(?:跑步|健身|打球|篮球|足球)',
    }
    for interest, pat in interests_map.items():
        if re.search(pat, msg):
            result.setdefault("interests", []).append(interest)

    # 状态
    status_map = {
        "备考中": r'(?:备考|考试|复习|冲刺)',
        "减肥中": r'(?:减肥|节食|健身|跑步)',
        "找工作中": r'(?:找工作|面试|招聘|简历)',
        "摸鱼中": r'(?:摸鱼|划水|无聊|不想上班)',
        "生气中": r'(?:气死|烦|火大|想骂人)',
        "开心": r'(?:开心|高兴|哈哈|笑死|乐)',
        "难过": r'(?:难过|伤心|哭|emo|抑郁)',
    }
    for status, pat in status_map.items():
        if re.search(pat, msg):
            result["status"] = status
            break

    # 事实
    facts = []
    m = re.search(r'(?:我|我女朋友|我男朋友|我对象|我家|我养).{0,6}(?:叫|是)([\u4e00-\u9fa5a-zA-Z]{1,6})', msg)
    if m:
        facts.append(m.group(0)[:15])

    # 偏好语气（要求明确的交流偏好表达）
    tone_map = {
        "幽默": r'(?:搞笑|幽默|笑话|梗|整活)',
        "温柔": r'(?:温柔点|摸摸我|抱抱我|安慰一下)',
        "直接": r'(?:说重点|别废话|直接点|一句话说清楚|简洁点)',
    }
    for tone, pat in tone_map.items():
        if re.search(pat, msg):
            result["tone"] = tone
            break

    if facts:
        result["facts"] = facts

    # 雷点/厌恶
    dislikes = []
    dislike_map = {
        "政治": r'政治',
        "恐怖": r'恐怖|吓人|鬼故事',
        "剧透": r'剧透|剧透',
        "脏话": r'脏话|骂人',
    }
    for dislike, pat in dislike_map.items():
        if re.search(pat, msg):
            dislikes.append(dislike)
    if dislikes:
        result["dislikes"] = dislikes

    return result if len(result) > 0 else None


def _parse_llm_result(raw: str) -> dict[str, Any] | None:
    """解析 LLM 返回的 JSON"""
    raw = raw.strip()
    # 去掉可能的 markdown 代码块
    m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        result = {}
        for k in ("name", "tone", "status"):
            if k in data and data[k]:
                result[k] = data[k]
        for k in ("tags", "interests", "facts"):
            if k in data and isinstance(data[k], list):
                result[k] = [x for x in data[k] if x]
        return result if result else None
    except json.JSONDecodeError:
        return None


def build_profile_text(user_id: int) -> str:
    """生成画像文本，用于注入 LLM 上下文"""
    p = get_profile(user_id)
    parts = []

    if p.get("name"):
        parts.append(f"昵称: {p['name']}")
    if p.get("tags"):
        parts.append(f"标签: {', '.join(p['tags'])}")
    if p.get("interests"):
        parts.append(f"兴趣: {', '.join(p['interests'][:5])}")
    if p.get("tone"):
        parts.append(f"喜欢语气: {p['tone']}")
    if p.get("status"):
        parts.append(f"当前状态: {p['status']}")
    if p.get("facts"):
        parts.append(f"已知: {'; '.join(p['facts'][:3])}")
    if p.get("events"):
        parts.append(f"事件: {'; '.join(e['text'] for e in p['events'][-3:])}")
    if p.get("dislikes"):
        parts.append(f"避开: {', '.join(p['dislikes'][:3])}")

    return "\n".join(parts) if parts else ""


# ════════════════════════════════════════════════════════════
# 测试接口
# ════════════════════════════════════════════════════════════

def _test_profile_apis():
    """内部测试: 存储/读取/更新/注入"""
    import tempfile, os
    global _DATA_FILE
    old_path = _DATA_FILE
    tmp = Path(tempfile.mktemp(suffix=".json"))
    _DATA_FILE = tmp  # type: ignore

    try:
        # 1. 新用户返回默认
        p = get_profile(12345)
        assert p["name"] == "", f"默认 name 应为空: {p['name']}"
        assert p["tags"] == [], f"默认 tags 应为空: {p['tags']}"

        # 2. 更新画像
        update_profile(12345, {"name": "小明", "tags": ["学生"], "interests": ["游戏"]})
        p2 = get_profile(12345)
        assert p2["name"] == "小明"
        assert "学生" in p2["tags"]
        assert "游戏" in p2["interests"]

        # 3. 增量更新（不覆盖已有）
        update_profile(12345, {"tags": ["夜猫子"], "tone": "幽默"})
        p3 = get_profile(12345)
        assert "学生" in p3["tags"], "增量应保留旧tag"
        assert "夜猫子" in p3["tags"], "增量应添加新tag"
        assert p3["tone"] == "幽默"

        # 4. 画像文本生成
        txt = build_profile_text(12345)
        assert "小明" in txt
        assert "学生" in txt
        assert "游戏" in txt

        # 5. 快速关键词提取
        r = _quick_extract("我是小明，现在在备考，烦死了")
        assert r and r.get("name") == "小明"
        assert "学生" in r.get("tags", [])
        assert r.get("status") == "备考中"

        r2 = _quick_extract("今天写代码写了一天，好累")
        assert "编程" in r2.get("interests", [])

        r3 = _quick_extract("哈哈这个笑话笑死我了")
        assert r3.get("tone") == "幽默"

        # 6. 敏感信息过滤
        r4 = _quick_extract("我的密码是123456")
        assert r4 is None, "密码相关内容不应提取"

        # 7. 空消息
        r5 = _quick_extract("嗯")
        assert r5 is None

        # 8. 多用户隔离
        update_profile(99999, {"name": "小红"})
        p_a = get_profile(12345)
        p_b = get_profile(99999)
        assert p_a["name"] == "小明"
        assert p_b["name"] == "小红"

        # 9. events
        update_profile(12345, {"events": [{"date": "2026-10-24", "text": "生日"}]})
        p_e = get_profile(12345)
        assert len(p_e["events"]) == 1
        assert "生日" in build_profile_text(12345)

        # 10. LLM 结果解析
        llm_out = '{"name":"大黄","tags":["程序员"],"interests":["游戏"]}'
        parsed = _parse_llm_result(llm_out)
        assert parsed and parsed["name"] == "大黄"
        assert "程序员" in parsed["tags"]

        # 11. 坏 JSON
        assert _parse_llm_result("乱七八糟") is None
        assert _parse_llm_result("") is None

    finally:
        _DATA_FILE = old_path  # type: ignore
        if tmp.exists():
            tmp.unlink()

    print("✅ 11/11 测试通过")
    return True


def _test_pipeline_integration():
    """模拟 pipeline 集成: extract → update → inject"""
    from unittest.mock import AsyncMock, patch

    # 模拟 pipeline 调用
    user_id = 11111
    msg = "我是小明，我喜欢打游戏和写代码，最近在备考"

    # Step 1: 提取
    extracted = _quick_extract(msg)
    assert extracted is not None
    update_profile(user_id, extracted)

    # Step 2: 注入
    profile_text = build_profile_text(user_id)
    assert "小明" in profile_text
    assert "游戏" in profile_text
    assert "备考" in profile_text

    # Step 3: 连续多轮
    msg2 = "我不喜欢政治话题"
    extracted2 = _quick_extract(msg2)
    if extracted2:
        update_profile(user_id, extracted2)
    p = get_profile(user_id)
    assert "小明" in p.get("name", "")

    print("✅ Pipeline 集成测试通过")


if __name__ == "__main__":
    _test_profile_apis()
    _test_pipeline_integration()
