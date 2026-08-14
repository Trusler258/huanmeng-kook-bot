"""
Phase 10 Capability System：CapabilityRouter（Huanmeng 2.0)

根据当前请求（用户消息 + 意图）选择相关 Capability（metadata 级关键词匹配）。
原则：
- 模型一开始只看到「当前请求相关」的能力目录，确认需要后再加载完整 Schema/正文。
- 普通聊天（intent=chat/空）不加载绝大多数能力，只保留核心常驻能力。
- 纯函数 / 无副作用，可独立测试。
"""
from __future__ import annotations

import re
from typing import Optional

from core.capability.metadata import (
    Capability,
    CATEGORY_TOOL, CATEGORY_COMMAND, CATEGORY_SKILL,
)
from core.capability.registry import get_capability_registry

# 默认单次最多选中的能力数
DEFAULT_TOP_K = 6

# 简单聊天意图：不路由任何能力（只保留 always_on）
CHAT_INTENTS: frozenset[str] = frozenset({"chat", ""})


def _tokenize(text: str) -> list[str]:
    t = str(text).lower()
    tokens: list[str] = []
    tokens += re.findall(r"[a-z0-9_]{2,}", t)
    zh = re.findall(r"[\u4e00-\u9fff]+", t)
    for seg in zh:
        n = len(seg)
        for i in range(n):
            for L in (2, 3, 4):
                if i + L <= n:
                    tokens.append(seg[i:i + L])
    return tokens


class CapabilityRouter:
    """能力路由器：query + intent → 相关能力列表（metadata 级）。"""

    def route(self, query: str, intent: str = "", top_k: int = DEFAULT_TOP_K,
              is_group: bool = True) -> list[Capability]:
        """返回当前请求相关的能力。简单聊天仅返回核心常驻能力。"""
        if intent in CHAT_INTENTS and not query.strip():
            return self._core()
        if intent in CHAT_INTENTS:
            # 普通聊天：不加载绝大多数能力，仅保留核心常驻
            return self._core()

        registry = get_capability_registry()
        candidates = [c for c in registry.all() if c.category in (CATEGORY_TOOL, CATEGORY_COMMAND, CATEGORY_SKILL)]
        tokens = _tokenize(query)
        scored: list[tuple[int, Capability]] = []
        for cap in candidates:
            hay = (cap.name + " " + cap.description + " " + " ".join(cap.aliases)).lower()
            score = 0
            for tok in tokens:
                if tok and tok in hay:
                    score += 1
            if score > 0:
                scored.append((score, cap))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        # 核心常驻能力始终带上，再补充命中项
        seen: set[str] = set()
        out: list[Capability] = []
        for c in self._core():
            out.append(c)
            seen.add(c.id)
        for _, cap in scored:
            if cap.id in seen:
                continue
            out.append(cap)
            seen.add(cap.id)
            if len(out) >= max(top_k, len(self._core())):
                break
        return out

    def _core(self) -> list[Capability]:
        return get_capability_registry().always_on()


# ── 全局单例 ───────────────────────────────────────────────
_router: Optional[CapabilityRouter] = None


def get_capability_router() -> CapabilityRouter:
    global _router
    if _router is None:
        _router = CapabilityRouter()
    return _router