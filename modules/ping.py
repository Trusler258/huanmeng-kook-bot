"""
Globalping 全球延迟测试
- .ping <target>                    默认 中国10节点 + 海外6节点
- .ping <target> CN,US              指定国家代码
- .ping <target> Beijing            城市 / magic location
- .ping <target> tcp                使用 TCP 协议
"""

from __future__ import annotations

import asyncio
import json
import httpx
import uuid
from typing import Optional

from core.logger import get_logger

logger = get_logger("ping")

API = "https://api.globalping.io/v1"

# 默认区域：中国 5 节点 + 海外 6 个知名地点（免费版限制）
DEFAULT_LOCATIONS = [
    {"country": "CN", "limit": 5},
    {"magic": "San Francisco"},
    {"magic": "Tokyo"},
    {"magic": "London"},
    {"magic": "Frankfurt"},
    {"magic": "Singapore"},
    {"magic": "Sydney"},
]


async def _create_measurement(target: str, locations: list[dict]) -> Optional[str]:
    """创建 ping 测量，清理 per-location limit（与顶层互斥）"""
    total_limit = 0
    clean = []
    for loc in locations:
        lc = {k: v for k, v in loc.items()}
        total_limit += lc.pop("limit", 1)
        clean.append(lc)
    body = {
        "type": "ping",
        "target": target,
        "limit": total_limit,
        "locations": clean,
        "measurementOptions": {"packets": 4},
    }
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.post(f"{API}/measurements", json=body)
            if r.status_code == 429:
                logger.warning("Globalping 限流")
                return None
            if r.status_code == 422:
                logger.warning("Globalping 422: body=%s", json.dumps(body, ensure_ascii=False)[:200])
                # 可能太多节点，降级重试
                if total_limit > 10:
                    body["limit"] = 10
                    r = await c.post(f"{API}/measurements", json=body)
            r.raise_for_status()
            return r.json()["id"]
    except Exception as e:
        logger.warning("Globalping 创建失败 [%s]: %s", type(e).__name__, str(e)[:100])
        return None


async def _poll_result(mid: str) -> Optional[dict]:
    """轮询结果（最多 25 秒）"""
    for _ in range(10):
        await asyncio.sleep(2.5)
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as c:
                r = await c.get(f"{API}/measurements/{mid}")
                r.raise_for_status()
                d = r.json()
                if d.get("status") == "finished":
                    # DEBUG: 记录首个 result 的完整数据
                    if d.get("results"):
                        r0 = d["results"][0]
                        logger.info("Globalping 结果样例: probe=%s result=%s",
                                    r0.get("probe"), json.dumps(r0.get("result", {}), ensure_ascii=False)[:200])
                    return d
                if d.get("status") == "failed":
                    # 部分探针超时也有数据可用
                    return d if d.get("results") else None
        except Exception:
            pass
    return None


def _parse_ping(raw: str) -> tuple[list[float], float]:
    """解析 ping rawOutput → (rtt列表, 丢包率%)"""
    import re as _re
    times = [float(m) for m in _re.findall(r"time=([\d.]+)\s*ms", raw)]
    # 严格匹配 "X% packet loss"（X 为整数或小数）
    m = _re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", raw)
    loss = float(m.group(1)) if m else 0.0
    return times, loss


# 节点名中英映射
_CITY_CN = {
    "San Francisco": "旧金山", "Tokyo": "东京", "London": "伦敦",
    "Frankfurt": "法兰克福", "Singapore": "新加坡", "Sydney": "悉尼",
    "Beijing": "北京", "Shanghai": "上海", "Guangzhou": "广州",
    "Shenzhen": "深圳", "Changsha": "长沙", "Hangzhou": "杭州",
    "Chengdu": "成都", "Wuhan": "武汉", "Nanjing": "南京",
    "Hong Kong": "香港", "Seoul": "首尔", "Mumbai": "孟买",
    "Paris": "巴黎", "Amsterdam": "阿姆斯特丹", "Moscow": "莫斯科",
    "New York": "纽约", "Los Angeles": "洛杉矶", "Chicago": "芝加哥",
    "Toronto": "多伦多", "São Paulo": "圣保罗", "Dubai": "迪拜",
}


