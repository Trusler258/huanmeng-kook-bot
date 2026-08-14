# Huanmeng 2.0 Phase 0 审计：`core/pipeline.py`

> 审计对象：`core/pipeline.py`（`process_message` 主管道，约 1087 行长文件中的绝对核心）
> 目标：为 Huanmeng 2.0 重构的 Phase 0 提供基于真实源码的凭证式审计。
> 说明：本文所有行号均指向 `core/pipeline.py` 当前版本；所有职责归类均从真实代码调用出发，无臆测。

---

## 1. 概述

`core/pipeline.py` 是 KOOK 聊天机器人的单条消息处理"上帝函数"载体。整条消息调用链为：

```
main.py → bot.py(HuanmengBot) → core/dispatcher.py(EventDispatcher.dispatch/_dispatch_inner)
       → core/queues.py(enqueue_message → per-chat worker)
       → core/pipeline.py(process_message)            ← 本文件
       → services/llm.py(generate_multi_reply_with_tools)
       → services/sender.py(send_sentences/send_by_chat_type)
       → 回写 core/context_manager.py
```

`pipeline.py` 共 1087 行，内含 8 个顶格函数 + 1 个模块级正则：

| 函数 | 行范围 | 代码量 | 定位 |
|---|---|---|---|
| `_clean_reply` | 10–25 | 16 行 | 回复后处理（去括号动作） |
| `_clean_name` | 46–47 | 2 行 | 清洗不可见字符 |
| `handle_poke_event` | 51–136 | 86 行 | 戳一戳事件处理 |
| `process_message` | 139–968 | **830 行** | 消息主管道（God function） |
| `_async_extract_profile` | 971–998 | 28 行 | 后台用户画像提取 |
| `_handle_command_route` | 1002–1023 | 22 行 | 指令路由 |
| `_send_test_card` | 1027–1049 | 23 行 | 测试卡片 |
| `get_msglog_context` | 1053–1066 | 14 行 | msglog 回溯搜索 |
| `_build_at_list` | 1069–1087 | 19 行 | 构建可 @ 用户列表 |

**结论先行**：`process_message`（830 行）在单个函数内同时承担了 8 个目标架构层的职责，且大量依赖函数体内部 `from ... import ...` 的隐式导入。这是 Huanmeng 2.0 最需要拆分的单体。

---

## 2. 目标架构层定义（Huanmeng 2.0）

本文使用的目标层（用于第 3、4 节归类）：

- **Conversation Runtime（对话运行时）**：消息生命周期编排、路由分派、各阶段协调——即"这段逻辑属于主流程调度"。
- **Context Engine（上下文引擎）**：对话历史存取、上下文裁剪、buffer 缓冲、上下文回写格式化。
- **Memory Engine（记忆引擎）**：长期记忆检索/写入、msglog 回溯、ST 抄送、记忆压缩。
- **Agent Runtime（LLM Agent / Planner/Executor/Verifier）**：LLM 调用、FC 工具循环、回复 JSON 解析、判回复/判搜索等模型编排。
- **Capability（能力）**：搜索/识图/编程/写作/画像等具体能力。
- **Service（泛服务）**：发送器、配置、日志、缓存等基础设施。
- **Plugin（插件）**：以模块化方式接入的独立功能（指令、preset、模式、节假日等）。
- **Response Delivery（回复投递）**：句子分批发送、卡片修复、表情/动作/指令结果回发等投递编排。

---

## 3. `process_message` 逐段职责归类表

> 行号区间为 `process_message`（139–968）内的相对职责段。『当前实现』列标注该段实际调用了哪些外部能力。

