"""
PC 状态上报 v7.00 — 纯 Windows 原生检测（SMTC + Win32 API）
完全脱离 Now Playing Service / WebSocket 音乐追踪
依赖: pip install pywin32 psutil requests
可选: pip install pynvml (NVIDIA GPU), wmi (电压), Pillow (截屏)

══ v7.00 P0 根治「SMTC 上报频率不一致导致歌词一卡一卡」——本地单调时钟驱动进度系统 ══
  架构：切歌/seek/暂停恢复/倍速变化 4 类事件时，仅锚定一次 SMTC 位置 + time.perf_counter() 墙钟
        之后 100% 由本地 perf_counter 单调积分 × play_rate 驱动 eff_ms 匀速增长，
        offset 直接叠加在最终 eff_ms 上（正=延后 负=提前），彻底摆脱 SMTC 上报频率
  细节：
    - LYRIC_TICK_MS 80ms → 10ms：主循环 10ms 细粒度推进，歌词行切换精度 ±10ms
    - 4 类事件触发 _anchor_clock(pos_ms, reason)：重置锚点 + 清空 drift_trim
    - seek 检测：SMTC.pos vs 本地理想值差 > 3000ms → 重锚（拖动进度条秒响应）
    - 倍速支持：读取 SMTC PlaybackRate，0.5x/2x 任意倍速进度严格对齐
    - 每 30s drift 检查：|SMTC.pos - 本地理想值| > 200ms → 设置 drift_target 渐进校正
      _apply_drift_step() 每轮 10ms tick ±50ms 向 target 推进（最多 5000ms/s 校正速度）
      保证长时间播放不累积漂移，校正过程无突跳不卡
    - 统一入口：tick / TIMER / FORCE / POST_T5 / TCP 上报 6 处进度计算
      **唯一使用 get_local_eff_ms()**，offset/drift/play_rate 4 条路径 100% 语义统一，
      不会再出现"定时和 tick 差一个 offset"的错位
    - playing 状态切换（paused→resumed）必须重锚：暂停期间 perf_counter 仍走，但
      _CLOCK_paused=true 让 dt_effective=0，恢复时重锚避免把暂停时长算进进度


══ v6.80 P0 双根修（解决「卡一下→慢→慢慢对齐→又卡」周期性卡慢 + TIMER 被DROP等tick追 导致卡）══
  P0-1 进度滤波相同y值不入窗（根治周期性卡慢循环，完美对应你描述）
  - 根因：Spotify SMTC 仅 1s 更新一次 progress，主循环 80ms 一轮，1s 内 ~12 轮 pos_ms_raw 相同
    → 旧逻辑 5 点滑窗全是相同 y → 最小二乘拟合斜率≈0 → clamp 到 900 → pred 比真实慢 10%
    → fused = 0.6×pred(慢)+0.4×raw(停) → eff_ms 卡死原地 → tick 算 idx 不推进 → 歌词"卡一下"
    → SMTC 1s 后更新 → 滑窗混入新点 → rate 慢慢回到 1000 → "慢→慢慢对齐" → 下个周期又卡
  - 修复：playing 且 pos_ms_raw == 滑窗最后一条 y 且（墙上距离 < 2s）→ 不入窗污染样本
    → 滑窗里只剩真实变化的点 → 拟合 rate 精确 ≈1000 → eff_ms 平滑增长，无周期性卡慢
  - 兜底：墙上距离 ≥2s 的重复点仍强制入窗，避免窗口太旧 pred 外推过远

  P0-2 TIMER 顺序放宽容忍 3 句（diff=2/3 快速补发，不再DROP等tick）
  - 根因：旧逻辑 next_idx > last + 1 → DROP（等 tick 兜底逐句补）
    当进度滤波卡慢或 tick 抖动导致 last 停在 X，而 TIMER 精确到了 X+2 → DROP → 等 tick 80ms×N 慢慢追
    → 用户感觉"TIMER 明明到了但没词，等半天 tick 才推出来 → 卡一下"
  - 修复：diff ∈ {2,3} → 从 last+1 到 next_idx-1 每句快速补发（v6.79 歌词事件队列缓冲不覆盖）
    补发完 last == next_idx-1 → 复用原代码精确发 next_idx（含 drift 计算 + TIMER 链延续不断）
    diff ≥4 → 仍 DROP（判定 seek/切歌，防串歌乱发）
  - 日志新增：TIMER:FILL+1 / TIMER:FILL+2 标记快速补发了几句，TIMER:DROP_diff>=4 标记 seek 类丢弃

══ v6.79 P0 根修：_lyric_pending 布尔位→pending槽+缓冲队列，根治快节奏连续句覆盖丢词（按用户日志：「我好想你」「纷飞的回忆」丢句）══
  - 核心bug：主循环周期80ms，若80ms内连续写2句lyric_event（如idx=16→17间隔81ms），第1句被覆盖丢失
  - 架构：`_lyric_pending=True/False` 保留作为**快速检查位**（99%场景0额外开销），另加 `_lyric_event_queue(deque maxlen=64)` 做溢出缓冲
  - 写入端(_stage_lyric_event)：槽空→直接写；槽被占→入队缓冲；切歌时自动清空队列防串歌
  - 读取端(主循环)：消费完槽→立即从队列popleft补入槽，置pending=True让下一轮继续发，保证零丢失
  - 7处写入路径统一调用：补位循环/TIMER/切歌intro(两处)/暂停恢复/FORCE/tick
  - 兼容旧行为：_lyric_pending API 不变，主循环外部接口零改动

══ v6.78 日志合并（按用户要求：gap分析并入歌词行，不再单独分行）══
  - 4 处歌词推送路径（补位循环/TIMER/FORCE/tick）统一把 [real_gap] N | LRC_gap=Xms 追加在歌词行末尾
  - 删除所有独立的 gap_line / gap_analysis_line / gap_for_line log() 调用
  - 示例（合并前 2 行 → 合并后 1 行）：
    旧：歌词(定时) [xx] idx=4: xxx
        [1712ms] 5  | LRC_gap=1699ms
    新：歌词(定时) [xx] idx=4: xxx  | [1712ms] 5  | LRC_gap=1699ms

══ v6.77 彻底去熔断 + 请求重试提速（按用户要求：解决「Ice Paper - 心如止水」全源超时搜不到）══
  - 强制移除 P1-4 熔断机制：删_CIRCUIT_BREAK状态/3个函数/快速路径return None/8处调用_circuit_source kwarg
    避免「3次抖动超时→5min跳过」导致15min搜不到必然存在的歌
  - 降重试 5→2 次：单源最多尝试3次，避免5次3.5s软超时叠加T5=18s总超时直接无命中
  - 重试退避改常量 1.0s：不再 0.25/0.5/1/2/4 指数递增（原退避合计空耗 7.75s），切歌/抖动更灵敏
  - 去线程级硬超时包装：ThreadPoolExecutor的Future.cancel只能杀pending，running HTTP仍卡worker，嵌套无收益
    直接依赖 requests timeout（urllib3 底层 socket select 真生效）

══ v6.76 P1-2 致命量纲修复（根治「drift=-2000~-3800ms 歌词提前 2~4 秒 + idx=7→8 仅 82ms 连发」）══
  - 单位错配：x=wall秒 y=pos_ms → rate 量纲=ms/秒(≈1000)，v6.74~v6.75 Line1619 额外×1000 → 微秒量级
    → fused = 0.6×pred(超前1e5ms) + 0.4×实测 → poll 重置窗口前每轮累计超前 2~4s（提前 emit=歌词延迟）
  - rate clamp [900,1100]：防止暂停/切歌残点拟合出 <100 或 >2000 的乱率
  - pred clamp |pred-pos_raw|>5000ms：单轮拟合异常直接降级用 pos_raw，免进度一次性拉飞

══ v6.75 调整（按用户要求）══
  - _LYRIC_SYNC_LOG 默认 True → False：正常运行不再打印 [LYRIC_SYNC] 全量诊断行，降低日志队列压力
  - 翻译歌词优先：_score_lyric_match 新增「翻译存在且有效」+0.5 超高优先级加权，在候选集合中优先选择带翻译的版本

══ v6.74 P1 四项重大升级（SMTC事件驱动/进度滤波/TCP粘包/阶梯评分）══
  P1-1 SMTC事件驱动改造：winrt事件总线订阅SessionsChanged/PlaybackInfoChanged/MediaPropertiesChanged三事件入队列，主循环10×100ms细粒度sleep+drain队列，事件触发立刻poll进度，1s兜底全量poll读媒体属性，降频降延迟
  P1-2 进度平滑滤波：5点滑窗最小二乘拟合播放速率rate_ms_per_wallms，正常播放eff_ms=0.6×预测+0.4×实测融合；与上次预测差>3s判定seek/切歌重置窗口，根除进度跳变抖动
  P1-3 TCP粘包+死连接检测：socks结构升级为{sock,buf,last_recv_ts,last_send_ts}；recv粘包循环按\n拆分残段保留；SO_KEEPALIVE/TCP_KEEPIDLE/KEEPINTVL/KEEPCNT保活；30s空闲主动发HEARTBEAT应用层心跳
  P1-5 阶梯提交+最优匹配评分：S1(0~1.5s)仅variants[0]×(LRCLIB_precise+网易云+QQ)=3条首发；S1未命中再追加剩余variants+LRCLIB_fuzzy全部；命中窗口内MIN_WAIT_S=1.5s收集所有HIT按_score_lyric_match评分选最优（字符重合度+duration差+行数合理性+来源偏好），首HIT立刻cancel pending但running完成的仍参与评分
══ v6.73 P0+P1-4 重大稳定性整改（解决「主线程卡死/with空等/句柄泄漏/HTTP卡线程/慢源占死池」）══
  P0-1 根除主线程卡死：log()砍所有stderr同步写兜底（sys.stderr.write/traceback.print_exc全删），异常栈统一入日志队列由worker输出；队列≥80%激进丢[LYRIC_SYNC]/[LYRIC_PROFILE]两类诊断；[LOG_DROP]永远不fallback stderr写
  P0-2 全局常驻线程池：废弃with ThreadPoolExecutor（with退出shutdown(wait=True)必须等running HTTP任务结束=空等），_LYRIC_EXECUTOR全局单例常驻，命中/超时仅cancel pending不等待running任务结束（砍R1超时→R2启动11s空等）；封面搜索也复用全局池
  P0-3 COM/句柄/线程全链路兜底：DXGI factory/adapter/output的Release()全放入finally块，hr≠0或异常路径绝不泄漏；SMTC缩略图DataReader/Stream显式Dispose；所有后台线程daemon=True；新增每5min健康自检（句柄数/活跃线程数超阈值告警）
  P1-4 HTTP层深度优化：全局_HTTP_SESSION(requests.Session)复用TCP/TLS连接（握手开销↓30%+）；所有HTTP请求套双层超时（requests.timeout + 线程级强制硬超时，卡死1s内强制释放）；单源熔断（同源连续3次失败→熔断5min，窗口内直接跳过不请求）

══ v6.72 修复（解决「歌词搜索啥都炸 / T3命中后等6.5s才写T5 / 2条精准404重复 / 网易云QQ超时无日志」用户反馈）══
  - LRCLIB 精准/模糊彻底拆成 only_precise / only_fuzzy 两个独立函数：模糊**绝不读SMTC duration，绝不碰精准**，不再 2 倍重复 HTTP + 重复精准 404 日志 + 占满线程槽
  - _consume_until_ok 彻底重写：concurrent.futures.as_completed → wait step100ms 轮询，命中立刻 cancel 所有 pending+立即 return，砍 Beyond 实测的「T3_hit→T5_ok 空等 6.5s」
  - 彻底移除酷狗歌词/酷狗封面 4 函数（仅剩 LRCLIB+网易云+QQ音乐 歌词源，QQ音乐 独家封面源）
  - _run_single_lyric_fetch ANY 异常打 SINGLE_EXC fn=xxx 日志，不再静默吞；超时场景 pending/done 数全打日志，不再 6s 超时后全无声
  - banner 文案全同步实际参数：LRCLIB=1次×2s，网易云/QQ=2.5s 单请求超时，封面 QQ音乐独家单平台

══ v6.71 P0 修复（根治「读歌词就炸 → traceback刷爆 → C层WriteFile阻塞 → 死卡+^C杀不掉」）══
  - _parse_lrc 最终输出前逐行强校验：每行必须是 (可float时间>=0非NaN, 任意文本) 二元组，坏行打LRC_PARSE_BAD_LINE丢弃
  - 新增 _sanitize_timeline()：任何来源（LRCLIB/网易云/QQ/缓存）写入 _SMTC_STATE["timeline"/"trans_timeline"] 前，统一二次强校验+净化
  - _current_lyric_idx / _current_lyric 逐行解包加 try/except：坏行 continue 跳过，绝不抛 ValueError 到外层
  - gap分析清理 debug 残留：FIRST/后续统一单行 gap_line，不再孤立 log(f"{N}") 数字行
  - 缓存命中读出来也走 sanitize：老缓存可能有历史坏行
"""
import json, time, os, socket, threading, traceback, sys, concurrent.futures, queue, collections

SERVER = os.environ.get("BOT_SERVER", "01240820.xyz")     # 留空时必须通过环境变量 BOT_SERVER 注入
_default_ports = "58890,62002"                           # 默认清空，真实端口请通过环境变量 BOT_PC_PORTS 注入（逗号分隔）
PORTS = [int(p.strip()) for p in os.environ.get("BOT_PC_PORTS", _default_ports).split(",") if p.strip()]
AUTH_KEY = os.environ.get("BOT_PC_KEY", "huanmeng_pc_2026")   # 留空时必须通过环境变量 BOT_PC_KEY 注入，建议 >= 32 位随机串

# ═══════════════════════════════════════════════════════════════
# 非阻塞日志（v6.73 P0-1 根治「日志洪灾 stderr 兜底→主线程 C 层 WriteFile 阻塞=^C杀不掉」）
#   主线程/任意 thread 调 log() → put_nowait 入有界队列（永不卡）
#   独立 worker daemon 负责实际写 stdout + flush（worker 即便阻塞写终端也不影响主线程 Ctrl+C）
#   ══ v6.73 激进策略（全链路 NO stderr 同步写）：
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
    ══ v6.73：内部任何异常**永不写 stderr**，彻底根除反压卡死链路"""
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
    ══ v6.73 P0-1 非阻塞日志入口（主线程永远零阻塞，永不写 stderr）：
      - 队列≥80% 高水位：[LYRIC_SYNC] / [LYRIC_PROFILE] 两类诊断 直接丢 不入队
      - 队列满 put_nowait 失败 → 诊断置换腾位 → 失败直接丢
      - [LOG_DROP] 汇总：入队失败直接丢，**永不 fallback stderr 同步写**
      - 累计丢计数 5s 节流打汇总
    """
    global _log_drop_total, _log_drop_diag, _log_drop_last_report_ts
    ts = time.strftime("%H:%M:%S")
    # ══ v6.73 可丢性判定（两类诊断统一）：
    is_diag = (isinstance(msg, str) and (
        msg.startswith("[LYRIC_SYNC]")
        or msg.startswith("[LYRIC_PROFILE]")
    ))
    # ══ v6.73 激进丢包：队列≥80% 时，诊断类直接跳过不入队（不占队列空间，不阻塞，不计数）
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
        # ══ v6.73 P0-1：[LOG_DROP] 也只用 put_nowait 入队，**永不 fallback stderr 同步写**，哪怕丢了也不反压
        rpt = f"[LOG_DROP] 过去 5s 静默丢弃 {total} 条日志（其中诊断 {diag} 条）— 队列≥{_LOG_QUEUE_HIGH_WM}高水位已自动激进丢[LYRIC_SYNC]/[LYRIC_PROFILE]"
        try:
            ts2 = time.strftime("%H:%M:%S")
            _log_queue.put_nowait((ts2, rpt, False))
        except Exception:
            # 队列满 → [LOG_DROP] 本身也丢，绝对不写 stderr
            pass

# ── 窗口标题 ──
try:
    import win32gui, win32process, psutil
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    log("WARN: pywin32/psutil 未安装，无窗口信息")

def get_window_title():
    if not HAS_WIN32: return {}, "", ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid) if pid else None
        info = {}
        if proc:
            try:
                mem = proc.memory_info()
                info["proc_handle_count"] = proc.num_handles()
                info["proc_mem_rss"] = mem.rss
                info["proc_mem_vms"] = mem.vms
                info["proc_cpu_percent"] = round(proc.cpu_percent(interval=0), 1)
                info["proc_pid"] = pid
            except Exception:
                pass
        return info, title or "", proc.name() if proc else ""
    except Exception:
        return {}, "", ""

# ── FPS 检测 (DWM 帧计时) ──
_fps_history = []

def get_fps():
    """DXGI 帧统计 — 读取实际渲染帧率（非显示器刷新率）
    ══ v6.73 P0-3：factory/adapter/output 三对象 Release() 统一进 finally
       任何 hr!=0 / 异常 / 中途 return 都必执行，根除 7×24 句柄泄漏"""
    factory = None
    adapter = None
    output = None
    _Release_f = _Release_a = _Release_o = None
    try:
        import ctypes
        from ctypes import wintypes, byref, sizeof, POINTER, c_void_p
        dxgi = ctypes.windll.dxgi

        class DXGI_FRAME_STATISTICS(ctypes.Structure):
            _fields_ = [
                ("PresentCount", ctypes.c_uint),
                ("PresentRefreshCount", ctypes.c_uint),
                ("SyncRefreshCount", ctypes.c_uint),
                ("SyncQPCTime", ctypes.c_longlong),
                ("SyncGPUTime", ctypes.c_longlong),
            ]
        class DXGI_RATIONAL(ctypes.Structure):
            _fields_ = [("Numerator", ctypes.c_uint), ("Denominator", ctypes.c_uint)]
        class DXGI_MODE_DESC(ctypes.Structure):
            _fields_ = [
                ("Width", ctypes.c_uint), ("Height", ctypes.c_uint),
                ("RefreshRate", DXGI_RATIONAL),
                ("Format", ctypes.c_uint),
                ("ScanlineOrdering", ctypes.c_uint),
                ("Scaling", ctypes.c_uint),
            ]
        factory = c_void_p()
        hr = dxgi.CreateDXGIFactory(
            ctypes.c_char_p(b"\x7b\x71\x66\x3c\xb0\x60\x4f\x70\xb7\xd7\x05\x7a\xb0\x4e\x85\xee"),
            byref(factory)
        )
        if hr != 0 or not factory:
            return 0
        vtable_f = ctypes.cast(factory, POINTER(POINTER(c_void_p))).contents
        _Release_f = ctypes.cast(vtable_f[2], ctypes.CFUNCTYPE(ctypes.c_ulong, c_void_p))

        adapter = c_void_p()
        _EnumAdapters = ctypes.cast(vtable_f[7], ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, ctypes.c_uint, POINTER(c_void_p)))
        hr = _EnumAdapters(factory, 0, byref(adapter))
        if hr != 0 or not adapter:
            return 0
        vtable_a = ctypes.cast(adapter, POINTER(POINTER(c_void_p))).contents
        _Release_a = ctypes.cast(vtable_a[2], ctypes.CFUNCTYPE(ctypes.c_ulong, c_void_p))

        output = c_void_p()
        _EnumOutputs = ctypes.cast(vtable_a[7], ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, ctypes.c_uint, POINTER(c_void_p)))
        hr = _EnumOutputs(adapter, 0, byref(output))
        if hr != 0 or not output:
            return 0
        vtable_o = ctypes.cast(output, POINTER(POINTER(c_void_p))).contents
        _Release_o = ctypes.cast(vtable_o[2], ctypes.CFUNCTYPE(ctypes.c_ulong, c_void_p))

        _GetFrameStatistics = ctypes.cast(vtable_o[16], ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(DXGI_FRAME_STATISTICS)))
        stats = DXGI_FRAME_STATISTICS()
        hr = _GetFrameStatistics(output, byref(stats))
        if hr != 0 or stats.SyncRefreshCount == 0:
            return 0
        global _fps_history
        now = time.time()
        _fps_history.append((now, stats.SyncRefreshCount))
        if len(_fps_history) > 5:
            _fps_history = _fps_history[-5:]
        if len(_fps_history) >= 2:
            t0, f0 = _fps_history[0]
            t1, f1 = _fps_history[-1]
            dt = t1 - t0
            if dt > 0.5 and f1 > f0:
                fps = (f1 - f0) / dt
                return round(fps)
        return 0
    except Exception:
        return 0
    finally:
        # ══ v6.73 P0-3：释放顺序严格 output → adapter → factory（子对象先释）
        #    任何对象/Release指针存在则调用，hr≠0/异常/中途return 全部兜底
        if output and _Release_o:
            try: _Release_o(output)
            except Exception: pass
        if adapter and _Release_a:
            try: _Release_a(adapter)
            except Exception: pass
        if factory and _Release_f:
            try: _Release_f(factory)
            except Exception: pass

# ══ v7.07 P0 B：DXGI FPS 探测移出上报线程 ══
#   get_fps() 内部是 ctypes COM(DXGI) 调用。在 GTX 960 等老驱动上该原生调用**可能阻塞**：
#   旧版它每次主循环同步跑在任何执行上报的线程里，一旦卡住那个线程 → 上报循环停摆、
#   主线程若恰在同一线程则 ^C 也失效（整进程观感"卡死"）。现在改为独立 daemon worker
#   后台每 ~1s 刷一次快照，上报/主线程只读 _fps_snapshot，DXGI 卡死只影响 FPS 这一个指标，
#   绝不拖垮采集与歌词上报。这属于"非阻塞采样"的稳定性加固，与 P0 banner 的 DXGI 承诺一致。
_fps_snapshot = 0               # 最近一次 FPS 快照（上报线程只读，不碰 COM）
_fps_worker_started = False     # 幂等标记

def _fps_worker_loop():
    """后台 daemon：在自己的线程里调 get_fps()（DXGI），刷新 _fps_snapshot。"""
    global _fps_snapshot
    while True:
        try:
            _fps_snapshot = get_fps()
        except Exception:
            pass
        time.sleep(1.0)

def _start_fps_worker():
    """幂等启动 FPS 探针线程。"""
    global _fps_worker_started
    if _fps_worker_started:
        return
    _fps_worker_started = True
    try:
        threading.Thread(target=_fps_worker_loop, name="fps_probe", daemon=True).start()
    except Exception:
        pass

def _read_fps_snapshot() -> int:
    """上报线程读取 FPS 快照（不触发任何 COM/DXGI 调用）。"""
    return _fps_snapshot

# ── GPU (NVIDIA) ──
try:
    import pynvml
    pynvml.nvmlInit()
    _GPU_HANDLE = pynvml.nvmlDeviceGetHandleByIndex(0)
    _GPU_NAME = pynvml.nvmlDeviceGetName(_GPU_HANDLE)
    if isinstance(_GPU_NAME, bytes):
        _GPU_NAME = _GPU_NAME.decode("utf-8", errors="replace")
    HAS_GPU = True
    log(f"GPU: {_GPU_NAME}")
except Exception:
    HAS_GPU = False
    _GPU_NAME = ""

# ── 电压 (WMI, 需要 OpenHardwareMonitor / LibreHardwareMonitor) ──
try:
    import wmi
    _WMI_VOLT = None
    for _ns in [r"root\OpenHardwareMonitor", r"root\LibreHardwareMonitor"]:
        try:
            _WMI_VOLT = wmi.WMI(namespace=_ns)
            log(f"电压监控: 启用 ({_ns})")
            break
        except Exception:
            pass
    if not _WMI_VOLT:
        log("WARN: OpenHardwareMonitor/LibreHardwareMonitor 未运行")
    HAS_VOLT = _WMI_VOLT is not None
except ImportError:
    HAS_VOLT = False
    _WMI_VOLT = None
    log("WARN: wmi 模块未安装，无电压信息")


# ═══════════════════════════════════════════════════════════════
# v6.73 P0-3 健康自检（每 5 分钟：句柄数 / 活跃线程数 / 日志队列水位 超阈值告警）
# ═══════════════════════════════════════════════════════════════
_HEALTH_CHECK_INTERVAL_S = 300.0   # 5 min
_HEALTH_HANDLE_WARN = 3000         # 进程句柄数 > 3000 告警
_HEALTH_THREAD_WARN = 50           # 活跃线程数 > 50 告警
_last_health_check_ts = 0.0
_health_lock = threading.Lock()

def _run_health_check(force: bool = False):
    """v6.73 P0-3：定期自检句柄/线程/队列水位，超出阈值打 WARN。force=True 忽略节流立刻执行"""
    global _last_health_check_ts
    now = time.time()
    with _health_lock:
        if (not force) and (now - _last_health_check_ts < _HEALTH_CHECK_INTERVAL_S):
            return
        _last_health_check_ts = now
    try:
        # 1) 活跃线程数（使用 threading.enumerate，不依赖 psutil 也能算）
        try:
            threads = threading.enumerate()
            thread_count = len(threads)
            daemon_count = sum(1 for t in threads if getattr(t, "daemon", False))
            alive_count = sum(1 for t in threads if getattr(t, "is_alive", lambda: False)())
            if thread_count > _HEALTH_THREAD_WARN:
                log(f"[HEALTH_WARN] 活跃线程数={thread_count}(alive={alive_count},daemon={daemon_count}) 超过阈值 {_HEALTH_THREAD_WARN}")
                # 打出前 10 个非 daemon/非主线程名便于定位泄漏
                names = [getattr(t, "name", "?") for t in threads if not getattr(t, "daemon", False)]
                if names:
                    log(f"[HEALTH_WARN] 非daemon线程名样本(≤10): {names[:10]}")
        except Exception:
            thread_count = 0
            daemon_count = 0

        # 2) 进程句柄数（Windows 下 psutil 有 num_handles；无 psutil 则跳过）
        handle_count = 0
        try:
            if HAS_WIN32 and "psutil" in sys.modules:
                import psutil as _ps
                me = _ps.Process()
                if hasattr(me, "num_handles"):
                    handle_count = int(me.num_handles() or 0)
                    if handle_count > _HEALTH_HANDLE_WARN:
                        log(f"[HEALTH_WARN] 进程句柄数={handle_count} 超过阈值 {_HEALTH_HANDLE_WARN} — 疑似 COM/SMTC/句柄泄漏")
        except Exception:
            handle_count = 0

        # 3) 日志队列水位（高水位时提示消费压力）
        try:
            qsize = int(_log_queue.qsize())
            pct = int(100 * qsize / _LOG_QUEUE_MAX) if _LOG_QUEUE_MAX else 0
            if pct >= 60:
                log(f"[HEALTH_WARN] 日志队列水位={qsize}/{_LOG_QUEUE_MAX} ({pct}%) — 终端消费慢，已自动激进丢诊断保核心")
        except Exception:
            pct = 0
            qsize = 0

        # 4) INFO 摘要（只在 force=True 或有任一项超标时才打完整）
        if force or handle_count > _HEALTH_HANDLE_WARN or thread_count > _HEALTH_THREAD_WARN or pct >= 60:
            log(f"[HEALTH_INFO] 句柄={handle_count} 线程={thread_count}(daemon={daemon_count}) 日志队列={qsize}/{_LOG_QUEUE_MAX}({pct}%)")
    except Exception:
        # 自检本身异常也绝不能崩主流程，且不打 traceback stderr
        pass


def _get_gpu_info():
    if not HAS_GPU:
        return None
    try:
        util = pynvml.nvmlDeviceGetUtilizationRates(_GPU_HANDLE)
        mem = pynvml.nvmlDeviceGetMemoryInfo(_GPU_HANDLE)
        temp = pynvml.nvmlDeviceGetTemperature(_GPU_HANDLE, pynvml.NVML_TEMPERATURE_GPU)
        return {
            "name": _GPU_NAME,
            "gpu_percent": util.gpu,
            "mem_total": mem.total,
            "mem_used": mem.used,
            "mem_percent": round(mem.used / mem.total * 100, 1) if mem.total else 0,
            "temp": temp,
        }
    except Exception:
        return None


_WMI_VOLT_LOCAL = threading.local()   # 每线程一份 WMI 连接（WMI COM 对象线程亲和，跨线程复用会失败）

_LAST_VOLT_ERR_TS = 0.0                # 电压错误去抖：避免 worker 每 2s 刷屏


def _get_vvolt_conn():
    """在**当前线程**按需建立 WMI 电压连接并缓存到线程本地。
    模块导入时在主线程建的 _WMI_VOLT 不能跨线程使用（COM 线程亲和），
    worker/上报线程必须各自现建，否则报 'winmgmts:root/OpenHardwareMonitor.Sensor' 失败。"""
    try:
        conn = getattr(_WMI_VOLT_LOCAL, "conn", None)
        if conn is None:
            import wmi as _wmi
            conn = None
            for _ns in [r"root\OpenHardwareMonitor", r"root\LibreHardwareMonitor"]:
                try:
                    conn = _wmi.WMI(namespace=_ns)
                except Exception:
                    conn = None
                if conn is not None:
                    break
            if conn is not None:
                _WMI_VOLT_LOCAL.conn = conn
        return conn
    except Exception:
        return None


def _get_voltages():
    """从 WMI 读取所有电压传感器。v7.07：改用当前线程的 WMI 连接（线程本地缓存）。"""
    global _LAST_VOLT_ERR_TS
    volts_conn = _get_vvolt_conn()
    if not volts_conn:
        return None
    try:
        result = {}
        for sensor in volts_conn.Sensor(SensorType="Voltage"):
            name = sensor.Name
            val = sensor.Value
            if val is None or val == 0:
                continue
            name_lower = name.lower()
            if "vcore" in name_lower and "gpu" not in name_lower:
                result["cpu_vcore"] = round(val, 3)
            elif "gpu" in name_lower and ("vcore" in name_lower or "mv" in name_lower):
                result["gpu_vcore"] = round(val, 3)
            elif "dram" in name_lower or "vdimm" in name_lower:
                result["dram"] = round(val, 3)
            elif "3vcc" in name_lower or "+3.3v" in name_lower or name_lower == "3.3v":
                result["v33"] = round(val, 3)
            elif "5vcc" in name_lower or "+5v" in name_lower or name_lower == "5v":
                result["v5"] = round(val, 3)
            elif "12v" in name_lower:
                result["v12"] = round(val, 3)
            elif "vsb" in name_lower or "standby" in name_lower:
                result["vsb"] = round(val, 3)
            elif "vbat" in name_lower or "cmos" in name_lower:
                result["vbat"] = round(val, 3)
            else:
                key = name[:20].replace(" ", "_").replace(".", "").replace("+", "").lower()
                if key and key not in result:
                    result[key] = round(val, 3)
        return result if result else None
    except Exception as e:
        # v7.07 去抖：worker 每 2s 采一次，失败时最多 30s 报一条，避免刷屏
        global _LAST_VOLT_ERR_TS
        try:
            now_e = time.time()
            if now_e - _LAST_VOLT_ERR_TS > 30:
                _LAST_VOLT_ERR_TS = now_e
                log(f"电压采集错误: {e}")
        except Exception:
            pass
        return None


def _fmt_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _get_disk_info():
    if not HAS_WIN32:
        return []
    disks = []
    try:
        for part in psutil.disk_partitions(all=False):
            if part.opts.startswith("ro"):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "drive": part.mountpoint,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": round(usage.percent, 1),
                })
            except Exception:
                pass
    except Exception:
        pass
    return disks


