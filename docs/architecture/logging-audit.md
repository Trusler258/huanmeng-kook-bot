# 幻梦 Bot（KOOK）日志系统审计报告（Phase 0）

> 审计范围：`core/`、`services/`、`modules/`、`bot.py`、`main.py`
> 目的：为 Huanmeng 2.0 重构厘清现有日志能力，重点评估「能否通过 trace_id 定位一次请求为什么慢」这一目标。
> 结论先行：**当前日志系统不支持 trace_id 请求链路追踪，且存在多处重复 traceback、日志刷屏热点、敏感内容落盘等需要重构的问题。**

---

## 1. 日志系统结构（core/logger.py）

`core/logger.py` 是唯一的日志统一入口，采用标准库 `logging` 封装。关键结构如下：

| 项 | 现状 |
|---|---|
| Logger 名 | 根 logger = `"huanmeng"`；`get_logger(name)` 返回 `huanmeng.<name>` 子 logger（如 `dispatcher`、`pipeline`、`llm`、`sender`） |
| 初始化 | `init_logger(debug_mode, log_to_file=True)`，在 `bot.py` 启动时调用（`bot.py:66`） |
| 控制台 Handler | `StreamHandler(sys.stdout)`，级别 DEBUG（debug 模式）或 INFO（非 debug） |
| 文件 Handler | `TimedRotatingFileHandler`，写入 `logs/huanmeng.log`，按天轮转、保留 7 天，**级别恒为 DEBUG**（`logger.py:159`）——即非 debug 模式下文件也始终记录 DEBUG |
| Web Handler | `WebSocketLogHandler`（`core/log_server.py`），级别 DEBUG，推送到浏览器控制台（端口 62000） |
| 便捷函数 | 模块级 `info/warning/error/debug/critical`（兼容旧代码）；`debug()` 额外受 `is_debug()` 门控（`logger.py:223-225`） |
| 动态开关 | `set_debug_mode()` 只调整 StreamHandler 级别，**不影响文件/WS Handler** |
| Formatter | 自定义 `_ColorFormatter` / `_FileFormatter`，输出 `[时间] 级别 (module:func:lineno) 消息`，仅时间+级别+源码位置，**无任何结构化字段** |

**关键结论（对 trace_id 目标）：**
- **`get_logger` / handler / Formatter 均无 trace_id 概念**。没有 `contextvars`、没有 Filter、没有 `extra` 字段注入，日志条目之间唯一的关联键是散落在消息文本里的 `chat_id` / `user_id`，且格式不统一。
- 文件 Handler 恒为 DEBUG 是本设计的一个隐患（见 §5 敏感内容落盘）。

### 1.1 根因：两次初始化 & WS Handler 的循环依赖
`init_logger` 内部 `import core.log_server`（`logger.py:165`），而 `log_server.py` 在 `start()` 里又用 `logging.getLogger("huanmeng")` 打日志（`log_server.py:470`）。启动时用 `try/except: pass` 吞掉首次导入异常（`logger.py:173-174`），属于脆弱设计，2.0 应改为惰性 / 显式装配 handler。

---

## 2. `logger.*` 调用分布统计

对 `*.py` 全库统计（范围含 `core/ services/ modules/ bot.py main.py`，另含 `utils/ logweb.py`），`logger.<level>` 调用共 **~591 处 / 47 个文件**，级别分布：

| 级别 | 调用数 | 文件数 | 说明 |
|---|---|---|---|
| `logger.info` | ~292 | 44 | 占绝对多数，主链路大量 info |
| `logger.warning` | ~136 | 39 | 含大量「失败/回退/跳过」 |
| `logger.error` | ~84 | 24 | 含大量 `exc_info=True` |
| `logger.debug` | ~79 | 21 | 受 `is_debug()` 门控，但文件恒记录 |

