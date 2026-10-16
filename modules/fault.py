"""DeepSeek 故障监控（对齐 QQ 的故障推送，频道语义）。

- 数据源：status.deepseek.com 官方 feed（运行时直读，不手抄状态）
- 国内访问三重阻断（ICP 403 / SNI 重置 / DoH 超时）→ 国外 DNS（UDP 53）拿香港节点直连
- `.owner fault <show|on|off|test>` 手动触发 + 按频道开关
- 后台轮询：`fault_watcher_loop()`（bg_tasks 插件托管），一有更新就推给已开启的频道
"""
from __future__ import annotations

import re
from pathlib import Path

from core.logger import get_logger
from utils.format_lang import format_lang

logger = get_logger("fault")

_ROOT = Path(__file__).resolve().parent.parent
_CFG_PATH = _ROOT / "config" / "bot_config.toml"

# ── 配置读写（只动目标行，保留注释）──────────────────────────

def _toml_write_in_section(path: Path, section: str, key: str,
                           value_literal: str | None) -> bool:
    """在 toml 的 `[section]` 里设置/删除一个键，**只动目标行**，保留其余内容与注释。

    ⚠️ 不要用 `toml.dumps()` 整文件重写——那会把所有注释和排版全丢掉。
    value_literal=None 表示删除该键。返回是否写入了文件。
    """
    lines = path.read_text(encoding="utf-8").split("\n") if path.exists() else []
    hdr = f"[{section}]"
    sec_start = next((i for i, ln in enumerate(lines) if ln.strip() == hdr), None)

    if sec_start is None:
        if value_literal is None:
            return False
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", hdr, f"{key} = {value_literal}"]
    else:
        sec_end = len(lines)
        for j in range(sec_start + 1, len(lines)):
            s = lines[j].strip()
            if s.startswith("[") and s.endswith("]"):
                sec_end = j
                break
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        hit = next((j for j in range(sec_start + 1, sec_end) if pat.match(lines[j])), None)
        if value_literal is None:
            if hit is None:
                return False
            del lines[hit]
        elif hit is not None:
            lines[hit] = f"{key} = {value_literal}"
        else:
            lines.insert(sec_start + 1, f"{key} = {value_literal}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def get_notify_channels() -> list:
    """读取 [fault] notify_channels（已开启故障推送的频道 ID 列表）。"""
    try:
        import toml as _toml
        d = _toml.load(_CFG_PATH) if _CFG_PATH.exists() else {}
    except Exception:
        return []
    return [int(c) for c in (d.get("fault", {}).get("notify_channels") or [])
            if str(c).strip().lstrip("-").isdigit()]


def set_notify_channel(channel_id: int, on: bool) -> bool:
    """开启/关闭某频道的故障推送，返回是否写入了配置。"""
    gs = get_notify_channels()
    if on:
        if channel_id in gs:
            return False
        gs.append(channel_id)
    else:
        if channel_id not in gs:
            return False
        gs = [x for x in gs if x != channel_id]
    return _toml_write_in_section(_CFG_PATH, "fault", "notify_channels",
                                  "[" + ", ".join(str(x) for x in gs) + "]" if gs else None)


def _fault_cfg(key, default):
    try:
        import toml as _toml
        return _toml.load(_CFG_PATH).get("fault", {}).get(key, default)
    except Exception:
        return default


# ── DeepSeek 状态 feed ──────────────────────────────────────

_FAULT_DOT = {"red": "🔴", "yellow": "🟡", "green": "🟢"}

# 当前这轮 DeepSeek 故障的实况（**兜底用**：`fault test` 正常时直读官方 feed，
# 拉取失败/超时才落到这份手抄快照，2026-10-15 09:2x 更新）。
_FAULT_DEFAULT = {
    "started": "2026-10-15 08:11",
    "status": format_lang("fault.default_status"),
    "components": format_lang("fault.default_components"),
    "symptom": format_lang("fault.default_symptom"),
}

_DS_FEED_URL = "https://status.deepseek.com/feed.rss"

# 国内访问 status.deepseek.com 的三重阻断（2026-10-15 实测）：
#   ① DNS 被分线路指到国内节点 → 该站没 ICP 备案，HTTP 明文直接返回
#      "Non-compliance ICP Filing" 403 页
#   ② HTTPS 在 TLS 握手时被 RST（SNI 阻断）
#   ③ DoH（1.1.1.1:443）也超时
# 但 **UDP 53 → 1.1.1.1 是通的**：用国外 DNS 拿到 statuspage.flashduty.com 的
# **香港** NLB 节点，对该 IP 直连（SNI/Host 仍用真域名）→ HTTP 200。
_DS_FEED_DNS = "1.1.1.1"


def _dns_a(name: str, server: str = _DS_FEED_DNS, timeout: float = 5.0) -> list:
    """极简 DNS A 记录查询（UDP 53，纯 stdlib，不依赖 dig/dnspython）。"""
    import random as _rd
    import socket as _s
    import struct as _st

    tid = _rd.randint(0, 65535)
    q = _st.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for lab in name.split("."):
        q += bytes([len(lab)]) + lab.encode()
    q += b"\x00" + _st.pack(">HH", 1, 1)
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()

    ancount = _st.unpack(">H", data[6:8])[0]
    i = 12
    while data[i] != 0:          # 跳过 question 的域名
        i += data[i] + 1
    i += 5                        # \0 + type + class
    out = []
    for _ in range(ancount):
        if data[i] & 0xC0:        # 压缩指针
            i += 2
        else:
            while data[i] != 0:
                i += data[i] + 1
            i += 1
        rtype, _, _, rdlen = _st.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in data[i:i + 4]))
        i += rdlen
    return out


