# 更新日志

## v2.3.93 — 补齐 2 个工具 + /~ctx 上下文用量（对齐 QQ 第三批）— 2026.10.16

一句话总结：补上跨对话持久工作区与黑话词典两个工具，新增 /~ctx 上下文用量查看。

### 新增
- **`ws_files` 工具**：沙箱持久工作区（`data/sandbox_ws`，跨对话保留），支持 list/read/write；
  配合 `run_code` 使用——运行时自动把工作区文件带入、运行后回写新产出，实现
  「写入数据 → 跑代码处理 → 读回结果」闭环。
- **`learn_slang` 工具**：把新学到的黑话/群内用语记进词典（`data/skills/slang.md`），
  写入后立即注入 system prompt，无需重启、无需改代码。
- **`/~ctx` 指令**：查看最近一次请求构建给 LLM 的上下文构成（系统提示/历史/记忆/技能/
  工具/搜索/任务 各类 token 数与占比），便于观察上下文预算是否合理。

### 说明
- 本轮其余「缺口」经核实多为**非缺口**或**平台不可用**：
  `web_fetch` = `read_url` 仅改名；`load_skill` KOOK 设计本就自动注入技能；
  `recall`（KOOK 平台无撤回事事件/API）、`licenses`（依赖面板）、`admin_notify`
  （QQ 私聊/加群语义）均不适用；`help_card` 已有 KOOK 原生卡片对应。

## v2.3.92 — 插件系统 manifest v2 + 功能追赶第二批（+8 插件，27/27）— 2026.10.16

一句话总结：解除 manifest v2 阻塞、补齐 8 套插件，KOOK 插件数 17 → **27**。

### 一、manifest v1 → v2（此前阻塞，本轮解除）
阻塞原因：KOOK 的 `core/plugin/api.py` 与 QQ 是**双向分叉**，不能单向覆盖。
解法：**以 KOOK 为基底合并**（KOOK 独有全留，QQ 的 v2 能力补入）。
- `core/plugin/manifest.py` 79 → 245 行：支持 manifest_version 1..2，
  v2 新增 commands/tools/config_schema/secrets/storage/limits/network/requires/
  events/hooks/timers/priority/tags；未知字段进 extra 不报错；
  **v1 manifest 解析结果与行为完全不变**（16 个旧 manifest 回归通过）
- `core/plugin/api.py` 638 → 1010 行，10 处合并：
  · 新增 `_perm_ok()` 权限门控 + `_HIGH_RISK_LEAVES`（通配不覆盖 raw 等高危能力）
  · PluginMessage 加 `send_ex`/`send_file_ex`/`delete`/`message_id_of`
    （KOOK 无 NapCat message_id → send_ex 返回 {status:ok} 降级、delete 记 WARNING 返 False）
  · PluginCapability 加 `_bind_command`（插件接管内置指令名）
  · PluginIdentity 加 `is_op`；新增 `PluginI18n`/`PluginFS`/`PluginPlugins`
  · PluginContext 加 `fs`/`i18n`/`plugins`/`has(perm)`；economy 改惰性 property
  · cleanup() 补 `background.cancel_all()`（原版漏了后台任务取消）
  · **保留 KOOK 全部独有**：PluginCard、image.extract_*、sandbox 返 dict、
    pipeline.on_reply/get_pre_hooks；**不搬** PluginNapCat（QQ 平台专属）
- 16 个 manifest.json 升 v2（AST 提取 register_command，兼容关键字/位置参数/
  循环变量/函数别名四种写法）

### 二、两项移植基础设施（让 QQ 插件零修改搬迁）
1. **command handler 签名适配层**：QQ 是 `h(args, user_id, group_id, sender_name,
   is_group, bot_qq)`，KOOK 是 `h(msg)`（_PluginMsg）。register_command 时用
   inspect 检测，≥2 位置参数自动包一层拆 msg 转调；KOOK 风格原样返回不包层。
   ⇒ 本轮 8 套插件全部零修改搬运验证。
2. **文本内联本地图片**：约定 `[图片:<路径>]` 标记，在 send_group_msg /
   send_private_msg / send_by_chat_type 三入口拦截（有标记先发图再发文字），
   并提供 `build_local_image_cq()` 兼容 QQ 移植模块。
3. 另补 `PluginImage.render_html` 委托（QQ 侧在 PluginImage、KOOK 侧在 PluginCard）。

### 三、新增 8 套插件（17 → 27）
| 插件 | 功能 | 适配 |
|---|---|---|
| dice | 掷骰子 + roll_dice 常驻工具 + 积分奖励 | 零适配 |
| steam | 价格/状态/绑定 + /~在干嘛（3 模块 1748 行） | 图片 CQ → send_file；@ 判断兼容 (met) |
| reward | 赞赏码 + 赞助名单 | 同步函数改返 [图片:] 标记 |
| op | OP/人格/主人/睡觉/含蓄/叙事模式（751 行） | 零适配 |
| box | 快递查询 | 零适配（KOOK 已有 send_box_card） |
| ping | Globalping 全球延迟测试 | CQ:at→(met)、图片→send_file |
| gh | 公会登记（+gh_store 50 行） | _resolve_nick 退化为 ID 串 |
| memory_link | 跨聊天记忆关联（478 行） | 零适配（依赖 KOOK 均有） |
| agnes | 文生图/视频/图生视频 | 零适配（KOOK 已有 modules/agnes） |
| voice | 语音回复（LLM→情绪音色→TTS） | 零适配（tts 签名前向兼容） |
| phi | Phigros 曲库/定数/理论值 + TapTap 存档/Best N | 零适配（27 文件） |

### 四、明确不搬/暂缓（非遗漏）
- **wdsj**：KOOK 已完整拥有（内置 cmd_wdsj + wdsj/wdsj_query 工具 +
  wdsj_tracker/api/cache 三 services）→ 无需搬
- **power**（功耗/电费）：数据源是 QQ 侧常驻服务 127.0.0.1:58893，
  KOOK 未部署该服务 → 需先上采集服务，属独立任务
- **relay**（管理员代发）：语义是 QQ 的「g<群号>/u<QQ号>」，KOOK 是频道模型，
  需重新设计，不硬搬
