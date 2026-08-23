"""
指令系统（重构版）
- 所有指令处理函数集中管理
- 统一签名: async def cmd_xxx(args, user_id, group_id, sender_name, is_group, bot_qq) -> str | None
- 全量 i18n + 详细日志
- 指令注册表 COMMAND_MAP
- 分发器 handle_command
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import string
import time
from datetime import date
from pathlib import Path

import httpx

from core.logger import get_logger
from core.config import get_config, reload_config, load_roles_config, save_roles_config
from utils.format_lang import format_lang
from modules.fav import get_all_fav, FAV_FILE as FAV_FILE_REF, reset_all_fav
from services.sender import send_group_msg, send_private_msg, send_raw_group, send_raw_user
from modules.search import perform_search
from modules.earthquake import cmd_eq
from modules.nasa import cmd_nasa
from modules.agnes import cmd_draw, cmd_video, cmd_img2video, owner_quota_get, owner_quota_set, owner_quota_reset
from modules.voice import cmd_voice
try:
    from modules.ping import cmd_ping
except ImportError:
    async def cmd_ping(args, user_id, group_id, sender_name, is_group, bot_qq):
        return "ping 模块未安装喵~"
try:
    from modules.op import cmd_op, cmd_persona, cmd_master, cmd_sleep, cmd_hanxu
except ImportError:
    cmd_op = cmd_persona = cmd_master = cmd_sleep = cmd_hanxu = None
try:
    from modules.weather import query_weather, build_weather_report
except ImportError:
    query_weather = build_weather_report = None

from modules.changelog import send_changelog_card, send_weather_card, send_box_card
from modules.whois_lookup import lookup_domain   # ★ WHOIS 域名查询

from core.token_tracker import cmd_cost, cmd_tokens

logger = get_logger("commands")


async def _cmd_gh(args, user_id, group_id, sender_name, is_group, bot_qq):
    try:
        from modules.gh import cmd_gh
    except ImportError:
        return "公会登记模块未安装喵~"
    return await cmd_gh(args, user_id, group_id, sender_name, is_group, bot_qq)


async def _cmd_update(args, user_id, group_id, sender_name, is_group, bot_qq):
    from modules.auto_update import cmd_update
    return await cmd_update(args, user_id, group_id, sender_name, is_group, bot_qq)


# ════════════════════════════════════════════════════════════
#  指令处理函数
# ════════════════════════════════════════════════════════════

async def cmd_help(args, user_id, group_id, sender_name, is_group, bot_qq):
    """显示帮助列表 — .help 发送预渲染图片卡片，.help <指令> 查看详细用法"""
    cfg = get_config()
    name = cfg.bot_name
    logger.info("指令 .help 触发 user=%d args=%s", user_id, args)

    # ── 详细帮助：.help <关键词> ──
    if args:
        keyword = args[0].lower().lstrip(".")
        detail = format_lang(f"help.detail.{keyword}", default=None)
        if detail:
            return detail
        return format_lang("help.not_found", keyword=keyword)

    # ── 完整帮助：发送预渲染指令卡片图片 ──
    card_path = Path(__file__).resolve().parent.parent / "data" / "help_card.png"
    if card_path.exists():
        try:
            from services.sender import send_by_chat_type
            cq = f"[img:file:{card_path.as_posix()}]"
            chat_id = group_id if is_group else user_id
            await send_by_chat_type(cq, chat_id, is_group, user_id if not is_group else None)
            return None  # 已发送
        except Exception as e:
            logger.warning("帮助卡片发送失败，降级卡片消息: %s", e)

    # ── 降级：用 KOOK 卡片消息发送 ──
    logger.info("help_card.png 不存在，降级为 KOOK 卡片消息")
    is_admin = cfg.is_admin(user_id, group_id)
    from utils.format_lang import get_lang_data
    sections = get_lang_data().get("help", {}).get("sections", [])
    if isinstance(sections, str) or not sections:
        sections = [
            ["--- 聊天 ---", f"@{name} <内容>     跟我说话"],
            ["--- 工具 ---", ".s <关键词>        联网搜索", ".remind <时间> <事> 定时提醒", ".抽 <A> <B>        随机选择"],
            ["--- 数据 ---", ".favlist           好感度"],
            ["--- 系统 ---", ".cost              Token消耗统计", ".info             运行状态", ".up [版本]         更新日志", ".ping <目标>       全球延迟测试"],
            ["--- 主人专用 ---", ".owner ...        配置管理", ".memory ...       记忆查询", ".preset ...        提示词注入管理", ".reload           热重载配置"],
        ]

    modules = [{"type": "header", "text": {"type": "plain-text", "content": f"📖 {name} 指令帮助"}}]
    modules.append({"type": "context", "elements": [{"type": "kmarkdown", "content": ".help <指令> 查看详细用法，例如: .help wzq"}]})

    for section in sections:
        if not section:
            continue
        label = section[0]
        if "主人" in label and not is_admin:
            continue
        modules.append({"type": "divider"})
        modules.append({"type": "section", "text": {"type": "kmarkdown", "content": f"**{label}**"}})
        for cmd in section[1:]:
            modules.append({"type": "context", "elements": [{"type": "kmarkdown", "content": f"`{cmd}`"}]})

    modules.append({"type": "divider"})
    modules.append({"type": "context", "elements": [{"type": "kmarkdown", "content": f"Powered by {name} | 输入 .help <指令> 查看详情"}]})

    import json as _json
    card_json = _json.dumps([{"type": "card", "theme": "secondary", "size": "lg", "modules": modules}], ensure_ascii=False)
    card_msg = f"[CARD]{card_json}[/CARD]"

    from services.sender import send_by_chat_type
    chat_id = group_id if is_group else user_id
    await send_by_chat_type(card_msg, chat_id, is_group, user_id if not is_group else None)
    return None


# ════════════════════════════════════════════════════════════
#  详细帮助数据
# ════════════════════════════════════════════════════════════

_HELP_DETAIL = {
    "nasa": "【NASA 每日天文图 .nasa】\n"
          "  nasa [日期]  查看 NASA 每日天文图片，如 .nasa 2025-06-01\n",
    "pgr": "【Phigros 查询 .pgr】\n"
          "  pgr login / me / top / song / new\n",
    "wzq": "【五子棋 .wzq】\n"
           "  duel @某人    发起挑战\n"
            "  accept        接受挑战\n"
           "  decline       拒绝挑战\n"
           "  cancel        取消未开始的挑战\n"
           "  <坐标>        落子 (H8 / 8,8)\n"
           "  ai <难度> [nofb] 人机对战 (可选无禁手)\n"
           "  board         查看棋盘\n"
           "  surrender     认输\n"
           "  undo          悔棋 (对手再发一次确认)\n"
           "  status        对局信息\n"
           "  history [N]   历史战绩\n"
           "  history <编号> board 查看对局棋盘",

    "remind": "【提醒 .remind .提醒】\n"
              "  .remind 30分钟后 吃饭\n"
              "  .remind 2小时后 开会\n"
              "  .remind 明天14:30 午休\n"
              "  .remind 14:30 下班\n"
              "  到期自动 @ 提醒",

    "chou": "【抽签 .抽】\n"
            "  .抽 吃面 吃饭 打游戏   空格分隔\n"
            "  .抽 选项A,选项B,选项C   逗号分隔\n"
            "  随机选一个 + 猫娘点评",

    "s": "【搜索 .s】\n"
         "  .s Python asyncio 用法\n"
         "  自动判断是否需要搜索 + DuckDuckGo",

    "favlist": "【好感度 .favlist】\n"
               "  查看当前群/私聊的好感度排行\n"
               "  从高到低，显示昵称",

    "luck": "【运气 .luck】\n"
            "  获取今日运气值 (1~100)\n"
            "  0.1% 概率抽到 1000\n"
            "  同一个人当天内结果不变",

    "up": "【更新日志 .up .updateinfo】\n"
          "  .up            最新版本\n"
          "  .up 0.9.4      指定版本\n"
          "  .up all        完整日志\n"
          "  生成精美 HTML 卡片",

    "updateinfo": "【更新日志 .up .updateinfo】\n"
                  "  .up            最新版本\n"
                  "  .up 0.9.4      指定版本\n"
                  "  .up all        完整日志",

    "info": "【系统信息 .info】\n"
            "  显示运行状态: 系统/运行时间/内存/磁盘/CPU\n"
            "  连接状态/消息统计/模型列表/配置摘要",

    "ping": "【在线 .ping】\n"
            "  测试 bot 是否在线\n"
            "  随机猫娘卖萌回复，@ 触发者",

    "owner": "【配置管理 .owner (主人)】\n"
             "  list <bot|adapter|roles>   列所有配置\n"
             "  get <路径>      读取: get bot.reply_threshold\n"
             "  set <路径> <值>  修改: set bot.reply_threshold 5\n"
             "  data get <名> [键] 读数据\n"
             "  data set <名> <键> <值> 改数据\n"
             "  data reset <名|all> 清空\n"
             "  wl show/add/remove  白名单快捷操作\n"
             "  luck list/set/del   运气管理",

    "preset": "【提示词注入 .preset (主人)】\n"
              "  .preset        查看当前注入\n"
              "  .preset clear  清除注入\n"
              "  或在消息中用 {{提示词}} 注入, {{reset}} 清除",

    "reload": "【重载 .reload (主人)】\n"
              "  热重载所有配置，不重启进程\n"
              "  也可用 systemctl reload robot",

    "tr": "【翻译 .tr .翻译】\n"
          "  .tr en 你好           中→英\n"
          "  .tr 中文 hello        英→中\n"
          "  .tr jp こんにちは     →日文\n"
          "  支持 en/zh/jp/kr/fr/de\n"
          "  也支持 中文/英文/日文/韩文/法文/德文",

    "translate": "【翻译 .tr .翻译】\n"
                 "  .tr en 你好\n"
                 "  .tr 中文 hello\n"
                 "  支持 6 种语言",

    "countdown": "【倒计时 .countdown .倒计时】\n"
                 "  .countdown 2026-12-25 圣诞节   添加\n"
                 "  .countdown                    查看\n"
                 "  .countdown list               详细列表\n"
                 "  .countdown del 1              删除",

    "balance": "【余额查询 .balance】\n"
               "  查询当前供应商 API 余额\n"
               "  (DeepSeek / 硅基流动 / 基元律动均支持,\n"
               "   基元律动需在 .env 配置 TR_COOKIE 登录态)\n",

    "model": "【模型供应商 .model .provider】\n"
             "  .model                    查看当前供应商与模型\n"
             "  .model <deepseek|tokenrhythm|siliconflow> [模型名]\n"
             "  切换供应商, 模型名缺省为 deepseek-v4-flash\n",

    "voice": "【语音 .voice】\n"
             "  .voice <文本>           Edge TTS 合成语音\n"
             "  .voice list             列出可用语音\n"
             "  .voice zh-CN-YunxiNeural <文本>  指定发音人\n"
             "  默认: 晓晓 (女声)",

    "cost": "【Token消耗 .cost】\n"
            "  查看今日/累计 token 消耗和费用\n",

    "tokens": "【Token计算 .tokens】\n"
              "  .tokens <文本>  计算 token 数和预估费用\n",

    "resetfav": "【重置好感 .resetfav (主人)】\n"
                "  清空所有好感度数据",

    "memory": "【记忆 .memory】\n"
              "  working      瞬时记忆（当前对话）\n"
              "  short        短时记忆（最近30条）\n"
              "  long         长时记忆（持久化）\n"
              "  search <关键词> 搜索全部记忆\n"
              "  clear        清空短时记忆",
}


async def cmd_favlist(args, user_id, group_id, sender_name, is_group, bot_qq):
    """查看当前聊天的好感度列表"""
    logger.info("指令 .favlist 触发 user=%d chat=%d", user_id, group_id if is_group else user_id)
    cfg = get_config()
    chat_id = group_id if is_group else user_id

    fav_data = get_all_fav(chat_id=chat_id, is_group=is_group)
    if not fav_data:
        return format_lang("favlist.empty")

    rows = []
    for key, val in sorted(fav_data.items(), key=lambda x: x[1], reverse=True):
        # 从 key 中提取 user_id: "g123:456" -> "456", "p:456" -> "456"
        uid = key.split(":")[-1] if ":" in key else key
        name = cfg.qq_name_map.get(uid, uid)
        rows.append((name, uid, val))

    from modules.cmd_cards import build_favlist
    return "__CARD__:" + json.dumps(build_favlist(rows), ensure_ascii=False)


async def cmd_info(args, user_id, group_id, sender_name, is_group, bot_qq):
    """返回系统和运行状态"""
    import platform
    import time as _time

    cfg = get_config()
    name = cfg.bot_name
    logger.info("指令 .info 触发 user=%d", user_id)

    d: dict = {
        "name": name,
        "system": {"os": f"{platform.system()} {platform.release()} ({platform.machine()})",
                   "python": platform.python_version()},
        "runtime": {}, "connection": {}, "config": {}, "models": {}, "token": {},
    }

    # ── 系统 ──
    try:
        import psutil
        mem = psutil.virtual_memory()
        total_mb = mem.total // (1024**2)
        used_mb = mem.used // (1024**2)
        d["system"]["memory"] = f"{used_mb}MB / {total_mb}MB ({mem.percent}%)"
        cpu_name = platform.processor() or "Unknown"
        cpu_count = psutil.cpu_count(logical=True)
        cpu_percent = psutil.cpu_percent(interval=0.3)
        d["system"]["cpu"] = f"{cpu_name} ({cpu_count}c) [{cpu_percent}%]"
        disk = psutil.disk_usage('/')
        disk_gb = disk.used / (1024**3)
        disk_total = disk.total / (1024**3)
        d["system"]["disk"] = f"{disk_gb:.1f}GB / {disk_total:.1f}GB ({disk.percent}%)"
    except ImportError:
        d["system"]["memory"] = "unavailable (install psutil)"
        d["system"]["disk"] = "unavailable"

    # ── 运行 ──
    try:
        import psutil
        proc = psutil.Process()
        create_time = proc.create_time()
        uptime_sec = int(_time.time() - create_time)
        hours = uptime_sec // 3600
        mins = (uptime_sec % 3600) // 60
        d["runtime"]["uptime"] = f"{hours}h {mins}m"
    except Exception:
        d["runtime"]["uptime"] = "unknown"

    from core.dispatcher import get_current_dispatcher
    from core.context_manager import get_context_mgr
    disp = get_current_dispatcher()
    ctx = get_context_mgr()
    stats = ctx.get_stats()
    d["runtime"]["msgs"] = disp.msg_count if disp else "?"
    d["runtime"]["chats"] = stats.get('active_chats', 0)
    d["runtime"]["tasks"] = stats.get('active_tasks', 0)

    d["runtime"]["scache"] = get_cache_stats().get('entries', 0)

    # ── 连接 ──
    from services.sender import _bot as _khl_bot
    from services.sender import _channel_cache
    d["connection"]["kook"] = 'connected' if _khl_bot is not None else 'not init'
    d["connection"]["cache"] = len(_channel_cache)

    # ── 配置 ──
    d["config"]["reply_threshold"] = cfg.reply_interest
    d["config"]["context_length"] = cfg.context_length
    d["config"]["private_chat"] = bool(cfg.enable_private)
    d["config"]["image_recog"] = bool(cfg.image_model.switch)
    d["config"]["groups"] = len(cfg.group_list)
    d["config"]["debug"] = bool(cfg.debug_mode)

    # ── 模型 ──（从运行时配置读取，不写死）
    d["models"]["reply"] = {"name": cfg.reply_model.name, "provider": cfg.reply_model.provider}
    d["models"]["cheap"] = {"name": cfg.cheap_model.name, "provider": cfg.cheap_model.provider}
    d["models"]["judge"] = {"name": cfg.judge_model.name, "provider": cfg.judge_model.provider}
    d["models"]["vision"] = {"name": cfg.image_model.name, "provider": cfg.image_model.provider,
                             "enabled": bool(cfg.image_model.switch)}

    # ── Token 消耗 ──
    try:
        from core.token_tracker import calc_cost
        cost = calc_cost(today_only=False)
        t = cost["today"]
        total = cost["total"]
        d["token"]["today_calls"] = t['calls']
        d["token"]["today_tokens"] = t['prompt'] + t['completion']
        d["token"]["today_cost"] = t['cost']
        d["token"]["total_calls"] = total['calls']
        d["token"]["total_tokens"] = total['prompt'] + total['completion']
        d["token"]["total_cost"] = total['cost']
    except Exception:
        pass

    from modules.cmd_cards import build_info
    return "__CARD__:" + json.dumps(build_info(d), ensure_ascii=False)


_TR_API = "https://tokenrhythm.studio/api"


async def _tr_balance() -> str:
    """Token Rhythm 余额（逆向接口，需 .env 配置 TR_COOKIE 或 TR_TOKEN）

    GET /wallet/expiring-credits  → 额度明细，真实余额 = Σ remainingCny
    GET /usage/panel?range=today  → 当日用量（调用次数 / 花费）
    """
    import os
    cookie = os.environ.get("TR_COOKIE", "").strip()
    token = os.environ.get("TR_TOKEN", "").strip()
    if not cookie and not token:
        return ("基元律动余额需要登录态鉴权（.env 未配置 TR_COOKIE / TR_TOKEN）\n"
                "可到用户中心查看: https://tokenrhythm.studio/account")

    headers = {"Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if token:
        headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie

    async def _api_get(path: str):
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_TR_API + path, headers=headers)
            if r.status_code == 401:
                raise ValueError("登录态已失效，请更新 .env 中的 TR_COOKIE")
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            data = r.json()
            if data.get("code") not in (0, None):
                raise ValueError(data.get("message", "业务错误"))
            return data.get("data", {})

    try:
        wallet = await _api_get("/wallet/expiring-credits?page=1&pageSize=100")
        usage = await _api_get("/usage/panel?range=today&page=1&pageSize=1")
    except Exception as e:
        return f"基元律动余额查询失败: {e}"

    credits = wallet.get("list", [])
    total = sum(float(x.get("remainingCny") or 0) for x in credits)
    summary = wallet.get("summary", {})
    next_expiry = str(summary.get("nextExpiryAt") or "")[:10]

    s = usage.get("summary", {})
    calls = s.get("calls", usage.get("total", 0))
    spent = float(s.get("costCny") or 0)

    lines = ["【基元律动 (TokenRhythm) 余额】",
             f"  可用余额: ¥{total:.2f}（{len(credits)} 条额度）"]
    if next_expiry:
        lines.append(f"  最早过期: {next_expiry}")
    lines.append(f"  今日消耗: ¥{spent:.4f} / {calls} 次调用")
    top = sorted(credits, key=lambda x: -float(x.get("remainingCny") or 0))[:3]
    for x in top:
        lines.append(f"  · {x.get('sourceLabel', '?')}: 余 ¥{float(x.get('remainingCny') or 0):.2f}")
    return "\n".join(lines)


async def cmd_balance(args, user_id, group_id, sender_name, is_group, bot_qq):
    """余额查询 .balance — 查询当前选中供应商的余额"""
    from core.config import get_config
    cfg = get_config()
    provider = (cfg.reply_model.provider or "DEEPSEEK").upper()

    # Token Rhythm 无官方余额 API → 走逆向接口（不依赖 API Key，用 Cookie 登录态）
    if provider == "TOKENRHYTHM":
        return await _tr_balance()

    key = cfg.reply_model.key
    if not key:
        return "未配置 API Key 喵~"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "SILICONFLOW":
                base_url = (cfg.reply_model.url or "https://api.siliconflow.cn/v1").rstrip("/")
                resp = await client.get(
                    f"{base_url}/user/info",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
                )
                if resp.status_code != 200:
                    return f"查询失败: HTTP {resp.status_code} 喵~"
                data = resp.json().get("data", {})
                balance = data.get("balance", "?")
                total = data.get("totalBalance", "?")
                charge = data.get("chargeBalance", "?")
                lines = ["【硅基流动 (SiliconFlow) 余额】"]
                lines.append(f"  当前余额: {balance}")
                lines.append(f"  总余额:   {total}")
                lines.append(f"  充值额:   {charge}")
                return "\n".join(lines)

            # 默认 DeepSeek
            resp = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
            )
            if resp.status_code != 200:
                return f"查询失败: HTTP {resp.status_code} 喵~"
            data = resp.json()
    except Exception as e:
        return f"查询余额失败: {e}"

    if not data.get("is_available"):
        return "余额查询不可用喵~"

    infos = data.get("balance_infos", [])
    if not infos:
        return "余额信息为空喵~"

    lines = ["【DeepSeek 余额】"]
    for info in infos:
        currency = info.get("currency", "?")
        total = info.get("total_balance", "?")
        granted = info.get("granted_balance", "?")
        topped_up = info.get("topped_up_balance", "?")
        lines.append(f"  总余额: {total} {currency}")
        lines.append(f"  赠送额: {granted} {currency}")
        lines.append(f"  充值额: {topped_up} {currency}")
    return "\n".join(lines)


async def cmd_box(args, user_id, group_id, sender_name, is_group, bot_qq):
    """查询快递物流信息（KOOK 原生卡片输出）"""
    if not args:
        return format_lang("box.prompt_input")

    tracking_no = args[0]
    ckey = os.getenv("KUAIBAO_CKEY", "").strip()
    logger.info("指令 .box 触发 单号=%s user=%d", tracking_no, user_id)

    # ckey 按接口文档为可选参数；配置了才携带，未配置则不带。
    params = {"trackingNo": tracking_no}
    if ckey:
        params["ckey"] = ckey

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://openapi.dwo.cc/api/kuaiok", params=params)
            if resp.status_code != 200:
                return format_lang("box.api_error")
            data = resp.json()
    except Exception as e:
        logger.warning("快递查询网络错误: %s", e)
        return format_lang("box.network_error", error=str(e))

    code = data.get("code", "")
    if code != "0000000000":
        logger.info("快递API业务码异常: %s desc=%s", code, data.get("desc", ""))
        return format_lang("box.api_error", message=data.get("desc", "未知错误"))

    try:
        pkgs = data.get("data", {}).get("packageInfoList", [])
        if not pkgs:
            return format_lang("box.no_result")

        pkg = pkgs[0]
        cp_name = pkg.get("cpName", "某快递")
        state = pkg.get("state", "未知")

        # 状态映射（i18n）
        state_map = {
            "TRANSPORT": format_lang("box.state_transport", default="运输中"),
            "DELIVERING": format_lang("box.state_delivering", default="派送中"),
            "SIGNED": format_lang("box.state_signed", default="已签收"),
            "RETURN": format_lang("box.state_return", default="退件"),
            "PENDING": format_lang("box.state_pending", default="待揽收"),
            "FINISH": format_lang("box.state_finish", default="已完成"),
        }
        state_text = state_map.get(state, state)
        latest_msg = pkg.get("operateMessage", "")
        details = pkg.get("trackingDetails", [])

        logger.info("快递查询成功: 单号=%s 状态=%s (%d条轨迹)", tracking_no, state_text, len(details))

        # 构建 KOOK 原生卡片并直接发送（无需图片截图，不再阻塞/后台任务）
        from modules.cmd_cards import build_box_card
        card_obj = build_box_card({
            "cp_name": cp_name,
            "cp": pkg.get("cp", ""),
            "tracking_no": tracking_no,
            "state": state,
            "state_text": state_text,
            "latest_msg": latest_msg,
            "details": details,
        })
        if is_group:
            await send_raw_group(card_obj, group_id)
        else:
            await send_raw_user(card_obj, user_id)
        return None

    except Exception as e:
        logger.error("快递信息解析失败: %s", e)
        return format_lang("box.parse_error")


LUCK_FILE = Path(__file__).resolve().parent.parent / "data" / "luck.json"


def _get_today_luck(user_id: int) -> int | None:
    """获取今天该用户的幸运值，未抽签返回 None"""
    today = date.today().isoformat()
    if not LUCK_FILE.exists():
        return None
    try:
        with open(LUCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(today, {}).get(str(user_id))
    except Exception as e:
        logger.warning("获取今日运气失败: %s", e)
        return None


def _set_today_luck(user_id: int, value: int):
    """保存今天该用户的幸运值"""
    LUCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    
    data = {}
    if LUCK_FILE.exists():
        try:
            with open(LUCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    
    if today not in data:
        data[today] = {}
    data[today][str(user_id)] = value
    
    # 只保留最近 7 天的数据
    sorted_days = sorted(data.keys(), reverse=True)[:7]
    data = {d: data[d] for d in sorted_days}
    
    with open(LUCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def cmd_luck(args, user_id, group_id, sender_name, is_group, bot_qq):
    """每日运势抽签 .luck — 今天内同一人返回相同值"""
    logger.info("指令 .luck 触发 user=%d", user_id)
    
    # 检查今天是否已经抽过
    cached = _get_today_luck(user_id)
    if cached is not None:
        return f"(met){user_id}(met) 你今天的运气是 {cached} 喵（今日已抽签）"
    
    # 首次抽签，生成幸运值
    if random.random() < 0.001:
        num = 1000
    else:
        num = random.randint(1, 100)
    
    _set_today_luck(user_id, num)
    return f"(met){user_id}(met) 你今天的运气是 {num} 喵"


async def cmd_testsys(args, user_id, group_id, sender_name, is_group, bot_qq):
    """发送测试卡片消息"""
    logger.info("指令 .testsys 触发 user=%d group=%d", user_id, group_id or 0)

    # 返回特殊标记，由分发器调用 _send_test_card 负责实际发送
    return "__SYS_TEST_CARD__"


async def cmd_testok(args, user_id, group_id, sender_name, is_group, bot_qq):
    """测试卡片回调"""
    logger.info("指令 .testok 触发（卡片按钮点击）user=%d", user_id)
    return "test ok"


async def cmd_testup(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.test-up — 返回 update_test_note.md 的内容（测试用）"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    note = root / "update_test_note.md"
    if note.exists():
        return note.read_text(encoding="utf-8").strip()
    return "update_test_note.md 不存在"


# ════════════════════════════════════════════════════════════
#  通知系统指令（性能降级 / GitHub 更新提示）
# ════════════════════════════════════════════════════════════

async def cmd_notify(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.notify — 配置通知目标频道（卡片按钮选择，仅管理员）"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "权限不足喵~"

    from services.sender import _channel_cache
    channels = list(_channel_cache.items())
    if not channels:
        return "当前没有已缓存的频道。请先让机器人进入至少一个群聊，再执行 .notify"

    from services.notify_system import get_target_channels
    targets = get_target_channels()

    markdown_content = "**KOOK BOT · 通知目标频道**\n点击按钮选择通知发送目标：\n\n"
    for cid, ch in channels:
        name = getattr(ch, 'name', None) or getattr(ch, 'channel_name', None) or str(cid)
        mark = "✓" if int(cid) in targets else "○"
        markdown_content += f"{mark} `{name}`（{cid}）\n"

    # KOOK 按钮: 放在 action-group 模块里，click=return-val，点击后通过
    # message_btn_click 事件把 value(指令字符串) 回传给 bot
    kook_buttons = []
    for cid, ch in channels:
        name = (getattr(ch, 'name', None) or getattr(ch, 'channel_name', None) or str(cid))[:20]
        kook_buttons.append({
            "type": "button",
            "theme": "primary",
            "value": f".notifyset {cid}",
            "click": "return-val",
            "text": {"type": "plain-text", "content": name},
        })
    # 每个 action-group 最多 4 个按钮，频道多时分多组
    groups = [kook_buttons[i:i + 4] for i in range(0, len(kook_buttons), 4)]
    action_groups = [{"type": "action-group", "elements": g} for g in groups]

    modules = [{"type": "section", "text": {"type": "kmarkdown", "content": markdown_content}}]
    modules.extend(action_groups)
    card = [{"type": "card", "theme": "secondary", "size": "lg", "modules": modules}]

    if is_group:
        await send_raw_group(card, group_id)
    else:
        await send_raw_user(card, user_id)
    return None


async def cmd_notifyset(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.notifyset <channel_id> — 添加通知目标频道（由卡片按钮点击触发）"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return None  # 非管理员静默

    if not args:
        return None
    try:
        cid = int(args[0])
    except (ValueError, TypeError):
        return "频道 ID 无效喵~"

    from services.notify_system import add_target_channel
    if add_target_channel(cid):
        return f"已添加通知目标频道：{cid}\n现在回复 .notifycheck 可立即测试；性能降级/GitHub更新将推送至该频道"
    return f"频道 {cid} 已在通知列表中"


async def cmd_notifycheck(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.notifycheck — 手动触发一次 GitHub 更新检测（用于验证配置）"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "权限不足喵~"

    from services.notify_system import check_github_update, get_target_channels, load_cfg
    targets = get_target_channels()
    if not targets:
        return "尚未配置通知目标频道，先执行 .notify 选择频道"
    await check_github_update()
    ncfg = load_cfg()
    return (f"通知目标频道：{targets}\n"
            f"更新检查间隔：{ncfg.get('update_interval_s', 1800)}s\n"
            f"上次已通知 SHA：{str(ncfg.get('last_notified_sha', ''))[:7] or '无'}\n"
            f"（无新提交则不会重复推送）")


async def cmd_notifyperf(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.notifyperf [on|off|status] — 开/关 LLM 性能降级通知（仅管理员）。

    用于关闭"KOOK BOT · LLM 服务降级"这类推送，避免在开发/调试期被打扰。
    - 无参数 / status：显示当前状态
    - on  ：开启性能降级通知
    - off ：关闭性能降级通知（降级/恢复卡片都不再推送）
    """
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "权限不足喵~"

    from services.notify_system import get_perf_enabled, set_perf_enabled
    if args and args[0].lower() in ("on", "off"):
        enable = args[0].lower() == "on"
        set_perf_enabled(enable)
        return f"已{'开启' if enable else '关闭'} LLM 性能降级通知。\n当前：{'开启' if enable else '已关闭'}（.notifyperf on 可恢复）"
    cur = get_perf_enabled()
    return f"LLM 性能降级通知当前状态：{'开启' if cur else '已关闭'}"


