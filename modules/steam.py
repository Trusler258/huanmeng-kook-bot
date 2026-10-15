# -*- coding: utf-8 -*-
"""Steam 状态与价格指令

【/~steam 系列】
    price <游戏名|商店链接>   当前价 + 折扣 + 地区对比（配了 ITAD key 还有史低）
    px <游戏名>               快捷版：只给第一条匹配
    who [@某人|SteamID|好友代码|链接]  查 Steam 状态（发卡片图）
                             不给参数=查自己；给 SteamID/好友代码可直接查别人
    bd <SteamID|好友代码|链接> 绑定；bd 看自己；bd del 解绑；bd list 列表
    help                      帮助
【独立入口】
    /~在干嘛 [@某人]          等同 who

数据层在 services/steam_api.py，卡片在 services/steam_card.py。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("steam")

ROOT = Path(__file__).resolve().parent.parent

HELP = "\n".join([
    "【Steam 指令 /~steam】",
    "  price <游戏名|链接>  价格 · 折扣 · 地区对比（+史低）",
    "  px <游戏名>          快捷版，只给第一条匹配",
    "  who [@某人|SteamID|好友代码]  查 Steam 状态；不带参数=查自己",
    "     （SteamID 支持 17 位 SteamID64 / account_id / STEAM_x:y:z / [U:1:x]）",
    "  bd <SteamID|好友代码|链接>    绑定；bd 查看；bd del 解绑；bd list 列表",
    "  help                 本帮助",
    "另一种写法：/~在干嘛 @某人",
])


# ── 工具 ──────────────────────────────────────────────────────

def _qq_of(text: str, group_id: int) -> int:
    """@某人 / 昵称 / QQ号 → QQ（复用棋类指令那套解析，延迟导入避免循环）"""
    try:
        from core.config import get_config
        from modules.commands import _parse_opponent
        return int(_parse_opponent(text, group_id, get_config()) or 0)
    except Exception as e:
        logger.debug("解析 @ 失败: %s", e)
        return 0


async def _notify(text: str, user_id: int, group_id: int, is_group: bool) -> None:
    """先发一条"正在查"的提示（查询可能 5~20 秒，商店接口时快时慢）"""
    try:
        from services.sender import send_group_msg, send_private_msg
        await (send_group_msg(text, group_id) if is_group else send_private_msg(text, user_id))
    except Exception as e:
        logger.debug("提示发送失败: %s", e)


async def _delayed_notify(text: str, user_id: int, group_id: int, is_group: bool,
                          delay: float = 2.0) -> None:
    """延迟发提示：查询快（SWR 命中约 0.5s）时会被调用方 cancel，
    避免「正在查」闪一下就跟着出图 —— 无谓的消息能少一条是一条"""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _notify(text, user_id, group_id, is_group)


async def _send_card(steamid: str, user_id: int, group_id: int, is_group: bool) -> bool:
    """Pillow 直绘卡片并发图。

    不再走 Chromium：服务器 i3-2130 上单张约 900ms、常驻近 400MB；
    现在 ~80ms、内存几十 MB（渲染实现在 services/steam_card_pillow.py）。
    """
    from services import steam_card as SC
    from services.sender import send_group_msg, send_private_msg

    out = ROOT / "data" / "img_temp" / ("steam_%d.png" % int(time.time() * 1000))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        p = await SC.render_card(steamid, out)
        if not p:
            return False
        cq = "[CQ:image,file=file:///%s]" % str(p).replace(chr(92), "/")
        await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
        return True
    except Exception as e:
        logger.warning("Steam 卡片渲染/发送失败: %s", e)
        return False


# ── price / px ────────────────────────────────────────────────

_REGION_CN = {"cn": "中国", "us": "美国", "ar": "阿根廷", "ru": "俄罗斯",
              "tr": "土耳其", "jp": "日本", "hk": "中国香港"}


async def _do_price(term: str, short: bool) -> str:
    from services import steam_api as S

    if not S.has_key():
        return "还没配置 STEAM_KEY 喵~（写在 config/.env 里）"

    appid = S.extract_appid(term)
    matched = ""
    if not appid:
        try:
            hits = await S.search_app(term)
        except Exception as e:
            logger.warning("storesearch 失败: %s", e)
            return "商店搜索失败了（Steam 商店接口时通时断，稍后再试）"
        if not hits:
            return "没搜到「%s」喵~ 换个关键词试试" % term
        appid = hits[0]["appid"]
        matched = hits[0].get("name") or ""
        more = len(hits) - 1

    try:
        p = await S.app_price(appid, "cn")
    except Exception as e:
        logger.warning("appdetails 失败: %s", e)
        return "拿不到价格（Steam 商店接口不通，稍后再试）"

    name = p.get("name") or matched or ("appid " + str(appid))
    lines = ["【%s】" % name]

    if p.get("is_free"):
        lines.append("  免费游戏")
    elif p.get("coming_soon"):
        lines.append("  尚未发售（%s）" % (p.get("release") or "时间未定"))
    elif p.get("final_fmt"):
        if p.get("discount"):
            lines.append("  当前价  %s（-%d%%，原价 %s）"
                         % (p["final_fmt"], p["discount"], p["initial_fmt"]))
        else:
            lines.append("  当前价  %s（暂无折扣）" % p["final_fmt"])
    else:
        lines.append("  没有价格信息")

    # 史低（ITAD，可选）
    if not short:
        try:
            low = await S.history_low(appid, "cn")
        except Exception:
            low = {}
        if low.get("low") is not None:
            lines.append("  史低    %s %s" % (low["low"], low.get("currency", "")))
        elif not S.itad_key():
            lines.append("  史低    （未配 ITAD_KEY，见 /~steam help）")

        # 自记价格：比我们记录过的还低就提示
        if p.get("final"):
            try:
                prev = S.record_price(appid, p["final"], p.get("currency", ""))
                if prev:
                    lines.append("  注意：比我们记过的最低价（%s 分）还低" % prev)
            except Exception:
                pass

    if not short:
        regions = await S.price_multi_region(appid, ("cn", "us", "ar", "ru"))
        if regions:
            lines.append("")
            lines.append("  地区对比")
            for r in regions:
                label = _REGION_CN.get(r["cc"], r["cc"].upper())
                price = "免费" if r.get("is_free") else (r.get("final_fmt") or "-")
                if r.get("discount"):
                    price += "（-%d%%）" % r["discount"]
                lines.append("    %-6s %s" % (label, price))

    lines.append("")
    lines.append("  store.steampowered.com/app/%s" % appid)
    if matched and more > 0 and short:
        lines.append("  （另有 %d 条相似结果，用 price 看更多）" % more)
    return "\n".join(lines)


# ── who / bd ──────────────────────────────────────────────────

async def _do_who(args, user_id, group_id, is_group, sender_name) -> str | None:
    from services import steam_api as S
    from services import steam_card as SC

    raw = " ".join(args).strip() if args else ""

    if not S.has_key():
        return "还没配置 STEAM_KEY 喵~"

    # ── 参数只有两路（v2.3.69）────────────────────────────────
    #   ① 带 @       → 当 QQ 查绑定
    #   ② 其余       → 当 Steam 标识**直接查**（SteamID64 / account_id / 好友代码 / ID2/3 / 链接）
    #   ⚠️ 为什么不再"先看能不能抠出数字当 QQ"：_parse_opponent 第一句是
    #      re.search(r"(\d{4,12})", s)，任何 4~12 位数字都会被当 QQ 号，于是
    #      1467315295 被当成 QQ、76561199427581023 被抠出前 12 位当 QQ ——
    #      **数字形式的 Steam 标识会全部失效**，用户只会看到「还没绑定」。
    #      用户明确要求：带 @ 的才识别为 QQ。
    direct = ""
    target = 0
    if raw:
        if "@" in raw or "[CQ:at" in raw or "(met)" in raw:
            target = _qq_of(raw, group_id)
            if not target:
                return ("没认出你 @ 的是谁喵~\n"
                        "  （也可以直接给 SteamID / 好友代码 / 资料链接）")
        else:
            try:
                direct, err = await S.resolve_steamid(raw)
            except Exception as e:
                logger.warning("解析 Steam 账号失败: %s", e)
                return "解析失败（网络问题），稍后再试"
            if not direct:
                return err or "认不出这是账号喵~"

    if direct:
        steamid = direct
        who = raw
    else:
        if not target:
            target = user_id
            who = "你"
        else:
            who = ("<@%d>" % target) if target != user_id else "你"
        steamid = S.get_bind(target)
        if not steamid:
            if target == user_id:
                return ("你还没绑定 Steam 喵~\n"
                        "用 /~steam bd <SteamID64 / 好友代码 / 个人资料链接> 绑一下\n"
                        "也可以不绑定直接查：/~steam who <SteamID / 好友代码 / 资料链接>"
                        "（不用加 @）")
            return "%s 还没绑定 Steam 喵~（让他用 /~steam bd 绑一下）" % who

    # ★ v2.3.61 文案修正：旧写法 `"正在查 %s 的 Steam…" % ("你的" if ...)` 拼出
    #   「正在查 你的 的 Steam…」。上面 182 行的 who 本来就是现成称呼（"你"/<@QQ>）。
    #   同时改为延迟 2s 才发：SWR 命中时 0.5s 内就出图，提示会被 cancel 不发送。
    tip = ("正在查你的 Steam…" if (not direct and target == user_id)
           else "正在查 %s 的 Steam…" % who)
    tip_task = asyncio.ensure_future(
        _delayed_notify(tip, user_id, group_id, is_group, delay=2.0))
    try:
        ok = await _send_card(steamid, user_id, group_id, is_group)
    finally:
        tip_task.cancel()
    if ok:
        return None
    # 渲染失败时区分"资料不公开"和"渲染出错"
    try:
        prof = await S.player_summary(steamid)
    except Exception:
        prof = {}
    if not prof:
        return "查不到这个账号，或者接口不通喵~"
    return "卡片渲染失败了喵~（详情看服务器日志）"


async def _do_bind(args, user_id, group_id, is_group) -> str:
    from services import steam_api as S

    if not args:
        sid = S.get_bind(user_id)
        if not sid:
            return ("你还没绑定 Steam 喵~\n"
                    "  /~steam bd <SteamID64 / 好友代码 / account_id / 资料链接>\n"
                    "  例：76561199427581023 · hkkhkghw · 1467315295\n"
                    "（不要用昵称，Steam 上重名的太多）")
        try:
            p = await S.player_summary(sid)
        except Exception:
            p = {}
        code = S.account_id_to_friend_code(S.steamid64_to_account_id(sid))
        extra = ("\n  好友代码：%s（可直接分享给别人加好友）" % code) if code else ""
        return ("你绑定的账号：%s\n  %s%s\n"
                "  换绑就再发一次 /~steam bd <新ID>\n  解绑：/~steam bd del"
                % (p.get("name") or "（查不到）", sid, extra))

    sub = args[0].lower()
    if sub in ("del", "解绑", "取消", "unbind"):
        return "已经解绑了喵~" if S.del_bind(user_id) else "你本来就没绑定过"
    if sub in ("list", "列表"):
        m = S._load_bind()
        if not m:
            return "还没有人绑定 Steam 喵~"
        lines = ["已绑定 Steam 的成员（%d 人）：" % len(m)]
        for qq, sid in list(m.items())[:20]:
            lines.append("  %s  →  %s" % (qq, sid))
        return "\n".join(lines)

    text = " ".join(args)
    try:
        sid, err = await S.resolve_steamid(text)
    except Exception as e:
        logger.warning("解析 SteamID 失败: %s", e)
        return "解析失败（网络问题），稍后再试"
    if not sid:
        return "绑定失败：%s" % (err or "认不出这个账号")

    try:
        p = await S.player_summary(sid)
    except Exception:
        p = {}
    S.set_bind(user_id, sid)
    name = p.get("name") or "（昵称查不到，可能资料私密）"
    tip = ""
    # 按自定义 URL 解析的容易认错人（vanity 与昵称不是一回事，重名/抢注很常见）→ 提醒核对
    if not S.extract_identity(text)[0]:   # 认得出本地标识就别误报警
        tip = ("\n（这是按自定义 URL 查到的账号，请核对上面的昵称是不是你）\n"
               "  对不上就用 SteamID64 重绑：个人资料页链接里那 17 位数字")
    code = S.account_id_to_friend_code(S.steamid64_to_account_id(sid))
    if code:
        tip += "\n  你的好友代码：%s" % code
    return "绑定成功喵~\n  %s\n  %s%s" % (name, sid, tip)


# ── 入口 ──────────────────────────────────────────────────────

async def cmd_steam(args, user_id, group_id, sender_name, is_group, bot_qq):
    """/~steam <子命令>"""
    if not args:
        return HELP
    action = args[0].lower()
    rest = args[1:]

    if action in ("help", "帮助", "?", "？"):
        return HELP
    if action in ("price", "价格", "p"):
        if not rest:
            return "用法：/~steam price <游戏名 或 商店链接>"
        return await _do_price(" ".join(rest), short=False)
    if action in ("px", "快捷"):
        if not rest:
            return "用法：/~steam px <游戏名>"
        return await _do_price(" ".join(rest), short=True)
    if action in ("me", "自己", "我的"):
        # /~steam me —— 看自己（等同 who 不带参数，跟 /~wdsj me 的习惯一致）
        return await _do_who([], user_id, group_id, is_group, sender_name)
    if action in ("who", "谁", "状态"):
        return await _do_who(rest, user_id, group_id, is_group, sender_name)
    if action in ("bd", "bind", "绑定"):
        return await _do_bind(rest, user_id, group_id, is_group)
    # 没给子命令时，把第一个词当游戏名（/~steam hades）
    return await _do_price(" ".join(args), short=False)


async def cmd_steam_doing(args, user_id, group_id, sender_name, is_group, bot_qq):
    """/~在干嘛 [@某人] —— 等同 /~steam who"""
    return await _do_who(args, user_id, group_id, is_group, sender_name)