- **phi/card_assets/**（215MB / 1300+ 曲绘 PNG）：传输成本过高未搬 →
  phi 卡片曲绘头像缺失，查询/存档/Best N 功能不受影响；需要时另行 rsync

### 验证
- 每个模块：py_compile → pyflakes 未定义名 → **线上真跑一次**
- **27/27 插件全部启用**；kook-bot.service 全程 active / NRestarts=0 /
  新进程 0 Traceback / 加载失败 0
- 全量 pyflakes：undefined name **4 个**（与改动前基线一致，均为
  `from __future__ import annotations`/字符串注解误报），**无新增**

---

## v2.3.91 — 功能追赶：把 QQ v2.1.6~v2.3.90 的演进补回来 — 2026.10.16

一句话总结：本端停在 v2.1.5，落后 QQ 两个大版本。本轮按「读 QQ commit + 更新日志」
逐项补齐，共落地 **6 个模块**，全部线上验证通过。

### 一、决策模型（QQ v2.3.88/89）—— 本端此前完全没有
- 新增 `services/decision.py`（497 行，原样移植，无平台依赖）
  - OpenRouter Decisions API / `inception/mercury-decide:free`，**免费**（usage.cost=0）
  - ⚠️ 该模型不能用 chat/completions，必须走 `/api/alpha/decisions`
  - `route()` 一次调用判 7 维（search/code/deep/tools/face/play/memory）+ which_tool
    选择题（选项从 `core.tools.get_tool_schemas()` **动态派生**，插件新工具自动纳入）
  - `judge()` 判 addressed/reply/interest/arch；`need_search()` 判要不要联网
  - **fail-open**：任何异常/超时/非 200 → 返回 None，绝不阻断主流程
- `services/llm.py`：`call_llm_with_tools` / `generate_multi_reply_with_tools` 加
  `force_tool` —— `tool_choice` 从 auto 变为指定函数，**仅第 0 轮强制**，之后恢复 auto；
  函数开头校验工具名在本次工具表内（`tools_override` 按需裁剪后可能不含），不在则丢弃并 WARNING
- `core/pipeline.py`：judge 之前**提前 fire** `route()`（与 judge/翻译/意图分类重叠，
  额外开销≈0）；search 维度命中 → `_fast_search=True`（补关键词漏判，即"该搜没搜"的窟窿）；
  which_tool 高置信度 → `force_tool`
- `bot.py`：启动日志打印「决策模型 | ...」；`config/bot_config.toml` 新增 `[decision]` 段；
  `.env` 新增 `OPENROUTER_KEY`/`DECISION_URL`/`DECISION_MODEL`
- 注：QQ 侧「tools 命中开思考」未移植 —— 本端 `call_llm_with_tools` 本就通过
  `_thinking_disabled_body` 关思考，无 thinking 机制
- 线上实测：恒大现状→search+search_web(0.9981)、帮我写个冒泡排序→code+write_code(0.9998)、
  晚安啦→None（fail-open 正常）；judge() → (7, True, True, True)

### 二、FC「先找后写」交付机制（QQ v2.3.81c/d/e）
KOOK 此前完全没有，"整理成文档发出来"类请求只回一句"我这就整理成md发出来"却永远不发文件。
- `_DELIVERABLE_FILE_RE` + `_needs_deliverable_file()` 识别要文件的请求
- 循环内累积检索素材 `_gathered`（去重），write_code 时把**原始素材由代码直接注入**
  `arguments.description` —— 素材 100% 进得去，不会被模型复述丢料
- 停条件例外：data_results 到手但还没调 write_code 且 round<3 → continue 再跑一轮，
  并注入定向指令（素材薄→换关键词多搜；素材足→立刻调 write_code，禁止只口头答应）
- 循环后交付兜底：要文件+有素材+没写过+模型仍不调 → 代码经 ToolRuntime 强制生成
- 注：`reasoning_content` 占位**不移植**（本端关思考，不产生该字段，不传不会 400）

### 三、最终轮「诚实归因 + 搜索空结果守卫」（QQ v2.0.4u + v2.3.48）
- 现象1：search_web 查完，LLM 却说"哦这个我知道喵"（装成本来就会）→ 注入 system 提醒
- 现象2：search_web 只返回 65 字符标题头（=没搜到）时模型照样编细节 → 薄结果守卫，
  要求诚实告知 + 严禁编造
- ⚠️ 关键：两条提醒必须 **append 到 msgs 末尾**，不能 insert(1)。DeepSeek 缓存按消息序列
  逐段匹配前缀，在位置 1 插入会让"位置1 及之后"全部对不上，命中率 98.4%→31.0%（QQ 实测）

### 四、i18n 提示语外化（QQ v2.3.90 体系）
- `modules/commands.py` **393 处**硬编码提示 → `format_lang(...)`，lang.toml
  **87 分组 / 514 条**（此前仅 63 处调用）
- 工具链与 QQ 共用：`scripts/scan_i18n.py` / `i18n_convert.py`（自带自验证：
  模板+kwargs 渲染 == 原文，0 失败才写回）/ `merge_lang_fragment.py`（幂等合并）
- ⚠️ 本端全仓 CRLF，转换器读写会把整文件转成 LF → 转换后显式转回 CRLF（diff 仅 367 增 513 删）
- 顺带补回对齐批 4 漏带的 2 条 lang.toml 条目（笔记本规则 / 回复长度自适应）
- 线上验证：449 个静态 key **缺失 0**，渲染抽查全对（无 [LANG:xxx] 泄漏）

### 五、用户画像（QQ v2.3.33/34/41）—— 本端此前完全没有
- 移植 `core/user_profile.py`（735 行）。v2.3.41 版式：**nick + text 两字段**，
  text 是一段自然语言（原来拆五个固定字段，LLM 为填满会硬凑、把玩梗当身份）
- 机制：每天 00:05 批量回看前一天 msglog，按 (会话,用户) 增量更新。单条消息没上下文，
  会把「我是你主人」当身份（实测 334 条里 20% 是疑问句、大量角色扮演）
- 隐私兜底：密码/token/手机号/身份证等一律不落盘；疑问句特征不当昵称
- **平台适配 2 处**：`_TEXT_TYPES` 加 "group"（本端用户消息 type 是 group，QQ 是 text，
  不加这条 collect_day 一条都收不到）；`get_bot_name()` → `get_config().bot_name`
- 接线：`plugins/bg_tasks` 新增 `_bg_profile_daily`；`core/pipeline` 注入
  `build_profile_text(chat_id, user_id, is_group)`（按会话分域）
- 已知降级：本端无 group_nicknames.json/group_settings → `group_ids()` 返回空，
  画像按用户分域（同用户在不同频道会合并）；QQ 是按群分域
- 线上实测：`collect_day('2026-10-15')` 收集到 2 个用户域（100 条 / 22 条），nick 解析正确

### 六、顺带核实"不需要搬"的三项（避免盲目移植）
- **tools 顺序漂移**：本端 `capability/router.py` 已按 `(-score, name)` 排序，
  顺序天然稳定 → QQ v2.3.28 的问题在此不存在
- **fmt_reminder 位置**：本端 `_build_messages` 的 user_parts 已是
  [上下文, fmt_reminder, 当前消息]，与 QQ v2.3.29 一致
- **会话分块/摘要压缩**：本端有自己的 `core/context_builder.py`（Phase 9 Context Engine，
  compress + budget），与 QQ v2.3.10 目标相同、实现不同，不搬
- **legacy 协议转换**：本端已有 `_calls_from_content`（等价 QQ 的
  `_legacy_calls_to_tool_calls`）
- **json_mode 兜底 / cost / tokens / cache**：本端已有（对齐批 4 带来）

### 未做（需评估，非遗漏）
- **manifest v1 → v2**：本端 `core/plugin/api.py` 与 QQ 是**双向分叉**
  （本端独有 `extract_cq_images`/`render_weather_card`，QQ 独有 24 个方法），
  不能单向覆盖 → 需合并两端差异，属插件系统核心改造，另立任务
- **/~mlink 跨聊天记忆关联**（QQ v2.3.11）：需先有 memory_link 插件，本端没有
- **功耗监测**（QQ v2.3.40）：QQ 侧已是独立插件 + 常驻服务，需整体评估
- **C 档卡片**（wdsj 卡片/排行榜/日报卡中文回退）：按用户定案"能做成 KOOK Card 的用
  KOOK Card"，需先定卡片方案