### 2.1 各文件 `logger.*` 总数（从高到低，仅列关键/高日志量文件）
| 文件 | logger 调用数 | 备注 |
|---|---|---|
| `modules/commands.py` | 62 | 指令处理，含多处 `exc_info=True` |
| `core/pipeline.py` | 52 | 主回复管道，info 33 处 |
| `services/llm.py` | 44 | LLM 调用，每次调用多行日志 |
| `services/sender.py` | 27 | 发送服务 |
| `services/pc_status.py` | 26 | PC 心跳，见 §5 刷屏热点 |
| `modules/tuf_commands.py` | 24 | 大量 `exc_info=True` |
| `modules/judge.py` | 23 | 判断模型 |
| `services/image_api.py` | 22 | 识图 |
| `services/tuf_api.py` | 22 | 大量 `exc_info=True` |
| `core/dispatcher.py` | 19 | 事件分发 |

### 2.2 `print()` 绕过 logger
在 `services/ core/ modules/ bot.py main.py` 范围内，真正的 `print()` 仅少量且多为启动标语/测试，不构成主链路绕过：
- `main.py:65-69`、`logweb.py:21-25`：启动横幅（合理）。
- `bot.py:234`：`print("")` 空行（无害）。
- `services/tuf_api.py:287-289`：`__main__` 自测输出（合理）。
- `modules/cchess/__init__.py:2286,2294`：GIF/HTML 生成提示（`print`，非 logger）。
- `core/user_profile.py:370,401`：`__main__` 测试输出（合理）。
- `modules/whois_lookup.py:128,130`：CLI 入口（合理）。

> 结论：主链路基本都走 logger，`print` 不属于主要问题；但 `cchess/__init__.py` 两处 `print` 建议在 2.0 换为 logger。

---

## 3. traceback / exception 使用情况

### 3.1 `import traceback` / `traceback.format_exc()` / `logger.exception` / `exc_info=True` 位置（范围内）

| 模式 | 位置 | 说明 |
|---|---|---|
| `import traceback` + `logger.error(…format_exc())` | `core/dispatcher.py:47-49` | dispatch 外层 catch |
| `import traceback` + `logger.warning(…format_exc())` | `core/pipeline.py:741-743` | JSON CALL 执行失败 |
| `import traceback` + `logger.error(…format_exc())` | `core/pipeline.py:951-952` | 追加回复（follow-up LLM）失败 |
| `import traceback` + `logger.error(…format_exc())` | `services/pc_status.py:232-233, 742-743` | 歌词推送 / offset 请求异常 |
| `logger.error(…, exc_info=True)` | `bot.py:132,160,193` | 消息/回调/Bot 运行异常 |
| `logger.error(…, exc_info=True)` | `modules/commands.py:544,801,1150,1334,3210` | 卡片/图片/指令执行异常 |
| `logger.error(…, exc_info=True)` | `modules/tuf_commands.py:411,452,473,558,631,714,786` | TUFD 卡片/下载异常 |
| `logger.error(…, exc_info=True)` | `services/tuf_api.py:78,110,142,191,223` | TUFD API 异常 |
| `logger.error(…, exc_info=True)` | `modules/changelog.py:697,828` | 卡片发送异常 |
| `logger.exception(...)` | `core/user_profile.py:50` | 画像文件损坏（最规范写法，仅此 1 处） |

### 3.2 重复 / 冗余 traceback 模式（重点问题）

**模式 A — dispatch 路径双重打印完整栈（确凿冗余）：**
```
bot.py:129 on_msg → dispatcher.dispatch(msg)
  bot.py:131-132  except → error("消息处理异常: %s", e, exc_info=True)   ← 打印完整栈 #1
  dispatcher.py:46-49  except → logger.error(…, traceback.format_exc())  ← 打印完整栈 #2
```
同一异常在 `bot.py` 与 `dispatcher.py` **各打印一次完整 traceback**，内容几乎完全重复，两次堆栈把日志瞬间刷满，且无 request 标识，难以定位是哪条消息。

**模式 B — 指令执行路径双重打印（部分冗余）：**
`pipeline.py:741-743` 对「JSON CALL 执行失败」用 `format_exc()` 打印完整栈，同时被调用的 `handle_command` 内部在 `commands.py:3210` 又用 `exc_info=True` 打印同一异常栈 → 同一指令失败被打印两次。