async def cmd_jsonraw(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.jsonraw <对话内容> → 输出 LLM 原始 JSON"""
    text = " ".join(args) if args else "喵"
    logger.info("指令 .jsonraw 触发 user=%d text='%s'", user_id, text[:40])
    from services.llm import call_llm
    from core.config import get_config
    cfg = get_config()
    max_chars = "40" if is_group else "12"

    fmt_reminder = (
        "【格式规则：严格输出 JSON，不要任何额外文字】\n"
        '{"replies":["完整的第一句话","自然的第二句话"],"fav":2,"calls":[],"face":null,"mood":"开心","action":"摇了摇尾巴","at":null,"mode":null,"origin":"user","actor":{"name":"当前发言者","qq":0}}\n'
        f"replies 2~5句，每句≤{max_chars}字，内容完整自然。fav -5~+5。\n"
        "mood: 当前情绪。action: 动作描写。at: @的QQ号，不@就null。mode: 模式切换。face: 极少用，通常null。\n"
        "origin: 谁发起操作(user/bot)。actor: 替谁执行({name,qq})，bot发起时actor=null。\n"
        '【致命规则：replies 内必须用标准 JSON，英文引号必须转义为 \\" ，或用中文引号「」替代！】\n'
        '【指令调用规则：如果有人要求你执行一个操作，必须通过calls执行对应指令。】\n'
        "【禁止：JSON之后严禁加任何注释、说明、//、/*、```、换行文字！】"
    )

    system = cfg.system_prompt or "你是幻梦，一只可爱的猫娘机器人。"
    raw = await call_llm(cfg.reply_model, [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{fmt_reminder}\n\n{sender_name}说：{text}"},
    ], max_tokens=3000, temperature=0.8, json_mode=True)
    return raw or "LLM 返回为空"


async def cmd_md(args, user_id, group_id, sender_name, is_group, bot_qq):
    """发送 KMarkdown 消息 .md <内容>"""
    if not args:
        return "用法: .md <KMarkdown内容>\\n示例: .md # 标题\\n> 引用\\n**粗体** *斜体*"
    raw = " ".join(args)
    raw = raw.replace("\\n", "\n")

    from services.sender import send_by_chat_type
    await send_by_chat_type(raw, group_id if is_group else user_id, is_group, user_id=user_id)
    return None


async def cmd_update_info(args, user_id, group_id, sender_name, is_group, bot_qq):
    """显示更新日志 .up [版本号]，无参数默认最新版本"""
    logger.info("指令 .up 触发 user=%d group=%d args=%s", user_id, group_id or 0, args)

    from modules.changelog import _read_update_log
    full_md = _read_update_log()

    custom_md = None
    if args and args[0].lower() != "all":
        # 指定版本
        target_version = args[0]
        import re
        clean = target_version.lstrip('vV')
        pattern = rf'(##\s*v?{re.escape(clean)}[\s\S]*?)(?=\n## |$)'
        match = re.search(pattern, full_md, re.IGNORECASE)
        if match:
            custom_md = match.group(1).strip()
        else:
            return f"❌ 未找到版本 {target_version} 的更新日志，请检查版本号是否正确。"
    elif not args:
        # 无参数：提取最新（第一个 ## v 段落）
        import re
        match = re.search(r'(##\s*v[\d.]+.*?)(?=\n## |$)', full_md, re.DOTALL)
        if match:
            custom_md = match.group(1).strip()

    # 异步生成并发送卡片
    async def _bg_send_changelog():
        try:
            result = await send_changelog_card(
                group_id=group_id if is_group else None,
                user_id=user_id if not is_group else None,
                is_group=is_group,
                custom_md=custom_md,
            )
            if result is not None:
                if is_group:
                    await send_group_msg(result, group_id)
                else:
                    await send_private_msg(result, user_id)
        except Exception as e:
            logger.error("[BG] 更新日志卡片后台发送失败: %s", e, exc_info=True)

    asyncio.create_task(_bg_send_changelog())
    return None


# ─── 搜索指令 ──────────────────────────────────────────────

async def cmd_search(args, user_id, group_id, sender_name, is_group, bot_qq):
    """搜索指令 .search [条数] [数据源] <关键词>，数据源: baidu/baike/bing/all(默认)"""
    if not args:
        return format_lang("search.prompt_input")

    limit = 4
    source = "all"

    # 解析可选条数
    if args[0].isdigit():
        limit = min(int(args[0]), 20)
        args = args[1:]
        if not args:
            return format_lang("search.prompt_input")

    # 解析可选数据源
    if args and args[0].lower() in ("baidu", "baike", "bing", "all"):
        source = args[0].lower()
        args = args[1:]
        if not args:
            return format_lang("search.prompt_input")

    query = " ".join(args)
    chat_id = group_id if is_group else user_id
    logger.info("指令 .search 触发 query='%s' limit=%d source=%s user=%d", query[:30], limit, source, user_id)

    result = await perform_search(query, sender_name=sender_name, user_id=user_id,
                                   chat_id=chat_id, limit=limit, source=source, is_group=is_group)
    return result or format_lang("search.no_result")


def _parse_github_url(url: str) -> dict | None:
    """解析 GitHub URL，返回 {owner, repo, type, ref, path} 或 None"""
    import re
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)(?:/tree|/blob)?(?:/([^/]+))?(?:/(.*))?', url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # 去掉 .git 后缀（仅当以 .git 结尾时）
    if repo.endswith('.git'):
        repo = repo[:-4]
    # 判断 URL 类型
    raw_path = m.group(4) or ""
    ref = m.group(3) or ""
    if '/blob/' in url:
        url_type = "file"
        ref = url.split('/blob/')[1].split('/')[0] if '/blob/' in url else "main"
        raw_path = url.split('/blob/' + ref + '/', 1)[1] if '/blob/' + ref + '/' in url else ""
    elif '/tree/' in url:
        url_type = "dir"
        ref = url.split('/tree/')[1].split('/')[0] if '/tree/' in url else "main"
        raw_path = url.split('/tree/' + ref + '/', 1)[1] if '/tree/' + ref + '/' in url else ""
    else:
        url_type = "repo"
        ref = "main"
    return {"owner": owner, "repo": repo, "type": url_type, "ref": ref, "path": raw_path}


async def _fetch_github_repo(owner: str, repo: str) -> str:
    """通过 GitHub API 获取仓库概览"""
    headers = {"Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return f"GitHub API 返回 {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
    lines = [
        f"仓库: {data.get('full_name', f'{owner}/{repo}')}",
        f"描述: {data.get('description', '无')}",
        f"语言: {data.get('language', '未知')}",
        f"Stars: {data.get('stargazers_count', 0)}  Forks: {data.get('forks_count', 0)}",
        f"开源协议: {data.get('license', {}).get('spdx_id', '无') if data.get('license') else '无'}",
        f"默认分支: {data.get('default_branch', 'main')}",
        f"创建: {data.get('created_at', '?')}  更新: {data.get('updated_at', '?')}",
        f"URL: {data.get('html_url', url)}",
    ]
    if data.get('topics'):
        lines.append(f"标签: {', '.join(data['topics'])}")
    return "\n".join(lines)


async def _fetch_github_contents(owner: str, repo: str, path: str = "", ref: str = "main") -> str:
    """通过 GitHub API 获取目录列表或文件内容"""
    import base64
    headers = {"Accept": "application/vnd.github+json"}
    api_path = f"/{path}" if path else ""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents{api_path}?ref={ref}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return f"GitHub API 返回 {resp.status_code}"
        data = resp.json()

    if isinstance(data, dict):
        # 单个文件
        if data.get("type") == "file" and data.get("content"):
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                size = data.get("size", 0)
                ext = path.rsplit(".", 1)[-1] if "." in path else ""
                header = f"文件: {path} ({size} bytes)"
                if ext:
                    header += f"\n```{ext}"
                else:
                    header += "\n```"
                return header + "\n" + content[:6000] + ("\n```" if len(content) <= 6000 else "\n...(截断)```")
            except Exception:
                return f"文件 {path} 无法解码"
        elif data.get("type") == "dir":
            items = [f"{'[DIR]' if d['type'] == 'dir' else '[FILE]'} {d['name']}" for d in data[:50]]
            return f"目录: {path or '/'} ({len(data)} 项)\n" + "\n".join(items) + (f"\n...(还有 {len(data)-50} 项)" if len(data) > 50 else "")
        return f"未知类型: {data.get('type')}"

    if isinstance(data, list):
        # 目录列表
        items = []
        for d in data[:50]:
            icon = "[DIR] " if d.get("type") == "dir" else "[FILE]"
            items.append(f"{icon}{d['name']}")
        return f"目录: {path or '/'} ({len(data)} 项)\n" + "\n".join(items) + (f"\n...(还有 {len(data)-50} 项)" if len(data) > 50 else "")
    return "GitHub API 返回格式异常"


async def cmd_read(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.read <URL> — 深度读取网页正文，返回纯文本摘要。支持 GitHub 仓库/文件"""
    if not args:
        return "用法: .read <网页URL>  例如 .read https://github.com/Trusler258/huanmeng-qqbot"
    url = args[0]
    if not url.startswith("http"):
        return "请输入完整的网页URL（http/https开头）"

    # GitHub URL → API 获取
    gh = _parse_github_url(url)
    if gh:
        owner, repo, gtype, ref, path = gh["owner"], gh["repo"], gh["type"], gh["ref"], gh["path"]
        logger.info("GitHub API 读取: %s/%s type=%s ref=%s path=%s", owner, repo, gtype, ref, path)
        try:
            if gtype == "repo":
                return await _fetch_github_repo(owner, repo)
            elif gtype in ("file", "dir"):
                return await _fetch_github_contents(owner, repo, path, ref)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.warning("GitHub API 读取失败: %s\n%s", e, tb)
            return f"GitHub API 读取失败: {e}\n\n堆栈:\n{tb}"

    # 普通网页 → scraper
    try:
        from modules.local_search import get_scraper
        scraper = get_scraper()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: scraper.scrape(url))
        if result:
            return result
        return f"无法读取该页面: {url}"
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.warning("页面读取失败: %s\n%s", e, tb)
        return f"读取页面失败: {e}\n\n堆栈:\n{tb}"


