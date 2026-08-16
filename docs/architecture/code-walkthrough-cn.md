# 幻梦 KOOK Bot 代码走读分析

> 仓库：`Trusler258/huanmeng-kook-bot`（v2.0.1 Hotfix，MIT License，69 commits）
> 分析时间：2026-08-16
> 代码规模：约 5.4 万行 Python（237 个文件，不含 .git）

---

## 一、项目定位

一个**由 LLM 驱动、工程化程度很高的 KOOK 群聊机器人**，基于社区 SDK `khl.py` 构建。核心差异化能力：

1. **跨平台实时歌词同步**（Windows SMTC / macOS Music.app + Spotify，3 歌词源 + 预测算法，精度 <100ms）
2. **三层记忆系统**（RAM 环形缓冲 → JSON 短期 → Markdown 长期，另建 SQLite/FTS5 数据层）
3. **好感度系统**（每人/每群 ±100 分，自动改变回复语气，-100 拉黑）
4. **Lua 插件沙箱**（lupa，剔除危险库 + 超时 + 调用预算）
5. **多 provider LLM 负载均衡**（官方推荐 DeepSeek，兼容 OpenAI 协议，槽位级故障切换）

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层  main.py（参数解析/信号处理）→ bot.py HuanmengBot      │
├─────────────────────────────────────────────────────────────┤
│ 编排层  core/                                                │
│  ├─ dispatcher.py   消息分发（on_message → dispatch）        │
│  ├─ pipeline.py     14 步消息处理流水线（约 830 行核心）     │
│  ├─ queues.py       每频道独立 asyncio.Queue + Worker 隔离   │
│  ├─ config.py       配置加载/热重载/权限标签/技能组装         │
│  ├─ context_manager.py   RAM 环形缓冲（per-chat FIFO）      │
│  ├─ agent/           Agent 子流程（planner/executor/verifier）│
│  ├─ capability/      Capability 注册/路由/按需注入           │
│  ├─ memory_engine/   2.0 新记忆引擎（SQLite+FTS5）           │
│  ├─ permissions/     14 权限位 + 高风险操作人工审批           │
│  ├─ plugin/          Python/Lua 插件管理器 + LuaSandbox      │
│  ├─ resilience/      熔断器 CircuitBreaker / 降级策略         │
│  └─ tool_runtime/    工具调用运行时 + 权限校验                │
├─────────────────────────────────────────────────────────────┤
│ 服务层  services/                                            │
│  ├─ llm.py           LLM 路由（4 槽位：reply/judge/cheap/image）│
│  ├─ delivery/        消息发送编排（formatter→policy→transport）│
│  ├─ tts.py           Qwen3-TTS 节点（TCP 62003）             │
│  ├─ pc_status.py     TCP 接收端（62002，PC 状态/歌词上报）    │
│  └─ image_api.py / tuf_api / pgr / nasa / wdsj_*            │
├─────────────────────────────────────────────────────────────┤
│ 功能层  modules/（30+ 插件模块，44 文件）                    │
│  commands.py（150KB 命令分发）/ wzq 五子棋 / cchess 象棋      │
│  reming 提醒 / weather / translate / 音游三件套 / 记忆记忆...  │
├─────────────────────────────────────────────────────────────┤
│ 技能层  skills/（12 个系统提示词，按文件名排序组装）          │
├─────────────────────────────────────────────────────────────┤
│ 客户端  scripts/（pc_status_reporter / mac_status_reporter） │
│         TCP 长连接 → 主服务 62002/62003，AUTH 密钥鉴权        │
└─────────────────────────────────────────────────────────────┘
```

数据层：`db/`（SQLAlchemy + Alembic 迁移 + FTS5 全文检索）+ `data/`（JSON/Markdown/HTML 模板）。

---

## 三、核心：14 步消息处理流水线

实现位置：`core/pipeline.py::process_message`（L139–1168，约 830 行）

| 步骤 | 文件/行 | 职责 |
|---|---|---|
| 1. 提示词注入拦截 | pipeline L187–206 | 检测 `{{...}}`，仅 admin 可 set/clear preset |
| 2. 引用消息注入 | L208–212 | quoted_msg 写入上下文 |
| 3. 上下文写入 | L213–249 | role_tag / fav 标签行、RAM 缓冲、STM 写入、memory_engine.observe |
| 4. 指令拦截 | L251–262 | `.` 前缀 → `_handle_command_route` |
| 5. 回复决策 | L272–309 | at_only / 私聊直回 / should_respond 判断 |
| 6. 刷屏检测 | modules/judge.py L210 | should_quick_reject 快拒（旧版 spam_guard 未接入 KOOK 管道） |
| 7. 自动搜索 | L696–720 | auto_search_if_needed |
| 8. 记忆+好感+时间 | L359–434 | get_top_memories / get_msglog_context / fav 注入 |
| 9. LLM 生成 | L722–733 | generate_multi_reply_with_tools |
| 10. 上下文回写 | L981–994 | 生成结果写回缓冲 |
| 11. 任务调度 | L1026–1041 | cancel_old_task + send_sentences（多句流式） |
| 12. 好感度更新 | L1156–1158 | 依据交互结果调分 |
| 13. 自动记忆保存 | L1160–1162 | maybe_save_memory（600s 冷却） |

另以 Phase 注释标注了 2.0 新阶段：意图分类（L311）、复杂度评估（L322）、Agent 适配（L337）、Context Engine 分桶（L383）、Capability 解析（L574）。

---

## 四、关键子系统详解

### 4.1 三层记忆系统

| 层 | 实现 | 容量/策略 |
|---|---|---|
| 即时 | `core/context_manager.py::memory_buffer`（per-chat FIFO）；`memory_engine/working.py::WorkingMemory` | 60 条上限，溢出 10 条触发提炼 |
| 短期 | `modules/stm.py`，`data/stm/stm_{chat_id}.json` | 滚动 30 条，溢出转长时 |
| 长期 | `modules/memory.py`，`data/memory_{chat_id}.md` | 模板压缩 + 自动滚动；扫描上限 3000 条 / 取 Top 10 |

2.0 新增 SQLite/FTS5 数据层：`core/memory_engine/types.py::MemoryRecord`（8 类记忆 + 5 状态）+ `db/migrations`（Alembic）。

### 4.2 好感度系统（modules/fav.py）

- 存储：`data/fav.json`，key 为 `g{群}:{uid}` / `p:{uid}`
- 数值：-100 ~ +100，默认 50，101 级
- 注入：pipeline 在 extra_info 注入「好感度：x/100」，上下文行带 `[fav=xx]`
- 拉黑：`fav <= -100` 直接忽略该用户消息

### 4.3 实时歌词同步（scripts/pc_status_reporter.py）

- **歌词源**：LRCLIB（精确+模糊分列）、QQ 音乐、网易云，阶梯提交 + `_score_lyric_match` 评分选优
- **预测器**：5 点滑窗最小二乘拟合速率 `rate_ms_per_wallms`，融合 `0.6×预测 + 0.4×实测`，clamp [0.90, 1.10]；seek 跳变 >3s 重置窗口
- **双通道投递**：TIMER 定时精确发下一句 + 80ms/10ms tick 主循环兜底；`_apply_drift_step` 渐进校正；diff=2/3 快速补发；`_lyric_event_queue`（deque）防 80ms 内多句丢失
- **服务端**：`services/pc_status.py` 校验 AUTH 后更新 `_PC_DATA`，支持 SHOT 截图、OFFSET_* 偏移校准下行协议

### 4.4 Lua 插件沙箱（core/plugin/lua.py）

- 加载：`loader.discover_plugins` 扫描 manifest.json（runtime 分 python/lua）
- 安全：剔除 os/io/package/require/dofile/loadfile/debug 等危险全局；线程内执行 + `LUA_TIMEOUT` 超时，超时置 `_poisoned` 重建；`_guard` 限制 bridge 调用预算
- bridge API：`command` / `on_event` / `every` / `send` / `remember` / `recall` / `publish` / `config`
- 示例：`plugins/greet/main.lua`、`plugins/echo/main.py`

### 4.5 LLM 路由（services/llm.py）

- 4 个槽位各绑 provider：reply（主回复）/ judge（判定）/ cheap（翻译等轻任务）/ image（视觉）
- 故障切换链：`call_judgment_pipeline` 用 `asyncio.gather` 并行 cheap+interest（同模型合并一次）；空内容 json_mode 重试；strict 模式失败回退普通客户端；连续失败走 persona fallback（`core/persona.py`）
- DeepSeek 前缀缓存：系统提示词跨调用复用，实测命中率约 89%

### 4.6 权限体系

- **群组级**（core/config.py::get_user_tag）：admin（admin_qq）> op（op_qqs / 分频道 group_owners）> friend（friend_qqs）> member
- **工具级**（core/permissions/types.py）：14 权限位，默认 DENY；HIGH 风险操作走 `approval.py` 人工审批
- 注意：`admin_qq`/`op_qqs` 为遗留命名，实际存 **KOOK 数字用户 ID**，与 QQ 无关

### 4.7 每频道 Worker 隔离（core/queues.py）

- `_group_queues` / `_group_tasks` 两个 dict，每个 chat 独立 `asyncio.Queue` + `_group_worker`
- 串行消费，每条消息用 `_trace_meta` 重建 RequestContext 防止 span 跨消息累积
- 渲染走独立队列，慢任务不阻塞其他频道

### 4.8 配置热重载

三条触发路径：
1. 控制文件 `data/control.txt`（`bot.py::_bg_control_watcher`，每秒轮询 `echo reload >`）
2. SIGUSR1（main.py，仅非 Windows）
3. 指令 `.reload`（commands.py，校验 admin）

核心 `core/config.py::reload_config`（L493–505）重建 BotConfig，保留 bot_id；bot.py 附带重载技能缓存。

---

## 五、游戏模块

| 游戏 | 实现 | AI |
|---|---|---|
| 五子棋 modules/wzq.py | PvP 决斗 + 人机，禁手 `_check_forbidden` | `ThreadPoolExecutor(1)` + α-β minimax；4 档难度（easy 深1+30%随机 → expert 迭代加深 1→3 深、8s 时限） |
| 中国象棋 modules/cchess/ | 自包含棋盘引擎（Board/push_uci，非外部进程） | α-β minimax（AI_DEPTH=2）+ 子力估值；SVG 棋盘渲染 |

---

## 六、技能层（skills/，12 个系统提示词）

`01 prompt_header`（人设头）→ `02 persona_lock`（人格锁定）→ `03 group_format`（群聊 KMarkdown）→ `04 private_format`（私聊 JSON）→ `05 fav_format` / `06 fav_tiers`（好感度）→ `07 anti_repeat`（防复读）→ `08 command_tools`（函数调用指令）→ `09 private_tone` → `10 play_mode`（扮演）→ `11 face_lib`（表情库）→ `12 kook_sdk`（KOOK 格式）。

组装：`core/config.py::build_system_prompt` 按文件名排序读取；`@scope:private` 标注的仅私聊使用；`llm.py::_load_skill_sections` 解析 `## 节名`，热重载可刷新缓存。

