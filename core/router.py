"""
Phase 6 Part2：轻量 Intent/Request Router（非 AI 分类器）

目标：在进入完整 Agent Pipeline 前，用 O(1) 的正则/关键词把请求粗分为
基础意图，避免普通简单聊天无条件触发 memory/skill/search/tool discovery
或额外 LLM 判断。

意图分类：
    command  —— 以 . 开头的指令（已在 pipeline 指令拦截段处理）
    search   —— 明显需要联网搜索（关键词 / 实时词 / 疑问句式）
    tool     —— 明显需要工具/数据查询（战绩/天气/地震/域名/抽卡等）
    plugin   —— 插件入口（预留）
    system   —— 系统事件 / 管理操作
    chat     —— 普通闲聊天（默认）

设计约束：
- 不做任何 LLM 调用，纯规则。
- 判断失败 / 未知 → 返回 "chat" 并允许调用方 fallback 到完整 pipeline，
  绝不因 router 出错导致消息丢失。
- 所有判断必须快（正则+集合），单次 < 1ms。
"""

from __future__ import annotations

import re
from typing import Optional

# ── 搜索触发特征 ──────────────────────────────────────────
_SEARCH_REALTIME = {"天气", "现在时间", "现在几点", "气温", "当前", "实时",
                    "新闻", "最新", "汇率", "股价", "期货", "比赛", "直播"}
# 明确要求"去搜"的意图词（用户主动要求搜索时触发）
_SEARCH_EXPLICIT = {"搜索", "搜一下", "查一下", "查一查", "帮我查", "给我查",
                    "介绍一下", "搜搜", "百度", "谷歌", "查查", "查资料",
                    "搜到", "搜了", "查资料", "帮查"}
# 命令行/指令查询：问"给X指令/命令/写法/tellraw/怎么写"时，具体语法(尤其带版本号，
# 如 MC 1.21.x 的 /tellraw 彩虹渐变)是外部事实，模型未必可靠，应触发联网搜索后实答，
# 避免机器人"只说不做"——仅口头说"帮主人找找看"却从不真正搜索。
_SEARCH_CMD = {
    "指令", "命令", "tellraw", "command", "gommand",
    "怎么写", "怎么写一个", "怎么给", "怎么配", "怎么设", "怎么用",
    "求一个", "来个指令", "来个命令", "给个指令", "给个命令",
}

# 纯知识问句（是什么/为什么/怎么/定义/解释/原理等）：模型自身知识优先直接回答，
# 不因此触发自动搜索——只有命中 _SEARCH_EXPLICIT 或实时词时才搜。
_SEARCH_QUESTION = re.compile(r'(今年|最近|昨天|今天|明天|上周|上月)\s*(.*?)(新闻|事件|发生|结果|排名|比分|价格|多少|什么)$')

# ── 工具 / 数据查询特征 ───────────────────────────────────
_TOOL_KEYWORDS = {
    # 战绩
    "战绩", "kd", "击杀", "死亡", "胜场", "日报",
    # 天气 / 地震
    "天气", "气温", "下雨", "地震", "震级", "台风",
    # 域名 / 代码 / 计算
    "域名", "注册商", "到期", "写代码", "算一下", "计算", "求解", "方程",
    # 抽卡 / 棋类（对局指令）
    "抽卡", "抽签", "五子棋", "象棋", "对局",
}

# ── 系统 / 管理特征 ───────────────────────────────────────
_SYSTEM_KEYWORDS = {"重启", "更新", "reload", "升级", "重载", "状态", "balance",
                    "余额", "cost", "tokens", "stats", "memory", "recall"}

# ── 插件入口（预留）───────────────────────────────────────
_PLUGIN_PREFIXES = ("/plugin", "/p ", "plugin:")


def classify_request(msg: str, *, is_group: bool = True,
                     is_mentioned: bool = False, role_tag: str = "friend") -> str:
    """返回意图：command / search / tool / plugin / system / chat。

    所有判断为规则，不调用 LLM。失败或未知一律回落 "chat"。
    """
    if not msg:
        return "chat"

    text = msg.strip()
    low = text.lower()

    # 指令已由 pipeline 指令拦截段处理，这里仍标记（用于 trace）
    if text.startswith("."):
        return "command"

    # 插件入口
    if low.startswith(_PLUGIN_PREFIXES):
        return "plugin"

    # 系统 / 管理操作（管理员专属）
    if role_tag == "admin" and any(k in low for k in _SYSTEM_KEYWORDS):
        return "system"

    # 明显搜索意图（用户明确要求搜索，或实时话题，或命令行/指令查询）
    if any(w in low for w in _SEARCH_EXPLICIT):
        return "search"
    if any(w in low for w in _SEARCH_REALTIME):
        return "search"
    if any(w in low for w in _SEARCH_CMD):
        return "search"
    if _SEARCH_QUESTION.search(text):
        return "search"

    # 工具 / 数据查询
    if any(w in low for w in _TOOL_KEYWORDS):
        return "tool"

    # Phase 20 P0：知识/执行复杂度 → 不当作普通 chat
    # 例："mysql历史"、"说说mysql历史"、"讲解TCP三次握手原理" 等
    # 不能一律归为 chat，否则 Fast Path 直接压短回答。
    # Phase 20 Hotfix C：知识类问题**不**因复杂度强制触发 search——
    # 模型自身可答的基础知识/概念/原理优先直接回答；需要外部资料时
    # 由 FC 层的 search_web 工具或用户明确"搜/查/介绍一下"再触发。
    try:
        from core.complexity import assess_complexity
        _complexity = assess_complexity(text)
        if _complexity.level == "task":
            return "tool"          # 执行型任务 → 走复杂处理（可进 Agent）
    except Exception:
        pass

    # 默认普通聊天
    return "chat"


def needs_search_heuristic(msg: str) -> bool:
    """快速判断是否需要搜索（仅规则，不触发模型判断）。

    返回 True 时调用方应执行搜索；返回 False 表示普通聊天，无需搜索。

    Phase 20 Hotfix C：只有明确搜索意图（搜/查/介绍一下）或实时话题
    （新闻/天气/最新…）才返回 True。纯知识问句（是什么/为什么/怎么/定义/
    原理/历史）不再因 complexity=knowledge 强制搜索——模型自身知识可答的
    优先直接回答，避免"缓存命中率定义"这类问题无谓触发 DeepSeek 搜索。
    """
    if not msg:
        return False
    low = msg.lower().strip()
    if any(w in low for w in _SEARCH_EXPLICIT):
        return True
    if any(w in low for w in _SEARCH_REALTIME):
        return True
    # 命令行/指令查询（给X指令/命令/tellraw/怎么写/怎么配）——需要联网获取具体语法，
    # 不能只口头说"帮主人找找看"。
    if any(w in low for w in _SEARCH_CMD):
        return True
    if _SEARCH_QUESTION.search(msg):
        return True
    return False


def resolve_intent(msg: str, *, is_group: bool = True,
                   is_mentioned: bool = False, role_tag: str = "friend") -> str:
    """对外便捷入口：返回意图并写入当前 trace（无上下文时安全忽略）。"""
    intent = classify_request(msg, is_group=is_group, is_mentioned=is_mentioned, role_tag=role_tag)
    try:
        from core.trace import patch_request
        patch_request(intent=intent)
    except Exception:
        pass
    return intent