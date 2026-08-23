"""
PC 状态 TCP 接收端 — 长连接接收 JSON 行
"""
from __future__ import annotations

import asyncio, json, os, time
from pathlib import Path
from core.logger import get_logger

logger = get_logger("pc_status")

_PC_DATA: dict = {}
_LAST_UPDATE: float = 0
_AUTH_KEY = os.environ.get("BOT_PC_KEY", "")
_client_writer: asyncio.StreamWriter | None = None
_shots_pending: dict[str, asyncio.Future] = {}  # SHOT 请求的 future
_offset_pending: asyncio.Future | None = None   # OFFSET 命令的 ACK future：set_result("OFFSET_CURRENT:<ms>") / set_result("OFFSET_ERROR:<type> <arg>")
_offset_lock: asyncio.Lock | None = None        # 并发只允许 1 条 OFFSET 请求，避免 ACK 对歪

# ── 实时歌词推送 ──
_lyric_channel: str | None = None  # 歌词推送目标频道 ID
_last_lyric: str = ""              # 上次推送的歌词行，去重用
_lyric_quiet_after: float = 0      # 停播后静默时间戳
_lyric_song: str = ""              # 当前歌曲名，切歌时重置


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """TCP 客户端处理：读 AUTH + JSON 行 + 响应 SHOT_RESULT"""
    global _PC_DATA, _LAST_UPDATE, _client_writer, _lyric_song, _last_lyric, _lyric_quiet_after

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            writer.close(); return
        line = line.decode().strip()
        if not line.startswith("AUTH ") or line[5:] != _AUTH_KEY:
            logger.warning("PC 客户端 AUTH 失败: %s", line[:30])
            writer.close(); return

        _client_writer = writer
        logger.info("PC 客户端已连接")

        while True:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode().strip()
            if line_str.startswith("SHOT:"):
                # 长度前缀协议: SHOT:<len>\n<data>
                try:
                    data_len = int(line_str[5:])
                except ValueError:
                    continue
                b64_data = await reader.readexactly(data_len)
                b64 = b64_data.decode()
                shot_id = "latest"
                fut = _shots_pending.pop(shot_id, None)
                if fut and not fut.done():
                    fut.set_result(b64)
                continue
            # ══ 下行 offset 命令的 ACK：OFFSET_CURRENT:<ms> / OFFSET_ERROR:<type> <arg> ══
            if line_str.startswith("OFFSET_CURRENT:") or line_str.startswith("OFFSET_ERROR:"):
                global _offset_pending
                if _offset_pending is not None and not _offset_pending.done():
                    _offset_pending.set_result(line_str)
                    _offset_pending = None
                continue
            try:
                data = json.loads(line_str)
                _PC_DATA = data
                _LAST_UPDATE = time.time()
                # 每 30s 打一次数据心跳
                if not hasattr(_handle_client, '_data_heartbeat'):
                    _handle_client._data_heartbeat = 0  # type: ignore
                if time.time() - _handle_client._data_heartbeat > 30:  # type: ignore
                    md = data.get("music", {}) or {}
                    music_keys = list(md.keys())
                    peer = writer.get_extra_info('peername') if writer else '?'
                    logger.info("PC 数据心跳 [%s]: music=%s lyric_event=%s lyric='%s' keys=%d",
                               peer, music_keys[:9], bool(md.get("lyric_event")),
                               md.get("lyric_line", "-")[:50], len(data))
                    _handle_client._data_heartbeat = time.time()  # type: ignore

                # ── 实时歌词推送 ──
                music = data.get("music", {}) or {}
                song = music.get("song", "") or ""
                playing = music.get("playing", False)

                # ═══════════════════════════════════════════════════════════
                # v6.81 P0 兜底：切歌检测独立于 lyric_event。
                #   客户端 v6.79 切歌时存在 pending/slot 错位 bug，可能导致 lyric_event
                #   整段停发，但心跳 JSON 中的 music.song 已正常更新为新歌。
                #   本处即使本轮没收到 lyric_event，只要 song 字段发生了变化且歌词推送
                #   频道已开启，就立刻主动发一条兜底格式的切歌 intro 并重置去重缓存，
                #   避免从切歌那一刻起整条歌词链路静默（用户典型现象：最后一句旧歌歌词
                #   之后，KOOK 频道里十几秒都不显示任何新歌相关消息）。
                # ═══════════════════════════════════════════════════════════
                if song and song != _lyric_song:
                    old_song = _lyric_song
                    _lyric_song = song
                    _last_lyric = ""
                    if _lyric_channel:
                        intro_safe = song
                        fallback_intro = f"**\u25b6 {intro_safe}**"
                        logger.info(
                            "切歌兜底推送 [old=%s new=%s has_lyric_event=%s channel=%s]",
                            (old_song or "(空)")[:30],
                            intro_safe[:30],
                            bool(music.get("lyric_event")),
                            _lyric_channel,
                        )
                        asyncio.create_task(_send_lyric(fallback_intro))

                lyric_event = music.get("lyric_event", "")
                if lyric_event:
                    # 解析时间戳: "lyric_text|1754626800.123"
                    lyric_text = lyric_event
                    lyric_ts = 0.0
                    if "|" in lyric_event:
                        parts = lyric_event.rsplit("|", 1)
                        lyric_text = parts[0]
                        try:
                            lyric_ts = float(parts[1])
                        except ValueError:
                            pass
                    delay_ms = int((time.time() - lyric_ts) * 1000) if lyric_ts else -1
                    logger.info("收到 lyric_event [延迟=%dms]: '%s' song=%s playing=%s channel=%s",
                               delay_ms, lyric_text[:60], song[:30] if song else "无", playing, _lyric_channel)
                if lyric_event and _lyric_channel:
                    logger.info("进入发送逻辑: lyric_event有值=%s channel=%s", bool(lyric_event), _lyric_channel)
                    send_text = lyric_text  # 已在上面解析好的纯文本
                    # （兜底切歌检测已在上面执行过，此处 song!=_lyric_song 条件理论上永远为 False，
                    #  保留老代码不删以防万一兜底被跳过）
                    if song and song != _lyric_song:
                        _lyric_song = song
                        _last_lyric = ""
                    logger.info("去重检查: send_text='%s' _last_lyric='%s'", send_text[:30], _last_lyric[:30])
                    if send_text != _last_lyric:
                        _last_lyric = send_text
                        _lyric_quiet_after = 0
                        if _lyric_channel:
                            logger.info("准备发送 KOOK: channel=%s text=%s", _lyric_channel, send_text[:30])
                            # 用 khl.py Bot 异步发送（在当前 event loop 里调度）
                            asyncio.create_task(_send_lyric(send_text))
                    # 停播 → 10s 静默
                    if not playing and _lyric_quiet_after == 0:
                        _lyric_quiet_after = time.time() + 10
            except json.JSONDecodeError:
                pass
    except asyncio.TimeoutError:
        logger.warning("PC 客户端 AUTH 超时，断开")
    except ConnectionResetError as e:
        logger.warning("PC 客户端连接重置: %s", e)
    except Exception as e:
        logger.warning("PC 客户端异常断开: %s", e)
    finally:
        if _client_writer is writer:
            _client_writer = None
        try:
            writer.close()
        except Exception:
            pass
        logger.info("PC 客户端断开连接")