def _fetch_feed_via_ip(ip: str, path: str, timeout: float = 10.0) -> str:
    """对指定 IP 直连拉 feed：SNI/Host 用真域名（证书校验正常），绕开国内 DNS 污染。"""
    import http.client as _hc
    import socket as _s
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    raw = _s.create_connection((ip, 443), timeout=timeout)
    tls = ctx.wrap_socket(raw, server_hostname="status.deepseek.com")
    conn = _hc.HTTPSConnection("status.deepseek.com", 443, timeout=timeout)
    conn.sock = tls
    try:
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0",
                                           "Accept": "*/*"})
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return body.decode("utf-8", "ignore")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_ds_feed(xml: str) -> dict:
    """从 feed XML 里解析最新一条。"""
    import html as _html

    m = re.search(r"<item>(.*?)</item>|<entry>(.*?)</entry>", xml, re.S)
    if not m:
        raise ValueError(format_lang("fault.parse_no_item"))
    block = m.group(1) or m.group(2)

    def _tag(name: str) -> str:
        mm = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
        return _html.unescape(mm.group(1)).strip() if mm else ""

    title = _tag("title")
    desc = _tag("description") or _tag("summary") or _tag("content")
    link = _tag("link")
    pub = _tag("pubDate") or _tag("updated") or _tag("published")

    # ⚠️ description 是 HTML 转义的：先解实体，再剥标签。
    #    剥标签必须用**空格**而不是 \n —— `<strong>Status:</strong> identified`
    #    若换成 \n 会把 "Status:" 和它的值拆到两行，status_word 就抓不到（已踩过）。
    text = _html.unescape(desc)
    text = re.sub(r"</p\s*>|<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]

    status_word, components, status_text = "", "", ""
    for l in lines:
        mm = re.match(r"Status:\s*\**\s*([A-Za-z]+)", l)
        if mm and not status_word:
            status_word = mm.group(1).lower()
        mm = re.match(r"Affected components:\s*(.+)", l)
        if mm and not components:
            components = mm.group(1).strip().replace(", ", "、")
        if (not status_text and not l.startswith("Status:")
                and not l.startswith("Affected components:")
                and re.search(r"[\u4e00-\u9fff]", l)):
            status_text = l.rstrip("。")

    started = ""
    if pub:
        import email.utils as _eu
        from datetime import timedelta as _td, timezone as _tz
        try:
            started = _eu.parsedate_to_datetime(pub).astimezone(
                _tz(_td(hours=8))).strftime("%Y-%m-%d %H:%M")
        except Exception:
            started = ""

    return {"title": title, "status_word": status_word, "status_text": status_text,
            "components": components, "started": started, "link": link}


_DS_STATUS_LEVEL = {
    # feed 里的 Status 词 → (圆圈, 是否已恢复)
    "resolved": ("green", True),
    "monitoring": ("yellow", False),
    "identified": ("red", False),
    "investigating": ("red", False),
}


async def _fetch_deepseek_status(timeout: float = 8.0):
    """拉 DeepSeek 状态页 feed，解析最新一条（**不手抄，运行时读官方 feed**）。

    返回 dict: {title, status_word, status_text, components, started, link}；
    **失败返回 None**（调用方回退 _FAULT_DEFAULT，fail-open）。
    """
    import asyncio as _aio

    def _get() -> str:
        last = None
        for path in ("/feed.rss", "/feed.atom"):
            for _attempt in range(2):
                ips = _dns_a("status.deepseek.com", _DS_FEED_DNS, timeout=timeout)
                if not ips:
                    raise RuntimeError(format_lang("fault.dns_fail"))
                for ip in ips:
                    try:
                        return _fetch_feed_via_ip(ip, path, timeout=timeout)
                    except Exception as e:
                        last = RuntimeError(f"{type(e).__name__}: {e} [{ip} {path}]")
        raise last or RuntimeError(format_lang("fault.all_nodes_fail"))

    try:
        xml = await _aio.wait_for(_aio.to_thread(_get), timeout=timeout * 2 + 4)
    except Exception as e:
        logger.warning("拉取 DeepSeek 状态 feed 失败（回退内置默认）: %s", e)
        return None

    try:
        return _parse_ds_feed(xml)
    except Exception as e:
        logger.warning("解析 DeepSeek 状态 feed 失败（回退内置默认）: %s", e)
        return None


def _build_fault_notice(level: str = "red", *, started: str = "", status: str = "",
                        components: str = "", symptom: str = "",
                        recovered: bool = False) -> str:
    """拼一条故障推送文本。

    level: red=不可用 / yellow=降级；recovered=True 时强制 green。
    全文不用句号（用户 2026-10-15 要求）。
    """
    dot = "🟢" if recovered else _FAULT_DOT.get(level, "🔴")
    head = format_lang("fault.notice_recovered") if recovered \
        else format_lang("fault.notice_down")
    lines = [format_lang("fault.notice_head", dot=dot), "", head,
             format_lang("fault.notice_ok"), ""]
    if started:
        lines.append(format_lang("fault.notice_started", started=started))
    if status:
        lines.append(format_lang("fault.notice_status", status=status))
    if components:
        lines.append(format_lang("fault.notice_components", components=components))
    # 恢复通知里不写「现象」——那是描述故障期的
    if symptom and not recovered:
        lines.append(format_lang("fault.notice_symptom", symptom=symptom))
    lines += ["", format_lang("fault.notice_thanks") if recovered else format_lang("fault.notice_recover_hint"),
              format_lang("fault.notice_detail")]
    return "\n".join(lines)


async def send_enabled_card(channel_id: int):
    """开启故障推送后，在当前频道发一张 KOOK 原生卡片确认。"""
    import json as _json
    from services.sender import send_group_msg

    card = [{"type": "card", "theme": "success", "size": "lg", "modules": [
        {"type": "header", "text": {"type": "plain-text",
                                    "content": format_lang("fault.enabled_title")}},
        {"type": "context", "elements": [{"type": "kmarkdown",
                                          "content": format_lang("fault.enabled_desc")}]},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "kmarkdown",
                                          "content": format_lang("fault.enabled_hint")}]},
    ]}]
    msg = f"[CARD]{_json.dumps(card, ensure_ascii=False)}[/CARD]"
    await send_group_msg(msg, channel_id)