### 验证
- 每个模块：py_compile → pyflakes 未定义名检查 → **线上真跑一次**
- 全量 pyflakes：undefined name **4 个**（与改动前基线一致，均为
  `from __future__ import annotations` / 字符串注解的已知误报），**无新增**
- kook-bot.service 全程 active / NRestarts=0 / 新进程 0 Traceback

---

## v2.3.90 — 与 QQ bot 全面对齐（仓库↔线上零差异 + 三处修复）— 2026.10.16

一句话总结：本端**仓库与线上服务器此前深度分叉**（插件 26 vs 17、版本号三处硬编码互不一致、
服务器有未回流的 QQ 移植改动、本地有未上线的成果）。本轮把两侧**逐文件对齐到零差异**，
并把版本号统一为 **v2.3.90**（与 QQ bot 同号）。

### 对齐（仓库 ↔ 线上：内容不同 28 → 0）
- **服务器 → 仓库**（此前只存在于线上、从未回流 git）
  · Chromium 改「懒加载 + 空闲 10 分钟回收」（原预启动后永不释放，常驻 10 进程 / 270MB 挂 14 天）
  · 日榜数据缺失（停机致基线快照缺失）→ 补位公告卡
  · `services/notify_system.py` 新增 `remove_target_channel()`（`.notifydel` 用）
- **仓库 → 服务器**
  · 笔记本 **note** 整套落地（新模块 `core/bot_notes.py` + pipeline 注入 + judge「记忆指令→强制回复」+ commands 注册/帮助）
  · **token 场景归因**（`scene` 参数贯穿）+ `.cache` 缓存命中率
  · `pc_status_reporter` **v7.36**（SMTC 卡死判定 3s→10s、暂停不计、补 global 声明）
  · wdsj 同名去重（防同名绑定重复上榜）、架构段瘦身（省 token）、`log_server` 绑定地址可配置
  · `plugin_share` 去敏感化（代码内不留地址）→ 地址改由 systemd `Environment=PLUGIN_LIB_BASE` 注入
- **结构归一**
  · 移出 8 个「按类型分组」旧插件（admin/core/game/media/notify/search/social/tool_commands）+ 空壳 `checkin`
    → 与线上一致：**17 个一域一插件**
  · 命名统一：线上 4 个文件残留的 `bot_qq` → `bot_kook`

### 修复（此前在线上**活跃**的 bug）
- `services/music_status.py`：`set_music()` 的 `def` 行丢失，函数体成了不可达死代码
  → `/~listening` 与「正在听」状态设置**全部 ImportError**（已在生产日志实证闭环）
- `modules/commands.py`：`cmd_info` 调用未导入的 `get_cache_stats`（定义在 `modules/judge.py`）
  → `/~info` 必 NameError
- `core/memory_engine/pipeline.py`：`MEMORY_STATUS_ARCHIVED` 未导入 →
  记忆 consolidation 的归档分支被 `except` 吞掉、**静默失效**
- 静态基线：pyflakes `undefined name` **11 → 4**（余 4 为 `from __future__ import annotations` / 字符串注解的误报）

### 版本号（本次重点）
- 新增 **`core/version.py`** 作为**单一真相源**：`bot.py` 的 `Bot.VERSION`、
  `bg_tasks` 的 KOOK 动态状态（「正在听 当前版本: X.Y.Z」）、README 徽章统一读它
  —— 此前三处各自硬编码 `2.0.2`，改一处必漏两处
- `data/update_log.md`（本文件，LLM 自我认知回答的来源）纳入版本管理
- ⚠️ `config/update.toml`（`beta 0.8.x` 那套）经核实**运行时无人读取**，属历史遗留，本次未动

---

## v1.5.2 — Chromium 改懒加载 + 空闲自动回收 — 2026.9.28

一句话总结：Chromium 以前启动时预启动、之后**永不释放**，实测本机常驻 10 个进程 / 270MB，
从 2026-09-14 起挂了 14 天；改成"要用才启、空闲 10 分钟自动关"，已清理存量并释放内存。

### 问题（实测）
- `pgrep -fc chrom` = 12 个进程，其中 10 个的启动时间是 **Mon Sep 14**（挂了 14 天）
- 更坑的是 **snap 版 chromium 会跑到自己的 `snap.chromium.chromium-*.scope`**，
  **不在服务的 cgroup 内** → 光 `systemctl restart kook-bot` 杀不掉它们，只能一直攒
- 近 7 天实际截图次数极少，等于为偶尔一次渲染全天养着浏览器

### 改法（与 QQ bot v2.3.67 同一套方案）
- `modules/changelog.py`：
  · 新增 `_BROWSER_IDLE_SEC = 600`、`_browser_busy`（在飞渲染计数）、`_last_render_ts`
  · `touch_browser()` / `reclaim_browser(force)` / `browser_idle_loop()`（每 60s 检查）
  · `_screenshot_html` 拆成「外壳 + `_screenshot_html_inner`」：外壳维护 busy±1 与 touch，
    异常路径也保证复位（否则一次异常会让 busy 永久 >0，回收永不触发）
- `bot.py`：**去掉启动期 `await _ensure_browser()` 预启动**，改起 `browser_idle_loop()`。
  首次真要渲染时懒加载（~1s），空闲 10 分钟自动释放。

### 顺带修掉一个隐患
`modules/changelog.py` **没有模块级 `logger`**（原先只在各函数内部各自 `get_logger`）。
新增的回收函数是模块级的，直接用 `logger` 会 `NameError`，而且会连锁炸在 except 分支里
（异常处理自己也抛），真出问题时连日志都留不下。已补模块级 `logger`。

### 验证
- 服务器 `python3 -m py_compile` 通过
- 重启后启动日志确认：`渲染队列就绪（Chromium 懒加载 + 空闲 10 分钟自动回收）`
  / `Chromium 空闲回收已启动`
- 重启 + 清理后 chromium 进程数 **12 → 0**

---
## v1.5.1 — 搜索革命 + 人格解放 + 基建加固 — 2026.8.6

### 🔍 搜索全面重构

**DeepSeek Responses API 原生搜索（`web_search`）**
- 用 DeepSeek 服务端 `web_search` 替代自建百度+Bing+百科管道
- 纯 urllib 实现，零第三方依赖
- 时效性极强：搜到当天的 DeepSeek 涨价公告、中联毒油 8000 吨事件
- 智能 inference：17 次尝试读一个 SPA 页面（直读→搜索→API→CDN→jsDelivr...）
- 复杂问题自动打开多个页面深度阅读，按时间线分段输出

**三层回退机制**
1. DeepSeek 原生搜索（45s 超时）→
2. Agent 搜索（百度+Bing+深度抓取）→
3. 本地搜索器（百科+百度+Bing）
- 任一失败自动降级，保证搜索永远不挂

