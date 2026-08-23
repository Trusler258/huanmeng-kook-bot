"""
地震速报模块 v1.2
- 5s 轮询 CENC 实时数据，M≥4.0 推送
- .eq 手动查询 | .eq sub 订阅 | .eq lv <x> 设置最低震级
"""

from __future__ import annotations

import asyncio
import json
import re
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.logger import get_logger

logger = get_logger("earthquake")

# ── EQ 专用 Web 监视器（端口 62001）───────────────────
_eq_logs: list[str] = []  # 最多保留 200 条

def _eq_info(msg: str):
    """记录到 EQ 日志缓冲 + 主日志（INFO）"""
    t = datetime.now(CST).strftime("%H:%M:%S")
    logger.info(msg)
    _eq_logs.append(f"[{t}] {msg}")
    if len(_eq_logs) > 200:
        _eq_logs.pop(0)

def _eq_debug(msg: str):
    """仅记录到 EQ 日志缓冲（DEBUG，不输出主日志）"""
    t = datetime.now(CST).strftime("%H:%M:%S")
    _eq_logs.append(f"[{t}] {msg}")
    if len(_eq_logs) > 200:
        _eq_logs.pop(0)

# ── 省级映射表（用于省筛，从震中地名提取省份） ──────────
# CENC EEW 的 HypoCenter 格式：「四川省宜宾市兴文县」「台湾花莲县」等
_PROVINCE_MAP: dict[str, str] = {
    # 直辖市 — 不加"省"
    "北京": "北京", "北京市": "北京",
    "上海": "上海", "上海市": "上海",
    "天津": "天津", "天津市": "天津",
    "重庆": "重庆", "重庆市": "重庆",
    # 省份 — 加"省"
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省",
    "吉林": "吉林省", "黑龙江": "黑龙江省",
    "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省",
    "河南": "河南省", "湖北": "湖北省", "湖南": "湖南省",
    "广东": "广东省", "海南": "海南省",
    "四川": "四川省", "贵州": "贵州省", "云南": "云南省",
    "陕西": "陕西省", "甘肃": "甘肃省", "青海": "青海省",
    "台湾": "台湾省",
    # 自治区 — 保持原名
    "内蒙古": "内蒙古", "广西": "广西", "西藏": "西藏",
    "宁夏": "宁夏", "新疆": "新疆",
    # 港澳
    "香港": "香港", "澳门": "澳门",
}

def _get_province(place: str) -> str | None:
    """从震中地名提取省份"""
    if not place:
        return None
    # 从长到短匹配，避免"吉林"误匹配"吉林省吉林市"时取错
    keys = sorted(_PROVINCE_MAP.keys(), key=len, reverse=True)
    for k in keys:
        if k in place:
            return _PROVINCE_MAP[k]
    return None
HEADERS = {"User-Agent": "HuanMeng-QQBot/1.1", "Accept": "application/json"}
CST = timezone(timedelta(hours=8))
# wolfx.jp 聚合了 CENC + 各省地震局数据，全部免费无认证
_SOURCES = [
    # (名称, URL, 格式)
    ("CENC", "https://api.wolfx.jp/cenc_eqlist.json", "cenc_eqlist"),
    ("CENC预警", "https://api.wolfx.jp/cenc_eew.json", "cenc_eew"),
    ("四川预警", "https://api.wolfx.jp/sc_eew.json", "sc_eew"),
    ("福建预警", "https://api.wolfx.jp/fj_eew.json", "fj_eew"),
    ("重庆预警", "https://api.wolfx.jp/cq_eew.json", "cq_eew"),
]
POLL_INTERVAL = 1          # 轮询间隔秒（Wolfx 限制 2/s，单源 1s 安全）
DEFAULT_MIN_MAG = 4.0      # 默认最低震级
SUBS_FILE = "eq_subs.json"

