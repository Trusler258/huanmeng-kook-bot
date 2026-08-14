"""
卡片格式化与校验（Huanmeng 2.0 Phase 5）

职责：
- 卡片 JSON 预校验与修复（KOOK 数组格式 [{"type":"card","modules":[...]}]）。
- 倒计时占位符 __COUNTDOWN__:秒数 → 毫秒时间戳（由程序计算，LLM 不负责毫秒）。
"""
from __future__ import annotations

import json
import re
import time

from core.logger import get_logger

logger = get_logger("sender.card")

# 倒计时占位符: __COUNTDOWN__:秒数
COUNTDOWN_RE = re.compile(r'"__COUNTDOWN__:(\d+)"')
# 卡片标记: [CARD]KOOK Card JSON[/CARD]
CARD_RE = re.compile(r'\[CARD\](.*?)\[/CARD\]', re.DOTALL)


def replace_countdown(card_json: str) -> str:
    """将卡片 JSON 中的 __COUNTDOWN__:秒数 占位符替换为毫秒时间戳。"""
    def _r(m):
        seconds = int(m.group(1))
        return str(int(time.time() * 1000) + seconds * 1000)
    return COUNTDOWN_RE.sub(_r, card_json)


def validate_and_repair_card_json(raw: str):
    """验证并修复卡片 JSON。返回 (ok: bool, fixed_json: str, error_detail: str)"""
    raw = raw.strip()
    detail = ""

    # ---- 解析 ----
    try:
        cards = json.loads(raw)
    except json.JSONDecodeError as e:
        detail = f"JSON解析失败: {e}"
        # 尝试修复：去掉末尾多余的 ]
        fixed = raw
        for _ in range(5):
            if fixed.rstrip().endswith("]]"):
                fixed = fixed.rstrip()[:-1]
            else:
                break
        # 补全缺失的括号
        open_brace = fixed.count("{") - fixed.count("}")
        open_bracket = fixed.count("[") - fixed.count("]")
        fixed += "}" * max(0, open_brace) + "]" * max(0, open_bracket)
        try:
            cards = json.loads(fixed)
            logger.info("卡片 JSON 括号修复成功 (补了%d个} %d个])", max(0, open_brace), max(0, open_bracket))
        except json.JSONDecodeError:
            return False, raw, detail

    # ---- 类型检查 ----
    if not isinstance(cards, list):
        cards = [cards]
    if not cards:
        return False, raw, "卡片数组为空"

    # ---- 结构验证与修复 ----
    fixed_count = 0
    for i, card in enumerate(cards):
        if not isinstance(card, dict):
            return False, raw, f"card[{i}] 不是 JSON 对象"
        if card.get("type") != "card":
            return False, raw, f"card[{i}].type 不是 'card'"
        if "modules" not in card:
            return False, raw, f"card[{i}] 缺少 modules 字段"
        if not isinstance(card["modules"], list):
            return False, raw, f"card[{i}].modules 不是数组"

        for j, mod in enumerate(card["modules"]):
            if not isinstance(mod, dict):
                return False, raw, f"card[{i}].modules[{j}] 不是对象"

            mtype = mod.get("type", "")
            # section 有 accessory 但缺 mode → 自动补 right
            if mtype == "section" and isinstance(mod.get("accessory"), dict) and "mode" not in mod:
                mod["mode"] = "right"
                fixed_count += 1
                logger.info("卡片修复: section[%d][%d] 自动补 mode=right", i, j)

            # accessory 内容校验：button 必须有 type/text/value
            acc = mod.get("accessory")
            if isinstance(acc, dict):
                atype = acc.get("type", "")
                if atype == "button":
                    # 自动修复：button.text 是字符串 → 转为 plain-text 对象
                    btn_text = acc.get("text")
                    if isinstance(btn_text, str):
                        acc["text"] = {"type": "plain-text", "content": btn_text}
                        fixed_count += 1
                        logger.info("卡片修复: button.text 字符串自动转对象: '%s'", btn_text[:50])
                    if "text" not in acc:
                        return False, raw, f"card[{i}].modules[{j}].accessory.button 缺少 text"
                    if "theme" not in acc:
                        acc["theme"] = "primary"
                        fixed_count += 1
                    if "value" not in acc:
                        acc["value"] = "click"
                        fixed_count += 1
                elif atype == "image":
                    if "src" not in acc:
                        return False, raw, f"card[{i}].modules[{j}].accessory.image 缺少 src"
                elif atype:
                    return False, raw, f"card[{i}].modules[{j}].accessory 未知类型: {atype}"

    if fixed_count:
        logger.info("卡片 JSON 修复了 %d 处，重新序列化", fixed_count)

    try:
        fixed_json = json.dumps(cards, ensure_ascii=False)
        return True, fixed_json, detail
    except Exception as e:
        return False, raw, f"修复后序列化失败: {e}"