**搜索输出优化**
- `max_output_tokens` 不限，复杂问题想写多长写多长
- 搜索总结脱钩猫娘人设，用独立事实型 system prompt（不缩句）
- 禁止 Markdown 格式（纯文本适配 QQ）
- 搜索提示改为 `🔍 web_search ['关键词']`，精确显示搜了什么
- 搜索提示追到初始回复之后，不再 async 抢跑导致顺序错乱

**写作检测改为 LLM 判断**
- 正则关键词白名单 → LLM 带最近 6 句上下文判断 writing/code/chat
- "写个2048" 不再被写作管道截胡生成 6315 字作文
- LLM 判断失败时回退关键词匹配

### 🗣️ 人格系统重大调整

**"主人"去僵化**
- 群聊称呼：admin/op 可以叫主人但不强制、不每句喊
- `fav_tiers` 标题栏去强制命名
- 权限拒绝去模板化：不再 "需要主人来操作才行喵~"，改为 "需要管理权限喵~"
- `play_mode` 全文件 "主人" → "[admin]"

**Bot 自主权**
- 新增规则 17："你有自己的主意"
- 群友邀请去做某事（温泉/游戏/吃饭）→ 自己决定去不去，不搬主人当挡箭牌
- 私聊保持亲密：白名单只有 Trusler，对面始终是主人

**搜索陪伴优化**
- CALL 铺垫禁止提历史翻车（"上次搜到驱动确实有点离谱"）
- 搜后总结禁止吐槽搜索质量，直接说结果

### 🧠 回复质量

**不限 token 输出**
- 主线回复 `max_tokens=None`（之前 3000），复杂问题由提示词控制长度
- 提示词：复杂问题 "放开了写，不限字数，讲深讲透"
- 搜索总结独立 prompt，不受猫娘 '每句≤40字' 压缩

**好感度系统**
- fav 幅度自由化：小事 ±1~5，明显 ±10~20，极端 ±30
- 代码已有 [-100, 100] 硬夹
- 回复里绝对禁止提好感度数字（后台数据不暴露）

**写作检测**
- `is_writing_request` 改为 LLM 判断（含最近 6 句上下文）
- 分类：writing / code / chat
- LLM 失败回退关键词匹配

### 💬 上下文持久化

**瞬时记忆磁盘化（`data/context_cache.json`）**
- 每 3 条写一次 + 退出时全量保存
- 启动时自动恢复，重启不丢近期对话
- 只保留最近 30 条，控制文件大小

### 🛡️ 风控与防护

**刷屏检测全部移除**
- `pipeline.py` 刷屏检测块
- `pipeline.py` 骚扰检测（连续无意义@ → 5min 忽略）
- 保留 `modules/spam_guard.py` 文件但不再调用

**全群忽略系统（`/~ignore` `/~unignore`）**
- `modules/ignore_users.py`：数据存 `data/ignored_users.json`
- 被忽略用户在所有群都被静默跳过
- admin 豁免
- `/~unignore all` 一键全部解除

**好感度 -100 自动忽略**
- 好感度 ≤ -100 的用户发消息直接 return
- 不记上下文、不走管道、不回复
- 日志打印 warning

### 🎨 UI 与展示

**五子棋模板修复**
- `wzq_board.html` 8 个 `${}` 占位符补全
- 棋盘现在正确显示：玩家名、状态、列标签、手数、日期

**CALL 顺序修正**
- `write_code` CALL 延迟到文字全部发出后再执行
- 顺序：回复 → 文件 → 追加文字

### 🔧 安全

**GitHub 仓库域名隐藏**
- `main_skill.md` 内 `01240820.xyz` 改为 `${host}` 模板变量
- 运行时由 `_build_self_awareness` 动态填入
- GitHub 公开版只有变量名

**用户画像防幻觉**
- `facts` 字段过滤：拒绝 "我查战绩"、"/~xxx" 等指令/聊天内容
- `name` 字段过滤强化

### 🐛 修复

- `web_search.py` 缺少 `import asyncio` 导致原生搜索报错
- `fav` 字段从 -5~+5 扩到自由范围时漏改 `fav_format` 节
- 上下文持久化加载路径遗漏

---

## v1.5.0 — Function Calling + 代码生成 + 用户画像 + 语音合成 + 视觉重构 — 2026.8.3

### 🤖 Function Calling 集成（DeepSeek FC）
- 8 个工具自动调用：天气、战绩、排名、搜索、地震、五子棋、PC 状态、代码生成
- 多轮 FC Agent 循环（最多 2 轮），同轮多工具并行执行 (`asyncio.gather`)
- 工具结果分类：错误回复 / 数据自然回复 / 动作直接返回
- 首轮非 JSON 输出 → `json_mode=True` 强制重试（补救重试 max_tokens 上限 800）
- 纯文本回复静默降级，不刷 WARNING
- FC 工具定义统一在 `core/tools.py` 的 `TOOLS` 表 + `_TOOL_CMD_MAP`
- `call_llm_with_tools` 独立封装（不依赖 `call_llm`，直接使用 OpenAI SDK）

### 💻 代码生成管道（FC write_code）
- LLM 自动检测编程语言 → 生成完整解法代码
- 单文件直接发送 QQ 文件，多文件打包 zip
- C++ 代码自动 g++ 编译 + 带超时执行（防止死循环）
- 编译/运行结果跟随文件一起回复
- **长题防御**：>2000 字代码题直接认怂"看不懂喵"，不烧 token
- 去掉了规格优化中间步骤和"正在生成代码喵"废话消息，原题直喂

### 👤 用户画像系统（`core/user_profile.py`）
- JSON 文件存储 `data/user_profiles.json`，按用户 QQ 号索引
- 每次发言后台异步提取：关键词快速提取（零成本，≥20字才调 LLM）
- 画像字段：tags(标签) / interests(兴趣) / dislikes(雷点) / tone(语气偏好) / events(重要事件) / status(当前状态)
- 画像注入 `extra_info`，LLM 根据画像个性化回复
- 回溯脚本 `scripts/backfill_profiles.py`：从 msglog 批量建画像
- 竞态安全：提取完成前不阻塞、不丢失

### 🎙️ 语音合成（`/~voice`）⚠️ 未完成
- Edge TTS 语音合成，默认女声晓晓（zh-CN-XiaoxiaoNeural）
- SSML 情绪分句控制：根据 `mood_detail` 动态插入 `<mstts:express-as>` 标签
- 物理参数调节：pitch+28Hz / rate+35% 童声尖音效果
- 生成 WAV 文件 → 通过 QQ 语音消息发送

### 🖥️ PC 状态监控（`services/pc_status.py`）
- 服务器端口 58890 接收 HTTP POST
- 本地 `scripts/pc_status_reporter.py` 定期采集：窗口标题 + 音乐播放器 + 歌词
- FC 工具 `system_status` + 指令 `/~sys` / `/sys` / `/~pc`
- 缓存有效期 30s，超时返回"未开机"

### ✨ Crystal Aurora v2 视觉重构（7 个模板）
- 背景：4 层径向渐变极光（粉/紫/蓝/青）+ conic-gradient 水晶折射 + 噪点纹理
- 卡片：blur(24px) saturate(1.4) 玻璃效果 + mask 渐变边框 + 顶部高光条 + 多层阴影
- 标题：三色渐变 + drop-shadow 发光
- 代码块：Mac 风格（红黄绿圆点 + 语言标签 + 渐变标题栏）+ One Dark token 配色
- 列表：发光圆点标记；表格：渐变表头 + 斑马纹；引用块：渐变 border + 引号装饰
- 模板：`changelog_card / md_card / daily_report / weather_card / box_card / leaderboard_card / wzq_board`