**模式 C — 低层 API 层层 `exc_info=True`：**
`services/tuf_api.py` 的 5 处、`modules/tuf_commands.py` 的 7 处、`modules/changelog.py` 的 2 处、`modules/commands.py` 的 5 处，全部在**最底层**（单个 HTTP 请求 / 单张卡片发送）就 `exc_info=True` 打完整栈。上层若再 catch 转发，会叠加成多层栈；即使不转发，也会为「一次失败」生成海量日志行，属于级别滥用（底层失败用 ERROR+完整栈，多数应降为 WARNING+消息）。

**模式 D — 丢栈的边界（反向问题）：**
`core/queues.py:44` 队列 worker 捕获 `process_message` 的异常时只 `logger.error("…: %s", e)`，**不打印栈**。由于 `process_message` 在 worker 协程里执行，异常不会冒泡到 `dispatcher.dispatch` 的 `format_exc`，因此**主回复管道真正崩溃原因没有任何栈**，与模式 A（dispatch 层过于冗余）形成鲜明对比——一个重复打印、一个完全不打印。

**建议（2.0）：**
1. 全库统一「只在进程边界 catch 一次并打印完整栈」，内部层只抛不吞或只记 `WARNING: 具体信息`，杜绝 A/B/C 的多层重复。
2. 用 `logger.exception()`（自动带栈）替代手写 `traceback.format_exc()`（当前全库仅 `core/user_profile.py:50` 用对了）。
3. 为每个异常打上 `trace_id + chat_id + 阶段`，让同一栈可归并去重。

---

## 4. 是否支持「trace_id 定位一次请求为什么慢」

**结论：不支持。**

### 4.1 现状：没有 request/message 级 trace_id
- 全库 grep 未发现任何 `trace_id` / `request_id` / `contextvars` / `Filter` 注入机制。
- 贯穿主链路的唯一标识是 `chat_id`（以及 `user_id`），但它同时被**同聊天的所有并发消息、命令、后台任务共享**，无法区分「同群里哪一条消息」。
- dispatcher 里有一个全局递增计数器 `self._msg_count`，只在 `dispatcher.py:226` 的 `📩 消息 #N` 一行出现，**没有透传给 pipeline / llm / sender**，无法作为贯穿标识符。

### 4.2 消息从 dispatcher → pipeline → LLM → sender 是否贯穿同一标识？
- **dispatcher**（`dispatcher.py:225`）：`📩 消息 #N | … | chat=…`
- **queue**（`queues.py:55`）：`[队列] 群%d 消息已入队`
- **pipeline**（`pipeline.py:537`）：`开始生成回复: speaker=%s chat=%d`
- **LLM**（`llm.py:383`）：`调用LLM [%s] url=…` —— **不含 user/chat/message，只有模型名和耗时**
- **sender**（`sender.py:368`）：`开始分批发送 %d 条句子 → chat=%s`

各阶段日志之间**没有任何公共键**，唯一的 `chat_id` 在 `llm.py` 里甚至没打。因此想回答「某一条消息为什么慢：卡在 dispatcher 识图？LLM 第一次调用？FC 工具轮？sender 分句？」——**无法从日志中把同一条消息的离散事件串起来**。

### 4.3 配套缺失
- LLM 调用已埋了耗时（`llm.py:408,444,455`）与 `record_llm_call` 性能埋点（`services/notify_system.py`），但**没有关联到消息/会话**，无法回答「哪一条请求慢」。
- 没有「阶段耗时」埋点（dispatcher 识图耗时、FC 每轮耗时、send 每句耗时均未记录总耗时时序）。