def _build_probe_row(p: dict, times: list[float], loss: float) -> dict:
    """解析探针数据为一行"""
    probe_id = p.get("id", "")[:10] if p.get("id") else ""
    city = p.get("city", "-")
    return {
        "id": probe_id,
        "country": p.get("country", ""),
        "city": city,
        "city_cn": _CITY_CN.get(city, city),
        "asn": p.get("asn", ""),
        "isp": p.get("network", ""),
        "min": min(times),
        "avg": sum(times) / len(times),
        "max": max(times),
        "loss": loss,
    }


def _format_card(data: dict, target: str, region_hint: str = "") -> str:
    """HTML 卡片 — 分组国内/海外，含探针ID/ASN/ISP"""
    results = data.get("results", [])
    cn, intl = [], []
    for r in results:
        p = r.get("probe", {})
        raw = r.get("result", {}).get("rawOutput", "")
        times, loss = _parse_ping(raw)
        if not times:
            continue
        row = _build_probe_row(p, times, loss)
        (cn if p.get("country") == "CN" else intl).append(row)

    # 排序：同城归位，按城市名
    cn.sort(key=lambda x: x["city"])
    intl.sort(key=lambda x: x["city"])

    def _section(label, rows):
        if not rows:
            return ""
        h = f'<tr><th colspan="6" style="background:rgba(59,130,246,.08);color:#93c5fd">{label} ({len(rows)})</th></tr>'
        body = ""
        for r in rows:
            avg = r["avg"]
            color = "#4ade80" if avg < 100 else ("#facc15" if avg < 250 else "#ef4444")
            body += (
                f'<tr><td style="font-size:10px;color:#64748b">{r["id"]}</td>'
                f'<td>{r["city_cn"]}</td>'
                f'<td style="font-size:10px;color:#9ca3af">AS{r.get("asn","")} {r["isp"]}</td>'
                f'<td style="color:{color};font-weight:600">{r["min"]:.0f}ms</td>'
                f'<td style="color:{color};font-weight:600">{r["avg"]:.0f}ms</td>'
                f'<td>{r["max"]:.0f}ms {r["loss"]:.0f}%</td></tr>'
            )
        return h + body

    ts = data.get("createdAt", "")[:19].replace("T", " ")
    header_extra = f" · {region_hint}" if region_hint else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f172a;color:#e2e8f0;font-family:"Microsoft YaHei","PingFang SC",sans-serif;padding:16px}}