# ─── WHOIS 指令 ──────────────────────────────────────────────

async def cmd_whois(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.whois <域名> — 查询域名注册信息（注册商/注册时间/到期时间/NS/状态）"""
    if not args:
        return "用法: .whois <域名>  例如 .whois 01240820.xyz"
    domain = " ".join(args)
    logger.info("指令 .whois 触发 domain=%s user=%d", domain, user_id)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lookup_domain, domain)
    return result


async def cmd_kook_user_id(args, user_id, group_id, sender_name, is_group, bot_qq, raw_event=None):
    """.kook_user_id <@的人> — 查询被 @ 用户的精确 KOOK user_id
    用法: .kook_user_id @某人
    """
    target_uid = ""

    # 方法1: 从 raw_event 的 mention 列表提取（最精确）
    if raw_event is not None:
        mention_list = []
        if hasattr(raw_event, 'mention') and raw_event.mention:
            mention_list = list(raw_event.mention)
        # 排除 bot 自身
        bot_id_str = str(bot_qq)
        for m in mention_list:
            m_str = str(m)
            if m_str != bot_id_str:
                target_uid = m_str
                break

    # 方法2: 从消息文本提取 (met)id(met)
    if not target_uid and raw_event is not None:
        import re as _re
        content = getattr(raw_event, 'content', '') or ''
        met_match = _re.search(r'\(met\)(\w+)\(met\)', content)
        if met_match:
            uid = met_match.group(1)
            if uid not in ('here', 'all') and uid != str(bot_qq):
                target_uid = uid

    # 方法3: 纯数字参数（直接当 user_id）
    if not target_uid and args:
        try:
            int(args[0])
            target_uid = args[0]
        except ValueError:
            pass

    if not target_uid:
        return "用法: .kook_user_id @某人\n请在指令后 @ 要查询的用户"

    # 查询昵称
    from utils.username import get_or_resolve_username
    nick = await get_or_resolve_username(target_uid)

    # 记录到 qq_name_map（昵称不为空且不等于 ID）
    if nick and nick != target_uid:
        try:
            from modules.admin import _save_user_name_map
            await _save_user_name_map(target_uid, nick)
        except Exception:
            pass

    return f"用户 ID: {target_uid}\n昵称: {nick}"


async def cmd_kook_channel_id(args, user_id, group_id, sender_name, is_group, bot_qq, raw_event=None):
    """.kook_channel_id — 返回当前频道的精确 ID"""
    channel_id = ""
    if raw_event is not None:
        # 尝试从 raw_event.ctx.channel 获取
        if hasattr(raw_event, 'ctx') and hasattr(raw_event.ctx, 'channel'):
            channel_id = str(raw_event.ctx.channel.id)

    if not channel_id:
        channel_id = str(group_id)

    chat_type = "频道(群聊)" if is_group else "私聊"
    return f"当前{chat_type} ID: {channel_id}"


async def cmd_write_code(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.write_code <描述> — 生成代码文件并发送"""
    if not args:
        return "用法: .write_code <描述>  例如 .write_code 用HTML写一个2048游戏"
    desc = " ".join(args)
    lang = "html" if any(kw in desc.lower() for kw in ("html", "网页", "web", "css", "javascript", "js")) else "python"
    from core.tools import _write_code
    return await _write_code(
        language=lang, description=desc,
        user_id=user_id, group_id=group_id, sender_name=sender_name,
        is_group=is_group, bot_qq=bot_qq,
    )


# ─── 忽略/解除忽略指令 ──────────────────────────────────────

async def cmd_ignore(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.ignore <QQ> — 全群忽略该用户（仅 admin）"""
    cfg = get_config()
    if user_id != cfg.admin_qq:
        return "只有管理员能操作喵~"
    if not args:
        return "用法: .ignore <QQ号>  例如 .ignore 123456789"
    try:
        target = int(args[0])
    except ValueError:
        return "QQ 号不对喵~"
    from modules.ignore_users import add_ignored
    add_ignored(target)
    return f"已忽略 {target}，所有群都不会再收到他的消息"


async def cmd_unignore(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.unignore <QQ> — 解除全群忽略（仅 admin）"""
    cfg = get_config()
    if user_id != cfg.admin_qq:
        return "只有管理员能操作喵~"
    if not args:
        return "用法: .unignore <QQ号> / all  例如 .unignore 123456789"
    arg = args[0].lower()
    from modules.ignore_users import remove_ignored, list_ignored
    if arg == "all":
        ids = list_ignored()
        count = 0
        for uid in ids:
            remove_ignored(uid)
            count += 1
        return f"已解除所有忽略（共 {count} 人）"
    try:
        target = int(args[0])
    except ValueError:
        return "QQ 号不对喵~"
    if remove_ignored(target):
        return f"已解除忽略 {target}"
    return f"{target} 不在忽略列表中喵~"


# ─── 天气指令 ──────────────────────────────────────────────

async def cmd_weather(args, user_id, group_id, sender_name, is_group, bot_qq):
    """天气查询指令 .天气 .weather（优先输出精美卡片图片）"""
    if not args:
        return format_lang("weather.prompt_input")
    
    city = " ".join(args)
    logger.info("指令 .weather 触发 city=%s user=%d (卡片模式优先)", city, user_id)
    
    # 先发提示
    from services.sender import send_group_msg, send_private_msg
    tip = format_lang("weather.searching")
    await (send_group_msg(tip, group_id) if is_group 
           else send_private_msg(tip, user_id))
    
    data = await query_weather(city)
    if data is None:
        return format_lang("weather.fallback_error")

    # ── 异步卡片生成（不阻塞聊天线程）──
    async def _bg_send_weather():
        try:
            card_result = await send_weather_card(
                data=data,
                group_id=group_id if is_group else None,
                user_id=user_id if not is_group else None,
                is_group=is_group,
            )
            if card_result is not None:
                # 卡片生成失败，回退纯文本
                fallback = build_weather_report(data, user_id)
                if is_group:
                    await send_group_msg(fallback, group_id)
                else:
                    await send_private_msg(fallback, user_id)
        except Exception as e:
            logger.error("[BG] 天气卡片后台发送失败: %s", e, exc_info=True)

    asyncio.create_task(_bg_send_weather())
    return None  # 后台异步处理，无需立即回复


# ─── 管理员指令 ────────────────────────────────────────────

async def cmd_reload(args, user_id, group_id, sender_name, is_group, bot_qq):
    """热重载配置（不重启进程）"""
    roles = load_roles_config()
    if user_id != roles.get("admin_qq"):
        logger.warning("非管理员尝试 reload user=%d", user_id)
        return format_lang("reload.permission_denied")

    logger.info("管理员 %s(%d) 触发热重载", sender_name, user_id)
    from core.config import full_reload
    full_reload()
    logger.info("热重载完成 (config + keywords + lang + skills)")
    return format_lang("reload.success")


# ── 模型供应商选择 ─────────────────────────────────────────
# 预设供应商表：展示名 + 对应 bot_config.toml 里的 provider 标签。
# 实际 url/key 从 config/.env 读取（<PROVIDER>_URL / <PROVIDER>_KEY），指令只切换 provider 标签。
_PROVIDERS = {
    "deepseek":     {"label": "DEEPSEEK",     "name": "DeepSeek"},
    "tokenrhythm":  {"label": "TOKENRHYTHM",  "name": "基元律动 (Token Rhythm)"},
    "siliconflow":  {"label": "SILICONFLOW",  "name": "硅基流动 (SiliconFlow)"},
}


def _provider_ready(label: str) -> bool:
    """判断供应商是否已在 .env 配好 url+key。"""
    try:
        import os
        url = os.environ.get(f"{label}_URL", "")
        key = os.environ.get(f"{label}_KEY", "")
        return bool(url and key)
    except Exception:
        return False


def _read_provider_cfg() -> tuple[str, str]:
    """读回当前 replyer_1 的 (provider, model_name)。"""
    import toml
    from pathlib import Path
    _cfg_path = Path(__file__).resolve().parent.parent / "config" / "bot_config.toml"
    data = toml.load(_cfg_path) if _cfg_path.exists() else {}
    m = data.get("model", {}).get("replyer_1", {})
    return m.get("provider", ""), m.get("name", "")


async def cmd_provider(args, user_id, group_id, sender_name, is_group, bot_qq):
    """切换回复模型供应商 .model / .provider / .供应商

    用法:
      .model                    查看当前供应商与可用列表
      .model <名字> [模型名]     切换回复模型到指定供应商（deepseek/tokenrhythm/siliconflow）
                             模型名缺省为 deepseek-v4-flash
    仅管理员可用。切换后写入 bot_config.toml 的 model.replyer_1 并热重载。
    """
    roles = load_roles_config()
    admin_qq = roles.get("admin_qq")
    if user_id != admin_qq:
        return format_lang("error.permission_denied")

    cur_provider, cur_model = _read_provider_cfg()

    # 无参数 → 展示列表
    if not args:
        lines = ["【模型供应商】", f"当前: {cur_provider or '未设置'} (模型 {cur_model or '-'})", ""]
        for key, p in _PROVIDERS.items():
            ready = _provider_ready(p["label"])
            mark = "● 当前" if p["label"] == cur_provider else ("✓ 已配" if ready else "✗ 未配 key")
            lines.append(f"  {key:<14} {p['name']}  {mark}")
        lines.append("")
        lines.append("切换: .model <deepseek|tokenrhythm|siliconflow> [模型名]")
        return "\n".join(lines)

    target = args[0].strip().lower()
    if target not in _PROVIDERS:
        return f"未知供应商: {target}\n可用: {', '.join(_PROVIDERS)}"

    label = _PROVIDERS[target]["label"]
    if not _provider_ready(label):
        return f"{_PROVIDERS[target]['name']} 尚未配置（.env 缺少 {label}_URL / {label}_KEY），无法切换"

    # 第二个参数为模型名，缺省 deepseek-v4-flash
    model_name = args[1].strip() if len(args) >= 2 and args[1].strip() else "deepseek-v4-flash"

    # 写回 bot_config.toml
    import toml
    from pathlib import Path
    _cfg_path = Path(__file__).resolve().parent.parent / "config" / "bot_config.toml"
    data = toml.load(_cfg_path) if _cfg_path.exists() else {}
    m = data.setdefault("model", {}).setdefault("replyer_1", {})
    m["provider"] = label
    m["name"] = model_name
    _cfg_path.write_text(toml.dumps(data), encoding="utf-8")

    reload_config()
    logger.info("供应商切换: %s/%s → %s/%s (by %s)", cur_provider, cur_model, label, model_name, sender_name)
    return f"回复模型已切换为: {_PROVIDERS[target]['name']}\n(provider={label}, model={model_name})"


async def cmd_add_relation(args, user_id, group_id, sender_name, is_group, bot_qq):
    """添加用户关系（仅管理员私聊）"""
    roles = load_roles_config()
    admin_qq = roles["admin_qq"]
    if user_id != admin_qq:
        return format_lang("error.permission_denied")
    
    if len(args) < 3:
        return format_lang("relation.prompt_usage")
    
    qq_id = args[0]
    nick = args[1]
    relation = args[2].lower()
    
    if not qq_id.isdigit():
        return format_lang("relation.invalid_qq")
    if relation == "admin":
        return format_lang("relation.invalid_role")
    
    if "qq_name_map" not in roles:
        roles["qq_name_map"] = {}
    roles["qq_name_map"][qq_id] = nick
    
    if relation == "friend":
        if "friend_qqs" not in roles:
            roles["friend_qqs"] = []
        if int(qq_id) not in roles["friend_qqs"]:
            roles["friend_qqs"].append(int(qq_id))
    
    try:
        save_roles_config(roles)
        logger.info("关系更新: %s(%s) → %s 操作者=%s", qq_id, nick, relation, sender_name)
        return format_lang("relation.success", qq=qq_id, name=nick, role=relation)
    except Exception as e:
        logger.error("保存关系配置失败: %s", e)
        return format_lang("relation.save_fail", error=e)


async def cmd_reset_fav(args, user_id, group_id, sender_name, is_group, bot_qq):
    """重置好感度数据（仅管理员）"""
    logger.info("指令 .resetfav 触发 user=%d", user_id)
    
    # 权限检查
    roles = load_roles_config()
    if user_id != roles.get("admin_qq"):
        return format_lang("reload.permission_denied")
    
    import modules.fav as fav_module
    fav_file = str(fav_module.FAV_FILE)
    if not os.path.exists(fav_file):
        return format_lang("resetfav.empty")
    
    success = reset_all_fav()
    if success:
        logger.info("管理员 %s(%d) 重置了全部好感度", sender_name, user_id)
        return format_lang("resetfav.success")
    else:
        return format_lang("error.command_error")


# ─── 图片指令 ──────────────────────────────────────────────

# 图片 API 配置（可按需扩展）
_IMG_APIS = {
    "sfw": [
        "https://api.waifu.pics/sfw/waifu",
        "https://api.waifu.pics/sfw/neko",
        "https://api.waifu.pics/sfw/shinobu",
    ],
    "nsfw": [
        "https://api.waifu.pics/nsfw/waifu",
        "https://api.waifu.pics/nsfw/neko",
    ],
}

_IMG_TEMP_DIR = Path(__file__).resolve().parent.parent / "data" / "img_temp"


async def _download_and_send_image(
    api_url: str,
    group_id: int | None,
    user_id: int | None,
    is_group: bool,
    tag: str = "img",
):
    """
    从 API 获取随机图片 URL → 下载到本地临时目录 → 通过 CQ 码发送。

    设计决策：先下载到本地再用 `file:///` 路径发送，与天气/快递/更新日志
    卡片使用相同的发送方式。sender.py 会读取本地文件并上传到 KOOK。
    如果本地下载失败，则尝试直接用远程 URL 发送。
    """
    _IMG_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    image_url = ""  # 外层作用域保存，供 except 块 fallback 使用

    try:
        # Step 1: 从 API 获取图片 URL
        logger.info("[%s] 请求图片 API: %s", tag, api_url)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()
            image_url = data.get("url", "")
            if not image_url:
                logger.error("[%s] API 返回无 url 字段: %s", tag, data)
                return

        logger.info("[%s] 获取到图片 URL: %s...", tag, image_url[:80])

        # Step 2: 下载图片到本地
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
            img_bytes = img_resp.content

        # 从 URL 或 Content-Type 推断扩展名
        ext = ".jpg"
        content_type = img_resp.headers.get("content-type", "")
        if "png" in content_type:
            ext = ".png"
        elif "gif" in content_type:
            ext = ".gif"
        elif "webp" in content_type:
            ext = ".webp"
        else:
            # 从 URL 路径推断
            url_path = image_url.split("?")[0]
            if url_path.endswith(".png"):
                ext = ".png"
            elif url_path.endswith(".gif"):
                ext = ".gif"
            elif url_path.endswith(".webp"):
                ext = ".webp"

        import uuid
        local_filename = f"img_{tag}_{uuid.uuid4().hex[:8]}{ext}"
        local_path = _IMG_TEMP_DIR / local_filename
        local_path.write_bytes(img_bytes)
        logger.info("[%s] 图片已保存: %s (%d bytes)", tag, local_path.name, len(img_bytes))

        # Step 3: 构造 CQ 码并发送
        normalized = str(local_path).replace("\\", "/")
        cq_msg = f"[img:file:{normalized}]"

        if is_group and group_id:
            await send_group_msg(cq_msg, group_id)
        elif not is_group and user_id:
            await send_private_msg(cq_msg, user_id)

        logger.info("[%s] 图片已发送", tag)

    except httpx.HTTPStatusError as e:
        logger.warning("[%s] API 请求失败 (HTTP %d): %s", tag, e.response.status_code, e)
        fallback_url = image_url or api_url
        await _send_image_url_fallback(fallback_url, group_id, user_id, is_group, tag)
    except Exception as e:
        logger.error("[%s] 图片处理异常: %s", tag, e, exc_info=True)
        if image_url:
            await _send_image_url_fallback(image_url, group_id, user_id, is_group, tag)


async def _send_image_url_fallback(
    image_url: str,
    group_id: int | None,
    user_id: int | None,
    is_group: bool,
    tag: str = "img",
):
    """备用方案：直接用远程 URL 构造 CQ 码发送（sender.py 支持 HTTP URL）"""
    try:
        logger.info("[%s] 尝试备用方案: 直接发送远程 URL", tag)
        cq_msg = f"[img:{image_url}]"
        if is_group and group_id:
            await send_group_msg(cq_msg, group_id)
        elif not is_group and user_id:
            await send_private_msg(cq_msg, user_id)
        logger.info("[%s] 备用方案已发送", tag)
    except Exception as e2:
        logger.error("[%s] 备用方案也失败了: %s", tag, e2)
        # 最终兜底：纯文本提示
        fallback = format_lang("image.service_error")
        if is_group and group_id:
            await send_group_msg(fallback, group_id)
        elif not is_group and user_id:
            await send_private_msg(fallback, user_id)


async def cmd_img(args, user_id, group_id, sender_name, is_group, bot_qq):
    """随机二次元图片 .img"""
    logger.info("指令 .img 触发 user=%d group=%d", user_id, group_id or 0)

    api_url = random.choice(_IMG_APIS["sfw"])

    async def _bg_send():
        await _download_and_send_image(
            api_url,
            group_id=group_id if is_group else None,
            user_id=user_id if not is_group else None,
            is_group=is_group,
            tag="img",
        )

    asyncio.create_task(_bg_send())
    return None  # 后台异步处理


async def cmd_img18(args, user_id, group_id, sender_name, is_group, bot_qq):
    """轻度 R18 图片 .img18"""
    logger.info("指令 .img18 触发 user=%d group=%d", user_id, group_id or 0)

    api_url = random.choice(_IMG_APIS["nsfw"])

    async def _bg_send():
        await _download_and_send_image(
            api_url,
            group_id=group_id if is_group else None,
            user_id=user_id if not is_group else None,
            is_group=is_group,
            tag="img18",
        )

    asyncio.create_task(_bg_send())
    return None  # 后台异步处理

# ─── 提醒指令 ──────────────────────────────────────────────

async def cmd_remind(args, user_id, group_id, sender_name, is_group, bot_qq):
    """设置定时提醒 .remind <时间> <内容>"""
    if not args or len(args) < 2:
        return format_lang("remind.usage")

    time_part = ""
    content_part = ""

    for i in range(len(args) - 1, 0, -1):
        candidate_time = " ".join(args[:i])
        from modules.remind import _parse_time
        ts, err = _parse_time(candidate_time)
        if err is None:
            time_part = candidate_time
            content_part = " ".join(args[i:])
            break

    if not time_part:
        return format_lang("remind.invalid_time")

    from modules.remind import _parse_time, add_reminder

    ts, err = _parse_time(time_part)
    if err:
        return err

    record = add_reminder(
        chat_id=group_id if is_group else user_id,
        user_id=user_id,
        target_time=ts,
        content=content_part,
        is_group=is_group,
    )

    from datetime import datetime
    target_dt = datetime.fromtimestamp(ts)
    time_display = target_dt.strftime("%m月%d日 %H:%M")
    logger.info("指令 .remind 触发: user=%d time=%s content='%s'",
               user_id, time_display, content_part[:30])
    return f"好的喵~ 我会在 {time_display} 提醒你「{content_part[:40]}」(๑•̀ㅂ•́)و✧"


# ─── 抽签指令 ──────────────────────────────────────────────

async def cmd_chou(args, user_id, group_id, sender_name, is_group, bot_qq):
    """随机抽取 .抽 选项A 选项B 选项C 或 .抽 选项A,选项B,选项C"""
    if not args:
        return format_lang("luck.prompt")

    full = " ".join(args)
    if "," in full:
        options = [o.strip() for o in full.split(",") if o.strip()]
    else:
        options = args

    if len(options) < 2:
        return format_lang("luck.min_options")

    pick = random.choice(options)
    logger.info("指令 .抽 触发: user=%d options=%s → '%s'", user_id, options, pick)

    reactions = [
        f"我帮你决定了喵~ 选「{pick}」！(。-`ω´-)✧",
        f"喵呜～ 命运指引着你走向「{pick}」！",
        f"尾巴晃了晃，指向了「{pick}」喵～",
        f"闭上眼睛默念三秒… 就决定是「{pick}」了！",
        f"不用纠结啦，当然是「{pick}」喵～",
    ]
    return random.choice(reactions)


# ─── Minecraft 日志分析（零上下文）─────────────────────────────

async def cmd_analyze(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    零上下文分析 .analyze <日志内容>
    直接发给 LLM，不注入任何聊天记录、记忆、架构上下文。
    """
    if not args:
        return "用法: .analyze <日志内容>\n直接粘贴 Minecraft 日志，零上下文分析。"
    
    content = " ".join(args)
    from services.llm import call_llm_raw
    prompt = (
        "你是一个 Minecraft 日志分析助手。请分析以下日志，找出问题、错误、警告，并给出修复建议。"
        "只基于当前提供的日志内容进行分析，不要引入任何外部知识或假设。\n\n"
        f"日志内容:\n{content}"
    )
    reply = await call_llm_raw(prompt)
    if not reply:
        return format_lang("analyze.fail")
    return reply


# ─── 提示词注入管理 ──────────────────────────────────────────

async def cmd_preset(args, user_id, group_id, sender_name, is_group, bot_qq):
    """查看或清除系统提示词注入 .preset [show|clear]"""
    from modules.preset import get_preset, clear_preset

    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return format_lang("preset.permission")

    action = args[0].lower() if args else "show"
    chat_id = group_id if is_group else user_id

    if action == "clear":
        cleared = clear_preset(chat_id)
        logger.info("指令 .preset clear 用户=%d cleared=%s", user_id, cleared)
        return format_lang("preset.cleared") if cleared else format_lang("preset.no_preset")

    preset = get_preset(chat_id)
    if preset:
        return f"【当前注入的提示词】({len(preset)}字)\n{preset[:500]}"
    else:
        return format_lang("preset.empty")


# ─── 五子棋 ─────────────────────────────────────────────────

async def _wzq_render_and_send(chat_id: int, is_group: bool, group_id: int, user_id: int, cfg):
    """背景生成棋盘卡片并发送"""
    from modules import wzq as g
    img = await g.render_board(chat_id, cfg)
    if img:
        cq = f"[img:file:{img.replace(chr(92), '/')}]"
        await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))


async def cmd_wzq(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    五子棋 .wzq <操作>
      duel @某人     发起挑战
      ai <难度>     人机对战
      accept         接受挑战
      cancel/unduel  取消挑战
      <坐标>         落子 (如 H8, 8,8)
      board          查看棋盘
      surrender      认输
      undo           申请悔棋
      status         查看状态
      admin clear    强制清除
    """
    chat_id = group_id if is_group else user_id
    from modules import wzq as g

    if not args:
        game = g.get_game(chat_id)
        if game and game.status == "playing":
            args = ["board"]
        else:
            return (
                "五子棋 .wzq\n"
                "  duel @某人 [nofb] 发起挑战(可选无禁手)\n"
                "  ai 新手/普通/困难/专家 [nofb]\n"
                "  accept      接受挑战\n"
                "  cancel/unduel 取消未接受的挑战\n"
                "  <坐标>      落子 (H8 / 8,8)\n"
                "  board       查看棋盘\n"
                "  surrender   认输\n"
                "  undo        悔棋\n"
                "  status      对局信息\n"
                "  admin clear 强制结束本群棋局(仅主人)"
            )

    action = args[0].lower()
    cfg = get_config()

    # ── 人机对战 ──
    if action in ("ai", "人机"):
        if len(args) < 2:
            return "用法: .wzq ai <难度>\n难度: 新手 普通 困难 专家"
        diff = args[1]
        diff_map = {"新手": "新手", "普通": "普通", "困难": "困难", "专家": "专家", "easy": "新手", "normal": "普通", "hard": "困难", "expert": "专家"}
        diff = diff_map.get(diff, diff)
        if diff not in ("新手", "普通", "困难", "专家"):
            return f"未知难度: {diff}\n可选: 新手 普通 困难 专家"
        nofb = args[-1].lower() in ("nofb", "无禁手", "noforbidden")
        result = g.create_duel_ai(chat_id, user_id, diff, forbidden=not nofb)
        if result.startswith("started_ai"):
            diff_name = result.split(":")[1]
            async def _bg_ai_start():
                img = await g.render_board(chat_id, cfg)
                if img:
                    cq = f"[img:file:{img.replace(chr(92), '/')}]"
                    await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            asyncio.create_task(_bg_ai_start())
            return format_lang("wzq.start", diff=diff_name)
        return result

    # ── 发起挑战 ──
    if action == "duel":
        if len(args) < 2:
            return "用法: .wzq duel @某人"

        # 从参数中提取 QQ 号（支持 @昵称 反向查映射）
        opponent = " ".join(args[1:])
        m = re.search(r'(\d{5,12})', opponent)
        if m:
            white_id = int(m.group(1))
        else:
            # 无数字 → 可能是 @昵称，反向查 roles.toml
            name = opponent.lstrip("@")
            found = None
            for qq, nick in cfg.qq_name_map.items():
                if nick == name:
                    found = int(qq)
                    break
            if found:
                white_id = found
            else:
                return format_lang("wzq.user_not_found", name=name)

        result = g.create_duel(chat_id, user_id, white_id,
                                forbidden=args[-1].lower() not in ("nofb", "无禁手", "noforbidden"))
        if result.startswith("waiting"):
            async def _bg_duel():
                import asyncio as _a
                await _a.sleep(0.5)
                img = await g.render_board(chat_id, cfg)
                if img:
                    cq = f"[img:file:{img.replace(chr(92), '/')}]"
                    await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            asyncio.create_task(_bg_duel())
            return f"挑战已发起！等待 (met){white_id}(met) 接受 (.wzq accept) 或拒绝 (.wzq decline)"
        return result

    # ── 接受 ──
    if action == "accept":
        result = g.accept_duel(chat_id, user_id)
        if result == "started":
            async def _bg():
                img = await g.render_board(chat_id, cfg)
                if img:
                    cq = f"[img:file:{img.replace(chr(92), '/')}]"
                    await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            asyncio.create_task(_bg())
            return None
        return result

    # ── 拒绝 ──
    if action == "decline":
        return g.decline_duel(chat_id, user_id)

    # ── 取消 ──
    if action in ("cancel", "unduel"):
        return g.cancel_duel(chat_id, user_id)

    # ★ admin 强制清除本群所有棋局
    if action == "admin":
        if not cfg.is_admin(user_id, group_id):
            return "只有主人才能用这个喵~"
        sub = args[1] if len(args) > 1 else ""
        if sub == "clear":
            game = g.get_game(chat_id)
            if not game:
                return "当前没有对局喵~"
            g.force_end(chat_id)
            return f"已强制结束本群棋局 (状态={game.status})"
        return "用法: .wzq admin clear  强制清除本群所有对局"

    # ── 落子 ──
    coord = g.parse_coord(action if len(args) == 1 else " ".join(args))
    if coord:
        row, col = coord
        ok, msg = g.make_move(chat_id, user_id, row, col)
        if ok:
            # ★ 玩家落子：先渲染发送棋盘，再让 AI 走（避免图片顺序颠倒）
            img = await g.render_board(chat_id, cfg)
            if img:
                cq = f"[img:file:{img.replace(chr(92), '/')}]"
                await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))

            if msg == "win":
                game = g.get_game(chat_id)
                winner_name = cfg.qq_name_map.get(str(game.white if game.winner == 2 else game.black), "?")
                return f"五连！{winner_name} 获胜！"
            if msg == "draw":
                return "棋盘满了，平局！"
            if msg.startswith("forbidden:"):
                reason = msg.split(":", 1)[1]
                game = g.get_game(chat_id)
                winner = cfg.qq_name_map.get(str(game.white), "白方")
                return f"禁手犯规({reason})！{winner} 获胜！"

            # AI 自动走棋
            game = g.get_game(chat_id)
            if game and game.ai_difficulty and game.turn == 2 and game.status == "playing":
                async def _ai_turn():
                    ai_ok, ai_msg = await g.ai_move_async(chat_id)
                    if ai_ok:
                        img = await g.render_board(chat_id, cfg)
                        if img:
                            cq = f"[img:file:{img.replace(chr(92), '/')}]"
                            await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
                        if ai_msg == "win":
                            ai_game = g.get_game(chat_id)
                            if ai_game.winner == 2:
                                await (send_group_msg("AI 获胜！", group_id) if is_group else send_private_msg("AI 获胜！", user_id))
                            elif ai_game.winner == 1:
                                await (send_group_msg("你赢了！", group_id) if is_group else send_private_msg("你赢了！", user_id))
                        elif ai_msg.startswith("forbidden:"):
                            await (send_group_msg("AI 禁手犯规，你赢了！", group_id) if is_group else send_private_msg("AI 禁手犯规，你赢了！", user_id))
                    else:
                        logger.warning("AI turn failed: %s", ai_msg)
                asyncio.create_task(_ai_turn())
            return None
        return msg

    # ── 棋盘 ──
    if action == "board":
        async def _bg():
            img = await g.render_board(chat_id, cfg)
            if img:
                cq = f"[img:file:{img.replace(chr(92), '/')}]"
                await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
        asyncio.create_task(_bg())
        return None

    # ── 认输 ──
    if action == "surrender":
        ok, msg = g.surrender(chat_id, user_id)
        if ok:
            loser = cfg.qq_name_map.get(str(user_id), str(user_id))
            return f"{loser} 认输了！"
        return msg

    # ── 悔棋 ──
    if action == "undo":
        game = g.get_game(chat_id)
        # ★ 如果对方已有悔棋申请，当前用户发送 undo = 确认悔棋
        if game and game.undo_request is not None and game.undo_request != user_id:
            ok, msg_res = g.confirm_undo(chat_id, user_id)
            if ok:
                async def _bg_undo():
                    img = await g.render_board(chat_id, cfg)
                    if img:
                        cq = f"[img:file:{img.replace(chr(92), '/')}]"
                        await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
                asyncio.create_task(_bg_undo())
                return msg_res
            return msg_res

        result = g.request_undo(chat_id, user_id)
        if result == "undo_request":
            opponent = game.white if user_id == game.black else game.black
            return f"悔棋申请已发送，等待 (met){opponent}(met) 同意 (.wzq undo)"
        return result

    # ── 状态 ──
    if action == "status":
        game = g.get_game(chat_id)
        if not game:
            return format_lang("wzq.no_game")
        bn = cfg.qq_name_map.get(str(game.black), str(game.black))
        wn = cfg.qq_name_map.get(str(game.white), str(game.white))
        return f"黑方 {bn} vs 白方 {wn} | 手数 {game.move_count} | 状态 {game.status}"

    # ── 历史记录 ──
    if action == "history":
        # 参数: [数量] [board] — board 渲染指定编号的棋盘
        count = 5; show_board_idx = 0
        for a in args[1:]:
            if a.lower() in ("board", "棋盘", "b"):
                if show_board_idx == 0:
                    show_board_idx = 1  # 默认第1局
            elif a.isdigit():
                n = min(int(a), 20)
                if show_board_idx == 0:
                    count = n
                else:
                    show_board_idx = n  # 指定第N局

        records = g.get_history(chat_id, max(count, show_board_idx))
        if not records:
            return format_lang("wzq.no_records")

        # 渲染特定对局的棋盘
        if show_board_idx > 0:
            idx = show_board_idx - 1
            if idx >= len(records):
                return format_lang("wzq.only_n_records", count=len(records))
            async def _bg_hist():
                img = await g.render_history_board(records[idx], cfg)
                if img:
                    cq = f"[img:file:{img.replace(chr(92), '/')}]"
                    await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            asyncio.create_task(_bg_hist())
            return None

        # 文字列表
        lines = [f"五子棋记录 (最近{min(count, len(records))}场)"]
        lines.append("  .wzq history <编号> board 查看棋盘")
        for i, r in enumerate(records[:count], 1):
            bn = cfg.qq_name_map.get(str(r["black"]), str(r["black"]))
            wn = cfg.qq_name_map.get(str(r["white"]), str(r["white"]))
            if r["winner"] == 1:
                result = f"{bn} 胜"
            elif r["winner"] == 2:
                result = f"{wn} 胜"
            else:
                result = "平局"
            lines.append(f"  {i}. [{r['time']}] {bn} vs {wn} -> {result} ({r['moves']}手)")
        return "\n".join(lines)

    # ── 测试渲染 ──
    if action == "test":
        async def _bg_test():
            img = await g.render_test_board(cfg)
            if img:
                cq = f"[img:file:{img.replace(chr(92), '/')}]"
                await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
        asyncio.create_task(_bg_test())
        return None

    return f"未知操作: {action}\n使用 .wzq 查看帮助"


# ─── 翻译 ───────────────────────────────────────────────────

async def cmd_translate(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    翻译 .tr <目标语言> <文本>
      .tr en 你好
      .tr 中文 hello
      .tr jp こんにちは
    """
    if len(args) < 2:
        return "用法: .tr <目标语言> <文本>\n例如: .tr en 你好\n     .tr 中文 hello"

    # 语言别名映射
    LANG_MAP = {
        "en": "English", "英文": "English", "英语": "English", "english": "English",
        "zh": "Chinese", "中文": "Chinese", "汉语": "Chinese", "chinese": "Chinese",
        "jp": "Japanese", "ja": "Japanese", "日文": "Japanese", "日语": "Japanese", "japanese": "Japanese",
        "kr": "Korean", "ko": "Korean", "韩文": "Korean", "韩语": "Korean", "korean": "Korean",
        "fr": "French", "法文": "French", "法语": "French", "french": "French",
        "de": "German", "德文": "German", "德语": "German", "german": "German",
    }
    target = args[0].lower()
    lang = LANG_MAP.get(target)
    if not lang:
        return f"不支持的语言: {target}\n支持: en/zh/jp/kr/fr/de 或 中文/英文/日文/韩文..."

    text = " ".join(args[1:])

    logger.info("翻译: %s → %s, text='%s'", text[:30], lang, text[:50])

    # 用廉价模型做翻译
    from services.llm import call_llm
    try:
        result = await call_llm(
            model_cfg=get_config().judge_model,
            messages=[{
                "role": "user",
                "content": f"Translate the following text to {lang}. Output ONLY the translation, no explanation, no quotation marks.\n\n{text}",
            }],
            max_tokens=1200,
            temperature=0.1,
        )
        translation = result.strip().strip('"').strip("'").strip()
        return f"[{lang}] {translation}"
    except Exception as e:
        logger.error("翻译失败: %s", e)
        return format_lang("translate.fail")


# ─── 中国象棋 ────────────────────────────────────────────────

async def cmd_xq(args, user_id, group_id, sender_name, is_group, bot_qq):
    """中国象棋 .xq [start|走法|board|resign|history]"""
    if not is_group:
        return "象棋对战仅支持群聊喵~"

    from modules.chinese_chess import (
        start_game, make_move, resign_game, show_board, show_history,
        _build_svg, _svg_to_png, _ROOT, INIT_BOARD,
    )
    from services.sender import send_group_msg

    if not args:
        return (
            "中国象棋 .xq <操作>\n"
            "  start        开始新对局\n"
            "  <走法>       炮二平五 / h2e2\n"
            "  board        查看棋盘\n"
            "  resign       认输\n"
            "  history      走棋记录"
        )

    action = args[0].lower()

    if action == "start":
        result = start_game(user_id, group_id)
        if result != "ok":
            return result
        # 渲染初始棋盘
        try:
            svg = _build_svg(INIT_BOARD)
            out = str(_ROOT / "data" / "img_temp" / f"xq_{group_id}.png")
            await _svg_to_png(svg, out)
            cq = f"[img:file:{out.replace(chr(92), '/')}]"
            await send_group_msg("对局开始！你执红方，请落子喵~\n" + cq, group_id)
            return None
        except Exception as e:
            return f"棋盘渲染失败喵: {e}"

    if action == "board":
        msg, img = show_board(group_id)
        if img:
            cq = f"[img:file:{img.replace(chr(92), '/')}]"
            await send_group_msg(cq, group_id)
        return msg

    if action == "resign":
        return resign_game(user_id, group_id)

    if action == "history":
        return show_history(group_id)

    # 走棋
    notation = " ".join(args)
    ok, msg, img = make_move(user_id, group_id, notation)
    if img:
        try:
            cq = f"[img:file:{img.replace(chr(92), '/')}]"
            await send_group_msg(cq, group_id)
        except Exception:
            pass
    return msg


# ─── 倒计时 ─────────────────────────────────────────────────

_COUNTDOWN_FILE = Path(__file__).resolve().parent.parent / "data" / "countdown.json"


def _load_countdowns() -> list[dict]:
    if _COUNTDOWN_FILE.exists():
        try:
            return json.loads(_COUNTDOWN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_countdowns(data: list[dict]):
    _COUNTDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COUNTDOWN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def cmd_countdown(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    倒计时 .countdown
      add <日期> <事件>     添加倒计时
      list                  查看所有
      del <编号>            删除
      无参数                 显示最近的
    """
    import datetime as _dt

    if not args:
        items = _load_countdowns()
        if not items:
            return format_lang("countdown.empty_list")
        lines = ["【倒计时】"]
        now = _dt.datetime.now()
        for i, item in enumerate(items, 1):
            try:
                target = _dt.datetime.strptime(item["date"], "%Y-%m-%d")
                days = (target - now).days
                sign = "已过" if days < 0 else "还有"
                days_str = f"{sign} {abs(days)} 天"
            except Exception:
                days_str = "?"
            lines.append(f"  {i}. [{item['date']}] {item['event']}  ({days_str})")
        return "\n".join(lines)

    action = args[0].lower()

    if action == "list":
        items = _load_countdowns()
        if not items:
            return format_lang("countdown.empty_single")
        lines = ["【倒计时列表】"]
        now = _dt.datetime.now()
        for i, item in enumerate(items, 1):
            try:
                target = _dt.datetime.strptime(item["date"], "%Y-%m-%d")
                days = (target - now).days
                sign = "已过" if days < 0 else "还有"
                days_str = f"{sign} {abs(days)} 天"
            except Exception:
                days_str = "?"
            lines.append(f"  {i}. [{item['date']}] {item['event']}  ({days_str})")
        return "\n".join(lines)

    if action == "del" or action == "delete":
        if len(args) < 2:
            return "用法: .countdown del <编号>"
        try:
            idx = int(args[1]) - 1
        except ValueError:
            return format_lang("countdown.delete_invalid_id")
        items = _load_countdowns()
        if idx < 0 or idx >= len(items):
            return format_lang("countdown.delete_out_of_range")
        removed = items.pop(idx)
        _save_countdowns(items)
        return f"已删除: [{removed['date']}] {removed['event']}"

    if action == "add":
        if len(args) < 3:
            return "用法: .countdown add <日期> <事件>\n例如: .countdown add 2026-12-25 圣诞节"
        date_str = args[1]
        event = " ".join(args[2:])
    else:
        # 快捷模式: .countdown 2026-12-25 圣诞节
        date_str = args[0]
        event = " ".join(args[1:])

    # 验证日期
    try:
        _dt.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"日期格式错误: {date_str}\n请使用 YYYY-MM-DD 格式，如 2026-12-25"

    items = _load_countdowns()
    items.append({"date": date_str, "event": event})
    _save_countdowns(items)

    target = _dt.datetime.strptime(date_str, "%Y-%m-%d")
    days = (target - _dt.datetime.now()).days
    days_str = f"还有 {days} 天" if days >= 0 else f"已过 {abs(days)} 天"
    return f"已添加倒计时喵~ [{date_str}] {event} ({days_str})"



# --- 排行榜处理 ---

async def _handle_wdsj_lb(args, is_group, group_id, user_id):
    import os
    from services import wdsj_api as api
    from services.sender import send_group_msg, send_private_msg
    from core.config import get_config

    if not args or args[0].lower() in ("help",):
        return "\n".join(["排行榜用法: .wdsj lb <榜单名> [周期] [img]",
                          "  双词: bw win, kbw tnt, sw kill ...",
                          "  周期: alltime/month/week/day  末尾+img=卡片图片"])

    # 先发提示
    from utils.format_lang import format_lang
    tip = format_lang("wdsj.lb_searching")
    await (send_group_msg(tip, group_id) if is_group 
           else send_private_msg(tip, user_id))

    # 检测 img 模式
    want_img = args[-1].lower() in ("img", "pic", "card", "图片")
    lb_args = args[:-1] if want_img else args[:]
    # 支持双词简写
    if len(lb_args) >= 2:
        bid = api.resolve_board_shorthand(lb_args[0], lb_args[1])
        if bid:
            board_id = bid
            period = (lb_args[2] if len(lb_args) > 2 else "ALLTIME").upper()
        else:
            board_id = api.resolve_board(lb_args[0])
            period = (lb_args[1] if len(lb_args) > 1 else "ALLTIME").upper()
    elif lb_args:
        board_id = api.resolve_board(lb_args[0])
        period = "ALLTIME"
    else:
        return "用法: .wdsj lb <榜单名> [周期] [img]"
    board_id = board_id or lb_args[0]
    period = period if period in api.PERIOD_LABELS else {
        "MONTH": "MONTHLY", "WEEK": "WEEKLY", "DAY": "DAILY", "ALL": "ALLTIME"
    }.get(period, "ALLTIME")

    data = await api.query_leaderboard(board_id, period)
    if not data:
        return format_lang("wdsj.lb_fail", board=board_id)

    # 图片模式
    if want_img:
        cfg = get_config()
        html = api.build_leaderboard_html(data, cfg.bot_name)
        from modules.changelog import _ensure_browser
        from pathlib import Path as _Path

        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"wdsj_lb_{board_id.replace(chr(47),'_')}_{ts}.png"
        out = str(_Path(__file__).resolve().parent.parent / "data" / "img_temp" / fname)
        try:
            browser = await _ensure_browser()
            page = await browser.new_page(viewport={"width": 440, "height": 600})
            await page.set_content(html)
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=out, full_page=True)
            await page.close()
            cq = f"[img:file:{out.replace(chr(92), '/')}]"
            await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            return None
        except Exception as e:
            return f"排行榜卡片渲染失败: {e}"

    # 文字模式
    board = data["board"]
    entries = data.get("entries", [])
    period_label = api.PERIOD_LABELS.get(data.get("type", "ALLTIME"), "总榜")
    lines = [f'{board.get("group","")} {board.get("displayName", board_id)} ({period_label})', ""]
    for e in entries:
        lines.append(f'  #{e["rank"]:>3}  {e["owner"]:<16} {e["value"]} {board.get("unit","")}')
    if not entries:
        lines.append("  (暂无数据)")
    return "\n".join(lines)


# ─── 洛花星雨战绩查询 ────────────────────────────────────────

_WDSJ_PLAYER_FILE = Path(__file__).resolve().parent.parent / "data" / "wdsj_player_name.json"


def _load_wdsj_bindings() -> dict:
    if _WDSJ_PLAYER_FILE.exists():
        try:
            return json.loads(_WDSJ_PLAYER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_wdsj_bindings(data: dict):
    _WDSJ_PLAYER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WDSJ_PLAYER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_bound_player(user_id: int) -> str | None:
    bindings = _load_wdsj_bindings()
    return bindings.get(str(user_id))


async def _handle_wdsj_bind(args: list, user_id: int) -> str:
    if not args:
        player = _get_bound_player(user_id)
        if player:
            return f"你当前绑定的玩家名是「{player}」喵~"
        return "你还没有绑定玩家名喵~ 试试 .wdsj bd <玩家名>"
    player = args[0]
    bindings = _load_wdsj_bindings()
    bindings[str(user_id)] = player
    _save_wdsj_bindings(bindings)
    return f"已绑定玩家名「{player}」喵~ 以后查战绩可以直接 .wdsj bw 不带名字啦"


# ─── 洛花星雨战绩查询 ──────────────────────────────────────

def _build_wdsj_summary(data: dict) -> str:
    """从 WDSJ API 返回中提取简短摘要"""
    pi = data.get("player", {})
    display = data.get("_template_display", "未知模式")
    hdr = data.get("headerCards", [])
    lines = [f"{pi.get('name', '?')} 的 {display} 战绩"]
    for c in hdr[:6]:
        v = c["value"]
        if c.get("hint"):
            v = f"{v} ({c['hint']})"
        lines.append(f"  {c['label']}: {v}")
    return "\n".join(lines)


async def cmd_wdsj(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    洛花星雨战绩查询 .wdsj <模板> <玩家名> [img]
    别名: bw=起床战争 kbw=击退战场 sw=空岛战争 kp=职业战争 ...
    """
    from services import wdsj_api as api
    from services.sender import send_group_msg, send_private_msg

    if not args or args[0].lower() == "help":
        # 渲染 MD 帮助卡片图片
        try:
            md_path = str(Path(__file__).resolve().parent.parent / "data" / "wdsj_help.md")
            cfg = get_config()
            html = api.build_help_card_html(md_path, cfg.bot_name)
            from modules.changelog import _ensure_browser
            browser = await _ensure_browser()
            page = await browser.new_page(viewport={"width": 520, "height": 100})
            await page.set_content(html)
            await page.wait_for_timeout(500)
            await page.set_viewport_size({"width": 520, "height": 100})
            out = str(Path(__file__).resolve().parent.parent / "data" / "img_temp" / f"wdsj_help_{int(time.time())}.png")
            await page.screenshot(path=out, full_page=True)
            await page.close()
            cq = f"[img:file:{out.replace(chr(92), '/')}]"
            await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            return None
        except Exception as e:
            logger.warning("wdsj help 卡片渲染失败: %s", e)
            return "\n".join([
                "洛花星雨战绩查询 .wdsj",
                "  <模式> <玩家> [img]     战绩",
                "  lb <榜> [周期] [img]   排行榜",
                "  boards                  简写速查",
                "  list                    模式别名",
                "简写: bw/kbw/sw/kp 周期: all/month/week/day"
            ])

    action = args[0].lower()

    # ★ 手动采集（仅 admin）
    if action == "collect":
        from core.config import get_config as _get_cfg
        if not _get_cfg().is_admin(user_id, group_id):
            return "只有管理员才能手动采集喵~"
        from services.sender import send_group_msg as _sgm
        await _sgm("⏳ 手动采集开始，请稍候...", group_id)
        from services.wdsj_tracker import daily_stats_collect
        import time as _t
        t0 = _t.time()
        await daily_stats_collect()
        elapsed = _t.time() - t0
        return f"✅ 手动采集完成 ({elapsed:.1f}s)"

    # 绑定玩家名
    if action in ("bd", "bind", "绑定"):
        if args[1:] and args[1] in ("list", "列表"):
            bindings = _load_wdsj_bindings()
            if not bindings:
                return "还没有人绑定喵~"
            lines = ["绑定玩家列表:"]
            for qq, name in sorted(bindings.items(), key=lambda x: x[1].lower()):
                lines.append(f"  {name}")
            return "\n".join(lines)
        return await _handle_wdsj_bind(args[1:], user_id)

    # 排行榜分支
    if action in ("lb", "排行榜", "ldb"):
        return await _handle_wdsj_lb(args[1:], is_group, group_id, user_id)

    if action in ("boards", "榜单", "榜"):
        lines = ["洛花星雨排行榜 简写速查 (用法: .wdsj lb <简写> [周期] [img])"]
        lines.append("")
        # 按游戏分组的简写
        groups = {
            "起床战争(bw)": [("win","胜"),("kill","击杀"),("beds","摧床"),("fk","最终"),("1k","首杀")],
            "击退战场(kbw)": [("kill","击杀"),("dead","死亡"),("tnt","TNT"),("arrow","弓箭"),("rod","鱼竿"),("jp","跳板")],
            "空岛战争(sw)": [("kill","击杀"),("win","胜"),("dead","死亡"),("1k","首杀")],
            "职业战争(kp)": [("kill","击杀"),("xp","经验")],
            "其他": [("pt","在线"),("cp","情侣"),("title","称号"),("guild","公会"),("dg win","画猜"),("cw win","色盲"),("cw kill","色盲杀"),("has win","躲猫猫")],
        }
        for group, items in groups.items():
            suffix = " " + group
            parts = []
            for short, name in items:
                parts.append(f"{short}={name}")
            lines.append(f"{suffix}: {', '.join(parts)}")
        return "\n".join(lines)

    if action == "list":
        lines = ["洛花星雨 13种游戏模式 (简写 => 全名)"]
        for tid, name in api.TEMPLATES.items():
            alias = [a for a, t in api.ALIASES.items() if t == tid]
            a_str = alias[0] if alias else tid
            lines.append(f"  {a_str} = {tid} ({name})")
        lines.append("")
        lines.append("排行榜别名: .wdsj lb <简写> [周期]")
        lines.append("  双词: 游戏名 指标 (如: bw win, kbw tnt, sw kill)")
        for a, bid in sorted(api.BOARD_ALIASES.items()):
            lines.append(f"  {a:6s} = {bid}")
        return "\n".join(lines)

    # ★ 群内排名
    if action in ("rank", "排名", "群排名"):
        mode_key = (args[1] if len(args) > 1 else "bw_kills").strip().lower()
        from services.wdsj_tracker import build_group_rank
        tip = f"正在生成绑定排行 ({mode_key}) 喵..."
        await (send_group_msg(tip, group_id) if is_group
               else send_private_msg(tip, user_id))
        text_result, image_path = await build_group_rank(group_id, mode_key)
        if image_path:
            cq = f"[img:file:{image_path.replace(chr(92), '/')}]"
            await (send_group_msg(cq, group_id) if is_group
                   else send_private_msg(cq, user_id))
            return None
        return text_result

    # ★ 今日击杀排名（图片渲染）
    # ★ 日榜 (默认起床，/wdsj daily are = 竞技场)
    if action in ("daily", "今日", "今天"):
        from services.wdsj_tracker import build_daily_rankings, build_arena_daily_rankings
        from datetime import datetime, date as _date
        import re

        mode = "bw"
        label_date = None
        for a in args[1:]:
            a_lower = a.lower().strip()
            if a_lower in ("are", "arena", "竞技", "竞技场"):
                mode = "are"
            elif re.match(r'^\d{1,2}[-/]\d{1,2}$', a):  # 7-20 or 07/20
                m = re.match(r'^(\d{1,2})[/-](\d{1,2})$', a)
                label_date = f"{datetime.now().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            elif re.match(r'^\d{4}-\d{2}-\d{2}$', a):  # 2026-07-20
                label_date = a
            elif re.match(r'^\d{4}$', a):  # 0720
                label_date = f"{datetime.now().year}-{int(a[:2]):02d}-{int(a[2:]):02d}"

        if mode == "are":
            rows, today, time_start, time_end = build_arena_daily_rankings(label_date=label_date)
        else:
            from datetime import date as _date, timedelta as _td
            # ★ push 模式强制用跨天（昨天 0:01 → 今天 0:01）
            is_push = len(args) >= 2 and args[-1].lower() in ("send", "push", "推送", "发送")
            if is_push and not label_date:
                label_date = (_date.today() - _td(days=1)).isoformat()
            # ★ 查询过去日期时自动用跨天模式
            use_cross = label_date and label_date != _date.today().isoformat()
            rows, today, new_players, time_start, time_end = build_daily_rankings(
                label_date=label_date, cross_day=use_cross)

            if not rows:
                now = datetime.now()
                label = today or "该日期"
                if today == _date.today().isoformat():
                    next_hour = (now.hour // 4 + 1) * 4
                    if next_hour >= 24:
                        next_hour = 0
                    next_time = f"{next_hour:02d}:01"
                    hint = f"，下一轮 {next_time}"
                else:
                    hint = ""
                return f"{label}还没有数据喵~ 榜单将在每天 0:01 / 4:01 / 8:01 / 12:01 / 16:01 / 20:01 产出{hint}"

        if mode == "are":
            html = _build_arena_daily_html(rows, today, time_start, time_end)
            prefix = "wdsj_arena"
        else:
            html = _build_daily_rank_html(rows, today, new_players, time_start, time_end)
            prefix = "wdsj_daily"

        out_path = await _render_html_to_png(html, prefix)
        if not out_path:
            return "排名卡片渲染失败喵~"
        cq = f"[img:file:{out_path.replace(chr(92), chr(47))}]"

        # ★ 推送模式：发到当前群（仅 is_group 时）
        if len(args) >= 2 and args[-1].lower() in ("send", "push", "推送", "发送"):
            if not is_group:
                return "send 模式仅在群聊中可用喵~"
            await send_group_msg(cq, group_id)
            return None

        await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
        return None

    # ★ 折线图
    if action in ("trend", "趋势", "走势", "折线"):
        from services.wdsj_tracker import generate_trend_chart
        player = None
        metric = "bw_kills"
        for a in args[1:]:
            a_lower = a.lower().strip()
            if a_lower in ("bw_kills", "bw_wins", "bw_finals", "bw_deaths", "arena_kills"):
                metric = a_lower
            else:
                player = a
        if not player:
            player = _get_bound_player(user_id)
        if not player:
            return "用法: .wdsj trend <玩家名> [指标]\n未绑定玩家名时试试 .wdsj bd <玩家名> 先绑定喵~"
        
        label = {"bw_kills":"击杀","bw_wins":"胜场","bw_finals":"最终击杀","bw_deaths":"死亡","arena_kills":"竞技场击杀"}.get(metric, metric)
        tip = f"正在生成 {player} 的 {label} 趋势图喵..."
        await (send_group_msg(tip, group_id) if is_group
               else send_private_msg(tip, user_id))
        
        img = generate_trend_chart(player, metric)
        if img:
            cq = f"[img:file:{img.replace(chr(92), '/')}]"
            await (send_group_msg(cq, group_id) if is_group
                   else send_private_msg(cq, user_id))
            return None
        return f"{player} 的 {label} 数据不够喵~ 需要至少 2 天的记录才能画趋势图"

    if len(args) < 2:
        # 尝试从绑定中查找
        player = _get_bound_player(user_id)
        if player:
            args = [args[0], player] + (args[1:] if len(args) > 1 else [])
        if len(args) < 2:
            return "用法: .wdsj <模板> <玩家名> [img]\n未绑定玩家名时可用 .wdsj bd <玩家名> 绑定喵~"

    # 提取 img 标记（在绑定补齐之后）
    want_img = args[-1].lower() in ("img", "pic", "card", "图片")
    player_parts = args[1:-1] if want_img else args[1:]
    # 如果只有模板+img，没有玩家名，尝试绑定
    if not player_parts or all(p in ("img", "pic", "card", "图片") for p in player_parts):
        player = _get_bound_player(user_id)
        if player:
            player_parts = [player]
            want_img = args[-1].lower() in ("img", "pic", "card", "图片")
    player = " ".join(player_parts)
    if not player:
        return "用法: .wdsj <模板> <玩家名> [img]\n未绑定玩家名时可用 .wdsj bd <玩家名> 绑定喵~"

    template_id = api.resolve_template(action)
    if not template_id:
        return f"未知模式: {action}\n用 .wdsj list 查看所有模式"

    display = api.TEMPLATES.get(template_id, template_id)
    logger.info("查询wdsj战绩: player=%s template=%s img=%s", player, template_id, want_img)

    # 先发提示
    from utils.format_lang import format_lang
    tip = format_lang("wdsj.player_searching", player=player, template=display)
    await (send_group_msg(tip, group_id) if is_group 
           else send_private_msg(tip, user_id))

    data = await api.query_player_stats(player, template_id)
    if not data:
        return format_lang("wdsj.player_not_found", player=player, template=display)

    # 图片模式：下载官方 PNG
    if want_img:
        snapshot = data.get("snapshotKey", "")
        if not snapshot:
            return "官方图片尚未生成，试试不带 img 看文字数据"
        image_url = f"/api/v1/images/{snapshot}"

        # 下载到项目 img_temp 目录（sender.py 可访问）
        from pathlib import Path as _Path
        out_dir = _Path(__file__).resolve().parent.parent / "data" / "img_temp"
        out_dir.mkdir(parents=True, exist_ok=True)
        save_path = str(out_dir / f"wdsj_{snapshot}.png")

        ok = await api.download_stats_image(image_url, save_path)
        if ok:
            cq = f"[img:file:{save_path.replace(chr(92), '/')}]"
            await (send_group_msg(cq, group_id) if is_group else send_private_msg(cq, user_id))
            # ★ 缓存数据：引用此图时直接返回文字数据，不调视觉模型
            from services.wdsj_cache import store as wdsj_store
            data["_template_display"] = display
            summary = _build_wdsj_summary(data)
            wdsj_store(snapshot, player, template_id, summary)
            return None
        else:
            return format_lang("wdsj.img_download_fail")

    # 文字模式：完整数据
    pi = data.get("player", {})
    vals = data.get("values", {})
    labels = data.get("labels", {})
    hdr = data.get("headerCards", [])
    smr = data.get("summaryCards", [])

    lines = [f"{pi.get('name', '?')} 的 {display} 战绩"]

    for c in hdr:
        v = c["value"]
        if c.get("hint"): v = f"{v} ({c['hint']})"
        lines.append(f"  {c['label']}: {v}")

    if smr:
        lines.append("")
        for c in smr:
            lines.append(f"  {c['label']}: {c['value']}")

    if vals:
        lines.append("")
        for k, v in vals.items():
            label = labels.get(k, k)
            lines.append(f"  {label}: {v}")

        return "\n".join(lines)

# ─── .owner 卡片渲染 ───────────────────────────────────

def _build_owner_help_md() -> str:
    return (
        "**.owner** — 配置管理（仅主人可用）\n\n"
        "---\n\n"
        "### 配置文件\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner list <域>` | 列出配置：`bot` / `adapter` / `roles` |\n"
        "| `.owner get <路径>` | 读取：`get bot.reply_threshold` |\n"
        "| `.owner set <路径> <值>` | 设置：`set bot.reply_threshold 5` |\n"
        "\n"
        "### 数据文件\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner data get <名> [键]` | 读取：`data get fav` 或 `data get fav g_123_456` |\n"
        "| `.owner data set <名> <键> <值>` | 设置：`data set fav g_123_456 100` |\n"
        "| `.owner data del <名> <键>` | 删除某条记录 |\n"
        "| `.owner data reset <名\\|all>` | 重置：`data reset fav` / `data reset all` |\n"
        "| 可用数据：`fav` `luck` `countdown` `reminders` `recall` `wzq` |\n"
        "\n"
        "### 快捷白名单\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner wl add group <群号>` | 添加群白名单 |\n"
        "| `.owner wl add private <QQ>` | 添加私聊白名单 |\n"
        "| `.owner wl remove group <群号>` | 移除群白名单 |\n"
        "| `.owner wl remove private <QQ>` | 移除私聊白名单 |\n"
        "| `.owner wl show` | 查看白名单 |\n"
        "| `.owner wl welcome set <群> <语>` | 设置入群欢迎语 |\n"
        "| `.owner wl welcome del <群>` | 删除入群欢迎语 |\n"
        "\n"
        "### 运气管理\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner luck list` | 查看今日全员运气值 |\n"
        "| `.owner luck set <QQ> <0~100>` | 修改某人的运气值 |\n"
        "| `.owner luck del <QQ>` | 删除某人的运气记录 |\n"
        "\n"
        "### 画图 / 视频配额\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner draw get [QQ]` | 查看画图用量，不填 QQ 看全部 |\n"
        "| `.owner draw set <QQ> <N>` | 设每日画图上限（默认 10）|\n"
        "| `.owner draw reset` | 重置今日画图用量 |\n"
        "| `.owner video get\\|set\\|reset` | 视频配额管理（默认 4）|\n"
        "\n"
        "### 指令白名单\n"
        "| 指令 | 说明 |\n"
        "|------|------|\n"
        "| `.owner cmd add <指令>` | 添加：`cmd add weather` |\n"
        "| `.owner cmd remove <指令>` | 移除 |\n"
        "| `.owner cmd list` | 查看白名单指令 |\n"
        "| `.owner cmd clear` | 清空白名单（所有指令仅主人可用）|\n"
    )


def _build_owner_help_html(md: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#1a1a2e;color:#e0e0e0;font-family:"Microsoft YaHei","PingFang SC",sans-serif;padding:28px;width:680px}}
.header{{background:linear-gradient(135deg,#f97316,#dc2626);color:#fff;border-radius:12px;padding:20px 24px;margin-bottom:20px}}
.header h1{{font-size:22px;margin-bottom:4px}}
.header .sub{{font-size:13px;opacity:.85}}
.section{{margin-bottom:18px}}
.section h3{{font-size:15px;color:#fb923c;border-left:3px solid #f97316;padding-left:10px;margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:rgba(249,115,22,.15);color:#fb923c;text-align:left;padding:6px 10px;font-weight:600}}
td{{padding:5px 10px;border-bottom:1px solid rgba(255,255,255,.06)}}
code{{background:rgba(249,115,22,.12);color:#fb923c;padding:1px 6px;border-radius:4px;font-size:12px}}
tr:hover td{{background:rgba(249,115,22,.06)}}
.footer{{text-align:center;font-size:11px;color:#666;margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,.08)}}
</style></head><body>
<div class="header"><h1>🤖 配置管理 .owner</h1><div class="sub">仅主人可用 | 幻梦 QQ Bot</div></div>
<div class="section"><h3>⚙️ 配置文件</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner list &lt;域&gt;</code></td><td>列出配置：<code>bot</code> / <code>adapter</code> / <code>roles</code></td></tr>
<tr><td><code>.owner get &lt;路径&gt;</code></td><td>读取配置值，如 <code>get bot.reply_threshold</code></td></tr>
<tr><td><code>.owner set &lt;路径&gt; &lt;值&gt;</code></td><td>设置配置值，如 <code>set bot.reply_threshold 5</code></td></tr>
</table></div>
<div class="section"><h3>📂 数据文件</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner data get &lt;名&gt; [键]</code></td><td>读取数据文件</td></tr>
<tr><td><code>.owner data set &lt;名&gt; &lt;键&gt; &lt;值&gt;</code></td><td>设置数据</td></tr>
<tr><td><code>.owner data del &lt;名&gt; &lt;键&gt;</code></td><td>删除记录</td></tr>
<tr><td><code>.owner data reset &lt;名|all&gt;</code></td><td>重置数据文件</td></tr>
<tr><td colspan="2">可用数据：<code>fav</code> <code>luck</code> <code>countdown</code> <code>reminders</code> <code>recall</code> <code>wzq</code></td></tr>
</table></div>
<div class="section"><h3>🛡️ 快捷白名单</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner wl add group &lt;群号&gt;</code></td><td>添加群白名单</td></tr>
<tr><td><code>.owner wl add private &lt;QQ&gt;</code></td><td>添加私聊白名单</td></tr>
<tr><td><code>.owner wl remove group &lt;群号&gt;</code></td><td>移除群白名单</td></tr>
<tr><td><code>.owner wl remove private &lt;QQ&gt;</code></td><td>移除私聊白名单</td></tr>
<tr><td><code>.owner wl show</code></td><td>查看白名单</td></tr>
<tr><td><code>.owner wl welcome set &lt;群&gt; &lt;语&gt;</code></td><td>设置入群欢迎语</td></tr>
<tr><td><code>.owner wl welcome del &lt;群&gt;</code></td><td>删除入群欢迎语</td></tr>
</table></div>
<div class="section"><h3>🍀 运气管理</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner luck list</code></td><td>查看今日全员运气值</td></tr>
<tr><td><code>.owner luck set &lt;QQ&gt; &lt;0~100&gt;</code></td><td>修改运气值</td></tr>
<tr><td><code>.owner luck del &lt;QQ&gt;</code></td><td>删除运气记录</td></tr>
</table></div>
<div class="section"><h3>🎨 画图 / 视频配额</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner draw get [QQ]</code></td><td>查看画图用量，不填 QQ 看全部</td></tr>
<tr><td><code>.owner draw set &lt;QQ&gt; &lt;N&gt;</code></td><td>设每日画图上限（默认 10）</td></tr>
<tr><td><code>.owner draw reset</code></td><td>重置今日画图用量</td></tr>
<tr><td><code>.owner video get|set|reset</code></td><td>视频配额管理（默认 4）</td></tr>
</table></div>
<div class="section"><h3>🔒 指令白名单</h3><table><tr><th>指令</th><th>说明</th></tr>
<tr><td><code>.owner cmd add &lt;指令&gt;</code></td><td>添加指令到白名单</td></tr>
<tr><td><code>.owner cmd remove &lt;指令&gt;</code></td><td>从白名单移除</td></tr>
<tr><td><code>.owner cmd list</code></td><td>查看白名单指令</td></tr>
<tr><td><code>.owner cmd clear</code></td><td>清空（仅主人可用所有指令）</td></tr>
</table></div>
<div class="footer">幻梦 QQ Bot | .owner 配置管理</div>
</body></html>"""

def _handle_owner_cmd(args):
    roles = load_roles_config()
    wl = roles.get("command_whitelist", [])
    if not isinstance(wl, list): wl = []
    if not args:
        return "~owner cmd add/remove/list/clear <指令名>"
    sub = args[0].lower()
    if sub in ("list", "show"):
        if not wl: return "指令白名单为空（所有指令可用）"
        return "指令白名单:\n  " + "\n  ".join(wl)
    if sub == "clear":
        roles["command_whitelist"] = []
        save_roles_config(roles)
        return "指令白名单已清空（所有指令可用）"
    if sub in ("add", "remove") and len(args) < 2:
        return f"~owner cmd {sub} <指令名>"
    cmd_name = args[1].lower().lstrip(".#")
    if sub == "add":
        if cmd_name in wl: return f"指令 {cmd_name} 已在白名单"
        wl.append(cmd_name)
        roles["command_whitelist"] = wl; save_roles_config(roles)
        return f"已添加: {cmd_name}"
    if sub == "remove":
        if cmd_name not in wl: return f"指令 {cmd_name} 不在白名单"
        wl.remove(cmd_name)
        roles["command_whitelist"] = wl; save_roles_config(roles)
        return f"已移除: {cmd_name}"
    return f"未知 cmd 子命令: {sub}"


# ─── 管理员 ──────────────────────────────────────────────────

async def cmd_owner(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    配置管理 .owner <action> ...
    
    config get/set/list  — 读写配置
    data get/set/del/reset — 管理数据文件
    wl add/remove        — 白名单快捷操作
    """
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return "权限不足喵~"

    if not args or (len(args) == 1 and args[0].lower() == "help"):
        md = _build_owner_help_md()
        from modules.changelog import render_card_to_image
        from services.sender import send_group_msg, send_private_msg
        import uuid

        html = _build_owner_help_html(md)

        filename = f"owner_{uuid.uuid4().hex[:8]}.jpg"
        img_path = await render_card_to_image(html, filename, width=680)
        if not img_path:
            return "卡片生成失败喵~"

        normalized = img_path.replace("\\", "/")
        cq = f"[img:file:{normalized}]"
        if is_group:
            await send_group_msg(cq, group_id)
        else:
            await send_private_msg(cq, user_id)
        return None

    from modules import admin

    action = args[0].lower()

    # ── config list ──
    if action == "list":
        section = args[1] if len(args) > 1 else "bot"
        return admin.config_list(section)

    # ── config get ──
    if action == "get":
        if len(args) < 2:
            return "用法: .owner get <路径>\n如: get bot.reply_threshold"
        return admin.config_get(args[1])

    # ── config set ──
    if action == "set":
        if len(args) < 3:
            return "用法: .owner set <路径> <值>\n如: set bot.reply_threshold 5"
        return admin.config_set(args[1], " ".join(args[2:]))

    # ── data 子命令 ──
    if action == "data":
        if len(args) < 3:
            return "用法: .owner data <get|set|del|reset> <名> [键] [值]"
        sub = args[1].lower()
        if sub == "get":
            key = args[3] if len(args) > 3 else ""
            # ★ fav key 自动纠正
            if args[2] == "fav" and key and key.isdigit():
                uid = int(key)
                key = f"g{group_id}:{uid}" if is_group else f"p:{uid}"
            return admin.data_get(args[2], key)
        if sub == "set":
            if len(args) < 5:
                return "用法: data set <名> <键> <值>"
            key = args[3]
            # ★ fav key 自动纠正
            if args[2] == "fav" and key.isdigit():
                uid = int(key)
                key = f"g{group_id}:{uid}" if is_group else f"p:{uid}"
                logger.info("fav key 自动纠正: %s → %s", args[3], key)
            return admin.data_set(args[2], key, " ".join(args[4:]))
        if sub == "del":
            key = args[3] if len(args) > 3 else "*"
            # ★ fav key 自动纠正
            if args[2] == "fav" and key != "*" and key.isdigit():
                uid = int(key)
                key = f"g{group_id}:{uid}" if is_group else f"p:{uid}"
            return admin.data_delete(args[2], key)
        if sub == "reset":
            return admin.data_reset(args[2])
        return f"未知 data 操作: {sub}"

    # ── luck 子命令 ──
    if action == "luck":
        sub = args[1].lower() if len(args) > 1 else "list"
        if sub == "list":
            return admin.luck_list()
        if sub == "set" and len(args) >= 4:
            return admin.luck_set(args[2], args[3])
        if sub == "del" and len(args) >= 3:
            return admin.luck_del(args[2])
        return "用法: luck list|set <QQ> <值>|del <QQ>"

    # ── files 子命令 ──
    if action == "files":
        path = args[1] if len(args) > 1 else "data/update_log.md"
        return admin.read_project_file(path)

    # ── cmd 子命令（指令白名单）──
    if action == "cmd":
        return _handle_owner_cmd(args[1:])

    # ── wl 子命令 ──
    if action == "wl":
        if len(args) < 2:
            return (
                "【白名单管理 .owner wl】\n"
                "  wl add group <群号> [atonly]  添加群白名单\n"
                "  wl remove group <群号>  移除群白名单\n"
                "  wl add private <QQ>    添加私聊白名单\n"
                "  wl remove private <QQ> 移除私聊白名单\n"
                "  wl show                查看所有白名单\n"
                "  wl cmd add|remove <群> <指令>  分群指令白名单\n"
                "  wl welcome set|del <群> [欢迎语]  入群欢迎语"
            )
        sub = args[1].lower()
        if sub == "cmd":
            return admin.wl_cmd_manage(args[2:])
        if sub == "welcome":
            return admin.wl_welcome_manage(args[2:])
        if sub == "show":
            return admin.show_whitelists()
        if sub == "add" and len(args) >= 4:
            result = admin.whitelist_add(args[2], int(args[3]))
            if len(args) >= 5 and args[4].lower() == "atonly" and args[2] == "group":
                admin.group_set(int(args[3]), "at_only", "true")
                result += "\n已设置 @仅模式"
            return result
        if sub == "remove" and len(args) >= 4:
            return admin.whitelist_remove(args[2], int(args[3]))
        if sub in ("gset", "set") and len(args) >= 4 and args[2].isdigit():
            gid = int(args[2])
            mode = args[3].lower()
            if mode in ("atonly", "aton", "@仅"):
                return admin.group_set(gid, "at_only", "true")
            if mode == "default":
                return admin.group_set(gid, "at_only", "false")
            return f"设置: group={gid} mode={mode}\n支持: atonly, default"
        return "格式: wl add|remove group|private <ID>\n      wl gset <群号> atonly|default\n例: wl add group 1058782600 atonly"

    # ── wdsj 推送群管理 ──
    if action == "wdsj":
        import toml
        from pathlib import Path
        _cfg_path = Path(__file__).resolve().parent.parent / "config" / "bot_config.toml"
        if len(args) < 2:
            return "用法: .owner wdsj groups <show|set|clear>\n  show  查看推送群\n  set 1058782600,247478659  设推送群\n  clear  清除(发全群)"
        sub = args[1].lower()
        if sub == "show" or sub == "groups":
            data = toml.load(_cfg_path) if _cfg_path.exists() else {}
            gs = data.get("wdsj", {}).get("target_groups", [])
            if gs:
                return f"WDSJ 日榜推送群: {', '.join(str(g) for g in gs)}"
            return "未设置推送群，凌晨自动发全群"
        if sub == "set":
            if len(args) < 3:
                return "用法: .owner wdsj groups set <群号1,群号2,...>"
            gs = [int(x.strip()) for x in args[2].split(",") if x.strip().isdigit()]
            data = toml.load(_cfg_path) if _cfg_path.exists() else {}
            data.setdefault("wdsj", {})["target_groups"] = gs
            _cfg_path.write_text(toml.dumps(data), encoding="utf-8")
            cfg.config["wdsj"] = data["wdsj"]  # 热更新内存
            return f"WDSJ 日榜推送群已设为: {', '.join(str(g) for g in gs)}"
        if sub == "clear":
            data = toml.load(_cfg_path) if _cfg_path.exists() else {}
            data.setdefault("wdsj", {}).pop("target_groups", None)
            _cfg_path.write_text(toml.dumps(data), encoding="utf-8")
            cfg.config["wdsj"] = data["wdsj"]
            return "已清除推送群限制，凌晨发全群"
        return f"未知: .owner wdsj {sub}"

    # ── draw/video 配额管理 ──
    if action in ("draw", "video"):
        from modules.agnes import owner_quota_get, owner_quota_set, owner_quota_reset
        if len(args) < 2:
            return f"用法: .owner {action} <get|set|reset> [参数]\n.owner {action} get [QQ]  查看用量\n.owner {action} set <QQ> <N>  设上限\n.owner {action} reset       重置"
        sub = args[1].lower()
        if sub == "get":
            qq = args[2] if len(args) > 2 else ""
            return owner_quota_get(action, qq)
        if sub == "set":
            if len(args) < 4:
                return f"用法: .owner {action} set <QQ> <N>"
            owner_quota_set(action, args[2], int(args[3]))
            return f"已设置 QQ {args[2]} 每日{action}上限为 {args[3]}"
        if sub == "reset":
            owner_quota_reset(action)
            return f"已重置今日{action}用量"
        return f"未知: .owner {action} {sub}"

    # ── 兼容旧指令 ──
    if action in ("glist", "gadd", "gremove", "padd", "premove", "gset"):
        return await _owner_legacy(args, action, admin)

    return f"未知操作: {action}\n.owner 查看帮助"


# ─── 记忆查询 ────────────────────────────────────────────────

async def cmd_memory(args, user_id, group_id, sender_name, is_group, bot_qq):
    """
    三层记忆查询 .memory [瞬时|短时|长时|搜索 <关键词>]
    """
    from modules import stm
    from modules.memory import read_long_memory, search_long_memory

    chat_id = group_id if is_group else user_id

    if not args:
        return (
            "【三层记忆 .memory】\n"
            "  working      瞬时记忆（当前对话上下文）\n"
            "  short        短时记忆（最近30条）\n"
            "  long         长时记忆（持久化）\n"
            "  search <关键词>  搜索所有记忆\n"
            "  clear        清空短时记忆"
        )

    action = args[0].lower()

    if action == "working":
        from core.context_manager import get_context
        ctx = get_context()
        msgs = ctx.get_history(chat_id, 15)
        if not msgs:
            return "瞬时记忆为空"
        lines = [f"【瞬时记忆 · 最近{len(msgs)}条】"]
        for m in msgs:
            lines.append(f"  [{m.get('tag','?')}] {m.get('sender','')}: {m.get('content','')[:80]}")
        return "\n".join(lines)

    if action in ("short", "stm", "短时"):
        return stm.summarize(chat_id)

    if action in ("long", "ltm", "长时"):
        return await read_long_memory(chat_id)

    if action == "search":
        if len(args) < 2:
            return "用法: .memory search <关键词>"
        kw = " ".join(args[1:])
        s = stm.search(chat_id, kw)
        l = await search_long_memory(chat_id, kw)
        parts = []
        if s:
            parts.append("【短时记忆匹配】")
            for e in s:
                t = __import__("time").strftime("%H:%M", __import__("time").localtime(e["time"]))
                parts.append(f"  [{t}] {e['content'][:100]}")
        if l:
            if parts: parts.append("")
            parts.append("【长时记忆匹配】")
            parts.append(l)
        return "\n".join(parts) if parts else f"未找到含「{kw}」的记忆"

    if action == "clear":
        return stm.clear(chat_id)

    return f"未知操作: {action}\n.memory 查看帮助"


async def _owner_legacy(args, action, admin):
    """兼容旧版 owner 子命令"""
    if action == "glist":
        return admin.show_whitelists()
    if action == "gadd" and len(args) >= 2 and args[1].isdigit():
        return admin.whitelist_add("group", int(args[1]))
    if action == "gremove" and len(args) >= 2 and args[1].isdigit():
        return admin.whitelist_remove("group", int(args[1]))
    if action == "padd" and len(args) >= 2 and args[1].isdigit():
        return admin.whitelist_add("private", int(args[1]))
    if action == "premove" and len(args) >= 2 and args[1].isdigit():
        return admin.whitelist_remove("private", int(args[1]))
    if action == "gset" and len(args) >= 2 and args[1].isdigit():
        gid = int(args[1])
        sub = args[2].lower() if len(args) > 2 else ""
        if not sub or sub == "show":
            return admin.config_get(f"adapter.group_settings.{gid}")
        if sub == "reply" and len(args) > 3 and args[3].isdigit():
            return admin.group_set(gid, "reply_threshold", args[3])
        if sub == "atonly":
            return admin.group_set(gid, "at_only", "true")
        if sub == "default":
            return admin.config_set(f"adapter.group_settings.{gid}", "null")
    return f"未知操作: {action}"

async def cmd_restart(args, user_id, group_id, sender_name, is_group, bot_qq):
    """远程重启 .restart"""
    from core.config import get_config
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id if is_group else 0):
        return "权限不足"
    from services.sender import send_group_msg, send_private_msg
    if is_group:
        await send_group_msg("正在重启喵～", group_id)
    else:
        await send_private_msg("正在重启喵～", user_id)
    import os
    os._exit(0)


# ════════════════════════════════════════════════════════════
#  指令注册表 & 分发器
# ════════════════════════════════════════════════════════════

async def cmd_lyric(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.lyric [on|off|status|offset] — 实时歌词推送管理 + 时间偏移远程下发
    offset 通过 PC 状态 TCP 连接下发 CMD:OFFSET_* 给客户端，即刻生效并持久化，无需手动改本地文件"""
    from services.pc_status import set_lyric_channel, get_lyric_channel, request_lyric_offset
    sub = (args[0].lower() if args else "").strip()

    # ══ 子命令 1：offset（远程 TCP 下发 CMD:OFFSET_*，客户端即刻生效，自动持久化）══
    if sub == "offset":
        rest = " ".join(args[1:]).strip().lower()
        # .lyric offset get
        if rest == "get":
            msg, _ms = await request_lyric_offset("GET")
            return msg
        # .lyric offset reset
        if rest == "reset":
            msg, _ms = await request_lyric_offset("RESET")
            return msg
        # .lyric offset ±<毫秒>
        if rest:
            try:
                # 兼容 .lyric offset -3000 / +1200 / 1500 / 3000ms
                val = int(rest.replace("ms", "").replace(" ", ""))
            except Exception:
                return (f"❌ offset 数值格式错误：`{rest}`\n"
                        "正确写法：.lyric offset -3000 （整数毫秒，负数=提前发送，正数=延后）")
            msg, _ms = await request_lyric_offset("SET", str(val))
            return msg
        # .lyric offset 无参数
        return ("ℹ️ `.lyric offset` 子命令用法：\n"
                "  .lyric offset ±<毫秒>   直接设置（TCP 下发，客户端即刻生效，负=提前，如 -3000 提前 3 秒）\n"
                "  .lyric offset get        查看客户端当前实际 offset\n"
                "  .lyric offset reset      重置 offset 为 0\n"
                "📦 新：offset 通过 PC 状态 TCP 连接直接下发到客户端，**无需改本地 JSON / 重启进程**，立刻生效并持久化。")

    # ══ 子命令 2：on ══
    if sub in ("on", "open", "1", "start", "开启"):
        if not is_group:
            return "实时歌词推送仅支持在频道内开启喵~"
        set_lyric_channel(str(group_id))
        logger.info("🎵 歌词推送开启: channel=%s user=%s(%d)", group_id, sender_name, user_id)
        return f"🎵 实时歌词推送已开启！当前频道收到歌词更新时会自动发出来喵~（停播10秒后自动关闭）"

    # ══ 子命令 3：off ══
    if sub in ("off", "close", "0", "stop", "关闭"):
        set_lyric_channel(None)
        logger.info("🎵 歌词推送关闭: user=%s(%d)", sender_name, user_id)
        return "实时歌词推送已关闭。"

    # ══ 子命令 4：空 / status（查询当前状态）══
    if sub == "" or sub in ("status", "state", "?", "查看", "状态"):
        current = get_lyric_channel()
        if current:
            return (f"📡 实时歌词推送状态：**已开启** → 频道 {current}\n"
                    f"用 `.lyric off` 关闭；用 `.lyric offset -3000` 查看提前量配置提示。")
        return ("📡 实时歌词推送状态：**未开启**\n"
                "在目标频道内发送 `.lyric on` 开启推送。\n"
                "📖 完整用法：.lyric [on | off | status | offset ±<毫秒> | offset get | offset reset]")

    # ══ 未知子命令：清晰报错，不再 fallback 成「开启/状态」避免误操作 ══
    return (f"❓ 未知子命令：`{sub}`\n"
            ".lyric 支持以下子命令：\n"
            "  频道开关: on / off / status\n"
            "  时间偏移: offset ±<毫秒>  |  offset get  |  offset reset\n"
            "提示：旧版 `.lyric` 直接带未知参数会被当成 on；现在必须显式写 on 才会开启频道。")


async def cmd_listening(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.listening <歌名> [-s 歌手] [-p 软件] [-o] — bash 风格 + 兼容旧式

    通过 KOOK API /api/v3/game/activity 设置 data_type=2 的音乐状态。
    用法（bash 风格 flag + 旧式位置参数都支持）：
      .listening 有点甜 汪苏泷、BY2           旧式：歌名 + 歌手
      .listening 有点甜 kugoumusic           旧式：歌名 + 软件（自动归一化别名）
      .listening -s 幻梦 -p kugoumusic 请输入文本
                                                bash 风格
      .listening -n 有点甜 --software qqmusic   bash 风格
      .listening off | -o                       结束"正在听"状态
      .listening | -q                           查看当前状态
    软件别名自动归一化（网易云/netease/163/cloudmusic、qq/qqmusic、酷狗/kugou/kugoumusic…）。
    """
    from services.music_status import set_music, clear_music, current_status, normalize_software

    parts = list(args or [])
    song, singer, software = "", "", ""
    off = show = False
    positional = []
    i = 0
    while i < len(parts):
        low = parts[i].lower()
        if low in ("-s", "--singer", "--artist") and i + 1 < len(parts):
            singer = parts[i + 1]; i += 2; continue
        if low in ("-p", "--software", "--soft", "--platform", "--source") and i + 1 < len(parts):
            software = parts[i + 1]; i += 2; continue
        if low in ("-n", "--name", "--song", "--title") and i + 1 < len(parts):
            song = parts[i + 1]; i += 2; continue
        if low in ("-o", "--off", "--stop", "--clear"):
            off = True; i += 1; continue
        if low in ("-q", "--status", "--state", "--show", "--query"):
            show = True; i += 1; continue
        if parts[i].startswith("-") and parts[i] != "-":
            i += 1; continue          # 未知 flag：忽略
        positional.append(parts[i]); i += 1

    # 旧式位置参数：歌名 [歌手] [软件]
    if not song and positional:
        song = positional[0]
    if not singer and len(positional) >= 2:
        singer = positional[1]
    if not software and len(positional) >= 3:
        # 第三位若能归一化为软件则录用，否则是多余的杂质，忽略
        if normalize_software(positional[2]) != positional[2].strip() \
                or positional[2].lower() in ("cloudmusic", "qqmusic", "kugou"):
            software = positional[2]

    only_one = len(positional) == 1 and not off and not singer and not software
    _so = song.strip().lower()

    # 结束
    if off or (only_one and _so in ("off", "close", "stop", "0", "结束", "关闭")):
        ok, msg = clear_music()
        return ("✅ 已结束" + ("正在听" if ok else f"（{msg}）") if ok
                else f"❌ 结束状态失败：{msg}")

    # 查看
    if show or (not song and not off) or (only_one and _so in ("status", "state", "?", "查看", "状态", "query")):
        cur = current_status()
        if not cur:
            return "📻 当前没有正在听的状态。用 `.listening <歌名> [-s 歌手] [-p 软件]` 设置。"
        return (f"📻 机器人当前状态：**正在听 {cur.get('music_name', '')}**\n"
                f"  - 歌手：{cur.get('singer', '') or '未指定'}\n"
                f"  - 软件：{cur.get('software', 'cloudmusic') or 'cloudmusic'}\n"
                f"用 `.listening off` 结束。")

    # 设置：歌名必填
    if not song:
        return "❌ 请先给出歌名，如 `.listening 有点甜 -s 幻梦 -p kugoumusic`"
    ok, msg = await set_music(song, singer=singer, software=software)
    if ok:
        return (f"🎵 已设置机器人状态：**正在听 {song}**\n"
                + (f"  - 歌手：{singer}\n" if singer else "")
                + f"  - 软件：{software.strip() or 'cloudmusic'}\n"
                f"用 `.listening off` 结束。")
    return f"❌ 设置失败：{msg}"


async def cmd_playing(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.playing <游戏名> [-o] — 设置机器人"正在玩"状态

    通过 KOOK API /api/v3/game/activity 设置 data_type=1 的游戏状态。
    用法：
      .playing 原神                   设置"正在玩 原神"（游戏库查不到会自动创建）
      .playing off | -o               结束"正在玩"状态
      .playing | -q | -s              查看当前状态
    """
    from services.music_status import set_game, clear_game, current_game

    parts = list(args or [])
    off = show = False
    positional = []
    i = 0
    while i < len(parts):
        low = parts[i].lower()
        if low in ("-o", "--off", "--stop", "--clear"):
            off = True; i += 1; continue
        if low in ("-q", "--status", "--state", "--show", "--query", "-s"):
            show = True; i += 1; continue
        if parts[i].startswith("-") and parts[i] != "-":
            i += 1; continue          # 未知 flag：忽略
        positional.append(parts[i]); i += 1

    game = " ".join(positional).strip()
    _g = game.lower()

    # 结束
    if off or (game and _g in ("off", "close", "stop", "0", "结束", "关闭")):
        ok, msg = clear_game()
        return ("✅ 已结束" + ("正在玩" if ok else f"（{msg}）") if ok
                else f"❌ 结束状态失败：{msg}")

    # 查看
    if show or not game or _g in ("status", "state", "?", "查看", "状态", "query"):
        cur = current_game()
        if not cur:
            return "🎮 当前没有正在玩的状态。用 `.playing <游戏名>` 设置。"
        return (f"🎮 机器人当前状态：**正在玩 {cur.get('name', '')}**\n"
                f"用 `.playing off` 结束。")

    # 设置
    ok, msg = await set_game(game)
    if ok:
        return (f"🎮 已设置机器人状态：**正在玩 {game}**\n"
                f"用 `.playing off` 结束。")
    return f"❌ 设置失败：{msg}"


# ── 插件管理：二次确认暂存（卸载 / 覆盖安装）─────────────
# token -> {"action": "unload"|"overwrite", "name": str, "fname": str|None, "expire": float}
_plugin_unload_pending: dict[str, dict] = {}
_PLUGIN_CONFIRM_TTL = 120  # 秒


def _plugin_admin_denied():
    return format_lang("error.permission_denied") or "无权限执行该指令"


def _plugin_gen_token() -> str:
    import secrets
    return secrets.token_hex(4)


async def _plugin_mgr():
    from core.plugin import get_plugin_manager
    return get_plugin_manager()


async def _plugin_action_result(ok: bool, err: str, ok_msg: str) -> str:
    return ok_msg if ok else f"❌ {err}"


async def cmd_plugin(args, user_id, group_id, sender_name, is_group, bot_qq, raw_event=None):
    """.plugin — 插件管理：状态/启停/分享
    旧式: status|list|reload|enable|disable|unload
    CLI: -pack|-down|-load|-list"""
    flags = [a.lower().lstrip("-") for a in args if a.startswith("-")]
    non_flags = [a for a in args if not a.startswith("-")]
    sub = (non_flags[0].lower() if non_flags else "")

    # ── 查看（任何人均可）──
    if not flags:
        if sub in ("status", "状态"):
            return await _plugin_status_card()
        if sub in ("list", "列表"):
            return await _plugin_status_text()

    # ── 以下为管理操作，仅管理员 ──
    cfg = get_config()
    if not cfg.is_admin(user_id, group_id):
        return _plugin_admin_denied()

    # ── CLI 风格（分享/打包/下载/加载）──
    if flags:
        from modules import plugin_share as PS

        # -list：列出 _down 已下载的 .hmp
        if "list" in flags:
            _, names = PS.list_downloads()
            if not names:
                return "plugins/_down/ 还没有下载的 .hmp"
            return "已下载的 .hmp（在 plugins/_down/）：\n" + "\n".join("  " + n for n in names)

        # -import <url>：直链导入 .hmp（下载 → 解包 → 加载，一步到位）
        if "import" in flags or "url" in flags:
            if not non_flags:
                return "用法：`.plugin -import <直链URL>`\n示例：`.plugin -import https://example.com/xxx.hmp`"
            raw_url = " ".join(non_flags)
            # KOOK 客户端会把 URL 自动渲染成 [url](url) Markdown 链接，
            # 先剥离包裹再校验（否则 is_hmp_url 会因 []() 字符判失败）。
            _m = re.search(r"https?://[^\s)\]]+", raw_url)
            if _m:
                raw_url = _m.group(0)
            if not PS.is_hmp_url(raw_url):
                return ("❌ 不是有效的 .hmp 直链（需 http/https 且以 .hmp 结尾）。\n"
                        "若你是引用聊天里的文件，请用 `.plugin -down -load`")
            ok, msg = PS.download_hmp(raw_url)
            if not ok:
                return "❌ " + msg
            return await _plugin_load_hmp(PS.local_filename_for(raw_url))

        # -pack <本地插件名>：打包并把 .hmp 发到当前聊天
        if "pack" in flags:
            if not non_flags:
                return "用法：`.plugin -pack <本地插件名>`"
            ok, msg, hmp_path = PS.pack_plugin(non_flags[0])
            if not ok:
                return "❌ " + msg
            if hmp_path:
                from services.sender import send_file
                chat_id = group_id if is_group else user_id
                if await send_file(str(hmp_path), chat_id, is_group):
                    return "✅ " + msg + "\n已将 .hmp 发到聊天，他人可引用后用 `.plugin -down -load` 导入"
            return "✅ " + msg

        # 从聊天附件/引用里找 .hmp
        url = await PS.fetch_hmp_url(raw_event) if raw_event is not None else None
        missing_attach = ("没找到 .hmp 附件。请对 `points.hmp`/`shop.hmp` 那条文件消息点「回复」，"
                          "在回复框里输入命令再发送。")

        # -down 或 -down -load（带附件）：下载到 _down，-load 时顺带解包+加载
        if "down" in flags:
            if not url:
                return missing_attach
            ok, msg = PS.download_hmp(url)
            if not ok:
                return "❌ " + msg
            if "load" not in flags:
                return "✅ " + msg
            return await _plugin_load_hmp(PS.local_filename_for(url))

        # -load [-list] [xxx.hmp] 或 -load（带附件）
        if "load" in flags:
            if non_flags:
                return await _plugin_load_hmp(non_flags[0])
            if url:
                ok, msg = PS.download_hmp(url)
                if not ok:
                    return "❌ " + msg
                return await _plugin_load_hmp(PS.local_filename_for(url))
            return missing_attach

        return ("CLI 用法：\n"
                "`.plugin -pack <本地插件名>`  打包为 .hmp\n"
                "`.plugin -import <直链URL>`  直链导入 .hmp（下载+解包+加载）\n"
                "`.plugin -down`   引用聊天中的 .hmp 下载保存到 _down\n"
                "`.plugin -down -load` 下载并加载\n"
                "`.plugin -load <xxx.hmp>` 解包+加载 _down 内的插件\n"
                "`.plugin -load -list` / `.plugin -down -list` 查看已下载")

    # ── 旧式管理子命令 ──
    if sub == "reload" and len(args) >= 2:
        name = args[1]
        ok, err = await (await _plugin_mgr()).reload(name)
        return await _plugin_action_result(ok, err, f"✅ 已热重载插件 `{name}`")

    if sub == "enable" and len(args) >= 2:
        name = args[1]
        ok, err = await (await _plugin_mgr()).enable(name)
        return await _plugin_action_result(ok, err, f"✅ 已启用插件 `{name}`")

    if sub == "disable" and len(args) >= 2:
        name = args[1]
        ok, err = await (await _plugin_mgr()).disable(name)
        return await _plugin_action_result(ok, err, f"✅ 已禁用插件 `{name}`")

    if sub == "unload" and len(args) >= 2:
        name = args[1]
        token = _plugin_gen_token()
        _plugin_unload_pending[token] = {"action": "unload", "name": name, "fname": None,
                                         "expire": time.time() + _PLUGIN_CONFIRM_TTL}
        return (f"⚠️ 确定要卸载插件 `{name}` 吗？\n"
                f"卸载会清理该插件的事件/定时器/命令注册，且需重启才能自动重新加载。\n"
                f"输入 `.plugin confirm {token}` 确认执行（{_PLUGIN_CONFIRM_TTL} 秒内有效）。")

    if sub == "confirm" and len(args) >= 2:
        token = args[1]
        pending = _plugin_unload_pending.pop(token, None)
        if not pending:
            return "❌ 确认令牌无效或已过期，请重新执行相关命令"
        if time.time() > pending["expire"]:
            return "❌ 确认已过期，请重新执行相关命令"
        if pending.get("action") == "overwrite":
            return await _plugin_overwrite_confirm(pending)
        name = pending["name"]
        ok, err = await (await _plugin_mgr()).unload(name)
        return await _plugin_action_result(ok, err, f"✅ 已卸载插件 `{name}`")

    # ── update：从插件库一键更新（无参列可更新，带参执行覆盖热重载）──
    if sub in ("update", "更新"):
        return await _plugin_update_cmd(non_flags)

    return ("用法：`.plugin [status|list]`\n"
            "`.plugin reload <名字>`  热重载\n"
            "`.plugin enable <名字>`  启用\n"
            "`.plugin disable <名字>` 禁用\n"
            "`.plugin unload <名字>`  卸载（需二次确认）\n"
            "`.plugin update [名字]`  插件库一键更新（无参列出可更新项）\n"
            "`.plugin -pack/-import/-down/-load` 插件打包/导入/下载/加载")


async def _plugin_load_hmp(fname: str) -> str:
    """解包 _down 里的 .hmp 并加载到运行时；已存在时按版本提示升级/降级覆盖安装。"""
    from modules import plugin_share as PS
    if fname.lower().endswith("/"):
        return "❌ 请传入 .hmp 文件名"
    p = PS._down_dir() / fname
    if not p.is_file():
        return f"❌ _down 里没有 `{fname}`，可用 `.plugin -load -list` 查看"

    name = PS.peek_hmp_name(p)
    if not name:
        ok, msg, _ = PS.unpack_hmp(p)
        return "❌ 无法识别插件名" if not ok else "❌ manifest 无效"

    ok, msg, conflict = PS.unpack_hmp(p)
    if not ok:
        # 已存在：有版本冲突信息 → 版本提示 + 覆盖确认；否则沿用原“补加载”逻辑
        mgr = await _plugin_mgr()
        cur = {x.get("name"): x for x in mgr.list()}.get(name)
        if conflict:
            return _plugin_overwrite_prompt(name, fname, conflict, cur)
        if cur and cur.get("state") == "enabled":
            return "❌ " + msg + "（该插件已在运行）"
        if cur and cur.get("state") != "enabled":
            ok2, err2 = await mgr.enable(name)
            return (f"✅ 检测到已存在 `{name}`，已重新启用" if ok2
                    else f"❌ 启用失败: {err2}")
        # 目录在但注册表无记录（刚 unload）→ 走 discover + load + init + enable
        ok3, msg3 = await PS.load_local_plugin(name)
        return (f"✅ 检测到已存在 `{name}`，已重新加载启用" if ok3
                else f"❌ 加载失败: {msg3}")
    ok2, msg2 = await PS.load_local_plugin(name)
    if not ok2:
        return f"❌ 解包成功但加载失败: {msg2}"
    return f"✅ {msg} | {msg2}"


def _plugin_overwrite_prompt(name: str, fname: str, conflict: dict, cur: Optional[dict]) -> str:
    """插件目录已存在：按版本差生成升级/降级提示，并下发覆盖确认令牌。"""
    from modules import plugin_share as PS
    local_ver = conflict.get("local_version") or "0.0.0"
    pkg_ver = conflict.get("pkg_version") or "0.0.0"
    cmp = PS.compare_versions(pkg_ver, local_ver)
    if cmp > 0:
        kind = "升级"
    elif cmp < 0:
        kind = "降级"
    else:
        kind = "同版本覆盖"
    running = "（该插件当前正在运行）" if (cur and cur.get("state") == "enabled") else "（当前未在运行）"
    token = _plugin_gen_token()
    _plugin_unload_pending[token] = {"action": "overwrite", "name": name, "fname": fname,
                                     "expire": time.time() + _PLUGIN_CONFIRM_TTL}
    return (f"⚠️ 插件 `{name}` 已存在，包内版本 v{pkg_ver}，本地 v{local_ver} → {kind} {running}\n"
            f"说明：`.plugin unload` 只从内存卸载，磁盘目录仍在，重启后会自动重新加载，"
            f"所以导入仍会提示已存在。\n"
            f"输入 `.plugin confirm {token}` 确认覆盖安装（{_PLUGIN_CONFIRM_TTL} 秒内有效）。")


async def _plugin_overwrite_confirm(pending: dict) -> str:
    """确认覆盖安装：卸载旧实例 → 清缓存模块 → 覆盖解包 → 重新加载启用。"""
    return await _plugin_overwrite_install(pending["name"], pending["fname"])


async def _plugin_overwrite_install(name: str, fname: str) -> str:
    """覆盖安装并热重载：卸载旧实例 → 清缓存模块 → 覆盖解包 → 重新加载启用。"""
    import sys as _sys
    from modules import plugin_share as PS
    mgr = await _plugin_mgr()
    cur = {x.get("name"): x for x in mgr.list()}.get(name)
    if cur:
        # 清理事件/定时器/命令注册（记录不存在时 unload 安全返回失败，不阻断）
        await mgr.unload(name)
    # 弹出被缓存的旧模块，确保覆盖后加载到新代码
    _sys.modules.pop(f"_hm_plugin_{name}", None)
    p = PS._down_dir() / fname
    ok, msg, _ = PS.unpack_hmp(p, overwrite=True)
    if not ok:
        return "❌ 覆盖解包失败: " + msg
    ok2, msg2 = await PS.load_local_plugin(name)
    if not ok2:
        return f"❌ 已覆盖文件但加载失败: {msg2}"
    return f"✅ {msg} | {msg2}"


async def _plugin_update_cmd(non_flags: list[str]) -> str:
    """`.plugin update`：从插件库拉取最新版并覆盖热重载。

    无参 → 列出库中比本地新的插件；带参 → 下载该插件 .hmp 并覆盖安装。
    """
    from modules import plugin_share as PS
    mgr = await _plugin_mgr()
    cur = {x.get("name"): x for x in mgr.list()}

    # 无参：列出可更新项（non_flags 恒含子命令词 update，仅 1 项即为无参）
    if len(non_flags) == 1:
        ok, plugins, err = PS.lib_list()
        if not ok:
            return "❌ 插件库不可用: " + err
        updates: list[str] = []
        for lp in plugins:
            p = cur.get(lp.get("name"))
            if not p:
                continue
            lv = str(p.get("version") or "0.0.0")
            pv = str(lp.get("version") or "0.0.0")
            if PS.compare_versions(pv, lv) > 0:
                updates.append(f"  {lp['name']}: v{lv} → v{pv}")
        if not updates:
            return f"插件库（{PS.lib_base()}）无可用更新，全部插件已是最新"
        return ("插件库有可用更新：\n" + "\n".join(updates)
                + "\n用 `.plugin update <名字>` 更新")

    # 带参：更新指定插件（non_flags[1] 才是插件名，non_flags[0] 是子命令词 update）
    name = non_flags[1]
    if not PS.validate_name(name):
        return "❌ 插件名非法（仅限字母/数字/_/-）"
    ok, info, err = PS.lib_latest(name)
    if not ok:
        return "❌ 插件库查询失败: " + err
    pv = str(info.get("version") or "0.0.0")
    dl = info.get("download_url") or PS.lib_download_url(name)
    p = cur.get(name)
    if not p:
        return (f"❌ 本地未安装插件 `{name}`（库中 v{pv}），"
                f"可用 `.plugin -import {dl}` 安装")
    lv = str(p.get("version") or "0.0.0")
    if PS.compare_versions(pv, lv) <= 0:
        return f"✅ `{name}` 已是最新（本地 v{lv}，库 v{pv}）"

    ok2, msg2 = PS.download_hmp(dl)
    if not ok2:
        return "❌ 下载失败: " + msg2
    return await _plugin_overwrite_install(name, PS.local_filename_for(dl))


def _plugin_cards_data() -> list[dict]:
    from core.plugin import get_plugin_manager
    mgr = get_plugin_manager()
    plugins = mgr.list()
    for p in plugins:
        p["ok"] = bool(p.get("state") == "enabled" and not p.get("error"))
        p["error"] = p.get("error") or ""
    return plugins


async def _plugin_status_card() -> str:
    import json as _json
    from modules.cmd_cards import build_plugin_status
    try:
        plugins = _plugin_cards_data()
    except Exception as e:
        return f"插件系统未就绪：{e}"
    return "__CARD__:" + _json.dumps(build_plugin_status(plugins), ensure_ascii=False)


async def _plugin_status_text() -> str:
    try:
        plugins = _plugin_cards_data()
    except Exception as e:
        return f"插件系统未就绪：{e}"
    if not plugins:
        return "当前未加载任何插件"
    lines: list[str] = []
    for p in plugins:
        state = p.get("state", "unknown")
        icon = "🟢" if p.get("ok") else "🔴"
        ver = p.get("version", "")
        entry = f"{icon} {p['name']} v{ver}  [{state}]"
        if p.get("error"):
            entry += f" — {p['error']}"
        lines.append(entry)
    ok_n = sum(1 for p in plugins if p.get("ok"))
    lines.append(f"— 共 {len(plugins)} 个，生效 {ok_n} 个 ——")
    return "\n".join(lines)


async def cmd_sys(args, user_id, group_id, sender_name, is_group, bot_qq):
    """.sys [card|shot|shotdesk] — PC 状态/截屏"""
    from services.pc_status import build_sys_card_html, format_pc_status, request_screenshot
    from modules.changelog import render_card_to_image
    from services.sender import send_group_msg, send_private_msg
    import uuid, base64, tempfile

    sub = (args[0].lower() if args else "")

    # 截屏
    if sub in ("shot", "shotdesk"):
        b64 = await request_screenshot(timeout=30.0)
        if not b64:
            return "截屏失败（PC 客户端未连接或超时）"
        from pathlib import Path as _Path
        tmp = _Path(__file__).resolve().parent.parent / "data" / "img_temp" / f"shot_{uuid.uuid4().hex[:8]}.jpg"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        import binascii
        try:
            data = binascii.a2b_base64(b64)
        except Exception as e:
            return f"截屏数据解码失败: {e}"
        tmp.write_bytes(data)
        cq = f"[img:file:{tmp.as_posix()}]"
        if is_group:
            await send_group_msg(cq, group_id)
        else:
            await send_private_msg(cq, user_id)
        return None

    # HTML 卡片
    if sub == "card":
        from core.config import get_config
        _cfg = get_config()
        _owner = _cfg.admin_id_str or str(_cfg.admin_qq) or "管理员"
        _bot_n = _cfg.bot_name or "KookBot"
        html = build_sys_card_html(owner=_owner, bot_name=_bot_n)
        if html:
            filename = f"sys_{uuid.uuid4().hex[:8]}.jpg"
            img_path = await render_card_to_image(html, filename, width=760)
            if img_path:
                normalized = img_path.replace("\\", "/")
                cq = f"[img:file:{normalized}]"
                if is_group:
                    await send_group_msg(cq, group_id)
                else:
                    await send_private_msg(cq, user_id)
                return None
        return format_pc_status(owner=_owner)

    # 默认：纯文字
    from core.config import get_config
    _cfg2 = get_config()
    _owner2 = _cfg2.admin_id_str or str(_cfg2.admin_qq) or "管理员"
    return format_pc_status(owner=_owner2)

COMMAND_MAP: dict[str, callable] = {
    "help":       cmd_help,
    "ping":       cmd_ping,
    "restart":    cmd_restart,
    "favlist":    cmd_favlist,
    "info":       cmd_info,
    "search":     cmd_search,  # LLM CALL 调用用
    "search_web":  cmd_search,  # FC 工具名别名
    "s":          cmd_search,  # 兼容 lang.toml 帮助文本中的 .s 用法
    "read":       cmd_read,
    "kook_user_id": cmd_kook_user_id,    # ★ 查询 @用户的 KOOK ID
    "kuid":         cmd_kook_user_id,    # 短别名
    "kook_channel_id": cmd_kook_channel_id,  # ★ 查询当前频道 ID
    "kcid":            cmd_kook_channel_id,  # 短别名
    "write_code":  cmd_write_code,  # ★ 代码生成
    "ignore":      cmd_ignore,      # ★ 忽略用户
    "unignore":    cmd_unignore,    # ★ 解除忽略
    "reload":     cmd_reload,
    "provider":   cmd_provider,      # ★ 模型供应商切换
    "model":      cmd_provider,      # 英语别名
    "供应商":     cmd_provider,      # 中文别名
    "update":     _cmd_update,
    "upd":        _cmd_update,    # 短别名
    "gh":         _cmd_gh,
    "添加关系":   cmd_add_relation,
    "resetfav":   cmd_reset_fav,
    "updateinfo": cmd_update_info,
    "up":         cmd_update_info,
    "box":        cmd_box,
    "testsys":    cmd_testsys,
    "testok":     cmd_testok,
    "test-up":    cmd_testup,
    "notify":     cmd_notify,
    "notifyset":  cmd_notifyset,
    "notifycheck": cmd_notifycheck,
    "notifyperf":  cmd_notifyperf,
    "jsonraw":    cmd_jsonraw,
    "md":         cmd_md,
    "draw":       cmd_draw,
    "绘画":       cmd_draw,
    "video":      cmd_video,
    "视频":       cmd_video,
    "voice":      cmd_voice,
    "语音":       cmd_voice,
    "img2video":  cmd_img2video,
    "图生视频":   cmd_img2video,
    "op":         cmd_op,        # ★ OP 权限管理
    "persona":    cmd_persona,   # ★ 私聊人格
    "人格":       cmd_persona,
    "主人":       cmd_master,    # ★ 私聊主人
    "sleep":      cmd_sleep,     # ★ 睡觉模式
    "含蓄":       cmd_hanxu,     # ★ 含蓄叙述风格
    "叙事":       cmd_hanxu,
    "remind":     cmd_remind,
    "提醒":       cmd_remind,
    "preset":     cmd_preset,
    "analyze":    cmd_analyze,
    "owner":      cmd_owner,
    "memory":     cmd_memory,
    "wzq":        cmd_wzq,
    "五子棋":     cmd_wzq,
    "xq":         cmd_xq,
    "象棋":       cmd_xq,
    "wdsj":       cmd_wdsj,
    "balance":    cmd_balance,
    "cost":       cmd_cost,
    "tokens":     cmd_tokens,
    "sys":        cmd_sys,
    "pc":         cmd_sys,
    "lyric":      cmd_lyric,
    "listening":  cmd_listening,   # ★ 设置机器人"正在听"状态
    "正在听":      cmd_listening,
    "playing":    cmd_playing,     # ★ 设置机器人"正在玩"状态
    "正在玩":      cmd_playing,
    "plugin":     cmd_plugin,      # ★ 插件管理（status卡片/list/reload/enable/disable/unload）
}


class _PluginMsg(dict):
    """插件命令 handler 收到的消息对象。

    同时支持 dict 访问（echo 插件用 msg.get("args")）与属性访问
    （Lua 插件用 msg.args），两种插件书写风格都能兼容。"""

    def __getattr__(self, key: str):
        return self.get(key)


def _find_plugin_command(cmd_name: str):
    """在 CapabilityRegistry 里查找插件动态注册的命令（Phase 13 插件系统）。

    插件通过 ctx.capability.register_command 注册进 CapabilityRegistry
    （cap.source 以 "plugin:" 开头，handler 经 bind_handler(cap.id, handler) 绑定），
    这些命令不进静态 COMMAND_MAP，因此分发时在此动态解析。找不到返回 None。
    """
    try:
        from core.capability import get_capability_registry, CATEGORY_COMMAND
        reg = get_capability_registry()
        for cap in reg.all():
            if (cap.category == CATEGORY_COMMAND
                    and cap.name == cmd_name
                    and cap.source and cap.source.startswith("plugin:")):
                handler = reg.get_handler(cap.id)
                if handler is not None:
                    return handler
    except Exception:
        pass
    return None


def _is_registered_command(cmd: str) -> bool:
    """判断 cmd（不带点）是否为已注册指令（静态 COMMAND_MAP 或插件动态命令）。"""
    if not cmd:
        return False
    if cmd in COMMAND_MAP:
        return True
    return _find_plugin_command(cmd) is not None


def _extract_embedded_command(text: str):
    """从消息文本中提取嵌入的已注册指令片段。

    用户可能发「执行 .plugin status」「能不能运行.luck」等，指令不在行首，
    若直接交给 LLM 会产生"假装执行"的幻觉。此处扫描文本中的 `.cmd ...`，
    仅当命令名已注册时返回从点开始的完整命令文本；URL 里的点（如 .com）
    因命令未注册会自动跳过。找不到返回 None。
    """
    if not text:
        return None
    idx = text.find(".")
    while idx != -1:
        m = re.match(r'\.([A-Za-z\u4e00-\u9fa5][^\s]*)(\s.*)?$', text[idx:])
        if m and _is_registered_command(m.group(1).lower()):
            return text[idx:]
        idx = text.find(".", idx + 1)
    return None


async def handle_command(
    text: str,
    user_id: int,
    group_id: int,
    sender_name: str,
    is_group: bool,
    bot_qq: int,
    raw_message: str = "",
    raw_event=None,
) -> str | None:
    """
    指令分发器。

    解析前缀(.) → 提取命令名和参数 → 查表 → 调用处理器 → 返回结果。

    Returns:
        - 文本字符串: 需要作为普通消息发送
        - "__SYS_TEST_CARD__": 需要由调用方构建并发送卡片
        - None: 不需要回复
    """
    # 解析前缀和命令部分（KOOK 统一前缀：.）
    if not text.startswith("."):
        return None
    prefix = "."
    cmd_part = text[1:].strip()
    # 过滤单个 "." 或 ".<空格>" 等无意义输入
    if not cmd_part:
        logger.warning("空指令内容: '%s'", text)
        return None

    # 提取命令名和参数
    parts = cmd_part.split()
    cmd_name = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []

    logger.debug("指令解析: prefix=%s cmd=%s args=%s user=%d", prefix, cmd_name, args, user_id)

    handler = COMMAND_MAP.get(cmd_name)
    plugin_handler = _find_plugin_command(cmd_name) if not handler else None
    if not handler and plugin_handler is None:
        logger.warning("未知指令: '%s%s'", prefix, cmd_name)
        return format_lang("error.unknown_command", prefix=prefix, cmd=cmd_name)

    # 插件动态命令：handler 是 async (msg) -> str|None，构造消息对象直接调用
    if plugin_handler is not None:
        # 透传引用消息 ID，供插件按需拉取被引用消息内容（如引用图片取 URL）
        quote_id = ""
        try:
            quote = getattr(raw_event, "quote", None)
            qid = getattr(quote, "id", None) if quote is not None else None
            if qid:
                quote_id = str(qid)
        except Exception:
            pass
        # 透传被 @ 的用户 ID 列表（排除 bot 自身），供插件按需使用（如 .摸头 @人 取头像）
        mentions: list[str] = []
        try:
            if hasattr(raw_event, "mention") and raw_event.mention:
                bid = str(bot_qq)
                for m in raw_event.mention:
                    ms = str(m)
                    if ms and ms != bid:
                        mentions.append(ms)
        except Exception:
            pass
        msg = _PluginMsg(
            args=args,
            text=raw_message,
            author=user_id,
            sender=sender_name,
            is_group=is_group,
            chat_id=group_id,
            quote_id=quote_id,
            mentions=mentions,
        )
        try:
            result = await plugin_handler(msg)
        except Exception as e:
            err_type = type(e).__name__
            logger.error("插件指令执行异常 [%s]: %s: %s", cmd_name, err_type, e, exc_info=True)
            return format_lang("error.command_error_detail", cmd=cmd_name, err=err_type, msg=str(e))
        if result is not None:
            logger.info("插件指令执行完成 [%s]: 返回%d字符", cmd_name, len(str(result)))
        else:
            logger.info("插件指令执行完成 [%s]: 无返回值（已自行发送）", cmd_name)
        return result

    # 执行处理器（普通指令）
    try:
        # 按需传入 raw_message / raw_event
        import inspect
        sig = inspect.signature(handler)
        kwargs = {}
        if "raw_message" in sig.parameters:
            kwargs["raw_message"] = raw_message
        if "raw_event" in sig.parameters:
            kwargs["raw_event"] = raw_event
        result = await handler(args, user_id, group_id, sender_name, is_group, bot_qq, **kwargs)
        if result is not None:
            logger.info("指令执行完成 [%s]: 返回%d字符", cmd_name, len(str(result)))
        else:
            logger.info("指令执行完成 [%s]: 无返回值（已自行发送）", cmd_name)
        return result
    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)
        logger.error("指令执行异常 [%s]: %s: %s", cmd_name, err_type, err_msg, exc_info=True)
        return format_lang("error.command_error_detail", cmd=cmd_name, err=err_type, msg=err_msg)


# ------每日排名HTML渲染------
def _build_daily_rank_html(rows, today, new_players, time_start="", time_end=""):
    header = "击杀"  # 用于瀑布颜色
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
        more = f' 等{len(new_players)}人' if len(new_players) > 6 else ''
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


def _build_arena_daily_html(rows, today, time_start="", time_end=""):
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


async def _render_html_to_png(html, prefix):
    import time
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = str(Path(__file__).resolve().parent.parent / "data" / "img_temp" / f"{prefix}_{ts}.png")
    from modules.changelog import _ensure_browser
    try:
        browser = await _ensure_browser()
        page = await browser.new_page(viewport={"width": 540, "height": 600})
        await page.set_content(html)
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=out, full_page=True)
        await page.close()
        return out
    except Exception:
        return None