---

## 七、客户端配套（scripts/）

| 脚本 | 平台 | 功能 |
|---|---|---|
| pc_status_reporter.py | Windows | SMTC 轮询正在播放 + 系统/GPU/网络状态 + 歌词事件 |
| mac_status_reporter.py | macOS | AppleScript 查 Music.app/Spotify |

通信：TCP 长连接 `BOT_SERVER` + 多端口 `BOT_PC_PORTS`，首行 `AUTH <BOT_PC_KEY>`，随后逐行 JSON payload。

---

## 八、工程化亮点

1. **Alembic 数据库迁移**：db/migrations 有正式迁移管线，2.0 数据层走 SQLAlchemy
2. **熔断与降级**：core/resilience 提供 CircuitBreaker + DegradationPolicy，异常时通知卡片（notify_system）
3. **GitHub Actions**：.github/workflows/notify.yml push 后 Webhook 秒推更新卡片
4. **测试覆盖**：tests/ 18 个文件，按 Phase 组织（phase6~20），覆盖上下文/队列/Agent/权限/可靠性等
5. **审计文档**：docs/architecture/ 含 dependency / logging / performance / pipeline 四份审计报告
6. **自更新管线**：modules/_auto_update/（analyzer/engine/patcher/safe_update/severity/snapshot/state）