_last_net = None
_last_net_ts = 0

def _get_net_speed():
    global _last_net, _last_net_ts
    if not HAS_WIN32:
        return {"upload": 0, "download": 0}
    try:
        net = psutil.net_io_counters()
        now = time.time()
        if _last_net is None or now == _last_net_ts:
            _last_net = net
            _last_net_ts = now
            return {"upload": 0, "download": 0}
        dt = now - _last_net_ts
        up = (net.bytes_sent - _last_net.bytes_sent) / dt
        down = (net.bytes_recv - _last_net.bytes_recv) / dt
        _last_net = net
        _last_net_ts = now
        return {"upload": round(up), "download": round(down)}
    except Exception:
        return {"upload": 0, "download": 0}


def _get_battery():
    if not HAS_WIN32:
        return None
    try:
        bat = psutil.sensors_battery()
        if bat is None:
            return None
        return {"percent": bat.percent, "plugged": bat.power_plugged}
    except Exception:
        return None


def _get_system_info():
    """采集系统资源信息"""
    info = {}
    if not HAS_WIN32:
        return info
    try:
        info["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        info["cpu_count"] = psutil.cpu_count(logical=True)
        freq = psutil.cpu_freq()
        if freq:
            info["cpu_freq"] = round(freq.current / 1000, 2)
        vm = psutil.virtual_memory()
        info["memory"] = {
            "total": vm.total,
            "used": vm.used,
            "available": vm.available,
            "percent": round(vm.percent, 1),
        }
        sm = psutil.swap_memory()
        info["swap"] = {
            "total": sm.total,
            "used": sm.used,
            "percent": round(sm.percent, 1),
        }
        info["boot_time"] = psutil.boot_time()
        info["uptime"] = int(time.time() - psutil.boot_time())
        info["disks"] = _get_disk_info()
        info["net"] = _get_net_speed()
        bat = _get_battery()
        if bat:
            info["battery"] = bat
        info["proc_count"] = len(psutil.pids())
    except Exception as e:
        log(f"系统信息采集错误: {e}")
    gpu = _get_gpu_info()
    if gpu:
        info["gpu"] = gpu
    volt = _get_voltages()
    if volt:
        info["voltages"] = volt
    return info


# ══ v7.07 P0 C-2：系统采集(WMI 电压/GPU/磁盘/网速)移出上报线程 ══
#   _get_system_info() 里的 WMI 查询(OpenHardwareMonitor 的 _WMI_VOLT.Sensor)在 provider 无响应时
#   可能阻塞数秒到更久。旧版它每主循环同步跑在上报线程 → 上报循环**静默**卡住（不持 GIL，所以
#   kugou/歌词线程照常，但上报与 30s 调试日志停摆）→ 表现"卡住但没报错、没有下一步"。
#   现在独立 daemon worker 后台每 ~2s 刷一份快照，上报线程只读 _sys_snapshot，WMI 慢/卡只影响
#   电压/GPU 这些指标本身，绝不拖垮上报与歌词。
_sys_snapshot = {}            # 最近一份系统信息快照（上报线程只读）
_sys_snapshot_lock = threading.Lock()
_sys_worker_started = False

def _sys_snapshot_worker_loop():
    """后台 daemon：每 ~2s 采集一次系统信息（含 WMI），刷 _sys_snapshot。
    v7.07：先在本线程 CoInitialize，且 _get_voltages 已改线程本地 WMI 连接。"""
    _coinit_current_thread()
    global _sys_snapshot
    while True:
        try:
            snap = _get_system_info() or {}
            with _sys_snapshot_lock:
                _sys_snapshot = snap
        except Exception:
            pass
        time.sleep(2.0)

def _start_sys_snapshot_worker():
    """幂等启动系统采集 worker。"""
    global _sys_worker_started
    if _sys_worker_started:
        return
    _sys_worker_started = True
    try:
        threading.Thread(target=_sys_snapshot_worker_loop, name="sys_probe", daemon=True).start()
    except Exception:
        pass

def _read_sys_snapshot():
    """上报线程读系统快照（不触发任何 WMI/psutil 采集，绝不阻塞）。"""
    with _sys_snapshot_lock:
        return _sys_snapshot


# ── 音乐播放器进程检测 ──
_MUSIC_PLAYERS = {
    "spotify": "Spotify", "cloudmusic": "网易云音乐",
    "qqmusic": "QQ音乐", "kugou": "酷狗音乐",
    "foobar2000": "foobar2000", "music.ui": "酷狗音乐",
    "kwmusic": "酷我音乐", "netease": "网易云音乐",
}
_last_player = ""
_last_player_ts = 0

def detect_music_player():
    global _last_player, _last_player_ts
    now = time.time()
    if now - _last_player_ts < 5 and _last_player:
        return _last_player
    _last_player_ts = now
    if not HAS_WIN32: return _last_player
    try:
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            for key, label in _MUSIC_PLAYERS.items():
                if key in name:
                    _last_player = label
                    return label
    except Exception:
        pass
    return _last_player


# ════════════════════════════════════════════════════════════
#  SMTC 原生检测（Windows.Media.Control）— 完全脱离 Now Playing Service
# ════════════════════════════════════════════════════════════

_SMTC_STATE = {
    "song": "",
    "artist": "",
    "title": "",
    "cover": "",            # data:image/jpeg;base64,...  or URL
    "duration_str": "",     # "m:ss"
    "duration_sec": 0,      # 歌曲时长（秒，LRCLIB 精确命中用）
    "progress_ms": 0,
    "playing": False,
    "hasSong": False,
    "lyric_line": "",       # 当前格式化歌词
    "lyric_event": "",      # 变化时发出（含时间戳）
    "timeline": [],         # LRC 时间轴 [(秒, 文本), ...]
    "trans_timeline": [],   # 翻译 LRC
}
_state_lock = threading.RLock()  # v6.79：Lock→RLock（可重入），允许同一线程嵌套加锁（如_stage_lyric_event被with _state_lock:内的调用方调用）

# ══ v6.74 P1-1 SMTC事件驱动改造 ══
_smtc_event_queue: "queue.Queue[str]" = queue.Queue(maxsize=64)
_smtc_events_subscribed = False

# ══ v7.06 SMTC worker 线程 ══
#   根因修复：酷狗等播放器的 SMTC winrt RPC（get_sessions/get_playback_info/try_get_media_properties_async）
#   **可能永久阻塞**（OS 原生层，即使 ^C 也无法打断）。旧版 tick_lyric / 主循环直接同步调用 poll_smtc，
#   一旦 RPC 卡死 → 整进程死锁（只剩独立线程 KUGOU_PROBE 还活着）。
#   现在：所有 SMTC 调用收敛到唯一 worker 线程 _smtc_poll_worker；
#         tick_lyric / 主循环只读 _SMTC_STATE 快照 + 用 _smtc_poll_wake 唤醒 worker，绝不直接碰 winrt。
#         worker 单次 poll 卡死只影响该线程，主循环 / tick / KUGOU 全部不受影响。
_smtc_poll_worker = None                 # SMTC 唯一轮询线程
_smtc_poll_wake = threading.Event()      # 唤醒信号：tick/主循环 dequeue 到事件时 set，worker 立即 poll
_smtc_poll_idle_s = 0.5                  # 无事件时 worker 最小轮询间隔（秒）
_smtc_poll_dead = False                  # 最近一次 poll 是否超时/异常（诊断用）

# ══ v6.74 P1-2 进度平滑滤波 ══
_PROGRESS_WINDOW_MAX = 5
_progress_window: "list[tuple[float, int]]" = []
_progress_window_lock = threading.Lock()
_last_predicted_pos_ms: int | None = None

_smtc_loop = None          # winrt 需要的独立 asyncio event loop
_smtc_mgr = None           # SMTCManager 缓存
_last_smtc_ts = 0.0        # 最后一次 SMTC 轮询时间戳（用于本地推算进度）
_last_song_key = ""        # "artist - title" 用于切歌检测
_last_lyric_raw = ""       # 去重（仅文本）
_last_trans_raw = ""
_last_lyric_idx = -1       # 去重（按 timeline 索引，解决副歌重复句误跳过问题）
_last_trans_idx = -1
_lyric_pending = False     # 有新歌词需上报（v6.79：仅作"槽位非空"快速检查位，真正的事件槽是_SMTC_STATE["lyric_event"]，溢出进_lyric_event_queue）
_lyric_event_queue: collections.deque = collections.deque(maxlen=64)  # v6.79：80ms内连续写≥2句时的缓冲队列（最多64句，防爆内存），主循环消费完槽自动从队首补
_last_emit_wall_ts_ms = 0.0  # ══ v6.67 分析：上一句歌词 emit 的墙上时间戳(ms)，0=切歌/暂停后首句 ══
_LYRIC_OFFSET_MS = 0       # 全局歌词毫秒偏移（正=延后 负=提前），持久化
_LYRIC_OFFSET_BASE = 1000  # v7.13: 用户实测 +1000ms 才准，以此作为新的"0"基准（默认/RESET 落点）

# ═══════════════════════════════════════════════════════════════
# v7.04 酷狗适配：UI Automation 真实播放进度源
#   酷狗 SMTC 恒报 0 → 本地时钟永远锚在 0，中途启动/切歌/长播全部从歌曲开头算词。
#   用 Windows UI Automation 直接读酷狗"进度条" RangeValue（单位厘秒 ×10=毫秒），
#   由独立后台线程 ~1s 刷新一次快照，poll_smtc 只消费这份廉价快照覆盖 SMTC pos。
#   依赖（可选）：pip install uiautomation
# ═══════════════════════════════════════════════════════════════
_KUGOU_PROG = {"valid": False, "pos_ms": 0, "dur_ms": 0, "ts": 0.0}
_KUGOU_PROG_LOCK = threading.Lock()
_KUGOU_PROBE_THREAD = None
_KUGOU_PROBE_START_LOCK = threading.Lock()
_KUGOU_TITLE_THREAD = None          # v7.19: 标题（歌名锚点）独立读线程
# ══ v7.19 切歌防卡：酷狗 UIA Control 是线程亲和对象（在 kugou_probe 线程 CoInitialize 创建）。
#    SMTC worker 切歌锚定时若直接跨线程调 _kugou_read_now() 里的 GetRangeValuePattern()，
#    歌曲结束/切歌瞬间酷狗重建进度条窗口，该 COM 调用会**永久阻塞** → SMTC worker 冻结
#    → 切歌检测/歌词链路全部停摆（用户报"切歌一点东西也没有"+ Ctrl+C 卡死）。
#    解法：切歌只需请求 kugou_probe 线程立刻多探一轮，SMTC worker 只读新鲜快照，绝不跨线程碰 UIA。══
_KUGOU_REFRESH_NOW = threading.Event()   # 置位 → 探针线程立即多跑一轮 _kugou_probe_once()
_KEY_ADJ_THREAD = None         # 方向键微调线程（v7.06）
_KUGOU_HWND = 0
_KUGOU_SLIDER = None          # 缓存的进度条 Control
_KUGOU_SLIDER_FAILS = 0
_KUGOU_RAW_V = -1.0           # 上次 RangeValue 原始值（单位判定用）
_KUGOU_RAW_TS = 0.0
_KUGOU_UNIT = None            # 10=厘秒(默认); 1=毫秒
_KUGOU_PROBE_DISABLED = False
# ══ v7.12: 酷狗窗口标题 → 真实歌名（SMTC title 常不更新，用它兜底切歌）══
_KUGOU_TITLE = ""             # 最近读到的酷狗窗口标题
_KUGOU_TITLE_TS = 0.0         # 最近标题时间戳（新鲜度判断）
# 滚动标题去抖：新歌名需**连续稳定读两次**才确认（避免滚动截断瞬时噪声误判切歌）
_KUGOU_TITLE_DB = {"key": "", "n": 0}
_KUGOU_TITLE_LOG_TS = 0.0     # 周期性标题状态日志节流（v7.16，便于观察锚点是否持续在读）
_KUGOU_TITLE_FAIL_TS = 0.0    # 标题读取失败日志节流（v7.16，读不到时明报，便于定位）
_KUGOU_TITLE_RAW_PREV = ""    # 探针上次解析出的歌名（v7.17，歌名变化立即打普通日志定位切歌）


def _kugou_find_hwnds() -> list:
    """枚举所有可见的 kugou/kgmusic 顶层窗口，返回 hwnd 列表。
    ⚠ v7.05 关键修复：酷狗常有两个窗口 —— 一个空的壳窗口(class=kugou_ui title='')，
       真正的进度条在"_歌名_酷狗音乐标题"的子窗口/另一个顶层窗口里。
       旧版只取第一个窗口导致找不到滑块，真实进度永远读不到。这里必须全部收集再逐个找滑块。"""
    try:
        import win32gui as _wg, win32process as _wp, psutil as _ps
    except Exception:
        return []
    target = ("kugou", "kgmusic", "kglow")
    hwnds = []
    def _cb(hwnd, _acc):
        try:
            if not _wg.IsWindowVisible(hwnd):
                return True
            _, pid = _wp.GetWindowThreadProcessId(hwnd)
            p = _ps.Process(pid)
            name = (p.name() or "").lower()
        except Exception:
            return True
        if any(t in name for t in target) or "kgmusic" in (_wg.GetClassName(hwnd) or "").lower():
            hwnds.append(hwnd)
        return True
    try:
        _wg.EnumWindows(_cb, None)
    except Exception:
        return []
    return hwnds


def _kugou_slider_in_hwnd(hwnd):
    """在单个窗口 UI 树里找"进度条" SliderControl（Name 含 进度/播放/时间 才算命中）。"""
    try:
        import uiautomation as _auto
        root = _auto.ControlFromHandle(hwnd)
    except Exception:
        return None
    found = [None]
    want = ("进度", "播放", "时间")
    class _Stop(Exception):
        pass
    def _walk(ctrl, depth):
        if depth > 30 or ctrl is None:
            return
        try:
            ct = (ctrl.ControlTypeName or "") or ""
            name = (ctrl.Name or "").strip() or ""
        except Exception:
            return
        if ct and ("slider" in ct.lower() or "progress" in ct.lower()) and any(w in name for w in want):
            found[0] = ctrl
            raise _Stop()
        try:
            child = ctrl.GetFirstChildControl()
            n = 0
            while child is not None and n < 2500:
                _walk(child, depth + 1)
                try:
                    child = child.GetNextSiblingControl()
                except Exception:
                    break
                n += 1
        except Exception:
            pass
    try:
        _walk(root, 0)
    except _Stop:
        pass
    except Exception:
        pass
    return found[0]


# 酷狗常见进度条/轨道条控件类（Win32 原生 Trackbar 毫秒级可定位，无需 UIA 全树遍历）
_KR_TRACKBAR_CLASSES = ("msctls_trackbar32", "TrackBar20W", "UIProgressBar", "ComctlSlider")


def _uia_apply_fast():
    """降低 uiautomation 全局搜索超时/等待，避免慢查询（默认 10s）拖住首次遍历。"""
    try:
        import uiautomation as _auto
        try:
            _auto.SetGlobalSearchTimeout(0.8)
        except Exception:
            pass
    except Exception:
        pass


def _kugou_find_slider_fast():
    """毫秒级快路径：对每个酷狗顶层窗口用 EnumChildWindows 直接找**标准 Trackbar 类**子窗口，
    命中即转成 uiautomation Control 返回 (hwnd, slider)。避免整棵 UIA 控件树逐节点属性 RPC（这才是首遍历十几秒的根因）。
    找不到返回 (0, None)，由调用方回退 UIA 全树遍历。"""
    try:
        import win32gui as _wg
        import uiautomation as _auto
    except Exception:
        return 0, None
    hwnds = _kugou_find_hwnds()
    found = []
    for top in hwnds:
        found.clear()
        def _cb(hwnd, acc):
            try:
                _wg.IsChild(top, hwnd)  # 确保是子窗口
            except Exception:
                return True
            try:
                cls = (_wg.GetClassName(hwnd) or "") or ""
            except Exception:
                return True
            if any(c in cls for c in _KR_TRACKBAR_CLASSES):
                try:
                    ctrl = _auto.ControlFromHandle(hwnd)
                    if ctrl is not None:
                        acc.append((hwnd, ctrl))
                except Exception:
                    pass
            return True
        try:
            _wg.EnumChildWindows(top, _cb, found)
        except Exception:
            pass
        if found:
            return found[0]
    return 0, None


def _kugou_find_slider():
    """遍历所有 kugou 窗口，返回 (hwnd, slider)；任一窗口有进度条即返回。
    v7.09：先走毫秒级 Trackbar 快路径，命中即返回；未命中再回退 UIA 全树遍历。"""
    try:
        hwnd, slider = _kugou_find_slider_fast()
        if slider is not None:
            return hwnd, slider
    except Exception:
        pass
    hwnds = _kugou_find_hwnds()
    for hwnd in hwnds:
        try:
            slider = _kugou_slider_in_hwnd(hwnd)
        except Exception:
            continue
        if slider is not None:
            return hwnd, slider
    return 0, None


def _reset_kugou_prog():
    with _KUGOU_PROG_LOCK:
        _KUGOU_PROG["valid"] = False


def _kugou_probe_once():
    """读一次酷狗进度条真实位置/时长，写快照。失败/无酷狗→快照 invalid。
    v7.05：改遍历所有酷狗窗口找滑块（修复只找第一个空壳窗口导致读不到真实进度）。"""
    global _KUGOU_HWND, _KUGOU_SLIDER, _KUGOU_SLIDER_FAILS, \
           _KUGOU_RAW_V, _KUGOU_RAW_TS, _KUGOU_UNIT, _KUGOU_PROBE_DISABLED
    try:
        if _KUGOU_PROBE_DISABLED:
            return
        try:
            import uiautomation
        except Exception:
            _KUGOU_PROBE_DISABLED = True
            _reset_kugou_prog()
            return
        # 拿到有效 (hwnd, slider)：优先用缓存，缓存失效则重找
        hwnd = _KUGOU_HWND
        slider = _KUGOU_SLIDER
        if slider is None:
            hwnd, slider = _kugou_find_slider()
            if slider is None and hwnd == 0 and not _kugou_find_hwnds():
                # 完全没有任何酷狗窗口 → 静默失能，快照 invalid
                _reset_kugou_prog()
                return
            _KUGOU_HWND = hwnd
            _KUGOU_SLIDER = slider
        if slider is None:
            _KUGOU_SLIDER_FAILS += 1
            if _KUGOU_SLIDER_FAILS >= 6:
                try:
                    log(f"[KUGOU_PROBE] 连续{_KUGOU_SLIDER_FAILS}次找不到进度条，强制重找窗口")
                except Exception:
                    pass
                _KUGOU_HWND = 0
                _KUGOU_SLIDER = None
                _KUGOU_SLIDER_FAILS = 0
            _reset_kugou_prog()
            return
        _KUGOU_SLIDER_FAILS = 0
        rv = slider.GetRangeValuePattern()
        v = float(rv.Value)
        m = float(rv.Maximum)
        if m <= 0 or v < 0 or v > m:
            _reset_kugou_prog()
            return
        now = time.monotonic()
        # 单位判定：隔 ≥0.5s 对比 RangeValue 增量，≈100/s=厘秒，≈1000/s=毫秒
        if _KUGOU_RAW_V >= 0 and (now - _KUGOU_RAW_TS) >= 0.5:
            dt = now - _KUGOU_RAW_TS
            dv = v - _KUGOU_RAW_V
            ups = (dv / dt) if dt > 0 else 0
            if _KUGOU_UNIT is None:
                _KUGOU_UNIT = 10 if abs(ups - 100) < 150 else 1
                try:
                    log(f"[KUGOU_PROBE] 进度单位判定 hwnd=0x{hwnd:X} unit={_KUGOU_UNIT} (rate={ups:.1f} unit/s)")
                except Exception:
                    pass
        _KUGOU_RAW_V = v
        _KUGOU_RAW_TS = now
        unit = _KUGOU_UNIT if _KUGOU_UNIT else 10
        pos_ms = max(0, int(v * unit))
        dur_ms = max(0, int(m * unit))
        was_valid = _KUGOU_PROG.get("valid", False)
        with _KUGOU_PROG_LOCK:
            _KUGOU_PROG["valid"] = True
            _KUGOU_PROG["pos_ms"] = pos_ms
            _KUGOU_PROG["dur_ms"] = dur_ms
            _KUGOU_PROG["ts"] = time.time()
        # ══ v7.15: 标题读取已移到 _kugou_probe_loop 每轮无条件执行（不依赖进度是否成功）══
        # 首次成功/首次读到时打一次日志，便于确认真正锚定的位置（不再报 0）
        if not was_valid:
            try:
                log(f"[KUGOU_PROBE] 获取真实进度 hwnd=0x{hwnd:X} pos_ms={pos_ms}(={pos_ms/1000:.1f}s/{dur_ms/1000:.1f}s) → 将覆盖 SMTC 的 0")
            except Exception:
                pass
    except Exception:
        try:
            _reset_kugou_prog()
        except Exception:
            pass


def _kugou_read_window_title(hwnd: int) -> str:
    """读酷狗窗口标题（准确锚点=完整 Win32 caption，**不随显示滚动**，稳定）。
    实测：酷狗有 2 个窗口——空壳(0x31E1E title='') + 真窗(0x31E20 title='歌手 - 歌名 - 酷狗音乐')。
    先从传入 hwnd 拿祖先，标题为空则枚举所有酷狗窗口取含"酷狗"的那个。失败返回空串。"""
    import ctypes

    def _gt(h):
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.windll.user32.GetWindowTextW(h, buf, 512)
            return buf.value[:200] if n and n > 0 else ""
        except Exception:
            return ""

    try:
        hroot = ctypes.windll.user32.GetAncestor(hwnd, 2)   # GA_ROOT → 顶层
        t = _gt(hroot)
        if t and "酷狗" in t:
            return t
    except Exception:
        pass
    try:
        for h in _kugou_find_hwnds():
            t = _gt(h)
            if t and "酷狗" in t:
                return t
    except Exception:
        pass
    return ""


def _kugou_title_song():
    """从最近酷狗窗口标题解析 (artist, title)。
    酷狗常规标题形如「Maroon 5、Wiz Khalifa - Payphone－酷狗音乐」→ 歌手在前、歌名在后。
    标题不新鲜/无法解析 → ("","")。只返回能稳定识别出的歌名。"""
    global _KUGOU_TITLE, _KUGOU_TITLE_TS
    try:
        with _KUGOU_PROG_LOCK:
            raw = _KUGOU_TITLE
            ts = _KUGOU_TITLE_TS
        if not raw or (time.time() - ts) >= 6.0:
            return "", ""
        t = raw.replace("酷狗音乐", "").replace("KuGou", "").replace("kuGou", "")
        t = t.strip(" \u2014\u2502|\u00b7\uff1a:\uff0d\t\r\n")
        # 将各种分隔（全半角横线/竖线）统一成 " - " 再切分
        t = t.replace("\uff0d", " - ").replace("\u2014", " - ").replace("\u2502", " - ").replace("|", " - ")
        parts = [p.strip(" \uff0d-\u2014\u2502|") for p in t.split(" - ") if p and p.strip(" -\uff0d\u2014\u2502|")]
        parts = [p for p in parts if p]
        if not parts:
            return "", ""
        # 酷狗：歌手在「 - 」前，歌名在后；多歌手用「、」连接保留原样
        artist = parts[0] if len(parts) >= 1 else ""
        title = parts[1] if len(parts) >= 2 else ""
        title = title.strip()
        if not title:
            return "", ""
        return artist, title
    except Exception:
        return "", ""


def _kugou_confirmed_title_song():
    """标题锚点确认：GetWindowText 是完整 caption、**不随显示滚动**，一次读到即稳定。
    只做"读到的歌名≠上一次记录的"这一轻判断（防切歌瞬间探针暂时读到空/旧占位），
    不再要求连续 2 次——那会导致切歌延迟/漏切。"""
    _ka, _kt = _kugou_title_song()
    if not _kt:
        return "", ""
    key = f"{_ka} - {_kt}"
    try:
        with _KUGOU_PROG_LOCK:
            prev_key = _KUGOU_TITLE_DB.get("key", "")
            _KUGOU_TITLE_DB["key"] = key   # 记录本次，供下次比较是否换歌
            if key != prev_key:
                try:
                    with _KUGOU_PROG_LOCK:
                        _raw = _KUGOU_TITLE
                    log(f"[KUGOU_TITLE] 切歌歌名: {_ka} - {_kt} (窗口标题='{str(_raw)[:80]}')")
                except Exception:
                    pass
                return _ka, _kt
    except Exception:
        pass
    return "", ""


def _coinit_current_thread():
    """在**当前线程**初始化 COM 公寓（COINIT_APARTMENTTHREADED=2）。失败静默。
    v7.07: uiautomation(kugou) 等托管 COM 在后台线程/上报线程使用时必须先 CoInitialize，
          否则报 '[WinError -2147221008] 尚未调用 CoInitialize' 和 'Can not load UIAutomationCore.dll'。
    注：一个线程只能以同一种公寓模型初始化一次；若后续仍要改线（如 part32），CoUninitialize 再重进。"""
    try:
        from ctypes import windll
        windll.ole32.CoInitializeEx(None, 2)
    except Exception:
        pass


def _kugou_probe_loop():
    """后台 ~1s 刷新一次酷狗真实进度快照（daemon）。
    v7.07: uiautomation 必须在**当前线程**先 CoInitialize 建立 COM 公寓，覆盖 kugou_probe 线程。
    v7.09: 首次即加速 uiautomation 超时 + 用毫秒级 Trackbar 快路径找滑块。
    v7.19: **标题（歌名锚点）已独立出去**到 _kugou_title_loop 单独线程 —— 只因在此线程内
           _kugou_probe_once() 的 UIA 进度读取可能阻塞（歌曲结束/切歌瞬间酷狗重建进度条窗口），
           若标题读取排在此之后会被一并拖住 → 切歌检测失效。现在本线程只负责进度快照。"""
    _coinit_current_thread()
    _uia_apply_fast()
    while True:
        try:
            _kugou_probe_once()
        except Exception:
            try:
                _reset_kugou_prog()
            except Exception:
                pass
        # ══ v7.19: 等 ~1s，但允许被 _KUGOU_REFRESH_NOW 打断提前多探一轮（SMTC 切歌锚定用）══
        _KUGOU_REFRESH_NOW.clear()
        _KUGOU_REFRESH_NOW.wait(1.0)


def _kugou_title_loop():
    """v7.19: 独立线程专门读酷狗窗口标题（歌名锚点，切歌检测唯一信号源）。
    用纯 Win32 GetWindowText/EnumWindows（非 UIA），永不阻塞、与进度探针彻底隔离——
    进度读 UIA 卡住也不影响这块，保证切歌永远能感知新歌。"""
    _coinit_current_thread()
    while True:
        global _KUGOU_TITLE, _KUGOU_TITLE_TS, _KUGOU_TITLE_FAIL_TS, _KUGOU_TITLE_RAW_PREV
        try:
            # ══ v7.18: **无条件**调用标题读取（内部对 hwnd 无效时自动枚举酷狗窗口兜底），
            #    不再依赖 _KUGOU_HWND / slider 是否有效——否则切歌瞬间酷狗重绘进度条导致
            #    _KUGOU_HWND 被 reset 成 0，标题读取整个停掉，_KUGOU_TITLE 停在旧歌 → 切歌检测失效。
            _tg = _kugou_read_window_title(_KUGOU_HWND)
            if _tg:
                with _KUGOU_PROG_LOCK:
                    _KUGOU_TITLE = _tg
                    _KUGOU_TITLE_TS = time.time()
            elif time.time() - _KUGOU_TITLE_FAIL_TS >= 20.0:
                _KUGOU_TITLE_FAIL_TS = time.time()
                log(f"[KUGOU_TITLE] WARN 读不到酷狗窗口标题 hwnd=0x{_KUGOU_HWND or 0:X}（切歌检测将失效）")
            # ══ v7.17: 歌名一旦变化立即打普通日志（与 [KUGOU_PROBE] 同级，不被诊断高水位丢弃），
            #    用于判断探针到底有没有捕获新歌——是"没读到新标题"还是"读到却没触发切歌"。══
            _la_t, _lt_t = _kugou_title_song()
            _nk = (f"{_la_t} - {_lt_t}").strip(" -") if (_la_t or _lt_t) else ""
            if _nk and _nk != _KUGOU_TITLE_RAW_PREV:
                _old_p = _KUGOU_TITLE_RAW_PREV or "(起点)"
                _KUGOU_TITLE_RAW_PREV = _nk
                try:
                    log(f"[KUGOU_TITLE] 歌名变更: {_old_p} -> {_nk}")
                except Exception:
                    pass
                # ══ v7.21: 标题线程检测到歌名变化 → 驱动切歌（不依赖 SMTC 判定）══
                # ══ v7.22: 切歌改丢后台线程执行——切歌内部 _await_kugou_pos 最长阻塞 15s，
                #    若在标题线程同步跑，会把标题读取线程卡死 → 换下一首时检测不到（"切歌没响应"）。
                try:
                    threading.Thread(target=_kugou_only_song_change, name="kugou_cut", daemon=True).start()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1.0)


def _kugou_ensure_probe():
    """确保后台探测线程已启动（幂等，可被每 tick 调用）。"""
    global _KUGOU_PROBE_THREAD, _KUGOU_TITLE_THREAD
    if _KUGOU_PROBE_THREAD is not None:
        # v7.19: 进度线程在跑即代表启动逻辑已完成（标题线程在首次启动时一并拉起）
        return
    with _KUGOU_PROBE_START_LOCK:
        if _KUGOU_PROBE_THREAD is not None:
            return
        t = threading.Thread(target=_kugou_probe_loop, name="kugou_probe", daemon=True)
        _KUGOU_PROBE_THREAD = t
        t.start()
        # v7.19: 标题（歌名锚点）读线程独立启动，与进度探针彻底隔离，切歌检测永不受进度 UIA 阻塞影响
        try:
            if _KUGOU_TITLE_THREAD is None:
                tt = threading.Thread(target=_kugou_title_loop, name="kugou_title", daemon=True)
                _KUGOU_TITLE_THREAD = tt
                tt.start()
        except Exception:
            pass


def _kugou_read_progress_ms():
    """廉价读快照（每 tick 调用）。有效且新鲜(<5s)返回 pos_ms，否则 None。"""
    _kugou_ensure_probe()
    try:
        with _KUGOU_PROG_LOCK:
            if _KUGOU_PROG["valid"] and (time.time() - _KUGOU_PROG["ts"]) < 5.0:
                return _KUGOU_PROG["pos_ms"]
    except Exception:
        pass
    return None


def _kugou_read_now():
    """切歌/锚定前**请求探针线程立刻多探一轮**并读最新快照（SAME-THREAD 防卡）。
    ══ v7.19 根修：旧版在此线程直接调 UIA GetRangeValuePattern()——这是跨线程使用
    在 kugou_probe 线程创建的 COM Control，歌曲结束/切歌瞬间酷狗重建进度条窗口时该调用
    永久阻塞，把调用方（SMTC worker）冻死 → 切歌检测/歌词全部停摆。
    现在只置 _KUGOU_REFRESH_NOW 让探针线程立刻多探一轮，本函数只轮询新鲜快照（纯 lock 读，
    绝不碰 UIA/COM），因此**永不可能阻塞**。返回 (pos_ms, dur_ms)；超时读不到返回 None。"""
    _kugou_ensure_probe()
    # 请求探针立刻多探一轮（最多等 ~1.2s，探针在 wait(1.0) 处被唤醒即多跑一轮）
    try:
        _KUGOU_REFRESH_NOW.set()
    except Exception:
        pass
    deadline = time.time() + 1.5
    while time.time() < deadline:
        try:
            with _KUGOU_PROG_LOCK:
                if _KUGOU_PROG["valid"] and (time.time() - _KUGOU_PROG["ts"]) < 1.0:
                    return (int(_KUGOU_PROG["pos_ms"]), int(_KUGOU_PROG["dur_ms"]))
        except Exception:
            pass
        time.sleep(0.03)
    return None


# 切歌/启动首轮：SMTC 恒报 0（酷狗）时，最多等这么久拿真实进度再锚定，避免从头播词。
# 冷启动首次 UIA 遍历实测可达 ~11s（酷狗窗口控件树遍历慢），故等待须覆盖它，否则首锚回 0；
# 已热探针下切歌用 _kugou_read_now() 同步直读，毫秒级返回，不进这个等待。
_SG_INIT_KUGOU_WAIT_S = 15.0


def _await_kugou_pos(timeout_s: float):
    """切歌/启动首轮锚定前调用：先**主动同步直读**一次（热状态瞬间拿到真实进度），
    拿不到才回退等后台线程周期快照（覆盖冷启动首次遍历场景）。
    返回 int(pos_ms)；超时或一直拿不到有效值返回 None。
    调用方务必在 **SMTC worker** 线程内调用（本函数短暂阻塞是可接受的切歌开销）。"""
    _kugou_ensure_probe()
    r = _kugou_read_now()
    if r is not None and r[0] > 0:
        return int(r[0])
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        p = _kugou_read_progress_ms()
        if p is not None and p > 0:
            return int(p)
        time.sleep(0.05)
    return None


def _kugou_duration_ms():
    """廉价读快照里的真实时长（毫秒），无效返回 None。"""
    try:
        with _KUGOU_PROG_LOCK:
            if _KUGOU_PROG["valid"] and (time.time() - _KUGOU_PROG["ts"]) < 5.0:
                return _KUGOU_PROG["dur_ms"]
    except Exception:
        pass
    return None


def _stage_lyric_event(line: str, event: str):
    """v6.79 P0 统一的歌词事件写入入口（7处写入全走这里）：
    - 若 pending 槽空闲 → 直接写入 _SMTC_STATE + 置 pending=True（99%场景，0额外开销）
    - 若 pending 槽被占（上一句还没被主循环80ms轮读取走） → 入队列缓冲，保证零丢失
    - 切歌时外部调用方会清空队列，防止旧歌串到新歌
    """
    global _lyric_pending, _lyric_event_queue
    if not _lyric_pending:
        # 正常场景：槽空，直接写
        with _state_lock:
            _SMTC_STATE["lyric_line"] = line
            _SMTC_STATE["lyric_event"] = event
        _lyric_pending = True
        return
    # 溢出场景：槽被占（<80ms内连续写第2+句） → 入队
    try:
        _lyric_event_queue.append((line, event))
        if len(_lyric_event_queue) >= 60:  # 接近上限警告
            log(f"[LYRIC_PROFILE] LYRIC_EVENT_QUEUE_NEAR_FULL len={len(_lyric_event_queue)}/64 pending=True → 可能主循环卡住或突发大量补发")
    except Exception as _e:
        # 队列炸了直接丢，不影响主线程
        try:
            log(f"[LYRIC_PROFILE] LYRIC_EVENT_QUEUE_APPEND_FAIL msg={_e!r} → 丢弃: {line[:20]!r}")
        except Exception:
            pass
LYRIC_TICK_MS = 10          # ══ v7.00 本地时钟驱动：10ms 细粒度主循环，eff_ms 严格单调匀速不卡
LYRIC_SONG_INIT_MS = 300   # 切歌/初始化后等待 SMTC 的最小间隔（避免抢跑）

# ═══════════════════════════════════════════════════════════════
# v7.00 本地单调时钟进度系统（按用户方案：切歌/seek/暂停恢复 只锚定一次 SMTC，
#       之后 100% 由本地 perf_counter 积分驱动进度，offset 直接作用）
# ══ 根治「SMTC 上报频率不一致导致歌词一卡一卡」：eff_ms 不再依赖每次 poll 得到的 progress_ms
# ═══════════════════════════════════════════════════════════════
# 锚定的墙上时间戳（使用 perf_counter — 单调不回拨，NTP 校时不影响歌词进度）
_CLOCK_anchor_wall_perf: float = 0.0
# 锚定时刻的 SMTC 播放位置（毫秒）
_CLOCK_anchor_pos_ms: int = 0
# 当前播放速率（SMTC.PlaybackRate，1.0=正常 0.5=半速 2.0=双倍）
_CLOCK_play_rate: float = 1.0
# 是否已暂停（暂停期间墙上时长不计入进度）
_CLOCK_paused: bool = False
# 漂移校正累计偏移（毫秒）：每 30s 对比一次 SMTC 位置，超出 200ms 阈值时
#                       每轮 tick 渐进式拉 ±50ms，避免突跳"卡一下"
_CLOCK_drift_trim_ms: int = 0
# 漂移校正目标值（非 0 时每轮 tick 向其推进 ±50ms，归零后清零）
_CLOCK_drift_target_ms: int = 0
# 上次 drift 校正检查的 wall_ts（time.time()，每 30s 一次）
_CLOCK_last_drift_check_ts: float = 0.0
# ══ v7.02 直接强制对准：上次 force_align 的 perf 时间戳（每 ~2s 一次）
_CLOCK_force_align_ts: float = 0.0
# ══ v7.03 force_align 可信度保护：SMTC 位置若"播放中却停在0"或"与本地呈恒定偏移（连续几轮 diff 几乎不变）"→ 视为 SMTC 不可信，不再每2s反复拽回本地时钟 ═─
_CLOCK_force_align_last_diff: int | None = None  # 上一次 force_align 算出的 diff
_CLOCK_force_align_flat_count: int = 0          # diff 与上轮几乎相同的连续轮数（恒定偏移计数）
# 锚定时记录的 pos_ms（用于 seek 检测，若 SMTC 下一次 pos 与本地理想值差 >3s 判定 seek）
_CLOCK_last_anchor_snapshot_ms: int = 0
# 上次 poll_smtc 拿到的原始 pos（用于检测 SMTC 位置是否真的发生了跳变）
#   酷狗音乐等流氓播放器会 SMTC 永远上报 0ms，导致"每 3s 本地走满3000ms 触发 seek 锚回0"死循环。
#   因此 seek 判定必须额外要求：本轮 pos_ms_raw 与 上轮 pos 不同（真·跳变），否则视为 SMTC 已挂，不重锚。
_CLOCK_last_smtc_pos_raw: int = -1
# 连续 poll 轮数 pos_ms_raw 与上次完全相同（>=2 轮时 seek 判定禁用，即便 |diff|>3s 也不重锚）
_CLOCK_smtc_pos_stuck_count: int = 0
# 上次的播放状态（用于检测 paused→resumed）
_CLOCK_last_playing_state: bool = False
# 上次的播放速率（用于检测倍速变化，若变化则顺手重锚一次 pos）
_CLOCK_last_playback_rate: float = 1.0

_CLOCK_lock = threading.Lock()


def get_local_eff_ms() -> int:
    """v7.00 统一的进度计算入口 — 所有路径（tick / TIMER / force / catchup / intro）**必须唯一使用此函数**。
    进度公式：
      eff_ms = elapsed_ms_since_anchor * play_rate
             + anchor_pos_ms
             + drift_trim_ms   (渐进式漂移校正)
             + _LYRIC_OFFSET_MS (人工延迟调节，正=延后 负=提前)
    保证 4 条路径 offset / drift / play_rate 语义 100% 统一，不会再出现"定时和 tick 差一个 offset"的错位。
    使用 time.perf_counter()（单调高精度，Windows 下 100ns 级，NTP 不回拨）。
    """
    with _CLOCK_lock:
        now_perf = time.perf_counter()
        if _CLOCK_anchor_wall_perf <= 0:
            # 未锚定过（启动前几秒，或 SMTC 会话为空）→ 兜底返回 0，等下次锚定
            return max(0, int(_LYRIC_OFFSET_MS or 0))
        # 1) 墙上时间差（秒）
        dt_wall_s = max(0.0, now_perf - _CLOCK_anchor_wall_perf)
        if _CLOCK_paused:
            dt_effective_s = 0.0
        else:
            dt_effective_s = dt_wall_s * float(_CLOCK_play_rate if _CLOCK_play_rate else 1.0)
        eff = int(dt_effective_s * 1000) \
              + int(_CLOCK_anchor_pos_ms or 0) \
              + int(_CLOCK_drift_trim_ms or 0) \
              + int(_LYRIC_OFFSET_MS or 0)
        return max(0, eff)


def _anchor_clock(pos_ms: int, *, reason: str = "") -> None:
    """v7.00 统一的时钟重锚入口 — 切歌/seek/暂停恢复/倍速变化 所有路径都必须走这里，
    绝不能直接改 _CLOCK_* 变量以免某处漏写状态不一致。
    每次重锚都会清空 drift_trim（新锚点本身就是正确基准，旧累计校正已无意义）。"""
    global _CLOCK_anchor_wall_perf, _CLOCK_anchor_pos_ms, \
           _CLOCK_drift_trim_ms, _CLOCK_drift_target_ms, \
           _CLOCK_last_anchor_snapshot_ms
    with _CLOCK_lock:
        pos_i = max(0, int(pos_ms or 0))
        _CLOCK_anchor_wall_perf = time.perf_counter()
        _CLOCK_anchor_pos_ms = pos_i
        _CLOCK_last_anchor_snapshot_ms = pos_i
        # 新锚点 → drift 校正全部清零（重新开始积累误差）
        _CLOCK_drift_trim_ms = 0
        _CLOCK_drift_target_ms = 0
    if reason:
        try:
            log(f"[LOCAL_CLOCK] ANCHOR reason={reason!r} pos_ms={pos_i} rate={_CLOCK_play_rate:.2f} paused={_CLOCK_paused}")
        except Exception:
            pass


def _apply_drift_step() -> None:
    """v7.00 每轮 tick 末尾调用一次 — 渐进式推进漂移校正（每轮最多 ±50ms），
    避免校正时歌词"跳一下卡一下"。_CLOCK_drift_target_ms 为 0 直接 return。"""
    global _CLOCK_drift_trim_ms, _CLOCK_drift_target_ms
    with _CLOCK_lock:
        if not _CLOCK_drift_target_ms:
            return
        target = int(_CLOCK_drift_target_ms)
    step = max(-50, min(50, target))  # 每轮最多 ±50ms（10ms tick → 5000ms/s 校正速度足够快）
    remaining_after = target - step
    if remaining_after == 0 or (abs(remaining_after) < 10):
        # 剩 <10ms 直接一步到位 + 清 target
        with _CLOCK_lock:
            _CLOCK_drift_trim_ms += int(target)
            _CLOCK_drift_target_ms = 0
    else:
        with _CLOCK_lock:
            _CLOCK_drift_trim_ms += int(step)
            _CLOCK_drift_target_ms = int(remaining_after)


def _clock_force_align() -> None:
    """v7.02 直接强制对准本地时钟 ≈ SMTC 进度。
    用户选择"直接强制对准"（而非温和漂移/不校准）：
      - 每 ~2s 用当前 SMTC pos 重新核对本地积分；
      - |偏差| >= 120ms 时直接把锚点暴力对准 SMTC（消除"播放到一半对不上"的线性漂移）；
      - 酷狗等 SMTC 恒报 0（pos 卡死）→ 跳过，避免被拖回 0；
      - |偏差| > 3s → 疑似 seek/切歌，交给 ②-3 seek / 切歌锚定逻辑，不在此暴力拽。"""
    global _CLOCK_force_align_ts, _CLOCK_force_align_last_diff, _CLOCK_force_align_flat_count
    try:
        if not _SMTC_STATE or not _SMTC_STATE.get("song", ""):
            _CLOCK_force_align_ts = 0.0
            _CLOCK_force_align_last_diff = None
            _CLOCK_force_align_flat_count = 0
            return
        tnow = time.perf_counter()
        if _CLOCK_force_align_ts and (tnow - _CLOCK_force_align_ts) < 2.0:
            return
        _CLOCK_force_align_ts = tnow
        with _CLOCK_lock:
            stuck = _CLOCK_smtc_pos_stuck_count
        if stuck >= 2:
            return  # SMTC pos 卡死（酷狗恒报0）→ 不可信，依赖本地积分
        eff = get_local_eff_ms()
        smtc = _SMTC_STATE.get("progress_ms", 0)
        local_wo_ofs = eff - int(_LYRIC_OFFSET_MS or 0)
        diff = smtc - local_wo_ofs   # +:本地落后 smtc更快；-:本地超前
        if smtc <= 0:
            # v7.03：SMTC 播放中却报 pos=0 → 不可信（酷狗恒报0类），绝不把本地时钟拽回0，
            #       否则每 ~2s 清 0 → 歌词永远停在第 3 句。直接依赖本地积分即可。
            return
        # v7.03：恒定偏移（切歌/缓冲期 SMTC 滞后）≠ 真正漂移。
        #       若连续几轮 diff 几乎不变（同一首歌内不增长）→ SMTC 相对本地是固定滞后/偏移，
        #       反复重锚只是"拽回再走满"死循环；真正的线性漂移会随播放时长递增，能被下面的同类判断区分。
        if _CLOCK_force_align_last_diff is not None and abs(diff - _CLOCK_force_align_last_diff) < 150:
            _CLOCK_force_align_flat_count += 1
            if _CLOCK_force_align_flat_count >= 2:
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_PROFILE] FORCE_ALIGN_SKIP 恒定偏移 {diff}ms 连续>{_CLOCK_force_align_flat_count} 轮不增长 → 判为 SMTC 滞后/偏移，停止拽回本地时钟")
                _CLOCK_force_align_last_diff = diff
                return
        else:
            _CLOCK_force_align_flat_count = 0
        _CLOCK_force_align_last_diff = diff
        if abs(diff) < 120:
            return  # 已足够准，不折腾
        if abs(diff) > 3000:
            return  # 超大偏差→疑似 seek/切歌，交给专门锚定逻辑
        _anchor_clock(int(smtc), reason=f"force_align diff={int(diff)}ms")
    except Exception:
        pass

_last_song_change_ts = 0.0 # 切歌时刻
_smtc_song_intro_emitted_at = 0.0  # v6.64: 切歌提示刚发出时间戳（poll_smtc 立刻打了就记这里，tick_lyric 防重复）
_last_playing_state = None # 播放/暂停状态切换检测
# ══ v7.02 切歌误判暂停修正 ══
_pause_suspect_since = None  # 检测到"停止播放"的时间戳（None=当前不是可疑暂停）；用于去抖确认真实暂停 vs 切歌一闪而过
_force_media_recheck = False # 置 True 时，下一轮 poll_smtc 强制重读媒体属性（Kugou 切歌 title 需重新确认）
_last_media_recheck_ts = 0.0 # v7.11: 周期性强制重读媒体属性的上次时间戳（兜底酷狗 SMTC 歌名滞后）
_PAUSE_DEBOUNCE_S = 1.0       # 停止播放持续超过该秒数才确认真实暂停并上报"已暂停"；窗口内恢复则视为切歌
_lyrics_fetched_for = ""   # 已经为哪首歌启动过歌词搜索（避免重复）
_cover_fetched_for = ""    # 已经为哪首歌启动过封面搜索

# ── 精确定时器提前调度：每发完一句立刻挂 Timer 触发下一句 ──
_next_lyric_timer = None    # threading.Timer 对象，cancel 旧的再挂新的
_next_lyric_timer_song = "" # 该 Timer 对应的 song_key，切歌时直接 cancel 重挂
_next_lyric_timer_lock = threading.Lock()

# ── 下载后补回前面已经唱过的歌词：按 0..current_idx 顺序以 250ms 间隔快速发出 ──
_catchup_thread = None            # threading.Thread
_catchup_thread_song = ""         # 该线程对应的 song_key，用于切歌时中断
_catchup_lock = threading.Lock()
_CATCHUP_INTERVAL_MS = 250        # 相邻补句的间隔（毫秒），给用户阅读时间，避免一次性刷屏
_CATCHUP_MIN_INTERVAL_MS = 60     # 下限：补 N>10 句时加速到 60ms 一句，确保唱到的句子别被截半天才出现


def _cancel_catchup(reason_song_key: str = ""):
    """切歌 / 暂停 / 手动中断：立刻取消正在执行的补句线程。注意：不 kill，只设标记 + wait 1 秒。
    ══ 修B 关键修复 ══
      - reason 带 "#paused" / "#catchup_begin" / 其他 tag 后缀：一定是外部明确要求，真 cancel
      - reason == 当前正在运行的 _catchup_thread_song（同一首歌不带 tag）：典型是 tick_lyric 的「切歌提示误 cancel 自己」，
        必须忽略，否则补位刚发 5 句就被自己 tick 中断，造成 6..N 全漏
      - reason 为空 或 为不同歌曲：真 cancel
    """
    global _catchup_thread, _catchup_thread_song
    with _catchup_lock:
        running = _catchup_thread_song
        is_tagged = ("#" in reason_song_key) if reason_song_key else False
        same_song_no_tag = (reason_song_key and running and reason_song_key == running and not is_tagged)
        if same_song_no_tag:
            # 同一首歌误 cancel → 忽略，不动 _catchup_thread_song 标记
            return
        t = _catchup_thread
        _catchup_thread_song = reason_song_key
        _catchup_thread = None
    if t and t.is_alive() and threading.current_thread() is not t:
        try:
            t.join(timeout=1.0)
        except Exception:
            pass


def _catchup_lyrics_until(song_key: str, target_idx: int):
    """歌词下载完成后补发：索引 0 → target_idx（含）全部按 timeline 顺序发出，每句间隔自适应 60~250ms。
    发送过程中切歌 / 暂停 / 歌变了 → 立刻中断。
    因为 lyric_event 走 TCP 每次都会经过 _last_lyric 去重服务器端，所以我们在客户端也要逐句改 _last_lyric_raw，
    保证服务端看到的 send_text 都是「新的」不会被跳。"""
    global _catchup_thread, _catchup_thread_song
    if target_idx is None or target_idx < 0:
        return
    # 取当前 timeline / trans_timeline 快照
    with _state_lock:
        if _SMTC_STATE.get("song") != song_key:
            return
        timeline = list(_SMTC_STATE.get("timeline", []) or [])
        trans_timeline = list(_SMTC_STATE.get("trans_timeline", []) or [])
    if not timeline:
        return
    n = min(target_idx + 1, len(timeline))
    if n <= 0:
        return
    # 句子数量多的话缩短间隔避免刷屏太久：10句内 250ms，20句内 150ms，>20句 60ms（上限约 1.2 秒）
    if n <= 10:
        interval_ms = _CATCHUP_INTERVAL_MS
    elif n <= 20:
        interval_ms = 150
    else:
        interval_ms = _CATCHUP_MIN_INTERVAL_MS
    total_expected_ms = max(0, n - 1) * interval_ms

    # ══ 修B：启动补位前，先把之前挂的所有定时器 + _last_lyric_idx 都重置干净，
    #    防止旧 Timer 跟补位线程抢同一批 idx（典型：下载完成时挂了 idx=27 的 Timer，
    #    补位 i=5 时 Timer 就触发了 idx=27 → 把 _last 写成 27，补位 i=6..26 全被 drop）
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
                # ══ v6.69：单句 try/except —— 某一句 timeline[i] 格式炸（比如 LRCLIB 吐了非二元组 / NaN 时间戳）
                #    不要整段补位 thread 静默死，打 [LYRIC_PROFILE] 栈后 continue 下一句，用户能看到其他歌词。
                try:
                    # 每句开始前都检查：歌有没有变 / 补句线程是不是被要求作废 / 是否暂停
                    with _state_lock:
                        cur_song = _SMTC_STATE.get("song")
                        playing = _SMTC_STATE.get("playing", True)
                    if cur_song != song_key or _catchup_thread_song != song_key:
                        # v7.02 fix: 分开打印两个判定条件——旧日志只打 song 对比，真正的失败
                        # 往往在 _catchup_thread_song!=song_key（补位被 cancel/误清标记），
                        # 导致同串报"song已变化"的假象。
                        _song_changed = cur_song != song_key
                        _mark_invalid = _catchup_thread_song != song_key
                        log(f"歌词补位中断: song_changed={_song_changed} mark_invalid={_mark_invalid} cur_song={cur_song!r} song_key={song_key!r} catchup_mark={_catchup_thread_song!r} 已发 {i}/{n} 句")
                        return
                    if not playing:
                        # 暂停就停在当前句不再继续发（用户暂停说明不想看了）
                        log(f"歌词补位中断: 检测到暂停，已发 {i}/{n} 句")
                        return
                    try:
                        t_sec, txt = timeline[i]
                    except Exception as _ue:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_UNPACK_FAIL i={i}/{n} timeline[i]={timeline[i]!r} msg={_ue!r} → 跳过此句（不中断整段补位）")
                        continue
                    try:
                        t_sec_f = float(t_sec)
                    except Exception as _fe:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_TSEC_BAD i={i}/{n} t_sec(raw)={t_sec!r} msg={_fe!r} → 跳过此句")
                        continue
                    # 找翻译
                    trans_txt = ""
                    best_dt = None
                    for tt, ttxt in trans_timeline:
                        try:
                            dt = abs(float(tt) - t_sec_f)
                        except Exception:
                            continue
                        if best_dt is None or dt < best_dt:
                            best_dt = dt
                            trans_txt = ttxt if dt < 0.6 else ""  # 0.6s 内视为同一句翻译
                    formatted = _format_lyric_line(txt, trans_txt)
                    # 发送：写 _SMTC_STATE + 标记 pending
                    ts = time.time()
                    with _state_lock:
                        if _SMTC_STATE.get("song") != song_key or _catchup_thread_song != song_key:
                            log(f"歌词补位中断#2: 内部再次校验失败，已发 {i}/{n} 句")
                            return
                        _last_lyric_idx = i
                        _last_lyric_raw = txt
                        _last_trans_raw = trans_txt
                        _last_trans_idx = -1
                        # trans_idx 不强制（翻译行可能少），保持 -1 让后续正常对齐
                        # v6.79：统一走 _stage_lyric_event（防溢出覆盖丢句）
                        _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                    # ══ v6.67-v6.78 分析：每句真实墙上间隔 vs LRC 文件内时间戳差（v6.78 合并到歌词行末尾，不再独立 2 行）
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
                    # v6.78：gap 直接并到歌词行末尾（每句都打，取消 5 秒节流；原 gap_line 本来每句都打，总行数≈不增反减）
                    log(f"歌词补位 [{ts:.3f}] {i+1}/{n}: {txt}" + (f" | 翻译: {trans_txt}" if trans_txt else "") + f"  | {gap_line}")
                    last_log_sent_ts = ts
                    # ══ v6.69 每 5 句打一行 CATCHUP_LOOP_PROGRESS（肉眼可见补位跑到哪了，防止"卡住了其实在跑"误判）
                    if i > 0 and i % 5 == 0:
                        log(f"[LYRIC_PROFILE] CATCHUP_LOOP_PROGRESS i={i}/{n} song={song_key!r} elapsed_from_start={ts - i_ts0:.3f}s last_sent_ts_delta={ts - last_log_sent_ts:.3f}s")
                    # 等待 interval_ms（最后一句不用等）
                    if i < n - 1:
                        # 逐小段 sleep，cancel/切歌能在 20ms 内响应中断（v6.69：30ms → 20ms，响应更灵敏）
                        slept = 0
                        step = 20
                        while slept < interval_ms:
                            time.sleep(step / 1000.0)
                            slept += step
                            with _state_lock:
                                cs = _SMTC_STATE.get("song")
                                pl = _SMTC_STATE.get("playing", True)
                            if cs != song_key or _catchup_thread_song != song_key or not pl:
                                return
                except Exception as _inner_ex:
                    import traceback
                    log(f"[LYRIC_PROFILE] CATCHUP_LOOP_SENTENCE_EXCEPTION song={song_key!r} i={i}/{n} msg={_inner_ex!r} traceback={traceback.format_exc()} → 跳过，继续 i+1")
                    continue
            log(f"歌词补位完成: {song_key} 共补发 {n} 句")
            # ══ 修A 关键修复：补位结束后 绝对不能调 _force_emit_current_lyric！
            #   否则 force_emit 用 eff_ms 重新算 idx（补位期间 progress 没更新，算出来 idx 更小）→
            #   就会刚补位到 27 又写回 14（刚才测试抓到的 27→14 回退 bug 的来源）。
            #   正确姿势：只给「target_idx + 1」挂一句精确定时器 + 让 80ms tick 兜底，绝不回写 _last_lyric_idx。
            #   如果补位期间音乐又前进了 ≥1 句，tick 在补位结束后（静音取消）会立刻推进 idx=target+1 正常发出。══
            try:
                with _state_lock:
                    if _SMTC_STATE.get("song") == song_key and _SMTC_STATE.get("playing", False):
                        tl_now = _SMTC_STATE.get("timeline", []) or []
                        ttl_now = _SMTC_STATE.get("trans_timeline", []) or []
                        next_i = n               # target_idx + 1
                        if tl_now and next_i < len(tl_now):
                            cur_t_ms = tl_now[n - 1][0] * 1000 + _LYRIC_OFFSET_MS
                            next_t_ms = tl_now[next_i][0] * 1000 + _LYRIC_OFFSET_MS
                            wait = max(0, next_t_ms - cur_t_ms) - int(LYRIC_TICK_MS * 0.5)
                            _schedule_next_lyric_at(song_key, tl_now, ttl_now, next_i, wait)
            except Exception as e:
                log(f"[LYRIC_PROFILE] CATCHUP_FINISH_SCHEDULE_NEXT_FAIL msg={e!r}")
                log(f"WARN: 补位完成后挂 next 定时器异常: {e}")
        except Exception as _outer_ex:
            import traceback
            log(f"[LYRIC_PROFILE] CATCHUP_LOOP_OUTER_EXCEPTION song={song_key!r} msg={_outer_ex!r} traceback={traceback.format_exc()}")
        finally:
            # ══ v6.6 二次加固 + v6.69 加 3 层兜底（防止任何原因标记残留导致 tick 永久静音 = "假死无反应"）
            #    ④ v6.69 无锁 global 兜底：哪怕 with _catchup_lock 永远拿不到（极端死锁），也强行清 song 标记
            #    （_run 函数头已 global 声明 _catchup_thread_song，这里直接用即可）
            try:
                self_thread = threading.current_thread()
                with _catchup_lock:
                    # ① 自匹配：直接清
                    if _catchup_thread is self_thread:
                        _catchup_thread = None
                    # ② 不管是不是本线程，只要标记还是这首歌 → 必须清
                    #    （哪怕这首歌被外部 cancel 过，残留 song/song#tag 都影响下一首歌判据）
                    if _catchup_thread_song == song_key:
                        _catchup_thread_song = ""
            except Exception as _e:
                # ③ 防炸：加锁/读取抛异常也至少尽力清（不持锁强行清，最坏就是跟新补位写冲突，但 "" 比 残留 song 安全）
                try:
                    if _catchup_thread_song == song_key:
                        _catchup_thread_song = ""
                except Exception:
                    pass
            # ④ v6.69 第四道防线：无锁 global 再清一次（防止上面 ①②③ 任何一个因为锁死/异常没生效）
            try:
                if _catchup_thread_song == song_key or ("#" not in song_key and _catchup_thread_song.startswith(song_key + "#")):
                    _catchup_thread_song = ""
                    log(f"[LYRIC_PROFILE] CATCHUP_FINALLY_FORCE_CLEAR_MARK song={song_key!r} → 第四道防线清残留成功")
            except Exception as _e4:
                try:
                    # 终极 fallback：直接写空串（不问值），就算写冲突也比残留好
                    _catchup_thread_song = ""
                except Exception:
                    pass
            # ══ v6.6 + v6.69：补位结束 / 中断 后 立刻触发 tick_lyric() 兜底推进
            #    v6.69 从 1 次 → 3 次（每次间隔 15ms），防止状态同步 / 锁竞争导致"第一次 tick 没推"
            for _k in range(3):
                try:
                    time.sleep(0.015)
                    tick_lyric()
                except Exception as _e2:
                    pass
            log(f"[LYRIC_PROFILE] CATCHUP_FINALLY_END song={song_key!r} n={n} mark={_catchup_thread_song!r} → 标记清理 + 3*tick 兜底完成")


    t = threading.Thread(target=_run, daemon=True)
    # v7.02 fix: 先把旧补句线程（最多 1s）停干净，再占用 补位 标记并立即 start。
    # 旧逻辑先设 _catchup_thread_song=目标 再 join(old)：join 延时最长 1s 期间，线程对象
    # 已存在但 is_alive()=False，tick_lyric/Timer._cb 把"未 start 的线程"误判成"已退出线程"
    # 清掉标记 → 新补位线程一起跑就被 _catchup_thread_song!=song_key 中断（"song已变化" 假报）。
    with _catchup_lock:
        old = _catchup_thread
    if old and old.is_alive() and old is not t:
        try:
            old.join(timeout=1.0)
        except Exception:
            pass
    with _catchup_lock:
        _catchup_thread = t
        _catchup_thread_song = song_key
    t.start()


def _schedule_next_lyric_at(song_key: str, timeline, trans_timeline, next_idx: int, wait_ms: float):
    """在下一句应触发时刻前（含 offset）精确调用一次 _force_emit_current_lyric。
    v6.66 严格模式：wait_ms 是 float（秒）threading.Timer 精确接受 float，不再 int 四舍五入/截断；允许 0 值。"""
    global _next_lyric_timer, _next_lyric_timer_song
    if next_idx < 0 or not timeline or next_idx >= len(timeline):
        return
    # v6.66 严格模式：wait_ms 直接按传值；None/NaN 安全兜底
    if wait_ms is None:
        return
    try:
        wait_f = float(wait_ms)
        if wait_f != wait_f:  # NaN
            return
    except Exception:
        return
    # 已经错过了时间点（负值）→ 1ms 后立即触发，让回调内 burst 追进度（不让 tick 兜底导致 10ms~未知卡顿）
    if wait_f < 0:
        wait_f = 1.0
    # 极端值保护：最多等 30 分钟
    if wait_f > 30 * 60 * 1000:
        wait_f = 30 * 60 * 1000.0
    # ══ v7.01 修链不中断：<=15ms 不再 return 静默（之前依赖tick兜底，但tick在clamped_by_drift/seek后可能卡多轮或漏句）
    #    改为最小 1ms 调度 — 0 会被 threading.Timer 当0立刻fire但仍建Timer开销；统一拉到1ms（下一个调度切片立刻fire，回调内burst会追平实际进度，不晚）
    if wait_f < 1.0:
        wait_f = 1.0
    lrc_next_t_ms = timeline[next_idx][0] * 1000.0 + _LYRIC_OFFSET_MS
    if _LYRIC_SYNC_LOG:
        log(f"[LYRIC_SYNC] TIMER:SCHEDULE song={song_key!r} next_idx={next_idx}/{len(timeline)} LRC_next_t={lrc_next_t_ms:.0f}ms wait_ms_float={wait_f:.2f} Timer_s(=wait/1000)={wait_f/1000.0:.4f}")

    def _cb():
        try:
            global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_emit_wall_ts_ms
            global _catchup_thread_song, _catchup_thread
            with _state_lock:
                if _SMTC_STATE.get("song") != song_key:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_song_changed song_key={song_key!r} next_idx={next_idx} current={_SMTC_STATE.get('song')!r}")
                    return
                # v6.6 加强：Timer 只在补位线程真 alive 时才静音，且顺手清残留
                try:
                    with _catchup_lock:
                        ct = _catchup_thread
                    if ct is not None and ct.is_alive() and _catchup_thread_song == song_key:
                        if _LYRIC_SYNC_LOG:
                            log(f"[LYRIC_SYNC] TIMER:DROP_muted_catchup_alive song={song_key!r} next_idx={next_idx}")
                        return
                    if (ct is None) and _catchup_thread_song == song_key:
                        with _catchup_lock:
                            # v7.02 fix: 与 tick 同样只在 线程对象彻底为空 时才清标记，
                            # 避免 join 旧线程期间(线程未 start → is_alive=False)误清启动中的补位。
                            if _catchup_thread is None and _catchup_thread_song == song_key:
                                _catchup_thread_song = ""
                except Exception:
                    pass
                tl = _SMTC_STATE.get("timeline", [])
                ttl = _SMTC_STATE.get("trans_timeline", [])
                playing = _SMTC_STATE.get("playing", False)
                if not tl or next_idx >= len(tl) or not playing:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_skip_condition song={song_key!r} next_idx={next_idx} tl_len={len(tl)} playing={playing}")
                    return
                # 严格顺序强保证
                last = _last_lyric_idx
                if next_idx <= last:
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:DROP_next<=last song={song_key!r} next_idx={next_idx} last={last}")
                    return
                # ══ v7.01：Timer 回调不再 diff>=4 DROP，也不再手写 fill 循环。
                #   直接使用 _burst_catchup_to_idx：
                #     - 从 last+1 追到「定时器原定的 next_idx」与「当前 eff_ms 实际已到的 idx」两者较大值
                #     - 最多 200 句（防 seek 到几百句的极端情况刷屏）
                #     - 中间句不挂 Timer，只在最后一句自动挂 schedule_next
                now_eff_cb = get_local_eff_ms()
                idx_now, _ = _current_lyric_idx(tl, now_eff_cb)
                if idx_now < 0:
                    idx_now = next_idx
                target_idx_cb = max(next_idx, idx_now)
                target_idx_cb = min(target_idx_cb, len(tl) - 1)
                if _LYRIC_SYNC_LOG and (target_idx_cb > last + 1 or idx_now > next_idx):
                    log(f"[LYRIC_SYNC] TIMER:BURST_CATCHUP song={song_key!r} scheduled_next={next_idx} eff_now_idx={idx_now} last={last} → burst_target={target_idx_cb} lines={target_idx_cb-last} eff_now_ms={now_eff_cb}")
                _burst_catchup_to_idx(song_key, tl, ttl, target_idx_cb,
                                      playing=playing, eff_ms_ref=now_eff_cb,
                                      max_lines=200)
            # 发完继续挂下一句 Timer：严格按 LRC 间隔 float
            with _state_lock:
                cur_song = _SMTC_STATE.get("song")
                tl2 = _SMTC_STATE.get("timeline", [])
                ttl2 = _SMTC_STATE.get("trans_timeline", [])
                playing2 = _SMTC_STATE.get("playing", False)
            if cur_song == song_key and cur_song == _next_lyric_timer_song and playing2:
                cur_idx_now2 = _last_lyric_idx  # 直接拿 burst 后实际追到的最新 idx
                if tl2 and cur_idx_now2 + 1 < len(tl2):
                    next_t_ms_f = tl2[cur_idx_now2 + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
                    # ══ v7.01：按当前真实进度算还剩多少到下一句（不管LRC里两句之间写死的固定间隔）
                    timer_now_eff = get_local_eff_ms()
                    next_wait_f = max(0.0, (next_t_ms_f - timer_now_eff) - _LYRIC_TIMER_PREMISS_MS)
                    # ══ v7.01 修链不中断：0/负/短等待不再跳过（之前<=15ms不调度，会造成idx=39/46这类"整句消失"和末尾idx=48→49卡死4min）
                    #    最小 1ms 保证链条始终挂下一个 Timer；哪怕下一句马上就到（甚至已经过了），1ms后回调里 burst 会追平。
                    if next_wait_f < 1.0:
                        next_wait_f = 1.0
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_SYNC] TIMER:CHAIN_SCHEDULE_NEXT song={song_key!r} cur_idx={cur_idx_now2} next_idx={cur_idx_now2+1} LRC_next={next_t_ms_f:.0f}ms eff_now={timer_now_eff}ms wait_ms_float={next_wait_f:.2f}(min=1ms强制)")
                    with _next_lyric_timer_lock:
                        if _next_lyric_timer_song == song_key:
                            try:
                                t = threading.Timer(next_wait_f / 1000.0,
                                                    _schedule_next_lyric_at,
                                                    args=(song_key, tl2, ttl2, cur_idx_now2 + 1, 0.0))
                                t.daemon = True
                                t.start()
                                _next_lyric_timer = t
                            except Exception:
                                pass
        except Exception as e:
            log(f"WARN: 下一句歌词定时器回调异常: {e}")
            if _LYRIC_SYNC_LOG:
                import traceback as _tb
                log(f"[LYRIC_SYNC] TIMER:EXCEPTION_TRACEBACK {_tb.format_exc()}")

    try:
        t = threading.Timer(wait_f / 1000.0, _cb)
        t.daemon = True
    except Exception:
        return
    with _next_lyric_timer_lock:
        old = _next_lyric_timer
        try:
            if old is not None:
                old.cancel()
        except Exception:
            pass
        _next_lyric_timer = t
        _next_lyric_timer_song = song_key
    try:
        t.start()
    except Exception:
        pass


def _cancel_all_lyric_timers(reason_song_key: str = ""):
    """切歌 / 暂停 / 状态切换时取消正在等待的 Timer，防止旧歌词误触发。"""
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

# ── 本地持久化：歌词 offset 等可调参数（%APPDATA%\Huanmeng\pc_status.json） ──
import os as _os
_LOCAL_CFG_DIR = _os.path.join(_os.environ.get("APPDATA", _os.path.expanduser("~")), "Huanmeng")
_LOCAL_CFG_PATH = _os.path.join(_LOCAL_CFG_DIR, "pc_status.json")
_LOCAL_CACHE_PATH = _os.path.join(_LOCAL_CFG_DIR, "pc_status.cache.json")
_CACHE_LRU_LIMIT = 200

_cache_lock = threading.Lock()
_CACHE = {
    "lyrics": {},    # artist_title_lower → dict(source, timeline, trans_timeline, fetched_at)
    "covers": {},    # artist_title_lower → dict(source, url, fetched_at)
    "meta": {"version": 1, "limit": _CACHE_LRU_LIMIT},
}


def _cache_key(artist: str, title: str) -> str:
    return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"


def _cache_evict_if_needed():
    """LRU 驱逐：超 _CACHE_LRU_LIMIT 时按 fetched_at 删最旧的 30%"""
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
    """处理 CMD:OFFSET_*，返回 (log_text, reply_line_or_None)。arg 已经 strip() 过。"""
    global _LYRIC_OFFSET_MS
    cmd = cmd.upper()
    if cmd == "RESET":
        old = _LYRIC_OFFSET_MS
        _LYRIC_OFFSET_MS = _LYRIC_OFFSET_BASE   # v7.13: RESET 落到"新 0"基准(+1000)
        _save_local_config()
        return (f"歌词偏移重置: {old}ms → {_LYRIC_OFFSET_BASE}ms", f"OFFSET_CURRENT:{_LYRIC_OFFSET_MS}\n")
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


# ══ v7.06 方向键实时微调歌词偏移 ══
#   ← = 提前(偏移减)   → = 延后(偏移加)
#   点按一次按 _KEY_ADJ_STEP_MS 调整；长按每 _KEY_ADJ_REPEAT_MS 连续调整（快速拉大档）。
#   用 pywin32 的 GetAsyncKeyState 轮询，不阻塞主循环（独立 daemon 线程）。
_KEY_ADJ_STEP_MS = 100     # 每次单次点按/一次长按钟的调整量（毫秒）
_KEY_ADJ_REPEAT_MS = 150   # 长按连续调整的最小间隔（毫秒）
_KEY_ADJ_KEYMAP = {0x25: -_KEY_ADJ_STEP_MS, 0x27: _KEY_ADJ_STEP_MS}  # VK_LEFT / VK_RIGHT
_KEY_ADJ_STATE = {}        # vk -> [held_last, last_adj_perf]


def _adjust_offset_by(key_delta_ms: int):
    """方向键驱动：把 _LYRIC_OFFSET_MS 增加 delta，并持久化 + 打日志。"""
    global _LYRIC_OFFSET_MS
    old = _LYRIC_OFFSET_MS
    _LYRIC_OFFSET_MS += key_delta_ms
    _save_local_config()
    try:
        log(f"方向键调整歌词偏移: {old}ms → {_LYRIC_OFFSET_MS}ms ({key_delta_ms:+d}ms) OFFSET_CURRENT:{_LYRIC_OFFSET_MS}")
    except Exception:
        pass


# ══ v7.08 方向键全局键盘钩子（补充）：低级别钩子与焦点/窗口无关，
#    即使游戏等前台应用用独占/raw input 吞掉方向键，钩子链仍能捕获到按键。
#    与 GetAsyncKeyState 轮询互为冗余：钩子装不上时回退轮询。
_KEY_ADJ_HOOK_LAST = {}   # vk -> 上次调整 perf_counter
_hook_proc_cb = None
_hHook = None
_user32 = None


def _console_foreground() -> bool:
    """方向键仅在**本脚本控制台获得焦点**时才响应（不全局）。
    生效的是前台窗口 == 本进程 console 窗口。无法判定时（拿不到句柄）放行 True。"""
    try:
        import ctypes
        fg = ctypes.windll.user32.GetForegroundWindow()
        con = ctypes.windll.kernel32.GetConsoleWindow()
        if con == 0:
            return True
        return fg == con
    except Exception:
        return True


def _hook_proc(nCode, wParam, lParam):
    """低级别键盘钩子回调：观察方向键按下，驱动同样的 _adjust_offset_by()。
    返回 CallNextHookEx 传递（不吞键），避免抢走游戏里本来的方向键用途。"""
    try:
        import ctypes
        if nCode == 0:  # HC_ACTION
            kbd = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if wParam in (0x0100, 0x0104) and kbd.vkCode in (0x25, 0x27):  # WM_KEYDOWN / WM_SYSKEYDOWN
                delta = 100 if kbd.vkCode == 0x27 else -100
                now = time.perf_counter()
                if (now - _KEY_ADJ_HOOK_LAST.get(kbd.vkCode, 0.0)) * 1000.0 >= _KEY_ADJ_REPEAT_MS:
                    _KEY_ADJ_HOOK_LAST[kbd.vkCode] = now
                    _adjust_offset_by(delta)
    except Exception:
        pass
    try:
        return _user32.CallNextHookEx(_hHook, nCode, wParam, lParam)
    except Exception:
        return 1


def _install_global_key_hook():
    """安装 WH_KEYBOARD_LL 全局低级键盘钩子。成功返回 hHook，否则 None。"""
    global _hook_proc_cb, _hHook, _user32, _KBDLLHOOKSTRUCT, _HOOKPROC
    try:
        import ctypes
        from ctypes import wintypes

        class _KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        _HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HMODULE, wintypes.DWORD]
        _user32.SetWindowsHookExW.restype = ctypes.c_void_p
        _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
        _user32.CallNextHookEx.restype = ctypes.c_long
        _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        _user32.GetMessageW.restype = ctypes.c_int
        _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        _hook_proc_cb = _HOOKPROC(_hook_proc)
        hHook = _user32.SetWindowsHookExW(
            13, _hook_proc_cb, ctypes.windll.kernel32.GetModuleHandleW(None), 0)
        if not hHook:
            _hook_proc_cb = None
            return None
        _hHook = hHook
        return hHook
    except Exception:
        return None


def _pump_key_hook():
    """在钩子线程跑消息泵（阻塞），钩子回调在此线程被系统调用。"""
    import ctypes
    msg = ctypes.wintypes.MSG()
    while True:
        r = _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
        if r in (0, -1):
            return
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))


def _keyboard_adjust_loop():
    """后台 daemon：GetAsyncKeyState 轮询方向键微调歌词偏移。
    v7.14: **不做全局**——仅当本脚本控制台处于前台焦点时才处理方向键，
    否则前台是游戏/其它程序时按下方向键不会误调偏移。"""
    try:
        import win32api
    except Exception:
        return
    while True:
        try:
            if not _console_foreground():
                # 本控制台无焦点：忽略按键，并复位按压状态，避免回到前台后误触发一次
                for vk in _KEY_ADJ_KEYMAP:
                    _KEY_ADJ_STATE[vk] = [False, 0.0]
                time.sleep(0.03)
                continue
            now_perf = time.perf_counter()
            for vk, delta in _KEY_ADJ_KEYMAP.items():
                try:
                    pressed = (win32api.GetAsyncKeyState(vk) & 0x8000) != 0
                except Exception:
                    pressed = False
                st = _KEY_ADJ_STATE.get(vk, [False, 0.0])
                held, last_adj = st
                if pressed:
                    if not held:
                        _adjust_offset_by(delta)              # 上升沿（刚按下）立即调一次
                        _KEY_ADJ_STATE[vk] = [True, now_perf]
                    elif _KEY_ADJ_REPEAT_MS and (now_perf - last_adj) * 1000.0 >= _KEY_ADJ_REPEAT_MS:
                        _adjust_offset_by(delta)              # 长按持续调整
                        _KEY_ADJ_STATE[vk] = [True, now_perf]
                else:
                    if held:
                        _KEY_ADJ_STATE[vk] = [False, 0.0]     # 松开复位
        except Exception:
            pass
        time.sleep(0.03)


def _start_keyboard_adjust():
    """启动方向键微调线程（幂等）。v7.14：仅本控制台前台时响应；win32api 不可用则静默跳过。"""
    global _KEY_ADJ_THREAD
    if _KEY_ADJ_THREAD is not None and _KEY_ADJ_THREAD.is_alive():
        return
    _KEY_ADJ_THREAD = threading.Thread(target=_keyboard_adjust_loop, daemon=True)
    _KEY_ADJ_THREAD.start()


# ── SMTC 初始化与轮询 ──

def _ensure_smtc_loop():
    """确保 winrt 用的独立事件循环已创建"""
    global _smtc_loop
    if _smtc_loop is None:
        import asyncio as _aio
        _smtc_loop = _aio.new_event_loop()
    return _smtc_loop


def _ensure_smtc_mgr():
    """确保 SMTCManager 已请求（只请求一次，缓存）
    ══ v6.74 P1-1：成功创建后订阅 SessionsChanged/PlaybackInfoChanged/MediaPropertiesChanged
       三事件，回调把事件名入 _smtc_event_queue（全异常静默），供主循环 drain 触发轻量 poll"""
    global _smtc_mgr, _smtc_events_subscribed
    if _smtc_mgr is not None:
        return _smtc_mgr
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SMTCManager
        )
        loop = _ensure_smtc_loop()
        _smtc_mgr = loop.run_until_complete(SMTCManager.request_async())
        log("SMTC: 已请求 Session Manager")
    except Exception as e:
        log(f"SMTC: 初始化失败: {e}")
        return None
    # ══ v6.74 P1-1：事件订阅（try/except 全异常静默，失败降级到纯 poll 兜底）══
    if (not _smtc_events_subscribed) and _smtc_mgr is not None:
        try:
            def _smtc_on_sessions_changed(*args, **kwargs):
                try:
                    _smtc_event_queue.put_nowait("SessionsChanged")
                except Exception:
                    pass
            def _smtc_subscribe_current_session_events(mgr_ref):
                try:
                    cur = mgr_ref.get_current_session()
                    if cur is not None:
                        def _on_playback_changed(*_a, **_k):
                            try:
                                _smtc_event_queue.put_nowait("PlaybackInfoChanged")
                            except Exception:
                                pass
                        def _on_media_changed(*_a, **_k):
                            try:
                                _smtc_event_queue.put_nowait("MediaPropertiesChanged")
                            except Exception:
                                pass
                        try:
                            if hasattr(cur, "add_playback_info_changed"):
                                cur.add_playback_info_changed(_on_playback_changed)
                            elif hasattr(cur, "playback_info_changed"):
                                cur.playback_info_changed += _on_playback_changed
                        except Exception:
                            pass
                        try:
                            if hasattr(cur, "add_media_properties_changed"):
                                cur.add_media_properties_changed(_on_media_properties_changed)
                            elif hasattr(cur, "media_properties_changed"):
                                cur.media_properties_changed += _on_media_properties_changed
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                if hasattr(_smtc_mgr, "add_sessions_changed"):
                    _smtc_mgr.add_sessions_changed(_smtc_on_sessions_changed)
                elif hasattr(_smtc_mgr, "sessions_changed"):
                    _smtc_mgr.sessions_changed += _smtc_on_sessions_changed
            except Exception:
                pass
            _smtc_subscribe_current_session_events(_smtc_mgr)
            _smtc_events_subscribed = True
            log("SMTC: 事件订阅已启用（SessionsChanged/PlaybackInfoChanged/MediaPropertiesChanged）")
        except Exception:
            # 任何订阅失败 → 静默降级到纯 poll（1s 兜底够用，不影响功能）
            _smtc_events_subscribed = False
    return _smtc_mgr


def _format_duration(total_sec: int) -> str:
    """秒 → "m:ss" """
    if total_sec <= 0:
        return ""
    m = total_sec // 60
    s = total_sec % 60
    return f"{m}:{s:02d}"


