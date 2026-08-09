"""
表情库 — 关键词匹配本地表情 GIF
用法: get_face("开心") → (filepath, CQ码)
LLM 输出 [FACE:开心] → pipeline 自动发图
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("face_lib")

# 关键词 → 文件名映射（由文件名自动生成）
_KEYWORD_MAP: dict[str, list[str]] = {}
_initialized = False


def _init():
    global _initialized
    if _initialized:
        return
    faces_dir = Path(__file__).resolve().parent.parent / "data" / "faces"
    if not faces_dir.is_dir():
        logger.warning("表情目录不存在: %s", faces_dir)
        _initialized = True
        return
    for f in faces_dir.iterdir():
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            continue
        stem = f.stem
        # 从文件名拆关键词：每个中文字/词都作为匹配关键词
        for kw in _extract_keywords(stem):
            _KEYWORD_MAP.setdefault(kw, []).append(str(f))
    logger.info("表情库加载: %d 关键词, %d 文件", len(_KEYWORD_MAP), len(list(faces_dir.iterdir())))
    _initialized = True


def _extract_keywords(name: str) -> list[str]:
    """从中文文件名提取关键词"""
    kw = [name]  # 完整文件名也是一个关键词
    # 拆出2-4字的短语
    for i in range(len(name)):
        for j in (2, 3, 4):
            if i + j <= len(name):
                kw.append(name[i:i + j])
    return kw


def get_face(keyword: str) -> Optional[str]:
    """根据关键词获取表情的绝对路径，找到返回首个匹配"""
    _init()
    keyword = keyword.strip()
    # 精确匹配优先
    if keyword in _KEYWORD_MAP:
        return random.choice(_KEYWORD_MAP[keyword])
    # 模糊匹配：包含关键词
    for kw, files in _KEYWORD_MAP.items():
        if keyword in kw or kw in keyword:
            return random.choice(files)
    return None


def make_cq(filepath: str) -> str:
    """生成图片标记"""
    return f"[img:file:{filepath.replace(chr(92), '/')}]"


def list_keywords() -> list[str]:
    """列出所有一级关键词（完整文件名）"""
    _init()
    return sorted(set(
        k for k in _KEYWORD_MAP if len(k) >= 3 and all('\u4e00' <= c <= '\u9fff' for c in k)
    ))[:30]
