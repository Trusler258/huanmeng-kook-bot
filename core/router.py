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
_SEARCH_TRIGGER = {"搜索", "查一下", "查一查", "什么是", "什么意思", "为什么",
                   "怎么", "如何", "定义", "百科", "解释", "多少", "何时",
                   "在哪", "介绍一下", "给我查", "帮我查", "热点", "够新的"}
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

    # 明显搜索意图
    if any(w in low for w in _SEARCH_TRIGGER):
        return "search"
    if any(w in low for w in _SEARCH_REALTIME):
        return "search"
    if _SEARCH_QUESTION.search(text):
        return "search"

    # 工具 / 数据查询
    if any(w in low for w in _TOOL_KEYWORDS):
        return "tool"

    # 默认普通聊天
    return "chat"


def needs_search_heuristic(msg: str) -> bool:
    """快速判断是否需要搜索（仅规则，不触发模型判断）。

    返回 True 时调用方应执行搜索；返回 False 表示普通聊天，无需搜索。
    """
    if not msg:
        return False
    low = msg.lower().strip()
    if any(w in low for w in _SEARCH_TRIGGER):
        return True
    if any(w in low for w in _SEARCH_REALTIME):
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