| 行范围 | 当前职责 / 代码片段 | 调用的外部能力 | 目标层 | 建议提取方式 |
|---|---|---|---|---|
| 139–152 | 函数签名、初始化 `get_context_mgr`/`get_config`、`_clean_name` | `core.context_manager`、`core.config` | Conversation Runtime | 抽取为 `MessageContext` 装配器（依赖注入，去掉隐式全局单例） |
| 154–164 | 图片/引用图片占位注入上下文 | `ctx.append_to_context` | Context Engine | 归入上下文写入器，由 dispatcher 传入结构化附件元数据 |
| 166–168 | 首次对话自动注册好感度 `ensure_fav` | `modules.fav` | Capability（关系） | 抽 `FavLifecycle`，在对话建立阶段调用 |
| 170–183 | 清洗不可见字符（`\u200b` 等）；全不可见则跳过 | 正则 + `re` | Conversation Runtime | 抽 `TextSanitizer`（注意：171 行重复 `import re as _re`） |
| 185–205 | 管理员提示词注入（`{{` 检测、preset 注入/reset） | `modules.preset`、`cfg.get_user_tag` | Plugin（preset） | 抽成 preset 插件钩子，避免 in-pipeline 分支 |
| 207–211 | 引用消息注入上下文 | `ctx.append_to_context` | Context Engine | 归入上下文写入器 |
| 213–238 | 角色标签/显示名/好感度计算 + 写 context_line + buffer + STM | `cfg.get_user_tag`、`cfg.get_display_name`、`modules.fav.get_fav`、`ctx.*`、`modules.stm` | Context Engine | 抽 `ContextRecordBuilder`（含 STM 抄送） |
| 240–255 | 指令拦截（`.` 前缀 → `_handle_command_route`） | `modules.commands.handle_command` | Conversation Runtime | 抽 `CommandInterceptor`，独立于对话主流程 |
| 257–259 | 非指令/非纯指令才写 LLM 上下文 | `ctx.append_to_context` | Context Engine | 归入写入器 |
| 261–296 | 回复判断（at_only/私聊直回/`should_respond` 模型判断） | `modules.judge.should_respond`、正则 | Agent Runtime（Verifier） | 抽 `ReplyDecider`，纳入 Agent 的 Verifier 阶段 |
| 298–302 | 自忽略机制 | `services.self_ignore` | Service | 抽 `IgnoreGuard`，在入队阶段即可拦截（前移到 dispatcher/queues，减少无效计算） |
| 305–399 | 记忆检索 + 好感度 + 上下文组装（`extra_info_parts`） | `modules.memory.get_top_memories`、`modules.holiday`、`modules.preset`、`get_msglog_context`、`services.image_api.get_recent_image_descriptions`、`modules.op` 模式、`_build_at_list`、架构上下文 | **Memory Engine + Capability（识图）+ Plugin（preset/模式）** | 拆为 `ContextAssembler`（记忆/时间/节假日/图片/主人提示/模式各为独立注入件），通过"上下文增强器"列表聚合 |
| 305–308 | `full_msg` + 记忆检索 + fav | `get_top_memories`、`get_fav` | Memory Engine | 见上 |
| 310–319 | 架构关键词检测 → 注入架构上下文 | `core.config.get_architecture_context`（经 `core.config` 从 `core.arch_loader` 再导出） | Capability（架构问答） | 抽为按需上下文增强器，去掉 `except Exception` 兜底 |
| 321–327 | 当前时间字符串 | `datetime` | Service | 抽 `TimeStampProvider` |
| 330–336 | 节假日信息 | `modules.holiday` | Plugin | 抽为上下文增强器 |
| 338–343 | preset 注入 | `modules.preset.get_preset` | Plugin | 抽为上下文增强器 |
| 345–351 | msglog 回溯（记忆不足 300 字时） | `get_msglog_context` → `modules.memory.search_msglog` | Memory Engine | 抽为记忆增强器，走统一记忆接口 |
| 353–366 | 最近图片描述（识图语境） | `services.image_api.get_recent_image_descriptions` | Capability（识图） | 抽为识图增强器 |
| 368 | fav 提示 | `get_fav` | Capability（关系） | 见上 |
| 370–373 | 可 @ 用户列表 | `_build_at_list` | Context Engine | 见上 |
| 375–376 | 追加架构上下文 | — | Capability | 见上 |
| 378–387 | 主人/OP 提示 | `cfg.group_owners`、`cfg.get_display_name` | Plugin（权限提示） | 抽为增强器 |
| 389–397 | 模式（sleeping/narrative）提示 | `modules.op` | Plugin（模式） | 抽为增强器 |
| 401–408 | 用户画像注入 | `core.user_profile.build_profile_text` | Capability（画像） | 抽为增强器 |
| 413–424 | 错误报告隔离（临时替换上下文） | `modules.error_report.build_error_report_prompt` | Capability（错误报告） | 抽为独立 Capability，隔离上下文由 Agent 层处理 |
| 426–464 | 系统提示词 + persona 全替换/回退 | `modules.op.get_persona`、`modules.memory.set_persona_override`、`cfg.*` | Agent Runtime | 抽 `SystemPromptFactory` |
| 466–468 | 死代码说明注释（工具预选已删除） | — | — | 删除注释，归档到 git 历史 |
| 470–488 | Agent 写作路由（`is_writing_request` → `generate_and_send_file`） | `utils.writing` | Capability（写作） | 抽为写作 Capability，由 Agent/Planner 路由 |
| 490–523 | 编程路由（长代码题 → `_write_code`） | `core.tools._write_code`、`services.sender` | Capability（编程） | 抽为编程 Capability |
| 525–534 | 自动搜索（关键词/实时/模型判断） | `modules.search.auto_search_if_needed` | Capability（搜索） | 抽为搜索 Capability，结果进入上下文增强器 |
| 536–544 | 主 LLM 调用 `generate_multi_reply_with_tools` | `services.llm` | Agent Runtime（Executer） | 抽为 Agent 的 Executor 步骤 |
| 546–559 | 空回复降级提示 | `services.sender.send_by_chat_type` | Response Delivery | 抽 `ReplyFallback` |
| 561–572 | 垃圾过滤（回显上下文格式）+ `_clean_reply` | 正则、`format_lang` | Response Delivery | 抽 `ReplyNormalizer` |
| 574–576 | actor 以真实发送者为准（防 LLM 伪造） | — | Agent Runtime | 归入 Agent 层安全校验 |
| 578–597 | `[FILE:...]` 临时文件生成 + 定时清理 | `tempfile`、`Path`、`asyncio` | Response Delivery | 抽 `FileDelivery` |
| 599–672 | 倒计时卡片兜底/修复（`_cd_re`/`_cd_loose` 启发式） | 正则、`json` | Response Delivery | 抽 `CountdownCardRepair`（独立于 pipeline） |
| 674–698 | 从回复扫描内联 `.commands` 并提取 CALL | `modules.commands.COMMAND_MAP` | Agent Runtime（Planner 后处理） | 抽 `InlineCommandScan` |
| 700–748 | CALL 执行（`handle_command` + 错误处理 + 发文件延迟队列） | `modules.commands.handle_command`、`services.sender` | Response Delivery + Plugin（指令） | 抽 `CallExecutor`（统一指令执行入口） |
| 750–769 | `is_at_me` 判定 + CALL 提示回写上下文 | `ctx.append_to_context` | Context Engine | 归入回写器 |
| 771–779 | 表情处理（`[FACE]` 残留去除 / face_cq 保留） | 正则 | Response Delivery | 抽 `FaceHandler` |
| 781–794 | 上下文回写（`[CARD]`→`[卡片]` 等标记简化） | `ctx.append_to_context/buffer` | Context Engine | 抽 `ContextWriter`，统一标记简化规则 |
| 796–810 | action 动作追加 + @ 处理（`(met)…(met)` 替换） | 正则 | Response Delivery | 抽 `ReplyFormatter` |
| 811–823 | mode 切换（`_load_modes`/`_save_modes`） | `modules.op` | Plugin（模式） | 抽模式插件，由 Agent 输出触发 |
| 825–849 | 发送任务创建/取消旧任务 + face 后发 | `ctx.cancel_old_task`、`services.sender.send_sentences`、`asyncio` | Response Delivery | 抽 `DeliveryCoordinator` |
| 851–953 | **CALL 结果回发 + 嵌套 LLM follow-up（约 100 行闭包）** | `handle_command`、`services.llm.call_llm`、`_build_system_text`、`services.sender` | Response Delivery + Agent Runtime | 抽 `CallResultHandler`，LLM follow-up 归入 Agent 层 |
| 955–958 | 好感度调整 | `modules.fav.update_fav` | Capability（关系） | 抽 `FavUpdater` |
| 960–962 | 自动记忆 | `modules.memory.maybe_save_memory` | Memory Engine | 抽 `MemoryWriter`，异步队列写入 |
| 964 | 完成日志 | `logger` | Service | — |
| 966–967 | 后台抽取用户画像 | `asyncio.ensure_future(_async_extract_profile)` | Capability（画像） | 抽画像异步任务，纳入工作队列 |

