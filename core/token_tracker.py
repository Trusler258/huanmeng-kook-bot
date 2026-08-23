"""
Token 消耗追踪
- 每次 LLM 调用记录 prompt/completion tokens + 缓存命中
- 每日汇总消耗金额（DeepSeek 价格）
- .cost 查看每日/累计消耗
- .tokens <文本> 计算 token 数和预估费用
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from core.logger import get_logger

logger = get_logger("token")

# DeepSeek 价格: ¥/百万 tokens
PRICE_CACHE_HIT = 0.02    # 缓存命中输入
PRICE_CACHE_MISS = 1.0    # 缓存未命中输入
PRICE_OUTPUT = 2.0         # 输出

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRACKER_FILE = "token_usage.jsonl"

# Tokenizer（DeepSeek V3，懒加载）
_TOKENIZER = None


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        # 优先 bot 内置目录，回退桌面
        for d in [
            DATA_DIR / "tokenizer",
            Path(os.path.expanduser("~/Desktop/deepseek_v3_tokenizer")),
        ]:
            if (d / "tokenizer.json").exists():
                try:
                    import transformers
                    _TOKENIZER = transformers.AutoTokenizer.from_pretrained(
                        str(d), trust_remote_code=True
                    )
                    logger.info("Tokenizer 已加载: %s", d)
                    return _TOKENIZER
                except Exception as e:
                    logger.warning("Tokenizer 加载失败 (%s): %s", d, e)
        return None
    return _TOKENIZER


def _token_count(text: str) -> int | None:
    tok = _get_tokenizer()
    if tok is None:
        return None
    try:
        return len(tok.encode(text))
    except Exception:
        return None


def _today_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"token_{date.today().strftime('%Y-%m')}.jsonl"


def record_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
):
    """记录一次 LLM 调用的 token 消耗"""
    entry = {
        "time": datetime.now().isoformat(),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
    }
    try:
        with open(_today_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_range(from_date: str, to_date: str | None = None) -> list[dict]:
    """加载指定日期范围的记录"""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date) if to_date else start
    records = []
    current = start
    while current <= end:
        f = DATA_DIR / f"token_{current.strftime('%Y-%m')}.jsonl"
        if f.exists():
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    r_date = r["time"][:10]
                    if from_date <= r_date <= (to_date or from_date):
                        records.append(r)
            except Exception:
                pass
        # 月份递增
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return records


def calc_cost(today_only: bool = False) -> dict:
    """计算消耗概览，返回字典"""
    target = date.today().strftime("%Y-%m-%d") if today_only else "2000-01-01"
    records = _load_range(target) if today_only else _load_range(target, date.today().strftime("%Y-%m-%d"))
    
    today_str = date.today().strftime("%Y-%m-%d")
    today_records = [r for r in records if r["time"][:10] == today_str]
    
    def _sum(recs, key):
        return sum(r.get(key, 0) for r in recs)
    
    total_prompt = _sum(records, "prompt_tokens")
    total_completion = _sum(records, "completion_tokens")
    total_cached = _sum(records, "cached_tokens")
    
    today_prompt = _sum(today_records, "prompt_tokens")
    today_completion = _sum(today_records, "completion_tokens")
    today_cached = _sum(today_records, "cached_tokens")
    
    def cost_str(prompt, completion, cached):
        cache_hit = cached / 1_000_000 * PRICE_CACHE_HIT
        cache_miss = (prompt - cached) / 1_000_000 * PRICE_CACHE_MISS
        output_cost = completion / 1_000_000 * PRICE_OUTPUT
        return cache_hit + cache_miss + output_cost
    
    return {
        "today": {
            "prompt": today_prompt,
            "completion": today_completion,
            "cached": today_cached,
            "calls": len(today_records),
            "cost": cost_str(today_prompt, today_completion, today_cached),
        },
        "total": {
            "prompt": total_prompt,
            "completion": total_completion,
            "cached": total_cached,
            "calls": len(records),
            "cost": cost_str(total_prompt, total_completion, total_cached),
        },
    }


async def cmd_cost(args, user_id, group_id, sender_name, is_group, bot_kook):
    """查看 Token 消耗 .cost"""
    data = calc_cost(today_only=False)
    t = data["today"]
    total = data["total"]
    from modules.cmd_cards import build_cost
    return "__CARD__:" + __import__("json").dumps(build_cost({
        "today": t, "total": total,
    }), ensure_ascii=False)


async def cmd_tokens(args, user_id, group_id, sender_name, is_group, bot_kook):
    """计算 token 数 .tokens <文本>"""
    if not args:
        return "用法: .tokens <文本>\n计算文本的 token 数和预估费用喵~"
    
    text = " ".join(args)
    count = _token_count(text)
    if count is None:
        return "Tokenizer 加载失败，请检查 ~/Desktop/deepseek_v3_tokenizer/ 目录喵~"
    
    input_cost = count / 1_000_000 * PRICE_CACHE_MISS
    output_cost = count / 1_000_000 * PRICE_OUTPUT
    lines = [
        f"文本: {text[:60]}{'...' if len(text) > 60 else ''}",
        f"Token: {count}",
        f"预估费用: 输入 ¥{input_cost:.6f} / 输出 ¥{output_cost:.6f}（缓存命中更便宜喵）",
    ]
    return "\n".join(lines)