def _thumbnail_to_base64(thumb_stream_ref) -> str:
    """把 SMTC 的缩略图 IRandomAccessStreamReference 转成 data:image/jpeg;base64,...
    失败返回空串
    ══ v6.73 P0-3：DataReader / IRandomAccessStream 显式 Close + Dispose，finally 兜底不泄漏句柄"""
    if thumb_stream_ref is None:
        return ""
    stream = None
    dr = None
    try:
        import asyncio as _aio
        loop = _ensure_smtc_loop()
        # OpenReadAsync → IInputStream
        stream = loop.run_until_complete(thumb_stream_ref.open_read_async())
        # 从流里读字节: 使用 DataReader
        from winrt.windows.storage.streams import DataReader
        size = stream.size
        if size <= 0 or size > 10 * 1024 * 1024:  # >10MB 忽略
            return ""
        dr = DataReader(stream)
        loop.run_until_complete(dr.load_async(int(size)))
        buf = bytearray(int(size))
        dr.read_bytes(buf)
        import base64
        b64 = base64.b64encode(bytes(buf)).decode()
        # 判断图像格式（简单读前几个字节）
        head = bytes(buf[:4])
        if head.startswith(b"\x89PNG"):
            mime = "image/png"
        elif head[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        else:
            mime = "image/jpeg"
        return f"data:{mime};base64,{b64}"
    except Exception:
        # 多数播放器不暴露缩略图或权限不足，不打日志
        return ""
    finally:
        # ══ v6.73 P0-3：显式释放 WinRT 流对象，避免句柄累积爆掉
        if dr is not None:
            try:
                # DataReader.Close() / Dispose() 二选一，都包 try/except 防异常崩主流程
                if hasattr(dr, "close"):
                    try: dr.close()
                    except Exception: pass
                if hasattr(dr, "dispose"):
                    try: dr.dispose()
                    except Exception: pass
            except Exception:
                pass
            dr = None
        if stream is not None:
            try:
                if hasattr(stream, "close"):
                    try: stream.close()
                    except Exception: pass
                if hasattr(stream, "dispose"):
                    try: stream.dispose()
                    except Exception: pass
                if hasattr(stream, "flush_async"):
                    try:
                        loop_ref = _ensure_smtc_loop()
                        if loop_ref is not None: loop_ref.run_until_complete(stream.flush_async())
                    except Exception: pass
            except Exception:
                pass
            stream = None


def _kugou_only_song_change():
    """v7.19/v7.21: 用酷狗窗口标题（歌名锚点）驱动切歌，完全独立于 SMTC。
    复用它依赖的所有叶子函数（_kugou_title_song/_await_kugou_pos/_anchor_clock/
    _stage_lyric_event/歌词&封面缓存）。只认当前标题 → 与 _last_song_key 一致则幂等不动。
    由标题线程在检测到歌名变更时调用（主驱动），poll_smtc 无 session 分支也兜底调用。"""
    global _last_song_key, _last_song_change_ts, _lyrics_fetched_for, _cover_fetched_for
    global _CLOCK_play_rate, _CLOCK_last_playback_rate, _CLOCK_paused, _CLOCK_last_playing_state
    global _smtc_song_intro_emitted_at, _lyric_pending
    # 直接读当前标题（标题线程已确保 _KUGOU_TITLE 新鲜），不靠 DB 变化判定——避免与 poll_smtc 竞态消费。
    _ka, _kt = _kugou_title_song()
    if not _kt:
        return
    cur_key = f"{_ka} - {_kt}".strip(" -") if _ka else _kt
    if not cur_key or cur_key == _last_song_key:
        return
    # 防并发与 poll_smtc 常规切歌路径竞态：仅当当前状态还没有这首歌才切
    with _state_lock:
        if _SMTC_STATE.get("song", "") == cur_key and _last_song_key == cur_key:
            return
        _last_song_change_ts = time.time()
        _SMTC_STATE["song"] = cur_key
        _SMTC_STATE["artist"] = _ka
        _SMTC_STATE["title"] = _kt
        _SMTC_STATE["cover"] = ""
        _SMTC_STATE["lyric_line"] = ""
        _SMTC_STATE["lyric_event"] = ""
        _SMTC_STATE["timeline"] = []
        _SMTC_STATE["trans_timeline"] = []
        _lyric_pending = False
    _last_song_key = cur_key
    print(f"[CUT) {time.strftime('%H:%M:%S')} 短语开始 cur_key={cur_key!r} ka={_ka!r} kt={_kt!r}", flush=True)
    log(f"SMTC 切歌(kugou标题兜底): {cur_key}")
    t_p0 = time.time()
    formatted_intro = f"**\u25b6 {cur_key}**"
    _smtc_song_intro_emitted_at = t_p0
    try:
        _lyric_event_queue.clear()
    except Exception:
        pass
    _stage_lyric_event(formatted_intro, f"{formatted_intro}|{t_p0:.3f}")
    global _last_lyric_idx, _last_trans_idx, _last_lyric_raw, _last_trans_raw, _last_emit_wall_ts_ms
    _last_lyric_idx = -1
    _last_trans_idx = -1
    _last_lyric_raw = ""
    _last_trans_raw = ""
    _last_emit_wall_ts_ms = 0.0
    _cancel_all_lyric_timers(cur_key)
    _cancel_catchup(cur_key)
    try:
        tick_lyric._last_song_sent = cur_key
    except Exception:
        pass
    # ══ 本地时钟：切歌 → 用酷狗真实进度锚定（避免从头算词）══
    _CLOCK_play_rate = 1.0
    _CLOCK_last_playback_rate = 1.0
    _CLOCK_paused = False
    _CLOCK_last_playing_state = True
    _pos_init = 0
    _kp_init = _await_kugou_pos(_SG_INIT_KUGOU_WAIT_S)
    if _kp_init is not None and _kp_init > 0:
        _pos_init = int(_kp_init)
    # ══ v7.23 防旧切歌线程误锚：本线程在 _await_kugou_pos(最长15s) 等待期间，标题线程可能已
    #    再次 spawn kugou_cut 切到下一首（_SMTC_STATE["song"] 已变为新歌）。若此刻仍用旧歌
    #    位置 _anchor_clock()，会把新歌本地时钟拉回旧歌位置 → 新歌"切歌没响应"、歌词从错误进度
    #    重启。等待结束后歌若已换，直接放弃本次锚定与后续写入（索引/定时器在新歌切歌里已被重置）。══
    with _state_lock:
        if _SMTC_STATE.get("song") != cur_key:
            return
    _anchor_clock(_pos_init, reason=f"song_init(kugou_only) {cur_key!r}")
    print(f"[CUT) {time.strftime('%H:%M:%S')} 锚定完成 pos={_pos_init}", flush=True)
    log(f"切歌锚定 [{t_p0:.3f}] {cur_key!r} → pos_ms={_pos_init}")
    # ══ 歌词 & 封面：缓存优先，未命中并发搜索 ══
    if _ka and _kt:
        _lyrics_fetched_for = cur_key
        _cover_fetched_for = cur_key
        cached_lyric = _cache_get_lyric(_ka, _kt)
        if cached_lyric:
            name, tl, trans = cached_lyric
            tl_clean = _sanitize_timeline(tl, tag="cache_timeline")
            trans_clean = _sanitize_timeline(trans or [], tag="cache_trans") if trans else []
            with _state_lock:
                if _SMTC_STATE["song"] == cur_key:
                    _SMTC_STATE["timeline"] = tl_clean
                    if trans_clean:
                        _SMTC_STATE["trans_timeline"] = trans_clean
            log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=缓存命中{len(tl_clean)}行 +翻译={len(trans_clean)}行")
            log(f"歌词: 缓存命中 {name} ({len(tl_clean)} 行)" + (f" +翻译 {len(trans_clean)} 行" if trans_clean else ""))
            _force_emit_current_lyric(cur_key)
        else:
            log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=未命中 立刻并发搜索")
            threading.Thread(target=_fetch_lyrics_bg, args=(_ka, _kt, cur_key, t_p0), daemon=True).start()
        cached_cover = _cache_get_cover(_ka, _kt)
        if cached_cover:
            csrc, curl = cached_cover
            with _state_lock:
                if _SMTC_STATE["song"] == cur_key:
                    cnow = _SMTC_STATE.get("cover", "")
                    if not cnow or cnow.startswith("data:"):
                        _SMTC_STATE["cover"] = curl
            log(f"封面: 缓存命中 {csrc} → {curl[:70]}...")
        else:
            threading.Thread(target=_fetch_cover_bg, args=(_ka, _kt, cur_key), daemon=True).start()


def poll_smtc():
    """
    每轮调用一次：
    - 读取当前活动媒体会话的进度、播放状态、媒体属性（标题/艺人/专辑/封面/时长）
    - 写入 _SMTC_STATE（加锁）
    - 返回是否有有效会话
    """
    global _last_smtc_ts, _last_song_key, _last_song_change_ts, _lyrics_fetched_for, _cover_fetched_for
    global _CLOCK_play_rate, _CLOCK_paused, _CLOCK_last_playing_state, _CLOCK_last_playback_rate, _CLOCK_last_drift_check_ts, _CLOCK_drift_target_ms
    global _force_media_recheck, _last_media_recheck_ts
    mgr = _ensure_smtc_mgr()
    if mgr is None:
        return False
    try:
        sessions = mgr.get_sessions()
    except Exception as e:
        log(f"SMTC: get_sessions 异常: {e}")
        return False
    if not sessions:
        with _state_lock:
            _SMTC_STATE["hasSong"] = False
            _SMTC_STATE["playing"] = False
        # ══ v7.23 删除此处的【同步】_kugou_only_song_change() ══
        #    v7.19 曾在这里同步调用它作为切歌兜底，但该函数内部 _await_kugou_pos(15s) 在冷启动
        #    首次切歌时会同步阻塞最长 15s → 直接冻住本线程。而本函数既被 smtc_poll worker 调，
        #    也被 tick_lyric 的 poll_smtc 调：tick_lyric 是唯一持续推算/发歌词的线程，被冻 15s
        #    = 切歌后 15s 内没有任何后续 lyric_event（用户日志：intro 弹出后 30s 无歌词）。
        #    它的检测源与标题线程完全同源（都读 _kugou_title_song），无任何新增覆盖，纯阻塞。
        #    酷狗标题驱动切歌已由 _kugou_title_loop 独立线程(v7.21)检测，并丢到**非阻塞**后台
        #    kugou_cut 线程(v7.22)执行，故此处不再需要同步兜底 → 保证切歌后歌词线程不被冻结。
        return False

    # v7.08: 本轮是否已用酷狗真实进度完成切歌锚定（②-5 需据此跳过，避免把真实锚点拽回 0）
    _song_init_kugou_anchored = False

    # 取第一个有媒体内容的会话
    session = None
    for s in sessions:
        try:
            pb = s.get_playback_info()
            if pb and pb.playback_status != 0:
                session = s
                break
        except Exception:
            continue
    if session is None:
        session = sessions[0]

    try:
        # ── 播放状态 + 进度 ──
        pb = session.get_playback_info()
        playing = (pb.playback_status == 4) if pb else False  # 4 = Playing
        try:
            tl = session.get_timeline_properties()
            start_sec = tl.start_time.total_seconds() if tl.start_time else 0
            end_sec = tl.end_time.total_seconds() if tl.end_time else 0
            pos_sec = tl.position.total_seconds() if tl.position else 0
            total_sec = max(0, int(end_sec - start_sec))
        except Exception:
            pos_sec = 0
            total_sec = 0
        duration_str = _format_duration(total_sec)

        # ── v7.04 酷狗适配：SMTC 进度恒 0 → 用 UI Automation 真实进度覆盖 pos_sec ──
        #    覆盖后贯穿 媒体重读判定 / 切歌锚定 / seek 检测 / 进度上报，全链路用真实位置，
        #    这才是「播到一半启动也能跟上」的关键（否则锚在 0 点从头算词）。
        try:
            _kp = _kugou_read_progress_ms()
            if _kp is not None and _kp >= 0:
                smtc_pos = int(pos_sec * 1000)
                # 仅当 SMTC 明显不可信(pos≈0)或与真实差>3s 才覆盖，避免干扰正常播放器
                if smtc_pos < 3000 or abs(smtc_pos - _kp) > 3000:
                    pos_sec = _kp / 1000.0
                    # 兜底：SMTC 未给时长时，用酷狗真实时长校准 total_sec
                    if total_sec <= 0:
                        _kdur = _kugou_duration_ms()
                        if _kdur and _kdur > 0:
                            total_sec = _kdur / 1000.0
                            duration_str = _format_duration(total_sec)
        except Exception:
            pass

        # ── 媒体属性（仅在切歌/首次/周期强制时读，避免频繁 RPC）──
        cur_key = ""
        media_info = None
        song_changed = False
        force_recheck = _force_media_recheck   # v7.02: 切歌误判暂停后强制重读媒体，确认真实新歌
        _force_media_recheck = False
        # ══ v7.11: 酷狗 SMTC 歌名滞后——切歌瞬间(或进度被实时覆盖后)进度差恒≈0，media 只在
        #    force_recheck 时重读一次；若那次酷狗 SMTC 还没更新到新歌，就永久卡在上一首。
        #    这里每 ~2s 强制重读一次媒体：一旦酷狗 SMTC 把 title 更新到新歌，立即触发切歌。══
        _now_mr = time.time()
        media_force = force_recheck
        if _now_mr - _last_media_recheck_ts >= 2.0:
            media_force = True
            _last_media_recheck_ts = _now_mr
        with _state_lock:
            if not _SMTC_STATE["song"] or abs(pos_sec * 1000 - _SMTC_STATE["progress_ms"]) > 15000 or _SMTC_STATE["duration_str"] != duration_str or media_force:
                # 进度跳变或时长变化 → 可能切歌，强查媒体属性
                try:
                    import asyncio as _aio
                    loop = _ensure_smtc_loop()
                    media_info = loop.run_until_complete(session.try_get_media_properties_async())
                except Exception:
                    media_info = None
            elif _last_song_key == "":
                try:
                    import asyncio as _aio
                    loop = _ensure_smtc_loop()
                    media_info = loop.run_until_complete(session.try_get_media_properties_async())
                except Exception:
                    media_info = None

        # ══ v7.12: 酷狗窗口标题兜底切歌（SMTC title 常不更新，导致切歌/跟随全部失效）══
        #    SMTC 拿到的 title 若与当前歌相同(没识别出新歌)或为空 → 用酷狗窗口标题解析出的
        #    真实歌名构造 media_info，让下方切歌判定能发现新歌并切歌/换词。
        if media_info is not None and media_info.title:
            _mk = f"{media_info.artist or ''} - {media_info.title}".strip(" -") if (media_info.artist or "") else media_info.title
        else:
            _mk = ""
        if not _mk or _mk == _last_song_key or not _last_song_key:
            _ka, _kt = _kugou_confirmed_title_song()
            if _kt and f"{_ka} - {_kt}".strip(" -") != _last_song_key:
                try:
                    import types
                    _onew = media_info
                    media_info = types.SimpleNamespace(
                        artist=_ka, title=_kt,
                        thumbnail=(None if _onew is None else getattr(_onew, "thumbnail", None)))
                except Exception:
                    pass

        if media_info is not None and media_info.title:
            artist = media_info.artist or ""
            title = media_info.title or ""
            cur_key = f"{artist} - {title}" if artist else title
            # ══ v7.22 防双切歌覆盖：kugou 标题驱动切歌后（_last_song_key 已注入），SMTC 的 title 若与当前
            #     song 的 title 相同（如 kugou 带"何流、宋晓峰"、SMTC 只给"何流"，title 一致），不算切歌，
            #     避免"何流、宋晓峰 - XX" → "何流 - XX" 反复覆盖 key、歌词从 0 重发。 ══
            _cur_song = _SMTC_STATE.get("song", "")
            _same_title = False
            try:
                if _cur_song and title:
                    _cur_title_stripped = _cur_song.split(" - ", 1)[-1].strip() if " - " in _cur_song else _cur_song.strip()
                    _new_title_stripped = title.strip()
                    if _cur_title_stripped and _new_title_stripped and _cur_title_stripped == _new_title_stripped:
                        _same_title = True
            except Exception:
                _same_title = False
            if cur_key and cur_key != _last_song_key and not _same_title:
                song_changed = True
                _last_song_key = cur_key
                _last_song_change_ts = time.time()
                # 读封面
                cover = ""
                try:
                    thumb = media_info.thumbnail
                    if thumb is not None:
                        cover = _thumbnail_to_base64(thumb)
                except Exception:
                    cover = ""
                # 写状态
                global _lyric_pending, _progress_window, _last_predicted_pos_ms
                with _state_lock:
                    _SMTC_STATE["song"] = cur_key
                    _SMTC_STATE["artist"] = artist
                    _SMTC_STATE["title"] = title
                    _SMTC_STATE["cover"] = cover
                    _SMTC_STATE["duration_str"] = duration_str
                    _SMTC_STATE["duration_sec"] = total_sec
                    _SMTC_STATE["timeline"] = []
                    _SMTC_STATE["trans_timeline"] = []
                    _SMTC_STATE["lyric_line"] = ""
                    _SMTC_STATE["lyric_event"] = ""
                    # ══ v6.81 P0 根修：切歌清空 lyric_event 槽后必须同步把 _lyric_pending 置 False，
                    #    否则上一首歌 pending=True 时切歌会让"槽=空串 + pending=True" 处于错位状态：
                    #    主循环 3636 的条件是 True and "" → False → 永不消费 → 永不补槽 → 队列中
                    #    的 intro/新歌歌词永远出不来，服务端从切歌那一秒开始完全收不到任何 lyric_event
                    #    （用户日志：01:46:44.861 最后一句，之后 11+ 秒只有心跳，没有任何 "收到 lyric_event"）
                    _lyric_pending = False
                # ══ v6.81 P1：切歌显式重置进度滑窗，防止两首歌 progress 跳变正好 < 3000ms
                #    时 1739 行的 delta 判断无法触发，旧歌残点污染新歌 rate 拟合
                try:
                    with _progress_window_lock:
                        _progress_window.clear()
                except Exception:
                    pass
                _last_predicted_pos_ms = None
                log(f"SMTC 切歌: {cur_key} (时长={duration_str})" + (" [封面OK]" if cover else ""))
                # ── v6.64 按要求：**切歌提示 + 启动歌词线程** 严格绑在一起（不再等 tick_lyric 80ms 才打切歌提示）
                #    顺序：切歌 log → 查歌词缓存 → (没命中 → 立刻打印切歌提示 → 0ms 启动歌词线程)
                #    切歌提示 timestamp 就是 [LYRIC_PROFILE] T0，用来算「切歌提示 → 最终 LRCLIB 命中」真实耗时
                t_p0 = time.time()
                formatted_intro = f"**\u25b6 {cur_key}**"
                _smtc_song_intro_emitted_at = t_p0   # tick_lyric 用来去重（避免重复打印切歌提示）
                with _state_lock:
                    if _SMTC_STATE["song"] == cur_key:
                        # v6.79 P0 切歌关键：立即清空旧歌事件队列，防止旧歌缓冲事件串到新歌
                        global _lyric_event_queue
                        try:
                            _lyric_event_queue.clear()
                        except Exception:
                            pass
                        # v6.79：统一走 _stage_lyric_event（防溢出覆盖丢句）
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
                # ══ v7.00 本地时钟：切歌 → 初始化 clock state 变量 ══
                _CLOCK_play_rate = 1.0
                _CLOCK_last_playback_rate = 1.0
                _CLOCK_paused = (not playing)
                _CLOCK_last_playing_state = playing
                # ══ v7.08 切歌/启动首轮锚定：必须在拆歌词/发歌词**之前**用真实进度锚定，
                #    否则酷狗 SMTC 恒报 0 → 本地时钟锚在 0 → 歌词从头 burst，直到酷狗真实进度
                #    （探针线程首次遍历约需数秒）才拿得到，而此时错词已经发出去。
                #    用自己的常量是因为 pos_sec 此时仍是 SMTC 的 0（顶部酷狗覆盖当时快照无效）。
                _song_init_kugou_anchored = False
                _sg_init_pos_ms = int(pos_sec * 1000)
                # 仅当播放中且 SMTC 位置不可信(pos≈0)才等酷狗真实进度；其它播放器 SMTC 给真实 pos 则直接锚，不白等。
                if playing and _sg_init_pos_ms < 3000:
                    _kp_init = _await_kugou_pos(_SG_INIT_KUGOU_WAIT_S)
                    if _kp_init is not None and _kp_init > 0:
                        _sg_init_pos_ms = int(_kp_init)
                        _song_init_kugou_anchored = True
                elif not playing:
                    _sg_init_pos_ms = 0
                _anchor_clock(_sg_init_pos_ms, reason=f"song_init {cur_key!r}" + (" kugou" if _song_init_kugou_anchored else ""))
                log(f"切歌锚定 [{t_p0:.3f}] {cur_key!r} → pos_ms={_sg_init_pos_ms} kugou={'Y' if _song_init_kugou_anchored else 'N'}")
                # ── 歌词 & 封面：先查本地缓存，命中就不走网络，严格 0ms 抢第一句 ──
                if artist and title:
                    _lyrics_fetched_for = cur_key
                    _cover_fetched_for = cur_key
                    # ① 歌词缓存
                    cached_lyric = _cache_get_lyric(artist, title)
                    if cached_lyric:
                        name, tl, trans = cached_lyric
                        # ══ v6.71 P0 修复：缓存命中读出来也必须走二次净化（老缓存可能有历史坏行）
                        tl_clean = _sanitize_timeline(tl, tag="cache_timeline")
                        trans_clean = _sanitize_timeline(trans or [], tag="cache_trans") if trans else []
                        if len(tl_clean) != len(tl or []):
                            log(f"[LYRIC_PROFILE] CACHE_SANITIZE_DROP song={cur_key!r} 缓存原始 {len(tl or [])} 行 → 净化 {len(tl_clean)} 行")
                        with _state_lock:
                            if _SMTC_STATE["song"] == cur_key:
                                _SMTC_STATE["timeline"] = tl_clean
                                if trans_clean:
                                    _SMTC_STATE["trans_timeline"] = trans_clean
                        log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=缓存命中{len(tl_clean)}行 +翻译={len(trans_clean)}行")
                        log(f"歌词: 缓存命中 {name} ({len(tl_clean)} 行)" + (f" +翻译 {len(trans_clean)} 行" if trans_clean else ""))
                        _force_emit_current_lyric(cur_key)
                    else:
                        # ══ v6.64：**切歌提示打完立刻启动歌词线程**（之前是先 start 再在 tick_lyric 80ms 后打提示）
                        log(f"切歌提示 [{t_p0:.3f}] offset={_LYRIC_OFFSET_MS}ms: {cur_key} | 歌词=未命中 立刻并发搜索")
                        threading.Thread(target=_fetch_lyrics_bg, args=(artist, title, cur_key, t_p0), daemon=True).start()
                    # ② 封面缓存（只有 SMTC cover 空/小图时才考虑覆盖）
                    need_cover = not cover or cover.startswith("data:")
                    if need_cover:
                        cached_cover = _cache_get_cover(artist, title)
                        if cached_cover:
                            csrc, curl = cached_cover
                            with _state_lock:
                                if _SMTC_STATE["song"] == cur_key:
                                    cnow = _SMTC_STATE.get("cover", "")
                                    if not cnow or cnow.startswith("data:"):
                                        _SMTC_STATE["cover"] = curl
                            log(f"封面: 缓存命中 {csrc} → {curl[:70]}...")
                        else:
                            threading.Thread(target=_fetch_cover_bg, args=(artist, title, cur_key), daemon=True).start()
                # ══ v7.08 首次锚定已在切歌块顶部完成（含酷狗真实进度等待），此处不再重复锚定 ══
        else:
            # 没拿到 media_info，但已有 song，仍更新 duration_str 兜底
            if duration_str and cur_key == "":
                with _state_lock:
                    if _SMTC_STATE["song"] and not _SMTC_STATE["duration_str"]:
                        _SMTC_STATE["duration_str"] = duration_str

        # ═══════════════════════════════════════════════════════════════
        # v7.00 本地时钟驱动进度系统（替换 v6.74~v6.80 滑窗滤波）
        #   - 切歌/seek/暂停恢复/倍速变化 4 类事件 → 立即调用 _anchor_clock() 重锚 SMTC.pos
        #   - 之后 100% 由本地 time.perf_counter() 单调积分驱动，完全摆脱 SMTC 上报频率
        #   - 每 30s 对比 SMTC.pos 与本地理想位置，偏差>200ms → 渐进式 drift_trim 校正
        # ═══════════════════════════════════════════════════════════════
        pos_ms_raw = int(pos_sec * 1000)
        now_wall = time.time()
        # ── ① 读取 PlaybackRate（倍速）：多数播放器 SMTC 给，失败默认 1.0 ──
        playback_rate = 1.0
        try:
            playback_rate = float(getattr(pb, "playback_rate", 1.0) or 1.0)
        except Exception:
            playback_rate = 1.0
        if playback_rate <= 0 or playback_rate > 10.0:
            playback_rate = 1.0
        # ── ② 事件检测： paused→resumed / rate_changed / seek / 首次未anchor ──
        need_anchor = False
        anchor_reason = ""
        # v7.01：SMTC pos卡死检测的临时工作变量（每轮必初始化，避免②-1/②-2/②-4分支走到未赋值）
        _CLOCK_smtc_pos_stuck_count_wip = 0
        _CLOCK_smtc_pos_raw_wip = pos_ms_raw
        # ②-1: paused → resumed（暂停→播放）必须 anchor
        if playing and (not _CLOCK_last_playing_state):
            need_anchor = True
            anchor_reason = "paused→resumed"
            # v7.02: Kugou/SMTC 切到下一首歌时，旧会话常先短暂报 paused 再 resumed，且位置重置回起点(pos≈0)。
            #         此时置强制重读媒体属性，下一轮 poll 重新拉 title 确认真实新歌，避免"切到下一首却一直停留上一首"。
            if pos_ms_raw < 3000:
                _force_media_recheck = True
                if _LYRIC_SYNC_LOG:
                    try:
                        log(f"[LYRIC_PROFILE] PAUSE_RESUME_AT_START pos_ms_raw={pos_ms_raw}ms<3000 → 置强制媒体重读(疑似切歌)")
                    except Exception:
                        pass
        # ②-2: 倍速变化 → 顺手重锚（保证 play_rate 更新后基准正确）
        elif abs(playback_rate - _CLOCK_last_playback_rate) > 0.01:
            need_anchor = True
            anchor_reason = f"rate_change {_CLOCK_last_playback_rate:.2f}→{playback_rate:.2f}"
        # ②-3: seek 检测（同歌内拖动进度条）→ SMTC 与本地理想差>3s 且 SMTC 位置真的跳变过
        #      v7.01 修：酷狗/部分网易云SMTC会恒报0导致"每3s本地走满 → seek锚回0"死循环。
        #               所以必须同时满足：
        #                 a) |pos_ms_raw - ideal_pos_ms| > 3000
        #                 b) 本轮 pos_ms_raw != 上轮 pos (即 SMTC 真的发生了跳变，不是永远卡同一个值)
        #                    连续 2+ 轮 pos 完全相同 → 标记 SMTC 位置挂掉，整条 seek 判定短路禁用。
        elif _CLOCK_anchor_wall_perf > 0:
            try:
                with _CLOCK_lock:
                    wperf = _CLOCK_anchor_wall_perf
                    apos = _CLOCK_anchor_pos_ms
                    prate = _CLOCK_play_rate if _CLOCK_play_rate else 1.0
                    last_smtc_pos = _CLOCK_last_smtc_pos_raw
                    stuck_count = _CLOCK_smtc_pos_stuck_count
                # 先更新连续相同计数（不计入全局 lock，与下面 anchor 分支一起在函数末尾统一写回）
                if last_smtc_pos == pos_ms_raw:
                    stuck_count_now = stuck_count + 1
                else:
                    stuck_count_now = 0
                if stuck_count_now >= 2:
                    # SMTC 位置连续 2 轮完全没动 → 视为位置上报失效（酷狗恒报0类），跳过 seek 判定
                    pass
                else:
                    dt_wall_s = max(0.0, time.perf_counter() - wperf)
                    ideal_pos_ms = int(apos + dt_wall_s * float(prate) * 1000)
                    big_diff = abs(pos_ms_raw - ideal_pos_ms) > 3000
                    # 首轮（last_smtc_pos == -1，无任何历史可参考跳变）：不能认定 seek
                    smtc_jumped = (last_smtc_pos >= 0) and (pos_ms_raw != last_smtc_pos)
                    if big_diff and smtc_jumped:
                        need_anchor = True
                        anchor_reason = f"seek smtc={pos_ms_raw}ms ideal={ideal_pos_ms}ms diff={abs(pos_ms_raw-ideal_pos_ms)}ms"
                # 把 stuck_count_now 写回（后续仍需：本轮 pos_ms_raw 作为下一轮 last_smtc_pos_raw，一起写）
                _CLOCK_smtc_pos_stuck_count_wip = stuck_count_now
                _CLOCK_smtc_pos_raw_wip = pos_ms_raw
            except Exception:
                _CLOCK_smtc_pos_stuck_count_wip = 0
                _CLOCK_smtc_pos_raw_wip = pos_ms_raw
        # ②-4: 完全没 anchor 过（启动首轮有歌但未切歌）→ 兜底
        elif _CLOCK_anchor_wall_perf <= 0 and pos_ms_raw >= 0:
            need_anchor = True
            anchor_reason = "first_poll_after_start"
        # ②-5: 切歌1s内 anchor_snapshot 与 pos差 >5s 兜底自愈
        #        v7.08: 若本轮已用酷狗真实进度完成切歌锚定，则跳过——此时 pos_ms_raw 仍是 SMTC 的 0，
        #              触发只会把刚锚好的真实位置又拽回 0，破坏"播到一半追上"。
        if (not need_anchor) and (not _song_init_kugou_anchored) and _SMTC_STATE.get("song", "") and (time.time()-_last_song_change_ts < 1.0):
            try:
                if abs(_CLOCK_last_anchor_snapshot_ms - pos_ms_raw) > 5000:
                    need_anchor = True
                    anchor_reason = "song_changed_snapshot_gap_heal"
            except Exception:
                pass
        # ── ②-6: 酷狗真实进度兜底强制对齐（v7.08）──
        #    切歌/启动首轮若首锚没等到真实进度（如首次遍历超时）而锚在 0，此后 SMTC 恒 0 会让
        #    seek 的 stuck_count≥2 永久禁用重锚，导致探针拿到真实进度后也永远不拽回——歌词一直从头播。
        #    这里每个周期拿探针快照：本地时钟与真实进度偏差 >2s 时，无视 stuck 禁用，强制重锚到真实进度。
        if (not need_anchor) and (not _song_init_kugou_anchored):
            try:
                _kp_snap = _kugou_read_progress_ms()
                if _kp_snap is not None and _kp_snap > 0:
                    # ══ v7.14: 剔除 offset 再比真实进度——offset 是用户补偿(常为正,把歌词延后)，
                    #    若不剔除，get_local_eff_ms() 会把 offset 算进"本地",导致 diff≈-offset 每轮恒>2s，
                    #    ②-6 便反复把本地拽回真实、把 offset 抵消掉 → 歌词表现为不受 offset 影响 → "越播越慢/要不停加偏移"。
                    _local_clock_raw = get_local_eff_ms() - int(_LYRIC_OFFSET_MS or 0)
                    _kugou_diff = _kp_snap - _local_clock_raw
                    if abs(_kugou_diff) > 2000:
                        need_anchor = True
                        anchor_reason = f"kugou_real_time_align diff={_kugou_diff:+d}ms"
                        pos_ms_raw = int(_kp_snap)
            except Exception:
                pass
        # ── ③ 执行 anchor ──
        if need_anchor:
            _CLOCK_play_rate = playback_rate
            _CLOCK_last_playback_rate = playback_rate
            _CLOCK_paused = (not playing)
            _anchor_clock(pos_ms_raw, reason=anchor_reason)
        else:
            _CLOCK_paused = (not playing)
            # rate 小变化仍同步（不重锚）
            if abs(_CLOCK_play_rate - playback_rate) > 0.001:
                with _CLOCK_lock:
                    _CLOCK_play_rate = playback_rate
            _CLOCK_last_playback_rate = playback_rate
        _CLOCK_last_playing_state = playing
        # ── ②末尾：把 SMTC 本轮 pos 写入"上次pos"+"卡死计数"（供下一轮 seek 判定使用）
        #    在 ②-3 分支里已经算好 wip；其他分支(②-1/②-2/②-4)走默认值 stuck=0 raw=本轮值
        #    注意：真实 seek 拖动时 pos_ms_raw 会跳变 → stuck_count 重置为 0，下轮立即恢复判定
        try:
            with _CLOCK_lock:
                prev_stuck = _CLOCK_smtc_pos_stuck_count
                _CLOCK_last_smtc_pos_raw = _CLOCK_smtc_pos_raw_wip
                _CLOCK_smtc_pos_stuck_count = _CLOCK_smtc_pos_stuck_count_wip
            new_stuck = _CLOCK_smtc_pos_stuck_count_wip
            # 卡死态"初触发"打一次日志，避免每轮刷（连续>=2 轮卡死）
            if prev_stuck < 2 and new_stuck >= 2:
                try:
                    log(f"[LOCAL_CLOCK] SMTC_POS_STUCK 连续{new_stuck}轮 pos_ms_raw={pos_ms_raw}ms 完全未变 → 禁用 seek 自动重锚（仅 paused→resume/切歌/拉条才会重锚）。若播放器SMTC确实不动，进度完全由本地 perf_clock 积分驱动")
                except Exception:
                    pass
        except Exception:
            pass
        # ── ③ 把 SMTC raw pos 写入状态（仅展示/调试用，歌词计算统一走 get_local_eff_ms()） ──
        #    v7.01：已移除 DRIFT_CHECK 自动校准（用户要求：不要自动校准SMTC，完全信任本地时钟 + 手动offset）
        eff_ms_for_write = pos_ms_raw

        # 总是更新: 进度、播放状态、SMTC 时间戳
        with _state_lock:
            _SMTC_STATE["progress_ms"] = eff_ms_for_write
            _SMTC_STATE["playing"] = playing
            _SMTC_STATE["hasSong"] = True
        _last_smtc_ts = time.time()
        return True
    except Exception as e:
        log(f"SMTC 轮询异常: {e}")
        try:
            log(traceback.format_exc())
        except Exception:
            pass
        return False


# ── LRC 解析 + 各平台歌词拉取（并行） ──

def _parse_lrc(text):
    """更宽容的 LRC 解析：
    - 支持一行多时间戳 [00:01.00][00:05.00]副歌
    - 支持 [mm:ss]、[mm:ss.xx]、[mm:ss.xxx]、[mm:ss,xxx]
    - 空文本行也保留（有些 LRC 用空行表示间奏，不解析但也不丢行）
    - ══ v6.71 P0 修复：最终输出前**逐行强校验**，确保每条都是 (可float时间, 任意文本) 二元组
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
                if t < 0 or t != t:
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
    ══ v6.71 P0 修复：任何来源写入 _SMTC_STATE["timeline"/"trans_timeline"] 之前，统一走**二次强校验+净化**。
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
    ══ v6.71 P0 修复：逐行解包也 try/except，坏行 continue 不崩。
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
                t = float(t)
            except Exception:
                continue
            if t != t or t < 0:
                continue
            if t <= sec:
                idx = i
                cur = "" if txt is None else txt
            else:
                break
        except Exception:
            continue
    return (idx, cur)


def _current_lyric(timeline, ms):
    return _current_lyric_idx(timeline, ms)[1]


# ══ 统一 HTTP 请求：UA/Referer + 响应健壮性检查（避免第三方 API 反爬返回空 HTML）══
# 参考经验：第三方音乐 API 若缺 headers 常触发限流/反爬，导致 json() 解析失败/空结果
_COMMON_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_DEFAULT_HEADERS = {"User-Agent": _COMMON_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


_HTTP_MAX_RETRIES = 2        # v6.77 去熔断提速：单请求最多自动重试 2 次（总计 3 次尝试），避免 5 次指数退避叠加总耗时 30+s 直接越过 T5=18s 总超时
_HTTP_RETRY_BACKOFF = 1.0    # v6.77 常量退避：每次重试等 1.0s（不再 0.25/0.5/1/2/4 指数递增空耗 7.75s），切歌/抖动响应更灵敏
# ══ 歌词搜索专属参数（修 C+：针对 49s 慢得离谱场景）════
_LYRIC_LRCLIB_RETRIES = 1    # ══ v6.65 再提速：1 次（Diamond Eyes / Plum-Maelstrom 实测：Spotify 时长常跟 LRCLIB 精确对不上，retries=2 就纯等 4s 没用；1 次 2s 不行立刻 fuzzy 中）
_LYRIC_LRCLIB_TIMEOUT = 2    # ══ v6.63 提速：单次 2s（海外节点抖就立刻放弃 转中文源 / fuzzy）
_LYRIC_POOL_MAX_WORKERS = 8  # 3 歌词源 × 2(繁简) × 2(LRCLIB 精确+模糊) = 最多 12；8 槽保证快源不被慢源卡住
_LYRIC_FIRST_RESULT_TMO_S1 = 6.0   # ══ v6.65 再提速：6s 上限（第一轮精确基本全 MISS 场景，9s 最后 3s 纯浪费；6s 到立刻进第二轮 fuzzy，欧美歌第二轮秒中）
_LYRIC_FIRST_RESULT_TMO_S2 = 12.0  # ══ v6.63 提速：12s 上限（不再 18/31s 等死）
_LYRIC_CN_TIMEOUT = 2.5            # ══ v6.63 提速：网易云/QQ音乐 单请求 2.5s（原 3.5s，封IP/半开 TCP 不耗时间）
# ══ v6.66 严格按进度+LRC时间戳模式 + 全开日志 ═══════════
_LYRIC_STRICT_SYNC = True           # True=严格按歌曲进度(eff_ms)和LRC时间戳推进；防跳句夹逼仅在误差超过 _LYRIC_MAX_DRIFT_MS 时才启用
_LYRIC_MAX_DRIFT_MS = 250.0         # 严格模式下：只有 (eff_ms - LRC[expected_idx].t_ms) > MAX_DRIFT 才夹逼 last+1；<=MAX_DRIFT 直接按原始idx推进（按用户要求严格）
_LYRIC_SYNC_LOG = False             # v6.75 默认 False：正常运行不刷 [LYRIC_SYNC]，排障时临时改回 True 开全量诊断
_LYRIC_TIMER_PREMISS_MS = 0.0       # 严格模式下，Timer 提前量改为 0（不再减去半 tick），严格按 LRC 间隔 wait；用户要求"按 LRC 时间戳输出"

# ═══════════════════════════════════════════════════════════════
# v6.73 P0-2 全局常驻线程池（根治 with ThreadPoolExecutor shutdown(wait=True) 空等 running HTTP 任务结束）
#   - 程序启动创建 1 次，永不销毁（shutdown 永远不调用）
#   - 歌词搜索 R1/R2、封面搜索、缓存持久化 全部复用同一池
#   - 命中/超时后仅对 pending 任务 cancel()，**不等待 running 任务结束**，主线程/后台线程立刻继续
# ═══════════════════════════════════════════════════════════════
_LYRIC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_LYRIC_POOL_MAX_WORKERS,
    thread_name_prefix="lyric_worker"
)
# v6.73 P1-4 HTTP 全局 Session（TCP/TLS 连接复用，同源握手开销↓30%+）；懒加载首次使用时初始化
_HTTP_SESSION = None
_HTTP_SESSION_LOCK = threading.Lock()
# v6.77 用户要求：彻底移除熔断机制（原 P1-4 连续失败3次→5min跳过请求，偶发超时会被放大成15min搜不到）

def _get_http_session():
    """v6.73 P1-4：懒加载全局 requests.Session，复用连接；线程安全单例"""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is not None:
            return _HTTP_SESSION
        import requests as _req_mod
        _HTTP_SESSION = _req_mod.Session()
        # 默认 UA：模拟普通浏览器，避免 requests 默认 UA 被部分 API 直接拒
        _HTTP_SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        return _HTTP_SESSION


def _http_get_json(url: str, *, referer: str = "", extra_headers: dict | None = None, timeout: int = 5,
                   verify: bool = True, retries: int = _HTTP_MAX_RETRIES, _debug_tag: str = ""):
    """v6.77 精简版：全局Session复用 + requests timeout + retries次常量1.0s退避重试 + 响应健壮性校验 + 分级失败日志
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
    session = _get_http_session()  # v6.73 P1-4：全局Session单例
    # v6.77 已移除：线程级硬超时包装（Future.cancel无法杀running，嵌套无收益）

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
        # ══ v6.77：直接用 requests.timeout（urllib3 socket select 真生效）
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
            # SSL fallback：用直接 _one_attempt(True) 跑，不再独立套硬超时（因为外层已经套过了；直接同步执行即可）
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
            ssl_fallback_ok = True
        if is_last_attempt or not should_retry:
            break
        real_retries_performed += 1
        backoff = _HTTP_RETRY_BACKOFF  # v6.77 常量1.0s退避（不再0.25/0.5/1/2/4指数递增，原空耗7.75s）
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


# ══ 繁→简 兜底转换（不依赖 opencc，Spotify 的繁体歌名/艺人在国内平台索引是简体）══
# 覆盖高频：TOP 500 华语艺人 + 歌名常见字，足够解决 99% 的「繁体全平台搜不到」问题
_T2S_TABLE = str.maketrans({
    # 常见藝人名字（仅单字符繁→简；自身已为简或繁简同形可留但避免重复）
    "榮":"荣","華":"华","陳":"陈","歲":"岁","愛":"爱","妳":"你","李":"李","為":"为","張":"张",
    "傑":"杰","謝":"谢","鋒":"锋","倫":"伦","蕭":"萧","騰":"腾","靜":"静","孫":"孙","劉":"刘",
    "鄧":"邓","麗":"丽","學":"学","譚":"谭","詠":"咏","許":"许","蘇":"苏","見":"见","謙":"谦",
    "璽":"玺","軼":"轶","漢":"汉","馬":"马","維":"维","泷":"泷","凱":"凯",
    # 歌名常見字
    "情":"情","後":"后","來":"来","時":"时","間":"间","長":"长","相":"相",
    "思":"思","念":"念","記":"记","憶":"忆","風":"风","雨":"雨","雲":"云","煙":"烟","酒":"酒","淚":"泪",
    "夢":"梦","夜":"夜","晝":"昼","離":"离","別":"别","傷":"伤","痛":"痛","開":"开","心":"心",
    "關":"关","於":"于","過":"过","還":"还","聽":"听","說":"说","話":"话","讓":"让","給":"给","應":"应",
    "該":"该","會":"会","將":"将","點":"点","樣":"样","覺":"觉","得":"得","對":"对","錯":"错","問":"问",
    "答":"答","數":"数","買":"买","賣":"卖","興":"兴","奮":"奋","輕":"轻","重":"重","難":"难",
    "簡":"简","單":"单","復":"复","雜":"杂","習":"习","熱":"热","鬧":"闹","亂":"乱",
    "親":"亲","遠":"远","近":"近","高":"高","低":"低","進":"进","退":"退","響":"响","聲":"声",
    "音":"音","樂":"乐","歡":"欢","笑":"笑","哭":"哭","認":"认","輸":"输","贏":"赢","敗":"败","嘗":"尝",
    "試":"试","剛":"刚","現":"现","實":"实","際":"际","質":"质","願":"愿","望":"望",
    "帶":"带","領":"领","導":"导","遊":"游","戲":"戏","迎":"迎","繫":"系","懷":"怀",
    "舊":"旧","換":"换","幫":"帮","閒":"闲","義":"义","氣":"气","勵":"励","嚮":"向",
    "讀":"读","書":"书","報":"报","紙":"纸","筆":"笔","畫":"画","圖":"图","標":"标",
    "籤":"签","號":"号","碼":"码","個":"个","們":"们","體":"体","傢":"家","價":"价",
    "錢":"钱","銀":"银","鐘":"钟","錶":"表","燈":"灯","電":"电","視":"视","機":"机","車":"车","輪":"轮",
    "飛":"飞","場":"场","園":"园","廳":"厅","樓":"楼","層":"层","區":"区","縣":"县",
    "鎮":"镇","鄉":"乡","島":"岛","國":"国","競":"竞","賽":"赛","運":"运","動":"动","員":"员",
    "隊":"队","組":"组","當":"当","選":"选","舉":"举","參":"参","準":"准","備":"备",
    "結":"结","繼":"继","續":"续","從":"从",
    "來":"来","去":"去","到":"到","往":"往","返":"返","回":"回","進":"进","出":"出","經":"经",
    "過":"过","歷":"历","史":"史","載":"载","傳":"传","遞":"递","送":"送","收":"收","發":"发",
    "佈":"布","編":"编","寫":"写","改":"改","變":"变","動":"动","靜":"静","止":"止","啟":"启",
    "擴":"扩","縮":"缩","減":"减",
    "補":"补","積":"积","損":"损","獲":"获","勝":"胜",
    "訴":"诉","訟":"讼","罰":"罚","獎":"奖","懲":"惩","責":"责",
    "權":"权","務":"务","職":"职","稱":"称","銜":"衔","級":"级",
    "階":"阶","檔":"档","頁":"页","册":"册","欄":"栏","版":"版","節":"节",
    "項":"项","條":"条","款":"款","例":"例","則":"则","規":"规","律":"律",
    "製":"制","戰":"战","爭":"争","衛":"卫","擊":"击","營":"营","團":"团","旅":"旅","師":"师",
    "裝":"装","護":"护","療":"疗","醫":"医","藥":"药","養":"养","髮":"发","膚":"肤",
    "齒":"齿","脣":"唇","齶":"腭","頸":"颈","項":"项","臍":"脐","臀":"臀",
    "腸":"肠","腦":"脑",
    # 歌名词常见后缀 / 特殊
    "兒":"儿","頭":"头","邊":"边","裡":"里","線":"线","形":"形","狀":"状","態":"态",
    "紅":"红","黃":"黄","藍":"蓝","綠":"绿","黑":"黑","白":"白","灰":"灰","粉":"粉",
    "銅":"铜","鐵":"铁","鋁":"铝","錫":"锡","鉛":"铅","鋼":"钢",
    "寶":"宝","貝":"贝","鑽":"钻",
    "雷":"雷","電":"电","霜":"霜","雹":"雹","露":"露","霧":"雾","霾":"霾","潮":"潮","濕":"湿",
    "乾":"干","燥":"燥","冷":"冷","熱":"热",
})


def _trad_to_simp(text: str) -> str:
    """繁→简转换：先用查表（覆盖高频华语艺人名/歌名字），可选 opencc 兜底补全剩余字。
    不抛异常，失败直接返回原文。"""
    if not text:
        return text
    try:
        s = text.translate(_T2S_TABLE)
    except Exception:
        s = text
    # opencc 兜底（装了就用，没装跳过）
    try:
        import opencc as _opencc
        if not hasattr(_trad_to_simp, "_cc"):
            try:
                _trad_to_simp._cc = _opencc.OpenCC('t2s')  # type: ignore
            except Exception:
                try:
                    _trad_to_simp._cc = _opencc.OpenCC('zhs2zhtw_vp.ini')  # type: ignore
                except Exception:
                    _trad_to_simp._cc = None  # type: ignore
        if _trad_to_simp._cc is not None:  # type: ignore
            s2 = _trad_to_simp._cc.convert(s)  # type: ignore
            if s2:
                s = s2
    except Exception:
        pass
    return s


def _artist_match(found: str, expected: str) -> bool:
    """艺人名匹配：同时尝试「原文」和「繁→简」两种写法，避免 Spotify 繁体歌名搜不到国内平台简体索引。"""
    def _norm(s: str) -> str:
        return s.lower().replace(" ", "").replace("\u3000", "")
    candidates = {_norm(expected), _norm(_trad_to_simp(expected))}
    f = _norm(found)
    f_sc = _norm(_trad_to_simp(found))
    if not f or not expected:
        return False
    for e in candidates:
        if not e:
            continue
        # 双向前缀 4 字匹配 + 完全包含
        if e == f or e == f_sc or e in f or e in f_sc or f in e or f_sc in e:
            return True
        if len(e) >= 3 and (e[:4] in f or e[:4] in f_sc):
            return True
        if len(f) >= 3 and (f[:4] in e or f_sc[:4] in e):
            return True
    return False


def _fetch_with_t2s_fallback(fn, tag: str, artist: str, title: str, **kwargs):
    """通用包装：先用 (artist, title) 搜，若返回 None 且繁简不同则自动用 (简体artist, 简体title) 再搜一次。
    用于解决 Spotify / Apple Music 繁体歌名在国内三家平台搜不到的问题。
    （_fetch_lyrics_bg 已改为 繁简×4平台 8 任务并发，这里仅作为单平台兜底保留）"""
    r = fn(artist, title, **kwargs)
    if r is not None:
        return r
    sc_a = _trad_to_simp(artist)
    sc_t = _trad_to_simp(title)
    if sc_a == artist and sc_t == title:
        return None
    try:
        r2 = fn(sc_a, sc_t, **kwargs)
        if r2 is not None:
            if isinstance(r2, tuple) and len(r2) >= 1:
                parts = list(r2)
                parts[0] = f"{parts[0]}*"
                return tuple(parts)
    except Exception:
        pass
    return None


def _run_single_lyric_fetch(fn, artist: str, title: str, is_simp_variant: bool, duration_sec=None):
    """单平台单版本 fetch：成功返回 (result_tuple, is_simp_variant)；失败返回 (None, False)
    ══ v6.63 提速：新增 duration_sec 可选参数，LRCLIB 精确传 duration、LRCLIB 模糊传 None
    ══ v6.72 日志补齐：ANY异常打 SINGLE_EXC fn=xxx 日志，不再静默吞；正常None MISS也打标记便于分析"""
    import traceback
    fn_name = getattr(fn, "__name__", repr(fn))[:80]
    r = None
    try:
        if duration_sec is not None:
            try:
                r = fn(artist, title, duration_sec=duration_sec)
            except TypeError:
                r = fn(artist, title)
        else:
            r = fn(artist, title)
    except Exception as _e:
        try:
            log(f"[LYRIC_PROFILE] SINGLE_EXC fn={fn_name} a={artist!r} t={title!r} dur={duration_sec} msg={_e!r} tb={traceback.format_exc(limit=2)[:300]}")
        except Exception:
            pass
        return (None, False)
    if r is None:
        return (None, False)
    if is_simp_variant:
        if isinstance(r, tuple) and len(r) >= 1:
            parts = list(r)
            parts[0] = f"{parts[0]}*"
            r = tuple(parts)
    return (r, is_simp_variant)


def _netease_fetch_lyrics_raw(artist: str, title: str):
    try:
        import requests
        tag = f"网易云歌词[{artist}-{title}]"
        q = requests.utils.quote(f"{title} {artist}")
        j1 = _http_get_json(
            f"https://music.163.com/api/search/get?s={q}&type=1&limit=3",
            referer="https://music.163.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=tag + "/搜索")
        items = []
        if isinstance(j1, dict):
            items = (j1.get("result") or {}).get("songs") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            found_artist = " ".join(
                a.get("name", "") for a in (item.get("artists") or []) if isinstance(a, dict))
            if not _artist_match(found_artist, artist):
                continue
            song_id = item.get("id")
            if not song_id:
                continue
            j2 = _http_get_json(
                f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1",
                referer="https://music.163.com/", timeout=_LYRIC_CN_TIMEOUT, _debug_tag=tag + f"/歌词(id={song_id})")
            if not isinstance(j2, dict):
                continue
            lrc = ((j2.get("lrc") or {}).get("lyric")) or ""
            tlyric = ((j2.get("tlyric") or {}).get("lyric")) or ""
            tl = _parse_lrc(lrc) if lrc else None
            if tl and len(tl) > 1:
                trans = _parse_lrc(tlyric) if tlyric else []
                return ("网易云", tl, trans)
    except Exception:
        pass
    return None


def _netease_fetch_lyrics(artist: str, title: str):
    return _fetch_with_t2s_fallback(_netease_fetch_lyrics_raw, "网易云", artist, title)


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
            tl = _parse_lrc(lrc) if lrc else None
            if tl and len(tl) > 1:
                return ("QQ音乐", tl, [])
    except Exception:
        pass
    return None


def _qqmusic_fetch_lyrics(artist: str, title: str):
    return _fetch_with_t2s_fallback(_qqmusic_fetch_lyrics_raw, "QQ音乐", artist, title)


# ══ v6.72 按要求彻底移除酷狗歌词源（仅保留 LRCLIB + 网易云 + QQ音乐 三个歌词源）══
#    之前虽然 submit 没有放酷狗任务，但函数残留 = 未来改代码容易误加回去，且 source 映射有对应 tag 易混淆，现在硬删除
_LRCLIB_UA = "HuanmengKookBot/6.0 (github.local; PCStatusReporter)"


def _lrclib_fetch_lyrics_raw_precise_only(artist: str, title: str, duration_sec=None):
    """══ v6.72 拆分：只做 LRCLIB 精准匹配(/api/get + duration)，失败直接 return None，绝不走模糊。
    之前「精准+模糊写在同一个raw函数」的写法，模糊submit进去会自动读SMTC补duration→先走精准，造成2倍重复HTTP+重复精准404日志+占满线程槽=炸。
    tag 后缀明确标 /精准"""
    try:
        import requests
        tag = f"LRCLIB[{artist}-{title}]"
        dur = int(duration_sec or 0)
        if dur is None or dur <= 0:
            with _state_lock:
                dur = int(_SMTC_STATE.get("duration_sec", 0) or 0)
        if dur <= 0:
            return None
        extra = {"User-Agent": _LRCLIB_UA}
        q_artist = requests.utils.quote(artist)
        q_title = requests.utils.quote(title)
        url = (f"https://lrclib.net/api/get?artist_name={q_artist}"
               f"&track_name={q_title}&album_name=&duration={dur}")
        j = _http_get_json(url, extra_headers=extra, timeout=_LYRIC_LRCLIB_TIMEOUT,
                           retries=_LYRIC_LRCLIB_RETRIES, _debug_tag=tag + "/精准")
        if isinstance(j, dict) and j.get("syncedLyrics") and not j.get("instrumental"):
            lrc = j.get("syncedLyrics") or ""
            trans = j.get("translatedLyric") or j.get("translation") or ""
            tl = _parse_lrc(lrc) if lrc else None
            if tl and len(tl) > 1:
                trans_tl = _parse_lrc(trans) if trans else []
                tag_src = " instrumental" if j.get("instrumental") else ""
                return (f"LRCLIB{tag_src}", tl, trans_tl)
    except Exception:
        pass
    return None


def _lrclib_fetch_lyrics_raw_fuzzy_only(artist: str, title: str):
    """══ v6.72 拆分：只做 LRCLIB 模糊搜索(/api/search)，**绝不读 SMTC duration，绝不走精准匹配**，彻底避免重复。
    tag 后缀明确标 /模糊"""
    try:
        import requests
        tag = f"LRCLIB[{artist}-{title}]"
        extra = {"User-Agent": _LRCLIB_UA}
        q = requests.utils.quote(f"{artist} {title}")
        arr = _http_get_json(
            f"https://lrclib.net/api/search?q={q}",
            extra_headers=extra, timeout=_LYRIC_LRCLIB_TIMEOUT,
            retries=_LYRIC_LRCLIB_RETRIES, _debug_tag=tag + "/模糊")
        data = None
        if isinstance(arr, list):
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                found_artist = rec.get("artistName", "") or ""
                if rec.get("syncedLyrics") and (
                    _artist_match(found_artist, artist) or
                    found_artist.lower().replace(" ", "") in artist.lower().replace(" ", "") or
                    len(arr) == 1
                ):
                    data = rec
                    break
            if (not data or not data.get("syncedLyrics")) and arr and isinstance(arr[0], dict) and arr[0].get("syncedLyrics"):
                data = arr[0]
        if not isinstance(data, dict):
            return None
        lrc = data.get("syncedLyrics") or ""
        trans = data.get("translatedLyric") or data.get("translation") or ""
        tl = _parse_lrc(lrc) if lrc else None
        if tl and len(tl) > 1:
            trans_tl = _parse_lrc(trans) if trans else []
            tag_src = " instrumental" if data.get("instrumental") else ""
            return (f"LRCLIB{tag_src}", tl, trans_tl)
    except Exception:
        pass
    return None


def _lrclib_fetch_lyrics(artist: str, title: str):
    """v6.63 兼容：先精准失败再模糊（非并发submit场景兜底保留）"""
    with _state_lock:
        duration = int(_SMTC_STATE.get("duration_sec", 0) or 0)
    r = _lrclib_fetch_lyrics_raw_precise_only(artist, title, duration_sec=duration)
    if r is not None:
        return r
    return _lrclib_fetch_lyrics_raw_fuzzy_only(artist, title)


def _lrclib_fetch_lyrics_precise(artist: str, title: str, duration_sec=None):
    """LRCLIB 精确版：**只做精准**，失败直接 None（v6.72 拆分，与模糊彻底并发独立）"""
    dur = duration_sec
    if dur is None:
        with _state_lock:
            dur = int(_SMTC_STATE.get("duration_sec", 0) or 0)
    return _fetch_with_t2s_fallback(_lrclib_fetch_lyrics_raw_precise_only, "LRCLIB", artist, title, duration_sec=dur)


def _lrclib_fetch_lyrics_fuzzy(artist: str, title: str):
    """LRCLIB 模糊版：**只做模糊，绝不碰精准**（v6.72 拆分，与精准彻底并发独立，无重复HTTP）"""
    return _fetch_with_t2s_fallback(_lrclib_fetch_lyrics_raw_fuzzy_only, "LRCLIB-fuzzy", artist, title)


def _score_lyric_match(result, artist: str, title: str, duration_sec) -> float:
    """══ v6.74 P1-5 评分函数：越高越好
    result = (name, tl, trans or None)
    评分项：
      - title字符重合度（set交集 / 传入title长度）
      - duration 对比：timeline 最后一条时间与 duration_sec 差值 <15% +0.3，>30% -0.3
      - 行数合理性：10<=len(tl)<=200 +0.2，<5 或 >500 -0.3
      - 来源偏好：name 含 "LRCLIB" +0.1，"网易云" +0.05，"QQ" +0.05
    """
    score = 0.0
    try:
        if not isinstance(result, tuple) or len(result) < 2:
            return -999.0
        name = str(result[0] or "")
        tl = result[1] or []
        try:
            tl_len = len(tl)
        except Exception:
            tl_len = 0

        # 1) title 字符重合率（用 set 交集 / max(1,len(set(title_lower)))）
        try:
            tl_title = ""
            # 从 name 去掉可能的源前缀（"LRCLIB"/"网易云"/"QQ音乐"），但用户要求改为用 result 里 title 与传入 title 的字符重合
            # 这里拿不到 result 的 title，退而用：传入 artist+title 字符集合 vs timeline[*][1] 所有文本去重后的字符集合重合
            tl_chars: "set[str]" = set()
            for row in tl[:min(tl_len, 50)]:
                try:
                    txt = str(row[1] or "")
                    for ch in txt.lower():
                        if ch.isalnum() or ord(ch) > 127:
                            tl_chars.add(ch)
                except Exception:
                    continue
            title_artist_str = f"{artist} {title}"
            q_chars: "set[str]" = set()
            for ch in title_artist_str.lower():
                if ch.isalnum() or ord(ch) > 127:
                    q_chars.add(ch)
            if q_chars and tl_chars:
                inter = len(q_chars & tl_chars)
                union_len = max(1, len(q_chars))
                score += (inter / union_len) * 1.0
        except Exception:
            pass

        # 2) duration 对比
        try:
            if tl_len > 0 and duration_sec and float(duration_sec) > 0:
                last_row = tl[-1]
                try:
                    last_t_sec = float(last_row[0])
                    dur = float(duration_sec)
                    diff_pct = abs(last_t_sec - dur) / dur
                    if diff_pct < 0.15:
                        score += 0.3
                    elif diff_pct > 0.30:
                        score -= 0.3
                except Exception:
                    pass
        except Exception:
            pass

        # 3) 行数合理性
        try:
            if 10 <= tl_len <= 200:
                score += 0.2
            elif tl_len < 5 or tl_len > 500:
                score -= 0.3
        except Exception:
            pass

        # 4) 来源偏好
        try:
            if "LRCLIB" in name:
                score += 0.1
            if "网易云" in name:
                score += 0.05
            if "QQ" in name:
                score += 0.05
        except Exception:
            pass

        # 5) v6.75 翻译优先：有翻译行且行数 ≥ 主歌词 50%（或至少 3 行有效）→ +0.5 超高优先，保证候选里选带翻译的版本
        try:
            if len(result) >= 3:
                trans = result[2] or []
                try:
                    trans_len = len(trans)
                except Exception:
                    trans_len = 0
                # 有效性判断：必须是 list/tuple，且至少有 3 行是 (t, txt) 二元组结构的有效行
                valid_trans_rows = 0
                if trans_len > 0:
                    sample_range = trans if trans_len <= 60 else trans[:60]
                    for r in sample_range:
                        try:
                            if isinstance(r, (tuple, list)) and len(r) >= 2:
                                _ = float(r[0])  # 可 float 时间戳
                                if str(r[1] or "") != "":
                                    valid_trans_rows += 1
                        except Exception:
                            continue
                if valid_trans_rows >= 3 and trans_len > 0:
                    ratio = valid_trans_rows / max(1, tl_len) if tl_len > 0 else 0.0
                    if ratio >= 0.5:
                        score += 0.5
                        score_name = "TRANS_FULL_MATCH(+0.5)"
                    else:
                        score += 0.2
                        score_name = "TRANS_PARTIAL(+0.2)"
                    # 静默用不到的变量赋值，避免 IDE 警告（debug 需要时可打日志）
                    _ = score_name
        except Exception:
            pass
    except Exception:
        return -999.0
    return round(score, 4)


def _fetch_lyrics_bg(artist: str, title: str, song_key: str, t_intro: float = 0.0):
    """后台线程：3 平台（LRCLIB 精确+模糊 + 网易云 + QQ音乐）× 繁简 2 variants 共 10 路并发，先命中先返回。
    ══ v6.64 新增 t_intro 传参 + [LYRIC_PROFILE] 耗时剖析（定位你看到的「切歌提示 13:45:21 → 歌词 13:45:32 卡 10s」到底是哪段 HTTP 卡）：
       T0 = 切歌提示 发出时刻（= poll_smtc 里打印切歌提示的 t_p0）
       T1 = 本线程函数首行 真正开始执行（threading.Thread.start 到 线程被 OS 调度之间的 gap）
       T2 = 10 路 concurrent.futures submit 完成（线程池就绪 + 所有任务开始抢执行）
       T3 = 第一轮首命中（或 S1 超时）
       T4 = 第二轮首命中（或 S2 超时）
       T5 = 最终写入 _SMTC_STATE[timeline] 成功
       日志里一眼能看：T0→T1 gap 是 OS 调度；T1→T2 是繁简转换 duration 读锁；T2→T3/T4 就是各平台 HTTP 慢；T4→T5 是 LRC 解析
    ══ v6.63 提速：
       1) LRCLIB 同步 Mac 版：**精确(带 duration) + 模糊(不带 duration)** 两条 × 繁简 2 variants = 4 路
       2) 网易云 / QQ音乐 × 繁简 2 variants = 各 2 路
       3) LRCLIB timeout 4→2s / retries 3→2；网易云/QQ 单请求 3.5→2.5s
       4) 首命中总上限 S1 7→9s / S2 10→12s；**两轮中间 5s 冷等去掉**
       5) 明确 4xx（如 LRCLIB 404）不重试
    繁简转换命中的 source 带 * 后缀。"""
    try:
        t1 = time.time()  # T1: 线程真正开始
        t0 = t_intro if t_intro > 0 else t1
        log(f"[LYRIC_PROFILE] T1 线程调度启动 T0={t0:.3f} T1={t1:.3f} gap={(t1-t0)*1000:.0f}ms song={song_key}")
        sc_a = _trad_to_simp(artist)
        sc_t = _trad_to_simp(title)
        a_changed = sc_a != artist
        t_changed = sc_t != title
        is_simp_only = not a_changed and not t_changed
        variants = []
        if is_simp_only:
            variants.append((artist, title, False))
        else:
            variants.append((artist, title, False))
            variants.append((sc_a, sc_t, True))
        with _state_lock:
            dur = int(_SMTC_STATE.get("duration_sec", 0) or 0)

        def _schedule_tasks(ex, stage: str = "ALL"):
            """══ v6.74 P1-5 阶梯提交：
            stage="S1"（首发窗口 0~1.5s）：仅 variants[0] × (LRCLIB_precise + 网易云 + QQ) = 最多 3 条，不含 LRCLIB_fuzzy
            stage="ALL"（S1 未命中后追投）：variants 全部 × (LRCLIB_precise + LRCLIB_fuzzy + 网易云 + QQ) = 原完整集
            """
            fs = []
            if stage == "S1":
                variant_list = variants[:1] if variants else []
                for (va, vt, vsimp) in variant_list:
                    fs.append(ex.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_precise, va, vt, vsimp, dur))
                    fs.append(ex.submit(_run_single_lyric_fetch, _netease_fetch_lyrics, va, vt, vsimp))
                    fs.append(ex.submit(_run_single_lyric_fetch, _qqmusic_fetch_lyrics, va, vt, vsimp))
                return fs
            for (va, vt, vsimp) in variants:
                fs.append(ex.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_precise, va, vt, vsimp, dur))
                fs.append(ex.submit(_run_single_lyric_fetch, _lrclib_fetch_lyrics_fuzzy, va, vt, vsimp, None))
                fs.append(ex.submit(_run_single_lyric_fetch, _netease_fetch_lyrics, va, vt, vsimp))
                fs.append(ex.submit(_run_single_lyric_fetch, _qqmusic_fetch_lyrics, va, vt, vsimp))
            return fs

        def _consume_scored(futures_all, *, first_result_timeout: float, min_wait_s: float = 1.5, round_tag: str) -> tuple | None:
            """══ v6.74 P1-5：命中窗口内维护 best_result + best_score
            - MIN_WAIT_S=1.5s 或 first_result_timeout 到期后返回 best（若有）
            - 第一个 HIT 出现后立刻 cancel pending（running 中的完成了仍可参与评分）
            - 对每个 HIT 调 _score_lyric_match 打分，选分数最高者
            """
            import traceback
            t_c0 = time.time()
            log(f"[LYRIC_PROFILE] T2_submit_done@{t_c0:.3f} round={round_tag} tasks={len(futures_all)} timeout={first_result_timeout}s MIN_WAIT_S={min_wait_s}s")
            STEP_S = 0.1
            processed: "set[int]" = set()
            best_result: tuple | None = None
            best_score: float | None = None
            first_hit_seen = False
            pending_cancelled = False
            try:
                while True:
                    elapsed = time.time() - t_c0
                    min_wait_ok = elapsed >= min_wait_s
                    tmo_hit = (first_result_timeout is not None and elapsed >= first_result_timeout)
                    # 离开条件：(超时 或 min_wait_ok 且 已有 best) 且 没有"还 running 中 可能继续产出 HIT 的必要"
                    if tmo_hit or (min_wait_ok and (first_hit_seen or all(f.done() for f in futures_all))):
                        if first_hit_seen:
                            # 最后再等一小段 STEP 让即将 done 的 running（非 cancel 的）也进评分
                            deadline = min_wait_s + 0.2
                            while time.time() - t_c0 < deadline:
                                try:
                                    done_rush, _ = concurrent.futures.wait(futures_all, timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED)
                                except Exception:
                                    done_rush = set()
                                for fut in done_rush:
                                    fid = id(fut)
                                    if fid in processed:
                                        continue
                                    processed.add(fid)
                                    try:
                                        r, _is = fut.result()
                                    except Exception:
                                        continue
                                    if r is None:
                                        continue
                                    try:
                                        sc = _score_lyric_match(r, artist, title, dur)
                                    except Exception:
                                        sc = -999.0
                                    if best_score is None or sc > best_score:
                                        best_score = sc
                                        best_result = r
                                        log(f"[LYRIC_PROFILE] task {round_tag} RUSH_UPDATE_BEST score={sc} src={str(r[0])[:40]!r}")
                                if all(f.done() for f in futures_all):
                                    break
                        break
                    remaining = max(0.01, min(STEP_S, first_result_timeout - elapsed)) if first_result_timeout is not None else STEP_S
                    try:
                        done_set, _pending_set = concurrent.futures.wait(
                            futures_all, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                    except Exception:
                        done_set = set()
                    for fut in done_set:
                        fid = id(fut)
                        if fid in processed:
                            continue
                        processed.add(fid)
                        t_done = time.time()
                        dt_ms = (t_done - t_c0) * 1000
                        try:
                            r, _is_simp = fut.result()
                        except Exception as _ie:
                            log(f"[LYRIC_PROFILE] task {round_tag} @{t_done:.3f} +{dt_ms:.0f}ms -> EXC(step轮询) {_ie!r}")
                            try:
                                log(f"[LYRIC_PROFILE]   EXC_TB {traceback.format_exc(limit=2)[:300]}")
                            except Exception:
                                pass
                            continue
                        hit_tag = "HIT" if r is not None else "MISS"
                        src = ""
                        if r is not None:
                            try:
                                src = str(r[0])[:60]
                            except Exception:
                                src = ""
                        log(f"[LYRIC_PROFILE] task {round_tag} @{t_done:.3f} +{dt_ms:.0f}ms -> {hit_tag} {src}")
                        if r is not None:
                            try:
                                sc = _score_lyric_match(r, artist, title, dur)
                            except Exception:
                                sc = -999.0
                            is_better = (best_score is None or sc > best_score)
                            if is_better:
                                best_score = sc
                                best_result = r
                                log(f"[LYRIC_PROFILE] task {round_tag} @{t_done:.3f} NEW_BEST score={sc} src={src!r}")
                            # ══ 第一个 HIT 出现 → 立刻 cancel pending；running 的完成后仍可进评分
                            if not first_hit_seen:
                                first_hit_seen = True
                                cancel_attempt = 0
                                cancel_ok = 0
                                for f in futures_all:
                                    if not f.done():
                                        cancel_attempt += 1
                                        if f.cancel():
                                            cancel_ok += 1
                                pending_cancelled = True
                                log(f"[LYRIC_PROFILE] T3_first_hit@{t_done:.3f} round={round_tag} 自T0={(t_done-t0)*1000:.0f}ms 自本轮={(t_done-t_c0)*1000:.0f}ms cancel_attempt={cancel_attempt} cancel_ok={cancel_ok} pending已取消 → MIN_WAIT_S({min_wait_s}s)内继续收集running完成HIT评分选最优")
                    if all(f.done() for f in futures_all):
                        break
            except Exception as _outer_consume:
                try:
                    log(f"[LYRIC_PROFILE] T3_CONSUME_OUTER_EXC round={round_tag} msg={_outer_consume!r} tb={traceback.format_exc(limit=3)[:400]}")
                except Exception:
                    pass
            finally:
                try:
                    for f in futures_all:
                        if not f.done():
                            f.cancel()
                except Exception:
                    pass
            status_line = f"best_score={best_score} best_src={str(best_result[0])[:40] if best_result else 'None'!r}"
            if best_result is not None:
                log(f"[LYRIC_PROFILE] T3_end@{time.time():.3f} round={round_tag} PICK_BEST 自T0={(time.time()-t0)*1000:.0f}ms {status_line}")
            else:
                log(f"[LYRIC_PROFILE] T3_end@{time.time():.3f} round={round_tag} 本轮无命中 自T0={(time.time()-t0)*1000:.0f}ms")
            return best_result

        result = None
        executor = _LYRIC_EXECUTOR
        # ══ v6.74 P1-5 阶段一：S1 窗口，仅 variants[0] × (LRCLIB_precise + 网易云 + QQ) 最多 3 条首发
        S1_WINDOW_S = 1.5
        s1_timeout = max(S1_WINDOW_S + 0.2, _LYRIC_FIRST_RESULT_TMO_S1)
        futures_s1 = _schedule_tasks(executor, stage="S1")
        log(f"[LYRIC_PROFILE] P1-5 S1_SUBMIT variants[:1] tasks={len(futures_s1)} S1_WINDOW={S1_WINDOW_S}s")
        result = _consume_scored(futures_s1, first_result_timeout=s1_timeout, min_wait_s=S1_WINDOW_S, round_tag="S1")

        if result is None:
            with _state_lock:
                if _SMTC_STATE.get("song") != song_key:
                    log(f"[LYRIC_PROFILE] S1 结束已切歌 → 放弃后续 song={song_key}")
                    return
            # ══ v6.74 P1-5 阶段二：S1 未命中 → 追投 variants 剩余 + 所有 LRCLIB_fuzzy（即完整全集，但 S1 已经跑过的任务不重复 submit；此处简化直接 submit ALL 全集，重复任务因 _score_lyric_match 评分一致不影响最终结果，且 S1 原任务大多已 done/cancel）
            futures_s2_all = _schedule_tasks(executor, stage="ALL")
            log(f"[LYRIC_PROFILE] P1-5 S2_APPEND variants×全部 tasks={len(futures_s2_all)} timeout={_LYRIC_FIRST_RESULT_TMO_S2}s")
            result = _consume_scored(futures_s2_all, first_result_timeout=_LYRIC_FIRST_RESULT_TMO_S2, min_wait_s=min(1.5, _LYRIC_FIRST_RESULT_TMO_S2 * 0.5), round_tag="S2")

        t5 = time.time()
        if result is None:
            log(f"[LYRIC_PROFILE] T5_fail@{t5:.3f} 两轮结束均未命中 总耗时={(t5-t0)*1000:.0f}ms(自T0) song={song_key}")
            log(f"歌词: 未找到 ({artist} - {title})")
            return
        name, tl, trans = result
        # ══ v6.71 P0 修复：任何来源（LRCLIB/网易云/QQ/缓存）写 timeline/trans_timeline 之前，
        #    统一走 _sanitize_timeline 二次净化，确保写入 _SMTC_STATE 的 100% 是干净 (float, str) 二元组
        tl_clean = _sanitize_timeline(tl, tag="timeline")
        trans_clean = _sanitize_timeline(trans or [], tag="trans_timeline") if trans else []
        if len(tl_clean) != len(tl or []):
            log(f"[LYRIC_PROFILE] T5_SANITIZE_DROP 原始 {len(tl or [])} 行 → 净化后 {len(tl_clean)} 行（丢 {max(0,len(tl or [])-len(tl_clean))} 坏行）")
        with _state_lock:
            if _SMTC_STATE["song"] != song_key:
                log(f"[LYRIC_PROFILE] T5_fail@{t5:.3f} 命中了但已切歌 总耗时={(t5-t0)*1000:.0f}ms song={song_key}")
                return
            _SMTC_STATE["timeline"] = tl_clean
            if trans_clean:
                _SMTC_STATE["trans_timeline"] = trans_clean
        log(f"[LYRIC_PROFILE] T5_ok@{t5:.3f} 命中写入 timeline 总耗时={(t5-t0)*1000:.0f}ms(自T0) source={name} lines={len(tl_clean)}")
        log(f"歌词: {name} ({len(tl_clean)} 行)" + (f" +翻译 {len(trans_clean)} 行" if trans_clean else ""))
        # ══ v6.68 定位卡死：先发歌词决策，写缓存后移（缓存IO不阻塞用户看到第一句）
        with _state_lock:
            if _SMTC_STATE.get("song") != song_key:
                log(f"[LYRIC_PROFILE] POST_T5:DROP_song_changed_before_emit song={song_key!r} current={_SMTC_STATE.get('song')!r}")
                _cache_put_lyric(artist, title, name, tl_clean, trans_clean)
                return
            playing_now = _SMTC_STATE.get("playing", False)
            eff_ms = get_local_eff_ms()
            dt_from_last_smtc = 0
            cur_idx, cur_txt = _current_lyric_idx(tl_clean, eff_ms)
            # ══ v6.71 P0 修复：LRC 时间戳取法加 try/except，坏行绝不抛到外层
            try:
                lrc_t0 = (tl_clean[0][0] * 1000 if tl_clean else 0)
            except Exception:
                lrc_t0 = 0
            try:
                lrc_t_cur = (tl_clean[cur_idx][0] * 1000 if (tl_clean and cur_idx >= 0 and cur_idx < len(tl_clean)) else None)
            except Exception:
                lrc_t_cur = None
            log(f"[LYRIC_PROFILE] POST_T5:DECIDE song={song_key!r} lines={len(tl_clean)} playing={playing_now} eff_ms={eff_ms} OFFSET={_LYRIC_OFFSET_MS}ms → LRC[0]={lrc_t0}ms LRC[cur_idx={cur_idx}]={lrc_t_cur}ms cur_txt(repr)={cur_txt!r} cur_idx_last_lyric_idx={_last_lyric_idx}")
        if cur_idx > 0:  # ══ v6.68 修：去掉 and cur_txt（间奏空行 cur_idx 也推进，原条件永远 False 导致不补位卡死），只要 idx>0 就补 0..cur_idx
            log(f"[LYRIC_PROFILE] POST_T5:BRANCH_CATCHUP cur_idx={cur_idx}>0 → do_catchup 0..{cur_idx} cur_txt_empty={not bool(cur_txt)}")
            log(f"歌词下载完成时进度已推进到 idx={cur_idx}，启动补位 0..{cur_idx}")
            _catchup_lyrics_until(song_key, cur_idx)
        else:
            log(f"[LYRIC_PROFILE] POST_T5:BRANCH_FORCE cur_idx={cur_idx} → _force_emit_current_lyric cur_txt_empty={not bool(cur_txt)}")
            _force_emit_current_lyric(song_key)
        # ══ v6.68 写缓存移到最后，不阻塞歌词输出
        _cache_put_lyric(artist, title, name, tl_clean, trans_clean)
    except Exception as e:
        import traceback
        log(f"[LYRIC_PROFILE] POST_T5:FATAL_EXCEPTION msg={e!r} traceback={traceback.format_exc()}")
        log(f"WARN: 歌词后台线程异常(已吞): {e}")


def _emit_one_lyric_at_idx(song_key: str, timeline, trans_timeline, emit_idx: int,
                           playing: bool, schedule_next: bool,
                           eff_ms_ref: int):
    """v7.01 burst helper：按**给定精确索引 emit_idx** 发一句歌词（不再按eff_ms重算idx，严格顺序）。
    用于 tick / Timer 追过期行时的连发循环。
    - emit_idx: 必须 > _last_lyric_idx（调用方负责，否则本条静默跳过）
    - schedule_next: True → 发完本句挂 emit_idx+1 的精确定时器；False → 仅发（中间行），不挂 Timer
    - eff_ms_ref: 用于 drift 日志 / schedule_next wait 计算 / FIRST gap 判定 的进度参考快照
    返回：是否真的发送了（空行不stage_event，但仍会推进last索引，也返回True；被drop返回False）"""
    global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _last_emit_wall_ts_ms
    if emit_idx < 0 or not timeline or emit_idx >= len(timeline):
        return False
    # 严格顺序强保证：emit_idx 必须大于 _last_lyric_idx
    if emit_idx <= _last_lyric_idx:
        if _LYRIC_SYNC_LOG:
            log(f"[LYRIC_SYNC] BURST:DROP_emit_idx<=last song={song_key!r} emit_idx={emit_idx} last={_last_lyric_idx}")
        return False
    # 取文本 + 翻译
    try:
        t_sec, txt = timeline[emit_idx]
    except Exception as _e:
        log(f"[LYRIC_PROFILE] BURST:UNPACK_FAIL emit_idx={emit_idx} timeline[i]={timeline[emit_idx]!r} msg={_e!r} → skip")
        return False
    trans_txt = ""
    if trans_timeline:
        best_dt = 0.6
        target_t = timeline[emit_idx][0]
        for tt, ttxt in trans_timeline:
            try:
                dt = abs(float(tt) - float(target_t))
            except Exception:
                continue
            if dt < best_dt and ttxt:
                best_dt = dt
                trans_txt = ttxt
            if best_dt == 0:
                break
    # 先更新 last 索引（即使是空行也推进，防止下一轮再发）
    _last_lyric_idx = emit_idx
    # 翻译行索引保持简单策略：直接把 emit_idx 记上，后续 tick 里 _current_lyric_idx 会重算正确值
    _last_trans_idx = emit_idx
    _last_lyric_raw = txt if (txt and txt.strip()) else _last_lyric_raw
    if trans_txt:
        _last_trans_raw = trans_txt
    # 空行 / 纯空白：不发送事件到 KOOK（但索引已推进），只在 schedule_next=True 时挂下一句定时器
    if not txt or not txt.strip():
        if schedule_next and playing and emit_idx + 1 < len(timeline):
            next_t_ms_f = timeline[emit_idx + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
            wait_ms_f = max(0.0, (next_t_ms_f - eff_ms_ref) - _LYRIC_TIMER_PREMISS_MS)
            _schedule_next_lyric_at(song_key, timeline, trans_timeline, emit_idx + 1, wait_ms_f)
        return True
    # 正常有文本：写 lyric_event（走 stage 队列，防覆盖）
    formatted = _format_lyric_line(txt, trans_txt)
    ts = time.time()
    _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
    # ── 日志：gap + drift ──
    cur_wall_ms_f = int(ts * 1000)
    N_f = emit_idx + 1
    if _last_emit_wall_ts_ms == 0.0:
        gap_for = f"{N_f} (FIRST)"
        real_gap_str_f = "FIRST"
        lrc_gap_ms_f = 0
    else:
        rg_f = cur_wall_ms_f - int(_last_emit_wall_ts_ms)
        real_gap_str_f = f"{rg_f}ms"
        try:
            lrc_gap_ms_f = int((timeline[emit_idx][0] - timeline[emit_idx - 1][0]) * 1000) if emit_idx > 0 else 0
        except Exception:
            lrc_gap_ms_f = 0
        gap_for = f"[{rg_f}ms] {N_f}  | LRC_gap={lrc_gap_ms_f}ms"
    if _LYRIC_SYNC_LOG:
        try:
            lrc_t_now = timeline[emit_idx][0] * 1000.0 + _LYRIC_OFFSET_MS
            drift_now = (eff_ms_ref - lrc_t_now)
            marker_next = " +schedule_next" if schedule_next else ""
            log(f"[LYRIC_SYNC] BURST:EMIT song={song_key!r} idx={emit_idx}/{len(timeline)} drift={drift_now:.0f}ms(+超前 -滞后) eff_ref={eff_ms_ref}ms LRC_t={lrc_t_now:.0f}ms emit_ts={ts:.3f} real_gap={real_gap_str_f} LRC_gap={lrc_gap_ms_f}ms{marker_next}")
        except Exception:
            pass
    log(f"歌词(burst) [{ts:.3f}] idx={emit_idx}/{len(timeline)} offset={_LYRIC_OFFSET_MS}ms: {txt}" + (f" | 翻译: {trans_txt}" if trans_txt else "") + f"  | {gap_for}")
    _last_emit_wall_ts_ms = float(cur_wall_ms_f)
    # 挂下一句 Timer（仅 schedule_next=True 时挂 1 次）
    if schedule_next and playing and emit_idx + 1 < len(timeline):
        next_t_ms_f = timeline[emit_idx + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
        wait_ms_f = max(0.0, (next_t_ms_f - eff_ms_ref) - _LYRIC_TIMER_PREMISS_MS)
        if _LYRIC_SYNC_LOG:
            log(f"[LYRIC_SYNC] BURST:SCHEDULE_NEXT song={song_key!r} cur={emit_idx} next={emit_idx+1} LRC_next={next_t_ms_f:.0f}ms eff_ref={eff_ms_ref}ms wait_ms_float={wait_ms_f:.2f}")
        _schedule_next_lyric_at(song_key, timeline, trans_timeline, emit_idx + 1, wait_ms_f)
    return True


def _burst_catchup_to_idx(song_key: str, timeline, trans_timeline,
                          target_idx: int, playing: bool,
                          eff_ms_ref: int,
                          max_lines: int = 200) -> int:
    """v7.01：连发 _last_lyric_idx+1 → target_idx（含）。
    - 单次最多发 max_lines 句（防止 seek 到几百句后一次性刷屏）
    - 最后一句发完自动挂 schedule_next=True（下一句定时器），中间句不挂
    - 返回实际发了几句（含跳过的空行/丢的drop行也算推进了idx的消耗数）"""
    if target_idx is None or target_idx < 0:
        return 0
    start_i = _last_lyric_idx + 1
    if start_i > target_idx:
        return 0
    end_i = min(target_idx, len(timeline) - 1, start_i + max_lines - 1)
    if end_i < start_i:
        return 0
    sent = 0
    for i in range(start_i, end_i + 1):
        is_final = (i == end_i)
        ok = _emit_one_lyric_at_idx(song_key, timeline, trans_timeline, i,
                                    playing=playing,
                                    schedule_next=is_final,
                                    eff_ms_ref=eff_ms_ref)
        sent += 1 if ok else 0
    return sent


def _force_emit_current_lyric(song_key: str):
    """歌词下载完成或状态变化时立即调用：按当前进度强行发出一句，保证歌词与音乐严格同步
    用 timeline 索引去重而不是文本去重，解决副歌/重复句被误跳过的问题。
    发完一句会自动给「下一句」安排精确定时器 Timer，确保按时间轴踩点触发（含 offset 提前量）。"""
    global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_emit_wall_ts_ms
    with _state_lock:
        if _SMTC_STATE.get("song") != song_key:
            return
        timeline = _SMTC_STATE.get("timeline", [])
        trans_timeline = _SMTC_STATE.get("trans_timeline", [])
        if not timeline:
            return
        playing = _SMTC_STATE.get("playing", False)
        eff_ms = get_local_eff_ms()
        idx, cur = _current_lyric_idx(timeline, eff_ms)
        trans_idx, cur_trans = _current_lyric_idx(trans_timeline, eff_ms)
        if not cur:
            # ══ v6.68 修：首句是间奏空行（LRCLIB 常把前奏/间奏写空行）时不静默 return 导致"卡死"
            #    仍然：推进 _last_lyric_idx=idx（tick 不会重复判），如果有下一句直接挂 Timer，等待第一句有人声
            log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE song={song_key!r} idx={idx} cur_txt(repr)='' eff_ms={eff_ms} → 仍推进idx并调下一句Timer，避免卡在首句空行")
            if idx > _last_lyric_idx:
                _last_lyric_idx = idx
                _last_trans_idx = trans_idx
            if playing and idx + 1 < len(timeline):
                next_t_ms_f = timeline[idx + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
                wait_ms_f = max(0.0, (next_t_ms_f - eff_ms) - _LYRIC_TIMER_PREMISS_MS)
                log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE_SCHEDULE song={song_key!r} idx={idx} next_idx={idx+1} LRC_next_t={next_t_ms_f:.0f}ms wait_ms_float={wait_ms_f:.2f}")
                _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)
            else:
                log(f"[LYRIC_PROFILE] FORCE:EMPTY_LINE_NOSCHEDULE song={song_key!r} idx={idx} playing={playing} has_next={idx+1<len(timeline)} → 无下一句可调度（将依赖tick兜底）")
            return
        # ══ 按时间轴索引去重：文本相同但位置不同（副歌重复句）也要重发 ══
        if idx == _last_lyric_idx and trans_idx == _last_trans_idx:
            # 没推进到新一句 → 但仍需：如果播放中且有下一句，挂下一句的 Timer（避免切 seek 后停在旧 idx）
            if playing and idx + 1 < len(timeline):
                next_t_ms_f = timeline[idx + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
                # v6.66 严格模式：按 eff_ms 精确算剩余时间，float 不 int，减 premiss(0 默认)
                wait_ms_f = max(0.0, (next_t_ms_f - eff_ms) - _LYRIC_TIMER_PREMISS_MS)
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] FORCE:NO_PROGRESS_SCHEDULE song={song_key!r} idx={idx} next_idx={idx+1} eff_ms={eff_ms} LRC_next_t={next_t_ms_f:.0f}ms wait_ms_float={wait_ms_f:.2f} | old_int写法(next-eff-int(LYRIC*0.5))={max(0,int(next_t_ms_f-eff_ms)-int(LYRIC_TICK_MS*0.5))}ms")
                _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)
            return
        _last_lyric_idx = idx
        _last_trans_idx = trans_idx
        _last_lyric_raw = cur
        _last_trans_raw = cur_trans
        formatted = _format_lyric_line(cur, cur_trans)
        ts = time.time()
        # v6.79：统一走 _stage_lyric_event（防溢出覆盖丢句）
        _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
    # v6.78：gap 计算提到 if 外，避免 _LYRIC_SYNC_LOG True/False 两分支重复写
    cur_wall_ms_f = int(ts * 1000)
    N_f = idx + 1
    if _last_emit_wall_ts_ms == 0.0:
        gap_for_force = f"{N_f} (FIRST)"
        real_gap_str_f = "FIRST"
        lrc_gap_ms_f = 0
    else:
        rg_f = cur_wall_ms_f - int(_last_emit_wall_ts_ms)
        real_gap_str_f = f"{rg_f}ms"
        try:
            lrc_gap_ms_f = int((timeline[idx][0] - timeline[idx - 1][0]) * 1000) if idx > 0 else 0
        except Exception:
            lrc_gap_ms_f = 0
        gap_for_force = f"[{rg_f}ms] {N_f}  | LRC_gap={lrc_gap_ms_f}ms"
    # v6.66 [LYRIC_SYNC] force emit（补位下载完成触发）：打 drift
    if _LYRIC_SYNC_LOG:
        try:
            with _state_lock:
                tl = _SMTC_STATE.get("timeline", [])
            lrc_t_now = (tl[idx][0] * 1000.0 + _LYRIC_OFFSET_MS) if (tl and idx >= 0 and idx < len(tl)) else None
            drift_now = (eff_ms - lrc_t_now) if lrc_t_now is not None else None
            log(f"[LYRIC_SYNC] FORCE:EMIT song={song_key!r} idx={idx}/{len(timeline)} drift={drift_now:.0f if drift_now is not None else 'None'}ms(+超前 -滞后) eff_ms={eff_ms} LRC_t={lrc_t_now} emit_ts={ts:.3f} real_gap={real_gap_str_f} LRC_gap={lrc_gap_ms_f}ms")
        except Exception as _fex:
            import traceback
            log(f"[LYRIC_PROFILE] FORCE:EMIT_EXCEPTION song={song_key!r} idx={idx} msg={_fex!r} traceback={traceback.format_exc()}")
    # v6.78：gap 直接并到歌词行末尾，不再独立 2 行；_last_emit_wall_ts_ms 统一在 if 外写 1 次
    log(f"歌词(补位) [{ts:.3f}] idx={idx}/{len(timeline)} offset={_LYRIC_OFFSET_MS}ms: {cur}" + (f" | 翻译: {cur_trans}" if cur_trans else "") + f"  | {gap_for_force}")
    _last_emit_wall_ts_ms = float(cur_wall_ms_f)
    # ══ 发完一句 → 立刻挂下一句的精确定时器（v7.01：按当前真实eff_ms算剩余，不管LRC固定间隔）══
    if playing and idx + 1 < len(timeline):
        next_t_ms_f = timeline[idx + 1][0] * 1000.0 + _LYRIC_OFFSET_MS
        wait_ms_f = max(0.0, (next_t_ms_f - eff_ms) - _LYRIC_TIMER_PREMISS_MS)
        if _LYRIC_SYNC_LOG:
            log(f"[LYRIC_SYNC] FORCE:SCHEDULE_NEXT song={song_key!r} cur={idx} next={idx+1} LRC_next={next_t_ms_f:.0f}ms eff_now={eff_ms}ms wait_ms_float={wait_ms_f:.2f}")
        _schedule_next_lyric_at(song_key, timeline, trans_timeline, idx + 1, wait_ms_f)


# ── 封面 API（三平台并行搜 URL）──

def _netease_fetch_cover_raw(artist: str, title: str) -> str:
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
            found_artist = " ".join(
                a.get("name", "") for a in (item.get("artists") or []) if isinstance(a, dict))
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


def _netease_fetch_cover(artist: str, title: str) -> str:
    return _fetch_with_t2s_fallback(_netease_fetch_cover_raw, "网易云封面", artist, title)


def _qqmusic_fetch_cover_raw(artist: str, title: str) -> str:
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
            found_artist = ";".join(
                s.get("name", "") for s in (song.get("singer") or []) if isinstance(s, dict))
            if not _artist_match(found_artist, artist):
                continue
            albummid = song.get("albummid") or song.get("albumMid") or ""
            if albummid:
                url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg?max_age=2592000"
                return ("QQ音乐", url)
    except Exception:
        pass
    return None


def _qqmusic_fetch_cover(artist: str, title: str) -> str:
    return _fetch_with_t2s_fallback(_qqmusic_fetch_cover_raw, "QQ音乐封面", artist, title)


# ══ v6.72 按要求彻底移除酷狗封面源（仅保留 QQ音乐 封面源，根治 SSL verify=False 警告源头）══
#    之前虽然 _fetch_cover_bg funcs=[_qqmusic_fetch_cover] 没有调酷狗，但函数残留=未来误加风险+代码垃圾，现在硬删除


def _fetch_cover_bg(artist: str, title: str, song_key: str):
    """后台线程：只查 QQ音乐 封面 URL，拿到就写，切歌自动丢弃，全异常静默不崩溃
    ══ v6.63：按要求只留 QQ 封面，网易云 + 酷狗封面搜索移除（去掉酷狗 SSL verify=False WARN 源头）
    ══ v6.73 P0-2：复用全局 _LYRIC_EXECUTOR 常驻池，不临时建池；单 future 不用 as_completed，直接等单个结果"""
    try:
        result = None
        # ══ v6.73 P0-2：单任务（只有 QQ 封面）直接 submit，不用 as_completed/临时池
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
            if _SMTC_STATE.get("song") != song_key:
                return
            cur_cover = _SMTC_STATE.get("cover", "")
            if not cur_cover or cur_cover.startswith("data:"):
                _SMTC_STATE["cover"] = url
                log(f"封面: {name} → {url[:70]}...")
        _cache_put_cover(artist, title, name, url)  # ══ 写持久化缓存 ══
    except Exception as e:
        log(f"WARN: 封面后台线程异常(已吞): {e}")


# ── 歌词格式化 + Tick ──

def _format_lyric_line(text: str, trans_text: str = "") -> str:
    """歌词格式: 粗体主词 + 斜体括号 + 翻译代码块换行
    ⚠️ 必须保证非空 text 至少产生一个可见 part，不然会被 tick 的空过滤当成无效句。"""
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
        # ══ 防止纯括号句 (间奏)/(啦啦啦) 被替换成空字符串而丢句 ══
        if not line_parts:
            # 连括号内容都没有？直接整句粗体兜底
            line_parts.append(f"**{text.strip()}**")
        parts.append(" ".join(line_parts))
    if trans_text:
        parts.append(f"\n`{trans_text}`")
    return "".join(parts)


def tick_lyric():
    """每次 tick 做一次: SMTC 轮询 + 歌词推算 + 播放/暂停状态切换提示"""
    global _last_lyric_raw, _last_trans_raw, _last_lyric_idx, _last_trans_idx, _lyric_pending, _last_playing_state, _last_emit_wall_ts_ms
    # v7.02: 切歌误判暂停去抖 / 强制媒体重读 标志
    global _pause_suspect_since, _force_media_recheck
    # v6.6：tick_lyric L2074 顺手清理残留标记时 会写入 _catchup_thread_song / 读 _catchup_thread
    #      所以必须 global，否则 Python 当局部变量处理 → 赋值前引用 UnboundLocalError
    global _catchup_thread_song, _catchup_thread

    poll_smtc()

    with _state_lock:
        song = _SMTC_STATE.get("song", "")
        if not song:
            _last_playing_state = None
            return
        playing = _SMTC_STATE.get("playing", False)
        timeline = _SMTC_STATE.get("timeline", [])
        trans_timeline = _SMTC_STATE.get("trans_timeline", [])
        # ══ v7.00：统一走本地单调时钟，完全脱离 SMTC 上报频率 ══
        eff_ms = get_local_eff_ms()
        # 切歌初期: 强制显示 ▶ 歌名（只发一次）
        since_change = time.time() - _last_song_change_ts
        if since_change < LYRIC_SONG_INIT_MS / 1000.0 and _last_lyric_raw == "" and not timeline:
            pass

        # ── 切歌：重置索引去重标记 + 取消旧歌词定时器 + 取消正在补发的歌词 + 打印 timeline 总行数 ──
        #    ══ 修B关键修复：如果 _catchup_thread_song == song（同一首歌已经在跑补位），
        #       绝对不能 cancel 补位 / 重置 _last_lyric_idx，否则补位到一半的 idx = 5 被重置回 -1，
        #       下一轮 tick 再推算 idx=27 就「跳句 6..26 全跳过」造成漏句乱序。
        #       只在「确实切到不同歌曲」或 _catchup_thread_song 为空（没有补位）时才执行切歌重置。══
        if song != tick_lyric._last_song_sent:
            _cancel_all_lyric_timers(song)  # 新歌进来，旧 Timer 全部作废
            same_song_catchup_running = (_catchup_thread_song == song)
            if not same_song_catchup_running:
                _cancel_catchup(song)
                _last_lyric_idx = -1
                _last_trans_idx = -1
                _last_lyric_raw = ""
                _last_trans_raw = ""
                _last_emit_wall_ts_ms = 0.0
            # ══ v6.64 去重 / v6.65 修条件永远 False 的 bug：
            #    「切歌提示」在 poll_smtc 检测到切歌那一瞬间就立刻打印了（严格先于 80ms tick 调度），
            #    tick_lyric 这边如果在 800ms 内检测到 _smtc_song_intro_emitted_at >= _last_song_change_ts
            #    （也就是上一次切歌时刻之后立刻打了提示）→ 跳过重复打切歌提示。
            #    只有极端 case（比如 tick 检测切歌比 poll_smtc 先到，或进程重启的首轮 tick）才回退到这里补发一条切歌提示 ══
            now = time.time()
            intro_recently_emitted = (_smtc_song_intro_emitted_at > 0 and (now - _smtc_song_intro_emitted_at) < 0.8 and _smtc_song_intro_emitted_at >= _last_song_change_ts - 0.2)
            formatted = f"**\u25b6 {song}**"
            ts = now
            total = len(timeline)
            ttotal = len(trans_timeline)
            if intro_recently_emitted:
                pass
            else:
                # v6.79：统一走 _stage_lyric_event（防溢出覆盖丢句）
                _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                log(f"切歌提示 [{ts:.3f}] offset={_LYRIC_OFFSET_MS}ms: {song} | timeline={total} 行 + 翻译={ttotal} 行" + ("" if not same_song_catchup_running else " | [补位进行中 跳过reset]"))
            tick_lyric._last_song_sent = song

        # ── 播放/暂停状态切换提示 ──
        # v7.02: 加去抖，修复 Kugou/SMTC「切歌被误判成 暂停→继续播放」。
        #         Kugou 切到下一首时，旧会话常先短暂报 paused（几百 ms）再 resumed，且位置重置回起部。
        #         处理：检测到停止播放 → 记录 _pause_suspect_since + 立即取消定时器/补位（防卡顿），
        #         但不立刻上报"已暂停"；若去抖窗口 _PAUSE_DEBOUNCE_S 内恢复播放 → 判定为切歌，
        #         静默跳过，并置 _force_media_recheck 让 poll_smtc 重读媒体确认真实新歌；
        #         持续暂停超窗口才确认真实暂停上报。
        _now_p = time.time()
        # ── v7.03 修复 v7.02 bug：真实暂停永远无法上报「已暂停」 ═─
        #    v7.02 在首次检测到停止播放时立即 `_last_playing_state = playing(False)`，
        #    此后每轮 tick playing 恒为 False，外层状态切换 guard `_last_playing_state != playing`
        #    恒为 False → 去抖超时分支成为死代码，「真实暂停 >窗口」永远不发提示。
        #    修复：把窗口到期判断独立出来，用 _pause_suspect_since 单独轮询，无需依赖状态再次切换。
        if not playing and _pause_suspect_since is not None:
            if (_now_p - _pause_suspect_since) >= _PAUSE_DEBOUNCE_S:
                # 窗口内一直未恢复 → 确认为真实暂停
                _pause_suspect_since = None
                _last_playing_state = playing
                if since_change > 0.3:
                    formatted = "**\u23f8 已暂停**"
                    log_text = "⏸ 已暂停"
                    ts = time.time()
                    _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                    # ══ v6.5 修：暂停/继续 状态提示 绝对不能 reset _last_lyric_idx / _last_trans_idx！
                    #    只把 raw 置状态占位符，避免下一句相同歌词被误判重复；last 保留原值推进。═
                    _last_lyric_raw = f"__STATE__{playing}__"
                    _last_trans_raw = ""
                    _last_emit_wall_ts_ms = 0.0  # ══ v6.67 状态切换后下一句为 FIRST，避免暂停时长污染gap
                    log(f"状态提示 [{ts:.3f}] offset={_LYRIC_OFFSET_MS}ms: {log_text}")
                # 已确认真实暂停：停在暂停态，跳过后方歌词推进逻辑
                return

        if _last_playing_state is not None and _last_playing_state != playing:
            if not playing:
                # 暂停方向：取消所有正在等的定时器 + 中断补位（恢复时 tick 兜底会重新挂）
                _cancel_all_lyric_timers(song + "#paused")
                _cancel_catchup(song + "#paused")
                if _pause_suspect_since is None:
                    # 停止播放首次出现 → 记起点，等去抖窗口（不在此处上报，等上方独立轮询确认）
                    _pause_suspect_since = _now_p
                    _last_playing_state = playing
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_PROFILE] PAUSE_DEBOUNCE 检测到停止播放，等待 {_PAUSE_DEBOUNCE_S}s 确认（切歌或真实暂停）")
                    return
                # 已有可疑起点：窗口到期确认已由上方独立块处理，这里仅同步状态并静默等待
                _last_playing_state = playing
                return
            else:
                # playing，且之前是停止播放状态 → 恢复
                _resumed_quick = (_pause_suspect_since is not None and (_now_p - _pause_suspect_since) < _PAUSE_DEBOUNCE_S)
                _pause_suspect_since = None
                _last_playing_state = playing
                if _resumed_quick:
                    # 去抖窗口内即恢复 → 大概率是切歌，不打印"继续播放"，置强制媒体重读确认真实新歌
                    _force_media_recheck = True
                    if _LYRIC_SYNC_LOG:
                        log(f"[LYRIC_PROFILE] PAUSE_DEBOUNCE 暂停<{int(_PAUSE_DEBOUNCE_S*1000)}ms 即恢复 → 判定为切歌，静默跳过 已暂停/继续播放 上报")
                    return
                if since_change > 0.3:
                    formatted = "**\u25b6 继续播放**"
                    log_text = "▶ 继续播放"
                    ts = time.time()
                    _stage_lyric_event(formatted, f"{formatted}|{ts:.3f}")
                    _last_lyric_raw = f"__STATE__{playing}__"
                    _last_trans_raw = ""
                    _last_emit_wall_ts_ms = 0.0
                    log(f"状态提示 [{ts:.3f}] offset={_LYRIC_OFFSET_MS}ms: {log_text}")
        _last_playing_state = playing

        if not playing and eff_ms == 0:
            # v6.66 [LYRIC_SYNC]: 每轮 tick 即使 return 也打进度（用户要求所有日志全开，方便定位卡死/静音）
            if _LYRIC_SYNC_LOG:
                try:
                    with _state_lock:
                        smtc_prog = _SMTC_STATE.get("progress_ms", 0)
                        smtc_song = _SMTC_STATE.get("song", "")
                    dt_clamp = int((time.time() - _last_smtc_ts) * 1000) if _last_smtc_ts > 0 else 0
                    log(f"[LYRIC_SYNC] tick:skip(not playing + eff=0) song={smtc_song!r} eff_ms={eff_ms} smtc_progress_ms={smtc_prog} dt_clamp_from_last_smtc={dt_clamp}ms timeline={len(timeline)}")
                except Exception:
                    pass
            return
        idx, cur = _current_lyric_idx(timeline, eff_ms)
        trans_idx, cur_trans = _current_lyric_idx(trans_timeline, eff_ms)
        # v6.66 [LYRIC_SYNC]: 每轮 tick 打 eff_ms 分解 —— 「一会快一会慢」本质看 SMTC.progress_ms 是否跳变/本地墙钟推算是否正确
        if _LYRIC_SYNC_LOG and timeline:
            try:
                with _state_lock:
                    smtc_prog = _SMTC_STATE.get("progress_ms", 0)
                lrc_t_ms = (timeline[idx][0] * 1000 + _LYRIC_OFFSET_MS) if (idx is not None and idx >= 0 and idx < len(timeline)) else None
                drift_ms = (eff_ms - lrc_t_ms) if lrc_t_ms is not None else None
                dt_clamp = int((time.time() - _last_smtc_ts) * 1000) if _last_smtc_ts > 0 else 0
                log(f"[LYRIC_SYNC] tick:eff_decomp song={song!r} eff_ms={eff_ms} = smtc_progress_ms={smtc_prog} + wall_clamp_from_last_smtc={dt_clamp}ms + OFFSET={_LYRIC_OFFSET_MS}ms | idx={idx}/{len(timeline)} LRC_t_ms={lrc_t_ms} drift_ms={drift_ms:.0f if drift_ms is not None else 'None'} last={_last_lyric_idx}")
            except Exception:
                pass

        # ══ v6.6 加强：补位线程 真正 alive 时 才静音 tick
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
                # v7.02 fix: 只有 补位线程对象已彻底为空(ct is None) 才清理标记，防止误清启动窗口。
                # 仅凭 is_alive()=False 判断"线程已退出"不成立：线程刚创建、或 join 旧线程期间
                # is_alive()=False 但线程即将 start，此刻清标记会让补位被误判中断。
                _ct = _catchup_thread
                if _catchup_thread_song == song and _ct is None:
                    _catchup_thread_song = ""
        elif catchup_alive and _catchup_thread_song == song:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:muted_by_catchup_alive song={song!r} idx={idx}/{len(timeline)} eff_ms={eff_ms}")
            return

        # ══ v6.66 严格模式 / 夹逼改造：
        #    用户要求「严格按歌曲进度和 LRC 时间戳输出」。原逻辑 只要 idx>last+1 就强制 last+1 —— 这会人为把进度变慢（就是你看到的"一会慢"）。
        #    新逻辑：
        #      - STRICT 且 drift(eff-LRC[idx].t) <= MAX_DRIFT(250ms)：按原始 idx 直接推进（严格按进度）
        #      - 超过 MAX_DRIFT（seek/进度跳了 ≥0.25s）：夹逼 last+1 继续保证顺序不漏；且打一条 ERROR 日志说明被夹逼了多少
        clamped_by_drift = False
        clamped_original_idx = None
        if idx != -1 and _last_lyric_idx >= 0 and idx > _last_lyric_idx + 1:
            lrc_t_ms_raw = timeline[idx][0] * 1000 + _LYRIC_OFFSET_MS if idx < len(timeline) else None
            drift_raw = (eff_ms - lrc_t_ms_raw) if lrc_t_ms_raw is not None else (_LYRIC_MAX_DRIFT_MS + 1)
            use_strict = (
                _LYRIC_STRICT_SYNC and lrc_t_ms_raw is not None and abs(drift_raw) <= _LYRIC_MAX_DRIFT_MS
            )
            if use_strict:
                # 严格模式：误差<=250ms 放行，不夹逼，严格按 LRC 时间戳发 idx
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] tick:STRICT_PASS_SKIP_CLAMP song={song!r} idx={idx} last={_last_lyric_idx} eff_ms={eff_ms} LRC_t_ms={lrc_t_ms_raw} drift={drift_raw:.0f}ms <= MAX_DRIFT={_LYRIC_MAX_DRIFT_MS:.0f}ms → 不夹逼 按原始idx推进")
            else:
                clamped_by_drift = True
                clamped_original_idx = idx
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
                    log(f"[LYRIC_SYNC] tick:CLAMPED_BY_DRIFT song={song!r} original_idx={clamped_original_idx} → clamped_to_last+1={idx} last={_last_lyric_idx} eff_ms={eff_ms} LRC[original].t_ms={lrc_t_ms_raw} drift={drift_raw if isinstance(drift_raw,(int,float)) else '?'}ms MAX_DRIFT={_LYRIC_MAX_DRIFT_MS:.0f}ms")
        else:
            # 同步夹逼日志：没夹逼就打一条路径正常
            if _LYRIC_SYNC_LOG and idx != -1 and timeline and _last_lyric_idx >= 0:
                log(f"[LYRIC_SYNC] tick:clamp_check_NOOP song={song!r} idx={idx} last={_last_lyric_idx} diff={idx-_last_lyric_idx} → 直接放行")

        # ══ 修A：严格顺序强保证（tick 侧）—— idx <= last 说明 Timer 已发过 / 有回退，
        #    直接 drop 绝不允许出现「30 发过了，29 又发」这种回退刷屏 ══
        if idx != -1 and _last_lyric_idx >= 0 and idx <= _last_lyric_idx:
            if _LYRIC_SYNC_LOG:
                log(f"[LYRIC_SYNC] tick:DROP_idx<=last song={song!r} idx={idx} last={_last_lyric_idx} eff_ms={eff_ms}")
            return

        # ══ v7.01：tick 侧改「单步推进」为「burst 连发追满当前进度」
        #    没夹逼(clamped_by_drift=False) → 直接从 last+1 连发追到 idx_original（当前 eff_ms 应到的行）
        #    被夹逼(clamped_by_drift=True，drift>MAX_DRIFT 疑似seek/长暂停) → 只推进 last+1 这1步(保持原严格顺序不漏句策略)
        #    单次 tick 最多追 200 句（一首歌不可能 10ms 内真的过 200 句以上，防 seek 刷屏）
        if idx != -1 and idx > _last_lyric_idx:
            if clamped_by_drift:
                target = _last_lyric_idx + 1
                if target >= len(timeline):
                    target = len(timeline) - 1
                if _LYRIC_SYNC_LOG:
                    log(f"[LYRIC_SYNC] tick:BURST_CLAMPED song={song!r} original_idx={clamped_original_idx} → clamped_burst_to_last+1={target} max_catchup=1 (drift>{_LYRIC_MAX_DRIFT_MS:.0f}ms，防止seek乱跳) last={_last_lyric_idx} eff_ms={eff_ms}")
                _burst_catchup_to_idx(song, timeline, trans_timeline, target,
                                      playing=playing, eff_ms_ref=eff_ms, max_lines=1)
            else:
                target = idx
                if _LYRIC_SYNC_LOG and target > _last_lyric_idx + 1:
                    log(f"[LYRIC_SYNC] tick:BURST_CATCHUP song={song!r} from={_last_lyric_idx+1} → to={target} eff_ms={eff_ms} lines={target-_last_lyric_idx}(含空行)")
                _burst_catchup_to_idx(song, timeline, trans_timeline, target,
                                      playing=playing, eff_ms_ref=eff_ms, max_lines=200)
            return