> 归类要点：`process_message` 的 830 行几乎覆盖了除 `Plugin` 实现外的全部目标层——它是"编排 + 上下文 + 记忆 + 能力调度 + 回复投递"的合体，是 Huanmeng 2.0 拆分的主战场。

---

## 4. 其余函数职责归类

| 函数 | 行范围 | 目标层 | 建议 |
|---|---|---|---|
| `handle_poke_event` | 51–136 | Conversation Runtime + Memory Engine + Capability（关系）+ Response Delivery | 与 `process_message` 高度重复（上下文组装/记忆/好感度/LLM/发送/记忆写入），应复用同一套 Agent + Delivery 管线，仅差异化为"戳一戳"插件 |
| `_async_extract_profile` | 971–998 | Capability（画像） | 保留为独立后台任务；注意 974–976 行在函数内 import |
| `_handle_command_route` | 1002–1023 | Conversation Runtime | 与 process_message 内 CALL 执行（700–748）形成**两套平行的指令分发路径**，应统一 |
| `_send_test_card` | 1027–1049 | Response Delivery | 纯投递，可归入测试插件 |
| `get_msglog_context` | 1053–1066 | Memory Engine | 归入记忆检索接口 |
| `_build_at_list` | 1069–1087 | Context Engine | 归入上下文增强器 |
| `_clean_reply` / `_clean_name` | 10–25 / 46–47 | Response Delivery / Service | 各自独立工具，可下沉到 `utils/` |

