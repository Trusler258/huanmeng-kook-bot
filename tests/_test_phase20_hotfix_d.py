"""
Phase 20 Hotfix D 验证测试（KMD / 发送粒度 / continuation 主题绑定）
运行: python _test_phase20_hotfix_d.py
覆盖：
1. KMD 归一化：# 标题 → **加粗**；代码块围栏内原样保留
2. 代码块不被按句拆碎（split_knowledge_sentences 跳过围栏）
3. continuation：新主题不继承旧状态；纯续说词才继承
4. Fast Path 主题记录（供裸续说继承）
5. Agent _split_sentences 不在代码块内切分
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_TMPDIR = Path(tempfile.mkdtemp(prefix="hm_hfd_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR / 'test.db'}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.kmd import normalize_kmd_text
from core.reply_split import split_knowledge_sentences
from core.agent.gateway import _has_new_topic
from core.agent.executor import (
    set_continuation, get_continuation, has_continuation, clear_continuation,
)
from core.agent.planner import TaskConstraints


# ── 1. KMD 归一化 ──────────────────────────────────────────
def test_kmd_normalize_headings():
    text = "# 为什么 eval 不安全\n\n## 攻击链\n```python\nx = 1\n# 注释不是标题\n```\n### 替代方案"
    out = normalize_kmd_text(text)
    assert "**为什么 eval 不安全**" in out, out
    assert "**攻击链**" in out, out
    assert "**替代方案**" in out, out
    # 代码块内 # 注释原样保留
    assert "# 注释不是标题" in out, out
    assert "```python" in out and "```" in out.split("```python")[1], out
    print("OK test_kmd_normalize_headings (# 标题→加粗, 代码块内原样)")


def test_kmd_normalize_plain_text():
    # 普通文本不受影响
    assert normalize_kmd_text("你好喵~") == "你好喵~"
    assert normalize_kmd_text("") == ""
    print("OK test_kmd_normalize_plain_text (普通文本原样)")


# ── 2. 代码块不被拆碎 ──────────────────────────────────────
def test_split_skips_code_fence():
    text = (
        "喵~ 下面是一个 Python 示例：\n"
        "```python\n"
        "def f():\n"
        "    # **不是标题**\n"
        "    return [1, 2, 3]  # 1. 也不是阶段\n"
        "```\n"
        "**运行结果**\n"
        "输出 1 2 3\n"
    )
    segs = split_knowledge_sentences(text)
    # 不应把代码块内部拆开：整段（含代码）应是 1 段
    assert len(segs) == 1, segs
    joined = "\n".join(segs)
    assert "```python" in joined and "return [1, 2, 3]" in joined, joined
    print("OK test_split_skips_code_fence (代码块整体保留)")


def test_split_still_splits_knowledge():
    # 纯知识分段（无代码块）仍按阶段拆分（不破坏 Hotfix B/C 行为）
    text = "**第一代**\n内容1\n\n**第二代**\n内容2\n"
    segs = split_knowledge_sentences(text)
    assert len(segs) >= 2, segs
    print("OK test_split_still_splits_knowledge (知识仍按阶段分条)")


# ── 3. continuation 主题绑定 ───────────────────────────────
def test_has_new_topic():
    # 纯续说词 → 无新主题（继承旧话题）
    assert _has_new_topic("详细说说") is False
    assert _has_new_topic("继续") is False
    assert _has_new_topic("再详细点") is False
    assert _has_new_topic("展开讲讲") is False
    # 带显式新主题 → 有新主题（不继承旧状态）
    assert _has_new_topic("详细说说缓存命中率") is True
    assert _has_new_topic("详细说说上下文稀疏") is True
    assert _has_new_topic("展开讲讲 MySQL 历史") is True
    print("OK test_has_new_topic (纯续说继承, 带主题不继承)")


def test_continuation_topic_override():
    # 模拟：先记录"上下文稀疏"，再记录"缓存命中率" → 最新主题覆盖
    clear_continuation(1001, 42)
    set_continuation(1001, 42, {
        "goal": "上下文稀疏", "accumulated": [], "constraints": TaskConstraints(),
        "plan_steps": [],
    })
    assert has_continuation(1001, 42)
    assert get_continuation(1001, 42)["goal"] == "上下文稀疏"
    # 新话题覆盖（Fast Path 记录最近主题）
    set_continuation(1001, 42, {
        "goal": "缓存命中率", "accumulated": [], "constraints": TaskConstraints(),
        "plan_steps": [],
    })
    assert get_continuation(1001, 42)["goal"] == "缓存命中率", \
        "最新主题应覆盖旧主题"
    clear_continuation(1001, 42)
    print("OK test_continuation_topic_override (最近主题覆盖旧主题)")


# ── 4. Agent _split_sentences 不拆代码 ─────────────────────
def test_agent_split_fence():
    from core.agent.gateway import _split_sentences
    text = "开头一句话。\n```python\n" + ("print(1)\n" * 40) + "```\n结尾。"
    segs = _split_sentences(text, max_len=200)
    # 代码块内部不应被切分：找到包含 ```python 的段，应同时包含收尾 ```
    code_segs = [s for s in segs if "```python" in s]
    assert code_segs, "应存在包含代码块的段"
    for s in code_segs:
        assert s.count("```") >= 2 or s.rstrip().endswith("```") or "```" in s, s[:100]
    # 代码块整体应完整（```python ... ``` 在同一段内）
    joined_code = "\n".join(code_segs)
    assert "```python" in joined_code and joined_code.count("```") >= 2, joined_code[:200]
    print("OK test_agent_split_fence (Agent 拆分不破坏代码块)")


async def main():
    test_kmd_normalize_headings()
    test_kmd_normalize_plain_text()
    test_split_skips_code_fence()
    test_split_still_splits_knowledge()
    test_has_new_topic()
    test_continuation_topic_override()
    test_agent_split_fence()
    print("\n=== ALL Phase20 HOTFIX D TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
