"""
TUF 谱面/玩家查询插件（重写版 v2）
- 统一入口 .tuf <子命令>，数据源 https://api.tuforums.com 官方 API
- 支持绑定自己的 TUF 玩家 ID（.tuf bind），之后 .tuf me 直接查自己

子命令：
  谱面: search <词> [页] | info <ID/名称> | passes <ID> | dl <ID> | rerate <ID>
  玩家: bind <名称/ID> | me | player [名称/ID] | rank [名称/ID] | passesby [名称/ID]
  排行: lb [榜] [页] | lb creators [页]
  统计: stats | countries
  资料: song <名称> | packs [页]
  帮助: help
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import RLock

from core.logger import get_logger

from services import tuf_api as api

logger = get_logger("plugin.tuf")

_DATA_FILE = Path(__file__).resolve().parent / "data.json"
_LOCK = RLock()

ID_RE = re.compile(r"^\d+$")


# ── 绑定数据（按 KOOK user_id 存 TUF player_id/name）────────
def load_binds() -> dict:
    with _LOCK:
        if _DATA_FILE.exists():
            try:
                return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"binds": {}}


def save_binds(data: dict):
    with _LOCK:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def get_bind(uid) -> dict | None:
    return load_binds()["binds"].get(str(uid))


def set_bind(uid, player_id, name):
    data = load_binds()
    data["binds"][str(uid)] = {"player_id": player_id, "name": name}
    save_binds(data)


def del_bind(uid):
    data = load_binds()
    data["binds"].pop(str(uid), None)
    save_binds(data)


async def _resolve_player_id(arg: str | None, uid) -> tuple[int | None, str]:
    """解析玩家参数 → (player_id, 显示名)。arg 为空时用绑定，纯数字当 ID，否则按名字搜索。"""
    if not arg:
        b = get_bind(uid)
        if not b:
            return None, "你还没有绑定 TUF 玩家，先用 `.tuf bind <名称/ID>` 绑定，或直接给出名称/ID"
        return int(b["player_id"]), b["name"]
    if ID_RE.match(arg.strip()):
        return int(arg.strip()), arg.strip()
    # 按名字搜索
    found = await api.search_players(arg.strip(), limit=1)
    if not found:
        return None, f"未找到玩家: {arg}"
    p = found[0]
    return int(p["id"]), p.get("name") or arg


def _err(msg: str) -> str:
    return f"❌ {msg}"


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        self.ctx.capability.register_command(
            name="tuf", description="TUF 谱面/玩家查询：.tuf help 查看用法",
            handler=self._cmd)

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def on_unload(self):
        pass

    # ══════════════════════════════════════════════════════
    async def _cmd(self, msg):
        args = msg.get("args") or []
        uid = msg.get("author")
        sub = args[0].lower() if args else "help"

        handlers = {
            "help": self._help,
            "search": self._search,
            "info": self._info,
            "passes": self._passes,
            "dl": self._dl,
            "rerate": self._rerate,
            "bind": self._bind,
            "unbind": self._unbind,
            "me": self._me,
            "player": self._player,
            "rank": self._rank,
            "passesby": self._passesby,
            "lb": self._lb,
            "stats": self._stats,
            "countries": self._countries,
            "song": self._song,
            "packs": self._packs,
        }
        h = handlers.get(sub)
        if not h:
            return f"未知子命令: {sub}\n.tuf help 查看用法"
        try:
            return await h(args[1:], uid, msg)
        except Exception as e:
            logger.error("[TUF] %s 执行异常: %s", sub, e, exc_info=True)
            return _err(f"{sub} 执行出错: {e}")

    # ── 帮助 ──────────────────────────────────────────────
    async def _help(self, args, uid, msg):
        return (
            "🎵 TUF 查询（ADOFai 谱面社区）\n"
            "━━━━━━━━━━━━━━━━\n"
            "【谱面】\n"
            "  .tuf search <关键词> [页]   搜索谱面\n"
            "  .tuf info <ID/名称>         谱面详情\n"
            "  .tuf passes <ID>            谱面通关记录\n"
            "  .tuf dl <ID>                谱面下载直链\n"
            "  .tuf rerate <ID>            谱面改版历史\n"
            "【玩家】\n"
            "  .tuf bind <名称/ID>         绑定你的 TUF 玩家\n"
            "  .tuf me                     查询自己（需绑定）\n"
            "  .tuf player [名称/ID]       玩家档案\n"
            "  .tuf rank [名称/ID]         排名历史\n"
            "  .tuf passesby [名称/ID]     玩家通关记录\n"
            "【排行/统计】\n"
            "  .tuf lb [榜] [页]           排行榜（ranked/pp/wf/12k/xacc/passes）\n"
            "  .tuf lb creators [页]       创作者排行\n"
            "  .tuf stats                  全局统计\n"
            "  .tuf countries              国家分布\n"
            "【资料】\n"
            "  .tuf song <名称>            歌曲信息\n"
            "  .tuf packs [页]             关卡包\n"
        )

    # ── 谱面 ──────────────────────────────────────────────
    async def _search(self, args, uid, msg):
        if not args:
            return "用法: .tuf search <关键词> [页码]\n支持 song:/artist:/creator: 字段语法"
        page = 1
        query_args = list(args)
        if len(args) > 1 and args[-1].isdigit():
            page = int(args[-1])
            query_args = args[:-1]
        query = " ".join(query_args)
        r = await api.search_levels(query, limit=10, page=page)
        if not r["results"]:
            return f"😢 未找到谱面: {query}"
        lines = [f"🔍 搜索结果: {query}（共 {r['total']} 个）"]
        for i, lv in enumerate(r["results"], 1):
            song = lv.get("song", "?")
            artist = lv.get("artist", "?")
            diff = api.get_diff_name(lv)
            lines.append(f"{i}. [{diff}] {song} - {artist}")
        if r["hasMore"]:
            lines.append(f"… 还有更多，.tuf search {query} {page + 1}")
        return "\n".join(lines)

    async def _resolve_level(self, arg: str) -> dict | None:
        if ID_RE.match(arg.strip()):
            return await api.get_level(int(arg.strip()))
        return await api.get_level(arg.strip())

    async def _info(self, args, uid, msg):
        if not args:
            return "用法: .tuf info <谱面ID/名称>"
        lv = await self._resolve_level(" ".join(args))
        if not lv:
            return _err("未找到该谱面")
        d = lv["level"] if isinstance(lv, dict) and "level" in lv else lv
        song = d.get("song", "?")
        artist = d.get("artist", "?")
        creator = api.get_creator_name(d)
        diff = api.get_diff_name(d)
        lines = [
            f"🎵 {song} - {artist}",
            f"├ 创作者: {creator}",
            f"├ 难度: {diff} | BPM: {api.fmt_bpm(d.get('bpm'))}",
            f"├ 时长: {api.fmt_duration(d.get('levelLengthInMs'))} | 格子: {int(d.get('tilecount') or 0)}",
            f"├ 通关: {api.fmt_num(d.get('clears'))} | 点赞: {api.fmt_num(d.get('likes'))} | 下载: {api.fmt_num(d.get('downloadCount'))}",
        ]
        rating = d.get("rating")
        if isinstance(rating, dict) and rating.get("averageDifficultyId"):
            lines.append(f"├ 评级: 平均难度 ID {rating.get('averageDifficultyId')}")
        video = d.get("videoLink")
        if video:
            lines.append(f"├ 视频: {video}")
        return "\n".join(lines)

    async def _passes(self, args, uid, msg):
        if not args:
            return "用法: .tuf passes <谱面ID>"
        lv = await api.get_level(int(args[0].strip())) if ID_RE.match(args[0].strip()) else None
        if not lv:
            return _err("未找到该谱面")
        d = lv["level"] if "level" in lv else lv
        lid = d["id"]
        r = await api.get_level_passes(lid, limit=5)
        if not r["passes"]:
            return f"该谱面还没有通关记录 ({d.get('song', '?')})"
        lines = [f"🏆 {d.get('song', '?')} 通关记录（共 {r['total']}）"]
        for i, p in enumerate(r["passes"][:5], 1):
            pname = (p.get("player") or {}).get("name", "?") if isinstance(p.get("player"), dict) else "?"
            acc = api.fmt_pct(p.get("accuracy"))
            speed = p.get("speed", 100)
            wf = "👑" if p.get("isWorldsFirst") else ""
            lines.append(f"{i}. {pname} | acc {acc} | x{speed} {wf}")
        return "\n".join(lines)

    async def _dl(self, args, uid, msg):
        if not args:
            return "用法: .tuf dl <谱面ID>"
        lv = await api.get_level(int(args[0].strip())) if ID_RE.match(args[0].strip()) else None
        if not lv:
            return _err("未找到该谱面")
        d = lv["level"] if "level" in lv else lv
        link = d.get("dlLink") or d.get("legacyDllink")
        if not link:
            return _err("该谱面没有下载链接")
        return f"⬇️ {d.get('song', '?')}\n下载: {link}"

    async def _rerate(self, args, uid, msg):
        if not args:
            return "用法: .tuf rerate <谱面ID>"
        lv = await api.get_level(int(args[0].strip())) if ID_RE.match(args[0].strip()) else None
        if not lv:
            return _err("未找到该谱面")
        hist = lv.get("rerateHistory", []) if isinstance(lv, dict) else []
        d = lv["level"] if "level" in lv else lv
        if not hist:
            return f"{d.get('song', '?')} 暂无改版历史"
        lines = [f"📜 {d.get('song', '?')} 改版历史"]
        for i, h in enumerate(hist[:8], 1):
            old = h.get("oldDiff", {}).get("name", "?") if isinstance(h.get("oldDiff"), dict) else "?"
            new = h.get("newDiff", {}).get("name", "?") if isinstance(h.get("newDiff"), dict) else "?"
            lines.append(f"{i}. {old} → {new}")
        return "\n".join(lines)

    # ── 玩家 ──────────────────────────────────────────────
    async def _bind(self, args, uid, msg):
        if not args:
            b = get_bind(uid)
            if b:
                return f"你当前绑定: {b['name']} (ID {b['player_id']})\n.tuf unbind 解除绑定"
            return "用法: .tuf bind <TUF玩家名称/ID>\n（在 tuf.gg 可查到你的玩家 ID）"
        arg = " ".join(args)
        pid, name = await _resolve_player_id(arg, uid)
        if pid is None:
            return _err(name)
        set_bind(uid, pid, name)
        return f"✅ 已绑定 TUF 玩家: {name} (ID {pid})\n现在可以用 .tuf me 查询自己"

    async def _unbind(self, args, uid, msg):
        del_bind(uid)
        return "已解除绑定"

    async def _me(self, args, uid, msg):
        pid, name = await _resolve_player_id(None, uid)
        if pid is None:
            return _err(name)
        return await self._player([str(pid)], uid, msg)

    async def _player(self, args, uid, msg):
        arg = " ".join(args) if args else None
        pid, name = await _resolve_player_id(arg, uid)
        if pid is None:
            return _err(name)
        p = await api.get_player(pid)
        if not p:
            return _err("获取玩家信息失败")
        flag = p.get("country", "")
        lines = [
            f"👤 {p.get('name', '?')} {('🇨' if flag else '')}",
            f"├ 排名分: {api.fmt_num(p.get('rankedScore'))} | PP: {api.fmt_num(p.get('ppScore'))}",
            f"├ 综合分: {api.fmt_num(p.get('generalScore'))} | 12K: {api.fmt_num(p.get('score12K'))}",
            f"├ 世界第一: {p.get('worldsFirstCount', 0)} (PP {p.get('worldsFirstPPCount', 0)})",
            f"├ 通关数: {p.get('universalPassCount', 0)} (总 {p.get('totalPasses', 0)})",
            f"├ 平均精度: {api.fmt_pct(p.get('averageXacc'))}",
        ]
        top = p.get("topDiff")
        if isinstance(top, dict) and top.get("name"):
            lines.append(f"├ 最高难度: {top.get('name')}")
        created = p.get("createdAt", "")
        if created and len(created) >= 10:
            lines.append(f"├ 注册: {created[:10]}")
        return "\n".join(lines)

    async def _rank(self, args, uid, msg):
        arg = " ".join(args) if args else None
        pid, name = await _resolve_player_id(arg, uid)
        if pid is None:
            return _err(name)
        series = await api.get_player_rank_history(pid)
        if not series:
            return f"{name} 暂无排名历史"
        # 取最近若干条 + 最早/最高/最新
        recent = series[-10:]
        lines = [f"📈 {name} 排名历史"]
        for s in recent:
            date = s.get("date", "?")
            rr = s.get("rankedScoreRank")
            gr = s.get("generalScoreRank")
            lines.append(f"  {date}: 排名分 #{rr} | 综合分 #{gr}")
        return "\n".join(lines)

    async def _passesby(self, args, uid, msg):
        arg = " ".join(args) if args else None
        pid, name = await _resolve_player_id(arg, uid)
        if pid is None:
            return _err(name)
        passes = await api.get_player_passes(pid, limit=10)
        if not passes:
            return f"{name} 暂无通关记录"
        lines = [f"🏆 {name} 最近通关（{len(passes)}）"]
        for i, p in enumerate(passes, 1):
            acc = api.fmt_pct(p.get("accuracy"))
            speed = p.get("speed", 100)
            wf = "👑" if p.get("isWorldsFirst") else ""
            score = api.fmt_num(p.get("scoreV2"))
            lines.append(f"{i}. acc {acc} | x{speed} | {score} {wf}")
        return "\n".join(lines)

    # ── 排行/统计 ─────────────────────────────────────────
    async def _lb(self, args, uid, msg):
        if not args:
            return ("用法: .tuf lb <榜> [页]\n"
                    "榜: ranked(排名分) / general(综合) / pp / wf / 12k / xacc / passes\n"
                    "或 .tuf lb creators [页] 查看创作者排行")
        if args[0].lower() in ("creators", "creator", "作者"):
            page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
            r = await api.get_creators_leaderboard(page)
            if not r["results"]:
                return "未获取到创作者排行"
            lines = [f"🎨 创作者排行榜 (第 {page} 页)"]
            for i, c in enumerate(r["results"][:10], 1):
                rank = (i - 1) + (page - 1) * 10 + 1
                lines.append(f"#{rank} {c.get('name', '?')}")
            return "\n".join(lines)
        field_key = args[0].lower()
        field = api.resolve_rank_field(field_key)
        if not field:
            return _err(f"未知榜单: {field_key}")
        page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        r = await api.get_leaderboard(field, page=page, limit=10)
        if not r["results"]:
            return "未获取到排行榜"
        label = api._RANK_LABELS.get(field_key, field)
        # KOOK 卡片输出
        return await self._send_lb_card(r["results"], label, field, page, msg)

    async def _send_lb_card(self, results, label, field, page, msg):
        """排行榜 KOOK 卡片（v3 数据自带 name）。"""
        from services.sender import send_raw_group, send_raw_user
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, p in enumerate(results[:10], 1):
            rank = (i - 1) + (page - 1) * 10 + 1
            name = p.get("name") or f"玩家{p.get('id', '?')}"
            country = p.get("country") or ""
            flag = f" [{country}]" if country and country != "XX" else ""
            val = p.get(field)
            if field == "averageXacc":
                val_s = api.fmt_pct(val)
            else:
                val_s = api.fmt_num(val)
            m = medal.get(rank, f"#{rank}")
            lines.append(f"**{m} {name}**{flag}\n└ {label}: **{val_s}**")
        card = [{
            "type": "card",
            "theme": "primary",
            "size": "lg",
            "modules": [
                {"type": "header", "text": {"type": "plain-text",
                                            "content": f"🏆 TUF {label} 排行"}},
                {"type": "section", "text": {"type": "kmarkdown",
                                             "content": "\n".join(lines)}},
                {"type": "section", "text": {"type": "kmarkdown",
                                             "content": f"第 {page} 页 · 展示 {len(results[:10])} 条"}},
            ],
        }]
        try:
            if msg.get("is_group"):
                await send_raw_group(card, msg.get("chat_id"))
            else:
                await send_raw_user(card, msg.get("author"))
            return None  # 已发送卡片
        except Exception as e:
            logger.error("[TUF] lb 卡片发送失败: %s", e)
            # 回退纯文本
            fallback = [f"#{i + (page - 1) * 10 + 1} {(p.get('name') or '?')} — "
                        f"{api.fmt_pct(p.get(field)) if field == 'averageXacc' else api.fmt_num(p.get(field))}"
                        for i, p in enumerate(results[:10])]
            return "🏆 TUF " + label + " 排行\n" + "\n".join(fallback)

    async def _stats(self, args, uid, msg):
        s = await api.get_statistics()
        if not s:
            return _err("获取统计失败")
        ov = s.get("overview", {})
        lines = ["📊 TUF 全局统计"]
        for k in ("players", "levels", "passes", "songs", "artists", "packs"):
            if k in ov:
                lines.append(f"  {k}: {api.fmt_num(ov[k])}")
        # 难度分布
        diffs = s.get("difficulties", {})
        if isinstance(diffs, dict):
            items = list(diffs.items())[:8]
            if items:
                lines.append("  难度分布:")
                for name, cnt in items:
                    lines.append(f"    {name}: {api.fmt_num(cnt)}")
        return "\n".join(lines) if len(lines) > 1 else _err("统计为空")

    async def _countries(self, args, uid, msg):
        stats = await api.get_country_stats()
        if not stats:
            return _err("获取国家分布失败")
        lines = ["🌍 玩家国家分布 Top10"]
        for c in stats[:10]:
            lines.append(f"  {c.get('country', '?')}: {api.fmt_num(c.get('playerCount'))}")
        return "\n".join(lines)

    # ── 资料 ──────────────────────────────────────────────
    async def _song(self, args, uid, msg):
        if not args:
            return "用法: .tuf song <歌曲名称>"
        songs = await api.search_songs(" ".join(args))
        if not songs:
            return f"😢 未找到歌曲: {' '.join(args)}"
        lines = ["🎵 歌曲搜索结果"]
        for s in songs[:5]:
            artists = s.get("artists", [])
            anames = [a.get("name", "") for a in artists if isinstance(a, dict)] if artists else []
            lines.append(f"  {s.get('name', '?')} — {'、'.join(anames) if anames else '?'}")
        return "\n".join(lines)

    async def _packs(self, args, uid, msg):
        page = int(args[0]) if args and args[0].isdigit() else 1
        r = await api.get_packs(page, limit=10)
        if not r["packs"]:
            return "未获取到关卡包"
        lines = [f"📦 关卡包 (第 {page} 页, 共 {r['total']})"]
        for i, pk in enumerate(r["packs"][:10], 1):
            rank = (i - 1) + (page - 1) * 10 + 1
            lines.append(f"{rank}. {pk.get('name', '?')} ({pk.get('levelsCount', pk.get('levelCount', '?'))} 关)")
        return "\n".join(lines)
