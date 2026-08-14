"""
消息格式化与路由（Huanmeng 2.0 Phase 5）

职责：
- 把一条待发送的字符串解析为结构化的 OutgoingMessage（文本段 + 特殊内容段）。
- 特殊内容包括：卡片 / HTTP 图片 / 本地图片 / CQ 文件 / 纯文本。
- 兜底归一化：裸 JSON 泄漏 → 替换为友好提示；裸卡片 JSON → 自动补 [CARD] 标记。

解析与发送解耦：本模块只回答"这条消息要发什么",
发送由 response_delivery 编排，避免 Card/KMarkdown fallback 导致重复发送。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

# 图片 URL 标记: [img:url]  本地路径标记: [img:file:path]
IMG_URL_RE = re.compile(r'\[img:(https?://[^\]]+)\]')
IMG_FILE_RE = re.compile(r'\[img:file:([^\]]+)\]')
# 卡片标记: [CARD]KOOK Card JSON[/CARD]
CARD_RE = re.compile(r'\[CARD\](.*?)\[/CARD\]', re.DOTALL)
# CQ 文件标记: [CQ:file,file=file:///本地路径,name=文件名]（对齐 QQ onebot 发文件）
CQ_FILE_RE = re.compile(r'\[CQ:file,file=file:///([^\],>]+),name=([^\]]+)\]')

# 兜底提示（LLM 格式错误时替换裸 JSON 泄漏）
_JSON_LEAK_FALLBACK = "呜…刚才脑子乱了一下喵，能再说一遍吗？(＞﹏＜)"


@dataclass
class OutgoingSegment:
    """一条特殊内容段。"""
    kind: str          # "card" | "img_url" | "img_file" | "file"
    content: str = ""  # card_json / url / 本地路径
    extra: Optional[str] = None  # 文件 name（CQ 文件）


@dataclass
class OutgoingMessage:
    """解析后的待发送消息：文本段 + 特殊内容段。"""
    text_before: str = ""
    segments: list[OutgoingSegment] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text_before and not self.segments


def _normalize(message: str) -> str:
    """兜底归一化：裸 JSON 泄漏 / 裸卡片 JSON 自动补标记。"""
    stripped = message.strip()
    # 兜底：检测裸 JSON 泄漏（LLM 格式错误时 _parse_reply 回退会把原始 JSON 当文本发）
    if stripped.startswith('{') and '"replies"' in stripped and ('"fav"' in stripped or '"calls"' in stripped):
        stripped = _JSON_LEAK_FALLBACK
    # 兜底：裸卡片 JSON（含 "type":"card" 且以 [{ 开头）自动补标记
    elif stripped.startswith('[{') and '"type":"card"' in stripped and '[CARD]' not in stripped:
        frag = stripped
        for _ in range(3):
            try:
                json.loads(frag)
                stripped = f'[CARD]{frag}[/CARD]'
                break
            except json.JSONDecodeError:
                frag = frag.rstrip().rstrip(']').rstrip()
    return stripped


def parse(message: str) -> OutgoingMessage:
    """把字符串解析为 OutgoingMessage。

    优先级：卡片 > CQ 文件 > 本地图片 > HTTP 图片 > 纯文本。
    文本段取特殊内容之前的部分（若存在），以 KMarkdown 发送。
    """
    message = _normalize(message)

    card_match = CARD_RE.search(message)
    if card_match:
        return OutgoingMessage(
            text_before=message[:card_match.start()].strip(),
            segments=[OutgoingSegment("card", card_match.group(1).strip())],
        )

    cq_match = CQ_FILE_RE.search(message)
    if cq_match:
        return OutgoingMessage(
            text_before=message[:cq_match.start()].strip(),
            segments=[OutgoingSegment("file", cq_match.group(1).strip(), cq_match.group(2))],
        )

    img_file_match = IMG_FILE_RE.search(message)
    if img_file_match:
        return OutgoingMessage(
            text_before=message[:img_file_match.start()].strip(),
            segments=[OutgoingSegment("img_file", img_file_match.group(1))],
        )

    img_url_match = IMG_URL_RE.search(message)
    if img_url_match:
        return OutgoingMessage(
            text_before=message[:img_url_match.start()].strip(),
            segments=[OutgoingSegment("img_url", img_url_match.group(1))],
        )

    return OutgoingMessage(text_before=message.strip(), segments=[])