# ── 烈度/震度 ──────────────────────────────────────────
_INTENSITY_ROMAN = {1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX",10:"X"}

def _estimate_intensity(mag: float, depth: float) -> float:
    v = mag * 1.5 - depth / 20.0
    return round(max(0, min(v, 12)), 1)

def _estimate_shindo(mag: float, depth: float) -> str:
    if mag < 3:    return "1"
    if mag < 4:    return "2" if depth > 20 else "3"
    if mag < 5:    return "3" if depth > 30 else "4"
    if mag < 6:    return "4" if depth > 40 else "5-"
    if mag < 7:    return "5+" if depth > 50 else "6-"
    if mag < 8:    return "6+" if depth > 60 else "7"
    return "7"


# ── 状态 & 订阅 ──────────────────────────────────────────

_last_eq: dict | None = None   # 记录上次推送的地震，用于一报/二报判断
# {group_id: {"min_mag": 4.0}}
_subs: dict[int, dict] = {}


def _subs_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / SUBS_FILE


def load_subs():
    global _subs
    p = _subs_path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            _subs = {}
            for k, v in raw.items():
                gid = int(k)
                if isinstance(v, dict):
                    _subs[gid] = {"min_mag": float(v.get("min_mag", DEFAULT_MIN_MAG))}
                elif isinstance(v, list):
                    # 旧格式兼容：只存了 group_id 列表
                    _subs[gid] = {"min_mag": DEFAULT_MIN_MAG}
        except Exception:
            _subs = {}


def save_subs():
    raw = {}
    for gid, v in _subs.items():
        entry = {"min_mag": v["min_mag"]}
        if "provinces" in v:
            entry["provinces"] = v["provinces"]
        raw[str(gid)] = entry
    _subs_path().parent.mkdir(parents=True, exist_ok=True)
    _subs_path().write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 数据获取 ────────────────────────────────────────────

def _parse_eq(item: dict) -> dict | None:
    eq = {}
    for k in ("M","m","magnitude","震级","mag","Magnitude"):
        if k in item:
            try: eq["mag"] = float(item[k]); break
            except: pass
    if "mag" not in eq: return None

    for k in ("LOCATION_C","location","epicenter","place","位置","epi","地名","HypoCenter"):
        if k in item and item[k]:
            eq["place"] = str(item[k]).strip(); break
    eq.setdefault("place", "未知")

    for k in ("O_TIME","time","origin_time","发震时间","datetime","date","ot","OriginTime"):
        if k in item:
            dt = _parse_time(str(item[k]))
            if dt: eq["time"] = dt; break
    eq.setdefault("time", datetime.now(CST))

    for k in ("EPI_DEPTH","depth","焦点深度","深度","dep","Depth"):
        if k in item:
            try: eq["depth"] = float(item[k]); break
            except: pass
    eq.setdefault("depth", 10.0)

    lon, lat = 0.0, 0.0
    for k in ("EPI_LON","longitude","lng","经度","lon","Longitude"):
        if k in item:
            try: lon = float(item[k]); break
            except: pass
    for k in ("EPI_LAT","latitude","lat","纬度","Latitude"):
        if k in item:
            try: lat = float(item[k]); break
            except: pass
    eq["lon"], eq["lat"] = lon, lat

    for k in ("EventID","id","ID","eqid","event_id","_id","ID","EventID"):
        if k in item and item[k]:
            eq["id"] = str(item[k]); break
    eq.setdefault("id", f"{eq['time'].strftime('%Y%m%d%H%M%S')}_{eq['mag']:.1f}_{eq['lon']:.2f}_{eq['lat']:.2f}")
    return eq


def _deduplicate(quakes: list[dict]) -> list[dict]:
    """合并多源数据：同一位置 60 秒内视为同一事件，取最早时间 + 最大震级"""
    if not quakes:
        return []
    quakes.sort(key=lambda q: q["time"])
    merged = []
    for q in quakes:
        found = False
        for m in merged:
            dt = abs((q["time"] - m["time"]).total_seconds())
            if dt < 60 and q["place"] == m["place"]:
                m["mag"] = max(m["mag"], q["mag"])
                found = True
                break
        if not found:
            merged.append(dict(q))
    return merged


async def _fetch_all_sources() -> list[dict] | None:
    """并行请求全部地震数据源，合并去重"""
    # 响应缓存：URL → (hash, items)，内容不变就跳过解析
    global _resp_cache
    import hashlib
    
    async def _fetch_one(name: str, url: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=6, verify=False) as c:
                r = await c.get(url, headers=HEADERS)
                r.raise_for_status()
                raw = r.text
                h = hashlib.md5(raw.encode()).hexdigest()
                cached = _resp_cache.get(url)
                if cached and cached[0] == h:
                    return cached[1]  # 内容不变，返回缓存的解析结果
                data = r.json()
        except Exception:
            return []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if "HypoCenter" in data or "Magnitude" in data:
                items = [data]
            else:
                items = []
                for key in sorted(data.keys()):
                    if key.startswith("No") and isinstance(data[key], dict):
                        item = data[key].copy()
                        item["_no"] = key
                        items.append(item)
                if not items:
                    for k in ("records", "data", "earthquakes", "list", "shuju"):
                        if k in data and isinstance(data[k], list):
                            items = data[k]
                            break
        else:
            items = []
        # 缓存结果
        _resp_cache[url] = (h, items)
        return items

    # 并行请求全部数据源
    tasks = [_fetch_one(name, url) for name, url, _ in _SOURCES]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for i, items in enumerate(results):
        name = _SOURCES[i][0]
        if isinstance(items, Exception):
            logger.debug("数据源 %s 失败: %s", name, items)
            continue
        if items:
            _eq_debug(f"{name}: {len(items)}条")
            all_items.extend(items)

    if not all_items:
        return None

    parsed = []
    for item in all_items:
        eq = _parse_eq(item)
        if eq:
            parsed.append(eq)
    return _deduplicate(parsed) or None


def _parse_time(s: str) -> datetime | None:
    for pat, fmt in [
        (r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})","%Y-%m-%d %H:%M:%S"),
        (r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})","%Y/%m/%d %H:%M:%S"),
        (r"(\d{8})(\d{6})","%Y%m%d%H%M%S"),
        (r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})","%Y-%m-%dT%H:%M:%S"),
    ]:
        m = re.search(pat, s)
        if m:
            try:
                if fmt == "%Y%m%d%H%M%S":
                    return datetime.strptime(m.group(0), fmt).replace(tzinfo=CST)
                return datetime.strptime(m.group(0), fmt).replace(tzinfo=CST)
            except: pass
    return None