---

## 5. 具体问题清单（含行号）

### 5.1 职责混杂（God function）
- **P1** `process_message` 单函数 830 行（139–968），同时承担编排、上下文、记忆、能力路由、回复投递 8 层职责。见第 3 节表格，几乎每 20–40 行切换一个目标层。
- **P1** `_send_call_results` 为函数体内嵌套的约 100 行闭包（853–953），内含**第二次 LLM 调用**（`call_llm`、`_build_system_text`，919–922、912–913），把"Agent 生成后续回复"塞进了投递回调。
- **P2** `handle_poke_event`（51–136）与 `process_message` 的 305–963 段存在大量复制粘贴式的重复逻辑（记忆检索、好感度、模式、LLM 生成、发送、记忆写入），两处需要各自维护。

### 5.2 函数过长
- **P1** `process_message`：830 行。
- **P2** `_send_call_results` 闭包（853–953）：约 100 行。
- **P2** `handle_poke_event`：86 行。

### 5.3 隐式 import（函数体内 `from … import …`）
以下 import 全部位于函数体内，按需才加载，导致：依赖不可静态发现、循环导入规避混乱、`except ImportError` 被当作功能开关而掩盖真实错误。
- `handle_poke_event`：52、58、77、93（`from modules.op import …`）
- `process_message`：148、167、189、217、237、299、315、331、338、358、390、403、416、437、445、472、502、511、587、635、686、712、741、814、885、912、928、951
- `_async_extract_profile`：974、994
- `_handle_command_route`：1003
- `_send_test_card`：1028

