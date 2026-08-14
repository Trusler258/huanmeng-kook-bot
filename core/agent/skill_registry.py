"""
Phase 7 Skill Registry（Huanmeng 2.0）

需求：
- 只扫描 SKILL metadata/description，不把所有 Skill 正文放进 prompt；
- Planner 根据任务选择候选 Skill，执行前才加载对应 Skill 全文；
- 执行完成后不继续占用上下文；
- 保持现有 Skill 文件格式兼容（skills/*.md，首行 `## 节名`）。

实现：包装既有 core.context_builder 的 discover_skills / load_skill，
并增加基于关键词的候选选择（select）。优点：不重复读盘逻辑，且天然兼容存量
Skill 文件（metadata 级发现 + 按需全文加载已在 Phase 6 Part4 落地）。
"""
from __future__ import annotations

from typing import Optional

from core.context_builder import (
    discover_skills as _discover,
    load_skill as _load_skill,
    reload_skill_discovery as _reload,
)
from core.trace import record_skill


class SkillRegistry:
    """Skill 注册表：metadata 级发现 + 关键词候选选择 + 按需加载全文。"""

    def metadata(self) -> list[dict]:
        """返回全部 Skill 的 metadata（name + description），不加载正文。"""
        return _discover()

    def reload(self) -> None:
        """清除 metadata 缓存（配置热重载时调用）。"""
        _reload()

    def names(self) -> list[str]:
        return [m.get("name", "") for m in self.metadata() if m.get("name")]

    def select(self, query: str, top_k: int = 3) -> list[str]:
        """根据任务描述(query)在 metadata 上做关键词匹配，返回候选 Skill 名。

        只比较 name / description，不加载正文。query 为空返回 []。
        匹配命中即列入候选（按出现次数降序），不足 top_k 时不再补其他。
        """
        if not query:
            return []
        q = str(query).lower()
        # 提取 query 中可用的中文词/英文词片段
        tokens = _tokenize(q)
        scored: list[tuple[int, str]] = []
        for meta in self.metadata():
            name = meta.get("name", "")
            desc = (meta.get("description", "") or "").lower()
            hay = (name + " " + desc).lower()
            score = 0
            for tok in tokens:
                if tok and tok in hay:
                    score += 1
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [name for _, name in scored[:top_k]]

    def load(self, name: str, budget: Optional[int] = None) -> str:
        """按名加载选中 Skill 的完整正文（仅被选中的才读取全文）。

        未命中返回 ""。默认按 DEFAULT_BUDGETS['skill'] 截断。
        """
        if not name:
            return ""
        text = _load_skill(name, budget=budget)
        if text:
            record_skill(name, selected=True, loaded=True)
        return text


# ── 简单分词：中文按单字/双字滑窗，英文按空格/下划线 ──
def _tokenize(text: str) -> list[str]:
    import re
    tokens: list[str] = []
    # 英文 / 数字词
    tokens += re.findall(r"[a-z0-9_]{2,}", text)
    # 中文：2-4 字滑窗（覆盖常见关键词）
    zh = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in zh:
        n = len(seg)
        for i in range(n):
            for L in (2, 3, 4):
                if i + L <= n:
                    tokens.append(seg[i:i + L])
    return tokens


# ── 全局单例 ────────────────────────────────────────────────
_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """获取全局 SkillRegistry 单例（惰性创建）。"""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry