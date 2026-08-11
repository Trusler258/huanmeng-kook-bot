"""
Mac 状态上报 v1.20（同步 PC v6.80，周期性卡慢循环双根修 + TIMER快速补发）
纯 macOS 原生媒体检测：AppleScript 查询 Music.app + Spotify
完全脱离 Windows SMTC / Win32 API

依赖: pip install requests
可选: pip install Pillow (截屏)

══ v1.20 P0 双根修（同步 PC v6.80，解决「卡一下→慢→慢慢对齐→又卡」周期性卡慢 + TIMER被DROP等tick追 导致卡）══
  P0-1 进度滤波相同y值不入窗（根治周期性卡慢循环）
  - 根因：Music/Spotify AppleScript 查询 progress 有时 1s 才刷新一轮，主循环多次相同y
    → 旧逻辑 5 点滑窗全是相同 y → 最小二乘拟合斜率≈0 → clamp 到 900 → pred 比真实慢 10%
    → fused = 0.6×pred(慢)+0.4×raw(停) → eff_ms 卡死原地 → tick 算 idx 不推进 → 歌词"卡一下"
    → 下个进度刷新 → 滑窗混入新点 → rate 慢慢回到 1000 → "慢→慢慢对齐" → 下个周期又卡
  - 修复：playing 且 pos_ms == 滑窗最后一条 y 且（墙上距离 < 2s）→ 不入窗污染样本
    → 滑窗里只剩真实变化的点 → 拟合 rate 精确 ≈1000 → eff_ms 平滑增长，无周期性卡慢
  - 兜底：墙上距离 ≥2s 的重复点仍强制入窗，避免窗口太旧 pred 外推过远
  - +seek跳变检测：last_pred 与当前 pos_ms 差 >3000ms → 重置窗口（同步PC版）

  P0-2 TIMER 顺序放宽容忍 3 句（diff=2/3 快速补发，不再DROP等tick）
  - 根因：旧逻辑 next_idx > last+1 → DROP（等 tick 兜底逐句补）
    当进度滤波卡慢或 tick 抖动导致 last 停在 X，而 TIMER 精确到了 X+2 → DROP → 等 tick 80ms×N 慢慢追
    → 用户感觉"TIMER 明明到了但没词，等半天 tick 才推出来 → 卡一下"
  - 修复：diff ∈ {2,3} → 从 last+1 到 next_idx-1 每句快速补发（v1.19 歌词事件队列缓冲不覆盖）
    补发完 last == next_idx-1 → 复用原代码精确发 next_idx（含 drift 计算 + TIMER 链延续不断）
    diff ≥4 → 仍 DROP（判定 seek/切歌，防串歌乱发）
  - 日志新增：TIMER:FILL+1 / TIMER:FILL+2 标记快速补发了几句，TIMER:DROP_DIFF_GE4 标记 seek 类丢弃

══ v1.19 P0 根修（同步 PC v6.79，按用户日志：「我好想你」「纷飞的回忆」间隔81ms→主循环80ms周期覆盖丢失）══
  - 核心bug：主循环周期~80ms，<80ms内连续写≥2句lyric_event（如idx=16→17间隔81ms）→第1句被覆盖彻底丢失
  - 架构：保留 `_lyric_pending` 布尔位作为 99% 场景的快速检查位，加 `_lyric_event_queue(deque maxlen=64)` 做溢出缓冲
  - 写入端(_stage_lyric_event)：槽空→直接写；槽被占→入队；切歌时清空队列防串歌
  - 读取端(主循环)：消费完槽→从队首补入→置pending→下一轮继续发，保证零丢失
  - Lock→RLock：允许同一线程嵌套加锁（_stage_lyric_event常被with _state_lock内的代码调用）
  - 7 处写入路径统一：补位循环/TIMER/切歌intro(两处)/暂停恢复/FORCE/tick

══ v1.18 日志合并（同步 PC v6.78，按用户要求：gap分析并入歌词行，不再单独分行）══
  - 4 处歌词推送路径（补位循环/TIMER/FORCE/tick）统一把 [real_gap] N | LRC_gap=Xms 追加在歌词行末尾
  - 删除所有独立的 gap_line / gap_analysis_line / gap_for_line log() 调用
  - 示例（合并前 2 行 → 合并后 1 行）：
    旧：歌词(定时) [xx] idx=4: xxx
        [1712ms] 5  | LRC_gap=1699ms
    新：歌词(定时) [xx] idx=4: xxx  | [1712ms] 5  | LRC_gap=1699ms

══ v1.17 彻底去熔断 + 请求重试提速（同步 PC v6.77，按用户要求解决「Ice Paper - 心如止水」搜不到）══
  - 强制移除 P1-4 熔断机制：删_CIRCUIT_BREAK状态/3个函数/快速路径return None
    避免「3次抖动超时→5min跳过」导致15min搜不到必然存在的歌
  - 降重试 5→2 次：单源最多尝试3次，避免5次2.5s软超时叠加越过 T5=18s 总超时
  - 重试退避改常量 1.0s：不再 0.25/0.5/1/2/4 指数递增（原退避合计空耗 7.75s）
  - 去线程级硬超时包装：ThreadPoolExecutor的Future.cancel只能杀pending，running HTTP仍卡worker
    直接依赖 requests timeout（urllib3 底层 socket select 真生效）

══ v1.16 P1-2 致命量纲修复（同步 PC v6.76，根治 drift=-2000~-3800ms 歌词提前 2~4 秒）══
  - 原首尾两点拟合波动大且量纲混乱，改为与 PC v6.76 一致的最小二乘拟合：x=wall秒 y=pos_ms → rate=ms/秒≈1000
  - rate clamp [900,1100]（0.9x~1.1x），防止暂停/切歌残点拟合出 <100 或 >2000 的乱率
  - pred clamp |pred-pos_ms|>5000ms：单轮拟合异常直接降级用 pos_ms，免进度一次性拉飞

══ v1.15 同步 PC v6.75 两项调整 ══
  - _LYRIC_SYNC_LOG 默认 True → False：正常运行不刷 [LYRIC_SYNC]，排障时临时改回 True
  - 翻译优先：_score_lyric_match 新增「翻译有效行数≥3 且 ≥主歌词50%」+0.5 超高权重

══ v1.14 重大整改（同步 PC v6.74 P1-2/P1-3/P1-5，解决「进度抖跳变/TCP粘包死连/歌词首命中质量差」）══
  P1-2 进度平滑滤波：全局滑窗 _progress_window (max=5条)，≥3条时首尾两点拟合播放速率系数，0.6×预测+0.4×实测融合；跳变>3s时重置窗口（seek/切歌检测）
  P1-3 TCP粘包+死连接检测：socks dict 结构升级为 {sock,buf,last_recv_ts,last_send_ts}；SO_KEEPALIVE+TCP_NODELAY双保险；recv数据写入buf再按b"\n"拆完整行；30s无收发主动发HEARTBEAT心跳
  P1-5 阶梯提交+最优匹配评分：新增_score_lyric_match综合评分（字符重合/时长吻合/行数合理/源偏好）；MIN_WAIT_S=1.5s第一阶段仅提交简体×(LRCLIB精准+网易云+QQ)=3条；窗口内保留最高分best_result，超时才返回最优

══ v1.13 重大整改（同步 PC v6.73 P0+P1-4，解决「主线程卡死/with空等11s/HTTP卡线程/慢源占死池」）══
  P0-1 根除主线程卡死：log()砍所有stderr同步写兜底（sys.stderr.write/traceback.print_exc全删），异常栈统一入日志队列由后台worker输出；队列≥80%激进丢[LYRIC_SYNC]/[LYRIC_PROFILE]两类诊断；[LOG_DROP]永远不fallback stderr写
  P0-2 全局常驻线程池：废弃with ThreadPoolExecutor（with退出shutdown(wait=True)必须等running HTTP任务结束=空等11s），_LYRIC_EXECUTOR全局单例常驻，命中/超时仅cancel pending不等待running任务结束；封面搜索也复用全局池
  P0-3 线程全链路兜底：所有后台线程daemon=True；新增每5min健康自检（活跃线程数超阈值告警）
  P1-4 HTTP层深度优化：全局_HTTP_SESSION(requests.Session)复用TCP/TLS连接（握手开销↓30%+）；所有HTTP请求套双层超时（requests.timeout + 线程级强制硬超时，卡死1s内强制释放）；单源熔断（同源连续3次失败→熔断5min，窗口内直接跳过不请求）

══ v1.12 修复（同 PC v6.72，解决「歌词搜索啥都炸 / T3命中后等6.5s才写T5 / 精准404重复 / 网易云QQ超时无日志」用户反馈）══
  - LRCLIB 精准/模糊彻底拆成 precise_only / fuzzy_only 两个独立函数：模糊**绝不做 duration 精准过滤**，不再 2 倍重复 HTTP + 占满线程槽
  - 两轮首命中 as_completed → wait step100ms 轮询，命中立刻 cancel 所有 pending+立即 break，砍 PC 实测同款「T3_hit→T5_ok 空等 6.5s」
  - 彻底移除酷狗歌词/酷狗封面 4 函数（仅剩 LRCLIB+网易云+QQ音乐 歌词源，QQ音乐 独家封面源）
  - _run_single_lyric_fetch ANY 异常打 SINGLE_EXC fn=xxx 日志，不再静默吞；超时场景 pending/done 数全打日志，不再 6s 超时后全无声
  - banner 文案全同步实际参数：LRCLIB=1次×2s，网易云/QQ=2.5s 单请求超时，封面 QQ音乐独家单平台
"""
"""
═══════════════════════════════════════════════════════════
⚠️  必须设置的环境变量（全清默认值，避免硬编码端口/密钥/域名）：
    export BOT_SERVER="你的服务器域名或IP"
    export BOT_PC_PORTS="端口1,端口2"
    export BOT_PC_KEY="与服务器约定的 AUTH 密钥"
    未设置会启动报错并提示。
═══════════════════════════════════════════════════════════
"""
import json, time, os, socket, threading, traceback, sys, concurrent.futures, queue, collections
import os as _os

# ─────────────────────────────────────────────────────────
# ⚠️  服务器配置：端口/密钥/域名 全部清空默认值，必须环境变量显式指定
#     （专门的 mac 版，避免任何硬编码痕迹）
# ─────────────────────────────────────────────────────────
SERVER = _os.environ.get("BOT_SERVER", "")
_default_ports = ""  # ══ Mac 版：默认端口清空，必须设置
PORTS = [int(p.strip()) for p in _os.environ.get("BOT_PC_PORTS", _default_ports).split(",") if p.strip()]
AUTH_KEY = _os.environ.get("BOT_PC_KEY", "")  # ══ Mac 版：密钥默认清空，必须设置

# ═══════════════════════════════════════════════════════════════
# 非阻塞日志（v1.13 P0-1 根治「日志洪灾 stderr 兜底→主线程 C 层 write 阻塞=^C杀不掉」— 同 PC v6.73）
#   主线程/任意 thread 调 log() → put_nowait 入有界队列（永不卡）
#   独立 worker daemon 负责实际写 stdout + flush（worker 即便阻塞写终端也不影响主线程 Ctrl+C）
#   ══ v1.13 激进策略（全链路 NO stderr 同步写）：
#     ① 队列 ≥ 80% 水位：[LYRIC_SYNC] / [LYRIC_PROFILE] 两类诊断**直接丢，不入队**，永远给核心行腾位置
#     ② 队列满：仅做「诊断置换腾位」，失败直接丢，**永不 fallback sys.stderr 同步写**（含 [LOG_DROP] 汇总）
#     ③ worker 内部异常：仅静默 drop，绝不用 traceback.print_exc(stderr) 反压调用方
#     ④ 任何异常栈统一调用 log(traceback.format_exc()) → 入日志队列，由后台线程输出
# ═══════════════════════════════════════════════════════════════
_LOG_QUEUE_MAX = 2000
_LOG_QUEUE_HIGH_WM = int(_LOG_QUEUE_MAX * 0.8)  # 1600 — 超过即触发激进丢诊断
_log_queue: "queue.Queue[tuple[str, float, bool]]" = queue.Queue(maxsize=_LOG_QUEUE_MAX)
_log_lock = threading.Lock()
_log_drop_total = 0
_log_drop_diag = 0           # [LYRIC_SYNC]/[LYRIC_PROFILE] 类诊断丢计数
_log_drop_last_report_ts = 0.0
_LOG_DROP_REPORT_INTERVAL = 5.0  # s

def _log_worker():
    """后台 stdout 写 worker — 独立 daemon 线程，阻塞写终端不影响主循环
    ══ v1.13：内部任何异常**永不写 stderr**，彻底根除反压卡死链路（同 PC v6.73）"""
    import sys as _sys
    stdout = _sys.stdout
    while True:
        try:
            item = _log_queue.get()
        except Exception:
            continue
        try:
            ts_str, msg, _ = item
            line = f"[{ts_str}] {msg}\n"
            try:
                stdout.write(line)
                stdout.flush()
            except Exception:
                # stdout 本身报错（如关闭的管道）**静默 drop**，绝不 fallback 写 stderr
                pass
        except Exception:
            # worker 自身异常直接吞：不打 stderr、不崩 worker、不反压任何线程
            pass
        finally:
            try:
                _log_queue.task_done()
            except Exception:
                pass

# 启动日志 worker（daemon=True — 主进程退出时自动 kill）
_log_worker_thread = threading.Thread(target=_log_worker, name="log_worker", daemon=True)
_log_worker_thread.start()
del _log_worker_thread

def log(msg):
    """
    ══ v1.13 P0-1 非阻塞日志入口（主线程永远零阻塞，永不写 stderr — 同 PC v6.73）：
      - 队列≥80% 高水位：[LYRIC_SYNC] / [LYRIC_PROFILE] 两类诊断 直接丢 不入队
      - 队列满 put_nowait 失败 → 诊断置换腾位 → 失败直接丢
      - [LOG_DROP] 汇总：入队失败直接丢，**永不 fallback stderr 同步写**
      - 累计丢计数 5s 节流打汇总
    """
    global _log_drop_total, _log_drop_diag, _log_drop_last_report_ts
    ts = time.strftime("%H:%M:%S")
    # ══ v1.13 可丢性判定（两类诊断统一）：
    is_diag = (isinstance(msg, str) and (
        msg.startswith("[LYRIC_SYNC]")
        or msg.startswith("[LYRIC_PROFILE]")
    ))
    # ══ v1.13 激进丢包：队列≥80% 时，诊断类直接跳过不入队（不占队列空间，不阻塞，不计数）
    if is_diag and _log_queue.qsize() >= _LOG_QUEUE_HIGH_WM:
        with _log_lock:
            _log_drop_total += 1
            _log_drop_diag += 1
    else:
        dropped_any = False
        try:
            _log_queue.put_nowait((ts, msg, is_diag))
        except queue.Full:
            dropped_any = True
        if dropped_any:
            swapped = False
            if is_diag:
                with _log_lock:
                    _log_drop_total += 1
                    _log_drop_diag += 1
            else:
                # 非诊断 → 尝试把队头一条诊断 pop 掉，腾位置再入队本条
                head = None
                try:
                    head = _log_queue.get_nowait()
                except queue.Empty:
                    head = None
                if head is not None:
                    _, _, head_diag = head
                    if head_diag:
                        with _log_lock:
                            _log_drop_total += 1
                            _log_drop_diag += 1
                        try:
                            _log_queue.put_nowait((ts, msg, is_diag))
                            swapped = True
                        except queue.Full:
                            swapped = False
                            with _log_lock:
                                _log_drop_total += 1
                    else:
                        # 队头也是核心行，不能丢 → 塞回去，本条直接丢
                        try:
                            _log_queue.put_nowait(head)
                        except queue.Full:
                            pass
                        with _log_lock:
                            _log_drop_total += 1
                else:
                    with _log_lock:
                        _log_drop_total += 1
    # 丢计数汇总报告（5s 节流）
    now = time.time()
    need_report = False
    total = 0
    diag = 0
    with _log_lock:
        if (_log_drop_total > 0) and (now - _log_drop_last_report_ts >= _LOG_DROP_REPORT_INTERVAL):
            total = _log_drop_total
            diag = _log_drop_diag
            _log_drop_last_report_ts = now
            _log_drop_total = 0
            _log_drop_diag = 0
            need_report = True
    if need_report:
        # ══ v1.13 P0-1：[LOG_DROP] 也只用 put_nowait 入队，**永不 fallback stderr 同步写**，哪怕丢了也不反压
        rpt = f"[LOG_DROP] 过去 5s 静默丢弃 {total} 条日志（其中诊断 {diag} 条）— 队列≥{_LOG_QUEUE_HIGH_WM}高水位已自动激进丢[LYRIC_SYNC]/[LYRIC_PROFILE]"
        try:
            ts2 = time.strftime("%H:%M:%S")
            _log_queue.put_nowait((ts2, rpt, False))
        except Exception:
            # 队列满 → [LOG_DROP] 本身也丢，绝对不写 stderr
            pass


# ─────────────────────────────────────────────────────────
# macOS 原生：AppleScript 查 Music.app + Spotify 当前播放状态
#   优先级：先查 Music.app，没播放再查 Spotify；哪个在 playing 就用哪个；
#   都不 playing 就取首个有歌曲信息的，两者都空才返回空。
#   返回 dict: {song, artist, title, cover(当前仅 URL/空), duration_str, duration_sec,
#               progress_ms, playing, hasSong, source("Music"|"Spotify")}
# ─────────────────────────────────────────────────────────
_HAS_OSASCRIPT = None
def _ensure_osascript():
    global _HAS_OSASCRIPT
    if _HAS_OSASCRIPT is None:
        import shutil, subprocess
        _HAS_OSASCRIPT = bool(shutil.which("osascript"))
        if not _HAS_OSASCRIPT:
            log("WARN: 未检测到 osascript 命令（macOS 系统自带），媒体信息不可用")
    return _HAS_OSASCRIPT


def _run_applescript(script: str):
    import subprocess
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2.5
        )
        if r.returncode != 0:
            return (False, (r.stderr or "").strip())
        return (True, (r.stdout or "").strip())
    except Exception as e:
        return (False, str(e))


# Music.app AppleScript 脚本：返回 "artist\\ntitle\\nalbum\\nduration_sec\\nposition_sec\\nplayer_state\nartwork_url"
_MUSIC_APP_AS = '''
tell application "Music"
    try
        if player state is playing or player state is paused then
            set t to current track
            set _art to ""
            try
                set _art to artist of t
            end try
            set _nam to ""
            try
                set _nam to name of t
            end try
            set _alb to ""
            try
                set _alb to album of t
            end try
            set _dur to 0
            try
                set _dur to duration of t as number
            end try
            set _pos to 0
            try
                set _pos to player position as number
            end try
            set _st to ""
            if player state is playing then
                set _st to "playing"
            else if player state is paused then
                set _st to "paused"
            else
                set _st to "stopped"
            end if
            set _aw to ""
            try
                set _aw to artworks of t
                -- 这里只做占位，直接导出 artwork 二进制太麻烦，后面统一走网络三平台封面
            end try
            return _art & linefeed & _nam & linefeed & _alb & linefeed & _dur & linefeed & _pos & linefeed & _st
        else
            return ""
        end if
    on error errMsg
        return ""
    end try
end tell
'''

# Spotify AppleScript 脚本：返回同格式
_SPOTIFY_APP_AS = '''
tell application "Spotify"
    try
        if player state is playing or player state is paused then
            set trk to current track
            set _art to artist of trk
            set _nam to name of trk
            set _alb to album of trk
            set _dur to (duration of trk) / 1000
            set _pos to player position
            set _st to ""
            if player state is playing then
                set _st to "playing"
            else if player state is paused then
                set _st to "paused"
            else
                set _st to "stopped"
            end if
            return _art & linefeed & _nam & linefeed & _alb & linefeed & _dur & linefeed & _pos & linefeed & _st
        else
            return ""
        end if
    on error errMsg
        return ""
    end try
end tell
'''


