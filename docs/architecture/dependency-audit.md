# 幻梦 Bot — Phase 0 依赖审计报告

> 审计对象：`kook-bot-opensource`（KOOK 聊天机器人）源码
> 审计范围：`core/*`、`services/*`、`modules/*`、`utils/*`、`bot.py`、`main.py`
> 审计目标：为 Huanmeng 2.0 重构（目标分层 core / services / modules / utils / plugins）提供依赖现状基线
> 说明：本文档全部结论基于真实源码读取，给出了具体文件与符号引用，未修改任何源码。

---

## 1. 分层目标与现状总览

| 目标层 | 目录 | 当前定位 | 是否纯净 |
|--------|------|----------|----------|
| core | `core/**` | 基础设施（config/logger/queues/dispatcher/pipeline/tools/context/user_profile/arch_loader/token_tracker/log_server） | ❌ **重度反向依赖 services 与 modules** |
| services | `services/**` | 外部服务封装（LLM/发送/PC状态/TTS/战绩/通知等） | ⚠️ 相对干净，仅依赖 core + utils |
| modules | `modules/**` | 业务功能模块（指令/记忆/搜索/判断/游戏等） | ⚠️ 依赖 core + services + 互相关联 |
| utils | `utils/**` | 纯工具（format_lang/username/writing） | ✅ 最干净 |
| plugins | （不存在） | 目标新增层 | ❌ 尚未拆分 |

**核心结论**：`core/pipeline.py`、`core/dispatcher.py`、`core/tools.py`、`core/queues.py` 在**模块顶层或函数内**import 了 `services.*` 与 `modules.*`，这是 plugin 解耦的最大障碍。`core` 层目前并不具备"底层不依赖上层"的纯净性。

---

## 2. 各模块 import 关系（模块顶层）

### 2.1 入口

| 文件 | 模块顶层依赖 | 说明 |
|------|-------------|------|
| `main.py` | stdlib：asyncio/argparse/signal/sys/os | 纯净；`HuanmengBot`、`core.logger` 均在 `main()` 内延迟导入 |
| `bot.py` | `khl`(Bot/MessageTypes/Message)、`core.logger`、`core.config`、`utils.format_lang`、`services.sender`、`core.dispatcher`、`core.context_manager` | 组装层，负责注入各单例 |

### 2.2 core 层

| 文件 | 模块顶层依赖 | 关键点 |
|------|-------------|--------|
| `core/config.py` | `core.arch_loader`（L10）、os、toml、dataclasses、pathlib、dotenv | `core→core` 私有依赖，无 modules/services |
| `core/logger.py` | stdlib 仅 | 纯净；`core.log_server` 在 `init_logger()` 内延迟导入 |
| `core/queues.py` | `core.logger` | 纯净；`core.pipeline` 在 worker 内延迟导入 |
| `core/context_manager.py` | `core.logger`、`core.config` | `core→core` |
| `core/user_profile.py` | `core.logger` | `core→core`；`services.llm`/`core.config` 在函数内延迟 |
| `core/tools.py` | stdlib 仅（json/asyncio/logging/typing） | **顶层最干净**；所有 services/modules 依赖全部放在函数体内 |
| `core/token_tracker.py` | `core.logger` | `core→core` |
| `core/arch_loader.py` | stdlib 仅 | 纯净 |
| `core/log_server.py` | stdlib 仅 | 纯净 |
| `core/dispatcher.py` | `core.logger`、`core.config`、**`core.pipeline`**（L17） | `core→core`，但 pipeline 又下拉 modules（见 §3） |
| `core/pipeline.py` | `core.logger`、`core.config`、`utils.format_lang`、**`modules.judge`(L30)、`modules.memory`(L31)、`modules.fav`(L37)、`modules.commands`(L38)、`modules.search`(L39)、`services.llm`(L40)、`services.sender`(L41)** | 🚨 **core 层顶层直接 import 5 个 modules + 2 个 services** |

