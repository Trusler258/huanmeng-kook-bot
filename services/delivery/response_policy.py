"""
响应策略（Huanmeng 2.0 Phase 5）

职责：
- 管理句子分段发送的人为延迟（0.5~1.5s 随机等待不再隐藏在 Sender 内部）。
- 提供 max_sentences / max_total_delay / delay_policy 配置。
- 普通聊天默认不产生明显人为延迟；复杂 Agent 可显式启用 PROGRESS → FINAL。

delivery_policy 值：
  - "random"  在 min_interval~max_interval 间随机，受 max_total_delay 上限约束
  - "none"    不添加任何人为延迟
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ResponsePolicyConfig:
    """ResponsePolicy 配置。"""
    max_sentences: int = 10          # 分段发送的最大句子数
    max_total_delay: float = 3.0     # 整批句间延迟总和上限（秒），避免拖沓
    delay_policy: str = "random"     # "random" | "none"
    min_interval: float = 0.5        # random 模式下的最小句间等待（秒）
    max_interval: float = 1.5        # random 模式下的最大句间等待（秒）


class ResponsePolicy:
    """句间延迟策略。"""

    def __init__(self, cfg: Optional[ResponsePolicyConfig] = None):
        self.cfg = cfg or ResponsePolicyConfig()

    def compute_delays(self, num_sentences: int) -> List[float]:
        """返回每条句子发送前的等待时间列表（首条为 0）。

        单条句子或 delay_policy="none" 时全部为 0（无人为延迟）。
        """
        if num_sentences <= 1 or self.cfg.delay_policy == "none":
            return [0.0] * num_sentences

        delays = [0.0]
        for _ in range(num_sentences - 1):
            delays.append(random.uniform(self.cfg.min_interval, self.cfg.max_interval))

        total = sum(delays)
        if total > self.cfg.max_total_delay and total > 0:
            scale = self.cfg.max_total_delay / total
            delays = [d * scale for d in delays]
        return delays

    def cap_sentences(self, sentences: List[str]) -> List[str]:
        """按 max_sentences 截断句子列表（不足时原样返回）。"""
        if len(sentences) <= self.cfg.max_sentences:
            return sentences
        return sentences[: self.cfg.max_sentences]