# ── 格式化 ──────────────────────────────────────────────

def _is_same_event(a: dict, b: dict) -> bool:
    """判断两个地震是否为同一事件（同时间同地点）"""
    td = abs((a["time"] - b["time"]).total_seconds())
    if td > 300:  # 时间差 > 5 分钟，不是同一个
        return False
    # 地点比较：取前 4 个字符或完整匹配
    pa = a.get("place", "")[:6]
    pb = b.get("place", "")[:6]
    return pa == pb

def _diff(old_val, new_val, fmt=".1f", unit="") -> str:
    """比较新旧值，返回变化箭头或不变"""
    try:
        o = float(old_val)
        n = float(new_val)
        if fmt == ".0f":
            o_s, n_s = f"{o:.0f}", f"{n:.0f}"
        else:
            o_s, n_s = f"{o:{fmt}}", f"{n:{fmt}}"
        if o_s == n_s:
            return f"{n_s}{unit}"
        return f"{o_s}{unit} → {n_s}{unit}"
    except (ValueError, TypeError):
        # 非数字直接比较
        if str(old_val) == str(new_val):
            return str(new_val)
        return f"{old_val} → {new_val}"

def _format(eq: dict) -> str:
    mag, depth = eq["mag"], eq["depth"]
    i = _estimate_intensity(mag, depth)
    roman = _INTENSITY_ROMAN.get(min(int(i), 10), "?")
    shindo = _estimate_shindo(mag, depth)
    dt = eq["time"].astimezone(CST).strftime("%Y年%m月%d日 %H:%M:%S")
    lo = f"{abs(eq['lon']):.2f}{'E' if eq['lon']>=0 else 'W'}"
    la = f"{abs(eq['lat']):.2f}{'N' if eq['lat']>=0 else 'S'}"
    return (
        "[CENC/中国地震台网 地震报告]\n"
        "===================\n"
        f"震中　　　　 | {eq['place']}\n"
        f"震级　　　　 | M{mag:.1f}\n"
        f"深度　　　　 | {depth:.0f} km\n"
        f"发震时间　　 | {dt}(UTC+8)\n"
        f"经纬度　　　 | {lo} {la}\n"
        f"预估最大烈度 | {i:.1f} [{roman}]\n"
        f"预估最大震度 | {shindo}\n"
        "==================="
    )


