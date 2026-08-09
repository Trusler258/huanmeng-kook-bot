# -*- coding: utf-8 -*-
"""一次性脚本：pc_status_reporter.py v6.76 → v6.77
A) 删 dangling docstring 残段 + 熔断快速路径
B) 删 HARD_TIMEOUT_S
C) 删 _one_attempt_with_hard_timeout 函数包装
D) 调调用改为 _one_attempt(False)
E) 删 ssl_fallback_ok = False
F) backoff 公式改常量
G) 删熔断计数块
H) 剥离 8 处 _circuit_source=X kwarg
I) 标题 v6.76→v6.77
J) CHANGELOG 插入 v6.77 段
K) run() banner v6.76→v6.77
"""
import re, sys
path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
orig_n = len(lines)
changes = []

# A) Delete dangling docstring / circuit-fastpath block: 1-indexed 1878..1890 = 0-indexed 1877..1889 inclusive
del lines[1877:1890]
changes.append('A: del dangling 1877..1889 (0-idx) = 残段docstring+熔断快速路径')

content = ''.join(lines)

# B) Delete HARD_TIMEOUT_S assignment line
old_hard = '    HARD_TIMEOUT_S = float(timeout) + 1.0  # 线程级硬超时：requests.timeout + 1s 强制KILL兜底\n'
content = content.replace(old_hard, '    # v6.77 已移除：线程级硬超时包装（Future.cancel无法杀running，嵌套无收益）\n')
changes.append('B: removed HARD_TIMEOUT_S')

# C) Delete _one_attempt_with_hard_timeout def (between end of _one_attempt return and start of last_reason line)
pat_harddef = re.compile(r'\n    def _one_attempt_with_hard_timeout\(\):.*?(?=\n    last_reason = \"\")', re.DOTALL)
content, nsub = pat_harddef.subn('\n', content, count=1)
changes.append(f'C: removed _one_attempt_with_hard_timeout: nsub={nsub}')

# D) Replace call site
old_call = '''        # ══ v6.73 P1-4：每次请求先走双层超时（_one_attempt_with_hard_timeout）
        try:
            result, should_retry, reason = _one_attempt_with_hard_timeout()
'''
new_call = '''        # ══ v6.77：直接用 requests.timeout（urllib3 socket select 真生效）
        try:
            result, should_retry, reason = _one_attempt(False)
'''
content = content.replace(old_call, new_call)
changes.append('D: call _one_attempt(False) directly')

# E) Delete unused ssl_fallback_ok = False
content = content.replace('        ssl_fallback_ok = False\n', '')
changes.append('E: removed unused ssl_fallback_ok')

# F) backoff formula constant
old_backoff = '''        backoff = _HTTP_RETRY_BACKOFF_BASE * (2 ** attempt)
        if _debug_tag:
            log(f"WARN: {_debug_tag} 请求失败，{backoff:.2f}s 后第{real_retries_performed}/{retries}次重试… 原因: {last_reason}")'''
new_backoff = '''        backoff = _HTTP_RETRY_BACKOFF  # v6.77 常量1.0s退避（不再0.25/0.5/1/2/4指数递增，原空耗7.75s）
        if _debug_tag:
            log(f"WARN: {_debug_tag} 请求失败，{backoff:.2f}s 后第{real_retries_performed}/{retries}次重试… 原因: {last_reason}")'''
content = content.replace(old_backoff, new_backoff)
changes.append('F: backoff → constant _HTTP_RETRY_BACKOFF')

# G) Remove circuit_breaker counting block
pat_circuit_count = re.compile(r'\n    # ══ v6\.73 P1-4 熔断计数：.*?(?=\n    if \(not final_ok\) and _debug_tag:)', re.DOTALL)
content, nsub = pat_circuit_count.subn('\n', content, count=1)
changes.append(f'G: removed circuit_break counting block: nsub={nsub}')

# H) Strip all _circuit_source="..." kwargs
content, nsub = re.subn(r',\s*_circuit_source="[^"]+"(?=\))', '', content)
changes.append(f'H: stripped _circuit_source kwargs: nsub={nsub}')

# I) Header title
content = content.replace('PC 状态上报 v6.76 — 纯 Windows 原生检测',
                          'PC 状态上报 v6.77 — 纯 Windows 原生检测', 1)
changes.append('I: title v6.76→v6.77')

# J) CHANGELOG insert before v6.76 entry
old_v676_start = '══ v6.76 P1-2 致命量纲修复'
new_v677_changelog = (
    '══ v6.77 彻底去熔断 + 请求重试提速（按用户要求：解决「Ice Paper - 心如止水」全源超时搜不到）══\n'
    '  - 强制移除 P1-4 熔断机制：删_CIRCUIT_BREAK状态/3个函数/快速路径return None/8处调用_circuit_source kwarg\n'
    '    避免「3次抖动超时→5min跳过」导致15min搜不到必然存在的歌\n'
    '  - 降重试 5→2 次：单源最多尝试3次，避免5次3.5s软超时叠加T5=18s总超时直接无命中\n'
    '  - 重试退避改常量 1.0s：不再 0.25/0.5/1/2/4 指数递增（原退避合计空耗 7.75s），切歌/抖动更灵敏\n'
    '  - 去线程级硬超时包装：ThreadPoolExecutor的Future.cancel只能杀pending，running HTTP仍卡worker，嵌套无收益\n'
    '    直接依赖 requests timeout（urllib3 底层 socket select 真生效）\n'
    '\n'
    '══ v6.76 P1-2 致命量纲修复'
)
content = content.replace(old_v676_start, new_v677_changelog, 1)
changes.append('J: added v6.77 changelog')

# K) run() banner
old_banner_run = 'log("=== PC 状态上报 v6.76 ('
new_banner_run = 'log("=== PC 状态上报 v6.77 (去熔断+重试2次+常量退避1s+去硬超时 根治Ice Paper超时未命中 + P1-2量纲修复drift超前2~4s/82ms连发 + '
content = content.replace(old_banner_run, new_banner_run, 1)
changes.append('K: run() banner v6.76→v6.77')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
with open(path, 'r', encoding='utf-8') as f:
    new_n = len(f.readlines())
print('=== OK ===')
for c in changes:
    print('  ', c)
print(f'LINES: {orig_n} → {new_n}')