def _parse_as_result(raw: str):
    if not raw:
        return None
    parts = raw.split("\n")
    if len(parts) < 6:
        return None
    artist = parts[0] or ""
    title = parts[1] or ""
    if not title:
        return None
    try:
        dur = float(parts[3]) if parts[3] else 0.0
    except Exception:
        dur = 0.0
    try:
        pos = float(parts[4]) if parts[4] else 0.0
    except Exception:
        pos = 0.0
    state = (parts[5] or "").lower()
    playing = state == "playing"
    return dict(artist=artist.strip(), title=title.strip(),
                album=(parts[2] or "").strip(),
                duration_sec=int(max(0, dur)),
                progress_ms=int(max(0, pos) * 1000),
                playing=playing)


def _query_music_app():
    ok, out = _run_applescript(_MUSIC_APP_AS)
    if not ok or not out:
        return None
    r = _parse_as_result(out)
    if r:
        r["source"] = "Music"
    return r


def _query_spotify():
    ok, out = _run_applescript(_SPOTIFY_APP_AS)
    if not ok or not out:
        return None
    r = _parse_as_result(out)
    if r:
        r["source"] = "Spotify"
    return r


# ─────────────────────────────────────────────────────────
# macOS 系统信息（轻量版：CPU / 内存 / 磁盘 / 电池 / 网络 / boot_time）
#   不依赖 psutil，优先用 AppleScript + sysctl / ioreg / top
# ─────────────────────────────────────────────────────────
def _sysctl(name: str):
    import subprocess
    try:
        r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            v = (r.stdout or "").strip()
            if v:
                try:
                    if "." in v: return float(v)
                    return int(v)
                except Exception:
                    return v
    except Exception:
        pass
    return None


def _get_battery_mac():
    """用 pmset -g batt 读电池（macOS 自带 pmset）。无电池则返回空 dict"""
    import subprocess
    try:
        r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=1)
        if r.returncode != 0:
            return {}
        out = r.stdout or ""
        if "Battery" not in out or "AC" not in out:
            return {}
        # 形如: " -InternalBattery-0 (id=...)\n 98%; charging; 2:05 remaining present: true\n"
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        info_line = ""
        for l in lines:
            if "%" in l and ";" in l:
                info_line = l; break
        if not info_line:
            return {}
        parts = [p.strip() for p in info_line.split(";")]
        pct = 0
        for p in parts:
            if p.endswith("%"):
                try: pct = int(p.rstrip("%"))
                except: pass
        charging = False
        remain = None
        if "charging" in info_line: charging = True
        # 剩余时间: "2:05 remaining" 或 "(no estimate)" 或 "charged"
        import re
        m = re.search(r'([0-9]+):([0-9]{2})\s+remaining', info_line)
        if m:
            remain = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        ret = {"battery_percent": pct, "battery_charging": charging}
        if remain is not None:
            ret["battery_remaining_sec"] = remain
        return ret
    except Exception:
        return {}


def _get_system_info_mac():
    """Mac 版系统信息（纯原生工具，无 pywin32/psutil/wmi 依赖）"""
    info = {"os": "macOS"}
    try:
        info["hostname"] = socket.gethostname()
    except Exception:
        pass
    # CPU: sysctl
    cpu_pct = _sysctl("vm.loadavg")  # 这个一般是 load avg，不是 percent，留空先
    cpu_count = _sysctl("hw.ncpu")
    cpu_freq_mhz = _sysctl("hw.cpufrequency")
    if cpu_freq_mhz:
        try: cpu_freq_mhz = int(cpu_freq_mhz) // 1_000_000
        except: cpu_freq_mhz = None
    if cpu_count is not None: info["cpu_count"] = cpu_count
    if cpu_freq_mhz: info["cpu_freq_mhz"] = cpu_freq_mhz
    # 尝试 top -l 1 拿 cpu percent（可能慢，所以只试一次）
    try:
        import subprocess, re
        r = subprocess.run(["top", "-l", "1", "-n", "0"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            m = re.search(r'CPU usage:\s*([0-9.]+)%\s*user', r.stdout or "")
            if m:
                info["cpu_user"] = float(m.group(1))
            m2 = re.search(r'CPU usage:\s*[0-9.]+%\s*user,\s*([0-9.]+)%\s*sys', r.stdout or "")
            if m2:
                info["cpu_sys"] = float(m2.group(1))
                info["cpu_percent"] = round(info.get("cpu_user", 0.0) + info["cpu_sys"], 1)
            m3 = re.search(r'PhysMem:\s*([0-9]+)M\s+used', r.stdout or "")
            if m3:
                info["mem_used_mb"] = int(m3.group(1))
    except Exception:
        pass
    # 内存: sysctl hw.memsize
    mem_total = _sysctl("hw.memsize")
    if mem_total:
        try: info["mem_total_gb"] = round(int(mem_total) / (1024**3), 1)
        except: pass
    # 磁盘：df -h /
    try:
        import subprocess, re
        r = subprocess.run(["df", "-k", "/"], capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            lines = (r.stdout or "").splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    try:
                        total = int(parts[1]) * 1024
                        used = int(parts[2]) * 1024
                        info["disk_root"] = {"total": total, "used": used, "use_pct": parts[4]}
                    except Exception:
                        pass
    except Exception:
        pass
    # 网络: netstat -ib / route get default (太杂，先留空)
    # 电池: pmset
    bat = _get_battery_mac()
    if bat: info.update(bat)
    # uptime: sysctl kern.boottime 是 struct timeval，用 BOOTTIME 秒级
    bt = _sysctl("kern.boottime")
    if bt:
        try:
            # kern.boottime 返回类似 "{ sec = 1720000000, usec = 0 }"，提取 sec 整数
            import re
            m = re.search(r'sec\s*=\s*(\d+)', str(bt))
            if m:
                info["boot_time"] = int(m.group(1))
                info["uptime"] = int(time.time() - int(m.group(1)))
        except Exception:
            pass
    # 进程数: ps aux | wc -l
    try:
        import subprocess
        r = subprocess.run(["/bin/sh", "-c", "ps aux | wc -l"], capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            v = (r.stdout or "").strip()
            if v.isdigit():
                info["proc_count"] = max(0, int(v) - 1)
    except Exception:
        pass
    return info


# ─────────────────────────────────────────────────────────
# macOS 当前前台窗口（AppleScript，不依赖 pywin32/psutil）
# ─────────────────────────────────────────────────────────
_FRONT_APP_AS = '''
tell application "System Events"
    try
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
        set windowName to ""
        try
            tell frontApp
                if (count of windows) > 0 then
                    set windowName to name of front window
                end if
            end tell
        end try
        return appName & linefeed & windowName
    on error
        return ""
    end try
end tell
'''

def get_front_window():
    """返回 (app_name, window_title)，都可能为空串"""
    if not _ensure_osascript():
        return ("", "")
    ok, out = _run_applescript(_FRONT_APP_AS)
    if not ok or not out:
        return ("", "")
    parts = out.split("\n", 1)
    return (parts[0] or "", (parts[1] if len(parts) > 1 else "") or "")


# ─────────────────────────────────────────────────────────
# 音乐播放器识别：从 AppleScript 查询成功的 source 直接标名（Music / Spotify）
# ─────────────────────────────────────────────────────────
_last_player = ""
_last_player_ts = 0
def detect_music_player(source_from_media: str = ""):
    global _last_player, _last_player_ts
    now = time.time()
    if source_from_media:
        _last_player = source_from_media
        _last_player_ts = now
        return source_from_media
    if now - _last_player_ts < 5 and _last_player:
        return _last_player
    return _last_player


# ─────────────────────────────────────────────────────────
#  媒体状态缓存（对应 PC 版 _SMTC_STATE，改名避免 Windows 痕迹）
# ─────────────────────────────────────────────────────────

_MEDIA_STATE = {
    "song": "",
    "artist": "",
    "title": "",
    "cover": "",            # URL / data:image/jpeg;base64,...
    "duration_str": "",     # "m:ss"
    "duration_sec": 0,      # 歌曲时长秒（LRCLIB 精确命中用）
    "progress_ms": 0,
    "playing": False,
    "hasSong": False,
    "lyric_line": "",
    "lyric_event": "",
    "timeline": [],
    "trans_timeline": [],
    "source": "",           # "Music" | "Spotify"
}
_state_lock = threading.RLock()  # v1.19：Lock→RLock（可重入），允许同一线程嵌套加锁（_stage_lyric_event被with _state_lock:内的代码调用）
_last_media_ts = 0.0        # 最后一次媒体采样时间（本地推算进度用）
_last_song_key = ""         # "artist - title" 切歌检测
_last_lyric_raw = ""
_last_trans_raw = ""
_last_lyric_idx = -1
_last_trans_idx = -1
_last_emit_wall_ts_ms = 0.0  # v1.07 分析：上一句歌词 emit 的墙上时间戳(ms)，0=切歌/暂停后首句
_lyric_pending = False        # v1.19：仅作"槽位非空"快速检查位，真正的事件槽是_MEDIA_STATE["lyric_event"]，溢出进_lyric_event_queue
_lyric_event_queue: collections.deque = collections.deque(maxlen=64)  # v1.19：<80ms内连续写≥2句时的缓冲队列（最多64句），主循环消费完槽自动从队首补
_LYRIC_OFFSET_MS = 0

def _stage_lyric_event(line: str, event: str):
    """v1.19 P0 统一的歌词事件写入入口（7处写入全走这里，同步PC v6.79）：
    - 若 pending 槽空闲 → 直接写入 _MEDIA_STATE + 置 pending=True（99%场景，0额外开销）
    - 若 pending 槽被占（上一句还没被主循环80ms轮读取走） → 入队列缓冲，保证零丢失
    - 切歌时外部调用方会清空队列，防止旧歌串到新歌
    """
    global _lyric_pending, _lyric_event_queue
    if not _lyric_pending:
        with _state_lock:
            _MEDIA_STATE["lyric_line"] = line
            _MEDIA_STATE["lyric_event"] = event
        _lyric_pending = True
        return
    try:
        _lyric_event_queue.append((line, event))
        if len(_lyric_event_queue) >= 60:
            log(f"[LYRIC_PROFILE] LYRIC_EVENT_QUEUE_NEAR_FULL len={len(_lyric_event_queue)}/64 pending=True → 可能主循环卡住或突发大量补发")
    except Exception as _e:
        try:
            log(f"[LYRIC_PROFILE] LYRIC_EVENT_QUEUE_APPEND_FAIL msg={_e!r} → 丢弃: {line[:20]!r}")
        except Exception:
            pass
LYRIC_TICK_MS = 80
LYRIC_SONG_INIT_MS = 300
_last_song_change_ts = 0.0
_media_song_intro_emitted_at = 0.0  # v1.04: 切歌提示在媒体检测立刻打印的时间戳（tick_lyric 防重复）
_last_playing_state = None
_lyrics_fetched_for = ""
_cover_fetched_for = ""

# ══ v1.14 P1-2 进度平滑滤波（同 PC v6.74）══
_PROGRESS_WINDOW_MAX = 5
_progress_window = []
_progress_window_lock = threading.Lock()
_progress_last_pred_ms = None

# ── 精确定时器提前调度 ──
_next_lyric_timer = None
_next_lyric_timer_song = ""
_next_lyric_timer_lock = threading.Lock()

# ── 下载后补位线程 ──
_catchup_thread = None
_catchup_thread_song = ""
_catchup_lock = threading.Lock()
_CATCHUP_INTERVAL_MS = 250
_CATCHUP_MIN_INTERVAL_MS = 60


def _cancel_catchup(reason_song_key: str = ""):
    """与 PC v6.6 同逻辑：带 #tag 一定 cancel；同首歌无 tag 误 cancel 忽略。"""
    global _catchup_thread, _catchup_thread_song
    with _catchup_lock:
        running = _catchup_thread_song
        is_tagged = ("#" in reason_song_key) if reason_song_key else False
        same_song_no_tag = (reason_song_key and running and reason_song_key == running and not is_tagged)
        if same_song_no_tag:
            return
        _catchup_thread_song = reason_song_key
        _catchup_thread = None
        t = _catchup_thread
    if t and t.is_alive() and threading.current_thread() is not t:
        try: t.join(timeout=1.0)
        except Exception: pass


def _catchup_lyrics_until(song_key: str, target_idx: int):
    """与 PC v6.6 完全一致：下载后补位 0..target_idx，v6.6 finally 3 层清理 + tick 兜底。"""
    global _catchup_thread, _catchup_thread_song
    if target_idx is None or target_idx < 0:
        return
    with _state_lock:
        if _MEDIA_STATE.get("song") != song_key:
            return
        timeline = list(_MEDIA_STATE.get("timeline", []) or [])
        trans_timeline = list(_MEDIA_STATE.get("trans_timeline", []) or [])
    if not timeline:
        return
    n = min(target_idx + 1, len(timeline))
    if n <= 0:
        return
    if n <= 10:
        interval_ms = _CATCHUP_INTERVAL_MS
    elif n <= 20:
        interval_ms = 150
    else:
        interval_ms = _CATCHUP_MIN_INTERVAL_MS
    total_expected_ms = max(0, n - 1) * interval_ms

    _cancel_all_lyric_timers(song_key + "#catchup_begin")
    with _state_lock:
        _last_lyric_idx = -1
        _last_trans_idx = -1
        global _last_emit_wall_ts_ms
        _last_emit_wall_ts_ms = 0.0

    log(f"歌词补位启动: {song_key} 需要补发 0..{n-1} 共 {n} 句 间隔={interval_ms}ms 预计用时≈{total_expected_ms}ms")

    def _run():
        global _catchup_thread, _catchup_thread_song
        global _last_lyric_idx, _last_trans_idx, _last_lyric_raw, _last_trans_raw, _lyric_pending, _last_emit_wall_ts_ms
        try:
            last_log_sent_ts = 0.0
            for i in range(n):
                i_ts0 = time.time()
                # ══ v1.09（同 PC v6.69）：单句 try/except —— 防止 LRCLIB 某行格式异常导致整段补位 thread 静默死
                try:
                    with _state_lock:
                        cur_song = _MEDIA_STATE.get("song")
                        playing = _MEDIA_STATE.get("playing", True)
                    if cur_song != song_key or _catchup_thread_song != song_key:
                        log(f"歌词补位中断: song已变化({cur_song!r}!={song_key!r}) 或被 cancel，已发 {i}/{n} 句")
                        return
                    if not playing:
                        log(f"歌词补位中断: 检测到暂停，已发 {i}/{n} 句")
                        return
                    try:
                        t_sec, txt = timeline[i]
                    except Exception as _ue:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_UNPACK_FAIL i={i}/{n} timeline[i]={timeline[i]!r} msg={_ue!r} → 跳过此句")
                        continue
                    try:
                        t_sec_f = float(t_sec)
                    except Exception as _fe:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_TSEC_BAD i={i}/{n} t_sec(raw)={t_sec!r} msg={_fe!r} → 跳过此句")
                        continue
                    trans_txt = ""
                    if trans_timeline:
                        best_dt = 0.6
                        for tt, ttxt in trans_timeline:
                            try:
                                dt = abs(float(tt) - t_sec_f)
                            except Exception:
                                continue
                            if dt < best_dt and ttxt:
                                best_dt = dt
                                trans_txt = ttxt
                            if best_dt == 0:
                                break
                    formatted = _format_lyric_line(txt, trans_txt)
                    # 发送：写 _MEDIA_STATE + pending
                    ts = time.time()
                    with _state_lock:
                        if _MEDIA_STATE.get("song") != song_key or _catchup_thread_song != song_key:
                            return
                        _last_lyric_idx = i
                        _last_trans_idx = i
                        _last_lyric_raw = txt
                        _last_trans_raw = trans_txt
                        # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
                        _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                    # ══ v1.18（同步 PC v6.78）：gap 分析并入歌词行末尾，不再独立 2 行
                    total_sent = i + 1
                    cur_wall_ms = int(ts * 1000)
                    N = i + 1
                    real_gap_ms = 0
                    lrc_gap_ms = 0
                    if _last_emit_wall_ts_ms == 0.0:
                        gap_line = f"{N} (FIRST)"
                    else:
                        real_gap_ms = cur_wall_ms - int(_last_emit_wall_ts_ms)
                        try:
                            lrc_gap_ms = int((timeline[i][0] - timeline[i - 1][0]) * 1000) if i > 0 else 0
                        except Exception:
                            lrc_gap_ms = 0
                        gap_line = f"[{real_gap_ms}ms] {N}  | LRC_gap={lrc_gap_ms}ms"
                    _last_emit_wall_ts_ms = float(cur_wall_ms)
                    # v1.18：取消 1 秒节流，每句都打印（原 gap_line 本来每句都打，总行数≈不增反减）
                    log(f"歌词补位 [{ts:.3f}] {total_sent}/{n}: {txt}" + (f" | 翻译: {trans_txt}" if trans_txt else "") + f"  | {gap_line}")
                    last_log_sent_ts = time.time()
                    # ══ v1.09（同 PC v6.69）每 5 句打一行 CATCHUP_LOOP_PROGRESS，肉眼可见运行进度
                    if i > 0 and i % 5 == 0:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_PROGRESS i={i}/{n} song={song_key!r} elapsed_from_sentence_start={ts - i_ts0:.3f}s throttle_delta={ts - last_log_sent_ts:.3f}s")
                    # 最后一句不用再 sleep；v1.09 从整段 sleep → 20ms step 分段（切歌 cancel 响应更灵敏 20ms 内）
                    if i < n - 1:
                        slept = 0
                        step = 20
                        while slept < interval_ms:
                            time.sleep(step / 1000.0)
                            slept += step
                            with _state_lock:
                                cs = _MEDIA_STATE.get("song")
                                pl = _MEDIA_STATE.get("playing", True)
                            if cs != song_key or _catchup_thread_song != song_key or not pl:
                                return
                except Exception as _inner_ex:
                    import traceback
                    log(f"[LYRIC_PROFILE] CATCHUP_LOOP_SENTENCE_EXCEPTION song={song_key!r} i={i}/{n} msg={_inner_ex!r} traceback={traceback.format_exc()} → 跳过，继续 i+1")
                    continue
            # 补位全部发完 → 如果还在播放且有下一句，立刻挂 Timer 衔接（补位终点无缝切到 tick/Timer）
            log(f"歌词补位完成: {song_key} 共补发 {n} 句")
            try:
                with _state_lock:
                    if _MEDIA_STATE.get("song") == song_key and _MEDIA_STATE.get("playing", False):
                        tl_now = _MEDIA_STATE.get("timeline", []) or []
                        ttl_now = _MEDIA_STATE.get("trans_timeline", []) or []
                        next_i = n
                        if tl_now and next_i < len(tl_now):
                            cur_t = tl_now[n - 1][0] * 1000
                            next_t = tl_now[next_i][0] * 1000
                            wait = max(0, int(next_t - cur_t - LYRIC_TICK_MS * 0.5))
                            _schedule_next_lyric_at(song_key, tl_now, ttl_now, next_i, wait)
            except Exception as _ie:
                log(f"[LYRIC_PROFILE] CATCHUP_FINISH_SCHEDULE_NEXT_FAIL msg={_ie!r}")
        except Exception as _outer_ex:
            import traceback
            log(f"[LYRIC_PROFILE] CATCHUP_LOOP_OUTER_EXCEPTION song={song_key!r} msg={_outer_ex!r} traceback={traceback.format_exc()}")
        finally:
            # ══ v6.6 二次加固 + v1.09（同 PC v6.69）第 ④ 道防线 无锁强制清残留 + tick*3 兜底
            try:
                self_thread = threading.current_thread()
                with _catchup_lock:
                    if _catchup_thread is self_thread:  # ① 自匹配清理
                        _catchup_thread = None
                    if _catchup_thread_song == song_key:  # ② 强制清理同一首歌标记
                        _catchup_thread_song = ""
            except Exception as _e:
                # ③ 防炸：加锁失败时无锁清理
                try:
                    if _catchup_thread_song == song_key:
                        _catchup_thread_song = ""
                except Exception:
                    pass
            # ④ v1.09 第 4 道：无锁 global 再清一次（song#tag 前缀残留也一起清）
            try:
                if _catchup_thread_song == song_key or ("#" not in song_key and _catchup_thread_song.startswith(song_key + "#")):
                    _catchup_thread_song = ""
                    log(f"[LYRIC_PROFILE] CATCHUP_FINALLY_FORCE_CLEAR_MARK song={song_key!r} → 第④道防线清残留成功")
            except Exception as _e4:
                try:
                    _catchup_thread_song = ""
                except Exception:
                    pass
            # ══ 补位结束后立即 tick 兜底：v6.6 1次 → v1.09 3次（每次15ms间隔），避免一次没生效即断档
            for _k in range(3):
                try:
                    time.sleep(0.015)
                    tick_lyric()
                except Exception as _e2:
                    pass
            log(f"[LYRIC_PROFILE] CATCHUP_FINALLY_END song={song_key!r} n={n} mark={_catchup_thread_song!r} → 标记清理 + 3*tick 兜底完成")

    with _catchup_lock:
        _catchup_thread = threading.Thread(target=_run, daemon=True)
        _catchup_thread_song = song_key
        _catchup_thread.start()


def _schedule_next_lyric_at(song_key: str, timeline, trans_timeline, next_idx: int, wait_ms: float):
    """在下一句应触发时刻精确推进 next_idx：PC v6.66 严格模式同逻辑。"""
    global _next_lyric_timer, _next_lyric_timer_song
    import math as _math
    import traceback as _tb
    if next_idx < 0 or not timeline or next_idx >= len(timeline):
        return
    if wait_ms is None:
        return
    try:
        wait_f = float(wait_ms)
    except Exception:
        return
    if _math.isnan(wait_f):
        return
    # v7.01 修链不中断：wait_f 负值/<=15ms 不再静默return（依赖tick兜底会造成clamped/seek后卡多轮或丢句）
    # 统一最小 1ms 调度，保证Timer链条永不中断
    if wait_f < 0:
        wait_f = 1.0
    wait_f = min(wait_f, 30.0 * 60.0 * 1000.0)
    if wait_f < 1.0:
        wait_f = 1.0

    def _cb():
        try:
            global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_emit_wall_ts_ms
            global _catchup_thread_song, _catchup_thread
            with _state_lock:
                if _MEDIA_STATE.get("song") != song_key:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_SONG_CHANGED next_idx={next_idx} expected={song_key} actual={_MEDIA_STATE.get('song')}")
                    return
                try:
                    with _catchup_lock:
                        ct = _catchup_thread
                    if ct is not None and ct.is_alive() and _catchup_thread_song == song_key:
                        if _LYRIC_SYNC_LOG:
                            log(f"[LYRIC_SYNC] TIMER:DROP_CATCHUP_ALIVE next_idx={next_idx} song={song_key}")
                        return
                    if (ct is None or not ct.is_alive()) and _catchup_thread_song == song_key:
                        with _catchup_lock:
                            if _catchup_thread_song == song_key:
                                _catchup_thread_song = ""
                except Exception:
                    pass
                tl = _MEDIA_STATE.get("timeline", [])
                ttl = _MEDIA_STATE.get("trans_timeline", [])
                playing = _MEDIA_STATE.get("playing", False)
                if not tl or next_idx >= len(tl) or not playing:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_SKIP_COND next_idx={next_idx} has_tl={bool(tl)} tl_len={len(tl) if tl else 0} playing={playing}")
                    return
                last = _last_lyric_idx
                if next_idx <= last:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_NEXT_LE_LAST next_idx={next_idx} last={last}")
                    return
                # ══ v1.20 P0 根修（同步 PC v6.80）：TIMER 顺序放宽容忍 3 句（diff=2/3 快速补发）
                #   同PC v6.80 P0-2：旧逻辑 next_idx > last+1 → DROP，tick 慢时 last 追不上 TIMER → 卡一下
                #   修复：diff ∈ {2,3} → 快速补发 last+1..next_idx-1，然后 last=next_idx-1 复用原代码发精确版
                #         diff >= 4 → DROP（seek/切歌，交 tick 兜底）
                diff = next_idx - last
                if diff >= 4:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_DIFF_GE4 next_idx={next_idx} last={last} diff={diff} → 疑似 seek/切歌，交 tick 兜底")
                    return
                if diff >= 2:
                    fill_start = last + 1
                    fill_end = next_idx - 1
                    for fill_idx in range(fill_start, fill_end + 1):
                        try:
                            if fill_idx >= len(tl):
                                break
                            fill_txt = tl[fill_idx][1] if fill_idx < len(tl) else ""
                            best_fill_trans = ""
                            best_fill_dt = 0.6
                            if ttl:
                                target_t_fill = tl[fill_idx][0]
                                for tt, ttxt in ttl:
                                    dtt = abs(tt - target_t_fill)
                                    if dtt < best_fill_dt and ttxt:
                                        best_fill_dt = dtt
                                        best_fill_trans = ttxt
                                    if best_fill_dt == 0:
                                        break
                            _last_lyric_idx = fill_idx
                            _last_trans_idx = fill_idx
                            if fill_txt and fill_txt.strip():
                                _last_lyric_raw = fill_txt
                                if best_fill_trans:
                                    _last_trans_raw = best_fill_trans
                            fts = time.time()
                            fwall_ms = int(fts * 1000)
                            prev_last_emit = _last_emit_wall_ts_ms if _last_emit_wall_ts_ms else 0.0
                            freal_gap = int(float(fwall_ms) - prev_last_emit) if prev_last_emit else 0
                            N_fill = fill_idx + 1
                            try:
                                if fill_idx > 0:
                                    flrc_gap = int((tl[fill_idx][0] - tl[fill_idx-1][0]) * 1000)
                                else:
                                    flrc_gap = 0
                            except Exception:
                                flrc_gap = 0
                            if fill_idx == 0:
                                fgap_line = f"{N_fill} (FIRST)"
                            else:
                                fgap_line = f"[{freal_gap}ms] {N_fill}  | LRC_gap={flrc_gap}ms"
                            if fill_txt and fill_txt.strip():
                                f_formatted = _format_lyric_line(fill_txt, best_fill_trans)
                                _stage_lyric_event(f_formatted, f"{f_formatted}|{fts:.3f}")
                            if _LYRIC_SYNC_LOG:
                                log(f"[LYRIC_SYNC] TIMER:FILL+{fill_idx-last} idx={fill_idx}/{len(tl)} last_old={last} next_target={next_idx} → 补{fill_idx-last}句（原应DROP_NEXT_GT_LASTP1）")
                            if fill_txt and fill_txt.strip():
                                log(f"歌词(补位Timer) [{fts:.3f}] idx={fill_idx}/{len(tl)} offset={_LYRIC_OFFSET_MS}ms: {fill_txt}" + (f" | 翻译: {best_fill_trans}" if best_fill_trans else "") + f"  | {fgap_line}")
                            else:
                                log(f"歌词(补位Timer) [{fts:.3f}] idx={fill_idx}/{len(tl)} offset={_LYRIC_OFFSET_MS}ms: (空行)" + f"  | {fgap_line}")
                            _last_emit_wall_ts_ms = float(fwall_ms)
                        except Exception as _fill_e:
                            try:
                                if _LYRIC_SYNC_LOG:
                                    log(f"[LYRIC_SYNC] TIMER:FILL_EXC fill_idx={fill_idx} msg={_fill_e!r}")
                            except Exception:
                                pass
                            continue
                    last = _last_lyric_idx
                cur_txt = tl[next_idx][1] if next_idx < len(tl) else ""
                lrc_t_now = float(tl[next_idx][0]) * 1000.0 + float(_LYRIC_OFFSET_MS)
                wall_now = 0.0
                if playing and _last_media_ts > 0:
                    wall_now = (time.time() - _last_media_ts) * 1000.0
                now_eff_ms = float(_MEDIA_STATE["progress_ms"]) + wall_now + float(_LYRIC_OFFSET_MS)
                if now_eff_ms < 0.0:
                    now_eff_ms = 0.0
                drift_cb = now_eff_ms - lrc_t_now
                if not cur_txt or not cur_txt.strip():
                    _last_lyric_idx = next_idx
                    _last_trans_idx = next_idx
                else:
                    best_trans = ""
                    best_dt = 0.6
                    if ttl:
                        target_t = tl[next_idx][0]
                        for tt, ttxt in ttl:
                            dt = abs(tt - target_t)
                            if dt < best_dt and ttxt:
                                best_dt = dt
                                best_trans = ttxt
                            if best_dt == 0:
                                break
                    _last_lyric_idx = next_idx
                    _last_trans_idx = next_idx
                    _last_lyric_raw = cur_txt
                    _last_trans_raw = best_trans
                    formatted = _format_lyric_line(cur_txt, best_trans)
                    ts = time.time()
                    # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
                    _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                    cur_wall_ms = int(ts * 1000)
                    N = next_idx + 1
                    if last < 0:
                        lrc_gap_ms = 0
                    else:
                        lrc_gap_ms = int((tl[next_idx][0] - tl[last][0]) * 1000)
                    if _last_emit_wall_ts_ms == 0.0:
                        real_gap_ms = 0
                        gap_line = f"{N} (FIRST)"
                    else:
                        real_gap_ms = cur_wall_ms - int(_last_emit_wall_ts_ms)
                        gap_line = f"[{real_gap_ms}ms] {N}  | LRC_gap={lrc_gap_ms}ms"
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:EMIT drift_cb={drift_cb:.3f}ms eff={now_eff_ms:.3f}ms LRC_t={lrc_t_now:.3f}ms idx={next_idx}/{len(tl)} real_gap={real_gap_ms} LRC_gap={lrc_gap_ms}")
                    # v1.18（同步 PC v6.78）：gap_line 合并到歌词行末尾
                    log(f"歌词(定时) [{ts:.3f}] idx={next_idx}/{len(tl)} offset={_LYRIC_OFFSET_MS}ms: {cur_txt}" + (f" | 翻译: {best_trans}" if best_trans else "") + f"  | {gap_line}")
                    _last_emit_wall_ts_ms = float(cur_wall_ms)
            with _state_lock:
                cur_song = _MEDIA_STATE.get("song")
                tl2 = _MEDIA_STATE.get("timeline", [])
                ttl2 = _MEDIA_STATE.get("trans_timeline", [])
                playing2 = _MEDIA_STATE.get("playing", False)
            if cur_song == song_key and cur_song == _next_lyric_timer_song and playing2:
                if tl2 and next_idx + 1 < len(tl2):
                    next_t_ms_f = float(tl2[next_idx + 1][0]) * 1000.0 + float(_LYRIC_OFFSET_MS)
                    cur_t_ms_f = float(tl2[next_idx][0]) * 1000.0 + float(_LYRIC_OFFSET_MS)
                    next_wait_f = max(0.0, (next_t_ms_f - cur_t_ms_f) - _LYRIC_TIMER_PREMISS_MS)
                    old_next_t_ms = int(tl2[next_idx + 1][0] * 1000 + _LYRIC_OFFSET_MS)
                    old_cur_t_ms = int(tl2[next_idx][0] * 1000 + _LYRIC_OFFSET_MS)
                    old_next_wait = max(0, old_next_t_ms - old_cur_t_ms) - LYRIC_TICK_MS
                    # v7.01 修链不中断：0/负/短等待不再跳过（<=15ms不调度会导致整句漏发或末尾卡死）
                    if next_wait_f < 1.0:
                        next_wait_f = 1.0
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:CHAIN_SCHEDULE_NEXT idx={next_idx}->{next_idx+1} next_wait_f={next_wait_f:.3f}ms (Timer_s={next_wait_f/1000.0:.6f}s min=1ms强制) | old_int={old_next_wait}ms (diff_f-old={next_wait_f-float(old_next_wait):.3f}ms)")
                    with _next_lyric_timer_lock:
                        if _next_lyric_timer_song == song_key:
                            try:
                                t = threading.Timer(next_wait_f / 1000.0,
                                                    _schedule_next_lyric_at,
                                                    args=(song_key, tl2, ttl2, next_idx + 1, 0.0))
                                t.daemon = True
                                t.start()
                                _next_lyric_timer = t
                            except Exception:
                                pass
        except Exception as e:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] TIMER:EXCEPTION_TRACEBACK err={e}\n{_tb.format_exc()}")
            else:
                log(f"WARN: 歌词定时器回调异常: {e}")

    lrc_next_t_ms = float(timeline[next_idx][0]) * 1000.0 + float(_LYRIC_OFFSET_MS)
    if _LYRIC_SYNC_LOG:
        log(f"[LYRIC_SYNC] TIMER:SCHEDULE lrc_next_t_ms={lrc_next_t_ms:.3f}ms wait_f={wait_f:.3f}ms Timer_s={wait_f/1000.0:.6f}s idx={next_idx} song={song_key}")
    with _next_lyric_timer_lock:
        _next_lyric_timer_song = song_key
        try:
            t = threading.Timer(wait_f / 1000.0, _cb)
            t.daemon = True
            t.start()
            _next_lyric_timer = t
        except Exception:
            pass


def _cancel_all_lyric_timers(reason_song_key: str = ""):
    global _next_lyric_timer, _next_lyric_timer_song
    with _next_lyric_timer_lock:
        old = _next_lyric_timer
        _next_lyric_timer = None
        _next_lyric_timer_song = reason_song_key
    try:
        if old is not None:
            old.cancel()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────
# 本地持久化：歌词 offset / LRC 缓存（Mac 版路径：~/Library/Application Support/Huanmeng）
# ─────────────────────────────────────────────────────────
_LOCAL_CFG_DIR = _os.path.join(_os.path.expanduser("~"), "Library", "Application Support", "Huanmeng")
_LOCAL_CFG_PATH = _os.path.join(_LOCAL_CFG_DIR, "mac_status.json")
_LOCAL_CACHE_PATH = _os.path.join(_LOCAL_CFG_DIR, "mac_status.cache.json")
_CACHE_LRU_LIMIT = 200

_cache_lock = threading.Lock()
_CACHE = {
    "lyrics": {},
    "covers": {},
    "meta": {"version": 1, "limit": _CACHE_LRU_LIMIT},
}


def _cache_key(artist: str, title: str) -> str:
    return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"


def _cache_evict_if_needed():
    for bucket_name in ("lyrics", "covers"):
        bucket = _CACHE[bucket_name]
        if len(bucket) <= _CACHE_LRU_LIMIT:
            continue
        items = sorted(bucket.items(), key=lambda kv: kv[1].get("fetched_at", 0))
        drop_n = len(items) - int(_CACHE_LRU_LIMIT * 0.7)
        if drop_n <= 0:
            continue
        for k, _ in items[:drop_n]:
            bucket.pop(k, None)


def _load_cache():
    global _CACHE
    try:
        if _os.path.isfile(_LOCAL_CACHE_PATH):
            import json
            with open(_LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("lyrics"), dict):
                _CACHE["lyrics"] = raw["lyrics"]
                _CACHE["covers"] = raw.get("covers") or {}
                _CACHE["meta"] = raw.get("meta") or _CACHE["meta"]
                log(f"缓存加载: 歌词 {len(_CACHE['lyrics'])} 首 + 封面 {len(_CACHE['covers'])} 首 → {_LOCAL_CACHE_PATH}")
    except Exception as e:
        log(f"WARN: 缓存读取失败: {e}")


def _save_cache():
    try:
        _os.makedirs(_LOCAL_CFG_DIR, exist_ok=True)
        import json
        with _cache_lock:
            _cache_evict_if_needed()
            with open(_LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(_CACHE, f, ensure_ascii=False)
    except Exception as e:
        log(f"WARN: 缓存写入失败: {e}")


def _cache_get_lyric(artist: str, title: str):
    k = _cache_key(artist, title)
    rec = _CACHE["lyrics"].get(k)
    if not rec:
        return None
    tl = rec.get("timeline") or []
    trans = rec.get("trans_timeline") or []
    if isinstance(tl, list):
        return (rec.get("source", "Cache"), tl, trans if isinstance(trans, list) else [])
    return None


def _cache_put_lyric(artist: str, title: str, src: str, tl, trans_tl):
    if not (artist and title and tl):
        return
    rec = {
        "source": str(src),
        "timeline": list(tl) if tl else [],
        "trans_timeline": list(trans_tl) if trans_tl else [],
        "fetched_at": time.time(),
    }
    with _cache_lock:
        _CACHE["lyrics"][_cache_key(artist, title)] = rec
    threading.Thread(target=_save_cache, daemon=True).start()


def _cache_get_cover(artist: str, title: str):
    rec = _CACHE["covers"].get(_cache_key(artist, title))
    if not rec or not rec.get("url"):
        return None
    return (rec.get("source", "Cache"), rec["url"])


def _cache_put_cover(artist: str, title: str, src: str, url: str):
    if not (artist and title and url):
        return
    rec = {
        "source": str(src),
        "url": url,
        "fetched_at": time.time(),
    }
    with _cache_lock:
        _CACHE["covers"][_cache_key(artist, title)] = rec
    threading.Thread(target=_save_cache, daemon=True).start()


def _load_local_config():
    global _LYRIC_OFFSET_MS
    try:
        if _os.path.isfile(_LOCAL_CFG_PATH):
            import json
            with open(_LOCAL_CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg.get("lyric_offset_ms"), int):
                _LYRIC_OFFSET_MS = cfg["lyric_offset_ms"]
            log(f"本地配置加载: offset={_LYRIC_OFFSET_MS}ms → {_LOCAL_CFG_PATH}")
    except Exception as e:
        log(f"WARN: 本地配置读取失败: {e}")


def _save_local_config():
    try:
        _os.makedirs(_LOCAL_CFG_DIR, exist_ok=True)
        import json
        with open(_LOCAL_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump({"lyric_offset_ms": _LYRIC_OFFSET_MS}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"WARN: 本地配置写入失败: {e}")


def _apply_offset_cmd(cmd: str, arg: str = ""):
    """CMD:OFFSET_ADD/SET/RESET/GET，返回 (log_text, reply_line_or_None)"""
    global _LYRIC_OFFSET_MS
    cmd = cmd.upper()
    if cmd == "RESET":
        old = _LYRIC_OFFSET_MS
        _LYRIC_OFFSET_MS = 0
        _save_local_config()
        return (f"歌词偏移重置: {old}ms → 0ms", f"OFFSET_CURRENT:{_LYRIC_OFFSET_MS}\n")
    if cmd == "GET":
        return (f"歌词偏移查询: {_LYRIC_OFFSET_MS}ms", f"OFFSET_CURRENT:{_LYRIC_OFFSET_MS}\n")
    try:
        val = int(arg)
    except Exception:
        return (f"OFFSET 参数错误: {arg!r}", f"OFFSET_ERROR:INVALID_ARG {arg}\n")
    if cmd == "SET":
        old = _LYRIC_OFFSET_MS
        _LYRIC_OFFSET_MS = val
        _save_local_config()
        return (f"歌词偏移设置: {old}ms → {val}ms", f"OFFSET_CURRENT:{_LYRIC_OFFSET_MS}\n")
    if cmd == "ADD":
        old = _LYRIC_OFFSET_MS
        _LYRIC_OFFSET_MS += val
        _save_local_config()
        return (f"歌词偏移调整: {old}ms + {val}ms = {_LYRIC_OFFSET_MS}ms", f"OFFSET_CURRENT:{_LYRIC_OFFSET_MS}\n")
    return (f"未知 OFFSET 子命令: {cmd}", f"OFFSET_ERROR:UNKNOWN {cmd}\n")


# ─────────────────────────────────────────────────────────
#  媒体轮询（macOS AppleScript：Music.app / Spotify 双引擎）
# ─────────────────────────────────────────────────────────

def _format_duration(total_sec: int) -> str:
    if total_sec <= 0:
        return ""
    m = total_sec // 60
    s = total_sec % 60
    return f"{m}:{s:02d}"


def poll_media():
    """mac 版：AppleScript 查 Music.app / Spotify，结构写入 _MEDIA_STATE（加锁），
    并触发：切歌检测 → 歌词缓存 / 后台搜歌词 / 后台搜封面。
    逻辑等价于 PC v6.6 的 poll_smtc()，只是把 winrt SMTCManager 替换为 AppleScript。"""
    global _last_song_key, _last_media_ts, _lyrics_fetched_for, _cover_fetched_for
    if not _ensure_osascript():
        return False
    results = []
    for fn, src_name in ((_query_music_app, "Music"), (_query_spotify, "Spotify")):
        try:
            r = fn()
            if r:
                results.append((src_name, r))
        except Exception:
            continue
    # 优先级：优先 playing 的那个；否则首个有数据的；都空就 hasSong=False
    chosen_src = None
    chosen = None
    for s, r in results:
        if r.get("playing"):
            chosen_src, chosen = s, r
            break
    if chosen is None and results:
        chosen_src, chosen = results[0]
    if chosen is None:
        with _state_lock:
            _MEDIA_STATE["hasSong"] = False
            _MEDIA_STATE["playing"] = False
        return False

    try:
        artist = (chosen.get("artist") or "").strip()
        title = (chosen.get("title") or "").strip()
        if not title:
            with _state_lock:
                _MEDIA_STATE["hasSong"] = False
                _MEDIA_STATE["playing"] = False
            return False
        cur_key = f"{artist} - {title}" if artist else title
        playing = bool(chosen.get("playing"))
        pos_ms = int(chosen.get("progress_ms") or 0)
        total_sec = int(chosen.get("duration_sec") or 0)
        duration_str = _format_duration(total_sec)
        source = chosen_src or ""

        # ══ v1.14 P1-2 进度平滑滤波（同 PC v6.74）══
        # 1) 写入滑窗 + seek/切歌 重置检测
        wall_ts = time.time()
        seek_or_song_change = False
        # 先根据上一轮预测判断跳变（seek/切歌）
        if _progress_last_pred_ms is not None:
            if abs(pos_ms - _progress_last_pred_ms) > 3000:
                seek_or_song_change = True
        # 切歌判断：artist - title 不同就切（也给进度跳变超过 15s 的强校验）
        song_changed = False
        with _state_lock:
            jump_detected = (not _MEDIA_STATE.get("song")) or abs(pos_ms - _MEDIA_STATE.get("progress_ms", 0)) > 15000 or _MEDIA_STATE.get("duration_str") != duration_str
        if seek_or_song_change:
            jump_detected = True
        if cur_key != _last_song_key or jump_detected:
            if cur_key != _last_song_key:
                song_changed = True
                _last_song_key = cur_key
                _last_song_change_ts = time.time()
            # ══ v1.14 P1-2：切歌/跳变 → 重置滑窗
            with _progress_window_lock:
                _progress_window.clear()
                _progress_last_pred_ms = None
            # 切歌 / 跳变 → 写媒体状态 + 触发歌词/封面
            global _lyric_pending
            with _state_lock:
                _MEDIA_STATE["song"] = cur_key
                _MEDIA_STATE["artist"] = artist
                _MEDIA_STATE["title"] = title
                _MEDIA_STATE["cover"] = ""  # 后面缓存/三平台并行补
                _MEDIA_STATE["duration_str"] = duration_str
                _MEDIA_STATE["duration_sec"] = total_sec
                _MEDIA_STATE["timeline"] = []
                _MEDIA_STATE["trans_timeline"] = []
                _MEDIA_STATE["lyric_line"] = ""
                _MEDIA_STATE["lyric_event"] = ""
                _MEDIA_STATE["source"] = source
                # ══ v1.21 P0 根修（同步 PC v6.81）：切歌清空 lyric_event 槽后必须同步把 _lyric_pending 置 False
                #    错位状态：pending=True 但槽="" → 主循环消费条件 True and "" = False → 永不消费永不补槽
                _lyric_pending = False
            if song_changed:
                log(f"媒体切歌: {cur_key} (时长={duration_str}, 来源={source})")
            # ── v1.04 同 PC v6.64：切歌提示 + 启动歌词严格在媒体检测切歌这一瞬间完成（不再等 80ms tick）
            t_p0 = time.time()
            formatted_intro = f"**\u25b6 {cur_key}**"
            _media_song_intro_emitted_at = t_p0
            with _state_lock:
                if _MEDIA_STATE["song"] == cur_key:
                    # v1.19 P0 切歌关键：立即清空旧歌事件队列，防止旧歌缓冲事件串到新歌
                    global _lyric_event_queue
                    try:
                        _lyric_event_queue.clear()
                    except Exception:
                        pass
                    # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
                    _stage_lyric_event(formatted_intro, f"{formatted_intro}|{t_p0:.3f}")
            global _last_lyric_idx, _last_trans_idx, _last_lyric_raw, _last_trans_raw, _last_emit_wall_ts_ms
            _last_lyric_idx = -1
            _last_trans_idx = -1
            _last_lyric_raw = ""
            _last_trans_raw = ""
            _last_emit_wall_ts_ms = 0.0
            _cancel_all_lyric_timers(cur_key)
            _cancel_catchup(cur_key)
            tick_lyric._last_song_sent = cur_key
            # 歌词 & 封面：先缓存命中，再异步搜网
            if artist and title:
                _lyrics_fetched_for = cur_key
                _cover_fetched_for = cur_key
                # 歌词缓存
                cached_lyric = _cache_get_lyric(artist, title)
                if cached_lyric:
                    name, tl, trans = cached_lyric
                    # ══ v1.11 P0 修复：缓存命中读出来也必须走二次净化（老缓存可能有历史坏行）
                    tl_clean = _sanitize_timeline(tl, tag="cache_timeline")
                    trans_clean = _sanitize_timeline(trans or [], tag="cache_trans") if trans else []
                    with _state_lock:
                        if _MEDIA_STATE["song"] == cur_key:
                            _MEDIA_STATE["timeline"] = tl_clean
                            if trans_clean:
                                _MEDIA_STATE["trans_timeline"] = trans_clean
                    log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=缓存命中{len(tl_clean)}行 +翻译={len(trans_clean)}行")
                    log(f"歌词: 缓存命中 {name} ({len(tl_clean)} 行)" + (f" +翻译 {len(trans_clean)} 行" if trans_clean else ""))
                    _force_emit_current_lyric(cur_key)
                else:
                    log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=未命中 立刻并发搜索")
                    threading.Thread(target=_fetch_lyrics_bg, args=(artist, title, cur_key, (total_sec if total_sec else None), t_p0), daemon=True).start()
                # 封面缓存
                cached_cover = _cache_get_cover(artist, title)
                if cached_cover:
                    csrc, curl = cached_cover
                    with _state_lock:
                        if _MEDIA_STATE["song"] == cur_key and not _MEDIA_STATE.get("cover"):
                            _MEDIA_STATE["cover"] = curl
                    log(f"封面: 缓存命中 {csrc} → {curl[:70]}...")
                else:
                    threading.Thread(target=_fetch_cover_bg, args=(artist, title, cur_key), daemon=True).start()
        else:
            # 没切歌，兜底写 duration_str 如果之前空
            if duration_str:
                with _state_lock:
                    if _MEDIA_STATE["song"] and not _MEDIA_STATE["duration_str"]:
                        _MEDIA_STATE["duration_str"] = duration_str
            if source:
                with _state_lock:
                    if not _MEDIA_STATE.get("source"):
                        _MEDIA_STATE["source"] = source

        # ══ v1.16 P1-2 同步 PC v6.76：最小二乘拟合 + rate clamp + pred clamp（原首尾两点拟合波动大且量纲混乱）══
        #   x=wall秒, y=pos_ms → rate 量纲 = ms/秒，正常≈1000；pred = last_pos + Δs × rate_ms_per_sec
        #   rate clamp [900,1100] + pred clamp |pred-pos_raw|>5000ms → 免拟合异常拉飞进度
        # ══ v1.20 P0 根修（同步 PC v6.80）：相同y值不入窗 + seek跳变重置窗口
        #   同PC v6.80 P0-1：Music/Spotify AppleScript 查询 progress 有时 1s 才刷新一轮，主循环多次相同y
        #   → 滑窗相同y污染拟合样本 → rate 低 clamp 到 900 → eff_ms 周期性卡慢循环
        #   修复：playing 且 pos_ms == 滑窗最后一条 y 且（墙上距离 < 2s）→ 不入窗
        eff_pos_ms = pos_ms
        try:
            # v1.20 + seek/跳变检测（同步PC版）：last_pred 与当前 pos_ms 差 > 3000ms → 重置窗口
            if _progress_last_pred_ms is not None:
                if abs(pos_ms - _progress_last_pred_ms) > 3000:
                    with _progress_window_lock:
                        _progress_window.clear()
                    _progress_last_pred_ms = None
            now_for_window = wall_ts
            with _progress_window_lock:
                # v1.20 P0：相同y不入窗（playing 且 <2s 间隔）
                skip_append = False
                if playing and _progress_window:
                    lw, lp = _progress_window[-1]
                    if lp == pos_ms and (now_for_window - lw) < 2.0:
                        skip_append = True
                if not skip_append:
                    _progress_window.append((now_for_window, pos_ms))
                    if len(_progress_window) > _PROGRESS_WINDOW_MAX:
                        _progress_window.pop(0)
                win = list(_progress_window)
            if len(win) >= 3 and playing:
                n = len(win)
                sx = sy = sxx = sxy = 0.0
                x0 = win[0][0]
                for wx, wy in win:
                    x = wx - x0
                    y = float(wy)
                    sx += x; sy += y; sxx += x*x; sxy += x*y
                denom = n * sxx - sx * sx
                if denom != 0:
                    rate_ms_per_sec = (n * sxy - sx * sy) / denom
                else:
                    rate_ms_per_sec = 1000.0
                rate_ms_per_sec = min(1100.0, max(900.0, rate_ms_per_sec))
                last_wall, last_pos = win[-1]
                cur_wall_now = time.time()
                pred_pos_ms = int(last_pos + (cur_wall_now - last_wall) * rate_ms_per_sec)
                if pred_pos_ms < 0:
                    pred_pos_ms = 0
                if abs(pred_pos_ms - pos_ms) > 5000:
                    pred_pos_ms = pos_ms
                _progress_last_pred_ms = pred_pos_ms
                fused = int(0.6 * pred_pos_ms + 0.4 * pos_ms)
                if fused >= 0 and abs(fused - pos_ms) < 10000:
                    eff_pos_ms = fused
            else:
                # 窗口不足或暂停：直接用实测，记录 last_pred 供跳变检测
                _progress_last_pred_ms = pos_ms
        except Exception as _p12_exc:
            try:
                log(f"[LYRIC_PROFILE] P1-2_PROGRESS_FILTER_EXC msg={_p12_exc!r}")
                log(traceback.format_exc())
            except Exception:
                pass
            eff_pos_ms = pos_ms
            try:
                _progress_last_pred_ms = pos_ms
            except Exception:
                pass

        # 总是更新：进度 / 播放状态 / 时间戳
        with _state_lock:
            _MEDIA_STATE["progress_ms"] = eff_pos_ms
            _MEDIA_STATE["playing"] = playing
            _MEDIA_STATE["hasSong"] = True
        _last_media_ts = time.time()
        return True
    except Exception as e:
        log(f"媒体轮询异常: {e}")
        return False


# ─────────────────────────────────────────────────────────
#  LRC 解析 + 各平台歌词拉取（与 PC v6.6 逻辑完全相同，原封不动搬）
# ─────────────────────────────────────────────────────────

def _parse_lrc(text):
    """更宽容的 LRC 解析：
    - 支持一行多时间戳 [00:01.00][00:05.00]副歌
    - 支持 [mm:ss]、[mm:ss.xx]、[mm:ss.xxx]、[mm:ss,xxx]
    - 空文本行也保留（有些 LRC 用空行表示间奏，不解析但也不丢行）
    - ══ v1.11 P0 修复（同 PC v6.71）：最终输出前**逐行强校验**，确保每条都是 (可float时间, 任意文本) 二元组
      防止 LRCLIB/第三方源吐畸形行 → 后续 tick/补位 裸解包抛 ValueError → traceback 刷爆 stderr → WriteFile C 层阻塞 → 死卡+^C杀不掉"""
    import re
    if not text:
        return []
    r = []
    tag_re = re.compile(r'\[(\d{1,2}):(\d{1,2})(?:[.:,](\d{1,3}))?\]')
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        tags = list(tag_re.finditer(raw_line))
        if not tags:
            continue
        last_end = tags[-1].end()
        txt = raw_line[last_end:].strip()
        for m in tags:
            mm, ss, ms = m.group(1), m.group(2), m.group(3) or "0"
            try:
                mm_i = int(mm)
                ss_i = int(ss)
                ms_i = int(ms.ljust(3, '0')[:3])
                t = float(mm_i * 60 + ss_i + ms_i / 1000.0)
                if t < 0 or t != t:  # NaN / 负值 直接丢
                    log(f"[LYRIC_PROFILE] LRC_PARSE_BAD_LINE mm={mm!r} ss={ss!r} ms={ms!r} t={t!r} → 丢弃")
                    continue
                r.append((t, txt))
            except Exception as _e:
                log(f"[LYRIC_PROFILE] LRC_PARSE_BAD_LINE mm={mm!r} ss={ss!r} ms={ms!r} msg={_e!r} → 丢弃此时间戳")
                continue
    if not r:
        return []
    r.sort(key=lambda x: x[0])
    out = []
    last_t = None
    last_txt = None
    for row in r:
        try:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                log(f"[LYRIC_PROFILE] LRC_PARSE_BAD_ROW row={row!r} → 丢弃（非二元组）")
                continue
            t, txt = row
            t = float(t)
            if t != t or t < 0:
                log(f"[LYRIC_PROFILE] LRC_PARSE_BAD_ROW t(NaN/negative)={t!r} row={row!r} → 丢弃")
                continue
        except Exception as _e2:
            log(f"[LYRIC_PROFILE] LRC_PARSE_BAD_ROW row={row!r} msg={_e2!r} → 丢弃")
            continue
        if last_t is not None and abs(t - last_t) < 0.001 and txt == last_txt:
            continue
        out.append((t, txt))
        last_t = t
        last_txt = txt
    return out


def _sanitize_timeline(tl, *, tag="timeline"):
    """
    ══ v1.11 P0 修复（同 PC v6.71）：任何来源写入 _MEDIA_STATE["timeline"/"trans_timeline"] 之前，统一走**二次强校验+净化**。
       保证：返回列表中的每一行都是 (float_t >= 0 且非NaN, str_text_or_None_ok) 二元组。
       后续 _current_lyric_idx / tick / 补位循环 就不会遇到"非二元组解包 ValueError → traceback 刷爆 stderr → 死卡"。
    """
    if tl is None:
        return []
    try:
        rows = list(tl)
    except Exception as _e:
        log(f"[LYRIC_PROFILE] SANITIZE_{tag.upper()}_NOT_ITERABLE msg={_e!r} tl_type={type(tl).__name__} → 重置为空")
        return []
    clean = []
    for idx, row in enumerate(rows):
        try:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                log(f"[LYRIC_PROFILE] SANITIZE_{tag.upper()}_BAD_ROW idx={idx} row={row!r} → 丢弃（非二元组）")
                continue
            t, txt = row
            try:
                t_f = float(t)
            except Exception as _fe:
                log(f"[LYRIC_PROFILE] SANITIZE_{tag.upper()}_BAD_T idx={idx} t(raw)={t!r} msg={_fe!r} → 丢弃")
                continue
            if t_f != t_f or t_f < 0:
                log(f"[LYRIC_PROFILE] SANITIZE_{tag.upper()}_BAD_T idx={idx} t(NaN/negative)={t_f!r} → 丢弃")
                continue
            clean.append((t_f, "" if txt is None else txt))
        except Exception as _outer_row:
            import traceback
            log(f"[LYRIC_PROFILE] SANITIZE_{tag.upper()}_ROW_CRASH idx={idx} msg={_outer_row!r} traceback={traceback.format_exc()} → 丢弃")
            continue
    try:
        clean.sort(key=lambda x: x[0])
    except Exception:
        pass
    return clean


def _current_lyric_idx(timeline, ms):
    """
    返回 (索引, 文本)；找不到返回 (-1, "")。索引用于区分文本相同但时间轴位置不同的重复句。
    ══ v1.11 P0 修复（同 PC v6.71）：逐行解包也 try/except，坏行 continue 不崩。
       哪怕 sanitize 有漏网之鱼也不会 tick 每 80ms 炸一次刷 stderr。
    """
    if not timeline or ms < 0:
        return (-1, "")
    try:
        sec = float(ms) / 1000.0
    except Exception:
        return (-1, "")
    idx = -1
    cur = ""
    for i, row in enumerate(timeline):
        try:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            t, txt = row
            try:
                t_f = float(t)
            except Exception:
                continue
            if t_f <= sec:
                idx = i
                cur = txt if txt else ""
            else:
                break
        except Exception:
            continue
    return (idx, cur if cur else "")


def _current_lyric(timeline, ms):
    return _current_lyric_idx(timeline, ms)[1]


_COMMON_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_DEFAULT_HEADERS = {"User-Agent": _COMMON_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

_HTTP_MAX_RETRIES = 2        # v1.17 去熔断提速：单请求最多自动重试 2 次（总计 3 次尝试），避免 5 次指数退避叠加总耗时 30+s 直接越过 T5=18s 总超时
_HTTP_RETRY_BACKOFF = 1.0    # v1.17 常量退避：每次重试等 1.0s（不再 0.25/0.5/1/2/4 指数递增空耗 7.75s），切歌/抖动响应更灵敏
_LYRIC_LRCLIB_RETRIES = 1  # ══ v1.05 再提速：1 次（同 PC v6.65：Spotify 时长常跟 LRCLIB 精确对不上，retries=2 纯等 4s；1 次 2s 不行立刻 fuzzy 命中）
_LYRIC_LRCLIB_TIMEOUT = 2  # ══ v1.03 提速：单次 2s（原 4s，海外节点抖就立刻放弃转中文源）
_LYRIC_POOL_MAX_WORKERS = 8  # 3 平台 × 2(繁简) × 2(LRCLIB 精确+模糊) = 最多 12；8 槽保证快源不被慢源卡住
_LYRIC_FIRST_RESULT_TMO_S1 = 6.0   # ══ v1.05 再提速：6s 上限（第一轮精确全 MISS 场景，9s 最后 3s 纯浪费；6s 到立刻进第二轮 fuzzy）
_LYRIC_FIRST_RESULT_TMO_S2 = 12.0  # ══ v1.03 提速：12s 上限（不再 18/31s 等死）
_LYRIC_CN_TIMEOUT = 2.5            # ══ v1.03 提速：网易云/QQ音乐 单请求 2.5s（原 3.5s，封 IP / TCP 半开 不耗时间）
_LYRIC_STRICT_SYNC = True
_LYRIC_MAX_DRIFT_MS = 250.0
_LYRIC_SYNC_LOG = False             # v1.15 默认 False：正常运行不刷 [LYRIC_SYNC]，排障时临时改回 True 开全量诊断
_LYRIC_TIMER_PREMISS_MS = 0.0
# ══ v1.14 P1-5 阶梯提交窗口（MIN_WAIT_S 内只提交第一阶段 3 条快源，窗口内保留最高分）
_MIN_WAIT_S = 1.5


# ═══════════════════════════════════════════════════════════════
# v1.14 P1-5 最优匹配评分函数（同 PC v6.74）
# ═══════════════════════════════════════════════════════════════
def _score_lyric_match(result, artist, title, duration_sec) -> float:
    """
    result = (name, tl, trans or None)；score 越高越好
    - 字符重合 0~0.4
    - 时长吻合 +0.3 / -0.3
    - 行数合理性 +0.2 / -0.3
    - 源偏好 LRCLIB+0.1 / 网易云+0.05 / QQ+0.05
    """
    try:
        score = 0.0
        if not isinstance(result, tuple) or len(result) < 2:
            return -100.0
        src_name = str(result[0]) if result[0] else ""
        tl = result[1] if len(result) > 1 else []
        # ── 1) 字符重合打分（artist+title 联合 set 重合率 0~0.4）──
        try:
            cur_title_set = set(_trad_to_simp(title or "").strip().lower())
            res_name_set = set(_trad_to_simp(src_name).strip().lower())
            # 再把 artist 也并入 set 做联合打分
            cur_artist_set = set(_trad_to_simp(artist or "").strip().lower())
            cur_union = cur_title_set | cur_artist_set
            if cur_union and res_name_set:
                inter = len(cur_union & res_name_set)
                union_n = len(cur_union | res_name_set)
                if union_n > 0:
                    jaccard = inter / union_n
                    score += min(0.4, 0.4 * jaccard)
            # 额外：title 直接含 src 的 name 或反之 → 补一点分（上限 0.4）
            st = _trad_to_simp(title or "").strip().lower()
            sn = _trad_to_simp(src_name).strip().lower()
            if st and sn and (st in sn or sn in st):
                score = max(score, 0.15)
        except Exception:
            pass

        # ── 2) 时长吻合 ──
        try:
            dur = float(duration_sec or 0)
            if isinstance(tl, list) and len(tl) > 0:
                last_row = tl[-1]
                if isinstance(last_row, (list, tuple)) and len(last_row) >= 1:
                    lrc_last_sec = float(last_row[0])
                    denom = max(1.0, dur)
                    diff_ratio = abs(lrc_last_sec - dur) / denom
                    if diff_ratio < 0.15:
                        score += 0.3
                    elif diff_ratio > 0.3:
                        score -= 0.3
        except Exception:
            pass

        # ── 3) 行数合理性 ──
        try:
            n = len(tl) if isinstance(tl, list) else 0
            if 10 <= n <= 200:
                score += 0.2
            elif n < 5 or n > 500:
                score -= 0.3
        except Exception:
            pass

        # ── 4) 源偏好 ──
        try:
            name_upper = src_name.upper()
            if "LRCLIB" in name_upper:
                score += 0.1
            if "网易云" in src_name:
                score += 0.05
            if "QQ" in name_upper:
                score += 0.05
        except Exception:
            pass

        # ── 5) v1.15 翻译优先（同步 PC v6.75）：有翻译行且有效行数≥3 且 ≥ 主歌词 50% → +0.5 超高优先
        try:
            if isinstance(result, (tuple, list)) and len(result) >= 3:
                trans = result[2]
                if not trans:
                    trans = []
                try:
                    trans_n = len(trans) if isinstance(trans, (tuple, list)) else 0
                except Exception:
                    trans_n = 0
                valid_rows = 0
                if trans_n > 0 and isinstance(trans, (tuple, list)):
                    check = trans if trans_n <= 60 else trans[:60]
                    for r in check:
                        try:
                            if isinstance(r, (tuple, list)) and len(r) >= 2:
                                _ = float(r[0])
                                if str(r[1] or "") != "":
                                    valid_rows += 1
                        except Exception:
                            continue
                if valid_rows >= 3 and trans_n > 0:
                    tl_n = len(tl) if isinstance(tl, (tuple, list)) else 0
                    ratio = valid_rows / max(1, tl_n) if tl_n > 0 else 0.0
                    if ratio >= 0.5:
                        score += 0.5
                    else:
                        score += 0.2
        except Exception:
            pass

        return float(score)
    except Exception as _sc_exc:
        try:
            log(f"[LYRIC_PROFILE] P1-5_SCORE_EXC msg={_sc_exc!r}")
            log(traceback.format_exc())
        except Exception:
            pass
        return -100.0


# ═══════════════════════════════════════════════════════════════
# v1.13 P0-2 全局常驻线程池（根治 with ThreadPoolExecutor shutdown(wait=True) 空等 running HTTP 任务结束）
#   - 程序启动创建 1 次，永不销毁（shutdown 永远不调用）
#   - 歌词搜索 R1/R2、封面搜索、缓存持久化、HTTP 硬超时包装 全部复用同一池
#   - 命中/超时后仅对 pending 任务 cancel()，**不等待 running 任务结束**，主线程/后台线程立刻继续
# ═══════════════════════════════════════════════════════════════
_LYRIC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_LYRIC_POOL_MAX_WORKERS,
    thread_name_prefix="lyric_worker"
)
# v1.13 P1-4 HTTP 全局 Session（TCP/TLS 连接复用，同源握手开销↓30%+）；懒加载首次使用时初始化
_HTTP_SESSION = None
_HTTP_SESSION_LOCK = threading.Lock()
# v1.17 用户要求：彻底移除熔断机制（原 P1-4 连续失败3次→5min跳过请求，偶发超时会被放大成15min搜不到）

def _get_http_session():
    """v1.13 P1-4：懒加载全局 requests.Session，复用连接；线程安全单例"""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is not None:
            return _HTTP_SESSION
        import requests as _req_mod
        _HTTP_SESSION = _req_mod.Session()
        # 默认 UA：模拟普通 Chrome on Mac，避免 requests 默认 UA 被部分 API 直接拒
        _HTTP_SESSION.headers.update({
            "User-Agent": _COMMON_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        return _HTTP_SESSION


# ═══════════════════════════════════════════════════════════════
# v1.13 P0-3 健康自检（每 5 分钟：活跃线程数 / 日志队列水位 超阈值告警）
# ═══════════════════════════════════════════════════════════════
_HEALTH_CHECK_INTERVAL_S = 300.0   # 5 min
_HEALTH_THREAD_WARN = 50           # 活跃线程数 > 50 告警
_last_health_check_ts = 0.0
_health_lock = threading.Lock()

def _run_health_check(force: bool = False):
    """v1.13 P0-3：定期自检线程/队列水位，超出阈值打 WARN。force=True 忽略节流立刻执行"""
    global _last_health_check_ts
    now = time.time()
    with _health_lock:
        if (not force) and (now - _last_health_check_ts < _HEALTH_CHECK_INTERVAL_S):
            return
        _last_health_check_ts = now
    try:
        # 1) 活跃线程数（使用 threading.enumerate，零依赖）
        try:
            threads = threading.enumerate()
            thread_count = len(threads)
            daemon_count = sum(1 for t in threads if getattr(t, "daemon", False))
            alive_count = sum(1 for t in threads if getattr(t, "is_alive", lambda: False)())
            if thread_count > _HEALTH_THREAD_WARN:
                log(f"[HEALTH_WARN] 活跃线程数={thread_count}(alive={alive_count},daemon={daemon_count}) 超过阈值 {_HEALTH_THREAD_WARN}")
                names = [getattr(t, "name", "?") for t in threads if not getattr(t, "daemon", False)]
                if names:
                    log(f"[HEALTH_WARN] 非daemon线程名样本(≤10): {names[:10]}")
        except Exception:
            thread_count = 0
            daemon_count = 0

        # 2) 日志队列水位（高水位时提示消费压力）
        try:
            qsize = int(_log_queue.qsize())
            pct = int(100 * qsize / _LOG_QUEUE_MAX) if _LOG_QUEUE_MAX else 0
            if pct >= 60:
                log(f"[HEALTH_WARN] 日志队列水位={qsize}/{_LOG_QUEUE_MAX} ({pct}%) — 终端消费慢，已自动激进丢诊断保核心")
        except Exception:
            pct = 0
            qsize = 0

        # 3) INFO 摘要
        if force or thread_count > _HEALTH_THREAD_WARN or pct >= 60:
            log(f"[HEALTH_INFO] 线程={thread_count}(daemon={daemon_count}) 日志队列={qsize}/{_LOG_QUEUE_MAX}({pct}%)")
    except Exception:
        # 自检本身异常也绝不能崩主流程，且不打 traceback stderr
        pass


def _http_get_json(url: str, *, referer: str = "", extra_headers: dict | None = None, timeout: int = 5,
                   verify: bool = True, retries: int = _HTTP_MAX_RETRIES, _debug_tag: str = ""):
    """v1.17 精简版（同步 PC v6.77）：全局Session复用 + requests timeout + retries次常量1.0s退避重试 + 响应健壮性校验 + 分级失败日志
    已移除：
      ① 熔断（用户强制要求，避免「短时抖动超时+熔断→15min内搜不到必然存在的歌」，如 Ice Paper - 心如止水）
      ② 线程级硬超时（ThreadPoolExecutor 的 Future.cancel 只能杀 pending，running HTTP 还会卡 worker，嵌套无收益）
    - 可重试错误：连接异常/超时/5xx/SSL异常/响应截断(JSON解析失败)
    - 不重试：4xx(除429限流)/明确的"找不到"/Content-Type是HTML且无JSON迹象(典型反爬)
    返回 dict | list（解析成功的 JSON），失败返回 None。"""
    import warnings, urllib3
    # ══ 全局关 Unverified HTTPS request（verify=False 时不刷屏）══
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    try:
        warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    try:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    except Exception:
        pass
    headers = dict(_DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    session = _get_http_session()  # v1.13 P1-4：全局Session单例
    # v1.17 已移除：线程级硬超时包装（Future.cancel无法杀running，嵌套无收益）

    def _ssl_reason(reason: str) -> bool:
        """SSL fallback 触发条件收窄：必须明确是 SSL/certificate 错误，普通 HTTP 404 不进入。"""
        rl = (reason or "").lower()
        return ("ssl" in rl) or ("certificate" in rl) or ("sslerror" in rl)

    def _one_attempt(do_ssl_fallback: bool):
        """单次 HTTP 请求尝试（requests软超时 + 业务逻辑解析）。返回 (json_ok_result, should_retry, reason)"""
        use_verify = verify and not do_ssl_fallback
        try:
            resp = session.get(url, headers=headers, timeout=timeout, verify=use_verify)
            code = resp.status_code
            if code == 200:
                pass
            elif code == 429 or (500 <= code <= 599):
                return None, True, f"HTTP {code}"
            elif 400 <= code <= 499:
                if _debug_tag:
                    log(f"WARN: {_debug_tag} HTTP {code} URL={url[:100]} (不重试)")
                return None, False, f"HTTP {code}"
            else:
                return None, True, f"HTTP {code}"
            ctype = resp.headers.get("Content-Type", "") or ""
            if "json" not in ctype.lower() and "javascript" not in ctype.lower():
                body_sample = (resp.text or "")[:80]
                if not body_sample.lstrip().startswith(("{", "[")):
                    if _debug_tag:
                        log(f"WARN: {_debug_tag} Content-Type={ctype!r} 非JSON 样例={body_sample!r}")
                    return None, True, f"Content-Type={ctype!r} 非JSON"
            try:
                j = resp.json()
                return j, False, ""
            except Exception as je:
                sample = (resp.text or "")[:80]
                if _debug_tag:
                    log(f"WARN: {_debug_tag} JSON 解析失败 {je} 样例={sample!r}")
                return None, True, f"JSONDecodeError: {type(je).__name__}"
        except Exception as e:
            ename = type(e).__name__
            is_ssl = ("SSL" in ename) or ("certificate" in str(e).lower()) or ("CERTIFICATE" in ename)
            if is_ssl and verify and not do_ssl_fallback:
                return None, True, f"SSL错误({ename})，准备降级verify=False重试"
            if is_ssl and do_ssl_fallback:
                return None, False, f"SSL_fallback也失败({ename})，放弃此请求"
            return None, True, f"{ename}: {e}"

    last_reason = ""
    real_retries_performed = 0
    final_ok = False
    for attempt in range(retries + 1):
        is_last_attempt = (attempt == retries)
        # ══ v1.17：直接调用 requests.timeout（urllib3 底层 socket select 真生效）
        try:
            result, should_retry, reason = _one_attempt(False)
        except Exception as _outer_e:
            import traceback as _tb
            log(f"[HTTP_HARD_EXC] tag={_debug_tag!r} attempt={attempt} msg={_outer_e!r}")
            try:
                log(_tb.format_exc())
            except Exception:
                pass
            result, should_retry, reason = None, True, f"OuterWrapper_exc: {_outer_e!r}"
        if result is not None:
            final_ok = True
            if attempt > 0 and _debug_tag:
                log(f"WARN: {_debug_tag} 第{attempt}次重试成功 ✅ (上次原因: {last_reason or reason})")
            break
        last_reason = reason
        if should_retry and verify and _ssl_reason(reason):
            try:
                r2, sr2, rsn2 = _one_attempt(do_ssl_fallback=True)
            except Exception as _sfe:
                r2, sr2, rsn2 = None, True, f"SSL_fallback_sync_exc: {_sfe!r}"
            if r2 is not None:
                final_ok = True
                if _debug_tag:
                    log(f"WARN: {_debug_tag} SSL 校验失败，verify=False 降级成功 (attempt={attempt})")
                result = r2
                break
            last_reason = f"{reason} → fallback:{rsn2}"
            should_retry = sr2
        if is_last_attempt or not should_retry:
            break
        real_retries_performed += 1
        backoff = _HTTP_RETRY_BACKOFF  # v1.17 常量1.0s退避（不再0.25/0.5/1/2/4指数递增，原空耗7.75s）
        if _debug_tag:
            log(f"WARN: {_debug_tag} 请求失败，{backoff:.2f}s 后第{real_retries_performed}/{retries}次重试… 原因: {last_reason}")
        # 退避也分段sleep（100ms step），极端情况下切歌响应更灵敏
        slept = 0.0
        step = 0.1
        while slept < backoff:
            time.sleep(step)
            slept += step
            break  # 实际仍然完整sleep，但结构上保持与其他逻辑一致

    if (not final_ok) and _debug_tag:
        if real_retries_performed > 0:
            log(f"WARN: {_debug_tag} 真实重试 {real_retries_performed} 次后仍失败 ❌ 最终原因: {last_reason}")
        else:
            log(f"WARN: {_debug_tag} 未重试即失败 ❌ 最终原因: {last_reason}")
    return result


def _trad_to_simp(text: str) -> str:
    """用 OpenCC 繁转简（无依赖时原样返回）。Mac 版不强依赖 opencc，避免额外 pip。"""
    if not text:
        return ""
    try:
        import opencc
        if not hasattr(_trad_to_simp, "_cc"):
            try:
                _trad_to_simp._cc = opencc.OpenCC("t2s.json")
            except Exception:
                _trad_to_simp._cc = None
        if _trad_to_simp._cc:
            return _trad_to_simp._cc.convert(text)
    except Exception:
        pass
    return text


def _artist_match(found: str, expected: str) -> bool:
    if not expected:
        return True
    if not found:
        return False
    fa = _trad_to_simp(found).strip().lower()
    ea = _trad_to_simp(expected).strip().lower()
    if not ea or not fa:
        return False
    if fa == ea:
        return True
    # 常见：英文名 "/" 或 "," 或 "、" 分隔，任一匹配即中
    import re
    f_items = [x.strip() for x in re.split(r'[/,&、]|feat\.?|ft\.?|\&', fa) if x.strip()]
    e_items = [x.strip() for x in re.split(r'[/,&、]|feat\.?|ft\.?|\&', ea) if x.strip()]
    if not f_items or not e_items:
        return False
    for ei in e_items:
        for fi in f_items:
            if ei == fi:
                return True
            if len(ei) >= 3 and (ei in fi or fi in ei):
                return True
    return False


def _fetch_with_t2s_fallback(fn, tag: str, artist: str, title: str, **kwargs):
    """繁简兜底：先搜原词，失败再繁/简转换后再搜一次。"""
    r = fn(artist, title, **kwargs)
    if r:
        return r
    simp_artist = _trad_to_simp(artist)
    simp_title = _trad_to_simp(title)
    if simp_artist != artist or simp_title != title:
        r2 = fn(simp_artist, simp_title, **kwargs)
        if r2:
            src_name = None
            if isinstance(r2, tuple):
                src_name, *rest = r2
                if len(rest) == 1:
                    return (f"{src_name}(繁→简)", rest[0])
                if len(rest) >= 2:
                    return (f"{src_name}(繁→简)", rest[0], rest[1])
            else:
                return r2
    return None


def _run_single_lyric_fetch(fn, artist: str, title: str, is_simp_variant: bool, duration_sec=None):
    """══ v1.12 新增 ANY 异常打 SINGLE_EXC 日志，不再静默吞（解决 6s 超时后网易云/QQ 完全无日志的问题）"""
    import traceback
    fn_name = getattr(fn, "__name__", repr(fn))[:80]
    try:
        if duration_sec:
            r = fn(artist, title, duration_sec=duration_sec)
        else:
            r = fn(artist, title)
        if r:
            if is_simp_variant:
                if isinstance(r, tuple) and len(r) >= 2:
                    return (f"{r[0]}(繁→简)", *r[1:])
                return r
            return r
    except Exception as _e:
        try:
            log(f"[LYRIC_PROFILE] SINGLE_EXC fn={fn_name} a={artist!r} t={title!r} dur={duration_sec} simp={is_simp_variant} msg={_e!r} tb={traceback.format_exc(limit=2)[:300]}")
        except Exception:
            pass
    return None


def _netease_fetch_lyrics_raw(artist: str, title: str):
    try:
        import requests
        tag = f"网易云歌词[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j = _http_get_json(
            f"https://music.163.com/api/search/get?s={q}&type=1&limit=3",
            referer="https://music.163.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=tag)
        songs = []
        if isinstance(j, dict):
            songs = (j.get("result") or {}).get("songs") or []
        sid = None
        for item in songs:
            if not isinstance(item, dict):
                continue
            found_artist = " ".join(a.get("name", "") for a in (item.get("artists") or []) if isinstance(a, dict))
            if not _artist_match(found_artist, artist):
                continue
            found_title = item.get("name") or ""
            if found_title and found_title.strip().lower() != title.strip().lower():
                # 非精确标题也接受，但要做个宽松判定（否则别名永远搜不到）
                t1 = _trad_to_simp(found_title).strip().lower()
                t2 = _trad_to_simp(title).strip().lower()
                if t1 != t2 and t2 not in t1 and t1 not in t2:
                    continue
            sid = item.get("id")
            if sid:
                break
        if not sid:
            return None
        j2 = _http_get_json(
            f"https://music.163.com/api/song/lyric?id={sid}&lv=1&kv=1&tv=-1",
            referer="https://music.163.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=f"网易云歌词LRC {sid}")
        if not isinstance(j2, dict):
            return None
        lrc_text = (j2.get("lrc") or {}).get("lyric") or ""
        trans_text = (j2.get("tlyric") or {}).get("lyric") or ""
        if not lrc_text:
            return None
        return ("网易云", lrc_text, trans_text)
    except Exception:
        pass
    return None


def _netease_fetch_lyrics(artist: str, title: str):
    return _fetch_with_t2s_fallback(_netease_fetch_lyrics_raw, "网易云歌词", artist, title)


def _qqmusic_fetch_lyrics_raw(artist: str, title: str):
    try:
        import requests
        tag = f"QQ音乐歌词[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j1 = _http_get_json(
            f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=3&w={q}&format=json",
            referer="https://y.qq.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=tag + "/搜索")
        songs = []
        if isinstance(j1, dict):
            songs = (((j1.get("data") or {}).get("song") or {}).get("list")) or []
        for song in songs:
            if not isinstance(song, dict):
                continue
            found_artist = ";".join(
                s.get("name", "") for s in (song.get("singer") or []) if isinstance(s, dict))
            if not _artist_match(found_artist, artist):
                continue
            songmid = song.get("songmid") or song.get("songMid") or ""
            if not songmid:
                continue
            j2 = _http_get_json(
                f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={songmid}&format=json&nobase64=1",
                referer="https://y.qq.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=tag + f"/歌词({songmid})")
            if not isinstance(j2, dict):
                continue
            lrc = j2.get("lyric") or ""
            if lrc:
                return ("QQ音乐", lrc, "")
    except Exception:
        pass
    return None


def _qqmusic_fetch_lyrics(artist: str, title: str):
    return _fetch_with_t2s_fallback(_qqmusic_fetch_lyrics_raw, "QQ音乐歌词", artist, title)


# ══ v1.12 按要求彻底移除酷狗歌词源（仅剩 LRCLIB + 网易云 + QQ音乐 三个歌词源）══
#    之前虽未 submit 酷狗任务，但函数残留=未来误加风险+代码垃圾，现在硬删除


# ══ v1.12 LRCLIB 精准/模糊彻底拆分（同 PC v6.72，解决重复HTTP+重复精准404日志+占满线程槽）══
#   - precise_only：必须有 duration，**只选 duration 差<5s 的 artist/title 匹配项**，否则直接 None（绝不做模糊）
#   - fuzzy_only：**完全忽略 duration 参数**，仅按 artist/title 匹配打分（绝不碰精准 duration 过滤）

def _lrclib_fetch_items_common(artist: str, title: str):
    """Mac版LRCLIB统一用/search接口（Mac版无/get单独接口），这里抽公共HTTP+列表解析"""
    try:
        import requests
        q = requests.utils.quote(f"{title} {artist}")
        base_url = f"https://lrclib.net/api/search?q={q}"
        j = None
        tag = f"LRCLIB[{artist}-{title}]"
        for attempt in range(_LYRIC_LRCLIB_RETRIES):
            j = _http_get_json(base_url, timeout=_LYRIC_LRCLIB_TIMEOUT, retries=1, _debug_tag=f"{tag}#search{attempt+1}")
            if j is not None:
                break
            time.sleep(0.5)
        items = []
        if isinstance(j, list):
            items = j
        elif isinstance(j, dict) and isinstance(j.get("data"), list):
            items = j["data"]
        return items
    except Exception:
        return None


def _lrclib_pick_synced_plain_common(best_match):
    """公共：从best_match dict里取synced/plain/trans_text，plain兜底转[00:00.00]"""
    synced = best_match.get("syncedLyrics") or ""
    plain = best_match.get("plainLyrics") or ""
    trans_text = best_match.get("translatedLyrics") or ""
    lrc = synced if synced else plain
    if not lrc:
        return None
    if not synced and plain:
        lrc = "[00:00.00]" + plain.replace("\n", "\n[00:00.00]")
    return (lrc, trans_text)


def _lrclib_fetch_lyrics_raw_precise_only(artist: str, title: str, duration_sec=None):
    """LRCLIB 精准版：**只有 duration 差距<5s 的才 score，否则直接 None**（绝不做模糊兜底）"""
    try:
        dur = 0
        try: dur = int(duration_sec or 0)
        except: dur = 0
        if dur <= 0:
            return None
        items = _lrclib_fetch_items_common(artist, title)
        if not items:
            return None
        best_match = None
        best_score = -1e9
        for it in items:
            if not isinstance(it, dict):
                continue
            plain_lrc = it.get("plainLyrics") or ""
            synced_lrc = it.get("syncedLyrics") or ""
            if not synced_lrc and not plain_lrc:
                continue
            found_artist = it.get("artistName") or ""
            found_title = it.get("trackName") or ""
            if not _artist_match(found_artist, artist):
                continue
            ft = _trad_to_simp(found_title).strip().lower()
            tt = _trad_to_simp(title).strip().lower()
            title_ok = (ft == tt) or (tt and tt in ft) or (ft and ft in tt)
            if not title_ok:
                continue
            it_dur = 0
            try: it_dur = int(it.get("duration") or 0)
            except: it_dur = 0
            diff = abs(it_dur - dur) if it_dur > 0 else 9999
            # ══ v1.12 PRECISE_ONLY：diff >=5s 直接跳过（哪怕是唯一匹配也不勉强，交给fuzzy解决）══
            if diff >= 5:
                continue
            score = 0
            if ft == tt:
                score += 100
            score += (50 - diff)  # diff 0-4 → 50-46
            if synced_lrc:
                score += 20
            if it.get("instrumental"):
                score -= 10
            if score > best_score:
                best_score = score
                best_match = it
        if best_match is None:
            return None
        picked = _lrclib_pick_synced_plain_common(best_match)
        if picked is None:
            return None
        return ("LRCLIB-precise", picked[0], picked[1])
    except Exception:
        pass
    return None


def _lrclib_fetch_lyrics_raw_fuzzy_only(artist: str, title: str):
    """LRCLIB 模糊版：**完全忽略 duration，绝不做任何精准diff过滤**（与precise彻底并发独立）"""
    try:
        items = _lrclib_fetch_items_common(artist, title)
        if not items:
            return None
        best_match = None
        best_score = -1e9
        for it in items:
            if not isinstance(it, dict):
                continue
            plain_lrc = it.get("plainLyrics") or ""
            synced_lrc = it.get("syncedLyrics") or ""
            if not synced_lrc and not plain_lrc:
                continue
            found_artist = it.get("artistName") or ""
            found_title = it.get("trackName") or ""
            if not _artist_match(found_artist, artist):
                continue
            ft = _trad_to_simp(found_title).strip().lower()
            tt = _trad_to_simp(title).strip().lower()
            title_ok = (ft == tt) or (tt and tt in ft) or (ft and ft in tt)
            if not title_ok:
                continue
            score = 0
            if ft == tt:
                score += 100
            # ══ v1.12 FUZZY_ONLY：duration 完全不参与score（精准/模糊彻底解耦）
            if synced_lrc:
                score += 20
            if it.get("instrumental"):
                score -= 10
            if score > best_score:
                best_score = score
                best_match = it
        if best_match is None:
            return None
        picked = _lrclib_pick_synced_plain_common(best_match)
        if picked is None:
            return None
        return ("LRCLIB-fuzzy", picked[0], picked[1])
    except Exception:
        pass
    return None


def _lrclib_fetch_lyrics_precise(artist: str, title: str, duration_sec=None):
    """精准wrapper：繁简转换 + precise_only raw"""
    r = _lrclib_fetch_lyrics_raw_precise_only(artist, title, duration_sec=duration_sec)
    if r:
        return r
    simp_artist = _trad_to_simp(artist)
    simp_title = _trad_to_simp(title)
    if simp_artist != artist or simp_title != title:
        r2 = _lrclib_fetch_lyrics_raw_precise_only(simp_artist, simp_title, duration_sec=duration_sec)
        if r2:
            return (f"{r2[0]}(繁→简)", *r2[1:])
    return None


def _lrclib_fetch_lyrics_fuzzy(artist: str, title: str):
    """模糊wrapper：繁简转换 + fuzzy_only raw（**完全不碰duration精准**）"""
    r = _lrclib_fetch_lyrics_raw_fuzzy_only(artist, title)
    if r:
        return r
    simp_artist = _trad_to_simp(artist)
    simp_title = _trad_to_simp(title)
    if simp_artist != artist or simp_title != title:
        r2 = _lrclib_fetch_lyrics_raw_fuzzy_only(simp_artist, simp_title)
        if r2:
            return (f"{r2[0]}(繁→简)", *r2[1:])
    return None


def _fetch_lyrics_bg(artist: str, title: str, song_key: str, duration_sec=None, t_intro: float = 0.0):
    """后台线程：3 平台（LRCLIB+网易云+QQ音乐）× 繁简 2 variants + LRCLIB 精确/模糊 并发搜歌词。
    ══ v1.14 P1-5 阶梯提交 + 最优匹配评分（同 PC v6.74）：
       T0 = 切歌提示发出时刻；T1=线程真正开始；
       第一阶段 MIN_WAIT_S=1.5s 内只提交 variants[0]（简体×3条快源）；窗口内所有 HIT 参与评分，保留 best；
       超时追加第二阶段剩余（繁体+LRCLIB_fuzzy），最终取 best_score；
       第一个 HIT 出现后 cancel pending，但 running 完成的后续结果仍可参与评分。
    命中 → 写 timeline/trans_timeline + 缓存 + 补位 0..current_idx（歌词下载时进度已推进部分）"""
    try:
        t1 = time.time()
        t0 = t_intro if t_intro > 0 else t1
        log(f"[LYRIC_PROFILE] T1 线程调度启动 T0={t0:.3f} T1={t1:.3f} gap={(t1-t0)*1000:.0f}ms song={song_key}")
        simp_a = _trad_to_simp(artist)
        simp_t = _trad_to_simp(title)
        changed = simp_a != artist or simp_t != title

        executor = _LYRIC_EXECUTOR

        # ══ v1.14 P1-5 _schedule_tasks：两阶段阶梯提交
        # 第一阶段（PHASE1）：variants[0]（原词/简体 当 changed 时先只提交 繁原词 这份即 variants[0]）× (LRCLIB_precise + 网易云 + QQ) = 最多 3 条
        #   注：用户需求 "variants[0]（简体）" — 这里统一优先提交「artist/title 原词」这份作为 variants[0]；changed 时再追加 simp 那份
        phase1_tasks = {}
        # 1) LRCLIB-precise × variants[0]（原词 artist,title）
        phase1_tasks[executor.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_precise, artist, title, False, duration_sec)] = ("LRCLIB-precise", False)
        # 2) 网易云 × variants[0]
        phase1_tasks[executor.submit(_run_single_lyric_fetch, _netease_fetch_lyrics, artist, title, False)] = ("网易云", False)
        # 3) QQ音乐 × variants[0]
        phase1_tasks[executor.submit(_run_single_lyric_fetch, _qqmusic_fetch_lyrics, artist, title, False)] = ("QQ音乐", False)

        t2 = time.time()
        log(f"[LYRIC_PROFILE] T2_phase1_submit_done@{t2:.3f} tasks={len(phase1_tasks)} MIN_WAIT_S={_MIN_WAIT_S}s variants(chged={changed}) → phase2 延迟追加")

        import traceback
        STEP_S = 0.1

        # ══ v1.14 P1-5 _consume_until_ok：维护 best_result/best_score，MIN_WAIT_S 窗口内所有 HIT 评分保留最高
        best_result = None
        best_score = -1e9
        processed_fids = set()
        phase2_submitted = False
        phase2_tasks = {}

        def _submit_phase2():
            """追加第二阶段：繁体(changed时) + LRCLIB_fuzzy × 所有 variants"""
            nonlocal phase2_submitted, phase2_tasks
            if phase2_submitted:
                return
            phase2_submitted = True
            # LRCLIB-fuzzy × variants[0]
            phase2_tasks[executor.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_fuzzy, artist, title, False)] = ("LRCLIB-fuzzy", False)
            if changed:
                # 简体 variants: LRCLIB-precise + LRCLIB-fuzzy + 网易云 + QQ
                phase2_tasks[executor.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_precise, simp_a, simp_t, True, duration_sec)] = ("LRCLIB-precise-simp", True)
                phase2_tasks[executor.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_fuzzy, simp_a, simp_t, True)] = ("LRCLIB-fuzzy-simp", True)
                phase2_tasks[executor.submit(_run_single_lyric_fetch, _netease_fetch_lyrics, simp_a, simp_t, True)] = ("网易云-simp", True)
                phase2_tasks[executor.submit(_run_single_lyric_fetch, _qqmusic_fetch_lyrics, simp_a, simp_t, True)] = ("QQ音乐-simp", True)
            log(f"[LYRIC_PROFILE] P1-5_phase2_submit@{time.time():.3f} 追加 {len(phase2_tasks)} 条任务（fuzzy+简体variants）")

        def _all_tasks():
            return dict(list(phase1_tasks.items()) + list(phase2_tasks.items()))

        first_hit_occurred = False
        round_cancelled_all_pending = False

        # ══ 两轮（S1=6s含MIN_WAIT窗口，S2=12s 兜底）
        for i, tmo in enumerate((_LYRIC_FIRST_RESULT_TMO_S1, _LYRIC_FIRST_RESULT_TMO_S2), 1):
            rtag = f"R{i}"
            t_r0 = time.time()
            round_done = False
            try:
                while True:
                    now_loop = time.time()
                    elapsed = now_loop - t_r0
                    # ══ v1.14 P1-5：MIN_WAIT_S 到期后仍未 best → 触发 phase2 提交
                    if (not phase2_submitted) and (now_loop - t2 >= _MIN_WAIT_S):
                        # 窗口内没出现有效 best 或 best_score 较低（<0.3 认为质量差），都追加 phase2
                        if best_result is None or best_score < 0.3:
                            _submit_phase2()
                    if elapsed >= tmo:
                        pending_cnt = sum(1 for f in _all_tasks() if not f.done())
                        done_cnt = len(_all_tasks()) - pending_cnt
                        log(f"[LYRIC_PROFILE] T3_timeout@{now_loop:.3f} round={rtag} 整体超时 {tmo}s best={'YES' if best_result else 'NO'} best_score={best_score:.3f}，已完成 {done_cnt} 条，pending {pending_cnt} 条立刻全部 cancel")
                        for f in _all_tasks():
                            if not f.done():
                                f.cancel()
                        round_cancelled_all_pending = True
                        break
                    remaining = max(0.01, min(STEP_S, tmo - elapsed))
                    all_futs = list(_all_tasks().keys())
                    if not all_futs:
                        break
                    done, _pending = concurrent.futures.wait(
                        all_futs, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    tasks_now = _all_tasks()
                    for fut in done:
                        fid = id(fut)
                        if fid in processed_fids:
                            continue
                        processed_fids.add(fid)
                        t_done = time.time()
                        dt_ms = (t_done - t_r0) * 1000
                        tag_tup = tasks_now.get(fut, ("?", False))
                        try:
                            r = fut.result(timeout=0)
                        except Exception as _ie:
                            log(f"[LYRIC_PROFILE] task {rtag}:{tag_tup[0]} @{t_done:.3f} +{dt_ms:.0f}ms -> EXC {_ie!r}")
                            try:
                                log(f"[LYRIC_PROFILE]   EXC_TB {traceback.format_exc(limit=2)[:300]}")
                            except Exception:
                                pass
                            continue
                        if r:
                            # ══ v1.14 P1-5：综合评分，保留最高分 best_result
                            # 先把 r 解析成 (name, tl, trans) 形式供评分
                            try:
                                src_n = str(r[0]) if r and len(r) >= 1 else ""
                                lrc_t = r[1] if (r and len(r) >= 2) else ""
                                tl_scored = _parse_lrc(lrc_t) if isinstance(lrc_t, str) else (lrc_t if isinstance(lrc_t, list) else [])
                                trans_t = r[2] if (r and len(r) >= 3) else ""
                                ttl_scored = _parse_lrc(trans_t) if isinstance(trans_t, str) else (trans_t if isinstance(trans_t, list) else [])
                                r_for_score = (src_n, tl_scored, ttl_scored if ttl_scored else None)
                            except Exception:
                                r_for_score = r
                            sc = _score_lyric_match(r_for_score, artist, title, duration_sec)
                            src_show = ""
                            try:
                                src_show = str(r[0])[:60]
                            except Exception:
                                src_show = ""
                            log(f"[LYRIC_PROFILE] task {rtag}:{tag_tup[0]} @{t_done:.3f} +{dt_ms:.0f}ms -> HIT src={src_show} score={sc:.3f} prev_best={best_score:.3f}")
                            if sc > best_score:
                                best_score = sc
                                best_result = r
                                log(f"[LYRIC_PROFILE] P1-5_NEW_BEST@{t_done:.3f} score={sc:.3f} src={src_show} → 取代 prev_best")
                            # 第一个 HIT 出现 → cancel 所有 pending（running 跑完的仍会进入评分）
                            if not first_hit_occurred:
                                first_hit_occurred = True
                                cancel_attempt = 0
                                cancel_ok = 0
                                for f in tasks_now:
                                    if not f.done():
                                        cancel_attempt += 1
                                        if f.cancel():
                                            cancel_ok += 1
                                log(f"[LYRIC_PROFILE] P1-5_FIRST_HIT_CANCEL@{t_done:.3f} cancel_attempt={cancel_attempt} cancel_ok={cancel_ok} — 仅取消pending；running完成仍可评分")
                        else:
                            log(f"[LYRIC_PROFILE] task {rtag}:{tag_tup[0]} @{t_done:.3f} +{dt_ms:.0f}ms -> MISS")
                    # 检查是否：MIN_WAIT_S 已过 + best_result 已有 + phase2 未提交 → 此时立刻提交 phase2 也可以
                    if (not phase2_submitted) and (now_loop - t2 >= _MIN_WAIT_S) and (best_result is None or best_score < 0.5):
                        _submit_phase2()
                    # 所有 future 都 done → 不用等超时
                    if all(f.done() for f in _all_tasks()):
                        log(f"[LYRIC_PROFILE] T3_all_done@{now_loop:.3f} round={rtag} {len(_all_tasks())} 条任务全部跑完 best={'YES' if best_result else 'NO'} best_score={best_score:.3f}")
                        break
            except Exception as _outer_round:
                try:
                    log(f"[LYRIC_PROFILE] T3_ROUND_OUTER_EXC round={rtag} msg={_outer_round!r} tb={traceback.format_exc(limit=3)[:400]}")
                except Exception:
                    pass
            finally:
                try:
                    for f in _all_tasks():
                        if not f.done():
                            f.cancel()
                except Exception:
                    pass
            # ══ v1.14 P1-5：每轮结束后 MIN_WAIT_S 已到 → 若 best 已有且分数够高（>=0.3），无需进下一轮
            if i == 1 and best_result is not None and best_score >= 0.3:
                log(f"[LYRIC_PROFILE] P1-5_R1_END_OK@{time.time():.3f} R1结束 best_score={best_score:.3f}≥0.3 → 接受best，跳过R2")
                break
            if i == 1:
                with _state_lock:
                    if _MEDIA_STATE.get("song") != song_key:
                        log(f"[LYRIC_PROFILE] R1 结束已切歌 → 放弃 R2 song={song_key}")
                        return
                # R1 结束但没 phase2 → 提交 phase2 再进 R2（理论上 MIN_WAIT_S 已经触发）
                if not phase2_submitted:
                    _submit_phase2()

        result = best_result
        t5 = time.time()
        if result is None:
            log(f"[LYRIC_PROFILE] T5_fail@{t5:.3f} 两轮未命中 总耗时={(t5-t0)*1000:.0f}ms(自T0) song={song_key}")
            log(f"歌词: 所有源失败 {artist} - {title}")
            return
        log(f"[LYRIC_PROFILE] P1-5_FINAL_BEST@{t5:.3f} 总耗时={(t5-t0)*1000:.0f}ms(自T0) best_score={best_score:.3f} src={str(result[0])[:60] if result else ''}")
        src_name, lrc_text, trans_text = result[0], (result[1] if len(result) > 1 else ""), (result[2] if len(result) > 2 else "")
        timeline_raw = _parse_lrc(lrc_text)
        trans_raw = _parse_lrc(trans_text or "")
        # ══ v1.11 P0 修复：任何来源（LRCLIB/网易云/QQ/缓存）写 timeline/trans_timeline 之前，
        #    统一走 _sanitize_timeline 二次净化，确保写入 _MEDIA_STATE 的 100% 是干净 (float, str) 二元组
        tl_clean = _sanitize_timeline(timeline_raw, tag="timeline")
        trans_clean = _sanitize_timeline(trans_raw or [], tag="trans_timeline") if trans_raw else []
        if not tl_clean:
            log(f"[LYRIC_PROFILE] T5_fail LRC空 总耗时={(time.time()-t0)*1000:.0f}ms source={src_name}")
            log(f"歌词: {src_name} 返回空/非同步LRC，放弃")
            return
        with _state_lock:
            if _MEDIA_STATE.get("song") != song_key:
                log(f"[LYRIC_PROFILE] T5_fail 命中但已切歌 总耗时={(time.time()-t0)*1000:.0f}ms song={song_key}")
                return
            _MEDIA_STATE["timeline"] = tl_clean
            if trans_clean:
                _MEDIA_STATE["trans_timeline"] = trans_clean
        t5b = time.time()
        log(f"[LYRIC_PROFILE] T5_ok@{t5b:.3f} 命中写入 timeline 总耗时={(t5b-t0)*1000:.0f}ms(自T0) source={src_name} lines={len(tl_clean)}")
        log(f"歌词: {src_name} ({len(tl_clean)} 行)" + (f" +翻译 {len(trans_clean)} 行" if trans_clean else ""))
        # 后面 timeline/trans_timeline 变量统一换成 sanitize 后的版本（补位/force_emit/索引/timer 全用干净版）
        timeline = tl_clean
        trans_timeline = trans_clean
        # ══ v1.08 同PC v6.68：先发歌词决策，写缓存后移（缓存IO不阻塞第一句）+ 详细诊断
        with _state_lock:
            if _MEDIA_STATE.get("song") != song_key:
                log(f"[LYRIC_PROFILE] POST_T5:DROP_song_changed_before_emit song={song_key!r} current={_MEDIA_STATE.get('song')!r}")
                _cache_put_lyric(artist, title, src_name, tl_clean, trans_clean)
                return
            playing = _MEDIA_STATE.get("playing", False)
            progress_raw = _MEDIA_STATE["progress_ms"]
            eff_ms = progress_raw
            if playing and _last_media_ts > 0:
                eff_ms += int((time.time() - _last_media_ts) * 1000)
            dt_from_last_media = (int((time.time() - _last_media_ts) * 1000) if _last_media_ts > 0 else 0)
            if _LYRIC_OFFSET_MS:
                eff_ms += _LYRIC_OFFSET_MS
                if eff_ms < 0:
                    eff_ms = 0
            cur_idx, cur_txt = _current_lyric_idx(timeline, eff_ms)
            lrc_t0 = (timeline[0][0] * 1000 if timeline else 0)
            lrc_t_cur = (timeline[cur_idx][0] * 1000 if (timeline and cur_idx >= 0 and cur_idx < len(timeline)) else None)
            log(f"[LYRIC_PROFILE] POST_T5:DECIDE song={song_key!r} lines={len(timeline)} playing={playing} progress_ms={progress_raw} wall_clamp={dt_from_last_media}ms OFFSET={_LYRIC_OFFSET_MS}ms → eff_ms={eff_ms} LRC[0]={lrc_t0}ms LRC[cur_idx={cur_idx}]={lrc_t_cur}ms cur_txt(repr)={cur_txt!r} cur_idx_last_lyric_idx={_last_lyric_idx}")
        if cur_idx > 0:
            log(f"[LYRIC_PROFILE] POST_T5:BRANCH_CATCHUP cur_idx={cur_idx}>0 → do_catchup 0..{cur_idx} cur_txt_empty={not bool(cur_txt)}")
            log(f"歌词下载完成时进度已推进到 idx={cur_idx}，启动补位 0..{cur_idx}")
            _catchup_lyrics_until(song_key, cur_idx)
        else:
            log(f"[LYRIC_PROFILE] POST_T5:BRANCH_FORCE cur_idx={cur_idx} → _force_emit_current_lyric cur_txt_empty={not bool(cur_txt)}")
            _force_emit_current_lyric(song_key)
        # ══ v1.08 写缓存移到最后，不阻塞歌词输出
        # ══ v1.11 P0 修复：缓存里也存 sanitize 后的干净 timeline，下次读出来就不带坏行
        _cache_put_lyric(artist, title, src_name, tl_clean, trans_clean)
    except Exception as e:
        import traceback
        log(f"[LYRIC_PROFILE] POST_T5:FATAL_EXCEPTION msg={e!r} traceback={traceback.format_exc()}")
        log(f"WARN: 歌词后台线程异常(已吞): {e}")


def _force_emit_current_lyric(song_key: str):
    global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_emit_wall_ts_ms
    with _state_lock:
        if _MEDIA_STATE.get("song") != song_key:
            return
        timeline = _MEDIA_STATE.get("timeline", [])
        trans_timeline = _MEDIA_STATE.get("trans_timeline", [])
        if not timeline:
            return
        playing = _MEDIA_STATE.get("playing", False)
        smtc_f = float(_MEDIA_STATE["progress_ms"])
        wall_f = 0.0
        if playing and _last_media_ts > 0:
            wall_f = (time.time() - _last_media_ts) * 1000.0
        OFFSET_f = float(_LYRIC_OFFSET_MS)
        eff_ms_f = smtc_f + wall_f + OFFSET_f
        if eff_ms_f < 0.0:
            eff_ms_f = 0.0
        eff_ms = int(eff_ms_f)
        idx, cur = _current_lyric_idx(timeline, eff_ms)
        trans_idx, cur_trans = _current_lyric_idx(trans_timeline, eff_ms)
        if not cur:
            # ══ v1.08 同PC v6.68：首句/间奏空行不静默return
            log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE song={song_key!r} idx={idx} cur_txt(repr)='' eff_ms={eff_ms} → 仍推进idx并调下一句Timer，避免卡在首句空行")
            if idx > _last_lyric_idx:
                _last_lyric_idx = idx
                _last_trans_idx = trans_idx
            if playing and idx + 1 < len(timeline):
                next_t_ms_f = float(timeline[idx + 1][0]) * 1000.0 + OFFSET_f
                wait_ms_f = max(0.0, (next_t_ms_f - eff_ms_f) - _LYRIC_TIMER_PREMISS_MS)
                log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE_SCHEDULE song={song_key!r} idx={idx} next_idx={idx+1} LRC_next_t={next_t_ms_f:.0f}ms wait_ms_float={wait_ms_f:.2f}")
                _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)
            else:
                log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE_NOSCHEDULE song={song_key!r} idx={idx} playing={playing} has_next={idx+1<len(timeline)} → 无下一句可调度（将依赖tick兜底）")
            return
        if idx == _last_lyric_idx and trans_idx == _last_trans_idx:
            if playing and idx + 1 < len(timeline):
                next_t_ms_f = float(timeline[idx + 1][0]) * 1000.0 + OFFSET_f
                wait_ms_f = max(0.0, (next_t_ms_f - eff_ms_f) - _LYRIC_TIMER_PREMISS_MS)
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] FORCE:NO_PROGRESS_SCHEDULE idx={idx}->{idx+1} next_t_ms_f={next_t_ms_f:.3f} eff_ms={eff_ms_f:.3f} wait_ms_f={wait_ms_f:.3f}ms (Timer_s={wait_ms_f/1000.0:.6f}s)")
                _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)
            return
        lrc_t_now = float(timeline[idx][0]) * 1000.0 + OFFSET_f
        drift_now = eff_ms_f - lrc_t_now
        prev_idx = _last_lyric_idx
        if prev_idx < 0:
            lrc_gap_ms = 0
        else:
            lrc_gap_ms = int((timeline[idx][0] - timeline[prev_idx][0]) * 1000)
        _last_lyric_idx = idx
        _last_trans_idx = trans_idx
        _last_lyric_raw = cur
        _last_trans_raw = cur_trans
        formatted = _format_lyric_line(cur, cur_trans)
        ts = time.time()
        cur_wall_ms = int(ts * 1000)
        N = idx + 1
        if _last_emit_wall_ts_ms == 0.0:
            real_gap_ms = 0
        else:
            real_gap_ms = cur_wall_ms - int(_last_emit_wall_ts_ms)
        # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
        _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
        if _LYRIC_SYNC_LOG:
            log(f"[LYRIC_SYNC] FORCE:EMIT drift_now={drift_now:.3f}ms eff={eff_ms_f:.3f}ms LRC_t={lrc_t_now:.3f}ms idx={idx}/{len(timeline)} real_gap={real_gap_ms} LRC_gap={lrc_gap_ms}")
    # v1.18（同步 PC v6.78）：gap_line 计算 + 合并到歌词行末尾
    if _last_emit_wall_ts_ms == 0.0:
        gap_line = f"{N} (FIRST)"
    else:
        gap_line = f"[{real_gap_ms}ms] {N}  | LRC_gap={lrc_gap_ms}ms"
    log(f"歌词(补位) [{ts:.3f}] idx={idx}/{len(timeline)} offset={_LYRIC_OFFSET_MS}ms: {cur}" + (f" | 翻译: {cur_trans}" if cur_trans else "") + f"  | {gap_line}")
    _last_emit_wall_ts_ms = float(cur_wall_ms)
    if playing and idx + 1 < len(timeline):
        next_f = float(timeline[idx + 1][0]) * 1000.0 + OFFSET_f
        cur_f = float(timeline[idx][0]) * 1000.0 + OFFSET_f
        wait_ms_f = max(0.0, (next_f - cur_f) - _LYRIC_TIMER_PREMISS_MS)
        old_next_t_ms = int(timeline[idx + 1][0] * 1000 + _LYRIC_OFFSET_MS)
        old_cur_t_ms = int(timeline[idx][0] * 1000 + _LYRIC_OFFSET_MS)
        old_wait_ms = max(0, old_next_t_ms - old_cur_t_ms) - int(LYRIC_TICK_MS * 0.5)
        if _LYRIC_SYNC_LOG:
            log(f"[LYRIC_SYNC] FORCE:SCHEDULE_NEXT idx={idx}->{idx+1} wait_ms_f={wait_ms_f:.3f}ms (Timer_s={wait_ms_f/1000.0:.6f}s) | old_int={old_wait_ms}ms (diff_f-old={wait_ms_f-float(old_wait_ms):.3f}ms)")
        _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)


# ─────────────────────────────────────────────────────────
#  封面 API（与 PC v6.6 完全一致：网易云 / QQ音乐 / 酷狗三平台并行搜 URL）
# ─────────────────────────────────────────────────────────

def _netease_fetch_cover_raw(artist: str, title: str):
    try:
        import requests
        tag = f"网易云封面[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j = _http_get_json(
            f"https://music.163.com/api/search/get?s={q}&type=1&limit=3",
            referer="https://music.163.com/", timeout=5, _debug_tag=tag)
        items = []
        if isinstance(j, dict):
            items = (j.get("result") or {}).get("songs") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            found_artist = " ".join(a.get("name", "") for a in (item.get("artists") or []) if isinstance(a, dict))
            if not _artist_match(found_artist, artist):
                continue
            album = item.get("album") or {}
            if not isinstance(album, dict):
                continue
            pic = album.get("picUrl") or album.get("blurPicUrl") or album.get("pic_str") or ""
            if pic:
                if pic.startswith("//"):
                    pic = "https:" + pic
                if "param=" not in pic and "?" not in pic:
                    pic = f"{pic}?param=500y500"
                return ("网易云", pic)
    except Exception:
        pass
    return None


def _netease_fetch_cover(artist: str, title: str):
    return _fetch_with_t2s_fallback(_netease_fetch_cover_raw, "网易云封面", artist, title)


def _qqmusic_fetch_cover_raw(artist: str, title: str):
    try:
        import requests
        tag = f"QQ音乐封面[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j = _http_get_json(
            f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=3&w={q}&format=json",
            referer="https://y.qq.com/", timeout=5, _debug_tag=tag)
        songs = []
        if isinstance(j, dict):
            songs = (((j.get("data") or {}).get("song") or {}).get("list")) or []
        for song in songs:
            if not isinstance(song, dict):
                continue
            found_artist = ";".join(s.get("name", "") for s in (song.get("singer") or []) if isinstance(s, dict))
            if not _artist_match(found_artist, artist):
                continue
            albummid = song.get("albummid") or song.get("albumMid") or ""
            if albummid:
                url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg?max_age=2592000"
                return ("QQ音乐", url)
    except Exception:
        pass
    return None


def _qqmusic_fetch_cover(artist: str, title: str):
    return _fetch_with_t2s_fallback(_qqmusic_fetch_cover_raw, "QQ音乐封面", artist, title)


def _kugou_fetch_cover_raw(artist: str, title: str):
    try:
        import requests
        tag = f"酷狗封面[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j = _http_get_json(
            f"https://msearchcdn.kugou.com/api/v3/search/song?keyword={q}&page=1&pagesize=3&showtype=1",
            referer="https://www.kugou.com/", timeout=5, _debug_tag=tag)
        items = []
        if isinstance(j, dict):
            items = (j.get("data") or {}).get("info") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            found_artist = item.get("singername", "") or ""
            if not _artist_match(found_artist, artist):
                continue
            for key in ["album_img", "img_url", "album_photo", "cover_url", "pic"]:
                url = item.get(key) or ""
                if url and url.startswith("http"):
                    return ("酷狗", url)
    except Exception:
        pass
    return None


def _kugou_fetch_cover(artist: str, title: str):
    return _fetch_with_t2s_fallback(_kugou_fetch_cover_raw, "酷狗封面", artist, title)


def _fetch_cover_bg(artist: str, title: str, song_key: str):
    """后台线程：只查 QQ音乐 封面 URL，拿到就写，切歌自动丢弃，全异常静默不崩溃
    ══ v1.13 P0-2：复用全局 _LYRIC_EXECUTOR 常驻池，不临时建池；单 future 不用 as_completed，直接等单个结果"""
    try:
        result = None
        # ══ v1.13 P0-2：单任务（只有 QQ 封面）直接 submit，不用 as_completed/临时池
        fut = _LYRIC_EXECUTOR.submit(_qqmusic_fetch_cover, artist, title)
        try:
            r = fut.result(timeout=6.0)  # ══ 总上限 6s（requests 单请求 2.5s×2步≈5s 内完成）
            if r:
                result = r
        except concurrent.futures.TimeoutError:
            fut.cancel()
        except Exception:
            pass
        if result is None:
            return
        name, url = result
        with _state_lock:
            if _MEDIA_STATE.get("song") != song_key:
                return
            cur_cover = _MEDIA_STATE.get("cover", "")
            if not cur_cover or cur_cover.startswith("data:"):
                _MEDIA_STATE["cover"] = url
                log(f"封面: {name} → {url[:70]}...")
        _cache_put_cover(artist, title, name, url)  # ══ 写持久化缓存 ══
    except Exception as e:
        log(f"WARN: 封面后台线程异常(已吞): {e}")


# ─────────────────────────────────────────────────────────
#  歌词格式化 + tick（tick_lyric 对应 PC v6.6，保留 5 道残留清理防线）
# ─────────────────────────────────────────────────────────

def _format_lyric_line(text: str, trans_text: str = "") -> str:
    import re
    parts = []
    if text:
        parens = re.findall(r'[(（]([^)）]*)[)）]', text)
        main = re.sub(r'\s*[(（][^)）]*[)）]\s*', ' ', text).strip()
        line_parts = []
        if main:
            line_parts.append(f"**{main}**")
        for p in parens:
            line_parts.append(f"*({p})*")
        if not line_parts:
            line_parts.append(f"**{text.strip()}**")
        parts.append(" ".join(line_parts))
    if trans_text:
        parts.append(f"\n`{trans_text}`")
    return "".join(parts)


def tick_lyric():
    """每次 tick 做一次: poll_media + 歌词推算 + 状态切换提示（v6.66 严格模式）"""
    global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_playing_state, _last_emit_wall_ts_ms
    global _catchup_thread_song, _catchup_thread

    poll_media()

    with _state_lock:
        song = _MEDIA_STATE.get("song", "")
        if not song:
            _last_playing_state = None
            return
        playing = _MEDIA_STATE.get("playing", False)
        timeline = _MEDIA_STATE.get("timeline", [])
        trans_timeline = _MEDIA_STATE.get("trans_timeline", [])
        smtc_progress_ms_f = float(_MEDIA_STATE["progress_ms"])
        dt_clamp_f = 0.0
        if playing and _last_media_ts > 0:
            dt_clamp_f = (time.time() - _last_media_ts) * 1000.0
        OFFSET_f = float(_LYRIC_OFFSET_MS)
        eff_ms_f = smtc_progress_ms_f + dt_clamp_f + OFFSET_f
        if eff_ms_f < 0.0:
            eff_ms_f = 0.0
        eff_ms = int(eff_ms_f)
        since_change = time.time() - _last_song_change_ts

        if song != tick_lyric._last_song_sent:
            _cancel_all_lyric_timers(song)
            same_song_catchup_running = (_catchup_thread_song == song)
            if not same_song_catchup_running:
                _cancel_catchup(song)
                _last_lyric_idx = -1
                _last_trans_idx = -1
                _last_lyric_raw = ""
                _last_trans_raw = ""
                _last_emit_wall_ts_ms = 0.0
            now = time.time()
            intro_recently_emitted = (_media_song_intro_emitted_at > 0 and (now - _media_song_intro_emitted_at) < 0.8 and _media_song_intro_emitted_at >= _last_song_change_ts - 0.2)
            formatted = f"**\u25b6 {song}**"
            ts = now
            total = len(timeline)
            ttotal = len(trans_timeline)
            if intro_recently_emitted:
                pass
            else:
                # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
                _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                log(f"切歌提示 [{ts:.3f}] offset={_LYRIC_OFFSET_MS}ms: {song} | timeline={total} 行 + 翻译={ttotal} 行" + ("" if not same_song_catchup_running else " | [补位进行中 跳过reset]"))
            tick_lyric._last_song_sent = song

        if _last_playing_state is not None and _last_playing_state != playing:
            if not playing:
                _cancel_all_lyric_timers(song + "#paused")
                _cancel_catchup(song + "#paused")
            if since_change > 0.3:
                if playing:
                    formatted = "**\u25b6 继续播放**"
                    log_text = "▶ 继续播放"
                else:
                    formatted = "**\u23f8 已暂停**"
                    log_text = "⏸ 已暂停"
                ts = time.time()
                # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
                _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                _last_lyric_raw = f"__STATE__{playing}__"
                _last_trans_raw = ""
                _last_emit_wall_ts_ms = 0.0  # 状态切换后下一句为 FIRST，避免暂停时长污染gap
                log(f"状态提示 [{ts:.3f}] offset={_LYRIC_OFFSET_MS}ms: {log_text}")
                _last_playing_state = playing
                return
        _last_playing_state = playing

        if not playing and eff_ms == 0:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:skip(not playing+eff=0) song={song} eff_ms={eff_ms} smtc_progress_ms={smtc_progress_ms_f:.3f} dt_clamp={dt_clamp_f:.3f} OFFSET={OFFSET_f:.3f} timeline_len={len(timeline)}")
            return
        idx, cur = _current_lyric_idx(timeline, eff_ms)
        trans_idx, cur_trans = _current_lyric_idx(trans_timeline, eff_ms)
        if _LYRIC_SYNC_LOG:
            last_show = _last_lyric_idx
            if idx != -1 and idx < len(timeline):
                lrc_t_ms_show = float(timeline[idx][0]) * 1000.0 + OFFSET_f
                drift_ms_show = eff_ms_f - lrc_t_ms_show
                log(f"[LYRIC_SYNC] tick:eff_decomp eff_ms={eff_ms_f:.3f}(=progress{smtc_progress_ms_f:.3f}+wall{dt_clamp_f:.3f}+OFFSET{OFFSET_f:.3f}) idx={idx} LRC_t_ms={lrc_t_ms_show:.3f} drift_ms={drift_ms_show:.3f} last={last_show}")
            else:
                log(f"[LYRIC_SYNC] tick:eff_decomp eff_ms={eff_ms_f:.3f}(=progress{smtc_progress_ms_f:.3f}+wall{dt_clamp_f:.3f}+OFFSET{OFFSET_f:.3f}) idx={idx} LRC_t_ms=N/A drift_ms=N/A last={last_show}")

        catchup_alive = False
        try:
            with _catchup_lock:
                ct = _catchup_thread
            if ct and ct.is_alive():
                catchup_alive = True
        except Exception:
            pass
        if (not catchup_alive) and _catchup_thread_song == song:
            with _catchup_lock:
                if _catchup_thread_song == song:
                    _catchup_thread_song = ""
        elif catchup_alive and _catchup_thread_song == song:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:muted_by_catchup_alive song={song} idx={idx}")
            return

        clamped_flag = False
        if idx != -1 and _last_lyric_idx >= 0 and idx > _last_lyric_idx + 1:
            lrc_t_ms_raw = float(timeline[idx][0]) * 1000.0 + OFFSET_f
            drift_raw = eff_ms_f - lrc_t_ms_raw
            if _LYRIC_STRICT_SYNC and abs(drift_raw) <= _LYRIC_MAX_DRIFT_MS:
                clamped_flag = False
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] tick:STRICT_PASS_SKIP_CLAMP idx={idx} last={_last_lyric_idx} idx-lastp1={idx-(_last_lyric_idx+1)} drift_raw={drift_raw:.3f}ms <= MAX_DRIFT={_LYRIC_MAX_DRIFT_MS:.3f}ms → 放行不夹逼")
            else:
                clamped_flag = True
                idx = _last_lyric_idx + 1
                cur = timeline[idx][1] if idx < len(timeline) else ""
                trans_idx = idx
                cur_trans = ""
                if trans_timeline:
                    best_dt = 0.6
                    target_t = timeline[idx][0]
                    for tt, ttxt in trans_timeline:
                        dt = abs(tt - target_t)
                        if dt < best_dt and ttxt:
                            best_dt = dt
                            cur_trans = ttxt
                        if best_dt == 0:
                            break
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] tick:CLAMPED_BY_DRIFT idx_orig={idx+1 if clamped_flag else idx}→clamped={idx} last={_last_lyric_idx} drift_raw={drift_raw:.3f}ms MAX_DRIFT={_LYRIC_MAX_DRIFT_MS:.3f}ms STRICT={_LYRIC_STRICT_SYNC} → 夹逼到 last+1")
        else:
            if _LYRIC_SYNC_LOG and idx != -1 and _last_lyric_idx >= 0:
                log(f"[LYRIC_SYNC] tick:clamp_check_NOOP idx={idx} last={_last_lyric_idx} idx<=last+1={idx <= _last_lyric_idx + 1} → 不触发夹逼检查")

        if idx != -1 and _last_lyric_idx >= 0 and idx <= _last_lyric_idx:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:DROP_idx<=last idx={idx} last={_last_lyric_idx} → drop")
            return

        if idx != -1 and (idx != _last_lyric_idx or trans_idx != _last_trans_idx):
            if not cur or not cur.strip():
                _last_lyric_idx = idx
                _last_trans_idx = trans_idx
                if timeline and idx + 1 < len(timeline):
                    next_wait_f = max(0.0, (timeline[idx + 1][0] - timeline[idx][0]) * 1000.0)
                    # v7.01 修链不中断：空行调度也强制min=1ms，不再<=15ms跳过
                    if next_wait_f < 1.0:
                        next_wait_f = 1.0
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] tick:EMPTY_LINE_SCHEDULE_NEXT idx={idx}->{idx+1} next_wait_f={next_wait_f:.3f}ms (Timer_s={next_wait_f/1000.0:.6f}s min=1ms强制)")
                    with _next_lyric_timer_lock:
                        if _next_lyric_timer_song == song:
                            try:
                                t = threading.Timer(next_wait_f / 1000.0,
                                                    _schedule_next_lyric_at,
                                                    args=(song, timeline, trans_timeline, idx + 1, 0.0))
                                t.daemon = True
                                t.start()
                                _next_lyric_timer = t
                            except Exception:
                                pass
                return
            target_idx = idx
            lrc_t_target = float(timeline[target_idx][0]) * 1000.0 + OFFSET_f
            drift_emit = eff_ms_f - lrc_t_target
            prev_idx = _last_lyric_idx
            if prev_idx < 0:
                lrc_gap_ms = 0
            else:
                lrc_gap_ms = int((timeline[idx][0] - timeline[prev_idx][0]) * 1000)
            _last_lyric_idx = idx
            _last_trans_idx = trans_idx
            _last_lyric_raw = cur
            _last_trans_raw = cur_trans
            formatted = _format_lyric_line(cur, cur_trans)
            ts = time.time()
            cur_wall_ms = int(ts * 1000)
            N = idx + 1
            if _last_emit_wall_ts_ms == 0.0:
                real_gap_ms = 0
            else:
                real_gap_ms = cur_wall_ms - int(_last_emit_wall_ts_ms)
            # v1.19（同步 PC v6.79）：统一走 _stage_lyric_event（防溢出覆盖丢句）
            _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:EMIT drift={drift_emit:.3f}ms eff={eff_ms_f:.3f}ms LRC_t={lrc_t_target:.3f}ms clamped={clamped_flag} idx={target_idx}/{len(timeline)} real_gap={real_gap_ms} LRC_gap={lrc_gap_ms}")
            # v1.18（同步 PC v6.78）：gap_line 计算 + 合并到歌词行末尾
            if _last_emit_wall_ts_ms == 0.0:
                gap_line = f"{N} (FIRST)"
            else:
                gap_line = f"[{real_gap_ms}ms] {N}  | LRC_gap={lrc_gap_ms}ms"
            log(f"歌词 [{ts:.3f}] idx={idx}/{len(timeline)} offset={_LYRIC_OFFSET_MS}ms: {cur}" + (f" | 翻译: {cur_trans}" if cur_trans else "") + f"  | {gap_line}")
            _last_emit_wall_ts_ms = float(cur_wall_ms)
            if playing and idx + 1 < len(timeline):
                next_t_f = float(timeline[idx + 1][0]) * 1000.0 + OFFSET_f
                cur_t_f = float(timeline[idx][0]) * 1000.0 + OFFSET_f
                wait_ms_f = max(0.0, (next_t_f - cur_t_f) - _LYRIC_TIMER_PREMISS_MS)
                old_next_t_ms = int(timeline[idx + 1][0] * 1000 + _LYRIC_OFFSET_MS)
                old_cur_t_ms = int(timeline[idx][0] * 1000 + _LYRIC_OFFSET_MS)
                old_wait_ms = max(0, old_next_t_ms - old_cur_t_ms) - int(LYRIC_TICK_MS * 0.5)
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] tick:SCHEDULE_NEXT idx={idx}->{idx+1} wait_ms_f={wait_ms_f:.3f}ms (Timer_s={wait_ms_f/1000.0:.6f}s) | old_int={old_wait_ms}ms (diff_f-old={wait_ms_f-float(old_wait_ms):.3f}ms)")
                _schedule_next_lyric_at(song, timeline, trans_timeline, idx + 1, wait_ms_f)


