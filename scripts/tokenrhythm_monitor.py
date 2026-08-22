#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基元律动 TokenRhythm —— 余额 / 用量监控（逆向接口，仅供本人账号使用）

已验证接口（全部挂在 `https://tokenrhythm.studio/api` 下，需登录态）：
  GET /auth/me                       当前用户（验证登录态）
  GET /usage/panel?range=today       今日用量明细（costCny / totalTokens / 缓存命中）
  GET /wallet/expiring-credits       额度明细，真实余额 = Σ remainingCny

鉴权方式（二选一）：
  --cookie "完整 Cookie 串"          浏览器 DevTools 复制（含 tr_session）
  --token  "Bearer xxx"              或 Authorization Bearer
也可走环境变量 TR_COOKIE / TR_TOKEN，避免明文写在命令行。

注意：
  - 这是逆向接口，非官方文档；字段名/路径可能随版本变化，脚本内有容错。
  - GET 请求实测无需 CSRF；若日后要 POST（如导出日志）需带 tr_csrf。
  - 用途限于本人账号余额监控。
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://tokenrhythm.studio/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _headers(cookie, token):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        h["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
    if cookie:
        h["Cookie"] = cookie
    return h


def _get(path, cookie, token):
    url = BASE + path
    req = urllib.request.Request(url, headers=_headers(cookie, token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise SystemExit(f"[HTTP {e.code}] 请求失败: {url}\n{body[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"[网络错误] 无法连接 {url}: {e.reason}")
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        raise SystemExit(f"[业务错误] code={payload.get('code')} msg={payload.get('message')}")
    return payload


def get_balance(cookie, token):
    """返回 (真实可用余额元, 额度明细 list)"""
    d = _get("/wallet/expiring-credits?page=1&pageSize=100", cookie, token)["data"]
    lst = d.get("list", [])
    total = sum(float(x.get("remainingCny", 0) or 0) for x in lst)
    return total, lst


def get_usage(cookie, token, rng="today"):
    d = _get(f"/usage/panel?range={rng}&page=1&pageSize=20", cookie, token)["data"]
    return d.get("summary", {}), d.get("total", 0)


def mask(s, head=6, tail=4):
    if not s or len(s) <= head + tail:
        return "***"
    return s[:head] + "…" + s[-tail:]


def cmd_me(cookie, token):
    m = _get("/auth/me", cookie, token)["data"]
    print(f"用户: {m.get('name')}  手机: {m.get('phoneMasked')}  状态: {m.get('status')}  角色: {m.get('role')}")
    return m


def cmd_balance(cookie, token):
    bal, lst = get_balance(cookie, token)
    print(f"真实可用余额: ¥{bal:.4f}  （来自 {len(lst)} 条额度，Σ remainingCny）")
    print("— 剩余最多的额度 —")
    for x in sorted(lst, key=lambda i: -float(i.get("remainingCny", 0) or 0))[:10]:
        print(f"  {str(x.get('sourceLabel','')):<14} 剩余 ¥{float(x.get('remainingCny',0) or 0):.4f}  过期 {str(x.get('expiresAt',''))[:10]}")
    return bal


def cmd_usage(cookie, token, rng):
    s, total = get_usage(cookie, token, rng)
    print(f"[{rng}] 调用 {total} 次 | 花费 ¥{float(s.get('costCny',0) or 0):.4f} | 总成本 ${float(s.get('actualCostUsd',0) or 0):.4f}")
    print(f"  tokens: 总 {s.get('totalTokens')} / 输入 {s.get('inputTokens')} / 输出 {s.get('outputTokens')} / 缓存读 {s.get('cacheReadTokens')}")
    print(f"  节省: ¥{float(s.get('tokenSavingCny',0) or 0):.4f} (${float(s.get('tokenSavingUsd',0) or 0):.4f})")


def cmd_monitor(cookie, token, warn, crit):
    bal, _ = get_balance(cookie, token)
    s, total = get_usage(cookie, token, "today")
    spent = float(s.get("costCny", 0) or 0)
    print(f"余额 ¥{bal:.4f} | 今日花费 ¥{spent:.4f} | 今日调用 {total} 次")
    if bal <= crit:
        print(f"🚨 危险：余额 ≤ ¥{crit:.2f}，建议立即停用 Key")
    elif bal <= warn:
        print(f"⚠️ 警告：余额 ≤ ¥{warn:.2f}，请关注消耗速度")
    else:
        print("✅ 余额充足")


def main():
    ap = argparse.ArgumentParser(description="TokenRhythm 余额/用量监控（逆向接口）")
    ap.add_argument("--cookie", help="浏览器复制的完整 Cookie 串（含 tr_session）")
    ap.add_argument("--token", help="Bearer token（Authorization 头）")
    ap.add_argument("cmd", nargs="?", default="balance",
                    choices=["balance", "usage", "monitor", "me"])
    ap.add_argument("--range", default="today", help="usage 时间范围: today/week/month")
    ap.add_argument("--warn", type=float, default=50.0, help="monitor 警告阈值(元)")
    ap.add_argument("--crit", type=float, default=10.0, help="monitor 危险阈值(元)")
    args = ap.parse_args()

    cookie = args.cookie or os.environ.get("TR_COOKIE")
    token = args.token or os.environ.get("TR_TOKEN")
    if not cookie and not token:
        print("缺少鉴权：请传 --cookie / --token，或设置环境变量 TR_COOKIE / TR_TOKEN")
        sys.exit(2)

    if args.cmd == "me":
        cmd_me(cookie, token)
    elif args.cmd == "balance":
        cmd_balance(cookie, token)
    elif args.cmd == "usage":
        cmd_usage(cookie, token, args.range)
    elif args.cmd == "monitor":
        cmd_monitor(cookie, token, args.warn, args.crit)


if __name__ == "__main__":
    main()
