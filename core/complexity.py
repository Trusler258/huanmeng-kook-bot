"""
Phase 20 P0：任务复杂度评估（纯规则，O(1)，不调用 LLM）

把「复杂度判断」从「意图分类」彻底拆开。意图（chat/search/tool/command）描述
"用户在干嘛"，复杂度描述"这个问题该被回答得多深、要不要进 Agent、上下文预算多大"。

复杂度等级：
    chat       —— 真正的闲聊/社交/确认（1~3 句短回）
    knowledge  —— 知识/解释/教程/历史/原理/对比分析类（需展开、可进 Agent、可搜索）
    task       —— 明确的执行型任务（写代码/部署/整理/分析项目等，需进 Agent）

输出：
    level            复杂度等级
    score            0~100 分数（供排序/调试）
    output_max_tokens LLM 输出 token 预算（回答长度策略）
    context_scale    上下文预算放大系数（Context 不足时自动扩容）
    detail_hint      注入到 prompt 的"展开/详细"提醒文本

设计约束：
- 绝不调用 LLM；所有判断为正则 + 关键词集合。
- 判断失败/未知 → 返回 chat（保守，不丢消息）。
- 与 core/agent/planner.py 的 should_plan 解耦：这里只给"复杂度"，是否真正进 Agent
  由 planner 结合消息结构决定，但 chat 等级永不进 Agent。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── 复杂度等级 ─────────────────────────────────────────────
LEVEL_CHAT = "chat"
LEVEL_KNOWLEDGE = "knowledge"
LEVEL_TASK = "task"

# 知识/解释类触发词（覆盖 历史/原理/教程/对比/分析/整理/介绍/讲解/部署…）
_KNOWLEDGE_WORDS: tuple[str, ...] = (
    "历史", "由来", "起源", "发展", "演变", "变迁", "沿革",
    "原理", "机制", "原理详解", "底层", "实现原理", "构造",
    "教程", "指南", "手册", "入门", "入门教程", "学习", "教学", "课程",
    "区别", "差异", "对比", "比较", "对照", "优缺点", "优劣", "哪个好",
    "分析", "剖析", "解析", "拆解", "探讨", "研究", "论述",
    "介绍", "讲解", "解释", "说明", "科普", "简述", "阐述", "概述",
    "是什么", "是什么", "什么意思", "含义", "定义", "概念", "用途", "作用",
    "为什么", "为何", "如何", "怎么", "怎样", "哪些", "几种", "有哪些",
    "部署", "配置", "架构", "设计", "方案", "流程", "步骤", "组成部分",
    "总结", "整理", "汇总", "归纳", "梳理", "提纲",
    "使用", "用法", "用法教程", "怎么用", "怎么实现", "怎么做",
)
# 长尾：单字/双字短词，避免误伤闲聊（如"怎么"单独出现较多，但"怎么啦/干嘛"已在闲聊集）
_KNOWLEDGE_STRONG: tuple[str, ...] = (
    "历史", "原理", "教程", "区别", "对比", "分析", "剖析", "解析",
    "介绍", "讲解", "说明", "是什么", "为什么", "如何", "部署", "架构",
    "总结", "整理", "汇总", "归纳", "梳理", "优缺点", "组成部分", "流程",
)

# 明确"要详细展开"的强触发词
_DETAIL_STRONG: tuple[str, ...] = (
    "详细", "具体", "完整", "展开", "说透", "全部", "一次说完", "尽可能详细",
    "讲清楚", "讲完整", "面面俱到", "详细讲解", "详细解释", "详细分析",
)

# 明确是执行型任务的动词（复用 planner 的任务动词，但这里独立维护避免循环依赖）
_TASK_VERBS: tuple[str, ...] = (
    "写", "做", "查", "找", "搜", "分析", "整理", "总结", "对比",
    "研究", "调查", "计算", "算", "部署", "配置", "安装", "生成", "创建",
    "设计", "修改", "优化", "修复", "讲解", "解释", "介绍", "生成一", "编一",
    "搭建", "开发", "实现", "翻译", "转换", "统计", "汇总", "规划", "计划",
    "列出", "出一", "出一份", "编写", "推导", "求解", "评估", "检查", "审查",
    "帮我", "帮我查", "帮我找", "帮我做", "帮我写", "帮我分析",
    "查一下", "找一下", "执行一下", "分析一下", "整理一下", "总结一下",
)

# 纯闲聊：命中即 chat，绝不升级（与 planner._CASUAL_TOKENS 对齐）
_CASUAL_WORDS: tuple[str, ...] = (
    "你好", "您好", "哈喽", "嗨", "hello", "hi", "在吗", "在不在",
    "谢谢", "感谢", "多谢", "辛苦了", "拜托", "好的", "好滴", "好嘞",
    "嗯", "哦", "哈哈", "呵呵", "嘿嘿", "嘻嘻", "666", "6", "牛", "厉害",
    "可以", "没问题", "行", "再见", "拜拜", "晚安", "早", "怎么啦", "干嘛",
)


@dataclass
class Complexity:
    level: str = LEVEL_CHAT
    score: int = 0
    output_max_tokens: int = 0          # 0 = 用默认/不设上限
    context_scale: float = 1.0
    detail_hint: str = ""

    # 便捷属性
    @property
    def is_chat(self) -> bool:
        return self.level == LEVEL_CHAT

    @property
    def needs_agent(self) -> bool:
        """是否建议进 Agent：task 必进；complex knowledge 也进（可搜索/多步展开）。"""
        return self.level in (LEVEL_TASK, LEVEL_KNOWLEDGE)

    @property
    def needs_search(self) -> bool:
        """知识类问题多数需要联网（历史/原理/教程/对比），若上下文中无答案应搜索。"""
        return self.level == LEVEL_KNOWLEDGE


# 输出 token 预算（按等级）
_OUTPUT_TOKENS = {
    LEVEL_CHAT: 0,          # 不设上限，走默认（短回由格式规则约束）
    LEVEL_KNOWLEDGE: 2000,  # 展开回答
    LEVEL_TASK: 3000,       # 任务执行 + 结果
}
# 上下文预算放大系数（Context 不足时自动扩容）
_CONTEXT_SCALE = {
    LEVEL_CHAT: 1.0,
    LEVEL_KNOWLEDGE: 1.6,
    LEVEL_TASK: 1.8,
}
# 详细提醒（注入 prompt）
_DETAIL_HINT = {
    LEVEL_CHAT: "",
    LEVEL_KNOWLEDGE: (
        "\n【本次为知识/讲解类问题，请完整展开回答】"
        "本条不受'回复1~3句、每句≤40字'限制，可突破上限。"
        "不要只回一两句。一定要用清晰的分段/分点/列表组织内容："
        "每个要点单独一行（可用 - 或 1. 2. 3. 列表），句与句之间用换行隔开，"
        "不要堆成一大段长文。把历史/原理/步骤/对比都说清楚，可输出 3~8 个要点，"
        "每个要点根据需要可到 150 字。"
    ),
    LEVEL_TASK: (
        "\n【本次为执行型任务，请按步骤完整执行并汇报结果】"
        "本条不受'回复1~3句'限制，可输出多句。"
        "需要调用工具就调用，不要只口头承诺；完成后把关键结果/文件/数据详细列出。"
    ),
}


def assess_complexity(msg: str) -> Complexity:
    """评估一条消息的复杂度。纯规则，不调用 LLM。失败/未知 → chat。"""
    if not msg:
        return Complexity()
    text = msg.strip()
    if not text:
        return Complexity()
    low = text.lower()

    # 1) 纯闲聊 → 直接 chat（无论多长，避免"哈哈哈哈哈"被误判）
    compact = re.sub(r"[\s，。！？、,.!?~～…\-—_]+", "", low)
    if any(tk in compact for tk in _CASUAL_WORDS if len(tk) >= 2):
        # 仅当整段基本由闲聊词构成时才判 chat，避免"帮我写个历史作业"被吞
        if _is_mostly_casual(compact):
            return Complexity(level=LEVEL_CHAT, score=5)

    score = 0
    detail = 0
    # 2) 详细程度加分
    if any(w in text for w in _DETAIL_STRONG):
        detail = 30
        score += 30

    # 3) 知识词命中
    k_hits = [w for w in _KNOWLEDGE_WORDS if w in text]
    # 剔除闲聊里的"怎么样/怎么啦"，避免"今天天气怎么样"误判为知识
    k_hits = [w for w in k_hits if not (w == "怎么" and ("怎么样" in text or "怎么啦" in text))]
    k_strong = [w for w in _KNOWLEDGE_STRONG if w in text]
    k_strong = [w for w in k_strong if not (w == "怎么" and ("怎么样" in text or "怎么啦" in text))]
    score += len(k_hits) * 8 + len(k_strong) * 6

    # 4) 任务动词命中
    t_hits = [w for w in _TASK_VERBS if w in text]
    score += len(t_hits) * 10

    # 5) 消息长度：越长越可能是复杂任务
    if len(text) >= 8:
        score += 5
    if len(text) >= 30:
        score += 10
    if len(text) >= 80:
        score += 10

    # ── 分级 ──
    # 纯知识动词（讲解/介绍/解释/分析/说明等）本质是"解释/讲解"，优先归 knowledge，
    # 而不是执行型任务。真正的 task 是"写/部署/搭建/实现/生成/帮我做"这类产出物动词。
    # 例："讲解TCP三次握手原理" → knowledge（展开讲解个要点），而非 task。
    _KNOWLEDGE_VERBS: tuple[str, ...] = (
        "讲解", "介绍", "解释", "分析", "说明", "阐述", "概述", "简述",
        "科普", "解析", "解读", "整理", "总结", "梳理", "归纳",
    )
    is_knowledge = bool(k_strong or k_hits)
    # 去掉偏知识的动词后，剩下的是"真正的执行动词"
    real_task = [v for v in t_hits if v not in _KNOWLEDGE_VERBS]

    # 明确执行型任务（写/部署/搭建/实现/生成/帮我X 等产出物动词）→ task
    strong_task = any(w in text for w in ("帮我写", "帮我做", "帮我查", "帮我找",
                                          "帮我分析", "帮我整理", "帮我配置",
                                          "部署", "搭建", "实现", "生成", "编写",
                                          "写一个", "写一个程序", "写代码"))
    if strong_task or (real_task and not is_knowledge):
        # 仅裸"帮我"（如"帮我一下"）不判 task，避免闲聊误触发
        if (strong_task or real_task) and not (len(real_task) == 1 and real_task[0] == "帮我"):
            return Complexity(
                level=LEVEL_TASK, score=score,
                output_max_tokens=_OUTPUT_TOKENS[LEVEL_TASK],
                context_scale=_CONTEXT_SCALE[LEVEL_TASK],
                detail_hint=_DETAIL_HINT[LEVEL_TASK],
            )

    # 知识类（含历史/原理/教程/区别/讲解/介绍/是什么/为什么/如何 等）→ knowledge
    if is_knowledge:
        return Complexity(
            level=LEVEL_KNOWLEDGE, score=score,
            output_max_tokens=_OUTPUT_TOKENS[LEVEL_KNOWLEDGE],
            context_scale=_CONTEXT_SCALE[LEVEL_KNOWLEDGE],
            detail_hint=_DETAIL_HINT[LEVEL_KNOWLEDGE],
        )

    # 还有其他真实执行动词 → task
    if real_task and not (len(real_task) == 1 and real_task[0] == "帮我"):
        return Complexity(
            level=LEVEL_TASK, score=score,
            output_max_tokens=_OUTPUT_TOKENS[LEVEL_TASK],
            context_scale=_CONTEXT_SCALE[LEVEL_TASK],
            detail_hint=_DETAIL_HINT[LEVEL_TASK],
        )

    # 默认 chat
    return Complexity(level=LEVEL_CHAT, score=score)


def _is_mostly_casual(compact: str) -> bool:
    """判断去除标点后的文本是否基本由闲聊词构成（避免"帮我写历史"被吞）。"""
    if not compact:
        return True
    rest = compact
    matched_any = False
    while rest:
        hit = None
        for tk in sorted(_CASUAL_WORDS, key=len, reverse=True):
            if rest.startswith(tk):
                hit = tk
                break
        if hit is None:
            break
        matched_any = True
        rest = rest[len(hit):]
    return matched_any and not rest