def _send_lyric_http(text: str, channel_id: str):
    """独立线程：HTTP 发歌词到 KOOK"""
    try:
        import requests
        # 尝试多种方式获取 token
        token = ""
        try:
            import services.sender as _s
            if _s._bot:
                token = _s._bot.client.token
        except Exception:
            pass
        if not token:
            try:
                import toml
                cfg = toml.load(Path(__file__).resolve().parent.parent / "config" / "bot_config.toml")
                token = cfg.get("kook", {}).get("token", "")
            except Exception:
                pass
        if not token:
            logger.error("KOOK_TOKEN 未找到")
            return
        resp = requests.post(
            "https://www.kookapp.cn/api/v3/message/create",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"type": 1, "target_id": channel_id, "content": text},
            timeout=5,
        )
        if resp.status_code == 200:
            logger.info("歌词已发送 KOOK: %s", text[:40])
        else:
            logger.warning("KOOK 发送失败 HTTP %d: %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.error("KOOK 发送异常: %s", e)


def get_pc_status() -> dict | None:
    if not _PC_DATA: return None
    if time.time() - _LAST_UPDATE > 30: return None
    return dict(_PC_DATA)


async def _send_lyric(text: str):
    """异步推送歌词到 KOOK 频道"""
    global _lyric_channel, _lyric_quiet_after, _last_lyric
    logger.info("_send_lyric 被调用: text=%s channel=%s", text[:30], _lyric_channel)
    try:
        if _lyric_quiet_after and time.time() > _lyric_quiet_after:
            _lyric_channel = None
            _lyric_quiet_after = 0
            _last_lyric = ""
            logger.info("歌词推送: 停播超时，自动关闭")
            return
        from khl import MessageTypes
        import services.sender as _sender
        if _sender._bot is None:
            logger.error("歌词推送失败: _bot 未初始化")
            return
        ch = await _sender._get_channel(_lyric_channel, is_group=True)
        if ch is None:
            logger.warning("歌词推送失败: 频道 %s 未获取", _lyric_channel)
            return
        import khl
        await _sender._bot.client.send(ch, text, type=khl.MessageTypes.KMD)
        logger.info("歌词已发送到 KOOK: %s", text[:40])
    except Exception as e:
        import traceback
        logger.error("歌词推送失败: %s\n%s", e, traceback.format_exc())


def set_lyric_channel(channel_id: str | None):
    """设置歌词推送频道，None 关闭"""
    global _lyric_channel, _last_lyric, _lyric_quiet_after, _lyric_song
    _lyric_channel = channel_id
    _last_lyric = ""
    _lyric_quiet_after = 0
    _lyric_song = ""


def get_lyric_channel() -> str | None:
    return _lyric_channel


# ════════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════════

def _fmt_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _fmt_speed(n: int) -> str:
    if n < 1024:
        return f"{n} B/s"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB/s"
    return f"{n/1024/1024:.2f} MB/s"


def _fmt_uptime(sec: int) -> str:
    d = sec // 86400
    h = (sec % 86400) // 3600
    m = (sec % 3600) // 60
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _bar_class(percent: float, warn: float = 70, danger: float = 90) -> str:
    """返回霓虹配色 CSS 类"""
    if percent >= danger:
        return "b-red"
    if percent >= warn:
        return "b-orange"
    return "b-pink"


def _mem_bar_class(percent: float) -> str:
    if percent >= 90:
        return "b-red"
    if percent >= 75:
        return "b-orange"
    return "b-cyan"


def _gpu_bar_class(percent: float) -> str:
    if percent >= 90:
        return "b-red"
    if percent >= 75:
        return "b-orange"
    return "b-green"


# ════════════════════════════════════════════════════════════
#  HTML 卡片构建
# ════════════════════════════════════════════════════════════

def _build_gpu_card(gpu: dict | None) -> str:
    """返回完整 GPU 卡片 HTML，无 GPU 返回空串"""
    if not gpu:
        return ""
    pct = gpu.get("gpu_percent", 0)
    mem_pct = gpu.get("mem_percent", 0)
    temp = gpu.get("temp", 0)
    name = gpu.get("name", "GPU")
    mem_used = _fmt_bytes(gpu.get("mem_used", 0))
    mem_total = _fmt_bytes(gpu.get("mem_total", 0))

    bar_cls = _gpu_bar_class(pct)
    mem_bar_cls = _gpu_bar_class(mem_pct)
    temp_cls = "b-green" if temp < 70 else ("b-orange" if temp < 85 else "b-red")

    return f'''
  <!-- GPU 卡 -->
  <div class="card c-gpu">
    <div class="head">
      <div class="head-icon">GPU</div>
      <div class="head-title">显卡</div>
      <div class="head-sub">{temp}C</div>
    </div>
    <div class="body">
      <div class="m-row">
        <span class="m-label">GPU</span>
        <div class="m-bar-w"><div class="m-bar {bar_cls}" style="width:{pct}%"></div></div>
        <span class="m-val">{pct}<span class="unit">%</span></span>
      </div>
      <div class="m-row">
        <span class="m-label">VRAM</span>
        <div class="m-bar-w"><div class="m-bar {mem_bar_cls}" style="width:{mem_pct}%"></div></div>
        <span class="m-val">{mem_pct}<span class="unit">%</span></span>
      </div>
      <div class="i-row"><span class="i-label">Model</span><span class="i-val">{name}</span></div>
      <div class="i-row"><span class="i-label">VRAM</span><span class="i-val">{mem_used} / {mem_total}</span></div>
    </div>
  </div>'''


def _build_disks_html(disks: list) -> str:
    if not disks:
        return '<div class="i-row"><span class="i-label">无可用磁盘</span></div>'
    rows = []
    for d in disks:
        drive = d.get("drive", "?")
        pct = d.get("percent", 0)
        free = _fmt_bytes(d.get("free", 0))
        total = _fmt_bytes(d.get("total", 0))
        bar_cls = "b-red" if pct >= 92 else ("b-orange" if pct >= 80 else "b-purple")
        rows.append(
            f'<div class="disk-row">'
            f'<span class="disk-drive">{drive}</span>'
            f'<div class="disk-bar-w"><div class="m-bar {bar_cls}" style="width:{pct}%"></div></div>'
            f'<span class="disk-val">{free} / {total}</span>'
            f'</div>'
        )
    return "\n".join(rows)


def _build_battery_html(battery: dict | None) -> str:
    if not battery:
        return ""
    pct = battery.get("percent", 0)
    plugged = battery.get("plugged", False)
    status = "AC" if plugged else "BAT"
    return f'<div class="i-row"><span class="i-label">Battery</span><span class="i-val">{pct}% ({status})</span></div>'


def _build_voltages_card(voltages: dict | None) -> str:
    """返回完整电压卡片 HTML，无电压返回空串"""
    if not voltages:
        return ""
    labels = {
        "cpu_vcore": "CPU Vcore",
        "gpu_vcore": "GPU Vcore",
        "dram": "DRAM",
        "v33": "+3.3V",
        "v5": "+5V",
        "v12": "+12V",
    }
    cells = []
    for key, label in labels.items():
        val = voltages.get(key)
        if val is not None:
            cells.append(
                f'<div class="v-cell"><span class="v-name">{label}</span>'
                f'<span class="v-val">{val}V</span></div>'
            )
    if not cells:
        return ""
    return f'''
  <!-- 电压卡 -->
  <div class="card c-volt">
    <div class="head">
      <div class="head-icon">VLT</div>
      <div class="head-title">电压</div>
    </div>
    <div class="body">
      <div class="volt-grid">{"".join(cells)}</div>
    </div>
  </div>'''


def _build_music_html(music: dict | None) -> str:
    if not music:
        return '<div class="music-empty">未播放</div>'

    song = music.get("song", "")
    if not song:
        return '<div class="music-empty">未播放</div>'

    player = music.get("player", "")
    cover = music.get("cover", "")
    if cover:
        cover = cover.replace("128y128", "500y500")
    progress_ms = music.get("progress_ms", 0)
    duration_str = music.get("duration", "")
    playing = music.get("playing", False)
    lyric_line = music.get("lyric_line", "") or music.get("lyric", "")

    progress_pct = 0
    cur_str = "0:00"
    if duration_str and progress_ms > 0:
        parts = duration_str.split(":")
        if len(parts) == 2:
            dur_sec = int(parts[0]) * 60 + int(parts[1])
            if dur_sec > 0:
                pos = int(progress_ms / 1000)
                progress_pct = round(pos / dur_sec * 100, 1)
                cur_str = f"{pos // 60}:{pos % 60:02d}"

    status_cls = "playing" if playing else "paused"
    status_text = "PLAYING" if playing else "PAUSED"

    cover_html = f'<img class="music-cover" src="{cover}">' if cover else '<div class="music-cover" style="display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--text-3)">--</div>'

    lyric_html = f'<div class="music-lyric">{lyric_line}</div>' if lyric_line else ""

    return f"""
    <div class="music-cover-wrap">
      {cover_html}
      <div class="music-info">
        <div class="music-song">{song}</div>
        <div class="music-player">{player}</div>
        <span class="music-status {status_cls}">{status_text}</span>
        <div class="music-progress-w"><div class="music-progress" style="width:{progress_pct}%"></div></div>
        <div class="music-time"><span>{cur_str}</span><span>{duration_str}</span></div>
      </div>
    </div>
    {lyric_html}"""


def build_sys_card_html(owner: str = "Trusler", bot_name: str = "幻梦") -> str | None:
    """构建系统状态 HTML 卡片。无数据返回 None。"""
    data = get_pc_status()
    if not data:
        return None

    from datetime import datetime

    tpl_path = Path(__file__).resolve().parent.parent / "data" / "templates" / "sys_card.html"
    if not tpl_path.exists():
        return None
    html = tpl_path.read_text(encoding="utf-8")

    # 基础
    hostname = data.get("hostname", "未知")
    boot_time_ts = data.get("boot_time", 0)
    uptime_sec = data.get("uptime", 0)
    if boot_time_ts:
        boot_str = datetime.fromtimestamp(boot_time_ts).strftime("%Y-%m-%d %H:%M:%S")
    else:
        boot_str = "未知"
    uptime_str = _fmt_uptime(uptime_sec) if uptime_sec else "未知"

    # CPU
    cpu_pct = data.get("cpu_percent", 0)
    cpu_count = data.get("cpu_count", 0)
    cpu_freq = data.get("cpu_freq", 0)
    cpu_bar_cls = _bar_class(cpu_pct)

    # GPU
    gpu_card = _build_gpu_card(data.get("gpu"))

    # 内存
    mem = data.get("memory", {})
    mem_pct = mem.get("percent", 0)
    mem_used = _fmt_bytes(mem.get("used", 0))
    mem_total = _fmt_bytes(mem.get("total", 0))
    mem_bar_cls = _mem_bar_class(mem_pct)
    swap_pct = data.get("swap", {}).get("percent", 0)

    # 磁盘
    disks = data.get("disks", [])
    disks_html = _build_disks_html(disks)
    disk_count = len(disks)

    # 网络
    net = data.get("net", {})
    net_up = _fmt_speed(net.get("upload", 0))
    net_down = _fmt_speed(net.get("download", 0))

    # 电池
    battery_html = _build_battery_html(data.get("battery"))

    # 电压
    voltages_card = _build_voltages_card(data.get("voltages"))

    # 进程数
    proc_count = data.get("proc_count", 0)

    # 窗口
    window_title = data.get("window", "(无活动窗口)")
    window_app = data.get("app", "")

    # 音乐
    music_html = _build_music_html(data.get("music"))

    # 时间
    now_str = datetime.now().strftime("%H:%M:%S")

    # 替换
    html = html.replace("{{HOSTNAME}}", hostname)
    html = html.replace("{{OWNER}}", owner)
    html = html.replace("{{BOOT_TIME}}", boot_str)
    html = html.replace("{{UPTIME}}", uptime_str)
    html = html.replace("{{PROC_COUNT}}", str(proc_count))
    html = html.replace("{{CPU_PERCENT}}", str(cpu_pct))
    html = html.replace("{{CPU_BAR_CLASS}}", cpu_bar_cls)
    html = html.replace("{{CPU_FREQ}}", str(cpu_freq))
    html = html.replace("{{CPU_COUNT}}", str(cpu_count))
    html = html.replace("{{GPU_CARD}}", gpu_card)
    html = html.replace("{{MEM_PERCENT}}", str(mem_pct))
    html = html.replace("{{MEM_BAR_CLASS}}", mem_bar_cls)
    html = html.replace("{{MEM_USED}}", mem_used)
    html = html.replace("{{MEM_TOTAL}}", mem_total)
    html = html.replace("{{SWAP_PERCENT}}", str(swap_pct))
    html = html.replace("{{DISK_COUNT}}", str(disk_count))
    html = html.replace("{{DISKS_HTML}}", disks_html)
    html = html.replace("{{NET_UP}}", net_up)
    html = html.replace("{{NET_DOWN}}", net_down)
    html = html.replace("{{BATTERY_HTML}}", battery_html)
    html = html.replace("{{VOLTAGES_CARD}}", voltages_card)
    html = html.replace("{{WINDOW_TITLE}}", window_title)
    html = html.replace("{{WINDOW_APP}}", window_app)
    html = html.replace("{{MUSIC_HTML}}", music_html)
    html = html.replace("{{UPDATE_TIME}}", now_str)
    html = html.replace("{{BRAND}}", f"Generated by {bot_name}")

    return html


def format_pc_status(owner: str = "管理员") -> str:
    """纯文本格式（保留兼容）"""
    data = get_pc_status()
    if not data:
        return "暂无 PC 状态数据（可能未开机或未运行采集脚本）"

    lines = []
    hostname = data.get("hostname", "未知")
    window = data.get("window", "")
    app = data.get("app", "")
    music = data.get("music", {})

    lines.append(f"[PC] {owner}'s {hostname}")

    if window:
        parts = [f"前台: {window}"]
        if app: parts.append(f"({app})")
        lines.append(" ".join(parts))
        # 新字段：句柄数 + 内存 + FPS
        handles = data.get("app_handles", 0)
        mem_mb = data.get("app_mem_mb", 0)
        fps = data.get("fps", 0)
        if handles or mem_mb or fps:
            detail_parts = []
            if handles: detail_parts.append(f"句柄{handles}")
            if mem_mb: detail_parts.append(f"内存{mem_mb}MB")
            if fps: detail_parts.append(f"{fps}FPS")
            lines.append(f"进程: {' '.join(detail_parts)}")
    else:
        lines.append("前台: (无)")

    # 开机时长
    boot = data.get("boot_time", 0)
    uptime = data.get("uptime", 0)
    if uptime:
        d = uptime // 86400; h = (uptime % 86400) // 3600; m = (uptime % 3600) // 60
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        parts.append(f"{m}m")
        lines.append(f"开机: {''.join(parts)}")

    if music:
        song = music.get("song", "")
        player = music.get("player", "")
        lyric_line = music.get("lyric_line", "") or music.get("lyric", "")
        cover = music.get("cover", "")
        progress_ms = music.get("progress_ms", 0)
        duration_str = music.get("duration", "")
        playing = music.get("playing", False)
        if song:
            status = "播放" if playing else "暂停"
            player_str = f" ({player})" if player else ""
            lines.append(f"音乐: [{status}] {song}{player_str}")
            if cover:
                cover = cover.replace("128y128", "500y500")
                lines.append(f"[img:{cover}]")
            if duration_str and progress_ms > 0:
                parts = duration_str.split(":")
                dur_sec = int(parts[0])*60 + int(parts[1]) if len(parts)==2 else 0
                if dur_sec > 0:
                    pos = int(progress_ms/1000)
                    cur_str = f"{pos//60}:{pos%60:02d}"
                    bar_len = 20
                    filled = int(pos/dur_sec*bar_len) if dur_sec else 0
                    bar = "="*filled + "-"*(bar_len-filled)
                    lines.append(f"进度: {bar} {cur_str} / {duration_str}")
            if lyric_line:
                lines.append(f"歌词: {lyric_line}")
        elif not music.get("hasSong", True):
            lines.append("音乐: (未播放)")
    else:
        lines.append("音乐: (未播放)")

    return "\n".join(lines)


async def request_screenshot(timeout: float = 30.0) -> str | None:
    """向 PC 客户端请求截屏，返回 base64 JPEG。无连接返回 None"""
    if _client_writer is None:
        return None
    shot_id = "latest"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _shots_pending[shot_id] = fut
    try:
        _client_writer.write(b"CMD:SHOT\n")
        await _client_writer.drain()
        b64 = await asyncio.wait_for(fut, timeout=timeout)
        return b64 if b64 else None
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def request_lyric_offset(sub: str, arg: str = "", timeout: float = 8.0) -> tuple[str, int | None]:
    """远程下发 CMD:OFFSET_SET/ADD/RESET/GET 到 PC 客户端并等 ACK。

    sub: SET | ADD | RESET | GET
    arg: SET/ADD 时是毫秒整数字符串；RESET/GET 时传 ""
    返回 (status_msg, current_offset_ms_or_None)
      status_msg 是给用户直接看的中文结果；current_offset_ms 成功时返回当前 offset，失败/未连接返回 None
    """
    global _offset_pending, _offset_lock
    sub_ok = {"SET", "ADD", "RESET", "GET"}
    sub_u = str(sub).strip().upper()
    if sub_u not in sub_ok:
        return (f"❌ 内部错误：未知 OFFSET 子命令 {sub!r}", None)

    if _client_writer is None:
        return ("❌ PC 客户端未连接，请先确保运行 `pc_status_reporter.py` 且进程在线（看日志：PC 客户端已连接）。\n"
                "连接后再发 `.lyric offset xxx` 即可。", None)

    if _offset_lock is None:
        # 首次调用时初始化锁（懒加载，避免模块导入时没 event loop 报 RuntimeError）
        _offset_lock = asyncio.Lock()
    async with _offset_lock:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        # 先把 Future 占上，ACK 来了就不会丢
        _offset_pending = fut
        try:
            if sub_u == "RESET":
                line = "CMD:OFFSET_RESET\n"
            elif sub_u == "GET":
                line = "CMD:OFFSET_GET\n"
            else:  # SET / ADD
                line = f"CMD:OFFSET_{sub_u} {str(arg).strip()}\n"
            _client_writer.write(line.encode("utf-8"))
            await _client_writer.drain()
            logger.info("下发 OFFSET 命令: %r (timeout=%.1fs)", line.strip(), timeout)
            ack = await asyncio.wait_for(fut, timeout=timeout)
            logger.info("收到 OFFSET ACK: %r", ack)
            if ack.startswith("OFFSET_CURRENT:"):
                try:
                    ms = int(ack.split(":", 1)[1].strip())
                except Exception:
                    return (f"❌ 客户端返回值解析失败: {ack!r}", None)
                sign = "提前" if ms < 0 else ("延后" if ms > 0 else "准时")
                sec = abs(ms) / 1000.0
                desc_map = {
                    "SET": "✅ 已下发「设置」歌词偏移",
                    "ADD": "✅ 已下发「微调」歌词偏移",
                    "RESET": "✅ 已「重置」歌词偏移",
                    "GET": "📋 当前歌词偏移",
                }
                header = desc_map.get(sub_u, "✅ 偏移操作结果")
                extra = ""
                if sub_u in ("SET", "ADD", "RESET"):
                    extra = "\n✨ 即刻生效（无需重启任何进程），`_save_local_config()` 已写入 `%USERPROFILE%\\.huanmeng_kook\\lyric_offset.json`，下次启动自动沿用。"
                return (f"{header}：**{ms} ms**（≈{sign} {sec:.2f} 秒）{extra}", ms)
            if ack.startswith("OFFSET_ERROR:"):
                body = ack.split(":", 1)[1].strip()
                if body.upper().startswith("INVALID_ARG"):
                    arg_bad = body.split(None, 1)[1] if " " in body else ""
                    return (f"❌ offset 数值格式错误：`{arg_bad}`\n"
                            "正确写法：`.lyric offset -3000`（整数毫秒，负数=提前发送，正数=延后）", None)
                if body.upper().startswith("UNKNOWN"):
                    return (f"❌ 客户端不支持该 OFFSET 子命令：`{body}`", None)
                return (f"❌ 客户端处理 OFFSET 失败：{body}", None)
            return (f"❌ 未知 ACK 格式：{ack!r}", None)
        except asyncio.TimeoutError:
            # 超时清理 pending future，避免下次污染
            try:
                if _offset_pending is fut:
                    _offset_pending = None
            except Exception:
                pass
            return (f"⏱ 客户端 {timeout:.0f} 秒内没有回应 OFFSET 命令。\n"
                    "可能原因：\n"
                    "  ① pc_status_reporter.py 版本较老（预埋 CMD:OFFSET_* 协议是新版），请先更新客户端文件再重启；\n"
                    "  ② 客户端卡死 / 网络断连（心跳日志不出现），请重启 pc_status_reporter.py。", None)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            try:
                if _offset_pending is fut:
                    _offset_pending = None
            except Exception:
                pass
            return (f"❌ PC 客户端连接中断（{type(e).__name__}），请重启 pc_status_reporter.py。", None)
        except Exception as e:
            import traceback
            logger.error("request_lyric_offset 异常: %s\n%s", e, traceback.format_exc())
            try:
                if _offset_pending is fut:
                    _offset_pending = None
            except Exception:
                pass
            return (f"❌ 内部异常：{type(e).__name__}: {e}", None)


async def start_pc_server(port: int = 62002):
    server = await asyncio.start_server(_handle_client, "0.0.0.0", port, limit=4_194_304)
    logger.info("PC 状态 TCP 接收端: 0.0.0.0:%d", port)
    return server


# ── 手机状态 ────────────────────────────────────────────

_PHONE_DATA: dict = {}
_PHONE_LAST_UPDATE: float = 0
_PHONE_client_writer: asyncio.StreamWriter | None = None
_PHONE_shots_pending: dict[str, asyncio.Future] = {}


async def _handle_phone_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global _PHONE_DATA, _PHONE_LAST_UPDATE, _PHONE_client_writer
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            writer.close(); return
        line = line.decode().strip()
        if not line.startswith("AUTH ") or line[5:] != _AUTH_KEY:
            logger.warning("Phone 客户端 AUTH 失败")
            writer.close(); return
        _PHONE_client_writer = writer
        logger.info("Phone 客户端已连接")
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=150)
            if not line:
                break
            try:
                data = json.loads(line.decode().strip())
                _PHONE_DATA = data
                _PHONE_LAST_UPDATE = time.time()
            except json.JSONDecodeError:
                pass
    except (asyncio.TimeoutError, ConnectionResetError, ConnectionAbortedError):
        pass
    except Exception as e:
        logger.warning("Phone 客户端异常: %s", e)
    finally:
        _PHONE_client_writer = None
        writer.close()