def _format_update(eq: dict, last: dict) -> str:
    """二报格式 — 对比新旧数据，标注变化"""
    mag, depth = eq["mag"], eq["depth"]
    i = _estimate_intensity(mag, depth)
    roman = _INTENSITY_ROMAN.get(min(int(i), 10), "?")
    shindo = _estimate_shindo(mag, depth)

    last_mag = last["mag"]
    last_depth = last["depth"]
    last_i = _estimate_intensity(last_mag, last_depth)
    last_roman = _INTENSITY_ROMAN.get(min(int(last_i), 10), "?")
    last_shindo = _estimate_shindo(last_mag, last_depth)

    dt = eq["time"].astimezone(CST).strftime("%Y年%m月%d日 %H:%M:%S")
    lo = f"{abs(eq['lon']):.2f}{'E' if eq['lon']>=0 else 'W'}"
    la = f"{abs(eq['lat']):.2f}{'N' if eq['lat']>=0 else 'S'}"
    last_lo = f"{abs(last['lon']):.2f}{'E' if last['lon']>=0 else 'W'}"
    last_la = f"{abs(last['lat']):.2f}{'N' if last['lat']>=0 else 'S'}"

    changed = (mag != last_mag or abs(depth - last_depth) > 0.5 or
               abs(eq['lon'] - last['lon']) > 0.01 or abs(eq['lat'] - last['lat']) > 0.01)
    tag = " [更新]" if changed else ""

    return (
        f"[CENC/中国地震台网 地震报告{tag}]\n"
        "===================\n"
        f"震中　　　　 | {eq['place']}\n"
        f"震级　　　　 | {_diff(last_mag, mag)} 级\n"
        f"深度　　　　 | {_diff(last_depth, depth, '.0f', ' km')}\n"
        f"发震时间　　 | {dt}(UTC+8)\n"
        f"经纬度　　　 | {last_lo} {last_la} → {lo} {la}\n"
        f"预估最大烈度 | {_diff(last_i, i)} [{last_roman} → {roman}]\n"
        f"预估最大震度 | {_diff(last_shindo, shindo)}\n"
        "==================="
    )


# ── 后台轮询 ────────────────────────────────────────────


# ── 天地图 Leaflet 地图 ──────────────────────────────

_TDT_KEY = "e8a3347bbea66fc10fc060d62e7e8d3d"

def _build_tianditu_map(lat: float, lon: float, mag: float) -> str:
    """Leaflet 天地图瓦片 + 震中标记"""
    zoom = 5 if mag >= 7 else (6 if mag >= 5 else 7)

    return f'''<link rel="stylesheet" href="https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
<div id="eqmap" style="width:448px;height:150px;border-radius:6px;overflow:hidden;margin:10px 0;background:#1a1a2e"></div>
<script src="https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function() {{
  if (typeof L === "undefined") {{
    document.getElementById("eqmap").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#666;font-size:12px">地图加载失败 | {lat:.2f} {lon:.2f}</div>';
    return;
  }}
  var map = L.map("eqmap", {{ zoomControl:false, attributionControl:false, dragging:false, scrollWheelZoom:false, doubleClickZoom:false, touchZoom:false, keyboard:false }}).setView([{lat}, {lon}], {zoom});
  var tk = "{_TDT_KEY}";
  L.tileLayer("https://t{{s}}.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=vec&STYLE=default&TILEMATRIXSET=w&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}&FORMAT=tiles&tk="+tk, {{ subdomains:["0","1","2","3","4","5","6","7"], maxZoom:18 }}).addTo(map);
  L.tileLayer("https://t{{s}}.tianditu.gov.cn/cva_w/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=cva&STYLE=default&TILEMATRIXSET=w&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}&FORMAT=tiles&tk="+tk, {{ subdomains:["0","1","2","3","4","5","6","7"], maxZoom:18 }}).addTo(map);
  var icon = L.divIcon({{ html:'<div style="width:18px;height:18px;background:radial-gradient(circle,#ff1744 30%,rgba(255,23,68,0) 70%);border:2px solid #fff;border-radius:50%;box-shadow:0 0 12px #ff1744"></div>', iconSize:[18,18], iconAnchor:[9,9], className:"" }});
  L.marker([{lat}, {lon}], {{ icon:icon }}).addTo(map);
}})();
</script>'''