tick_lyric._last_song_sent = ""  # type: ignore


def _lyric_tick_loop():
    """独立线程：每 LYRIC_TICK_MS ms 做一次 SMTC 轮询 + 歌词推算"""
    while True:
        try:
            tick_lyric()
        except Exception as e:
            import traceback
            log(f"歌词tick异常: {e}")
            log(traceback.format_exc())
        # ══ v7.02：用户选择"直接强制对准" → 周期性把本地时钟强制对准 SMTC（酷狗 pos 卡死自动跳过）。
        #    替换 v7.01 被禁用的 _apply_drift_step()（纯单锚点积分中途无校正 → 播到一半对不上）。
        try:
            _clock_force_align()
        except Exception:
            pass
        time.sleep(LYRIC_TICK_MS / 1000.0)


# ── 截屏功能 ──
try:
    from PIL import ImageGrab, Image
    import io
    import base64
    HAS_SHOT = True
except ImportError:
    HAS_SHOT = False
    log("WARN: Pillow 未安装, 截屏功能不可用 (pip install Pillow)")


def _take_screenshot() -> str:
    """截取全屏, 返回 base64 JPEG 字符串"""
    if not HAS_SHOT:
        return ""
    try:
        img = ImageGrab.grab(all_screens=True)
        w, h = img.size
        if w > 1920:
            ratio = 1920 / w
            img = img.resize((1920, int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
        log(f"截屏完成: {img.size[0]}x{img.size[1]}, {len(b64)} chars")
        return b64
    except Exception as e:
        log(f"截屏错误: {e}")
        return ""


# ── TCP 上报 ──

_last_good_music = {}

def _connect_one(port, ip):
    """══ v6.74 P1-3：返回 socket 或 None；连接时设置 keepalive/nodelay/timeout
       注意：socks 字典在上层 connect_tcp/_reconnect_port 里被包装成 {sock,buf,last_recv_ts,last_send_ts}"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            import struct
            _t5 = struct.pack("ll", 5, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, _t5)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, _t5)
        except Exception:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # ══ v6.74 P1-3：SO_KEEPALIVE + TCP_KEEPIDLE/KEEPINTVL/KEEPCNT（Windows 用 IOCtl 或 setsockopt，异常降级跳过）══
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        except Exception:
            pass
        try:
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                sio_keepalive_vals = (wintypes.DWORD * 3)(1, 15000, 5000)
                try:
                    sock.ioctl(socket.SIO_KEEPALIVE_VALS, sio_keepalive_vals)
                except Exception:
                    pass
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
    """══ v6.74 P1-3：返回 dict[port] = {sock, buf, last_recv_ts, last_send_ts}（不再裸 socket）"""
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
    socks: "dict[int, dict]" = {}
    while True:
        for port in PORTS:
            if port in socks and socks[port].get("sock") is not None:
                continue
            s = _connect_one(port, ip)
            if s:
                now = time.time()
                socks[port] = {
                    "sock": s,
                    "buf": bytearray(),
                    "last_recv_ts": now,
                    "last_send_ts": now,
                }
            else:
                time.sleep(1)
        if all(p in socks and socks[p].get("sock") is not None for p in PORTS):
            break
        log(f"TCP: 等待所有端口就绪 ({len([p for p in PORTS if p in socks and socks[p].get('sock')])}/{len(PORTS)})，3s 重试...")
        time.sleep(3)
    return socks


def _reconnect_port(port, ip, socks_dict):
    """══ v6.74 P1-3：关闭旧连接→重连→重建 {sock,buf,last_recv_ts,last_send_ts} 结构"""
    try:
        old_entry = socks_dict.get(port) or {}
        old_sock = old_entry.get("sock")
        if old_sock:
            try:
                old_sock.close()
            except Exception:
                pass
    except Exception:
        pass
    while True:
        s = _connect_one(port, ip)
        if s:
            now = time.time()
            socks_dict[port] = {
                "sock": s,
                "buf": bytearray(),
                "last_recv_ts": now,
                "last_send_ts": now,
            }
            return s
        log(f"TCP: {SERVER}:{port} 重连失败，3s 重试...")
        time.sleep(3)


def _smtc_drain_events_and_poll_light():
    """══ v6.74 P1-1：排空 _smtc_event_queue，若有事件 → 唤醒 SMTC worker 去 poll（v7.06 不再同步 poll）。
    主循环 / tick 永不直接调用 winrt，避免 SMTC RPC 卡死整进程。"""
    got_event = False
    try:
        while True:
            try:
                _smtc_event_queue.get_nowait()
                got_event = True
            except queue.Empty:
                break
    except Exception:
        got_event = False
    if got_event:
        try:
            _smtc_poll_wake.set()   # 通知 worker 立即 poll_smtc()
        except Exception:
            pass
    return got_event


def _smtc_poll_worker_loop():
    """══ v7.06 SMTC 唯一轮询线程：这里的 winrt RPC 即使卡死，也只卡本线程。
    轮询节奏：
      - 每轮先 poll_smtc() 一次（写 _SMTC_STATE 快照 + 触发切歌/锚定/歌词）；
      - 然后用 _smtc_poll_wake.wait(_smtc_poll_idle_s) 等唤醒：有事件(切歌/进度/媒体变更)立即再来一轮，
        无事件则按 idle 间隔兜底轮询（保证不靠事件也能持续读进度）。"""
    first = True
    while True:
        try:
            if first:
                # v7.10：启动首轮**立即 poll**，不等兜底 1s——否则"冷启动播一半"的场景下，
                #       会话检测被这次 1s sleep 拖慢，切歌检测晚约 1 秒，歌词整体滞后 ~1s。
                first = False
                _smtc_poll_wake.clear()
            else:
                _smtc_poll_wake.wait(_smtc_poll_idle_s)
                _smtc_poll_wake.clear()
            _kugou_ensure_probe()        # 顺带确保酷狗真实进度线程在跑（幂等）
            _smtc_poll_dead = not poll_smtc()
        except Exception:
            _smtc_poll_dead = True
            time.sleep(_smtc_poll_idle_s)


def _start_smtc_poll_worker():
    """启动 SMTC worker 线程（幂等）。"""
    global _smtc_poll_worker
    if _smtc_poll_worker is not None and _smtc_poll_worker.is_alive():
        return
    _smtc_poll_worker = threading.Thread(target=_smtc_poll_worker_loop, name="smtc_poll", daemon=True)
    _smtc_poll_worker.start()


def run():
    global _last_good_music, _lyric_pending
    _load_local_config()
    _load_cache()
    # ══ v7.10：尽早预热 UIA + 启动酷狗探针 ══
    #    冷启动"播一半"时，tick 首查切歌会现场做 UIA 首次初始化（加载 UIAutomationCore.dll +
    #    首次 ControlFromHandle ≈ 1s），把那 1s 压进歌词路径就造成整体滞后~1090ms。
    #    这里在 run() 最开头(先于 SMTC 会话查询/歌词 tick)一次性预加载 uiautomation 并让探针
    #    线程后台抢跑首定位，等歌词首查时首次初始化已完成、快照就绪 → 即时对齐。
    try:
        _uia_apply_fast()
        _kugou_ensure_probe()
    except Exception:
        pass
    log("=== 探针版本标记: pc_status_reporter v7.23 [TitleDrivesCut] ===")  # 确认跑的是新版探针
    log(f"目标端口: {PORTS}")
    log(f"本地配置文件: {_LOCAL_CFG_PATH}")
    log(f"  - 歌词 ms 偏移: {_LYRIC_OFFSET_MS}ms (正=延后 负=提前, CMD:OFFSET_ADD/SET/RESET/GET 在线调整; v7.06 方向键 ←提前/→延后 每按{_KEY_ADJ_STEP_MS}ms,长按连续)")
    log(f"本地缓存: {_LOCAL_CACHE_PATH} (LRU {_CACHE_LRU_LIMIT} 首)")
    log(f"  - 当前缓存: 歌词 {len(_CACHE['lyrics'])} 首 + 封面 {len(_CACHE['covers'])} 首")
    log(f"媒体检测: Windows SMTC (事件驱动: SessionsChanged/PlaybackInfoChanged/MediaPropertiesChanged + 1s兜底) + P1-2 5点滑窗最小二乘滤波校准(0.6预测+0.4实测, >3s seek重置)")
    log(f"歌词: 本地缓存优先 → P1-5 阶梯S1(0~1.5s) variants[0]×(LRCLIB精准+网易云+QQ)=3条首发 → S1未命中追加剩余variants+LRCLIB模糊全部 → 命中窗口内MIN_WAIT_S=1.5s收集所有HIT按评分选最优")
    log(f"  - 评分: _score_lyric_match(字符重合度+duration差<15%+0.3行数10~200+0.2来源LRCLIB+0.1网易云+0.05QQ+0.05)")
    log(f"封面: 本地缓存优先 → SMTC 缩略图(Dispose防泄漏) → QQ音乐 单平台搜 300x300 URL 覆盖")
    log(f"状态提示: 播放/暂停切换即时上报 ▶继续 / ⏸暂停")
    log(f"TCP: P1-3 dict[port]={{sock,buf,last_recv_ts,last_send_ts}} 粘包拆包; SO_KEEPALIVE+TCP_KEEPIDLE/KEEPINTVL/KEEPCNT保活; now-last>30s主动发 HEARTBEAT {{ts}}")
    log(f"P0 稳定性: 非阻塞日志(80%高水位激进丢诊断+零stderr同步写) + 全局常驻线程池(shutdown永不wait) + DXGI/SMTC句柄finally兜底 + 5min健康自检(句柄>3000/线程>50/队列>60% 告警)")
    log(f"P1-4 HTTP: 全局requests.Session(连接复用↓30%握手) + 双层硬超时(软+线程级KILL) + 单源熔断3次失败/5min窗口秒跳")
    if HAS_GPU: log(f"GPU 监控: 启用 ({_GPU_NAME})")
    if HAS_VOLT: log("电压监控: 启用 (OpenHardwareMonitor WMI)")
    if not HAS_WIN32: log("WARN: pywin32/psutil 未安装，仅基础信息")

    _ensure_smtc_loop()
    # ══ v7.07 P0 C-1：去掉同步 UIA 首探（治"开头卡 7 秒"）══
    #    旧版这里同步调 _kugou_probe_once()，其内部 uiautomation 首次遍历整棵控件树能卡 ~7s，
    #    把 main 上报循环进 while 前就拖住 7s。现在只启动后台探针线程：它带 COM 初始化，启动即
    #    （async）首探，~1s 内填好真实进度快照；首锚让 2s 一次 _clock_force_align 快速校正即可。
    try:
        _kugou_ensure_probe()
    except Exception:
        pass
    threading.Thread(target=_lyric_tick_loop, daemon=True).start()
    try:
        _start_sys_snapshot_worker()   # v7.07 P0 C-2：WMI 电压/GPU 采集独立线程，上报线程只读快照
        _start_fps_worker()    # v7.07 P0 B：DXGI FPS 探测独立线程，上报线程只读快照
        _start_keyboard_adjust()   # v7.06 方向键微调歌词偏移（←提前 / →延后）
    except Exception:
        pass

    socks = connect_tcp()
    ip = socket.getaddrinfo(SERVER, PORTS[0], socket.AF_INET, socket.SOCK_STREAM)[0][4][0]

    while True:
        try:
            now_main = time.time()

            # ══ v6.74 P1-3 应用层心跳：每主循环开始时判断 now - last_recv_ts > 30 或 now - last_send_ts > 30 → 发 HEARTBEAT
            for port in list(socks.keys()):
                entry = socks.get(port) or {}
                sk = entry.get("sock")
                if sk is None:
                    continue
                last_r = entry.get("last_recv_ts", 0.0) or 0.0
                last_s = entry.get("last_send_ts", 0.0) or 0.0
                need_hb = False
                if (now_main - last_r) > 30.0 or (now_main - last_s) > 30.0:
                    need_hb = True
                if need_hb:
                    try:
                        hb_msg = f"HEARTBEAT {int(now_main)}\n"
                        try:
                            sk.settimeout(5.0)
                        except Exception:
                            pass
                        sk.sendall(hb_msg.encode("utf-8"))
                        entry["last_send_ts"] = time.time()
                        socks[port] = entry
                    except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                        log(f"TCP: {SERVER}:{port} 心跳发送断开，重连...")
                        _reconnect_port(port, ip, socks)
                    except Exception:
                        pass

            proc_info, title, proc = get_window_title()
            fps = _read_fps_snapshot()   # v7.07: 只读快照，DXGI 探测已移到独立 daemon 线程，避免原生调用卡死上报线程
            player = detect_music_player()

            music = {}
            with _state_lock:
                if _SMTC_STATE.get("song"):
                    eff_ms_for_music = get_local_eff_ms()
                    eff_ms = eff_ms_for_music
                    music = {
                        "song": _SMTC_STATE["song"],
                        "cover": _SMTC_STATE["cover"],
                        "duration": _SMTC_STATE["duration_str"],
                        "progress_ms": eff_ms_for_music,
                        "raw_progress_ms": eff_ms,
                        "lyric_offset_ms": _LYRIC_OFFSET_MS,
                        "playing": _SMTC_STATE["playing"],
                        "hasSong": _SMTC_STATE["hasSong"],
                        "lyric_line": _SMTC_STATE.get("lyric_line", ""),
                        "player": player,
                    }
                    # ══ v6.81 P0 自愈：消费条件兼容「_lyric_pending=True 但槽为空串」错位状态
                    #    旧版本 v6.79 切歌时只清槽不 pending，导致错位后条件永远 False 永不消费。
                    #    正常路径 (A)：pending=True 且槽非空 → 读槽 → 发给服务端 → 消费 → 补槽
                    #    错位自愈 (B)：pending=True 但槽是空串 → 只清 pending → 仍然补槽（让队列里
                    #       的 intro/歌词回到槽里，pending 重新置 True，下一轮走正常路径 A）
                    cur_slot_event = _SMTC_STATE.get("lyric_event") or ""
                    if _lyric_pending and cur_slot_event:
                        # 路径 A：正常消费
                        music["lyric_event"] = cur_slot_event
                        _lyric_pending = False
                    elif _lyric_pending and not cur_slot_event:
                        # 路径 B：错位自愈，不发 lyric_event，只清 pending 让补槽逻辑把队列首项填回槽
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
                                    _SMTC_STATE["lyric_line"] = q_line
                                    _SMTC_STATE["lyric_event"] = q_event
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
            if title: data["window"] = title
            if proc: data["app"] = proc
            if proc_info:
                data["app_detail"] = proc_info
                data["app_handles"] = proc_info.get("proc_handle_count", 0)
                data["app_mem_mb"] = round(proc_info.get("proc_mem_rss", 0) / 1048576, 1)
            if fps: data["fps"] = fps
            if music: data["music"] = music

            if not hasattr(run, '_dbg_ts'):
                run._dbg_ts = 0  # type: ignore
            if time.time() - run._dbg_ts > 30:  # type: ignore
                log(f"载荷音乐: song={music.get('song','')[:30]} lyric_event={bool(music.get('lyric_event'))} player={player} cover={bool(music.get('cover'))}")
                run._dbg_ts = time.time()  # type: ignore

            sys_info = _read_sys_snapshot()   # v7.07 P0 C-2: 只读快照，WMI 采集已在独立 daemon 线程
            data.update(sys_info)

            payload = json.dumps(data, ensure_ascii=False) + "\n"
            payload_bytes = payload.encode("utf-8")
            for port in list(socks.keys()):
                entry = socks.get(port) or {}
                sk = entry.get("sock")
                if sk is None:
                    continue
                try:
                    try:
                        sk.settimeout(5.0)
                    except Exception:
                        pass
                    sk.sendall(payload_bytes)
                    entry["last_send_ts"] = time.time()
                    socks[port] = entry
                except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                    log(f"TCP: {SERVER}:{port} 发送断开，重连...")
                    _reconnect_port(port, ip, socks)

            # ══ v7.01 burst drain：如果 lyric_event_queue 还有积压歌词（tick/Timer burst 连发场景），
            #    本轮立刻逐句"小包单发"清队列，不等下一次心跳。
            #    协议完全不变（服务端不用改）— 仍然每条 lyric_event 一个独立 TCP 行/json，
            #    用精简 payload（不带 sys_info/window/app_detail），降低序列化/带宽开销。
            # ── 关键时序：主心跳路径A消费完槽后，一定会"补槽1条并置pending=True"再发正常心跳，
            #    所以这里进循环时 pending 就是 True（代表补好的第1条待发），要先消费槽，不要 abort。
            BURST_DRAIN_LIMIT = 200
            burst_sent_n = 0
            # 初始估算总待发：queue剩的 + 槽里可能还剩的 1 条
            burst_queue_expected = len(_lyric_event_queue) + (1 if _lyric_pending else 0)
            while burst_sent_n < BURST_DRAIN_LIMIT:
                # ── Step 1：先消费槽（若 pending 且槽非空）→ 发包
                consumed_this_round = False
                if _lyric_pending:
                    b_event = _SMTC_STATE.get("lyric_event") or ""
                    b_line = _SMTC_STATE.get("lyric_line", "")
                    if not b_event:
                        # pending=True 但槽空串 → 错位自愈，清 pending 进入补槽步骤
                        _lyric_pending = False
                    else:
                        # 正常消费：发包 + 清 pending
                        _lyric_pending = False
                        burst_music = {}
                        if music:
                            burst_music.update(music)
                            burst_music.pop("lyric_event", None)
                        burst_music["lyric_line"] = b_line
                        burst_music["lyric_event"] = b_event
                        b_data = {
                            "hostname": socket.gethostname(),
                            "music": burst_music,
                        }
                        b_payload = json.dumps(b_data, ensure_ascii=False) + "\n"
                        b_bytes = b_payload.encode("utf-8")
                        send_fail = False
                        for port in list(socks.keys()):
                            entry = socks.get(port) or {}
                            sk = entry.get("sock")
                            if sk is None:
                                continue
                            try:
                                try:
                                    sk.settimeout(5.0)
                                except Exception:
                                    pass
                                sk.sendall(b_bytes)
                                entry["last_send_ts"] = time.time()
                                socks[port] = entry
                            except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                                send_fail = True
                                log(f"TCP(burst): {SERVER}:{port} 发送断开，重连...")
                                _reconnect_port(port, ip, socks)
                        # 哪怕重连也先算这轮消费过（重连也已发），只当真·全断再减
                        if not (send_fail and not any((socks.get(p) or {}).get("sock") is not None for p in list(socks.keys()))):
                            burst_sent_n += 1
                            consumed_this_round = True
                # ── Step 2：现在 pending 一定是 False（要么被 Step1 清了，要么错位自愈清了）
                #           从 queue 补 1 条回到槽，pending=True，下一轮 while 头消费
                if _lyric_pending:
                    # 理论不会到这里，保险：pending=True 就停止（防止死循环）
                    try:
                        log(f"[LYRIC_PROFILE] BURST_DRAIN_STOP_STILL_PENDING sent={burst_sent_n} remaining_queue={len(_lyric_event_queue)} → pending=True状态异常，停止本轮burst drain")
                    except Exception:
                        pass
                    break
                got_one = False
                try:
                    if _lyric_event_queue:
                        q_line, q_event = _lyric_event_queue.popleft()
                        with _state_lock:
                            _SMTC_STATE["lyric_line"] = q_line
                            _SMTC_STATE["lyric_event"] = q_event
                        _lyric_pending = True
                        got_one = True
                except Exception as _qe3:
                    try:
                        log(f"[LYRIC_PROFILE] BURST_DRAIN_QUEUE_POP_FAIL msg={_qe3!r}")
                    except Exception:
                        pass
                if not got_one:
                    break  # queue 空 → 结束，pending=False 等着下次 tick/stage
            # burst 结束日志（只在实际发过>0条时打）
            if burst_sent_n > 0:
                try:
                    remaining = len(_lyric_event_queue) + (1 if _lyric_pending else 0)
                    if remaining:
                        log(f"[LYRIC_PROFILE] BURST_DRAIN_PARTIAL sent={burst_sent_n}/{burst_queue_expected} 剩余={remaining}(>={BURST_DRAIN_LIMIT}hit上限或中断) → 交给下轮心跳/drain继续")
                    else:
                        log(f"[LYRIC_PROFILE] BURST_DRAIN_DONE sent={burst_sent_n}/{burst_queue_expected} 槽+队列本轮清空")
                except Exception:
                    pass

            # ══ v6.74 P1-3 粘包读取：recv → append buf → while b"\n" in buf: split 完整行 decode 处理，剩余残段保留在 entry["buf"]
            for port, entry in list(socks.items()):
                sk = (entry or {}).get("sock")
                if sk is None:
                    continue
                try:
                    try:
                        sk.settimeout(0.1)
                    except Exception:
                        pass
                    chunk = sk.recv(4096)
                    if chunk:
                        entry_buf = entry.get("buf")
                        if entry_buf is None:
                            entry_buf = bytearray()
                        entry_buf.extend(chunk)
                        entry["buf"] = entry_buf
                        entry["last_recv_ts"] = time.time()
                        while True:
                            buf_ref = entry.get("buf")
                            if not buf_ref:
                                break
                            nl_pos = buf_ref.find(b"\n")
                            if nl_pos < 0:
                                break
                            line_bytes = bytes(buf_ref[:nl_pos])
                            del buf_ref[:nl_pos + 1]
                            entry["buf"] = buf_ref
                            try:
                                cmd = line_bytes.decode("utf-8", errors="replace").strip()
                            except Exception:
                                cmd = ""
                            if not cmd:
                                continue
                            if cmd.startswith("HEARTBEAT"):
                                continue
                            if cmd == "CMD:SHOT":
                                shot_b64 = _take_screenshot()
                                try:
                                    if shot_b64:
                                        resp = f"SHOT:{len(shot_b64)}\n".encode("utf-8") + shot_b64.encode("utf-8")
                                    else:
                                        resp = b"SHOT:0\n"
                                    try:
                                        sk.settimeout(5.0)
                                    except Exception:
                                        pass
                                    sk.sendall(resp)
                                    entry["last_send_ts"] = time.time()
                                    socks[port] = entry
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
                                        try:
                                            sk.settimeout(5.0)
                                        except Exception:
                                            pass
                                        sk.sendall(reply.encode("utf-8"))
                                        entry["last_send_ts"] = time.time()
                                        socks[port] = entry
                                    except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                                        _reconnect_port(port, ip, socks)
                                        break
                            elif cmd.startswith("="):
                                # v7.08: 「=xxx」直接设置歌词偏移毫秒数（等价 OFFSET_SET xxx）
                                log_text, reply = _apply_offset_cmd("SET", cmd[1:].strip())
                                log(log_text)
                                if reply:
                                    try:
                                        try:
                                            sk.settimeout(5.0)
                                        except Exception:
                                            pass
                                        sk.sendall(reply.encode("utf-8"))
                                        entry["last_send_ts"] = time.time()
                                        socks[port] = entry
                                    except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                                        _reconnect_port(port, ip, socks)
                                        break
                except socket.timeout:
                    pass
                except (ConnectionResetError, BrokenPipeError, OSError):
                    _reconnect_port(port, ip, socks)
                except Exception:
                    pass

            _run_health_check(force=False)

            # ══ v6.74 P1-1：1s 兜底 sleep 拆成 10 × 100ms，每段醒来先 drain 事件队列 → 有事件就 poll_smtc() 轻量读进度
            for _seg in range(10):
                # ① 先 drain 事件（若有 SMTC 事件 → 立刻 poll 一次进度/状态，seek/暂停实时响应）
                try:
                    _smtc_drain_events_and_poll_light()
                except Exception:
                    pass
                time.sleep(0.1)

        except Exception as e:
            log(f"ERR: {e}")
            try:
                log(traceback.format_exc())
            except Exception:
                pass
            # 异常兜底分段 sleep（避免 1s 整块卡期间事件堆积）
            try:
                for _seg in range(10):
                    try:
                        _smtc_drain_events_and_poll_light()
                    except Exception:
                        pass
                    time.sleep(0.1)
            except Exception:
                try:
                    time.sleep(1)
                except Exception:
                    pass


def _validate_env():
    """启动前校验：必须显式设 BOT_SERVER / BOT_PC_PORTS / BOT_PC_KEY，任一缺就报错退出。"""
    errs = []
    if not SERVER:
        errs.append("BOT_SERVER（服务器域名/IP）未设置")
    if not PORTS:
        errs.append("BOT_PC_PORTS（逗号分隔端口列表，例：20000,20001）未设置或为空")
    if not AUTH_KEY:
        errs.append("BOT_PC_KEY（与服务器约定的 AUTH 密钥）未设置")
    return errs


if __name__ == "__main__":
    env_errs = _validate_env()
    if env_errs:
        print("══ PC 状态上报：环境变量未配置，启动终止 ══")
        for e in env_errs:
            print(f"  - {e}")
        print("")
        print("请先在 PowerShell 中执行（示例）：")
        print('  $env:BOT_SERVER  = "你的机器人服务器域名或IP"')
        print('  $env:BOT_PC_PORTS = "端口1,端口2"       # 例：20000,20001')
        print('  $env:BOT_PC_KEY   = "与机器人 bot_config 中 pc_status AUTH 相同的密钥"')
        print("")
        sys.exit(1)

    # ══ v7.07 P0 A：根治「卡死 + ^C 无效」══
    #   旧版 run() 直接跑在主线程：主循环里任一原生调用(get_fps/DXGI、win32、winrt、UIA…)阻塞时，
    #   主线程就停在 C 层永远不返回字节码 → Ctrl+C 信号根本没机会被处理 → 表现"卡死 ^C都没用"。
    #   现在把整套采集/上报逻辑放到一个 daemon 工作线程跑，主线程只做最小信号等待循环：
    #     - run() 就算某个原生调用卡住，也只是卡住工作线程，各 worker(歌词/smtc/kugou/fps) 仍独立。
    #     - 主线程始终只 time.sleep(0.2)，sleep 会周期返回处理信号 → Ctrl+C 必然被抢到，永远可退。
    #     - 收到 ^C 先给工作线程 1s 尝试干净退出；仍卡死则 os._exit(0) 强杀（daemon 不阻止退出）。
    import threading as _t
    _core = _t.Thread(target=run, name="pc_reporter_core", daemon=True)
    _core.start()

    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[Ctrl+C] 收到中断，尝试干净退出...")
        _core.join(timeout=1.0)
        if _core.is_alive():
            print("[Ctrl+C] 工作线程仍卡死，强制结束进程 (os._exit)。")
        import os as _os
        _os._exit(0)