def get_phone_status() -> dict:
    """获取最新手机状态数据"""
    if _PHONE_LAST_UPDATE and (time.time() - _PHONE_LAST_UPDATE) < 300:
        return dict(_PHONE_DATA)
    return {}


def format_phone_status(owner: str = "管理员") -> str:
    data = get_phone_status()
    if not data:
        return "暂无手机状态数据（可能未连接或未运行采集脚本）"
    lines = []
    hostname = data.get("hostname", data.get("device", "未知"))
    lines.append(f"[Phone] {owner}'s {hostname}")

    window = data.get("window", "")
    app = data.get("app", "")
    if window:
        parts = [f"前台: {window}"]
        if app: parts.append(f"({app})")
        lines.append(" ".join(parts))

    # 电池：Android 上报为嵌套对象 {percent, charging, temperature, ...}
    battery = data.get("battery", None)
    if isinstance(battery, dict):
        pct = battery.get("percent", -1)
        charging = battery.get("charging", False)
        if pct >= 0:
            lines.append(f"电量: {pct:.0f}% ({'充电中' if charging else '放电'})")
    elif isinstance(battery, (int, float)) and battery >= 0:
        charging = "充电中" if data.get("charging") else "放电"
        lines.append(f"电量: {battery}% ({charging})")

    # CPU：Android 上报 cpu_freq + cpu_temp
    cpu_freq = data.get("cpu_freq", -1)
    cpu_temp = data.get("cpu_temp", -1)
    cpu_count = data.get("cpu_count", 0)
    cpu_parts = []
    if cpu_count: cpu_parts.append(f"{cpu_count}核")
    if cpu_freq > 0: cpu_parts.append(f"{cpu_freq:.1f}GHz")
    if cpu_temp > 0: cpu_parts.append(f"{cpu_temp:.0f}°C")
    if cpu_parts:
        lines.append(f"CPU: {' '.join(cpu_parts)}")

    # 内存：Android 上报 bytes，转换为 GB
    mem = data.get("memory", {})
    if mem:
        used = mem.get("used", 0)
        total = mem.get("total", 0)
        if total and total > 1024 * 1024:  # > 1MB → 按 GB 显示
            used_gb = used / (1024**3)
            total_gb = total / (1024**3)
            pct = mem.get("percent", used * 100 // total if total else 0)
            lines.append(f"内存: {used_gb:.1f}/{total_gb:.1f}GB ({pct:.0f}%)")
        elif total:
            lines.append(f"内存: {used}/{total}B ({used*100//total}%)")

    # 网络：Android 上报 net 对象
    net = data.get("net", {})
    if net:
        ips = [f"{k}={v}" for k, v in net.items() if v]
        if ips:
            lines.append(f"网络: {', '.join(ips)}")

    # 存储
    disks = data.get("disks", {})
    if disks:
        for path, info in disks.items():
            if isinstance(info, dict):
                free = info.get("free", 0)
                total = info.get("total", 0)
                if total and total > 1024 * 1024:
                    free_gb = free / (1024**3)
                    total_gb = total / (1024**3)
                    lines.append(f"存储: {free_gb:.1f}/{total_gb:.1f}GB")

    return "\n".join(lines)


async def request_phone_screenshot(timeout: float = 30.0) -> str | None:
    if _PHONE_client_writer is None:
        return None
    import uuid
    req_id = uuid.uuid4().hex[:8]
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _PHONE_shots_pending[req_id] = fut
    try:
        _PHONE_client_writer.write(f'{{"cmd":"SHOT","id":"{req_id}"}}\n'.encode())
        await _PHONE_client_writer.drain()
        b64 = await asyncio.wait_for(fut, timeout=timeout)
        return b64
    except asyncio.TimeoutError:
        return None
    finally:
        _PHONE_shots_pending.pop(req_id, None)


async def start_phone_server(port: int = 62003):
    server = await asyncio.start_server(_handle_phone_client, "0.0.0.0", port, limit=4_194_304)
    logger.info("Phone 状态 TCP 接收端: 0.0.0.0:%d", port)
    return server