### 5.4 全局状态
- **P1** 通过 `get_context_mgr()`（148 行）拉取全局单例 `ContextManager`（`core/context_manager.py:167–174`），`process_message` 全程直接写 `group_context`/`memory_buffer`/`active_send_tasks`，测试与并发隔离困难。
- **P2** `cfg = get_config()` 全局配置在各函数反复拉取（149、53、1003 等），persona/模式等状态读写（437–451、811–823）直接依托全局字典。
- **P2** `sender.py` 的 `_bot`/`_channel_cache` 模块级全局（`services/sender.py:27–28`），pipeline 依赖其已被 `init_sender` 初始化。

### 5.5 错误处理厚度
- **P1** 大量 `except Exception: pass` **静默吞错**，且无法区分"功能未配置"与"真实异常"：
  - 318–319、335–336、350–351、365–366、407–408、463–464、487–488、822–823、997–998 等。
- **P1** `except ImportError: pass` 被当作"功能探测"（99–100、220–221、396–397、407–408、463–464、487–488），真实缺包/拼写错误会被无声吞掉。例如 315 行 `from core.config import get_architecture_context` 依赖 `core/config.py:10` 的再导出，一旦该再导出被移走即静默失能。
- **P2** 嵌套闭包内 `except Exception: pass`（942、951–952 等）使投递期间的 LLM 错误难以观测。

### 5.6 死代码 / 冗余
- **P1** **未使用 import**：
  - 40 行 `generate_multi_reply`（全文仅 `generate_multi_reply_with_tools` 被使用，105/538 行）。
  - 34 行 `load_memories as _load_memories_for_context`（全文未引用）。
- **P2** **重复 import**：函数体内多次 `import re as _re`（171、242、263、491 行），而模块级 4 行已 `import re`；`from datetime import datetime` 在 70、322 行重复。
- **P2** 410–411 行日志 `"搜索=%d字", …, 0` 硬编码 0，误导（写作路由/编排已改，日志未跟上）。
- **P2** 466–468 行是"已删除死代码"的说明注释，应直接删除（`inject_tool_system`/`try_tool_select`/`get_tool_status` 在 `core/tools.py` 中确实不存在，已核实）。

### 5.7 逻辑正确性 / 脆弱点
- **P2** `sentences` 在"列表 ↔ 字符串（`" || ".join`）↔ 列表"之间反复切换（751→762→776→782），配合 `[CALL:…]`/`[FACE:…]` 正则剥离，极易在后续改动中漏处理一种标记。
- **P2** 782 行 `_context_reply` 用 `\S+` 清除 `[系统] 已调用:` 后缀，但 769 行写入的是 `"、".join` 的多个名字，`\S+` 只匹配到第一个 token，残留后续名字。
- **P2** 指令分发存在**双路径**：540 行起 LLM 返回 `calls` 走 700–748 的 CALL 执行（直接 `handle_command` + 重复 `__EQ_CARD__` 处理 867–872），而用户 `.` 指令走 `_handle_command_route`（250、1002–1023）。两条路径对 `__EQ_CARD__`/错误/发送的处理不统一。
- **P2** 720–727 行 `caller_id = actor["qq"]` 允许 LLM 输出的 `actor` 决定指令执行者身份；虽有 575–576 行的"真实发送者覆盖"，但 `origin=="bot"` 分支（725）会以 `bot_qq` 执行，属权限敏感路径，应在 Agent 层统一鉴权。
- **P3** 604 行 `_cd_loose` 宽松倒计时正则（短消息 + 纯数字单位）可能误命中普通数字文本。
- **P3** 819 行 `__import__("time")` 写法怪异，应直接 `import time`。

