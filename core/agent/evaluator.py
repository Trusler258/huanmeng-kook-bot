"""
Phase 7 Result Evaluator（Huanmeng 2.0）

需求：
- 判断工具结果是否成功、是否需要下一步、是否已经足够回答用户；
- 不要每一步都调用 LLM；只有确实需要重新规划时才调用 LLM。

设计：
- `evaluate()` 纯规则：返回 verdict ∈ {ok, fail, done, continue, replan}，
  不产生任何 LLM 调用。
- `decide_replan()`：仅在一步失败且重试耗尽后调用，询问 LLM 是"调整剩余步骤"
  还是"放弃并给出现有结论"。这是全流程里唯一为重新规划触发的 LLM 调用。

verdict 说明：
    ok         —— 步骤成功，可继续下一步
    fail       —— 步骤失败（重试后再 fail → 触发 replan 判定）
    done       —— 已足够回答用户，停止执行
    continue   —— 结果中性，继续下一步
    replan     —— 需要重新规划（由 decide_replan 决定是否真的调整）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger

logger = get_logger("agent.evaluator")

# 结果中视为失败/出错的标记（规则匹配，不调用 LLM）
_FAIL_MARKERS = ("失败", "出错", "超时", "未绑定", "错误", "exception", "traceback",
                 "无结果", "无法", "不存在", "未找到", "不允许", "请先")
# 结果中视为已含足够答案的标记
_DONE_MARKERS = ("完成", "已发送", "已生成", "如下", "结果", "资料", "数据", "总结",
                 "答案", "结论", "信息")


@dataclass
class Evaluation:
    verdict: str          # ok / fail / done / continue / replan
    reason: str = ""
    # 是否真的需要 LLM 重新规划（供 executor 决定是否调 decide_replan）
    wants_replan: bool = False


class ResultEvaluator:
    """轻量结果评估器：规则优先，LLM 仅用于重规划判定。"""

    def evaluate(self, step_result: str, goal: str, step_index: int,
                 total_steps: int, accumulated: str = "") -> Evaluation:
        """规则评估单步结果。不调用 LLM。"""
        text = (step_result or "").strip()
        low = text.lower()

        # 1. 空结果 / None → 视为中性，继续（不判 fail，避免误伤）
        if not text:
            return Evaluation("continue", "空结果，继续下一步")

        # 2. 显式失败标记 → fail
        failed = any(m in low for m in _FAIL_MARKERS)
        if failed:
            # 若同时含"完成/已发送"等成功标记，优先成功（如"文件发送失败"但前面成功）
            if any(m in low for m in _DONE_MARKERS) and not low.startswith("["):
                return Evaluation("ok", "含成功标记，视为成功")
            return Evaluation("fail", f"结果含失败标记: {text[:60]}", wants_replan=True)

        # 3. 是否已足够回答用户
        has_answer = any(m in low for m in _DONE_MARKERS)
        # 最后一步 或 结果已含答案 → done
        if has_answer or step_index >= total_steps - 1:
            return Evaluation("done", "已足够回答（结果含答案或已是最后一步）")

        # 4. 默认继续下一步
        return Evaluation("continue", "结果正常，继续下一步")

    async def decide_replan(self, goal: str, failed_step: str, remaining: list[str],
                            accumulated: str = "") -> bool:
        """失败且重试耗尽后，询问 LLM 是否需要重新规划。

        仅此处会因"重新规划"调用 LLM。返回 True 表示调整剩余步骤，False 表示放弃。
        """
        from core.agent.config import EVALUATOR_LLM_TIMEOUT
        from services.llm import call_llm
        from core.config import get_config
        from core.trace import record_llm

        remaining_text = "\n".join(f"- {s}" for s in remaining) or "（无）"
        prompt = (
            "你是一个任务规划评估器。当前任务目标：{goal}\n"
            "某一步执行失败：{failed_step}\n"
            "剩余计划步骤：\n{remaining_text}\n"
            "已收集的信息：\n{accumulated}\n\n"
            "判断：是否需要调整剩余步骤以继续完成目标？\n"
            "只回答 true（需要调整继续）或 false（信息不足/无法完成，放弃并给出已有结论）。"
        ).format(goal=goal[:200], failed_step=failed_step[:200],
                 remaining_text=remaining_text[:800], accumulated=(accumulated or "")[:800])

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