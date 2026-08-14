# 幻梦 Bot 性能审计报告（Phase 0）

> 审计目标：为 Huanmeng 2.0 重构做前置基线。定位事件循环被阻塞的高风险点、无超时/重试/回退的外部调用，以及外部调用「Timeout / Retry / Fallback / 资源预算」四要素的覆盖现状。
>
> 审计范围：`services/`、`core/`、`modules/`、`bot.py`、`main.py`（含 `scripts/*_status_reporter.py`，仅作参考，非事件循环常驻路径）。
> 方法：基于真实源码逐行核查，非臆测。只产出文档，不改源码。
> 结论等级：🔴 高（阻塞事件循环 / 无超时可挂死） · 🟠 中（缺重试/回退/预算） · 🟡 低（可优化）。

---

## 1. 总览

| 维度 | 现状 | 2.0 目标 |
|---|---|---|
| LLM 调用 | 同步 OpenAI 客户端已用 `run_in_executor` 隔离 ✅；有 `wait_for` 超时 ✅；**无网络层重试** ❌ | Timeout + Retry + Fallback + 预算 |
| Web 搜索 | DeepSeek Responses 走线程池 ✅；**Agent 多源回退是死代码**；`requests` 同步调用分散 | 统一异步 + 熔断 |
| 阻塞式外部调用 | `_calc`(subprocess)、`nasa`、`pgr` 直接在事件循环内执行同步 IO 🔴 | 全量 `to_thread`/`run_in_executor` |
| 磁盘写 on-loop | 每次 LLM 调用、每次发消息都同步写文件 🟡 | 异步/批量刷盘 |
| Playwright | 全部用 `async_playwright` ✅，渲染走队列 + 信号量 ✅；多数无超时/超时不一致 | 统一资源预算 + 超时 |
| 数据库 | **无 sqlite3 使用**（全文件系统 JSON 存储） | — |

关键结论：代码在「LLM 不阻塞事件循环」上做得比较好（`run_in_executor` + `wait_for`），但**事件循环仍有多处被同步 IO 阻塞**；外部调用普遍**缺重试与熔断**；搜索的**多级回退失效**；发送图片下载**无超时**。

---

## 2. 事件循环被阻塞的高风险点（🔴 优先修复）

| 文件:行号 | 问题 | 触发路径 | 损失 |
|---|---|---|---|
| `core/tools.py:262-279` | `_calc` 是 `async def`，内部直接 `subprocess.run(["python3","-c",code], timeout=5)`，**同步阻塞** | FC 智能体循环 → `execute_tool` → `_calc`（tools.py:683） | 每次最多阻塞事件循环 5s；多工具串行时成倍放大 |
| `modules/nasa.py:14` → `services/nasa.py:14-17` | `cmd_nasa`(async) 直接调同步 `get_apod()`，内含 `urllib.request.urlopen(timeout=10)` | `.nasa` 指令 | 阻塞事件循环最多 **10s** |
| `modules/pgr.py:194` → `services/pgr.py:28,41,56` | `_query_best`(async) 直接调同步 `get_bestn_image()`，内含 `urllib.request.urlopen(timeout=15)` | `.pgr` 指令 | 阻塞事件循环最多 **15s** |

> 已正确隔离（✅ 不需要改）：
> - `services/llm.py:404-407` `call_llm` → `run_in_executor` + `wait_for`
> - `services/llm.py:526-528,535-537` `call_llm_with_tools` 同上
> - `modules/web_search.py:467-508` `ds_native_search` → `run_in_executor`
> - `modules/commands.py:982` `cmd_whois` → `run_in_executor`
> - `core/tools.py:564` `read_url` 抓取 → `run_in_executor`

---

## 3. 详细问题清单（文件:行号 问题 严重程度 建议）

### 3.1 LLM 服务（services/llm.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `llm.py:404-407` | 有 `wait_for(timeout)` 超时 ✅，但**网络异常/超时统一返回 `""`，无重试** | 🟠 | 加指数退避重试（如 2 次），超时/5xx 才重试，4xx 不重试 |
| `llm.py:453-461` | 超时仅记日志返回空，调用方只能走 fallback | 🟠 | 超时重试一次；仍失败再降级 |
| `llm.py:416-422` | JSON 空 / `finish=length` 的递归重试无次数上限（虽各 1 次，但可叠加触发） | 🟡 | 统一纳入「单请求总重试预算」 |
| `llm.py:721` | FC 循环 `MAX_ROUNDS=2`（注释写 5），每轮最多 60s，加 json_mode 兜底 → **单消息最坏 ~2×60s+60s，无总预算** | 🟠 | 设「整体 LLM 时间预算」（如 90s）与轮次上限 |
| `llm.py:436-439`→`token_tracker.py:85` | 每次 LLM 调用同步 `open().write()` 追加 token 记录（on-loop 磁盘写） | 🟡 | 内存缓冲 + 定时批量刷盘，或 `to_thread` |
| `llm.py:380` | 每次调用 `_create_client` 新建 OpenAI 客户端（连接不复用） | 🟡 | 全局复用 client / 复用 `httpx` 连接池 |
| `llm.py:320` | `OpenAI(timeout=60.0)` 固定硬超时，与 `wait_for` 的软超时并存，语义重叠 | 🟡 | 统一为单一超时源 |

