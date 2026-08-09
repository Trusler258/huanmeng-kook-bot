# Windows 部署指南

本文档覆盖 Windows 10 1809+ 及 Windows 11 全量部署流程：

1. Bot 本体部署（Python 虚拟环境 + 配置初始化 + 前台/服务启动）
2. PC 状态 + 歌词同步上报脚本部署（SMTC 原生媒体控制）
3. 开机自启（任务计划程序 / NSSM 系统服务）

---

## 0. 前置条件

| 项目 | 最低要求 | 备注 |
|------|----------|------|
| 操作系统 | Windows 10 1809+ / Windows 11 | 低于 1809 无法使用 `ISystemMediaTransportControls` (SMTC)，歌词上报不可用，Bot 本体仍可运行 |
| Python | 3.10, 3.11, or 3.12 (64-bit) | 必须 64 位，pywin32 / winrt 不提供 32 位构建 |
| 网络 | 可访问 KOOK 网关及 LLM 提供商 API | 国内用户建议全程使用镜像源 |
| 权限 | 普通用户即可；开机自启功能需要管理员组权限 |

### 0.1 确认 Python 架构

打开 PowerShell 执行：

```powershell
python -c "import sys, struct; print(f'{sys.version} ({struct.calcsize(\"P\") * 8}-bit)')"
```

应输出类似 `Python 3.11.9 (tags/v3.11.9:de54cf5, Apr  2 2024, 10:12:36) [MSC v.1938 64 bit (AMD64)] on win32 (64-bit)`。如果显示 32-bit，请重新安装 64 位安装包，并在安装向导中勾选 "Add Python to PATH"。

---

## 1. 获取代码与创建虚拟环境

建议始终使用独立虚拟环境，避免污染 `site-packages` 并保证可复现：

```powershell
# 1. 克隆或解压项目
git clone <your-repo-url> kook-lyric-bot
cd kook-lyric-bot

# 2. 创建虚拟环境（首次运行执行一次即可）
python -m venv .venv

# 3. 激活虚拟环境 —— 每次新开 PowerShell 都要执行一次
.\.venv\Scripts\Activate.ps1
# 如果遇到执行策略报错：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteLocal -Force
#   然后重新执行上面的 Activate.ps1

# 4. 升级 pip 到最新版
python -m pip install --upgrade pip
```

---

## 2. 安装依赖

全程使用阿里云 PyPI 镜像加速，并在 Playwright 安装 Chromium 时指定腾讯云镜像：

```powershell
# A. 核心依赖（必装）
pip install -r requirements.txt `
  -i https://mirrors.aliyun.com/pypi/simple/ `
  --trusted-host mirrors.aliyun.com

# B. Windows SMTC 歌词上报依赖（如果你要在本机运行 pc_status_reporter.py）
pip install pywin32 psutil pynvml wmi Pillow `
  "winrt-Windows.Media.Control" `
  "winrt-Windows.Storage.Streams" `
  "winrt-Windows.Foundation.Collections" `
  -i https://mirrors.aliyun.com/pypi/simple/ `
  --trusted-host mirrors.aliyun.com

# C. Playwright + Chromium（所有 HTML 卡片渲染功能必装：天气/快递/更新日志/系统信息卡片/.sys card 等）
#    先配置国内镜像再安装
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://mirrors.cloud.tencent.com/playwright/"
python -m playwright install chromium