tick_lyric._last_song_sent = ""  # type: ignore


def _lyric_tick_loop():
    while True:
        try:
            tick_lyric()
        except Exception as e:
            # ══ v1.13 P0-1：严禁 traceback.print_exc() 同步写 stderr
            log(f"歌词tick异常: {e}")
            try:
                log(traceback.format_exc())
            except Exception:
                pass
        time.sleep(LYRIC_TICK_MS / 1000.0)


# ─────────────────────────────────────────────────────────
#  macOS 截屏（screencapture 命令，Pillow 装了也可，这里用原生 screencapture）
# ─────────────────────────────────────────────────────────
_HAS_SHOT = True  # macOS 自带 screencapture，默认可用


def _take_screenshot() -> str:
    import subprocess, tempfile, base64
    if not _HAS_SHOT:
        return ""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        _os.close(fd)
        r = subprocess.run(["screencapture", "-x", "-t", "jpg", "-D", "1", tmp],
                           capture_output=True, timeout=8)
        if r.returncode != 0:
            # 可能多显示器参数不行，退化成默认截图
            r = subprocess.run(["screencapture", "-x", "-t", "jpg", tmp], capture_output=True, timeout=8)
            if r.returncode != 0:
                return ""
        try:
            # 缩放到最大宽 1920（sips 是 macOS 自带）
            subprocess.run(["sips", "-Z", "1920", tmp], capture_output=True, timeout=5)
        except Exception:
            pass
        with open(tmp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        log(f"截屏完成: {len(b64)} chars")
        return b64
    except Exception as e:
        log(f"截屏错误: {e}")
        return ""
    finally:
        if tmp and _os.path.exists(tmp):
            try: _os.remove(tmp)
            except Exception: pass


# ─────────────────────────────────────────────────────────
#  TCP 上报（与 PC v6.6 同协议：AUTH KEY + 每行 JSON payload）
#  注意：SERVER/PORTS/AUTH_KEY 默认值全清空，必须从环境变量设置
# ─────────────────────────────────────────────────────────

_last_good_music = {}


def _validate_env():
    """启动前校验：必须显式设 BOT_SERVER / BOT_PC_PORTS / BOT_PC_KEY，任一缺就报错退出。"""
    errs = []
    if not SERVER:
        errs.append("BOT_SERVER（服务器域名/IP）未设置")
    if not PORTS:
        errs.append("BOT_PC_PORTS（逗号分隔端口列表，例：58890,62002）未设置或为空")
    if not AUTH_KEY:
        errs.append("BOT_PC_KEY（与服务器约定的 AUTH 密钥）未设置")
    return errs


def _connect_one(port, ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        # ══ v1.10（同 PC v6.70）C 层双保险：setsockopt SO_SNDTIMEO/SO_RCVTIMEO=5s
        #    macOS/Darwin struct timeval: 2× c_long = tv_sec tv_usec
        try:
            import struct
            _t5 = struct.pack("ll", 5, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, _t5)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, _t5)
        except Exception:
            pass
        # ══ v1.10 TCP_NODELAY 禁用 Nagle
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # ══ v1.14 P1-3 SO_KEEPALIVE + TCP keepalive 参数（macOS 尽力而为，异常静默降级）
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPALIVE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 30)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except Exception:
            pass
        sock.connect((ip, port))
        sock.sendall(f"AUTH {AUTH_KEY}\n".encode())
        log(f"TCP: 已连接 {SERVER}:{port}")
        return sock
    except Exception as e:
        log(f"TCP: 连接 {SERVER}:{port} 失败: {e}")
        return None


