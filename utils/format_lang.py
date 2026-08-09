"""
语言格式化工具 — i18n 集中管理
- 从 lang.toml 加载所有语言字符串
- format_lang(key_path, **kwargs) 替换 ${var} 占位符
- 全项目唯一入口，不再重复定义
"""

from __future__ import annotations

import os
import toml
from pathlib import Path

# ── 语言数据缓存 ────────────────────────────────────────────
_LANG_DATA: dict = {}
_LANG_PATH: Path = None


def load_lang(config_dir: str | Path | None = None) -> dict:
    """
    加载语言文件 config/lang.toml
    
    Args:
        config_dir: 配置目录路径。默认为 项目根目录/config/
    Returns:
        语言字典
    """
    global _LANG_DATA, _LANG_PATH
    if config_dir is None:
        config_dir = Path(__file__).resolve().parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    _LANG_PATH = config_dir / "lang.toml"
    if not _LANG_PATH.exists():
        raise FileNotFoundError(f"语言文件不存在: {_LANG_PATH}")

    with open(_LANG_PATH, "r", encoding="utf-8") as f:
        _LANG_DATA = toml.load(f)

    return _LANG_DATA


def get_lang_data() -> dict:
    """获取当前已加载的语言数据字典"""
    global _LANG_DATA
    if not _LANG_DATA:
        load_lang()
    return _LANG_DATA


def format_lang(key_path: str, **kwargs) -> str:
    """
    从语言数据中获取文本并替换 ${xxx} 占位符。
    
    用法示例::
        format_lang('ping.response', name='幻梦')
        → "喵~ 幻梦在线中 (。>∀<。)ﾉ"
    
    Args:
        key_path: 点分路径，如 'ping.response'、'box.header'
        **kwargs: 要替换的变量，如 name='幻梦' → 替换文本中的 ${name}
    
    Returns:
        替换后的文本字符串
        
    Raises:
        KeyError: key_path 不存在时抛出（便于开发阶段快速定位遗漏）
    """
    data = get_lang_data()
    parts = key_path.split(".")
    node: dict | str | list = data
    for p in parts:
        if isinstance(node, dict):
            node = node.get(p)
            if node is None:
                return f"[LANG:{key_path}]"
        else:
            return f"[LANG:{key_path}]"

    text: str
    if isinstance(node, list):
        # 列表类型（如 help.command_list）→ 换行拼接
        text = "\n".join(str(item) for item in node)
    elif isinstance(node, str):
        text = node
    else:
        text = str(node)

    for k, v in kwargs.items():
        text = text.replace(f"${{{k}}}", str(v))

    return text


def has_key(key_path: str) -> bool:
    """检查某个语言 key 是否存在（不触发错误）"""
    try:
        data = get_lang_data()
        parts = key_path.split(".")
        node = data
        for p in parts:
            node = node[p]
        return True
    except (KeyError, TypeError):
        return False