### 3.2 发送服务（services/sender.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `sender.py:281-288` | 外部图片下载 `aiohttp.ClientSession().get(img_url)` **无 timeout**，失败/慢速 URL 可无限挂起 | 🔴 | 加 `timeout=aiohttp.ClientTimeout(total=10)` |
| `sender.py:422` | `_log_msglog` 同步 `open().write()`，每次发送都 on-loop 写磁盘 | 🟡 | 同 token_tracker，异步/批量刷盘 |
| `sender.py:392,266,289` | `create_asset` / `send` 无显式超时（依赖 khl 库默认），失败仅整体 catch 后发 fallback，**无重试** | 🟠 | 包超时 + 一次重试 |
| `sender.py:314-354` | 发送失败只 fallback 一次到通用文案，无重试/替代通道 | 🟡 | 失败重试 1 次再降级 |

### 3.3 搜索模块（modules/search.py, web_search.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `search.py:50` | `ds_native_search` 有 `wait_for(45)` ✅ | — | 保持 |
| `search.py:56-60` | **回退逻辑失效**：`ds_native_search` 失败/超时后 `result_text` 恒为 `None`，直接返回「暂无搜索结果」，注释声称「回退 Agent 搜索」但该分支从未执行 | 🔴 | 接入 `agent_search`（现为死代码）作为真正的多源回退 |
| `web_search.py:462-464` | `agent_search`（百度+Bing+百科+深度抓取）**仅定义，无任何调用点**（死代码） | 🟠 | 接入回退链 或 删除 |
| `web_search.py:161-254` | `search_baidu/bing/baike` 用同步 `requests`，有 timeout ✅，**无重试/熔断** | 🟠 | 统一 `to_thread` + 熔断（同源连续失败降级） |
| `web_search.py:407-425` | `ThreadPoolExecutor` 深度抓取用了独立线程池，但整体仍在同步 `search()` 内，需确认调用方已 `to_thread` | 🟡 | 若作为回退被 async 调用，包一层 executor |
| `search.py:81-83` | `save_search_memory` 已用 `asyncio.to_thread` ✅ | — | 保持 |

### 3.4 判断模块（modules/judge.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `judge.py:54,70` | 搜索缓存刷盘同步 `open().write()`，由 `flush_search_cache` 触发；`_save_search_cache_to_disk` 若在主流程被调则 on-loop 写盘 | 🟡 | 确认刷盘时机放后台；否则 `to_thread` |
| `judge.py:159` | `_load_keywords` 每次 `init_keywords` 同步读 toml（仅启动/reload 时） | 🟡 | 可缓存 |
| `judge.py:322` | `should_respond` 三级判断串行/并行调用 LLM，超时已在 `call_llm` 层兜底 ✅ | — | 无 |

### 3.5 记忆模块（modules/memory.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `memory.py:99,112,332` | `load_memories/save_memories_to_file/_read_tail_lines` 全为同步文件 IO，`get_top_memories`/`search_msglog` 若在 async 消息路径被同步调用则 on-loop 阻塞 | 🟠 | 记忆读写在 async 路径用 `to_thread` |
| `memory.py:283,297` | `_compress_with_deepseek` 用 `asyncio.create_task` 后台执行 ✅，但内部 `append_memory`→同步读写，且 `asyncio.create_task` 无异常统一收集（`_compress_with_deepseek` 内部已 try 兜底） | 🟡 | 记录 task 引用，避免 GC 丢弃告警；可加 `TaskGroup` |

### 3.6 队列 / 并发（core/queues.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `queues.py:35-47` | `_group_worker` 已 catch 异常并记日志 ✅；但**任务集 `_group_tasks` 无清理**，长期运行字典无限增长 | 🟡 | 任务结束时移除引用 |
| `queues.py:21` | 渲染信号量 = 2，但 Playwright 单例浏览器并发截图可能超限（依赖浏览器实例内部串行） | 🟡 | 确认 `_ensure_browser` 并发安全 |
| `queues.py:86-92` | `submit_render` 的 `Future` 无超时，若渲染卡死则调用方永久 `await` | 🟠 | 给渲染任务加 `wait_for` 预算 |

### 3.7 日志（core/logger.py, log_server.py）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `logger.py:144,152` | `StreamHandler(sys.stdout)` + `TimedRotatingFileHandler`（按天轮转）为标准库异步安全的 handler，**无 on-loop 阻塞写** ✅ | — | 保持 |
| `log_server.py:363-393` | `WebSocketLogHandler.emit` 用 `loop.create_task(broadcast())` 非阻塞 ✅；但**每条日志一个 task**，DEBUG 高频时 task 风暴 | 🟡 | 合并/限流（串行队列 + 批量发送） |
| `log_server.py:425` | `_file_watcher` 独立模式每 0.5s 轮询读文件，仅 standalone 用 | 🟡 | 无 |