### 4.4 2.0 落地建议
1. 在 `dispatcher` 入口为每条消息生成 `trace_id`（可用 `uuid4` 短码），用 `contextvars.ContextVar` + 自定义 `logging.Filter` 注入到同 async 任务的**所有**子 logger 记录里，并作为 `extra` 写入 Formatter 输出与文件。
2. 在 `pipeline` 里把 `trace_id` 显式传给 `generate_multi_reply_with_tools` / `call_llm` / `send_sentences`，让 LLM 与 sender 日志也带 `trace_id + chat_id`。
3. 在关键阶段（识图 `_do_recognize_image`、FC 每轮 `round_idx`、LLM 返回、`send_sentences` 每句）打「阶段耗时」日志，形成一条请求的时序时间线。
4. 文件 Handler 增加结构化字段（`trace_id`、`chat_id`、`phase`），便于按 `trace_id` 过滤检索。

---

## 5. 日志刷屏 / 敏感内容落盘热点

| 位置 | 明细 | 问题 |
|---|---|---|
| `services/pc_status.py:79` | `logger.info("PC 数据心跳 …")` | **心跳循环内每拍一条 info**，且带歌词正文，长期高频刷屏 |
| `services/pc_status.py:127,130,137,142` | 每条 `lyric_event` 连续打 4 条 info（收到/进入发送/去重/准备发送） | 单条歌词产生 4 行刷屏 |
| `core/dispatcher.py:226` | 每条消息都打一条 info（含 `content[:30]`） | 高频，但内容已截断，可接受；建议降级/合并 |
| `core/pipeline.py:411` | `logger.info("额外信息: …")` 每条进入回复管道的消息都打 | 噪声 |
| `services/llm.py:383,444` | 每次 LLM 调用打 info（含重试），FC 每次工具轮再打 `556,724,778,801` | 一条消息可产生 5~10 行 LLM 日志，且 `llm.py:1175` 在 **debug 级打印完整原始输出 `raw[:200]`** |
| `core/logger.py:159` | 文件 Handler **恒为 DEBUG** | 即使 `debug_mode=False`，所有 debug 日志（含消息正文、LLM 原始输出）仍写入 `logs/huanmeng.log` |
| `core/pipeline.py:269` | debug 打 `content='%s'`（消息正文） | 属敏感内容，且因文件恒 DEBUG 会落盘 |
| `services/sender.py:374,377` | debug 打每句发送内容 | 同上 |

**敏感/刷屏要点：**
- **隐私**：`pipeline.py:269`、`dispatcher.py:44`、`llm.py:1175`、`sender.py:377` 等在 debug 级输出**完整用户消息 / LLM 原始输出**，而文件 Handler 恒为 DEBUG → 用户聊天内容被持久化到磁盘。2.0 应让文件 Handler 跟随 debug 开关，或对内容字段做脱敏/截断。
- **级别倒挂**：底层网络失败（`tuf_*`、卡片发送）全用 ERROR，应降级；而真正该 ERROR（`process_message` 崩溃）在 `queues.py:44` 却只打一句话。

---

## 6. 汇总问题清单（文件:行号 → 日志语句 → 问题 → 建议）

