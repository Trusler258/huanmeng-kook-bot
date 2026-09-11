"""
Token 消耗追踪
- 每次 LLM 调用记录 prompt/completion tokens + 缓存命中
- 每日汇总消耗金额（DeepSeek 价格）
- .cost 查看每日/累计消耗
- .tokens <文本> 计算 token 数和预估费用
- .cache 查看缓存命中率趋势 + 排查命中率低的原因
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
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
    cache_write_tokens: int = 0,
    scene: str = "",
):
    """记录一次 LLM 调用的 token 消耗

    cached_tokens: DeepSeek 返回的 prompt_tokens_details.cached_tokens（缓存命中）
    cache_write_tokens: prompt_tokens_details.cache_write_tokens（写入缓存）
    scene: 调用场景标识（reply/judge/search/tools 等），用于命中率归因
    """
    entry = {
        "time": datetime.now().isoformat(),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "scene": scene,
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


# ── 缓存命中率分析（.cache）────────────────────────────

def _hit_rate(prompt: int, cached: int) -> float:
    """缓存命中率 = cached / prompt"""
    return cached / prompt * 100 if prompt else 0.0


def analyze_cache(days: int = 7) -> dict:
    """分析缓存命中率，返回按天/按小时/按场景/按大小的统计数据"""
    today = date.today()
    start = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    records = _load_range(start, today.strftime("%Y-%m-%d"))

    # 按天聚合
    daily: dict[str, dict] = {}
    # 按小时聚合（最近一天）
    hourly: dict[str, dict] = {}
    # 按场景聚合
    by_scene: dict[str, dict] = {}
    # 按 prompt 大小分桶（区分主回复 vs judge/cheap 小调用）
    by_size: dict[str, dict] = {
        "主回复(>2000)": {"calls": 0, "prompt": 0, "cached": 0},
        "中(500-2000)": {"calls": 0, "prompt": 0, "cached": 0},
        "小(<500)": {"calls": 0, "prompt": 0, "cached": 0},
    }
    today_str = today.strftime("%Y-%m-%d")

    for r in records:
        d = r["time"][:10]
        h = r["time"][11:13]
        scene = r.get("scene", "") or "unknown"
        pt = r.get("prompt_tokens", 0)
        ct = r.get("cached_tokens", 0)
        daily.setdefault(d, {"calls": 0, "prompt": 0, "cached": 0, "cwrite": 0})
        daily[d]["calls"] += 1
        daily[d]["prompt"] += pt
        daily[d]["cached"] += ct
        daily[d]["cwrite"] += r.get("cache_write_tokens", 0)

        hourly.setdefault(h, {"calls": 0, "prompt": 0, "cached": 0})
        hourly[h]["calls"] += 1
        hourly[h]["prompt"] += pt
        hourly[h]["cached"] += ct

        by_scene.setdefault(scene, {"calls": 0, "prompt": 0, "cached": 0})
        by_scene[scene]["calls"] += 1
        by_scene[scene]["prompt"] += pt
        by_scene[scene]["cached"] += ct

        bucket = "主回复(>2000)" if pt > 2000 else ("中(500-2000)" if pt >= 500 else "小(<500)")
        by_size[bucket]["calls"] += 1
        by_size[bucket]["prompt"] += pt
        by_size[bucket]["cached"] += ct

    # 组装结果
    daily_out = [
        {
            "date": d,
            "calls": v["calls"],
            "prompt": v["prompt"],
            "cached": v["cached"],
            "cwrite": v["cwrite"],
            "rate": _hit_rate(v["prompt"], v["cached"]),
        }
        for d, v in sorted(daily.items())
    ]
    hourly_out = [
        {"hour": h, "calls": v["calls"], "prompt": v["prompt"], "cached": v["cached"],
         "rate": _hit_rate(v["prompt"], v["cached"])}
        for h, v in sorted(hourly.items())
    ]
    scene_out = [
        {"scene": s, "calls": v["calls"], "prompt": v["prompt"], "cached": v["cached"],
         "rate": _hit_rate(v["prompt"], v["cached"])}
        for s, v in sorted(by_scene.items(), key=lambda x: -x[1]["prompt"])
    ]
    size_out = [
        {"bucket": s, "calls": v["calls"], "prompt": v["prompt"], "cached": v["cached"],
         "rate": _hit_rate(v["prompt"], v["cached"])}
        for s, v in by_size.items()
    ]

    return {"daily": daily_out, "hourly": hourly_out, "by_scene": scene_out, "by_size": size_out, "today": today_str}


def _fmt_rate(rate: float) -> str:
    """命中率着色提示（纯文本用符号标记）"""
    if rate >= 70:
        return f"{rate:.1f}% ✓"
    if rate >= 40:
        return f"{rate:.1f}% ~"
    return f"{rate:.1f}% ✗"


async def cmd_cache(args, user_id, group_id, sender_name, is_group, bot_kook):
    """查看缓存命中率 .cache [天数]"""
    try:
        days = int(args[0]) if args else 7
        days = max(1, min(days, 30))
    except ValueError:
        days = 7

    data = analyze_cache(days)
    lines = [f"【缓存命中率分析】近{days}天"]

    # 按天
    lines.append("按天:")
    for d in data["daily"]:
        flag = " ← 今天" if d["date"] == data["today"] else ""
        lines.append(
            f"  {d['date']}: {d['calls']:>3}次 输入{d['prompt']:>7,} 缓存{d['cached']:>7,} "
            f"命中率{_fmt_rate(d['rate'])}{flag}"
        )

    # 按 prompt 大小（关键：区分主回复 vs judge）
    lines.append("按输入大小(关键):")
    for s in data["by_size"]:
        if s["calls"]:
            lines.append(
                f"  {s['bucket']:<12}: {s['calls']:>3}次 输入{s['prompt']:>7,} "
                f"命中率{_fmt_rate(s['rate'])}"
            )

    # 按场景（如果有多场景）
    scenes = data["by_scene"]
    if len(scenes) > 1:
        lines.append("按场景:")
        for s in scenes[:6]:
            lines.append(
                f"  {s['scene'][:12]:<12}: {s['calls']:>3}次 输入{s['prompt']:>7,} "
                f"命中率{_fmt_rate(s['rate'])}"
            )

    # 最近一天按小时（帮助定位低谷时段）
    hourly = data["hourly"]
    if hourly:
        lines.append("最近一天按小时(低谷段):")
        low = [h for h in hourly if h["rate"] < 40 and h["calls"] >= 1]
        if low:
            for h in low[:8]:
                lines.append(f"  {h['hour']}:00  {h['calls']}次 命中率{_fmt_rate(h['rate'])}")
        else:
            lines.append("  无明显低谷时段")

    # 结论：用主回复命中率（>2000 token）判断真实缓存健康度
    main_bucket = next((s for s in data["by_size"] if s["bucket"] == "主回复(>2000)"), None)
    main_rate = main_bucket["rate"] if main_bucket and main_bucket["calls"] else None
    today_rate = next((d["rate"] for d in data["daily"] if d["date"] == data["today"]), 0)
    lines.append("结论:")
    if main_rate is not None:
        if main_rate >= 70:
            lines.append(f"  主回复命中率 {main_rate:.1f}% — 缓存健康 ✓")
        elif main_rate >= 40:
            lines.append(f"  主回复命中率 {main_rate:.1f}% — 偏低，注意 system 是否频繁变化")
        else:
            lines.append(f"  主回复命中率 {main_rate:.1f}% — 低，system prompt 变化或调用不连续")
        lines.append(f"  总体命中率 {today_rate:.1f}%（受 judge 小调用稀释，仅供参考）")
    else:
        if today_rate >= 70:
            lines.append(f"  今日命中率 {today_rate:.1f}% 正常 ✓")
        else:
            lines.append(f"  今日命中率 {today_rate:.1f}% — 主回复调用少，多为 judge 小调用拉低")
            lines.append("  建议: ①保持 system prompt 稳定 ②群活跃时前缀缓存自然累积")

    return "\n".join(lines)