### 🩺 根因修复：LLM 空返回 & 不按 JSON 格式输出（最影响体验）

这组问题直接导致用户看到"回复生成失败"或回复牛头不对马嘴。根因不在提示词，而在代码层。

#### 根因 1：`_build_messages` 消息角色判断错误（`llm.py#L816`）
- **问题**：FC 管道的多轮上下文用 `i % 2` 奇偶判断 role（user/assistant 交替）
- **后果**：多轮对话中 bot 回复被标成 user、用户消息被标成 assistant，LLM 收到错乱的对话历史 → 输出格式不跟着走
- **修复**：改为 `bot_name:` 前缀判断，以消息内容本身确定发言人角色
- **影响范围**：所有走 FC 管道的群聊回复，之前的"LLM 不按 JSON 输出"很大概率是这里引起的

#### 根因 2：FC 管道 JSON 输出双重失败（`llm.py#L612` + `call_llm_with_tools`）
- **问题**：`call_llm_with_tools` 开启了 `response_format={"type":"json_object"}` → v4-flash **不支持与 tools 同时使用** → LLM 返回空 content + 空 tool_calls
- **后果**：管线检测到空 content → 重试（仍然空）→ 最终报"LLM返回空内容"给用户
- **修复**：撤回 `response_format`，改为在 `_build_messages` 的格式提醒最前面加 `★★★ 最重要规则：你的全部回复必须是 JSON 格式，绝不允许输出纯文本 ★★★`（提示词级硬约束）
- **附带优化**：JSON 补救重试的 `max_tokens` 从 3000 降到 800（v4-flash 3000 tokens 会导致 `finish_reason=length` 再触发一轮重试）

#### 根因 3：`_judge_combined` 判断模块 max_tokens 浪费（`llm.py#L1232`）
- **问题**：判断用户消息是否需要回复的 LLM 调用设了 `max_tokens=500`，实际只需要 `true/false` 两个字符
- **后果**：500 tokens = 一次判断浪费 ~400 token 配额，累积到并发时加剧 flash 的 token 压力
- **修复**：砍到 20

#### 其余 8 处修复
| # | 文件 | 修复内容 |
|---|------|----------|
| 1 | `sender.py#L197` | API 调用 `request` 参数 → `json.dumps(payload)`（payload 格式错误导致发送失败） |
| 2 | `commands.py#L3013` | `/#` 指令检查缺少 `cfg = get_config()` → NameError 崩溃 |
| 3 | `commands.py#L2899` | 加 `/s` 别名（lang.toml 引导用户用 `/~s` 但代码里找不到 → 回复"未知指令"） |
| 4 | `tools.py#L348` | `_agent_think` 残留 11 行无用代码（在其他 agent 工具清代码时遗漏） |
| 5 | `dispatcher.py#L31` | `_seen_ids` set → dict 按插入顺序截断（set 无顺序，清旧 ID 时误清新 ID → 消息被跳过） |
| 6 | `spam_guard.py#L126` | timestamps 和 messages 数组同步过滤（一个清了另一个没清 → 数组长度不一致） |
| 7 | `pipeline.py#L419` | 删除 60 行工具代理死代码（已被 FC 系统替代，但残留代码仍在拦截消息流） |
| 8 | `pipeline.py` + `tools.py` | `write_code` 引用了不存在的 `code_model` 变量 → NameError 崩溃 |
| — | `pipeline.py` | >2000 字代码题直接认怂，不调 LLM 避免 120s 超时浪费 |

### 🐛 其他修复
- **LLM 失败回退**：三个 LLM 调用返回空时补齐 `mood_detail` 字段，防止元组解包崩溃
- **v4-flash 内存压缩超时**：15s 超时导致压缩结果为空
- **changelog 卡片渲染器用错**：`changelog.py` 有两个 MD 渲染器（标准库版 `markdown_to_enhanced_html` + 简易正则版 `_md_to_html`），`generate_changelog_image` 错误调了简易版 → 模板 CSS 是为标准库版设计的 HTML 结构写的（表头高亮/斑马纹/代码语言标签/嵌套列表缩进/斜体粗体/多行引用块），简易版产不出对应结构 → 大量样式闲置失效。修复：优先调标准库版，失败回退简易版

### 📦 仓库整理 & 配置完善
- 移除 18 个泄露的私有文件（模板/模块/脚本）
- 清理 `lang.toml` 私有模块帮助文本（weather/box/wdsj/gh/tuf/xq）
- 添加 `example.bot_config.toml`、`example.roles.toml`、`example.adapter_config.toml`、`example.env`
- 配置模板重写：中文 key、provider 映射 env 环境变量
- `PLUGIN_DEV.md` 插件开发指南
- `test_fc_sim.py` FC 模拟测试

### 🔄 自动更新优化
- 自动处理 force-push 导致旧 SHA 404：降级全量下载 + 重试
- `/~upd` 短别名 + `/~upd test` 公开连通性测试
- `/~upd` 输出详细更新日志含 commit 信息

### 🎯 其他改进
- `/~op group del` 清群时同步清理全局 `op_qqs` 花名册
- `/~owner wdsj groups` 管理推送群 + `/~wdsj daily send` 强制推送
- `mood_detail` 管道集成（每句话对应情绪）
- `log_server` 支持分离式静态文件 (console.css/js/guard.js) 路由
- `log_server` `_build_html` 精简，删除 190 行旧版内联模板

### 🔮 文件变更
| 文件 | 变更 |
|------|------|
| `core/tools.py` | **重写**：FC 工具系统 (TOOLS + _TOOL_CMD_MAP + execute_tool + _write_code)，+872/-408 |
| `services/llm.py` | **大修**：`call_llm_with_tools` + FC 多轮 Agent + json_mode 兜底 + 提示词增强，+362 行 |
| `core/user_profile.py` | **新建**：用户画像系统，406 行 |
| `modules/voice.py` | **新建**：Edge TTS 语音合成，105 行 |
| `services/pc_status.py` | **新建**：PC 状态监控 HTTP 服务，77 行 |
| `scripts/pc_status_reporter.py` | **新建**：本地 PC 状态采集上报，97 行 |
| `scripts/backfill_profiles.py` | **新建**：msglog 回溯建画像，73 行 |
| `scripts/fix_config_toml.py` | **新建**：配置文件迁移脚本，66 行 |
| `test_fc_sim.py` | **新建**：FC 模拟测试，165 行 |
| `docs/PLUGIN_DEV.md` | **新建**：插件开发指南，252 行 |
| `data/user_profiles.json` | **新建**：用户画像数据 |
| `config/example.*.toml` | **新建**：example 配置模板 |
| `core/pipeline.py` | FC 集成 / write_code 认怂 / 工具代理死代码清理 / 用户画像注入 |
| `core/dispatcher.py` | `_seen_ids` 优化 / PC 状态路由 |
| `core/config.py` | 配置模板兼容 / PC 状态配置 |
| `modules/commands.py` | FC 指令 + voice/sys/upd 指令 + 批量修复 |
| `modules/auto_update.py` | force-push 容错 + 短别名 |
| `modules/memory.py` | 画像注入适配 |
| `modules/op.py` | group del 同步清理 |
| `modules/stats.py` | WDSJ 推送群管理 |
| `data/templates/*.html` | Crystal Aurora v2 7 个模板重构 |
| `data/update_log.md` | 本文档 |