# ── HTML 地震卡片 ─────────────────────────────────────

def _build_eq_card(eq: dict, sources: list[dict] | None = None) -> str:
    """构建手机风地震预警 HTML 卡片"""
    mag = eq["mag"]
    depth = eq.get("depth", 10)
    place = eq.get("place", "未知")
    lon = eq.get("lon", 0)
    lat = eq.get("lat", 0)
    dt = eq["time"].astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    i_val = _estimate_intensity(mag, depth)
    roman = _INTENSITY_ROMAN.get(min(int(i_val), 10), "?")

    # 颜色：按烈度分级
    if i_val >= 7:   bg_color = "#b71c1c"; accent = "#ff5252"
    elif i_val >= 6: bg_color = "#c62828"; accent = "#ff6659"
    elif i_val >= 5: bg_color = "#e65100"; accent = "#ff9100"
    elif i_val >= 4: bg_color = "#f57f17"; accent = "#ffd600"
    else:            bg_color = "#283593"; accent = "#536dfe"

    # 震级颜色
    if mag >= 7:     mag_color = "#ff1744"
    elif mag >= 6:   mag_color = "#ff6d00"
    elif mag >= 5:   mag_color = "#ffd600"
    elif mag >= 4:   mag_color = "#76ff03"
    else:            mag_color = "#4fc3f7"

    # 多源摘要
    src_rows = ""
    if sources:
        for s in sources[:5]:
            src_rows += f'<span class="src">{s}</span>\n'

    card = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:480px;background:#0d0d1a;color:#e0e0e0;font-family:"PingFang SC","Microsoft YaHei",sans-serif;overflow:hidden}}
