"""洛花星雨日榜卡片 HTML 构建（数据/展示层）。

从 modules/commands.py 迁出：原为 .wdsj daily 与 bg_tasks 日榜推送共用，
插件化后放在 services 以避免 plugins/wdsj 与 plugins/bg_tasks 互相依赖。
"""
from __future__ import annotations

from utils.format_lang import format_lang


def build_daily_rank_html(rows, today, new_players, time_start="", time_end=""):
    """起床战争每日增量榜卡片 HTML。"""
    header = format_lang("_build_daily_rank_html.m1")  # 用于瀑布颜色
    trs = ""
    for i, item in enumerate(rows, 1):
        name, diffs, kd_val = item[0], item[1], item[2]
        rank = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else str(i)
        kd = diffs.get("kills", 0)
        fd = diffs.get("finalKills", 0)
        wd = diffs.get("wins", 0)
        dd = diffs.get("deaths", 0)
        k_info = f"+{kd}" if kd > 0 else "0"
        if fd > 0:
            k_info += f" (+{fd})"
        w_info = f"+{wd}" if wd > 0 else "0"
        d_info = f"+{dd}" if dd > 0 else "0"
        kd_info = f"{kd_val:.1f}"
        trs += f"""
        <tr>
            <td class='rank'>{rank}</td>
            <td class='name'>{name}</td>
            <td class='num'>{k_info}</td>
            <td class='num'>{w_info}</td>
            <td class='num death'>{d_info}</td>
            <td class='num kd'>{kd_info}</td>
        </tr>"""
    footer = ''
    if new_players:
        names = ', '.join(new_players[:6])
        more = format_lang("_build_daily_rank_html.m2", new_players=len(new_players)) if len(new_players) > 6 else ''
        footer += f"<div class='footer new'>🆕 新玩家 (明天入榜): {names}{more}</div>"
    # 下一轮采集时间
    from datetime import datetime
    now = datetime.now()
    next_hour = ((now.hour // 4 + 1) * 4) % 24
    next_time = f"{next_hour:02d}:01"
    any_abs = any(len(item) > 3 and item[3] for item in rows) if rows else False
    footer += f"<div class='footer hint'>⏰ 榜单每天 0/4/8/12/16/20 点更新 · 下一轮 {next_time}</div>"
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html {{ height: auto; }}
body {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color: #e0e0e0; font-family: 'Microsoft YaHei', sans-serif; width: 520px; padding: 20px 20px 8px 20px; height: auto; }}
.title {{ text-align: center; font-size: 20px; font-weight: bold; color: #00d4ff; margin-bottom: 2px; }}
.date {{ text-align: center; font-size: 12px; color: #6a8; margin-bottom: 14px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ padding: 8px 6px; font-size: 13px; color: #8ab; text-align: left; border-bottom: 2px solid #2a4a6a; }}
th.num {{ text-align: center; }}
td {{ padding: 8px 6px; font-size: 14px; border-bottom: 1px solid #1e2d50; }}
td.rank {{ width: 36px; text-align: center; font-size: 16px; }}
td.name {{ font-weight: 500; }}
td.num {{ text-align: center; font-weight: bold; color: #ff6b6b; }}
td.death {{ color: #6b8; }}
td.kd {{ color: #f0a050; }}
.footer {{ text-align: center; font-size: 11px; color: #888; margin-top: 10px; padding: 6px; background: rgba(255,255,255,0.05); border-radius: 4px; }}
</style></head>
<body>
<div class='title'>洛花星雨 今日增量 - 起床战争</div>
<div class='date'>{today} · {time_start} → {time_end}</div>
<table>
<tr><th>#</th><th>玩家</th><th class='num'>击杀(+终杀)</th><th class='num'>胜场</th><th class='num'>死亡</th><th class='num'>KD</th></tr>
{trs}
</table>
{footer}
</body></html>"""


def build_arena_daily_html(rows, today, time_start="", time_end=""):
    """竞技场每日榜卡片 HTML。"""
    from datetime import datetime
    now = datetime.now()
    next_hour = ((now.hour // 4 + 1) * 4) % 24
    next_time = f"{next_hour:02d}:01"

    trs = ""
    for i, (name, diffs, kd_val, div) in enumerate(rows, 1):
        rank = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else str(i)
        kd = diffs.get("kills", 0)
        wd = diffs.get("wins", 0)
        ld = diffs.get("losses", 0)
        dd = diffs.get("deaths", 0)
        k_info = f"+{kd}" if kd > 0 else "0"
        w_info = f"+{wd}" if wd > 0 else "0"
        l_info = f"+{ld}" if ld > 0 else "0"
        d_info = f"+{dd}" if dd > 0 else "0"
        kd_info = f"{kd_val:.1f}"
        trs += f"""
        <tr>
            <td class='rank'>{rank}</td>
            <td class='name'>{name}</td>
            <td class='div'>{div}</td>
            <td class='num'>{k_info}</td>
            <td class='num'>{w_info}</td>
            <td class='num loss'>{l_info}</td>
            <td class='num death'>{d_info}</td>
            <td class='num kd'>{kd_info}</td>
        </tr>"""
    footer = f"<div class='footer hint'>⏰ 榜单每天 0/4/8/12/16/20 点更新 · 下一轮 {next_time}</div>"
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html {{ height: auto; }}
body {{ background: linear-gradient(135deg, #2d1b1b 0%, #3e1f1f 50%, #601f1f 100%); color: #e0e0e0; font-family: 'Microsoft YaHei', sans-serif; width: 560px; padding: 20px 20px 8px 20px; height: auto; }}
.title {{ text-align: center; font-size: 20px; font-weight: bold; color: #ff6b6b; margin-bottom: 2px; }}
.date {{ text-align: center; font-size: 12px; color: #a88; margin-bottom: 14px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ padding: 8px 6px; font-size: 13px; color: #c9a; text-align: left; border-bottom: 2px solid #6a2a2a; }}
th.num {{ text-align: center; }}
td {{ padding: 8px 6px; font-size: 14px; border-bottom: 1px solid #3e1f1f; }}
td.rank {{ width: 36px; text-align: center; font-size: 16px; }}
td.name {{ font-weight: 500; }}
td.div {{ font-size: 12px; color: #d4a; }}
td.num {{ text-align: center; font-weight: bold; color: #ff6b6b; }}
td.loss {{ color: #daa; }}
td.death {{ color: #6b8; }}
td.kd {{ color: #f0a050; }}
.footer {{ text-align: center; font-size: 11px; color: #888; margin-top: 10px; padding: 6px; background: rgba(255,255,255,0.05); border-radius: 4px; }}
</style></head>
<body>
<div class='title'>洛花星雨 今日战绩 - 竞技场</div>
<div class='date'>{today} · {time_start} → {time_end}</div>
<table>
<tr><th>#</th><th>玩家</th><th>段位</th><th class='num'>击杀</th><th class='num'>胜场</th><th class='num'>败场</th><th class='num'>死亡</th><th class='num'>KD</th></tr>
{trs}
</table>
{footer}
</body></html>"""