#    如果腾讯云镜像报 404，替换为 npmmirror：
#    $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright/"
#    python -m playwright install chromium
```

### 2.1 验证安装成功

```powershell
python -c "import khl, openai, playwright, toml, dotenv; print('Core deps: OK')"
python -c "import winrt.windows.media.control as m; print('SMTC winrt: OK')"   # 忽略 Importing from winrt 警告
python -m playwright install --dry-run chromium    # 应输出 "chromium → Already downloaded"
```

三条命令都无 `ImportError` 即表示依赖安装完成。

---

## 3. 初始化配置文件

以管理员身份运行的 PowerShell **不推荐**做日常开发操作（权限太大会导致后续生成的文件普通用户无法修改）。以下复制操作在普通用户 PowerShell 中执行即可：

```powershell
cd kook-lyric-bot
Copy-Item config\example.env              config\.env
Copy-Item config\example.bot_config.toml  config\bot_config.toml
Copy-Item config\example.adapter_config.toml config\adapter_config.toml
Copy-Item config\example.roles.toml       config\roles.toml
```

### 3.1 必须修改的字段

| 文件 | 字段 | 操作 |
|------|------|------|
| `config\.env` | `DEEPSEEK_KEY` | 填入从 https://platform.deepseek.com/ 获取的 API Key，形如 `sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `config\.env` | `ZHIPU_KEY` | （可选，要图片识别必填）填入 https://open.bigmodel.cn/ 的 Key |
| `config\.env` | `BOT_PC_KEY` | （如果启用歌词上报）填入一串 32 字符以上的随机字符串。PC 客户端脚本会用同样的字符串做 AUTH 校验，两端必须一致。生成方式：`python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `config\bot_config.toml` | `[kook] token` | 填入 KOOK 开发者门户 https://developer.kookapp.cn/bot 创建机器人后获取的 Bot Token |
| `config\bot_config.toml` | `[bot] bot的名字` | 修改为你想展示给用户的机器人昵称 |
| `config\bot_config.toml` | `[bot] bot的qq号` | 字段名遗留（旧QQ项目命名，当前KOOK项目与QQ/Tencent无任何关系）。**保持默认值 0 即可**，启动后会自动从 KOOK 网关拉取机器人自身的 KOOK 用户 ID，**绝对不要填入任何 QQ 号码**。 |
| `config\bot_config.toml` | `[bot] admin_qq` | 字段名遗留了旧 QQ 项目的命名，但存储的是 **KOOK 用户数字 ID**。将你的 KOOK 用户 ID 填在这里。该 ID 可以在 KOOK 客户端进入任意频道，对自己头像右键"复制用户 ID"获得；或者 Bot 上线后在群内对机器人发 `.kook_user_id @你自己` 获取。 |
| `config\adapter_config.toml` | `[kook] group_list` | 留空 `[]` 表示允许所有已邀请机器人进入的字频道；填入具体数字 ID 列表将启用白名单模式，仅这些频道会接收消息与指令。生产环境建议明确填写白名单。 |
| `config\roles.toml` | `admin_qq` / `op_qqs` / `qq_name_map` | 全部为遗留命名，与 QQ 无关，实际均存储 KOOK 数字 ID。`admin_qq` 必须与 bot_config.toml 的 `[bot] admin_qq` 保持完全一致（超级管理员ID）；`op_qqs` 是全局 OP 的 KOOK 用户ID数组；`qq_name_map` 是 KOOK 用户ID到显示昵称的映射表。 |

### 3.2 建议修改的字段

| 文件 | 字段 | 默认 | 说明 |
|------|------|------|------|
| `config\bot_config.toml` | `[bot] 回复兴趣` | 8 | 0-10，越高越"话多"。新服务器建议 6~7，避免在闲聊中刷屏。 |
| `config\bot_config.toml` | `[bot] 消息记录长度` | 100 | 上下文窗口消息条数；显存/内存不足可降至 50~60。 |
| `config\adapter_config.toml` | `[group_settings.<channelID>] at_only` | true | 默认仅当被 @ 时回复。对闲聊为主的频道可以设为 `false` 开启三级回复判断，让机器人参与群内话题。 |
| `config\adapter_config.toml` | `[kook] enable_private` | true | 是否允许私聊。如果你只希望在群内使用，改为 `false`。 |
| `config\bot_config.toml` | `[personality] personality_core / side / identity` | 示例人设 | **强烈建议**按 README "Custom Persona" 章节重写为你自己的人设。 |

---

## 4. 启动并验证 Bot

### 4.1 前台模式（首次调试推荐）

```powershell
cd kook-lyric-bot
.\.venv\Scripts\Activate.ps1

# 调试模式（推荐首次运行）：日志更详细
python main.py --debug

