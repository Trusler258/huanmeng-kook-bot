"""
Phase 12 Agent Runtime：Verifier（Huanmeng 2.0）

职责：
- verify_step：判断"某一步"是否成功。
- verify_goal：判断"目标"是否已完成（基于 success_conditions 与已收集信息）。
- decide_replan：判断是否需要对剩余步骤重新规划。

融合 Phase 7 的 ResultEvaluator 规则判定，并补充：
- success_conditions 校验
- goal_satisfied 判定（目标已满足时后续不得再调 Tool）
- 循环检测信号（repeated_action / no_progress）由 Executor + LoopDetector 提供

规则优先，不每步调 LLM；仅重规划判定偶发调用 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger
from utils.format_lang import format_lang

logger = get_logger("agent.verifier")

# 失败/成功标记（规则匹配）——全 Agent 模块唯一来源，勿在其他处重复定义
_FAIL_MARKERS = ("失败", "出错", "超时", "未绑定", "错误", "exception", "traceback",
                 "无结果", "无法", "不存在", "未找到", "不允许", "请先")
_DONE_MARKERS = ("完成", "已发送", "已生成", "如下", "结果", "资料", "数据", "总结",
                 "答案", "结论", "信息")


def has_answer_marker(text: str) -> bool:
    """结果文本是否含"已含答案/完成"标记。"""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _DONE_MARKERS)


@dataclass
class VerifyResult:
    verdict: str          # ok / fail / done / continue / replan
    goal_satisfied: bool = False
    reason: str = ""
    wants_replan: bool = False


class AgentVerifier:
    """步骤/目标验证器：规则优先，LLM 仅用于重规划判定。"""

    def verify_step(self, step_result: str, step_index: int, total_steps: int,
                    has_answer_marker: bool = False) -> VerifyResult:
        """验证单步是否成功。"""
        text = (step_result or "").strip()
        low = text.lower()
        if not text:
            return VerifyResult("continue", False, "空结果，继续")
        if any(m in low for m in _FAIL_MARKERS):
            if has_answer_marker and not low.startswith("["):
                return VerifyResult("ok", False, "含成功标记，视为成功")
            return VerifyResult("fail", False, "结果含失败标记",
                                wants_replan=True)
        # 是否已足够回答（含答案标记 或 已是最后一步）
        if has_answer_marker or step_index >= total_steps - 1:
            return VerifyResult("done", True, "已足够回答")
        return VerifyResult("continue", False, "结果正常，继续")

    def verify_goal(self, success_conditions: list[str],
                    accumulated: str) -> VerifyResult:
        """校验目标成功条件是否全部满足。"""
        if not success_conditions:
            return VerifyResult("continue", False, "无显式成功条件")
        text = (accumulated or "").lower()
        satisfied = all((c or "").lower() in text for c in success_conditions)
        if satisfied:
            return VerifyResult("done", True, "所有成功条件已满足")
        return VerifyResult("continue", False, "成功条件未全部满足")

    async def decide_replan(self, goal: str, failed_step: str, remaining: list[str],
                            accumulated: str = "") -> bool:
        """失败且重试耗尽后，询问 LLM 是否需要重新规划。"""
        from core.agent.config import EVALUATOR_LLM_TIMEOUT
        from services.llm import call_llm
        from core.config import get_config
        from core.trace import record_llm

        remaining_text = "\n".join(f"- {s}" for s in remaining) or "（无）"
        prompt = format_lang(
            "llm.agent.replan_judge",
            goal=goal[:200],
            failed_step=failed_step[:200],
            remaining_text=remaining_text[:800],
            accumulated=(accumulated or "")[:800],
        )
        try:
            raw = await call_llm(
                get_config().reply_model,
                [{"role": "user", "content": prompt}],
                max_tokens=10, temperature=0.0,
                timeout=EVALUATOR_LLM_TIMEOUT,
            )
            record_llm()
            if raw and "true" in raw.strip().lower():
                return True
        except Exception as e:
            logger.warning("重规划判定 LLM 失败，默认放弃重规划: %s", e)
        return False