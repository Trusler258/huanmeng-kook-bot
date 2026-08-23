import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from core.logger import get_logger

logger = get_logger("wdsj_tracker")

_HISTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "wdsj_history.json"

# 全量采集模板 — 每个模板存整个 values dict
_TEMPLATES = {
    "bedwars-stats": "起床战争",
    "arena-stats": "竞技场",
}

# 日报卡片用的字段映射（起床战争）
_DAILY_FIELDS = {
    "kills": ("击杀", "num"),
    "finalKills": ("终杀", "num"),
    "wins": ("胜场", "num"),
    "deaths": ("死亡", "death"),
}

# 竞技场日报字段
_ARENA_DAILY_FIELDS = {
    "kills": ("击杀", "num"),
    "wins": ("胜场", "num"),
    "losses": ("败场", "loss"),
    "deaths": ("死亡", "death"),
}

# 趋势图指标列表
_TREND_METRICS = [
    ("bw_kills", "bedwars-stats", "kills", "起床-击杀"),
    ("bw_wins", "bedwars-stats", "wins", "起床-胜场"),
    ("bw_finals", "bedwars-stats", "finalKills", "起床-终杀"),
    ("bw_deaths", "bedwars-stats", "deaths", "起床-死亡"),
    ("arena_kills", "arena-stats", "kills", "竞技场-击杀"),
]


def _load_history():
    if _HISTORY_FILE.exists():
        try:
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_history(data):
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ------每日采集------
async def daily_stats_collect():
    from services import wdsj_api as api
    from modules.commands import _load_wdsj_bindings

    bindings = _load_wdsj_bindings()
    if not bindings:
        return

    from datetime import datetime as dt
    ts_str = dt.now().strftime("%Y-%m-%dT%H:%M:%S")
    history = _load_history()
    players = list(set(bindings.values()))

    logger.info("每日战绩采集: %d 个玩家 x %d 模板", len(players), len(_TEMPLATES))

    total = 0
    sem = asyncio.Semaphore(3)
    done = 0

    async def _collect_one(player):
        nonlocal total, done
        async with sem:
            entry = history.setdefault(player, {})
            for tid in _TEMPLATES:
                try:
                    data = await api.query_player_stats(player, tid, timeout=5.0)
                    if data and data.get("values"):
                        vals = dict(data["values"])
                        series = entry.setdefault(tid, [])
                        # ★ 只追加，不做日级覆盖（多时间点才能算增量）
                        series.append({"ts": ts_str, "values": vals})
                        total += 1
                except Exception as e:
                    logger.warning("采集失败 %s/%s: %s", player, tid, e)
                await asyncio.sleep(0.2)
            done += 1
            if done % 5 == 0:
                logger.info("采集进度: %d/%d", done, len(players))

    await asyncio.gather(*[_collect_one(p) for p in players])
    _save_history(history)
    logger.info("每日战绩采集完成: %d 条新记录", total)
    return history


# ------趋势数据------
def get_player_trend(player, metric, days=None):
    """取趋势序列；每天只保留最后一条记录（按日期去重取最新）
    days=None 表示从首次记录到最新一条"""
    history = _load_history()
    entry = history.get(player, {})
    for mid, tid, fn, lb in _TREND_METRICS:
        if mid == metric:
            raw = entry.get(tid, [])
            if days is not None and days > 0:
                raw = raw[-days:]
            # 按日期分组，每天只取最后一条
            by_day = {}
            for s in raw:
                day = s["ts"][:10]  # YYYY-MM-DD
                by_day[day] = {"ts": s["ts"], "val": int(s["values"].get(fn, 0))}
            return [by_day[k] for k in sorted(by_day)]
    return []