## v1.4.2 — 自然对话重构 + OP权限分层 + 模式系统 --2026.7.15

### 🗣️ 自然对话重构
- **禁止回溯旧上下文**：只回应当前消息，不翻10分钟前的截图/聊天记录当素材
- **禁止元对话**：不说"被点名了""翻了一下记录""看到你们在聊XX"这类播报员式总结
- **水群语气**：像QQ群闲聊的女生，可偶尔用"草""笑死""确实""好家伙"等口语
- **内向OS**：可偶尔在回复中夹杂括号内心独白，如"(其实我也有点困了)"，比嘴上诚实
- **CALL 指令全面收紧**：只在用户明确说"帮我搜/查/找/介绍一下"时才调用工具。`tools.py` 的 `TOOL_DEFINITIONS` 移除"系统自动检测调用"矛盾指令

### 🔫 OP 权限分层（admin > op > user）
- `/~op add/del <QQ>` — 添加/移除 OP 次级管理员
- `/~op group set <群号> <OP_QQ>` — 群主权限指派给 OP（OP 在该群内等于 admin）
- `/~op group del <群号>` — 撤销指派，恢复 admin
- `/~op group list` — 查看所有 OP 和群指派
- OP 在指派群内自动获得 `[admin]` 角色标签，可用全部 CALL 指令

### 👤 私聊增强
- `/~persona <人格描述>` — 私聊切换 AI 人格，完全替换 system prompt
- `/~persona reset/show` — 恢复默认/查看当前人格
- `/~主人` — 将当前私聊对象设为主人，获得 admin 级权限

### 😴 模式系统
- `/~sleep` — 睡觉模式：全部回复用第三人称括号叙述，如"(迷迷糊糊翻了个身)"，10字内迷糊呓语
- `/~含蓄` / `/~叙事` — 含蓄叙述模式：日常可用，第三人称括号内心独白 ，如"(默默记下了这件事)"
- 三种模式互斥（普通/含蓄/睡觉），无论哪种都作用于群聊+私聊+戳一戳

### 🐛 关键修复
- **NapCat 缺 post_type**：`dispatcher` + `message_parser` 兼容缺少 `post_type` 字段的消息（有 `message_type` 就当作 message 事件）
- **cmd_whitelist=None 崩溃**：`grp_cmds` 为 None 时 `in` 操作抛 TypeError，改为 `if not grp_cmds: pass`
- **dispatch 异常捕获**：`_dispatch_inner` try/except + 完整 traceback 日志
- **scp 丢行连锁崩溃**：`recall.py`/`commands.py`/`judge.py` 三处语法错误导致全体消息丢失 ~100 分钟，修复并加强部署验证
- **分群昵称**：`get_display_name(uid, gid)` 优先读分群 `nicknames`，fallback 全局 `qq_name_map`
- **@优先回复**：当 @机器人 + CALL 指令时，丢弃 CALL 前的闲聊句子，直接回复指令结果

### 🌐 开源版同步
- GitHub `huanmeng-qqbot` 同步 v1.4.1 全部 42 文件（+4146/-742 行）
- README 版本 v1.1→v1.4.1，LICENSE 年份 2024→2026
- `wdsj_cache.py` 从开源版移除，`commands.py` 加 ImportError fallback
- 服务器 SSH key 配置 GitHub push（443 被墙，走 22 端口）

### 🔧 戳一戳
- 禁止重复"摸头很舒服"句式，每次随机情绪（疑惑/开心/害羞/吓一跳/嫌弃/淡定）
- 睡觉/含蓄模式下戳一戳也切换为括号第三人称叙述

### 🔮 文件变更
| 文件 | 变更 |
|------|------|
| `data/main_skill.md` | 规则 10~14：禁止回溯/元对话/水群语气/内向OS；规则 0 扩展：CALL 全局收紧 |
| `modules/op.py` | **新建**：OP 权限系统 + 私聊人格/主人 + 睡觉/含蓄模式 |
| `core/config.py` | `op_qqs` + `group_owners` 字段；`get_user_tag(gid)` 支持分群 OP；`get_display_name(uid,gid)` 分群昵称 |
| `modules/commands.py` | 注册 `op/persona/主人/sleep/含蓄` 指令；wdsj_cache fallback |
| `core/pipeline.py` | @优先丢弃闲聊 / 分群昵称 / 模式注入 / 私聊人格覆盖 / 私聊主人 / 戳一戳多样化 |
| `core/dispatcher.py` | post_type 缺失兼容 / cmd_whitelist None 防御 / try/except + traceback |
| `utils/message_parser.py` | `_is_message_event` 兼容无 post_type（有 message_type 即当消息） |
| `core/tools.py` | `TOOL_DEFINITIONS` 移除"系统自动检测调用"→ 明确仅在用户要求时执行 |
| `data/templates/*` | 1.4.1 同步至开源版 |
| `bot.py` / `main.py` | VERSION 1.4.1 → 1.4.2 |



### 🗣️ 回复质量控制
- **句数压缩**：群聊 1~5 → 1~3 句；短消息（喵/嗯/好）→ 1 句就够了
- **戳一戳精简**：token 400→150 + 注入"只用 1 句短回应"
- **禁止反复晚安**：说了晚安就别再提睡觉，翻来覆去显得假
- **禁止假设作息**：不说"明天上课/上班"，不知道对方放没放假
- **上下文不合并**：多人发言不再挤在一条 user 消息里，每条独立让 LLM 看清
- **私聊角色**：`[admin]` 统一叫主人，`[friend]` 只用你我，禁止精分切换
- **群聊 @ 功能**：注入可 @ 用户列表，LLM 可用 `[CQ:at,qq=QQ号]` @ 人

### ✍️ 全量消息录制 & 撤回
- `send_sentences` 用 `call_api` 获取真实 message_id → 撤回 bot 消息能匹配原文
- `send_group_msg` / `send_private_msg` 底层自动录 bot 消息到 msglog
- msglog 统一 JSONL，`type="bot"` 一键过滤所有 bot 回复，用于排查质量
- `mark_recalled` 跳过 msg_id=0 的 bot 消息，防止多条 bot 消息互相误中撤回

### 🧠 msglog 记忆回溯
- `search_msglog()` 从最近 300 条消息中关键词匹配，注入 extra_info
- 触发条件：消息 ≥6 字（短词不触发），用户消息优先，bot 消息限 2 条
- 效果：LLM 能看到上下文窗口之外的聊天记录，不会重复回答或遗漏上下文