### 5.8 外部能力依赖总清单（pipeline 直接调用）
| 能力 | 模块 | pipeline 内调用点 |
|---|---|---|
| 上下文管理 | `core/context_manager.py` | 148、154+ |
| 配置 | `core/config.py` | 149、315（架构上下文） |
| 好感度 | `modules/fav.py` | 167、223、308、957 |
| 记忆 | `modules/memory.py` | 31–36、307、962 |
| msglog 回溯 | `modules/memory.py#search_msglog` | 1053–1066 |
| 指令 | `modules/commands.py` | 250、738、858、1005 |
| 搜索 | `modules/search.py` | 527 |
| 回复判断 | `modules/judge.py` | 289 |
| 写作 | `utils/writing.py` | 472–473 |
| 编程 | `core/tools.py#_write_code` | 511 |
| 识图 | `services/image_api.py` | 358 |
| 画像 | `core/user_profile.py` | 403、974–975 |
| LLM | `services/llm.py` | 40、885、912、919 |
| 发送 | `services/sender.py` | 41、199–201、502–507、555、715、837、846、935、947、1015–1023、1047–1049 |
| 自忽略 | `services/self_ignore.py` | 299 |
| preset | `modules/preset.py` | 189、338 |
| 模式/人格 | `modules/op.py` | 93、217、390、437–439、814 |
| 节假日 | `modules/holiday.py` | 331 |
| 错误报告 | `modules/error_report.py` | 416 |
| STM | `modules/stm.py` | 237 |

> 结论：pipeline 直接耦合了 **5 个 `core.*`、8 个 `services.*`、8 个以上 `modules.*`、2 个 `utils.*`** 模块，这是典型的"编排层与能力层、上下文层、投递层全部同体"的耦合形态。

---

## 6. 重构建议（Phase 0 落地）

1. **先拆 `process_message` 为阶段函数**，每个阶段严格对应第 3 节表格中超出一行范围的职责段，保持 `MessageContext`（结构化数据类）在阶段间传递，替代隐式全局单例。
2. **建立 `ContextWriter` / `MemoryWriter` / `ReplyNormalizer` / `DeliveryCoordinator`** 四个独立服务，把 781–794、960–962、561–672、825–953 从 pipeline 抽出。
3. **统一指令执行入口**：让 CALL 执行（700–748）与 `_handle_command_route`（1002–1023）走同一 `CommandExecutor`，消除 `__EQ_CARD__`/错误处理的重复。
4. **Agent 层接管回复判断与 LLM 编排**：将 261–296（ReplyDecider）、426–464（SystemPromptFactory）、536–544（LLM 调用）、851–953（follow-up）归入 Agent Runtime，pipeline 只做协调。
5. **能力统一走 Capability 接口**：搜索（525–534）、识图（353–366）、编程（490–523）、写作（470–488）、画像（401–408、966–967）各自实现为可插拔 Capability，由 Planner 路由。
6. **清理隐式 import 与死 import**：把函数体内 `from … import …` 收敛为模块级显式导入（或明确的依赖注入），删除 40/34 行未用 import、466–468 注释、171/242/263/491 重复 `re`。
7. **错误处理分级**：以"可配置项缺失"与"运行时异常"区分，替换大面积 `except Exception: pass`；ImportError 仅在真正可选的插件边界使用。

---

## 7. 审计局限

- 本审计聚焦 `core/pipeline.py` 及其直接调用的外部能力；`core/tools.py` 的 FC Agent（`get_tool_schemas`/`execute_tool`/`_write_code`）仅在调用点（511、536–544 经由 `services/llm.py`）被评估，其内部实现属于另一份审计范围。
- 行号基于审计时点源码，重构后需以实际 diff 为准。