async def run_fault_test(channel_id: int) -> bool:
    """拉一次官方状态并发测试通知到指定频道，返回是否发送成功。"""
    from services.sender import send_group_msg
    info = await _fetch_deepseek_status()
    if info:
        lvl, recovered = _DS_STATUS_LEVEL.get(info["status_word"], ("red", False))
        txt = _build_fault_notice(
            lvl, started=info["started"], status=info["status_text"],
            components=info["components"], symptom=_FAULT_DEFAULT["symptom"],
            recovered=recovered)
    else:
        txt = _build_fault_notice("red", **_FAULT_DEFAULT)
    await send_group_msg(txt, channel_id)
    return True


# ── 后台轮询 ────────────────────────────────────────────────

async def fault_watcher_loop():
    """DeepSeek 状态 feed 轮询：一有更新就推给已开启故障推送的频道。

    - 轮询间隔 bot_config.toml [fault] poll_interval（秒，默认 300）
    - 判据：feed 的 <link> + status_word（同一事故链接不变，但状态会换）
    - 状态存 data/fault_watcher.json；**首次轮询只记基线不发**（防重启重发旧状态）
    - notify_channels 为空时只记录、不推
    """
    import asyncio
    import json as _json
    from services.sender import send_group_msg

    state_file = _ROOT / "data" / "fault_watcher.json"

    def _load() -> dict:
        try:
            return _json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(d: dict):
        state_file.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")

    st = _load()
    logger.info("故障监控已启动 | 轮询 %ss | 推送频道 %s",
                _fault_cfg("poll_interval", 300), get_notify_channels() or "无")

    while True:
        try:
            info = await _fetch_deepseek_status()
            if info and info.get("link"):
                key = f"{info['link']}|{info.get('status_word', '')}"
                if not st.get("last_key"):
                    st = {"last_key": key, "status_word": info.get("status_word", "")}
                    _save(st)
                    logger.info("故障监控: 建立基线 %s", key[:70])
                elif key != st.get("last_key"):
                    lvl, recovered = _DS_STATUS_LEVEL.get(
                        info.get("status_word", ""), ("red", False))
                    txt = _build_fault_notice(
                        lvl, started=info.get("started", ""),
                        status=info.get("status_text", ""),
                        components=info.get("components", ""),
                        symptom=_FAULT_DEFAULT["symptom"],
                        recovered=recovered)
                    cs = get_notify_channels()
                    if cs:
                        for c in cs:
                            try:
                                await send_group_msg(txt, c)
                            except Exception as e:
                                logger.warning("故障推送发到频道 %s 失败: %s", c, e)
                        logger.info("故障状态更新已推送 → 频道 %s（%s）", cs,
                                    info.get("status_text", "")[:30])
                    else:
                        logger.info("故障状态有更新但未开启推送频道: %s",
                                    info.get("status_text", "")[:30])
                    st = {"last_key": key, "status_word": info.get("status_word", "")}
                    _save(st)
        except Exception as e:
            logger.warning("故障监控轮询失败: %s", e)
        await asyncio.sleep(max(60, int(_fault_cfg("poll_interval", 300))))
