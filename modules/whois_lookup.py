"""
ICANN RDAP 域名 WHOIS 查询
数据源: https://rdap.org/domain/<域名>（自动重定向到注册局 RDAP 服务器）
零依赖，纯 HTTPS + JSON
"""

from __future__ import annotations

import ssl
import json
import urllib.request
import urllib.parse
import re
from typing import Any

RDAP_BASE = "https://rdap.org/domain/"


def _extract_domain(raw: str) -> str:
    """从 URL/带协议输入中提取裸域名"""
    raw = raw.strip().lower()
    # 去掉协议
    raw = re.sub(r'^https?://', '', raw)
    # 去掉路径/端口
    raw = raw.split('/')[0].split(':')[0]
    # 去掉 www. 前缀
    raw = re.sub(r'^www\.', '', raw)
    return raw.strip()


def _safe_get(d: dict, *keys: str, default: str = "未知") -> str:
    """安全从嵌套 dict 取值"""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return str(d) if d else default


def _format_date(raw: str) -> str:
    """格式化 ISO 日期"""
    if not raw or raw == "未知":
        return "未知"
    try:
        # ISO 8601 → YYYY-MM-DD
        return raw[:10]
    except Exception:
        return raw[:16] if len(raw) >= 10 else raw


def lookup_domain(domain: str) -> str:
    """
    查询域名 WHOIS 信息。
    返回格式化的纯文本字符串。
    """
    domain = _extract_domain(domain)
    if not domain:
        return "请输入有效域名，如 01240820.xyz"
    if '.' not in domain:
        return f"'{domain}' 不是有效域名格式，请包含顶级域（如 .com、.xyz）"

    url = f"{RDAP_BASE}{domain}"

    # 创建忽略 SSL 证书验证的 context（某些 RDAP 服务器证书可能过期）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"域名 {domain} 未注册或 RDAP 无数据"
        return f"查询失败: HTTP {e.code}"
    except Exception as e:
        return f"查询失败: {e}"

    lines = [f"域名: {domain}"]

    # 注册商
    registrar = _safe_get(data, "entities", 0, "vcardArray", 1, 1, "text")
    if registrar and registrar != "未知":
        lines.append(f"注册商: {registrar}")

    # 状态
    statuses = data.get("status", [])
    if statuses:
        lines.append(f"状态: {', '.join(statuses)}")

    # 事件（注册/到期/修改时间）
    events = data.get("events", [])
    for ev in events:
        action = ev.get("eventAction", "")
        date = _format_date(ev.get("eventDate", ""))
        if action == "registration":
            lines.append(f"注册时间: {date}")
        elif action == "expiration":
            lines.append(f"到期时间: {date}")
        elif action == "last changed":
            lines.append(f"最后修改: {date}")

    # NS 服务器
    nameservers = data.get("nameservers", [])
    if nameservers:
        ns_list = []
        for ns in nameservers:
            name = ns.get("ldhName", ns.get("objectClassName", ""))
            if name:
                ns_list.append(name)
        if ns_list:
            lines.append(f"NS: {', '.join(ns_list)}")

    # DNSSEC
    dnssec = _safe_get(data, "secureDNS", "delegationSigned", default="")
    if dnssec and dnssec != "未知":
        signed = "已签名" if dnssec else "未签名"
        lines.append(f"DNSSEC: {signed}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(lookup_domain(sys.argv[1]))
    else:
        print("用法: python whois_lookup.py <域名>")