def connect_tcp():
    ip = None
    for i in range(10):
        try:
            ip = socket.getaddrinfo(SERVER, PORTS[0], socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            break
        except Exception:
            time.sleep(3)
    if not ip:
        log(f"TCP: DNS 解析失败 {SERVER}，退出")
        sys.exit(1)
    # ══ v1.14 P1-3 socks 结构升级：dict[port] = {"sock": s, "buf": bytearray(), "last_recv_ts": ts, "last_send_ts": ts}
    socks = {}
    while True:
        for port in PORTS:
            if port in socks and socks[port].get("sock") is not None:
                continue
            s = _connect_one(port, ip)
            if s:
                now = time.time()
                socks[port] = {"sock": s, "buf": bytearray(), "last_recv_ts": now, "last_send_ts": now}
            else:
                time.sleep(1)
        if all(p in socks and socks[p].get("sock") is not None for p in PORTS):
            break
        log(f"TCP: 等待所有端口就绪 ({len([p for p in PORTS if p in socks and socks[p].get('sock')])}/{len(PORTS)})，3s 重试...")
        time.sleep(3)
    return socks


def _reconnect_port(port, ip, socks_dict):
    try:
        old_entry = socks_dict.get(port) or {}
        old_sock = old_entry.get("sock") if isinstance(old_entry, dict) else old_entry
        if old_sock:
            old_sock.close()
    except Exception:
        pass
    while True:
        s = _connect_one(port, ip)
        if s:
            now = time.time()
            # ══ v1.14 P1-3：重连成功也写新结构，buf 重置
            socks_dict[port] = {"sock": s, "buf": bytearray(), "last_recv_ts": now, "last_send_ts": now}
            return s
        log(f"TCP: {SERVER}:{port} 重连失败，3s 重试...")
        time.sleep(3)


def run():
    global _last_good_music, _lyric_pending
    # 启动前校验：必须显式设三项环境变量（全清默认值，专门 Mac 版不留硬编码）
    errs = _validate_env()
    if errs:
        print("════════════════════════════════════════════════════════════")
        print("⚠️  Mac 版已清除所有硬编码默认值，必须先设置以下环境变量：")
        for e in errs:
            print(f"  ❌  {e}")
        print("\n示例（放 ~/.zshrc 或启动脚本里）：")
        print("  export BOT_SERVER=\"你的服务器域名或IP\"")
        print("  export BOT_PC_PORTS=\"端口1,端口2\"")
        print("  export BOT_PC_KEY=\"与服务器 kook-bot pc_status AUTH 密钥\"")
        print("════════════════════════════════════════════════════════════")
        sys.exit(1)

    _load_local_config()
    _load_cache()
    log("=== Mac 状态上报 v1.16 (同步PC v6.76：P1-2 量纲修复 最小二乘+rate clamp+pred clamp 根治drift超前2~4s + v6.75：_LYRIC_SYNC_LOG默认False + 翻译优先+0.5超高权重 + P0-1无stderr反压 + P0-2全局常驻线程池 + P1-2进度滑窗滤波 + P1-3 TCP粘包buf+心跳30s + P1-5阶梯提交+最优评分 + 补位单句try不静默死+OUTER异常栈+每5句PROGRESS+step20ms+finally第4道无锁清残留+tick*3兜底 + POST_T5诊断/空行return/异常栈打印/cur_idx>0即补位 + 每句真实[gap_ms]N/LRC_gap + 严格按进度夹逼 + QQ封面独家 + _parse_lrc逐行强校验+_sanitize_timeline二次净化+_current_lyric_idx单句防炸 — 根治读歌词即死卡P0) ===")
    log(f"目标服务器: {SERVER}  端口: {PORTS}")
    log(f"本地配置文件: {_LOCAL_CFG_PATH}")
    log(f"  - 歌词 ms 偏移: {_LYRIC_OFFSET_MS}ms (正=延后 负=提前, CMD:OFFSET_ADD/SET/RESET/GET 在线调整)")
    log(f"本地缓存: {_LOCAL_CACHE_PATH} (LRU {_CACHE_LRU_LIMIT} 首)")
    log(f"  - 当前缓存: 歌词 {len(_CACHE['lyrics'])} 首 + 封面 {len(_CACHE['covers'])} 首")
    log(f"媒体检测: AppleScript (Music.app + Spotify) 双引擎 + LRCLIB 聚合词库 + 网易云兜底歌词 / 网易云+QQ+酷狗兜底封面")
    log(f"  - 歌词: 本地缓存优先 → LRCLIB(精准+模糊×3重试/4s) + 网易云(8秒兜底) → 8槽并发 + 首命中12s上限 + 失败5s后重搜 + 下载完成立即补位")
    log(f"  - 封面: 本地缓存优先 → 网易云/QQ/酷狗 三平台并行搜 URL (500×500)")
    log(f"  - 状态提示: 播放/暂停切换即时上报 ▶继续 / ⏸暂停")
    log(f"  - 媒体采样: osascript 2.5s 超时保护，tick 80ms 轮询 + 每句精确 Timer 双保险")

    threading.Thread(target=_lyric_tick_loop, daemon=True).start()

    socks = connect_tcp()
    ip = socket.getaddrinfo(SERVER, PORTS[0], socket.AF_INET, socket.SOCK_STREAM)[0][4][0]

    while True:
        try:
            app_name, win_title = get_front_window()
            player = detect_music_player(_MEDIA_STATE.get("source", ""))

            music = {}
            with _state_lock:
                if _MEDIA_STATE.get("song"):
                    eff_ms = _MEDIA_STATE["progress_ms"]
                    if _MEDIA_STATE.get("playing") and _last_media_ts > 0:
                        eff_ms += int((time.time() - _last_media_ts) * 1000)
                    if _LYRIC_OFFSET_MS:
                        eff_ms_offset = eff_ms + _LYRIC_OFFSET_MS
                        eff_ms_for_music = eff_ms_offset if eff_ms_offset >= 0 else 0
                    else:
                        eff_ms_for_music = eff_ms
                    music = {
                        "song": _MEDIA_STATE["song"],
                        "cover": _MEDIA_STATE["cover"],
                        "duration": _MEDIA_STATE["duration_str"],
                        "progress_ms": eff_ms_for_music,
                        "raw_progress_ms": eff_ms,
                        "lyric_offset_ms": _LYRIC_OFFSET_MS,
                        "playing": _MEDIA_STATE["playing"],
                        "hasSong": _MEDIA_STATE["hasSong"],
                        "lyric_line": _MEDIA_STATE.get("lyric_line", ""),
                        "player": player,
                    }
                    # ══ v1.21 P0 自愈（同步 PC v6.81）：消费条件兼容「_lyric_pending=True 但槽为空串」错位状态
                    #    正常路径 (A)：pending=True 且槽非空 → 读槽 → 发给服务端 → 消费 → 补槽
                    #    错位自愈 (B)：pending=True 但槽是空串 → 只清 pending → 让补槽逻辑从队首填回
                    cur_slot_event = _MEDIA_STATE.get("lyric_event") or ""
                    if _lyric_pending and cur_slot_event:
                        # 路径 A：正常消费
                        music["lyric_event"] = cur_slot_event
                        _lyric_pending = False
                    elif _lyric_pending and not cur_slot_event:
                        # 路径 B：错位自愈，不发 lyric_event，只清 pending 让补槽逻辑填回队首
                        try:
                            log(f"[LYRIC_PROFILE] LYRIC_PENDING_MISMATCH_HEAL → pending=True but slot_empty, "
                                f"queue_len={len(_lyric_event_queue)} → reset pending=False then refill from queue")
                        except Exception:
                            pass
                        _lyric_pending = False
                    # ── 补槽：pending 已消费或错位自愈后，若队列仍有待发歌词则立即填回槽并重置 pending
                    if not _lyric_pending:
                        try:
                            if _lyric_event_queue:
                                q_line, q_event = _lyric_event_queue.popleft()
                                with _state_lock:
                                    _MEDIA_STATE["lyric_line"] = q_line
                                    _MEDIA_STATE["lyric_event"] = q_event
                                _lyric_pending = True
                        except Exception as _qe:
                            try:
                                log(f"[LYRIC_PROFILE] LYRIC_EVENT_QUEUE_POP_FAIL msg={_qe!r}")
                            except Exception:
                                pass

            if music.get("song"):
                _last_good_music = music.copy()
                _last_good_music.pop("lyric_event", None)
            elif _last_good_music:
                music = _last_good_music.copy()

            data = {"hostname": socket.gethostname()}
            if win_title: data["window"] = win_title
            if app_name: data["app"] = app_name
            if music: data["music"] = music

            if not hasattr(run, '_dbg_ts'):
                run._dbg_ts = 0  # type: ignore
            if time.time() - run._dbg_ts > 30:  # type: ignore
                log(f"载荷音乐: song={music.get('song','')[:30]} lyric_event={bool(music.get('lyric_event'))} player={player} cover={bool(music.get('cover'))}")
                run._dbg_ts = time.time()  # type: ignore

            sys_info = _get_system_info_mac()
            data.update(sys_info)

            payload = json.dumps(data, ensure_ascii=False) + "\n"
            payload_bytes = payload.encode("utf-8")
            # ══ v1.14 P1-3 应用层心跳：now-last_recv>30s 或 now-last_send>30s → 发 HEARTBEAT
            now_main = time.time()
            for port in list(socks.keys()):
                entry = socks.get(port)
                if not entry:
                    continue
                s = entry.get("sock")
                if s is None:
                    continue
                last_recv = entry.get("last_recv_ts", 0.0)
                last_send = entry.get("last_send_ts", 0.0)
                need_heartbeat = False
                hb_bytes = None
                if (now_main - last_recv) > 30 or (now_main - last_send) > 30:
                    need_heartbeat = True
                    hb_bytes = f"HEARTBEAT {int(now_main)}\n".encode("utf-8")
                try:
                    s.settimeout(5.0)
                    s.sendall(payload_bytes)
                    entry["last_send_ts"] = time.time()
                    if need_heartbeat and hb_bytes:
                        try:
                            s.sendall(hb_bytes)
                            entry["last_send_ts"] = time.time()
                        except Exception:
                            pass
                except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                    log(f"TCP: {SERVER}:{port} 发送断开，重连...")
                    _reconnect_port(port, ip, socks)

            # ══ v1.14 P1-3 TCP粘包处理：recv -> buf 追加 -> 按 b"\n" 拆完整行
            for port, entry in list(socks.items()):
                s = entry.get("sock") if isinstance(entry, dict) else None
                if s is None:
                    continue
                buf = entry.get("buf")
                if buf is None:
                    buf = bytearray()
                    entry["buf"] = buf
                try:
                    s.settimeout(0.1)
                    raw_bytes = s.recv(4096)
                    if raw_bytes:
                        buf.extend(raw_bytes)
                        entry["last_recv_ts"] = time.time()
                        # 循环按 b"\n" 拆分出完整行
                        while True:
                            nl_idx = buf.find(b"\n")
                            if nl_idx < 0:
                                break
                            line_bytes = bytes(buf[:nl_idx])
                            del buf[:nl_idx + 1]
                            if not line_bytes:
                                continue
                            try:
                                cmd = line_bytes.decode(errors='replace').strip()
                            except Exception:
                                continue
                            if not cmd:
                                continue
                            if cmd == "CMD:SHOT":
                                shot_b64 = _take_screenshot()
                                try:
                                    if shot_b64:
                                        s.sendall(f"SHOT:{len(shot_b64)}\n".encode("utf-8") + shot_b64.encode("utf-8"))
                                    else:
                                        s.sendall(b"SHOT:0\n")
                                    entry["last_send_ts"] = time.time()
                                except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                                    _reconnect_port(port, ip, socks)
                                    break
                            elif cmd.startswith("CMD:OFFSET_") or cmd.startswith("CMD:OFFSET "):
                                rest = cmd[len("CMD:OFFSET"):]
                                rest = rest.lstrip(" _")
                                parts = rest.split(None, 1)
                                sub = parts[0].upper() if parts else "GET"
                                arg = (parts[1] if len(parts) > 1 else "").strip()
                                log_text, reply = _apply_offset_cmd(sub, arg)
                                log(log_text)
                                if reply:
                                    try:
                                        s.sendall(reply.encode("utf-8"))
                                        entry["last_send_ts"] = time.time()
                                    except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                                        _reconnect_port(port, ip, socks)
                                        break
                except socket.timeout:
                    pass
                except (ConnectionResetError, BrokenPipeError, OSError):
                    _reconnect_port(port, ip, socks)
                except Exception:
                    pass
            time.sleep(1)

        except Exception as e:
            # ══ v1.13 P0-1：严禁 traceback.print_exc() 同步写 stderr
            log(f"ERR: {e}")
            try:
                log(traceback.format_exc())
            except Exception:
                pass
            time.sleep(1)


if __name__ == "__main__":
    run()
# This Code by Trusler & Trae CN IDE