.header{{background:{bg_color};padding:12px 16px;display:flex;align-items:center;gap:8px}}
.header .icon{{font-size:24px}}
.header .title{{font-size:16px;font-weight:700;color:#fff}}
.header .sub{{font-size:11px;color:rgba(255,255,255,.7);margin-left:auto}}
.body{{padding:14px 16px}}
.mag-row{{display:flex;align-items:center;gap:16px;margin-bottom:12px}}
.mag-circle{{width:72px;height:72px;border-radius:50%;background:{mag_color};display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 20px {mag_color}33}}
.mag-num{{color:#fff;font-size:32px;font-weight:900}}
.mag-unit{{color:rgba(255,255,255,.8);font-size:14px;margin-left:2px}}
.mag-info{{flex:1}}
.mag-info .loc{{font-size:18px;font-weight:700;color:#fff;margin-bottom:4px}}
.mag-info .detail{{font-size:12px;color:#999;line-height:1.6}}
.intensity{{display:inline-block;background:{accent};color:#000;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:700;margin-top:4px}}
.map{{width:100%;height:150px;background:#1a1a2e;border-radius:6px;position:relative;overflow:hidden;margin:10px 0}}
.map img{{width:100%;height:100%;object-fit:cover;opacity:.6}}
.marker{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);}}
.marker svg{{width:32px;height:40px}}
.divider{{border-top:1px solid #1e1e3a;margin:10px 0}}
.sources{{padding:0 16px 12px}}
.sources .title{{font-size:11px;color:#666;margin-bottom:4px}}
.src{{display:inline-block;background:#1a1a2e;color:#4ade80;font-size:10px;padding:2px 8px;border-radius:4px;margin:2px 4px 2px 0}}
.footer{{padding:8px 16px;background:#0a0a14;font-size:10px;color:#555;display:flex;justify-content:space-between}}
</style></head><body>
<div class="header">
<span class="icon">⚡</span>
<span class="title">地震速报预警</span>
<span class="sub">M≥{DEFAULT_MIN_MAG:.0f} | {len(_SOURCES)}源</span>
</div>
<div class="body">
<div class="mag-row">
<div class="mag-circle"><span class="mag-num">{mag:.1f}</span><span class="mag-unit">级</span></div>
<div class="mag-info">
<div class="loc">{place}</div>
<div class="detail">
深度 {depth:.0f}km · {dt}<br>
{abs(lon):.2f}{'E' if lon>=0 else 'W'} {abs(lat):.2f}{'N' if lat>=0 else 'S'}
</div>
<div class="intensity">烈度 {i_val:.1f} ({roman})</div>
</div>
</div>
'''
    # 天地图 Leaflet 地图
    card += _build_tianditu_map(lat, lon, mag)

    card += f'''<div class="sources"><div class="title">数据源</div>{src_rows}</div>
<div class="footer"><span>幻梦 QQ Bot · 地震速报</span><span>{dt}</span></div>
</body></html>'''
    return card


async def _render_eq_card(eq: dict, sources: list[str] | None = None) -> str | None:
    """将地震卡片渲染为 PNG，返回文件路径"""
    try:
        from modules.changelog import _ensure_browser
        browser = await _ensure_browser()
        page = await browser.new_page()
        await page.set_viewport_size({"width": 480, "height": 400})
        html = _build_eq_card(eq, sources)
        await page.set_content(html, wait_until="load", timeout=10000)
        # 等 Leaflet 初始化 + 天地图瓦片加载
        try:
            await page.wait_for_selector(".leaflet-tile-loaded", timeout=6000)
        except Exception:
            pass
        await asyncio.sleep(0.8)
        import tempfile, os
        fd, png_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        await page.screenshot(path=png_path, full_page=True)
        await page.close()
        return png_path
    except Exception as e:
        logger.warning("地震卡片渲染失败: %s", e)
        return None


# ── 后台轮询 ────────────────────────────────────────────

_poll_task: asyncio.Task | None = None
_pushed_cache: dict[str, datetime] = {}  # place → last_push_time
_resp_cache: dict[str, tuple] = {}  # url → (hash, items)


async def start_polling():
    global _poll_task, _last_eq
    load_subs()
    # 启动 EQ 专用 Web 监视器
    asyncio.create_task(_start_eq_webserver())
    # 启动时先拉一次数据作为基准，不播报已过的地震
    try:
        data = await _fetch_all_sources()
        if data:
            parsed = [eq for eq in data if eq["mag"] >= DEFAULT_MIN_MAG]
            if parsed:
                parsed.sort(key=lambda x: x["time"], reverse=True)
                _last_eq = parsed[0]
    except Exception:
        pass
    _poll_task = asyncio.create_task(_poll_loop())


async def _poll_loop():
    global _last_eq, _pushed_cache
    while True:
        try:
            data = await _fetch_all_sources()
            if data:
                parsed = [eq for eq in data if eq["mag"] >= DEFAULT_MIN_MAG]
                if parsed:
                    parsed.sort(key=lambda x: x["time"], reverse=True)
                    newest = parsed[0]
                    # 跳过 30 分钟前的旧地震
                    age = (datetime.now(CST) - newest["time"]).total_seconds()
                    if age > 1800:
                        _last_eq = newest
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    # 防重复推送：同地点 60 秒内跳过
                    place_key = newest["place"][:8]
                    last_push = _pushed_cache.get(place_key)
                    if last_push and (datetime.now() - last_push).total_seconds() < 60:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    # 清理超过 5 分钟的旧缓存
                    _pushed_cache = {k: v for k, v in _pushed_cache.items()
                                     if (datetime.now() - v).total_seconds() < 300}
                    # 判断是新事件还是同一事件的更新
                    if _last_eq and _is_same_event(newest, _last_eq):
                        if newest["id"] != _last_eq["id"]:
                            msg = _format_update(newest, _last_eq)
                            _last_eq = newest
                            _pushed_cache[place_key] = datetime.now()
                            await _broadcast(newest, msg)
                    elif not _last_eq or newest["id"] != _last_eq.get("id", ""):
                        # 新地震
                        msg = _format(newest)
                        _last_eq = newest
                        _pushed_cache[place_key] = datetime.now()
                        await _broadcast(newest, msg)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)


async def _broadcast(eq: dict, msg: str):
    if not _subs:
        return
    # 渲染卡片
    png_path = await _render_eq_card(eq)
    from services.sender import send_group_msg
    mag = eq["mag"]
    place = eq.get("place", "")
    sent = 0
    for gid, cfg in list(_subs.items()):
        if mag < cfg.get("min_mag", DEFAULT_MIN_MAG):
            continue
        provs = cfg.get("provinces", [])
        if provs:
            eq_prov = _get_province(place)
            if not eq_prov or eq_prov not in provs:
                continue
        try:
            if png_path:
                cq = f"[img:file:{png_path.replace(chr(92), '/')}]"
            else:
                cq = msg
            await send_group_msg(cq, gid)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(1)
    if sent:
        _eq_info(f"已推送 M{mag:.1f} {place} → {sent}个群")


# ── 命令 ────────────────────────────────────────────────


# ── EQ Web 监视器 ──────────────────────────────────────
_eq_server: asyncio.AbstractServer | None = None

EQ_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>EQ Monitor</title>
<style>
body{background:#0a0a1a;color:#e5e7eb;font:13px monospace;margin:0;padding:10px}
h2{color:#c4b5fd;margin:0 0 8px 0}
.line{padding:2px 0;border-bottom:1px solid #1e1e3a}
.time{color:#6b7280}
.push{color:#f472b6}
.source{color:#4ade80}
</style></head><body>
<h2>地震监视器 · 62001</h2>
<div id="log"></div>
<script>
let ws=new WebSocket('ws://'+location.host+'/ws');
ws.onmessage=e=>{
let d=JSON.parse(e.data);
d.forEach(l=>{
let div=document.createElement('div');
div.className='line';
div.innerHTML='<span class="time">['+l.t+']</span> '+l.m;
document.getElementById('log').prepend(div);
});
while(document.getElementById('log').children.length>200)
document.getElementById('log').lastChild.remove();
};
</script></body></html>"""

async def _start_eq_webserver():
    global _eq_server
    async def handler(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            req = data.decode(errors="ignore")
            if "Upgrade: websocket" in req:
                # WS 握手
                key = ""
                for l in req.split("\r\n"):
                    if l.lower().startswith("sec-websocket-key:"):
                        key = l.split(":", 1)[1].strip()
                if key:
                    import hashlib, base64
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                    await writer.drain()
                    await _eq_ws_handler(reader, writer)
                    return
            # 普通 HTTP → HTML
            body = EQ_HTML.encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    _eq_server = await asyncio.start_server(handler, "0.0.0.0", 62001)
    logger.info("EQ监视器已启动 → http://0.0.0.0:62001")


async def _eq_ws_handler(reader, writer):
    """WebSocket 推送 EQ 日志"""
    idx = len(_eq_logs)
    while True:
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=2)
            if not data:
                break
            op = data[0] & 0x0F
            if op == 8:
                break  # close
        except asyncio.TimeoutError:
            # 推送新日志
            while idx < len(_eq_logs):
                entry = _eq_logs[idx]
                # 简单格式: [HH:MM:SS] msg
                payload = json.dumps([{"t": entry[1:9], "m": entry[12:]}]).encode()
                frame = bytes([0x81, len(payload)]) + payload
                writer.write(frame)
                idx += 1
            await writer.drain()
        except Exception:
            break

async def cmd_eq(args, user_id, group_id, sender_name, is_group, bot_kook):
    """
    .eq .地震
      .eq             最新 1 条 (M≥0)
      .eq m5          5 级以上
      .eq 四川        筛地点
      .eq sub         订阅地震速报 (M≥4.0)
      .eq unsub       取消订阅
      .eq lv 5        设置最低震级 M5.0 (仅订阅推送生效)
      .eq lv          查看当前群最低震级
    """
    global _subs

    if args:
        a0 = args[0].lower()

        # ── 等级设置 ──
        if a0 == "lv":
            if not is_group:
                return "等级设置只能在群聊中使用喵~"
            if group_id not in _subs:
                return "该群还未订阅地震速报喵~ 先 .eq sub"
            if len(args) > 1:
                try:
                    lv = float(args[1])
                    if lv < 0 or lv > 10:
                        return "震级范围 0~10 喵~"
                    _subs[group_id]["min_mag"] = lv
                    save_subs()
                    return f"已设置最低震级 M{lv:.1f} 喵~ 低于此级不推送"
                except ValueError:
                    return "用法: .eq lv <数字>  如 .eq lv 5"
            else:
                lv = _subs[group_id].get("min_mag", DEFAULT_MIN_MAG)
                return f"当前群最低推送震级: M{lv:.1f}"

        # ── 订阅管理 ──
        if a0 == "sub":
            if len(args) > 1 and args[1].lower() == "list":
                if not _subs:
                    return "暂无群订阅地震速报喵~"
                lines = ["地震速报订阅群:"]
                for g, cfg in sorted(_subs.items()):
                    pro = cfg.get("provinces", [])
                    p_text = "、".join(pro) if pro else "全部"
                    lines.append(f"  群{g:>12}  M≥{cfg.get('min_mag',DEFAULT_MIN_MAG):.1f}  {p_text}")
                return "\n".join(lines)
            if not is_group:
                return "订阅只能在群聊中使用喵~"
            provinces = []
            for a in args[1:]:
                if not a.endswith("省"):
                    a += "省"
                provinces.append(a)
            if group_id not in _subs:
                _subs[group_id] = {"min_mag": DEFAULT_MIN_MAG}
            if provinces:
                _subs[group_id]["provinces"] = provinces
                save_subs()
                p_text = "、".join(provinces)
                return f"已订阅 {p_text} 地震速报喵~ (M≥{_subs[group_id].get('min_mag',DEFAULT_MIN_MAG):.1f})"
            # 无省份 = 全国
            _subs[group_id].pop("provinces", None)
            save_subs()
            return f"已订阅全国地震速报喵~ (M≥{_subs[group_id].get('min_mag',DEFAULT_MIN_MAG):.1f})"

        if a0 == "unsub":
            if group_id not in _subs:
                return "该群还未订阅喵~"
            if len(args) > 1:
                # 取消指定省份
                rm = []
                for a in args[1:]:
                    if not a.endswith("省"):
                        a += "省"
                    rm.append(a)
                cur = _subs[group_id].get("provinces", [])
                kept = [p for p in cur if p not in rm]
                removed = [p for p in cur if p in rm]
                if not cur:
                    return "当前订阅全国，无法取消部分省份。用 .eq unsub 取消全部喵~"
                if not removed:
                    return f"未订阅 {', '.join(rm)} 喵~"
                if kept:
                    _subs[group_id]["provinces"] = kept
                    save_subs()
                    return f"已取消 {'、'.join(removed)} 地震订阅喵~"
                else:
                    # 全部取消变全国
                    _subs[group_id].pop("provinces", None)
                    save_subs()
                    return f"已取消 {'、'.join(removed)}，当前无省份限制(全国)喵~"
            else:
                del _subs[group_id]
                save_subs()
                return "已取消地震速报订阅喵~"

        # ── 查询参数 ──
        count = 1
        mag_filter = 0.0
        place_filter = ""
        for a in args:
            al = a.lower()
            if al.startswith("m") and al[1:].replace(".","").isdigit():
                mag_filter = float(al[1:])
            elif a.isdigit():
                count = min(int(a), 10)
            else:
                place_filter = a

    else:
        count, mag_filter, place_filter = 1, 0.0, ""

    data = await _fetch_all_sources()
    if data is None:
        return "[地震] 获取数据失败喵~"

    parsed = [eq for eq in data if eq["mag"] >= mag_filter]
    if place_filter:
        parsed = [eq for eq in parsed if place_filter in eq["place"]]

    if not parsed:
        d = ""
        if mag_filter: d += f" M≥{mag_filter}"
        if place_filter: d += f" \"{place_filter}\""
        return f"[CENC] 没有匹配的地震数据{d}喵~"

    parsed.sort(key=lambda x: x["time"], reverse=True)
    parsed = parsed[:count]

    # 渲染卡片
    eq = parsed[0]
    png_path = await _render_eq_card(eq)
    if png_path:
        # 返回特殊标记，让 commands.py 发图
        return f"__EQ_CARD__:{png_path}"
    return "\n\n".join(_format(eq) for eq in parsed)
