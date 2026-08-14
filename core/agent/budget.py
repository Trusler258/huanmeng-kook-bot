"""
Phase 12 Agent Runtime：Agent Budget 与循环检测（Huanmeng 2.0）

Agent 必须拥有：
- max_steps：最大执行步数
- deadline：总截止时间（绝对时间戳）
- token_budget：本次任务 LLM token 预算
- tool_budget：本次任务最多工具调用次数
- search_budget：本次任务最多搜索次数

并检测：
- repeated_action：连续重复调用同一工具（相同 tool+params）→ 强制停止
- no_progress：多步执行后状态无实质变化 → 停止
- goal_satisfied：目标已满足 → 禁止再调用 Tool

纯数据 / 无副作用，可独立测试。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

logger = get_logger("agent.budget")


@dataclass
class AgentBudget:
    """本次任务的资源预算与执行边界。"""
    max_steps: int = 5
    deadline_ms: float = 0.0          # 绝对截止时间（monotonic 毫秒），0=不限制
    token_budget: int = 8000          # LLM token 预算（约）
    tool_budget: int = 20             # 最多工具调用次数
    search_budget: int = 5            # 最多搜索调用次数

    # 运行期计数
    tool_calls: int = 0
    search_calls: int = 0
    llm_tokens: int = 0

    def hit_deadline(self) -> bool:
        if self.deadline_ms <= 0:
            return False
        return time.monotonic() * 1000.0 >= self.deadline_ms

    def remaining_seconds(self) -> float:
        if self.deadline_ms <= 0:
            return 99999.0
        return max(0.0, (self.deadline_ms - time.monotonic() * 1000.0) / 1000.0)

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.tool_budget and not self.hit_deadline()

    def can_search(self) -> bool:
        return self.search_calls < self.search_budget and not self.hit_deadline()

    def use_tool(self) -> None:
        self.tool_calls += 1

    def use_search(self) -> None:
        self.search_calls += 1

    def add_tokens(self, n: int) -> None:
        self.llm_tokens += max(0, int(n))


class LoopDetector:
    """检测 Agent 是否陷入循环 / 无进展 / 目标已满足。"""

    def __init__(self, max_repeat: int = 3, max_fruitless: int = 3) -> None:
        self._max_repeat = max_repeat
        self._max_fruitless = max_fruitless
        self._recent: list[str] = []          # 最近工具调用指纹
        self._fruitless = 0                    # 连续无进展计数

    def _fingerprint(self, tool: str, params: dict) -> str:
        try:
            import json
            return f"{tool}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"
        except Exception:
            return str(tool)

    def on_tool_call(self, tool: str, params: dict) -> bool:
        """记录一次工具调用；返回 True 表示出现重复动作应停止。"""
        fp = self._fingerprint(tool, params)
        self._recent.append(fp)
        if len(self._recent) > self._max_repeat:
            self._recent.pop(0)
        if len(self._recent) >= self._max_repeat and len(set(self._recent)) == 1:
            logger.warning("Agent 检测到 repeated_action: %s x%d", tool, self._max_repeat)
            return True
        return False

    def on_progress(self, progressed: bool) -> bool:
        """记录一次进展；返回 True 表示连续无进展应停止。"""
        self._fruitless = 0 if progressed else self._fruitless + 1
        if self._fruitless >= self._max_fruitless:
            logger.warning("Agent 检测到 no_progress x%d", self._max_fruitless)
            return True
        return False

    def goal_satisfied(self, verifier_result: str) -> bool:
        """依据 Verifier 判定目标是否已满足。"""
        return verifier_result in ("done", "goal_satisfied")

    def reset(self) -> None:
        self._recent.clear()
        self._fruitless = 0