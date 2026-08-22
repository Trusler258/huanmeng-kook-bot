"""
Phase 7 Result Evaluator（兼容层）

ResultEvaluator 现为 AgentVerifier 的兼容子类：规则判定（_FAIL_MARKERS/_DONE_MARKERS）
与重规划判定（decide_replan）统一收敛到 Phase 12 的 AgentVerifier，消除三处重复。
仅保留 evaluate()/Evaluation 作为 Phase 7 历史接口，供测试与旧调用方使用。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.agent.verifier import AgentVerifier, has_answer_marker


@dataclass
class Evaluation:
    verdict: str          # ok / fail / done / continue / replan
    reason: str = ""
    # 是否真的需要 LLM 重新规划（供 executor 决定是否调 decide_replan）
    wants_replan: bool = False


class ResultEvaluator(AgentVerifier):
    """轻量结果评估器：规则优先，LLM 仅用于重规划判定（逻辑委托 AgentVerifier）。"""

    def evaluate(self, step_result: str, goal: str, step_index: int,
                 total_steps: int, accumulated: str = "") -> Evaluation:
        """规则评估单步结果（委托 verify_step）。不调用 LLM。"""
        vr = self.verify_step(
            step_result, step_index, total_steps,
            has_answer_marker=has_answer_marker(step_result),
        )
        wants_replan = vr.wants_replan or vr.verdict == "replan"
        return Evaluation(vr.verdict, vr.reason, wants_replan)
