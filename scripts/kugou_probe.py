# -*- coding: utf-8 -*-
"""
酷狗(kugou.exe) 真实播放进度探针 v1
=====================================
用途：酷狗 SMTC 恒报 0，拿不到真实时间。本探针用 Windows UI Automation
直接读酷狗界面里的"进度条"控件，确认它到底暴露什么：
  - Value/RangeValue 的当前值究竟是"秒"还是"0-100 百分比"
  - 拿不拿得到总时长
跑法：先 `pip install uiautomation`，酷狗开着放歌时执行：
      python kugou_probe.py
"""
import sys, time, re

# ── 找 kugou 主窗口 hwnd ──
def find_kugou_hwnd():
    import win32gui, win32process, psutil
    target_names = ("kugou", "kgmusic", "kgmusic7", "kgmusic8", "kglow")
    found = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            p = psutil.Process(pid)
            name = (p.name() or "").lower()
        except Exception:
            return True
        if any(t in name for t in target_names) or "kgmusic" in (win32gui.GetClassName(hwnd) or "").lower():
            found.append((hwnd, win32gui.GetWindowText(hwnd), p.name(), win32gui.GetClassName(hwnd)))
        return True
    win32gui.EnumWindows(cb, None)
    return found

# ── UIA 遍历，抓进度条/滑块 + 时间文本 ──
def probe():
    wins = find_kugou_hwnd()
    if not wins:
        print("[探针] 没找到 kugou 窗口（确保酷狗正在运行）")
        return
    for hwnd, title, pname, cls in wins:
        print(f"\n[窗口] pid/进程={pname} hwnd=0x{hwnd:X} class={cls} title={title!r}")

    import uiautomation as auto
    results = {}
    for hwnd, title, pname, cls in wins:
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception as e:
            print(f"  [跳过] 0x{hwnd:X} UIA 挂不上: {e}")
            continue
        # 深度遍历，最多 40 层、5000 节点，防止卡死
        samples = []  # (control, name, kind) 采集「进度」Slider 和纯数字文本，稍后隔 4s 重读看递增

        def walk(ctrl, depth=0):
            if depth > 40:
                return
            try:
                ct = ctrl.ControlTypeName or ""
                name = (ctrl.Name or "").strip()
                aid = (ctrl.AutomationId or "").strip()
            except Exception:
                return
            # 重点关注：进度条/滑块，或名字含 进度/时间/播放 的控件，或形如 "mm:ss" 的时间文本
            time_like = bool(re.search(r'^\d{1,3}(:\d{2}){1,2}\.*\d*$', name.strip())) or bool(re.search(r'时间', name))
            interesting = ("progress" in ct.lower() or "slider" in ct.lower()
                           or "进度" in name or "时间" in name or "播放" in name
                           or (name.isdigit() and 0 < len(name) < 6)
                           or time_like)
            if not interesting:
                # 仍继续深入，但只记录有趣节点
                pass
            val = maxv = rv_val = rv_max = None
            try:
                vp = ctrl.GetValuePattern()
                val = vp.Value
            except Exception:
                pass
            try:
                if ctrl.ControlTypeName in ("SliderControl", "ProgressBarControl"):
                    rv = ctrl.GetRangeValuePattern()
                    rv_val, rv_max = rv.Value, rv.Maximum
            except Exception:
                pass
            if interesting:
                cur = (name, ct, aid)
                key = (hwnd, ct, name)
                results[key] = (val, maxv)
                extra = f" | RangeValue={rv_val} | RangeMax={rv_max}" if rv_val is not None else ""
                print(f"  [控件] {ct} | Name={name!r} | AutoId={aid!r} | Value={val!r}{extra}")
                # 采集用于时间递增判断的控件
                if "进度" in name and "Slider" in ct:
                    samples.append((ctrl, name, "slider"))
                elif ct == "TextControl" and name.isdigit() and 0 < len(name) < 6:
                    samples.append((ctrl, name, "digit"))
            # 递归
            if depth < 40:
                try:
                    child = ctrl.GetFirstChildControl()
                    walked = 0
                    while child and walked < 5000:
                        walk(child, depth + 1)
                        try:
                            child = child.GetNextSiblingControl()
                        except Exception:
                            break
                        walked += 1
                except Exception:
                    return
        try:
            walk(root)
        except Exception as e:
            print(f"  [遍历异常] {e}")

        # ── 采样：隔 4s 重读进度条 RangeValue，用单调时钟精测「unit/秒」来判定单位 ──
        slider_ctrl = next((c for c, n, k in samples if k == "slider"), None)
        if slider_ctrl is not None:
            print("  [采样] 精确测速（判定 RangeValue 单位）：")
            try:
                def _read(c):
                    rv = c.GetRangeValuePattern()
                    return float(rv.Value), float(rv.Maximum)
                v0, m0 = _read(slider_ctrl)
                t0 = time.monotonic()
                time.sleep(4)
                t1 = time.monotonic()
                v1, m1 = _read(slider_ctrl)
                dt = t1 - t0
                d_u = v1 - v0
                ups = d_u / dt if dt > 0 else 0
                dur_sec = m0 / 100.0 if abs(ups - 100) < 2 else m0 / ups
                print(f"    v0={v0}  v1={v1}  Δ={d_u:.0f}  dt={dt:.3f}s  → {ups:.2f} unit/秒")
                print(f"    RangeMax={m0}")
                print(f"    ★ 若 unit/秒 ≈ 100 → 单位=厘秒(0.01s)，position_ms=RangeValue×10，duration_ms=RangeMax×10")
                print(f"    ★ 若 unit/秒 ≈ 1000 → 单位=毫秒，position_ms=RangeValue，duration_ms=RangeMax")
                if abs(ups - 100) < 15:
                    print(f"    → 换算建议: position_ms≈{v0*10:.0f}, duration_ms≈{m0*10:.0f}（即歌曲约 {m0/100:.1f} 秒 = {m0/100/60:.2f} 分）")
            except Exception as e:
                print(f"    测速失败: {e}")

    print("\n[探针] 遍历完成。注意看是否有 RangeValue 控件：")
    print("    - 若某进度条 Value 是很大整数（几百~几千）= 直接是「秒」，×1000 即 position_ms，完美！")
    print("    - 若 Value 在 0~100 且 RangeMax=100 = 百分比，还需要总时长才能换算秒。")

if __name__ == "__main__":
    probe()