| 文件:行号 | 日志语句（摘要） | 问题 | 建议 |
|---|---|---|---|
| `core/logger.py:180-192` | `get_logger(name)` 返回子 logger | 无 trace_id / 结构化字段 | 2.0 引入 Filter + contextvars 注入 trace_id |
| `core/logger.py:159` | `file_handler.setLevel(logging.DEBUG)` | 非 debug 也把 debug（含正文）写盘 | 文件级别跟随 debug 开关，或脱敏 |
| `core/logger.py:164-174` | `try: import log_server` + `except: pass` | 循环依赖被静默吞掉 | 惰性装配 handler，去掉裸 except |
| `bot.py:132` | `error("消息处理异常: %s", e, exc_info=True)` | 与 dispatcher 重复打印完整栈（模式 A） | 二选一：只在进程边界打一次 |
| `core/dispatcher.py:47-49` | `logger.error(…, traceback.format_exc())` | 与 bot.py 重复；无 trace_id | 统一边界打印，带 trace_id |
| `core/queues.py:44` | `logger.error("[队列] 群%d 处理异常: %s", chat_id, e)` | **process_message 崩溃无栈**，无法定位 | 补 `logger.exception` 或 `exc_info=True` |
| `core/pipeline.py:741-743` | `logger.warning(…traceback.format_exc())` | 与 commands.py:3210 重复栈（模式 B） | 只留一处完整栈 |
| `core/pipeline.py:951-952` | `logger.error("追加回复失败:\n%s", format_exc())` | 手写 format_exc | 改 `logger.exception` |
| `core/pipeline.py:537` | `logger.info("开始生成回复…")` | 无请求标识，无法跨阶段关联 | 加 trace_id + 阶段序号 |
| `core/pipeline.py:269` | `logger.debug("content='%s'", msg_content[:40])` | 用户正文落盘（文件恒 DEBUG） | 脱敏 / 截断 / 级别跟随开关 |
| `core/dispatcher.py:226` | `logger.info("📩 消息 #N …")` | 每条消息一条 info | 保留但补 trace_id；或降级 debug |
| `core/dispatcher.py:44` | `logger.debug("dispatch 入口: content=%r", …[:80])` | 用户正文落盘 | 脱敏 |
| `services/llm.py:383` | `logger.info("调用LLM [%s] url=%s …")` | 每次调用（含重试）都打，无 chat/user，无法关联 | 加 trace_id + chat_id；重试合并 |
| `services/llm.py:444` | `logger.info("LLM [%s] 返回 %d字符 …")` | 同上 | 加 trace_id |
| `services/llm.py:1175` | `logger.debug("多句生成原始输出: %s", raw[:200])` | LLM 原始 JSON 落盘 | 仅 debug 且文件应按开关 |
| `services/pc_status.py:79` | `logger.info("PC 数据心跳 …")` | 心跳高频刷屏 | 降 debug 或节流 |
| `services/pc_status.py:127-142` | 每条歌词 4 条 info | 刷屏 | 合并为 1 条，或 debug |
| `services/pc_status.py:232-233,742-743` | `logger.error(…format_exc())` | 手写 format_exc | 改 `logger.exception` |
| `services/tuf_api.py:78,110,142,191,223` | `logger.error(…, exc_info=True)` ×5 | 底层失败打完整栈，级别倒挂 | 降 WARNING+消息，栈只留边界一次 |
| `modules/tuf_commands.py:411…786` | `logger.error(…, exc_info=True)` ×7 | 同上 | 同上 |
| `modules/changelog.py:697,828` | `logger.error(…, exc_info=True)` | 同上 | 同上 |
| `modules/commands.py:3210` | `logger.error("指令执行异常…", exc_info=True)` | 与 pipeline 重复栈 | 归并 |
| `core/user_profile.py:50` | `logger.exception("画像文件损坏，重置")` | ✅ 规范（唯一正确示例） | 全库推广 |
| `services/sender.py:368-379` | `send_sentences` 循环 debug | 每句一条 debug（含内容） | 保留 debug，内容脱敏 |

---

## 7. 审计结论与 2.0 优先级建议

1. **P0（架构）**：`core/logger.py` 引入 `trace_id`（`contextvars` + `Filter`），贯穿 dispatcher→pipeline→LLM→sender，为「定位一次请求为什么慢」打基础。这是本审计最核心的缺口。
2. **P0（正确性）**：修复 `queues.py:44` 丢栈问题（`process_message` 崩溃无栈）；统一「边界打印一次完整栈」策略，消除 bot/dispatcher、pipeline/commands 的重复 traceback。
3. **P1（隐私/体量）**：文件 Handler 级别跟随 debug 开关；用户消息正文、LLM 原始输出在 debug 级脱敏/截断。
4. **P1（噪音）**：压缩 `pc_status.py` 心跳与歌词日志、`llm.py` 重试日志；底层网络失败由 ERROR 降为 WARNING。
5. **P2（一致性）**：全库用 `logger.exception()` 取代手写 `traceback.format_exc()`；`print` 全量切换为 logger。