# 或正常模式
python main.py
```

启动成功应看到类似日志：

```
[INFO] [bot] KOOK 客户端连接成功，当前账号：你的Bot名 (ID=1234567890)
[INFO] [bot] 共加载 42 个指令模块，注册功能 xx 项
[INFO] [pc_status] PC 状态 TCP 接收端: 0.0.0.0:62002
[INFO] [tts] TTS TCP 接收端: 0.0.0.0:62003
```

### 4.2 验证消息通路

1. 登录 KOOK 客户端，在开发者门户为你的 Bot 生成邀请链接并邀请进入一个测试服务器。
2. 在频道中发送 `@你的Bot名 .ping`。
3. 机器人应在 1~3 秒内回复一条在线确认消息。
4. 再测试：
   - `@你的Bot名 .help` → 返回指令卡片图片或 KOOK 卡片消息
   - `@你的Bot名 .info` → 返回运行状态
   - `@你的Bot名 .s Python asyncio for 循环` → 触发搜索 + LLM 总结

以上任意一条失败请查文末 Troubleshooting 章节。

### 4.3 正确停止进程

在前台 PowerShell 中按 `Ctrl+C`。Bot 内部 signal handler 会依次：

1. 断开 KOOK WebSocket
2. 停掉 TCP 状态/歌词/TTS 服务器
3. flush 所有上下文/记忆数据到磁盘
4. 退出循环并关闭 asyncio event loop

切勿直接右上角关闭 PowerShell 窗口，可能导致正在写入的长时记忆数据损坏。一旦损坏，删除 `data/memory/long/` 中对应日期的 md 文件即可恢复。

---

## 5. PC 状态 + 歌词同步上报脚本

`scripts\pc_status_reporter.py` 是一个**独立运行的客户端程序**，它运行在你听歌的 Windows 本机上，主动通过 TCP 连接到机器人服务器（端口 62002/62003）。

- 如果你的 Bot 和听歌机器是**同一台 Windows**：参考本章节直接在本机运行，`BOT_SERVER` 填 `127.0.0.1`。
- 如果你的 Bot 部署在**云服务器**，听歌是本地 Windows 机器：`BOT_SERVER` 填服务器公网 IP 或解析到服务器的域名，确保服务器 62002/62003 的 TCP 端口对本机放行（见 Linux 部署文档的防火墙章节）。

### 5.1 配置环境变量

pc_status_reporter.py **不再内置任何默认值**。缺少任一环境变量时会立即拒绝启动并打印设置指引。

打开 **Windows PowerShell**（普通用户即可，不需要管理员），执行以下命令：

```powershell
# ── 临时生效（当前 PowerShell 窗口内有效，调试用） ──
$env:BOT_SERVER   = "127.0.0.1"                 # 如果 bot 和 reporter 是同一台
# $env:BOT_SERVER = "bot.example.com"            # 如果 bot 在远程服务器
$env:BOT_PC_PORTS = "62002,62003"                # 必须和服务器端监听的端口完全一致
$env:BOT_PC_KEY   = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # 必须和 config\.env 里的 BOT_PC_KEY 完全相同
```

若要**永久生效**（推荐，避免每次开窗口都重设），用系统设置写入当前用户的环境变量：

1. 按 `Win + S` 搜索 "环境变量"，打开 "编辑账户的环境变量"。
2. 点击上半部分（用户变量）的 "新建" 按钮。
3. 依次添加三条：变量名 `BOT_SERVER`、`BOT_PC_PORTS`、`BOT_PC_KEY`，值与上面完全一致。
4. 确定后关闭。**所有已经打开的 PowerShell / CMD 窗口必须关掉重开**，环境变量变更只对新进程生效。

### 5.2 启动并验证歌词上报

先开一个支持 SMTC 的播放器播放任意歌曲。支持列表：

- Spotify Desktop（推荐，测试最充分）
- 网易云音乐 UWP / 最新 Win32 版
- QQ音乐 UWP / Win32
- Apple Music (Windows 预览版)
- Groove Music / 媒体播放器 (Windows 11 自带)
- 任何在 Windows 音量合成器左上角显示专辑封面 + 标题的播放器（即使用了 SMTC API 的都可以）

然后，在项目目录执行：

```powershell
cd kook-lyric-bot
.\.venv\Scripts\Activate.ps1
python scripts\pc_status_reporter.py
```

启动成功应看到：

```
══ PC 状态上报 v6.80 Windows版 ══
SMTC: OK (已连接 ISystemMediaTransportControls)
正在连接 127.0.0.1:62002 ...
AUTH: OK (与服务器握手成功)
[20:15:03] 载荷音乐: song=ET - 我爱你但是我要回家 lyric_event=False player=Spotify cover=True
[20:15:07] 歌词: QQ音乐 (47行)
[20:15:07] 歌词补位启动: ...
```

此时回到 KOOK 服务器，发送 `.sys` 指令（或 `.sys card` 查看渲染卡片）应能看到 CPU / GPU / 内存 / 当前播放歌曲等信息。发送任意歌词相关查询，或等下一句歌词，机器人应该会在聊天中推送逐句歌词。

### 5.3 调节歌词延迟（LYRIC_OFFSET_MS）

歌词推送默认偏移 `OFFSET = 1500 ms`，即 LRC 文件中的时间戳 + 1500 ms 后推给用户，补偿网络 + KOOK 网关处理的延迟。

- 如果你发现歌词**总是比听到的早**：增大 OFFSET（每次 +300 ms 逐步试）。修改 pc_status_reporter.py 顶部的 `LYRIC_OFFSET_MS` 常量后重启进程。
- 如果你发现歌词**总是比听到的慢**：减小 OFFSET（不建议低于 500 ms，容易导致下一句网络未返回就被跳过）。

调试歌词同步问题时，将 pc_status_reporter.py 顶部的 `_LYRIC_SYNC_LOG = False` 改为 `True`，日志会打印每一条歌词的 drift、refill 动作和 gap 分析。提交 Issue 或提问时请附带这部分原始日志。

---

## 6. 开机自启：两种方案

二选一即可。个人桌面推荐方案 A（任务计划程序），配置最简单不需要额外软件；7x24 小时运行的服务器推荐方案 B（NSSM 注册系统服务），进程崩溃可自动重启。

### 方案 A：任务计划程序（推荐普通用户）

1. 按 `Win + S` 搜索 "任务计划程序"，打开。
2. 右侧点 "创建任务..."（不是"创建基本任务"）。
3. **常规** 选项卡：
   - 名称：`KookLyricBot`
   - 描述：可选
   - 勾选 "不管用户是否登录都要运行" 不勾选即可（推荐"只在用户登录时运行"，因为 SMTC 歌词上报必须运行在用户登录会话中）
   - 勾选 "使用最高权限运行"（避免写日志/写临时文件被 UAC 拦截）
   - 配置为：Windows 10 / 11
4. **触发器**：新建 → "登录时" → 确定（任何用户登录都行）。
5. **操作** 选项卡：新建 →
   - 操作：启动程序
   - 程序或脚本：`C:\path\to\kook-lyric-bot\.venv\Scripts\python.exe`（按实际路径替换）
   - 添加参数：`main.py`
   - 起始于：`C:\path\to\kook-lyric-bot`（关键，项目根目录）
6. **条件** 选项卡：取消勾选 "只有在计算机使用交流电源时才启动此任务"。
7. **设置** 选项卡：勾选 "如果任务失败，按以下频率重新启动" → 1 分钟、最多 3 次；勾选 "如果任务运行超过 3 天则停止任务"（不建议，让它永远跑）。
8. 确定，输入当前用户密码保存。
9. （可选）按同样步骤再建一个任务 `PcStatusReporter`：
   - 程序：同样的 python.exe 路径
   - 参数：`scripts\pc_status_reporter.py`
   - 起始于：同样的项目根目录

完成后，在任务计划程序库中选中任务 → 右键 "运行" 一次测试是否正常启动。查看 `logs\` 目录下的日志文件验证启动无报错。

### 方案 B：NSSM 注册系统服务（推荐 7x24 服务器）

NSSM = Non-Sucking Service Manager，是将任意 exe 包装成 Windows 服务的标准工具。

```powershell
# 1. 下载 NSSM：https://nssm.cc/release/nssm-2.24.zip
#    解压，按你的 CPU 架构选 win64/nssm.exe，放到项目根目录或者 C:\Windows\System32
#    （以下示例假设 nssm.exe 已在 PATH）