### 🐛 关键修复
- **CALL 系统**：正则 `~` 可选 / 清洗 HTML 碎片(`admin">`) / COMMAND_MAP 校验 / wdsj 参数校验 / 错误结果标记 `[CALL错误]` / follow-up LLM 诚实报错
- **LLM 伪装 CALL**：剥离写入上下文的 `[系统] 已调用:` 后缀，防止 LLM 从历史学会伪造
- **好感度缺失**：格式提醒追加 `[fav: ...]` 要求，防止 LLM 忘记加好感度
- **fav 越界**：`data_set` 对 fav 类型 clamp 0~100；服务器回正 Trusler 的 114514
- **fav key**：`data get/set/del` 对 fav 自动纠正纯数字 key → `g{chat}:{user}`
- **更新日志模板**：删除重复 `id="changelog-body"` + `<li>` 包裹 `<ul>` + 行内粗体代码转换
- **日报升级**：HTML 卡片替代纯文本，排行条+24h 热力图+幻梦锐评

### 🌐 服务器
- **NTP 自动校准**：`crontab` 每天 23:58 `ntpdate -s ntp.aliyun.com`

### 🔮 文件变更
| 文件 | 变更 |
|------|------|
| `core/pipeline.py` | CALL 大修 / 上下文标签剥离 / 句数+戳一戳 / 时间注入 / msglog回溯 / @列表 |
| `services/llm.py` | 格式提醒 `[fav]` / 上下文不合并 |
| `services/sender.py` | `_send_and_record` + `_log_bot_sent` 全量录制 |
| `modules/memory.py` | **新增** `search_msglog` msglog 回溯检索 |
| `modules/recall.py` | `mark_recalled` 跳过 msg_id=0 |
| `modules/admin.py` | fav `data_set` clamp 0~100 |
| `modules/commands.py` | fav key 自动纠正 / wzq duel 修复 / unduel+admin clear |
| `modules/stats.py` | `generate_daily_report_image` HTML 日报 / `record_message` +sender_name |
| `modules/changelog.py` | `_md_to_html` `<ul>`包裹 + `_inline_fmt` 行内格式 |
| `data/main_skill.md` | 句数 5→3 / 短消息规则 / 禁晚安 / 禁假设作息 / 私聊角色 / @规则 / wdsj文档 |
| `data/templates/daily_report.html` | **新建**：日报 HTML 卡片 |
| `data/templates/changelog_card.html` | 修复重复 `changelog-body` |
| `data/update_log.md` | v1.4.1 条目 |
| `core/dispatcher.py` | `record_message` +sender_name |
| `bot.py` / `main.py` | VERSION 1.4.0 → 1.4.1 |
| 服务器 crontab | 23:58 NTP 自动校准 |


## v1.4.0 — 五子棋 AI + 引用图片 + 质控大修 --2026.7.13

### 🎮 五子棋 AI 重写
- 从 LLM 猜坐标 → **Minimax + Alpha-Beta 剪枝**评分搜索
- 模式评估：全盘四方向扫描，活四 50000 / 冲四 1000 / 活三 1000
- 候选剪枝：只搜已有棋子半径 2 格，225 格砍到 20-40 格
- 走法排序：高分先搜，α-β 大量剪枝
- 四档难度：新手(depth 1) / 普通(2) / 困难(2) / 专家(迭代 1→2→3 + 8s 超时)
- 专用线程池 `_AI_EXECUTOR`，不阻塞 Playwright 渲染
- 立即防守：对手活四/五连威胁直接堵

### 📎 引用图片自动响应
- @机器人 + 引用消息中的图片 → 自动下载识别，描述注入管道
- WDSJ 战绩图智能分流：bot 自己发的战绩图被引用时，直接返回缓存数据（不耗视觉 token）
- `wdsj_cache.py`：snapshot → 数据映射，1 小时过期

### 🐛 关键修复
- **@检测严格化**：正则 `@<bot_qq>(?!\d)`，bot 自身 @ 保持 QQ 号不替换
- **更新日志卡片**：去 CDN 依赖（fonts/marked.js/highlight.js），Python `_md_to_html()` 服务端渲染
- **图片识别**：PIL 预处理 → 转 RGB + 长边缩至 2048 + 输出 PNG，避免智谱 1210 格式错误
- **日志控制台**：`addLine()` 改为单 `<span class="msg">`，解决实时日志复制换行
- **预搜索 CALL**：搜索结果注入加 `【系统预搜索】` 标记 + 代码级拦截重复 `[CALL:~search]`
- **||| 多句分隔**：格式提醒钉在 user message 末尾，防止长 extra_info 冲掉规则
- **不可见字符清洗**：管道入口正则洗零宽/双向控制/不可见运算符，纯垃圾跳过
- **wzq duel 修复**：缺 return 掉到末尾变"未知操作"；undo 申请人覆盖；AI 图片顺序颠倒
- **wzq 增强**：/~wzq unduel 撤销挑战、/~wzq admin clear 强制结束本群棋局，持久化 `wzq_games.json`
- **CALL 系统大修**：正则 `~` 可选（防 LLM 漏写前缀静默丢失）；程序级校验指令存在性 + wdsj 参数数量；清洗 CALL 文本中的 HTML 碎片(`admin">`)和换行；命令返回错误自动捕获 → 标注 `[CALL错误]` → follow-up LLM 诚实告知失败
- **wdsj 文档修正**：main_skill.md `[CALL:~wdsj <玩家名>]` → `[CALL:~wdsj bw 玩家名]`，避免 LLM 只传一个参数
- **LLM 伪装 CALL**：剥离写入上下文的 `[系统] 已调用:` 后缀，防止 LLM 从历史学会伪造（输出假 CALL 而不实际执行）
- **好感度缺失**：user message 格式提醒追加 `[fav: ...]` 要求，防止 LLM 被大量规则淹没忘加好感度

### 🔧 功能调整
- **搜索改为手动**：关闭 `auto_search_if_needed`，只响应用户 `/~search` 和 LLM `[CALL:~search]`
- **搜索进度提示**：`perform_search` 内只在实际搜索时发 "🔍 正在搜喵~"（缓存命中不提示）
- **统计开关**：`/~unstats` 暂停 / `/~setstats` 恢复，数据文件不动
- **退群指令**：`/~leave [群号]` 收集 GH/统计/好感度 → 重置 fav → 退群，文件全保留
- **戳一戳白名单**：`/~owner wl remove` 后该群戳一戳同步屏蔽

### 🔮 文件变更
| 文件 | 变更 |
|------|------|
| `modules/wzq.py` | AI 重写：Minimax + α-β + 评估 + 走法排序 + 专用线程池 |
| `core/dispatcher.py` | @ 严格检测 / 引用图片识别 / WDSJ 缓存分流 / 戳一戳白名单 |
| `modules/changelog.py` | `_md_to_html()` 服务端渲染 / 模板去 CDN |
| `services/image_api.py` | PIL 预处理：格式统一 → PNG |
| `core/pipeline.py` | @ 严格检测 / 关自动搜索 / 不可见字符清洗 / 预搜索标记 |
| `core/log_server.py` | 日志格式统一单 span |
| `services/llm.py` | ||| 格式提醒钉到 user message |
| `services/sender.py` | echo 匹配修复 |
| `utils/username.py` | bot @ 保持 QQ 号不替换 |
| `modules/search.py` | 搜索进度提示移到 perform_search 内部 |
| `modules/stats.py` | 统计开关 `_stats_disabled` |
| `modules/commands.py` | /~unstats /~setstats /~leave / cmd_wdsj 缓存 |
| `modules/leave.py` | **新建**：退群数据收集/清理 |
| `services/wdsj_cache.py` | **新建**：WDSJ 战绩图片缓存 |



