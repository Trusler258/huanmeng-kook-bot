"""
统一日志模块
- 支持控制台 256 色输出 + 文件持久化
- 五个级别：DEBUG / INFO / WARNING / ERROR / CRITICAL
- DEBUG 级别受 bot_config.toml 中 [bot] 调试模式 开关控制
- 日志文件按天轮转，保留最近 7 天
- ANSI 256 色：时间淡灰 / DEBUG天蓝 / INFO淡紫加粗 / WARN暖橙 / ERROR鲜红
- 消息内智能着色：数字浅黄 / [模块]薄荷绿 / Emoji柔粉 / 管理员亮粉 / 成功冰蓝
"""

import re
import os
import sys
import time
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ── ANSI 256 色常量 ───────────────────────────────────────
A_TIME   = "\033[38;5;240m"   # 时间戳 浅灰
A_DEBUG  = "\033[38;5;117m"   # DEBUG 浅天蓝
A_INFO   = "\033[38;5;147m"   # INFO 淡紫
A_WARN   = "\033[38;5;214m"   # WARN 暖橙
A_ERROR  = "\033[38;5;196m"   # ERROR 鲜红
A_SOURCE = "\033[38;5;244m"   # 源码位置 深灰
A_SYMBOL = "\033[38;5;219m"   # 📩✅🐱 柔粉
A_MODULE = "\033[38;5;84m"    # [模块] 薄荷绿
A_NUM    = "\033[38;5;226m"   # 数字 浅黄
A_HI     = "\033[38;5;159m"   # 成功提示 冰蓝
A_ADMIN  = "\033[38;5;207m"   # 管理员操作 亮粉
A_SEP    = "\033[38;5;105m"   # 分割线 淡紫
RESET    = "\033[0m"
BOLD     = "\033[1m"

# 基本色（兼容旧代码）
RED     = "\033[31m"
GREEN   = "\033[32m"
BRIGHT_GREEN = "\033[92m"
YELLOW  = "\033[33m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"

# 级别 → 颜色映射（256色 + 加粗）
_LEVEL_COLORS: dict[int, tuple[str, str, str]] = {
    logging.DEBUG:    (A_DEBUG, "", A_DEBUG),       # 天蓝 无加粗
    logging.INFO:     (A_INFO, BOLD, A_INFO),       # 淡紫 加粗
    logging.WARNING:  (A_WARN, BOLD, A_WARN),       # 暖橙 加粗
    logging.ERROR:    (A_ERROR, BOLD, A_ERROR),     # 鲜红 加粗
    logging.CRITICAL: (A_ERROR, BOLD, A_ERROR),
}


# ── 消息智能着色 ──────────────────────────────────────────

def _color_msg(msg: str) -> str:
    """对日志消息内容进行智能部分着色"""
    # 数字：QQ号(5位+)、群号、耗时、token数、条数
    msg = re.sub(r'(\b\d{5,}\b)', f'{A_NUM}\\1{RESET}', msg)
    msg = re.sub(r'(\d+\.?\d*[sm]s|[\d.]+KB|[\d]+字|[\d]+条|[\d]+次)', f'{A_NUM}\\1{RESET}', msg)
    # [模块名]
    msg = re.sub(r'\[([A-Za-z\u4e00-\u9fff\s]+)\]', f'[{A_MODULE}\\1{RESET}]', msg)
    # Emoji 标记
    msg = re.sub(r'([📩✅🐱📎⚠️🔒🎨⚙️📂🐾🗑️🌐🔥])', f'{A_SYMBOL}\\1{RESET}', msg)
    # 管理员 / 指令
    msg = re.sub(r'(\.\w+)', f'{A_ADMIN}\\1{RESET}', msg)
    msg = re.sub(r'(admin|管理员|主人)', f'{A_ADMIN}\\1{RESET}', msg)
    # 成功状态
    msg = re.sub(r'(启动成功|连接完成|重载完成|准备就绪|初始化完成|已连接|截图成功)', f'{A_HI}\\1{RESET}', msg)
    # 分割线
    msg = re.sub(r'(={3,}|---+|═══+)', f'{A_SEP}\\1{RESET}', msg)
    return msg

# ── 全局状态 ───────────────────────────────────────────────
_logger: logging.Logger = None
_log_dir: Path = None
_debug_enabled: bool | None = None   # None = 尚未加载


def _get_base_dir() -> Path:
    """返回项目根目录（core/ 的上一级）"""
    return Path(__file__).resolve().parent.parent


def _get_log_dir() -> Path:
    """日志输出目录：项目根目录/logs/"""
    global _log_dir
    if _log_dir is None:
        _log_dir = _get_base_dir() / "logs"
        _log_dir.mkdir(exist_ok=True)
    return _log_dir