---

## 九、值得注意的观察

1. **命名遗留**：`admin_qq`、`op_qqs`、`qq_name_map` 等字段名仍为 QQ 时代命名，实为 KOOK 数字 ID，新读者易误解（README 已专门说明）
2. **流水线文档漂移**：14 步流程在 `data/*.mermaid` 文档与 `pipeline.py` 代码间有差异（文档为旧版 QQ 流程，代码含 2.0 新 Phase），读代码以 pipeline.py 为准
3. **刷屏检测未完全接入**：spam_guard 模块存在但 KOOK 管道未调用，仅 judge.py 的 should_quick_reject 兜底
4. **主文件高密度**：commands.py 达 150KB、pipeline.py 单函数约 830 行，后续维护建议按域拆分
5. **代码规模**：5.4 万行中 core 62 文件 + services 24 文件承载了大部分复杂度，功能模块相对轻量——架构重心在"编排"而非"功能堆叠"

---

## 十、扩展建议

1. **新增命令**：在 modules/ 下新建模块，注册进 COMMAND_MAP，配置后即可被流水线命中
2. **新增歌词源**：在 pc_status_reporter.py 的阶梯提交链中追加 `_xxx_fetch_lyrics` 并实现 `_score_lyric_match` 兼容评分
3. **新增技能**：skills/ 下按 `NN_名称.md` 编号命名，重启或 .reload 后自动组装进系统提示词
4. **Lua 插件**：在 plugins/ 下建含 manifest.json 的目录，用 bridge 暴露的命令/事件/定时器开发轻量功能
5. **迁移旧 JSON 记忆**：2.0 memory_engine 提供 SQLite/FTS5 新线，可通过 db/migrations 增量迁移 data/ 下的历史 JSON/Markdown