### 3.8 Playwright / Chromium（async API，但需资源预算）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `bot.py:397-398` | `page.wait_for_timeout(500)` 固定等待 + `screenshot`，无 `set_content`/`screenshot` 超时 | 🟡 | 设页面加载/截图超时预算 |
| `modules/changelog.py:508-509` | `wait_for_timeout(150)` + `screenshot`，无超时 | 🟡 | 同上 |
| `modules/earthquake.py:517` | `page.screenshot` 无超时 | 🟡 | 同上 |
| `modules/pgr.py:207-208` | `wait_for_timeout(500)` + `screenshot`，无超时 | 🟡 | 同上 |
| `modules/commands.py:2103,2197,3354` | 多处 `screenshot` 无超时 | 🟡 | 统一工具函数收口 |
| `modules/chinese_chess.py:117-118` | 同上 | 🟡 | 同上 |

### 3.9 其他（bot.py / 工具）

| 位置 | 问题 | 严重程度 | 建议 |
|---|---|---|---|
| `bot.py:267,327,339,430` | 后台任务循环内同步 `read_text()` 读控制/状态文件（秒级低频，量小） | 🟡 | 可接受，量小时可 `to_thread` |
| `core/tools.py:613,625,628` | `_resolve_player/_save_binding` 同步读写 JSON，`_save_binding` 在 async 路径可能 on-loop | 🟡 | 绑定写入 `to_thread` |

---

## 4. 外部调用「Timeout/Retry/Fallback/预算」覆盖现状

| 外部调用 | 位置 | Timeout | Retry | Fallback | 资源预算 |
|---|---|---|---|---|---|
| OpenAI `chat.completions` | llm.py:404 | ✅ `wait_for` | ❌ | ✅ 空→fallback文案 | ❌ 单消息总预算 |
| OpenAI FC `chat.completions` | llm.py:526 | ✅ | ❌ | ✅ strict 失败回退普通 | ❌ 轮次/时间预算 |
| DeepSeek Responses 原生搜索 | search.py:50 | ✅ 45s | ❌ | ❌ **回退失效(死代码)** | ❌ |
| 百度/Bing/百科搜索 | web_search.py:165-254 | ✅ 8/5/5s | ❌ | ⚠️ 源间互为备源 | ❌ |
| 网页正文抓取 | web_search.py:262 | ✅ 6s | ❌ | ❌ | ✅ 线程池限流 |
| PUZZLE/NASA `urlopen` | services/nasa.py:17 | ✅ 10s | ❌ | ❌ | ❌ |
| PGR `urlopen` | services/pgr.py:28,41,56 | ✅ 15s | ❌ | ❌ | ❌ |
| WHOIS `urlopen` | whois_lookup.py:72 | ✅ 15s | ❌ | ❌ | ✅ 已 `to_thread` |
| KOOK `create_asset`/`send` | sender.py | ❌ 依赖库默认 | ❌ | ✅ 失败发 fallback | ❌ |
| 外部图片下载 | sender.py:281 | **❌ 无** | ❌ | ✅ 直接发 URL | ❌ |
| `subprocess`(calc) | tools.py:262 | ✅ 5s | ❌ | ❌ | ❌ 且同步阻塞 |
| Playwright 截图 | 多处 | ❌（多数无） | ❌ | ✅ 队列信号量 | ⚠️ 渲染队列+信号量2 |

---

## 5. 优先修复建议（Ranked）

1. 🔴 **`sender.py:281` 图片下载加 `aiohttp` 超时** — 当前可无限挂起，最危险的挂死点。
2. 🔴 **`core/tools.py:_calc` 改为 `run_in_executor`** — 事件循环被阻塞 5s/次。
3. 🔴 **`nasa`/`pgr` 同步 `urlopen` 移入 `to_thread`** — 阻塞 10~15s/次。
4. 🔴 **修复搜索回退链**（`search.py:56-60` 接入 `agent_search`，或删除死代码并降级处理）— 否则 DeepSeek 搜索失败即无结果，缺 Fallback。
5. 🟠 **LLM 加入网络层重试 + 单消息总时间/轮次预算**（`llm.py` FC 循环与 `call_llm`）。
6. 🟠 **`submit_render` 加 `wait_for` 预算**（queues.py:86）。
7. 🟡 **统一磁盘写异步化**：`token_tracker.record_usage`、`sender._log_msglog`、`memory` 读写 → 内存缓冲/批量/`to_thread`。
8. 🟡 **日志 task 风暴**：`log_server` 每日志一 task → 队列批处理。

---

## 6. 附：未发现的风险

- 未使用 `sqlite3` / 数据库连接（全为 JSON 文件存储），无 DB 连接池/锁问题。
- `time.sleep` 仅在 `scripts/*_status_reporter.py`（独立脚本进程）与 `modules/local_search.py`（已被 `run_in_executor` 隔离）中出现，**不在事件循环常驻路径**。
- `services/llm.py`、`modules/web_search.py` 的 LLM/搜索调用均已正确使用 executor，事件循环阻塞集中在第 2 节列出的 3 处 `urlopen`/`subprocess` 直调。