# ------趋势图------
def generate_trend_chart(player, metric, days=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    series = get_player_trend(player, metric, days)
    if not series:
        return None

    label = metric
    for mid, tid, fn, lb in _TREND_METRICS:
        if mid == metric:
            label = lb
            break

    dates = [datetime.strptime(s["ts"], "%Y-%m-%dT%H:%M:%S") for s in series]
    vals = [s["val"] for s in series]

    try:
        import matplotlib.font_manager as fm
        import os
        cache_dir = matplotlib.get_cachedir()
        for f in Path(cache_dir).glob("fontlist-v*.json"):
            f.unlink()
        fm._load_fontmanager(try_read_cache=False)
        plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "Noto Sans CJK SC", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    fig, ax = plt.subplots(figsize=(10, 4))
    line_color = "#7c3aed"
    ax.plot(dates, vals, marker="o", linewidth=2, markersize=5, color=line_color)
    ax.fill_between(dates, vals, alpha=0.08, color=line_color)

    vmin, vmax = min(vals), max(vals)
    margin = max((vmax - vmin) * 0.15, 1)
    ax.set_ylim(vmin - margin, vmax + margin)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(dates) // 10)))

    # 每天一个点，全部标注
    for d, v in zip(dates, vals):
        ax.annotate(str(v), (d, v), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=8, color=line_color)

    span = f"{dates[0]:%m/%d} ~ {dates[-1]:%m/%d}"
    ax.set_title(f"{player} - {label} 趋势 ({len(dates)}天, {span})", fontsize=13, fontweight="bold", color="#e0e0e0")
    ax.tick_params(colors="#999")
    ax.grid(True, alpha=0.2, color="#555")
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")
    for spine in ax.spines.values():
        spine.set_color("#333")

    fig.tight_layout()
    out_dir = Path(__file__).resolve().parent.parent / "data" / "img_temp"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"trend_{player}_{metric}_{int(time.time())}.png")
    fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)
    return out_path


# ------群内总排名------
async def build_group_rank(group_id, mode_key="bw_kills"):
    """取所有绑定玩家的最新指标值，排序生成排名卡片图片"""
    from modules.commands import _load_wdsj_bindings
    bindings = _load_wdsj_bindings()

    # 指标中文名
    label = mode_key
    for mid, tid, fn, lb in _TREND_METRICS:
        if mid == mode_key:
            label = lb
            break

    # 取每个玩家最新一条记录的值
    rows = []
    for qq, name in bindings.items():
        series = get_player_trend(name, mode_key)
        if series:
            rows.append((name, series[-1]["val"]))
    rows.sort(key=lambda x: -x[1])

    if not rows:
        return "暂无排名数据喵~ 需要先采集几天数据", None

    # 生成 HTML 卡片
    medal = ["🥇", "🥈", "🥉"]
    body_rows = []
    for i, (name, val) in enumerate(rows):
        rank_icon = medal[i] if i < 3 else f"#{i+1}"
        body_rows.append(
            f'<tr><td class="rank">{rank_icon}</td>'
            f'<td class="name">{name}</td>'
            f'<td class="val">{val}</td></tr>'
        )

    html = f"""<div class="rank-card">
      <div class="rank-header">
        <h2>群内排名 - {label}</h2>
        <div class="sub">{len(rows)} 名玩家</div>
      </div>
      <table>
        <thead><tr><th>#</th><th>玩家</th><th>{label}</th></tr></thead>
        <tbody>
          {''.join(body_rows)}
        </tbody>
      </table>
    </div>"""

    tmpl_path = Path(__file__).resolve().parent.parent / "data" / "templates" / "wdsj_card.html"
    tmpl = tmpl_path.read_text(encoding="utf-8")
    full_html = tmpl.replace("${CARD_CONTENT}", html)

    from modules.changelog import render_card_to_image
    import uuid
    filename = f"wdsj_rank_{mode_key}_{uuid.uuid4().hex[:8]}.jpg"
    img_path = await render_card_to_image(full_html, filename, width=680)
    return None, img_path