.header{{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;border-radius:10px;padding:12px 16px;margin-bottom:12px}}
.header h1{{font-size:16px}}
.header .sub{{font-size:11px;opacity:.85}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:rgba(59,130,246,.12);color:#93c5fd;text-align:left;padding:4px 6px;font-weight:600}}
td{{padding:3px 6px;border-bottom:1px solid rgba(255,255,255,.03)}}
tr:hover td{{background:rgba(59,130,246,.04)}}
.footer{{text-align:center;font-size:10px;color:#64748b;margin-top:10px}}
</style></head><body>
<div class="header"><h1>🌐 Globalping — {target}{header_extra}</h1><div class="sub">{ts} | {len(results)} 探针</div></div>
<table>{_section("🇨🇳 国内", cn)}{_section("🌍 海外", intl)}</table>
<div class="footer">Globalping (jsDelivr) · 幻梦 QQ Bot</div>
</body></html>"""


def _format_text(data: dict, target: str, region_hint: str = "") -> str:
    """纯文字 — 分组 + 排序"""
    results = data.get("results", [])
    cn, intl = [], []
    for r in results:
        p = r.get("probe", {})
        raw = r.get("result", {}).get("rawOutput", "")
        times, loss = _parse_ping(raw)
        if not times:
            continue
        row = _build_probe_row(p, times, loss)
        (cn if p.get("country") == "CN" else intl).append(row)
    cn.sort(key=lambda x: x["city"])
    intl.sort(key=lambda x: x["city"])

    header = f"🌐 Globalping — {target}"
    if region_hint:
        header += f" ({region_hint})"
    lines = [header, "=" * 42]

    def _add(rows, label):
        if not rows:
            return
        lines.append(f"  {label} ({len(rows)} 节点)")
        lines.append(f"  {'城市':>12}  {'min':>5}  {'avg':>5}  {'max':>5}  {'loss':>5}")
        lines.append("  " + "-" * 32)
        for r in rows:
            lines.append(
                f"  {r['city_cn']:>12}  {r['min']:>4.0f}ms {r['avg']:>4.0f}ms "
                f"{r['max']:>4.0f}ms {r['loss']:>4.0f}%"
            )
    _add(cn, "🇨🇳 国内")
    _add(intl, "🌍 海外")
    return "\n".join(lines)

    """分组输出：国内 CN + 国外"""
    results = data.get("results", [])
    cn_rows, intl_rows = [], []
    for r in results:
        p = r.get("probe", {})
        raw = r.get("result", {}).get("rawOutput", "")
        times, loss = _parse_ping(raw)
        if not times:
            continue
        mn, mx = min(times), max(times)
        avg = sum(times) / len(times)
        city = p.get("city", "-")
        city_cn = _CITY_CN.get(city, city)
        line = f"{city_cn:>12}  {mn:>5.0f}ms  {avg:>5.0f}ms  {mx:>5.0f}ms  {loss:>4.0f}%"
        if p.get("country") == "CN":
            cn_rows.append(line)
        else:
            intl_rows.append(line)

    lines = [f"🌐 Globalping — {target}", "=" * 34]
    if cn_rows:
        lines.append(f"  🇨🇳 国内 ({len(cn_rows)} 节点)")
        lines.append(f"  {'城市':>12}  {'min':>6}  {'avg':>6}  {'max':>6}  {'loss':>5}")
        lines.append("  " + "-" * 30)
        lines.extend(f"  {row}" for row in cn_rows)
    if intl_rows:
        lines.append("")
        lines.append(f"  🌍 海外 ({len(intl_rows)} 节点)")
        lines.append(f"  {'城市':>12}  {'min':>6}  {'avg':>6}  {'max':>6}  {'loss':>5}")
        lines.append("  " + "-" * 30)
        lines.extend(f"  {row}" for row in intl_rows)
    return "\n".join(lines)


# ── 命令入口 ──────────────────────────────────────────────

async def cmd_ping(args, user_id, group_id, sender_name, is_group, bot_kook):
    if not args:
        return "用法: .ping <域名或IP> [国家...|城市] [img]\n.ping google.com\n.ping baidu.com CN,JP\n.ping qq.com tcp\n加 img 输出图片卡片"

    target = args[0]
    use_card = False
    locations = []
    for a in args[1:]:
        al = a.strip(",").upper()
        if al in ("TCP", "ICMP"):
            continue
        if al in ("IMG", "IMAGE", "CARD"):
            use_card = True
            continue
        if len(al) == 2 and al.isalpha():
            locations.append({"country": al})
        else:
            locations.append({"magic": a})
    if not locations:
        locations = DEFAULT_LOCATIONS
        region_hint = "全球节点"
    else:
        region_hint = f"{len(locations)} 个指定地区"

    chat_id = group_id if is_group else user_id
    from services.sender import send_by_chat_type
    await send_by_chat_type(f"正在从{region_hint}测 {target} 的延迟喵~ 等几秒...", chat_id, is_group, user_id)

    mid = await _create_measurement(target, locations)
    if not mid and locations != DEFAULT_LOCATIONS:
        # 用户指定的区域可能无效，回退到默认
        mid = await _create_measurement(target, DEFAULT_LOCATIONS)
    if not mid:
        return f"Globalping 不可用（限流或网络），稍后再试喵~"

    data = await _poll_result(mid)
    if not data:
        return f"[CQ:at,qq={user_id}] 测量超时喵~"

    if use_card:
        from modules.changelog import render_card_to_image
        html = _format_card(data, target, region_hint)
        filename = f"ping_{uuid.uuid4().hex[:8]}.jpg"
        img_path = await render_card_to_image(html, filename, width=500)
        if img_path:
            from services.sender import send_group_msg, send_private_msg
            cq = f"[CQ:image,file=file:///{img_path.replace(chr(92), '/')}]"
            if is_group:
                await send_group_msg(cq, group_id)
            else:
                await send_private_msg(cq, user_id)
            return None
    return _format_text(data, target, region_hint)
