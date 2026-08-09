"""
系统提示词注入模块
- {{...}} 语法：管理员在消息中使用双大括号注入临时提示词
- 按聊天隔离存储，影响该聊天的所有后续回复
- {{reset}} 或 .preset 清除
"""
from __future__ import annotations

from core.logger import get_logger

logger = get_logger("preset")

# 按 chat_id 存储注入的提示词
_presets: dict[int, str] = {}


def set_preset(chat_id: int, text: str):
    """设置某个聊天会话的系统提示词注入"""
    _presets[chat_id] = text
    logger.info("提示词注入 [%d]: %s...", chat_id, text[:60])


def get_preset(chat_id: int) -> str:
    """获取当前注入的提示词，无注入返回空字符串"""
    return _presets.get(chat_id, "")


def clear_preset(chat_id: int) -> bool:
    """清除某个聊天的提示词注入，返回是否确实有东西被清除"""
    if chat_id in _presets:
        del _presets[chat_id]
        logger.info("提示词已清除 [%d]", chat_id)
        return True
    return False


def extract_preset_from_message(text: str) -> tuple[str | None, str]:
    """
    从消息中提取 {{...}} 注入内容。

    Returns:
        (注入文本, 清洗后的消息文本)
        - 注入文本为 None 表示没有 {{...}}
        - 清洗后的文本是去掉 {{...}} 部分的原始消息
    """
    import re
    match = re.search(r'\{\{(.+?)\}\}', text, re.DOTALL)
    if not match:
        return None, text

    preset_text = match.group(1).strip()
    cleaned = text[:match.start()] + text[match.end():]
    cleaned = cleaned.strip()
    return preset_text, cleaned