# ------每日排名数据------
def build_daily_rankings(label_date=None, cross_day=False):
    """
    cross_day=False: 当日最早 → 当日最新（手动查询）
    cross_day=True:  当日最早 → 次日最早（凌晨自动发送）
    """
    history = _load_history()
    if label_date is None:
        label_date = date.today().isoformat()
    from modules.commands import _load_wdsj_bindings
    bindings = _load_wdsj_bindings()

    # 计算次日日期（用于 cross_day 模式）
    tomorrow = (datetime.strptime(label_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") if cross_day else None

    rows = []
    new_players = []
    for uid, name in bindings.items():
        entry = history.get(name, {})
        bw = entry.get("bedwars-stats", [])
        if not bw:
            continue
        day_entries = [s for s in bw if s.get("ts", "").startswith(label_date)]
        if not day_entries:
            new_players.append(name)
            continue

        # baseline = 当日最早
        prev = day_entries[0].get("values", {})

        if cross_day:
            # 次日最早作为 end
            next_day = [s for s in bw if s.get("ts", "").startswith(tomorrow)]
            curr = next_day[0].get("values", {}) if next_day else day_entries[-1].get("values", {})
        else:
            # 当日最新
            curr = day_entries[-1].get("values", {})
        diffs = {}
        for k in _DAILY_FIELDS:
            pv = int(prev.get(k, 0))
            cv = int(curr.get(k, 0))
            diffs[k] = cv - pv
        # 增量全零标记（稍后判断是否全滤）
        is_zero = all(v == 0 for v in diffs.values())
        # KD 按今日增量计算: (击杀+终杀) / 死亡
        today_kills = diffs.get("kills", 0) + diffs.get("finalKills", 0)
        today_deaths = max(diffs.get("deaths", 1), 1)
        kd = today_kills / today_deaths
        rows.append((name, diffs, kd, is_zero))
    rows.sort(key=lambda x: -x[1].get("kills", 0))
    # 如果并非全员零增量，则过滤零增量玩家
    if rows and not all(r[-1] for r in rows):
        rows = [r for r in rows if not r[-1]]
    # 收集所有时间戳 → 计算时间段
    all_times = []
    cross_times = []
    for uid, name in bindings.items():
        bw = history.get(name, {}).get("bedwars-stats", [])
        day_entries = [s for s in bw if s.get("ts", "").startswith(label_date)]
        for e in day_entries:
            all_times.append(e.get("ts", ""))
        if cross_day:
            next_day = [s for s in bw if s.get("ts", "").startswith(tomorrow)]
            for e in next_day:
                cross_times.append(e.get("ts", ""))
    # 直接使用最早/最晚的真实采集时间
    time_start = min(all_times)[11:16] if all_times else "??:??"
    if cross_day and cross_times:
        end_ts = min(cross_times) if cross_times else max(all_times)
        # ★ 跨天模式：end 加日期前缀避免"00:01 → 00:01"的误解
        time_end = f"{end_ts[5:10]} {end_ts[11:16]}"
    else:
        time_end = max(all_times)[11:16] if all_times else "??:??"
    return rows, label_date, new_players, time_start, time_end


# ── 竞技场日报 ──

def build_arena_daily_rankings(label_date=None):
    """竞技场日榜: 击杀 / 胜场 / 败场 / 死亡 / KD"""
    history = _load_history()
    if label_date is None:
        label_date = date.today().isoformat()
    from modules.commands import _load_wdsj_bindings
    bindings = _load_wdsj_bindings()

    rows = []
    for uid, name in bindings.items():
        entry = history.get(name, {})
        arena = entry.get("arena-stats", [])
        if not arena:
            continue
        day_entries = [s for s in arena if s.get("ts", "").startswith(label_date)]
        if not day_entries:
            continue
        prev = day_entries[0].get("values", {})
        curr = day_entries[-1].get("values", {})
        diffs = {}
        for k in _ARENA_DAILY_FIELDS:
            pv = int(prev.get(k, 0))
            cv = int(curr.get(k, 0))
            diffs[k] = cv - pv
        if all(v == 0 for v in diffs.values()):
            continue
        kills = diffs.get("kills", 0)
        deaths = max(diffs.get("deaths", 1), 1)
        kd = kills / deaths
        rows.append((name, diffs, kd, curr.get("division", "?").replace("§c","").replace("§","")))
    rows.sort(key=lambda x: -x[1].get("kills", 0))

    all_times = []
    for uid, name in bindings.items():
        arena = history.get(name, {}).get("arena-stats", [])
        for e in arena:
            if e.get("ts", "").startswith(label_date):
                all_times.append(e.get("ts", ""))
    time_start = min(all_times)[11:16] if all_times else "??:??"
    time_end = max(all_times)[11:16] if all_times else "??:??"
    return rows, label_date, time_start, time_end