# 2. 注册 Bot 服务
nssm install KookLyricBot

# 弹出的图形界面：
#   Application 选项卡：
#     Path:      C:\path\to\kook-lyric-bot\.venv\Scripts\python.exe
#     Arguments: main.py
#     Startup dir: C:\path\to\kook-lyric-bot
#   Details 选项卡：
#     Display name: Kook Lyric Bot
#   Log on 选项卡：
#     勾选 "This account" → 输入你的 Windows 用户名 + 密码（SMTC 歌词上报必须在真实用户会话下运行，不能用 Local System）
#   I/O 选项卡：
#     Output (stdout):  C:\path\to\kook-lyric-bot\logs\service-stdout.log
#     Error  (stderr):  C:\path\to\kook-lyric-bot\logs\service-stderr.log
#   Recovery 选项卡：
#     First/Second/Subsequent failures → 全部改成 "Restart the service"，Restart delay = 1000 ms
#
# 点击 Install service 安装。

# 3. 注册 PC Status Reporter 服务（同样流程，Arguments 改成 scripts\pc_status_reporter.py）
nssm install KookLyricReporter

# 4. 启动服务
nssm start KookLyricBot
nssm start KookLyricReporter

# 查看服务状态
nssm status KookLyricBot
# 服务管理器里也能看到 services.msc → 找到 KookLyricBot → 状态: 正在运行
```

注意：**歌词上报服务必须运行在真实登录用户的账户下**，不能使用 `Local System`。Local System 账户无权访问用户会话的 SMTC 媒体控制接口，会导致 reporter 启动后永远找不到播放中的歌曲。

---

## 7. 常见问题 (FAQ)

### Q1：启动后日志里 KOOK 一直报 "连接失败" / 反复重连

A. 按顺序排查：
1. 确认 `[kook] token` 无前后空格、引号正确。
2. 确认 Bot Token 未过期、未被重置（在开发者门户重新复制一份粘贴）。
3. 本机网络环境需要直连 KOOK 网关。如果在受限网络，尝试在 PowerShell 中设置代理：
   ```powershell
   $env:HTTP_PROXY  = "http://127.0.0.1:7890"
   $env:HTTPS_PROXY = "http://127.0.0.1:7890"
   python main.py
   ```
   khl.py / httpx / openai SDK 均会自动读取这两个环境变量。

### Q2：.ping 有反应，但 @机器人对话没有回复

A. 常见原因：
1. 群内未真正 @机器人 —— 在 KOOK 客户端确认消息气泡中机器人名是蓝色高亮（即真正的 mention，而不是手敲的"@机器人名"文字）。
2. `group_list` 白名单模式下，当前频道不在列表中。检查 `adapter_config.toml` 的 `[kook] group_list`。
3. `at_only = true` 且消息里没有 @机器人，但你期望机器人主动接话。要么改成 `false`，要么每次都 @。
4. LLM 调用报错。看 `logs\` 日志搜索 "LLM FAIL" / "401" / "429"：
   - 401 = API Key 错误或过期，检查 `config\.env` 的 DEEPSEEK_KEY。
   - 429 = 速率限制 / 余额不足，在 DeepSeek 控制台查看用量和充值。

### Q3：图片识别后，LLM 答 "我不知道这张图是什么"

A. 双路径触发策略：
- 纯图片无文字的消息：走后台异步识别（不阻塞回复），识别结果注入后 LLM 不会"倒回去"再回答之前的图。正确用法是：发一张图，**紧接着**再提问"图里是什么？"或"帮我描述一下"。
- 图文混合 + 带 @ 的消息：同步等待识图完成后再进管道。直接一次性发 `@机器人 [图片] 这是什么？` 即可。

### Q4：.sys 提示 "PC 客户端未连接"，但 pc_status_reporter.py 明明在跑

A. TCP 握手失败，按顺序排查：
1. pc_status_reporter.py 启动日志里是否打印 `AUTH: OK`？如果连 `AUTH: OK` 都没有，说明 TCP 连不通：
   - Bot 和 reporter 是同一台：检查 `BOT_SERVER=127.0.0.1`，确认 `BOT_PC_PORTS=62002,62003`。
   - Bot 在远程服务器：`Test-NetConnection bot.example.com -Port 62002` 如果 TcpTestSucceeded = False，说明服务器防火墙/安全组未放通 62002/62003。
2. 有 `AUTH: FAIL` 日志：**100% 是两端 BOT_PC_KEY 不一致**。把两边的环境变量都重新复制一遍，注意前后绝对不能有空格、换行、引号等多余字符。
3. AUTH OK 但 `.sys` 仍说未连接：机器人端的 `BOT_PC_KEY` 环境变量没读到。检查你是如何启动 bot 的：如果用 NSSM/任务计划，确认系统服务启动的用户账户上下文里环境变量确实被设置了（系统级环境变量 vs 用户级环境变量 vs 运行服务的用户不同）。最稳妥办法：在 `config\.env` 里加上 `BOT_PC_KEY=xxxx`，bot.py 启动时会自动从 .env 读取，优先级高于系统环境变量。

### Q5：歌词推送 "卡一下 → 变慢 → 慢慢对齐 → 又卡一下" 的周期性循环

A. 这是 v6.80 之前的典型问题，如果你的脚本版本号显示小于 v6.80，请先升级到最新版。如果已经是最新版：
1. 检查你使用的播放器：Spotify 没问题；网易云音乐 2.10 之前的 Win32 版 SMTC 更新频率较低（1~2 秒一次），v6.80 的进度滤波器虽然能补偿，但会有漂移。建议升级播放器或改用 Spotify。
2. 调大 `LYRIC_OFFSET_MS` 到 1800~2000。
3. 开启 `_LYRIC_SYNC_LOG=True`，把日志发给维护者分析，通常一屏日志就能 100% 定位具体是哪一条出错。

### Q6：修改了 `bot_config.toml` 的 persona / threshold，需要重启吗？

A. 不需要。三种方式任选其一，均可**热重载**：
1. 在 KOOK 群内发 `@你的Bot名 .reload`（你必须是 [admin]）。
2. Linux：`systemctl reload kook-lyric-bot`（实际发送 SIGUSR1）。
3. Windows：如果是前台运行的 PowerShell，不支持信号，最简单方式是在项目目录执行 `echo reload > data\control.txt`，Bot 会在主循环下一次 tick 中检测并自动重载。

### Q7：Chromium 下载超慢或下载失败

A. 切换 Playwright 下载镜像：
```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright/"
python -m playwright install chromium
```
仍失败的话，手动下载：在 `https://registry.npmmirror.com/binary.html?path=playwright/` 找到对应系统的 Chromium zip，解压到：
```
%USERPROFILE%\AppData\Local\ms-playwright\chromium-<版本号>\chrome-win\chrome.exe
```

---

## 8. 日志与数据目录

运行后会自动创建以下目录，全部加入 `.gitignore`，不会被提交：

| 路径 | 内容 | 清理策略 |
|------|------|----------|
| `logs\` | 每天一个 `.log` 文件 | 按需删除早于 7 天的，不会影响运行 |
| `data\img_temp\` | 识图/绘图的临时图片（最多保留 48 小时） | 主循环每小时自动清理一次 |
| `data\memory\stm\` | 短时记忆 JSON 文件（每频道一个） | 通常不需要清理；如果某频道数据异常可单独删除对应 json |
| `data\memory\long\` | 长时记忆 Markdown（可人工编辑） | 按需整理，删除后机器人会失去对应日期的长期记忆 |

备份建议：每周备份一次 `config\` 目录和 `data\memory\long\` 目录到安全位置。