# ── 自定义 Formatter（支持颜色 + 时间戳 + 调用位置）───────
class _ColorFormatter(logging.Formatter):
    """256 色控制台 Formatter — 时间淡灰 / 级别分色 / 消息智能着色"""

    def format(self, record: logging.LogRecord) -> str:
        lv_color, lv_style, lv_text = _LEVEL_COLORS.get(record.levelno, (WHITE, "", WHITE))
        ts = f"{A_TIME}{time.strftime('%H:%M:%S', time.localtime(record.created))}.{f'{record.created % 1:.3f}'[2:]}{RESET}"
        level = f"{lv_style}{lv_color}{record.levelname:<8}{RESET}"
        src = f"{A_SOURCE}({record.module}:{record.funcName}:{record.lineno}){RESET}"
        msg = _color_msg(record.getMessage())
        return f"[{ts}] {level} {src} {msg}"


class _FileFormatter(logging.Formatter):
    """256 色文件 Formatter — 用于 tail -f 实时查看"""

    def format(self, record: logging.LogRecord) -> str:
        lv_color, lv_style, lv_text = _LEVEL_COLORS.get(record.levelno, (WHITE, "", WHITE))
        ts = f"{A_TIME}{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))}.{f'{record.created % 1:.3f}'[2:]}{RESET}"
        level = f"{lv_style}{lv_color}[{record.levelname:<8}]{RESET}"
        src = f"{A_SOURCE}({record.module}:{record.funcName}:{record.lineno}){RESET}"
        msg = _color_msg(record.getMessage())
        return f"[{ts}] {level} {src} {msg}"


# ── 公共 API ────────────────────────────────────────────────

def init_logger(debug_mode: bool = False, log_to_file: bool = True) -> logging.Logger:
    """
    初始化全局 Logger。
    
    Args:
        debug_mode: 是否启用 DEBUG 级别输出（来自 bot_config.toml）
        log_to_file: 是否同时写入文件
    Returns:
        配置好的 Logger 实例
    """
    global _logger, _debug_enabled
    _debug_enabled = debug_mode

    _logger = logging.getLogger("huanmeng")
    _logger.setLevel(logging.DEBUG)  # 根级别设为最低，由 handler 控制实际输出

    # 防止重复初始化：清理已有 handler
    if _logger.handlers:
        for h in _logger.handlers[:]:
            h.close()
            _logger.removeHandler(h)

    # ---- 控制台 Handler ----
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    console_handler.setFormatter(_ColorFormatter())
    _logger.addHandler(console_handler)

    # ---- 文件 Handler（可选）----
    if log_to_file:
        log_path = _get_log_dir() / "huanmeng.log"
        file_handler = TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # 文件始终记录所有级别
        file_handler.setFormatter(_FileFormatter())
        _logger.addHandler(file_handler)

    # ---- WebSocket 实时控制台 Handler ----
    try:
        from core.log_server import WebSocketLogHandler
        ws_handler = WebSocketLogHandler()
        ws_handler.setLevel(logging.DEBUG)
        # 纯文本 formatter（无 ANSI，颜色由前端 CSS 渲染）
        ws_fmt = logging.Formatter("[%(asctime)s] %(message)s")
        ws_fmt.default_msec_format = "%s.%03d"
        ws_handler.setFormatter(ws_fmt)
        _logger.addHandler(ws_handler)
    except Exception:
        pass  # 首次初始化时 log_server 可能尚未导入

    _logger.info("Logger 初始化完成 | debug=%s | 文件日志=%s", debug_mode, log_to_file)
    return _logger


def get_logger(name: str = "") -> logging.Logger:
    """
    获取 Logger 实例。若尚未初始化则自动用默认参数初始化。
    
    Args:
        name: 子模块名称，如 "judge" → 返回 "huanmeng.judge" logger
    """
    global _logger
    if _logger is None:
        init_logger()
    if name:
        return logging.getLogger(f"huanmeng.{name}")
    return _logger


def set_debug_mode(enabled: bool):
    """动态切换调试模式（reload 配置后调用）"""
    global _debug_enabled
    _debug_enabled = enabled
    if _logger is not None:
        for handler in _logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, TimedRotatingFileHandler):
                handler.setLevel(logging.DEBUG if enabled else logging.INFO)


def is_debug() -> bool:
    """查询当前是否处于调试模式"""
    global _debug_enabled
    if _debug_enabled is None:
        return False
    return _debug_enabled


# ── 便捷函数（兼容旧代码的 info/warning/error/debug 调用）──
def info(msg: str = "", *args, **kwargs):
    get_logger().info(msg, *args, **kwargs)

def warning(msg: str = "", *args, **kwargs):
    get_logger().warning(msg, *args, **kwargs)

def error(msg: str = "", *args, **kwargs):
    get_logger().error(msg, *args, **kwargs)

def debug(msg: str = "", *args, **kwargs):
    if is_debug():
        get_logger().debug(msg, *args, **kwargs)

def critical(msg: str = "", *args, **kwargs):
    get_logger().critical(msg, *args, **kwargs)