### 2.3 services 层

| 文件 | 模块顶层依赖 |
|------|-------------|
| `services/llm.py` | `openai`、`core.logger`、`core.config`、`utils.format_lang` |
| `services/sender.py` | `khl`(MessageTypes)、`core.logger`、`core.config`、`utils.format_lang` |
| 其余 services/* | 以 `core.config`/`core.logger` 为主（未逐一展开，均属 services 依赖 core） |

> services 层无 modules 反向依赖，结构较健康。

### 2.4 modules 层

| 文件 | 模块顶层依赖 | 关键点 |
|------|-------------|--------|
| `modules/commands.py` | `core.logger`、`core.config`、`core.token_tracker`、`utils.format_lang`、`modules.fav`、`modules.search`、`modules.pgr`、`modules.earthquake`、`modules.nasa`、`modules.agnes`、`modules.voice`、`modules.ping`、`modules.op`、`modules.weather`、`modules.changelog`、`modules.whois_lookup`、`modules.tuf_commands`、`services.sender` | 指令总装，重拖多模块；`changelog` 已做 playwright 延迟导入（L41-43） |
| `modules/judge.py` | `core.logger`、`core.config` | 🚨 **模块底部有副作用**：`init_keywords()`（L328）导入即读盘 |
| `modules/memory.py` | `core.logger`、`core.config`、`utils.format_lang` | 干净；`services.llm` 函数内延迟 |
| `modules/search.py` | `httpx`、`core.logger`、`modules.judge`、`utils.format_lang` | `modules→modules`：search 顶层依赖 judge |

### 2.5 utils 层

| 文件 | 模块顶层依赖 |
|------|-------------|
| `utils/format_lang.py` | 未知（属 utils，依赖 core.config 或独立） |
| `utils/username.py` | 通过 `init_username(bot)` 注入（bot.py L93-95） |
| `utils/writing.py` | 由 pipeline 在函数内延迟导入 |

---

## 3. 函数内部延迟 import（懒加载）清单

> 这是模块解耦与懒加载的关键信号。以下按文件列出"在函数体里 import"的符号。

### 3.1 `main.py`
- `main()`：`from bot import HuanmengBot`；`from core.logger import info, set_debug_mode`

### 3.2 `bot.py`
- `initialize()`：`services.llm._load_skill_sections`、`core.logger.init_logger`、`utils.username.init_username`、`modules.wzq.load_games`、`modules.judge.init_keywords`、`core.dispatcher._current_dispatcher` 赋值
- `_register_handlers()`：`khl.EventTypes`、`core.config.get_config`、`modules.commands.handle_command`、`services.sender.send_by_chat_type`
- `run()`：`core.queues.start_render_queue`、`modules.changelog._ensure_browser`
- 各后台任务 `_bg_*()`：`modules.remind`、`services.pc_status`、`services.tts`、`modules.earthquake`、`modules.holiday`、`services.notify_system`、`services.update_webhook`、`services.wdsj_tracker.daily_stats_collect`、`modules.commands._build_daily_rank_html`、`services.sender.send_group_msg/send_private_msg`、`core.config.get_config`
- `shutdown()`：`modules.judge.flush_search_cache`、`core.context_manager.save_context`
- `handle_reload()`：`services.llm.reload_skill_cache`、`_load_skill_sections`

### 3.3 `core/config.py`
- `reload_config()`：`core.logger.info/set_debug_mode`
- `set_debug_mode()`：`core.logger.set_debug_mode`
- `BotConfig.group_ids()`：`pathlib.Path`、`re`
- `BotConfig._build_self_awareness()`：`re`

### 3.4 `core/logger.py`
- `init_logger()`：`core.log_server.WebSocketLogHandler`（L165）

### 3.5 `core/queues.py`
- `_group_worker()`：`core.pipeline.process_message`（L37）——**延迟导入避免 pipeline 回环**

### 3.6 `core/dispatcher.py`
- `_dispatch_inner()`：`khl.message.PublicMessage/PrivateMessage`、`zlib`、`modules.ignore_users.is_ignored`、`utils.username.record_user_name`、`services.sender.log_user_message/cache_channel`、`core.queues.enqueue_message`、`core.context_manager.get_context_mgr`
- `_do_recognize_image()`：`services.image_api.recognize_image`
- `_bg_recognize_image()`：`core.context_manager.get_context_mgr`
- `_extract_card_images()`：`json`

### 3.7 `core/pipeline.py`
- `process_message()`：`core.context_manager.get_context_mgr`、`modules.fav.ensure_fav`、`modules.preset.*`、`modules.op.*`、`modules.stm.add_entry`、`services.self_ignore.*`、`core.config.get_architecture_context`（L315，注意该函数实际定义于 `core.arch_loader`，经 config 顶层 re-export）、`modules.holiday.get_today_holiday_text`、`modules.preset.get_preset`、`services.image_api.get_recent_image_descriptions`、`modules.error_report.build_error_report_prompt`、`modules.op.get_persona/get_persona_memory_id`、`modules.memory.set_persona_override`、`utils.writing.*`、`services.sender.send_group_msg/send_private_msg`、`core.tools._write_code`、`modules.commands.COMMAND_MAP`、`services.llm.call_llm/_build_system_text`、`modules.op._load_modes/_save_modes`、`core.user_profile.build_profile_text`
- `handle_poke_event()`：`core.context_manager.get_context_mgr`、`modules.fav.ensure_fav`、`modules.preset.get_preset`、`modules.op.get_mode/get_sleep_prompt_rule/get_narrative_prompt_rule`
- `_async_extract_profile()`：`core.user_profile.extract_from_message/update_profile`、`core.logger.get_logger`
- `_handle_command_route()`：`core.config.get_config`
- `_send_test_card()`：`utils.format_lang.format_lang`

### 3.8 `core/tools.py`（隔离性最佳）
- `_calc()`：`subprocess/tempfile/os`
- `_write_code()`：`re/zipfile/tempfile`、`pathlib`、`core.logger.get_logger`、`services.llm.call_llm`、`core.config.get_config`、`services.sender.send_group_msg/send_private_msg/send_file`
- `_compile_and_run()`：`subprocess/shutil`
- `_optimize_search_keywords()`：`services.llm.call_llm`、`core.config.get_config`
- `_agent_think()`：`services.llm.call_llm/call_llm_with_tools`、`core.config.get_config`、`modules.search.perform_search`
- `_system_status()`：`services.pc_status.format_pc_status`、`core.config.get_config`
- `_read_url()`：`modules.local_search.get_scraper`、`services.llm.call_llm`、`core.config.get_config`
- `execute_tool()`：`modules.commands.COMMAND_MAP`

### 3.9 `core/user_profile.py`
- `extract_from_message()`：`services.llm.call_llm`、`core.config.get_config`

### 3.10 `services/llm.py`
- `call_llm()`：`core.token_tracker.record_usage`、`services.notify_system.record_llm_call`
- `call_llm_with_tools()`：`services.notify_system.record_llm_call`
- `_build_dynamic_command_list()`：`modules.commands.COMMAND_MAP`（L213，try/except）
- `_parse_reply()`：`modules.face_lib.get_face/make_cq`（L912）
- `generate_multi_reply_with_tools()`：`core.tools.get_tool_schemas/execute_tool`（L705）
- `call_summary_model()`：`utils.format_lang.format_lang`

### 3.11 `services/sender.py`
- `_send_to_channel()`：`time`、`aiohttp/tempfile/os`
- `_log_msglog()`：`time`、`json`
- `send_raw_group()` / `send_raw_user()`：`json`、`khl.MessageTypes`

### 3.12 `modules/memory.py`
- `_compress_with_deepseek()`：`services.llm.call_llm`
- `_format_msglog_entries()`：`core.config.get_config`

### 3.13 `modules/search.py`
- `perform_search()`：`services.sender.send_by_chat_type`、`modules.web_search.ds_native_search`、`modules.memory.save_search_memory`
- `auto_search_if_needed()`：`modules.judge.needs_search`

### 3.14 `modules/commands.py`
- `_cmd_gh()`：`modules.gh.cmd_gh`（try/except）
- `_cmd_update()`：`modules.auto_update.cmd_update`
- `cmd_help()`：`services.sender.send_by_chat_type`、`utils.format_lang.get_lang_data`

---

## 4. 循环依赖风险

### 4.1 已识别的潜在/被延迟规避的回环

1. **`core.pipeline → modules.commands → services.sender → core.config`**
   - `pipeline.py` L38 顶层 import `modules.commands`；`commands.py` L28 顶层 import `services.sender`；`sender.py` L21 顶层 import `core.config`。链路到 `core.config` 为止，**无回环**（config 不反引 pipeline）。

2. **`core.pipeline → modules.judge ⇄ services.llm`（靠懒加载规避）**
   - `pipeline.py` L30 顶层 import `modules.judge`；`judge.py` 的 `should_respond()`（L269）在**函数内** `from services.llm import call_judgment_pipeline`；`services/llm.py` 顶层不 import modules。若 `judge.py` 改为顶层 import `services.llm`，则会形成 `pipeline→judge→llm→(pipeline)` 的潜在回环。**当前是安全的，但很脆弱。**

3. **`services.llm → modules.commands`（懒加载）**
   - `llm.py` `_build_dynamic_command_list()`（L213）函数内 import `modules.commands.COMMAND_MAP`；而 `modules/commands.py` 顶层不 import `services.llm`。若未来把 COMMAND_MAP 生成提前到模块顶，会引入 `llm⇄commands` 回环。

4. **`services.llm → core.tools → {services.llm}`（懒加载）**
   - `llm.py` `generate_multi_reply_with_tools()`（L705）函数内 import `core.tools`；`core/tools.py` 的 `_write_code/_optimize_search_keywords/_agent_think/_read_url` 又函数内 import `services.llm.call_llm`。**纯函数内互引，靠运行时调用，无导入回环**，但属于强耦合的环形调用。

5. **`core/queues.py → core.pipeline`（已用懒加载规避）**
   - `queues.py` `_group_worker()`（L37）函数内 import `core.pipeline`。若改为顶层导入，会与 `core/dispatcher.py`（顶层 import `core.pipeline`）共同构成 `queues→pipeline→…→queues` 潜在回环。当前安全。

### 4.2 模块级单例相互引用（运行期耦合，非导入回环）

| 单例 | 归属模块 | 注入方式 |
|------|----------|----------|
| `_current_dispatcher` | `core/dispatcher.py` L567 | `bot.py` L111-112 直接赋值 `_disp._current_dispatcher = self.dispatcher` |
| `_bot` | `services/sender.py` L27 | `bot.py` L89 `init_sender(self.khl_bot)` |
| `_global_ctx_mgr` | `core/context_manager.py` L167 | `bot.py` L98 `init_context()` |
| `_instance` | `core/config.py` L261 | `bot.py` L54 `load_bot_config()` |
| `_logger` | `core/logger.py` L75 | `bot.py` L65 `init_logger()` |
| `_channel_cache` | `services/sender.py` L28 | dispatcher 回调 `cache_channel()` |
| `init_username(bot)` | `utils/username.py` | `bot.py` L93-95 |

> 全部由 `bot.py` 作为"上帝组装器"注入。重构时可考虑用 DI 容器统一管理，避免各 singleton 被散点读写。

---

## 5. import 副作用（模块顶层导入时执行的有副作用代码）

| 文件 | 副作用 | 严重度 |
|------|--------|--------|
| `modules/judge.py` L328 `init_keywords()` | **模块导入即执行** `_load_keywords()`，它会 `get_config()` + `open(config/bot_config.toml)` 读盘（L157-160）。每次 import judge 都会触发一次磁盘读 + 可能触发 config 懒加载。 | 🟠 中 |
| `bot.py` L25 `logger = get_logger("bot")` | 若 logger 未初始化，`get_logger()`（logger.py L187-189）会回退调用 `init_logger()`，创建 `logs/huanmeng.log` 文件 + 打开 `TimedRotatingFileHandler` + 尝试 import `core.log_server`（L165）。**import bot 即产生文件 I/O 与 handler 初始化**。 | 🟠 中 |
| `core/queues.py` L21 `_render_semaphore = asyncio.Semaphore(2)` | import 即创建一个 Semaphore 对象（绑定 event loop 前创建，运行期随当前 loop）。 | 🟢 低 |
| `core/config.py` 顶层 `_CONFIG_DIR`/`_PROJECT_ROOT` | 仅路径计算，无盘 I/O。`load_dotenv` 在 try/except 中按需导入，不执行。`get_config()` 首次调用才读盘。 | 🟢 无 |
| `core/context_manager.py` 顶层 | 仅常量 + `_global_ctx_mgr=None`。磁盘恢复在 `ContextManager.__init__`（L40 `_load_from_disk`），由 `init_context()` 触发，非导入时执行。 | 🟢 无 |
| `core/logger.py` 顶层 | 仅常量定义。目录创建在 `_get_log_dir()`（L85-91），由 `init_logger()` 触发。 | 🟢 无 |
| `services/llm.py` 顶层 | 仅 `from openai import OpenAI`（库导入，不建连）。schema/技能文件均在函数内懒加载。 | 🟢 无 |
| `services/sender.py` 顶层 | 仅 `from khl import MessageTypes` + 模块全局 `_bot=None`。 | 🟢 无 |
| `modules/search.py` / `modules/memory.py` / `core/tools.py` / `core/user_profile.py` / `core/dispatcher.py` | 无顶层副作用。 | 🟢 无 |

> 🟠 建议：`judge.py` 的 `init_keywords()` 应从模块底部移到显式初始化入口（如 bot.py 已调用的 `init_keywords()`，L106），或改为惰性初始化，避免 import 触发读盘。

---

## 6. 目标架构依赖层级图（含反向依赖标注）

```
        ┌────────────────────────────────────────────────────────────┐
        │                        main.py                             │
        │   (在 main() 内延迟 import bot / core.logger)              │
        └───────────────────────────┬────────────────────────────────┘
                                    │
        ┌───────────────────────────▼────────────────────────────────┐
        │                        bot.py  (组装器/上帝注入)            │
        │  注入: sender._bot / dispatcher._current_dispatcher /       │
        │        ctx._global_ctx_mgr / config._instance /             │
        │        logger._logger / username                          │
        └──┬──────────┬───────────┬───────────┬───────────────────────┘
           │          │           │           │
   ┌───────▼──┐ ┌─────▼────┐ ┌────▼─────┐ ┌───▼────────┐
   │ core.logger│ │core.config│ │core.dispatcher│ │utils.format_lang│
   └─────────┘ └──────────┘ └────┬─────┘ └────────────┘
                                 │ 顶层
                        ┌────────▼────────┐
                        │   core.queues  │──(懒)→ core.pipeline
                        └─────────────────┘
                        ┌─────────────────┐
                        │  core.pipeline  │◀── dispatcher/queues
                        └──┬────┬────┬────┘
        ┌───────────────────┘    │    └───────────────────┐
        │ 顶层                   │ 顶层                   │ 顶层
   ┌────▼────────┐      ┌────────▼────────┐      ┌────────▼────────┐
   │ modules.judge│←──┐  │  modules.memory │      │  services.llm   │
   │ modules.fav  │   │  │  modules.fav    │      │  services.sender │
   │ modules.commands│  │  │  modules.commands│     └────────┬────────┘
   │ modules.search│   │  │  modules.search │               │ 顶层
   └────┬────────┘   │  └────────┬────────┘          ┌──────▼──────┐
        │ 顶层        │           │ 拓扑              │  services/*  │
   ┌────▼────────┐   │  ┌────────▼────────┐          └─────────────┘
   │  services.llm│───┘  │  services.sender│
   └─────────────┘ (懒)  └─────────────────┘
```

### 6.1 关键反向依赖结论（plugin 解耦障碍）

**① `core/pipeline.py` 是最大的反向依赖源（顶层）**
- 🚨 顶层 import：`modules.judge / modules.memory / modules.fav / modules.commands / modules.search`（5 个 modules）+ `services.llm / services.sender`（2 个 services）。
- 这意味着：**只要 import `core.dispatcher`（bot.py L110），就会连带加载整个 modules 指令体系**，plugins 无法被按需加载。

**② `core/tools.py`（函数内反向依赖）**
- 顶层虽干净，但 `execute_tool/_write_code/_agent_think/_read_url/_system_status` 在函数内 import `services.llm / services.sender / modules.commands / modules.search / modules.local_search / services.pc_status`。工具系统是 core 与 modules/services 的"胶水"层，需重构为插件注册式。

**③ `core/dispatcher.py` / `core/queues.py`（core→core 但连通 modules）**
- dispatcher 顶层 import `core.pipeline`；queues 懒 import `core.pipeline`。因 pipeline 下拉 modules，dispatch 链最终仍耦合 modules。

**④ `core/config.py` 反向借用 `get_architecture_context`**
- `config.py` L10 顶层 `from core.arch_loader import get_architecture_context`，而 `pipeline.py` L315 却 `from core.config import get_architecture_context`（经 config re-export）。语义混乱，应统一从 `core.arch_loader` 导入。

---

## 7. 重构建议（按优先级）

1. **斩断 `core→modules` 顶层依赖**：将 `core/pipeline.py` 顶层的 `modules.judge/memory/fav/commands/search` 与 `services.llm/sender` 改为函数内延迟 import（参考 `core/tools.py` 的隔离模式），或抽接口注入。
2. **`core/tools.py` 改为插件式工具注册**：把 `_TOOL_CMD_MAP` 与 `execute_tool` 的 handler 解析改为运行时注册表，由 plugins 层向 core 注册，而非 core 反向拉取。
3. **消除 `judge.py` 导入副作用**：把 `init_keywords()`（L328）从模块底部移入显式入口，避免 import 触发读盘。
4. **统一单例注入**：将 bot.py 手写的 7 处 singleton 注入（sender._bot / dispatcher._current_dispatcher / context_mgr / config._instance / logger._logger / username）收敛到 DI 容器或显式初始化协议。
5. **修正 `get_architecture_context` 归属**：统一从 `core.arch_loader` 导入，去除 `core.config` 的 re-export 依赖。
6. **为 `services.llm⇄modules.commands`、`services.llm⇄core.tools` 的懒加载回环加注释约束**，防止后续重构误改成顶层导入导致导入死锁。

---

## 8. 审计边界说明

- 本文档聚焦任务指定的 15 个核心文件 + 关键支撑（`core/arch_loader.py/context/token_tracker/log_server`、`utils/format_lang/username/writing` 的引入关系）。
- `modules/pgr/earthquake/nasa/agnes/voice/ping/op/weather/changelog/whois_lookup/tuf_commands/fav/local_search/web_search/gh/auto_update/remind/stm/error_report/holiday/preset/ignore_users/self_ignore` 等叶模块仅记录了被谁 import，未逐一展开其内部 import（其对 services/core 的依赖方向与已审计模块一致）。
- 未运行 import 图静态分析工具，结论基于逐文件 Read/Grep 人工核验。