## v1.3.0 — LLM 指令调用 + 表情库 + 地震大修 + 全指令文档 --2026.7.6

### 💡 LLM 可调用指令系统 [CALL:~xxx]
- 大模型可在回复中嵌入 `[CALL:~指令 参数]` 自动调用机器人功能
- 20+ 指令可被 LLM 调用：天气、地震、搜索、画图、管理配置等
- 管道自动检测 CALL → 执行 → 过滤标签 → 在主消息末尾附加 `[系统] 已调用: /~指令`
- 私聊/群聊均可使用
- 调用结果通过 follow-up LLM 自然回应

### 😊 表情库 [FACE:关键词]
- 新增 `modules/face_lib.py`：关键词匹配本地 emoji 图片
- 支持 15 种情绪表情（开心/害羞/坏笑/疑惑/大哭等）
- LLM 在回复末尾用 `[FACE:关键词]` 匹配并发送
- 表情文件位于 `data/faces/`

### 🗺️ 地震系统全面重构
- 数据源升级：CENC + 四川/福建/重庆 省级预警，5 源并行 1s 轮询
- MD5 响应缓存 + 60s 防重推冷却
- 省映射表支持 31 省级地区多省订阅
- 地图三代进化：OSM(不可达) → SVG雷达图(兜底) → 天地图Leaflet(最终)
- 天地图 WMTS 瓦片 (vec_w + cva_w)，真实地理地名
- 缩放策略：M≥7 z5 / M≥5 z6 / 其他 z7（覆盖省级范围）
- HTML 地震卡片 → Playwright 截图 → CQ 图片发送
- EQ 监视器 WebSocket 端口 58889

### 🗣️ 私聊语气优化
- 软禁"主人"称呼（不禁止但限制频率）
- 拟声词开头 ≤10%
- 每句 12-20 字，2-6 句结构
- 文字表情 QAQ/OvO/QwQ 替代颜文字

### 📋 全指令参考文档
- `data/幻梦Bot_完整指令文档.md`：50+ 指令完整用法说明
- `/~help` 改为发送预渲染指令文档卡片（即时响应，不等待渲染）
- 文档以 `## command_reference` 注入到 LLM system prompt

### 📄 废弃指令
- `/~s`（搜索）：已从 COMMAND_MAP 移除
- `/~img` / `/~img18`（随机二次元图）：已从 COMMAND_MAP 移除
- 旧 `tools.py` 中的天气工具：已移除，由 CALL 系统接管

### 🔧 其他改进
- 判决合并：cheap judge + interest judge 合并为单次 API 调用
- 消息去重：`dispatcher.py` 新增 `message_id` 去重
- 自动搜索聚焦明确关键词，不再拦截"再查一次"
- 旧 tools.py 清理：移除 weather 调用防重复查询
- Bot 服务修复：robot.service 废弃，只保留 bot.service 单实例
- service 配置修正：ExecStart 改为 `main.py`，WorkingDirectory 改为 `/root/bot`

### 🔮 文件变更
| 文件 | 变更 |
|------|------|
| `modules/earthquake.py` | 重写：多源、天地图卡片、省订阅、EQ监视器 |
| `modules/face_lib.py` | **新建**：表情库关键词匹配 |
| `data/main_skill.md` | 新增 command_tools、face_lib、command_reference、private_format/tone |
| `data/幻梦Bot_完整指令文档.md` | **新建**：完整指令参考 |
| `data/help_card.png` | **新建**：预渲染帮助卡片 |
| `services/llm.py` | 动态注入 + `_build_dynamic_command_list()` |
| `core/pipeline.py` | CALL/FACE 检测 + tools.py 清理 |
| `core/dispatcher.py` | message_id 去重 |
| `core/tools.py` | 移除 weather 工具 |
| `modules/commands.py` | 废弃 s/img/img18，/~help 改卡片模式 |

### 💬 私聊专用提示词

群聊和私聊现在使用**独立的格式规则**：

| 项目 | 群聊 | 私聊 |
|------|------|------|
| 最大句数 | 5 句 | 8 句 |
| 每句上限 | 40 字 | 30 字 |
| 表情风格 | 颜文字 (。>∀<。)ﾉ | 文字表情 (QAQ / OuO / QwQ) |
| 称呼规则 | 正常角色标签 [admin]/[friend] | 禁止主人/朋友，只用你我他 |
| 对话风格 | 多人聊天，偶尔插话 | 二人独处，更亲切简短 |

### 📁 main_skill.md 统一管理

所有 LLM 提示词从代码中移出，集中到 `data/main_skill.md`：

```
data/main_skill.md
├── ## prompt_header     # 角色身份：你是{bot_name}...
├── ## group_format      # 群聊格式规则（5句+颜文字）
├── ## private_format    # 私聊格式规则（8句+文字表情+你我他）
├── ## fav_format        # 好感度反馈标记
├── ## fav_tiers         # 好感度档位表
├── ## anti_repeat       # 防重复规则
├── ## play_mode         # 扮演模式规则
├── ## self_awareness    # 自我认知模板（版本/架构/配置）
```

### 🔧 代码改动

**`services/llm.py`**:
- 移除 60 行硬编码 `MULTI_REPLY_SYSTEM_STATIC`
- 新增 `_load_skill_sections()` — 按 `##` 解析 main_skill.md
- 新增 `_build_system_text(bot_name, personality, is_group)` — 群聊/私聊选择不同章节
- `generate_multi_reply()` 新增 `is_group` 参数

**`core/pipeline.py`**:
- 两处 `generate_multi_reply` 调用传入 `is_group`
- 移除"多行分批发送"残留代码

**`core/config.py`**:
- `_build_self_awareness()` 改为从 main_skill.md 读模板
- 模型信息更新为 DeepSeek + Zhipu

### 📦 新增文件

- `data/main_skill.md` — 所有提示词集中管理

---

## 2026-07-06 — 日志控制台格式修复

### 🐛 修复
**`core/log_server.py`**:
- `addLine()` 实时日志渲染从 flex 多列改为单 `<span class="msg">` 结构
- 与 `loadHistory()` 历史日志格式一致：`[时间] [级别] (来源) 消息`
- 解决实时推送日志复制时换行的问题

---

## 2026-07-11 — 预搜索重复 CALL 修复

### 🐛 修复
**`core/pipeline.py`**:
- 搜索结果注入时加前缀 `【系统预搜索】请直接基于这些内容回答，不要再调用 [CALL:~search] 重复搜索`
- CALL 处理中新增检查：管道已自动搜索过时，跳过 LLM 生成的 `[CALL:~search]`，避免重复搜索和 follow-up LLM 胡诌
- 根因：LLM 拿到预搜索结果后仍按 system prompt 要求生成 `[CALL:~search]`，二次搜索 baidu 超时导致 follow-up LLM 胡编乱造

---

