# macOS 部署指南

本文档覆盖 macOS 12 Monterey 及以上版本（推荐 macOS 13 Ventura / 14 Sonoma / 15 Sequoia）的全流程部署：

1. Bot 本体部署（Python 虚拟环境 + 配置初始化 + 前台/服务启动）
2. Mac 状态 + 歌词同步上报脚本部署（AppleScript 原生媒体查询：Music.app + Spotify）
3. 开机自启：launchd 系统服务注册

---

## 0. 前置条件

| 项目 | 最低要求 | 备注 |
|------|----------|------|
| 操作系统 | macOS 12 Monterey+ | macOS 11 及以下 AppleScript 查询 Music.app 的接口不完全兼容；歌词上报不可用，Bot 本体仍可运行 |
| Python | 3.10, 3.11, or 3.12 (arm64 / x86_64) | Apple Silicon (M1/M2/M3/M4) 原生 arm64 与 Intel x86_64 均支持；Rosetta 2 下也可运行 |
| 网络 | 可访问 KOOK 网关及 LLM 提供商 API | 国内用户建议全程使用镜像源 |
| 权限 | 普通用户即可；首次运行歌词上报时会弹出「自动化」权限请求，必须允许 |

### 0.1 确认 Python 安装与架构

打开「终端.app」（Terminal）执行：

```bash
python3 -c "import sys, struct; print(f'{sys.version} ({struct.calcsize(\"P\") * 8}-bit)')"
```

Apple Silicon 机器应输出 `... 64 bit (ARM64)`，Intel 机器输出 `... 64 bit (x86_64)`。如果命令不存在，或版本低于 3.10，按下面步骤安装。

### 0.2 安装 Python 3.10+（如果尚未安装）

**方式 A：Homebrew（推荐，更新方便）**

```bash
# 如果还没装 Homebrew：
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 Python（默认装最新稳定版，通常是 3.12）
brew install python

# 安装后验证
python3 --version
```

**方式 B：官网安装包**

前往 `https://www.python.org/downloads/macos/` 下载最新 3.10+ 安装包，双击运行。安装完成后在终端执行 `Install Certificates.command`（在 `/Applications/Python 3.x/` 目录下），否则 SSL 证书不完整会导致 pip/requests 报错。

### 0.3 安装 Xcode 命令行工具（必须）

Playwright 的 Chromium 和部分 Python C extension 需要系统级编译工具链：

```bash
xcode-select --install
```

弹出的对话框点击「安装」。如果系统提示「已安装」即可跳过。

---

## 1. 获取代码与创建虚拟环境

```bash
# 1. 克隆项目
git clone <your-repo-url> ~/kook-lyric-bot
cd ~/kook-lyric-bot

# 2. 创建虚拟环境（首次执行一次即可）
python3 -m venv .venv

# 3. 激活虚拟环境 —— 每次新开终端都要执行一次
source .venv/bin/activate
# 激活后，命令行提示符出现 (.venv) 前缀。此时 python 和 pip 指向 venv 内版本。

# 4. 升级 pip + setuptools + wheel
pip install --upgrade pip setuptools wheel
```

---

## 2. 安装依赖

中国大陆用户请全程使用阿里云 PyPI 镜像加速，Playwright Chromium 使用腾讯云镜像：

```bash
# A. 核心依赖（必装）
pip install -r requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  --trusted-host mirrors.aliyun.com

# B. macOS 歌词上报额外依赖（如果你要在本机运行 mac_status_reporter.py）
#    默认只有 requests + Pillow，mac_status_reporter.py 顶部有说明
pip install requests Pillow \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  --trusted-host mirrors.aliyun.com

# C. Playwright + Chromium（所有 HTML 卡片渲染功能必装）
PLAYWRIGHT_DOWNLOAD_HOST=https://mirrors.cloud.tencent.com/playwright/ \
  python -m playwright install chromium

#    如果腾讯云镜像报 404，替换为 npmmirror：
#    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright/ \
#      python -m playwright install chromium
```

### 2.1 安装 Playwright 系统级运行时依赖（macOS 通常不需要，但推荐执行一次以防缺库）

```bash
python -m playwright install-deps chromium
```

该命令会通过 Homebrew 安装 Chromium headless 运行所需的系统库（主要是 fontconfig、freetype 等）。macOS 自带大多数库，但 Homebrew 装的中文字体包能避免卡片渲染出现「豆腐块」（.notdef 空白字）。

### 2.2 验证安装成功

```bash
python -c "import khl, openai, playwright, toml, dotenv; print('Core deps: OK')"
python -m playwright install --dry-run chromium   # 应输出 "chromium → Already downloaded"
# 如果要跑歌词上报：
python -c "import requests; from PIL import Image; print('Reporter deps: OK')"
```

三条命令都无 `ImportError` 即表示依赖安装完成。

---

## 3. 初始化配置文件

```bash
cd ~/kook-lyric-bot

cp config/example.env              config/.env
cp config/example.bot_config.toml  config/bot_config.toml
cp config/example.adapter_config.toml config/adapter_config.toml
cp config/example.roles.toml       config/roles.toml

# 关键：将 .env 文件权限收紧为只有属主可读，防止其他系统用户偷看 API Key
chmod 600 config/.env
```

### 3.1 必须修改的字段

Windows 部署文档的「必须修改的字段」表格完全适用于 macOS，不再重复，仅强调几点：

| 字段 | macOS 部署特别说明 |
|------|--------------------|
| `config/bot_config.toml` `[bot] bot的qq号` | 遗留字段名（旧 QQ 项目命名，当前 KOOK 项目与腾讯 QQ 无任何关系）。**保持默认值 0 即可**，启动后会自动从 KOOK 网关拉取机器人自身的 KOOK 用户 ID，**绝对不要填入任何 QQ 号码**。 |
| `config/bot_config.toml` `[bot] admin_qq` | 字段名遗留，实际存储 **KOOK 用户数字 ID**。在 KOOK 客户端对自己头像右键「复制用户 ID」即可。 |
| `config/roles.toml` `admin_qq` / `op_qqs` / `qq_name_map` | 全部为遗留命名，与 QQ 无关，实际存储 KOOK 数字 ID。`admin_qq` 必须与 bot_config.toml 的 `[bot] admin_qq` 保持完全一致（超级管理员 ID）；`op_qqs` 是全局 OP 的 KOOK 用户 ID 数组；`qq_name_map` 是 KOOK 用户 ID 到显示昵称的映射字典。 |
| `config/.env` 中的 `BOT_PC_KEY` | **必须设置**。歌词上报客户端连接服务器时会发送 `AUTH <BOT_PC_KEY>`，两端必须完全一致。生成强随机密钥：`python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `config/adapter_config.toml` `[kook] group_list` | 留空 `[]` 表示允许所有已邀请机器人进入的字频道；填入具体数字 ID 列表将启用白名单模式，仅这些频道会接收消息与指令。生产环境建议明确填写白名单。 |

---

## 4. 启动并验证 Bot

### 4.1 前台模式（首次调试推荐）

```bash
cd ~/kook-lyric-bot
source .venv/bin/activate

# 调试模式（推荐首次运行）：日志更详细
python main.py --debug

# 或正常模式
python main.py
```

启动成功应看到类似日志：

```
[INFO] [bot] KOOK 客户端连接成功，当前账号：你的Bot名 (ID=1234567890)
[INFO] [bot] 共加载 xx 个指令模块，注册功能 xx 项
[INFO] [pc_status] PC 状态 TCP 接收端: 0.0.0.0:62002
[INFO] [tts] TTS TCP 接收端: 0.0.0.0:62003
```

### 4.2 验证消息通路

与 Windows / Linux 相同：在 KOOK 频道发送 `@你的Bot名 .ping` → 有回复；再测试 `.help`、`.info`、`.s 关键词` 等。

### 4.3 正确停止进程

在前台终端按 `Ctrl+C`。Bot 内部 signal handler 会依次：断开 KOOK WebSocket、停掉 TCP 服务、flush 所有上下文/记忆数据到磁盘、退出循环。切勿直接关闭终端窗口，可能导致长时记忆 Markdown 文件写入中途损坏（损坏后删除对应日期文件即可恢复）。

---

## 5. Mac 状态 + 歌词同步上报脚本

`scripts/mac_status_reporter.py` 是运行在用户听歌的 macOS 本机上的独立客户端程序，主动通过 TCP 连接到机器人服务器（端口 62002/62003）。**Bot 服务器不运行此脚本**。

- 如果 Bot 和听歌是**同一台 Mac**：`BOT_SERVER` 填 `127.0.0.1`
- 如果 Bot 部署在**云服务器**，听歌是本地 Mac：`BOT_SERVER` 填服务器公网 IP 或域名，确保服务器 62002/62003 TCP 端口对本机放行（见 Linux 部署文档的防火墙与云安全组章节）

### 5.1 配置环境变量

mac_status_reporter.py **清空了所有默认值**。缺少任一环境变量时会立即拒绝启动并打印设置指引。

**方式 A：临时生效（当前终端窗口内有效，调试用）**

```bash
export BOT_SERVER="127.0.0.1"                 # 如果 bot 和 reporter 在同一台
# export BOT_SERVER="bot.example.com"         # 如果 bot 在远程服务器
export BOT_PC_PORTS="62002,62003"              # 必须和服务器监听端口完全一致
export BOT_PC_KEY="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # 必须和 config/.env 里的 BOT_PC_KEY 完全相同
```

**方式 B：永久生效（推荐），写入当前用户的 shell 配置文件**

根据你使用的 shell（macOS 10.15+ 默认是 zsh，之前是 bash）编辑对应文件：

```bash
# zsh（macOS Catalina 及以后默认）
echo 'export BOT_SERVER="127.0.0.1"'              >> ~/.zshrc
echo 'export BOT_PC_PORTS="62002,62003"'           >> ~/.zshrc
echo 'export BOT_PC_KEY="替换为与服务器端一致的KEY"' >> ~/.zshrc
source ~/.zshrc

# bash（macOS Mojave 及更早默认，或手动改回 bash 的用户）
echo 'export BOT_SERVER="127.0.0.1"'              >> ~/.bash_profile
echo 'export BOT_PC_PORTS="62002,62003"'           >> ~/.bash_profile
echo 'export BOT_PC_KEY="替换为与服务器端一致的KEY"' >> ~/.bash_profile
source ~/.bash_profile
```

### 5.2 授予 AppleScript「自动化」与「辅助功能」权限（关键步骤！）

macOS 的 AppleScript 安全机制要求显式授权。**首次运行歌词上报脚本时必须完成以下步骤，否则 AppleScript 查询 Music.app / Spotify 会永远返回空字符串，脚本会报告"没有检测到播放的歌曲"。**

1. 先在终端执行一次脚本（见 5.3），触发系统弹出权限对话框。
2. 打开「系统设置」（System Settings）→ 「隐私与安全性」（Privacy & Security）：
   - **自动化（Automation）**：在列表中找到「终端」（Terminal）或「iTerm」，勾选允许它控制 **Music** 和 **Spotify**。
   - **辅助功能（Accessibility）**：如果列表里没有终端，点击「+」号，从「应用程序 - 实用工具」中添加「终端.app」，勾选允许。mac_status_reporter 通过 AppleScript 查询前台窗口和 System Events 进程时需要此权限。
   - **完全磁盘访问权限（Full Disk Access）**：可选，但如果 Music.app 的音乐库在受保护的目录下（如 `~/Music/` 受 SIP 保护的子目录），建议也勾选以防万一。
3. 勾选后**完全退出终端再重新打开**（右键 Dock 图标 → 退出，确保进程完全终止），然后重新运行脚本。权限变更只对新进程生效。

如果脚本运行后仍无法读取播放信息，进入「隐私与安全性」→ 最底部的「其他」部分点击「自动化」，把终端对应的 Music / Spotify 勾选框**取消再重新勾选一次**，然后重启终端。macOS Ventura 及以后版本存在权限缓存不刷新的问题。

### 5.3 启动并验证歌词上报

先打开一个支持的播放器播放任意歌曲。AppleScript 原生支持列表：

- **Music.app**（macOS 自带，原 iTunes）— 播放 Apple Music 曲库、本地音乐文件、匹配的云端音乐
- **Spotify Desktop**（推荐，测试最充分）— 从 Spotify 官网安装的桌面版
- 其他播放器（如网易云音乐 Mac 版、QQ音乐 Mac 版）**不支持原生 AppleScript 媒体接口**，无法被自动检测。如果要使用这些播放器，需要在对应播放器设置中开启「系统通知显示播放信息」或开启「将播放信息写入 SMTC 兼容接口」（macOS 没有 SMTC，少数播放器通过 NowPlaying 插件模拟），但成功率不高，推荐改用 Music.app 或 Spotify。

然后执行：

```bash
cd ~/kook-lyric-bot
source .venv/bin/activate
python scripts/mac_status_reporter.py
```

启动成功应看到类似输出：

```
══ Mac 状态上报 v1.20 macOS版 ══
AppleScript: OK (osascript 可用)
正在连接 127.0.0.1:62002 ...
AUTH: OK (与服务器握手成功)
[20:15:03] 载荷音乐: song=陈奕迅 - 富士山下 lyric_event=False player=Music cover=True
[20:15:07] 歌词: QQ音乐 (47行)
[20:15:07] 歌词补位启动: ...
```

此时回到 KOOK 发送 `.sys` 应能看到 CPU / 内存 / 电池 / 当前播放歌曲等信息；歌词会按播放进度逐句推送。

### 5.4 调节歌词延迟（LYRIC_OFFSET_MS）

mac_status_reporter.py 顶部的 `_LYRIC_OFFSET_MS` 常量默认值为 0。对于远程服务器部署的 Bot，建议在 1500 ~ 2000 ms 之间调节：

- 歌词总是**比听到的早**：增大 OFFSET（每次 +300 ms 逐步调）
- 歌词总是**比听到的慢**：减小 OFFSET（不建议低于 500 ms）

修改保存后重启 reporter 脚本生效。调试歌词同步问题时，将脚本顶部的 `_LYRIC_SYNC_LOG = False` 改为 `True`，日志会打印每一句的 drift、refill 动作和 gap 分析。提交 Issue 时请附带这部分原始日志。

---

## 6. 开机自启：launchd 注册服务

macOS 的标准服务管理框架是 launchd，对应 Linux 的 systemd、Windows 的服务管理器。以下为 Bot 本体和歌词上报分别创建 launchd plist，实现用户登录后自动后台运行、进程崩溃自动重启。

### 6.1 Bot 本体 launchd plist

用任意文本编辑器创建 `~/Library/LaunchAgents/com.kooklyric.bot.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kooklyric.bot</string>

    <!-- 可执行文件：用 venv 绝对路径的 python，避免 PATH 问题 -->
    <key>ProgramArguments</key>
    <array>
        <string>/Users/你的用户名/kook-lyric-bot/.venv/bin/python</string>
        <string>main.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/你的用户名/kook-lyric-bot</string>

    <!-- 环境变量：从 .env 文件读取，launchd 默认不加载 ~/.zshrc -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/你的用户名/kook-lyric-bot/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- 运行条件：用户登录后才启动（与 Windows "只在用户登录时运行" 一致） -->
    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
    </array>

    <!-- 开机自启：登录取决于 RunAtLoad -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 进程崩溃自动重启：只要非正常退出码就重启，间隔最小 10 秒 -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- 超时设置：启动 60 秒内不算 "启动失败"，避免 Playwright 首次拉 Chromium 被判定挂死 -->
    <key>ExitTimeOut</key>
    <integer>30</integer>

    <!-- 日志：stdout/stderr 分别写入 logs/ 目录 -->
    <key>StandardOutPath</key>
    <string>/Users/你的用户名/kook-lyric-bot/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/你的用户名/kook-lyric-bot/logs/launchd-stderr.log</string>

    <!-- 资源限制（可选）：防止内存泄漏吃光机器内存 -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>4096</integer>
    </dict>
</dict>
</plist>
```

**重要：** 将文件中所有 `/Users/你的用户名/` 替换为你实际的 home 目录绝对路径（可以用 `echo $HOME` 查看）。plist 中**绝对不能出现 `~` 波浪号路径展开符**，launchd 不会解析。

### 6.2 歌词上报 launchd plist

同样，创建 `~/Library/LaunchAgents/com.kooklyric.reporter.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kooklyric.reporter</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/你的用户名/kook-lyric-bot/.venv/bin/python</string>
        <string>scripts/mac_status_reporter.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/你的用户名/kook-lyric-bot</string>

    <!-- 必须显式注入 BOT_* 三个环境变量，launchd 不读取 ~/.zshrc -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/你的用户名/kook-lyric-bot/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>BOT_SERVER</key>
        <string>127.0.0.1</string>
        <key>BOT_PC_PORTS</key>
        <string>62002,62003</string>
        <key>BOT_PC_KEY</key>
        <string>替换为与服务器端一致的KEY</string>
    </dict>

    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/你的用户名/kook-lyric-bot/logs/launchd-reporter-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/你的用户名/kook-lyric-bot/logs/launchd-reporter-stderr.log</string>
</dict>
</plist>
```

### 6.3 加载并验证 launchd 服务

```bash
# 先确保 logs 目录存在
mkdir -p ~/kook-lyric-bot/logs

# 1. 加载 Bot 服务
launchctl load ~/Library/LaunchAgents/com.kooklyric.bot.plist

# 2. 加载歌词上报服务
launchctl load ~/Library/LaunchAgents/com.kooklyric.reporter.plist

# 3. 验证已加载：应能看到两个 Label 列出
launchctl list | grep kooklyric

# 4. 查看是否在运行
#    PID 列显示数字 = 正在运行；PID = "-" 且 Status != 0 = 启动失败，立即看日志
launchctl print gui/$UID/com.kooklyric.bot
launchctl print gui/$UID/com.kooklyric.reporter
```

### 6.4 日常管理命令

```bash
# 重启服务（例如改了配置需要热加载之外的重启）
launchctl unload ~/Library/LaunchAgents/com.kooklyric.bot.plist
launchctl load   ~/Library/LaunchAgents/com.kooklyric.bot.plist

# 简化：使用 kickstart（macOS 11+ 推荐）
launchctl kickstart -k gui/$UID/com.kooklyric.bot      # -k = 杀掉重启，不用 unload/load 两步

# 实时看日志（等于 tail -f）
tail -f ~/kook-lyric-bot/logs/launchd-stdout.log
tail -f ~/kook-lyric-bot/logs/launchd-reporter-stdout.log

# 临时停止（不取消开机自启，下次登录仍会启动）
launchctl unload ~/Library/LaunchAgents/com.kooklyric.bot.plist
# 重新启用
launchctl load   ~/Library/LaunchAgents/com.kooklyric.bot.plist

# 彻底移除（不再开机自启）
launchctl unload -w ~/Library/LaunchAgents/com.kooklyric.bot.plist
launchctl unload -w ~/Library/LaunchAgents/com.kooklyric.reporter.plist
rm ~/Library/LaunchAgents/com.kooklyric.bot.plist
rm ~/Library/LaunchAgents/com.kooklyric.reporter.plist
```

### 6.5 热重载配置

改了 `bot_config.toml` / `adapter_config.toml` 后，不需要重启进程。两种方式：

1. 在 KOOK 频道发 `@你的Bot名 .reload`（你必须是 [admin] 角色）
2. 在终端向 Python 进程发送 SIGUSR1 信号：
   ```bash
   pkill -SIGUSR1 -f "python main.py"
   tail -n 20 ~/kook-lyric-bot/logs/launchd-stdout.log
   # 应看到 "[bot] 收到 SIGUSR1 信号，热重载配置..." 一行
   ```

---

## 7. 常见问题 (FAQ)

### Q1：歌词上报脚本启动后一直报"没有检测到播放的歌曲"，但 Music/Spotify 明明在播放

A. 100% 是 AppleScript 权限问题。按以下步骤彻底排查：

1. **手动测试 AppleScript 是否工作**（脚本外独立验证，排除脚本代码问题）：
   ```bash
   # 测试 Music.app
   osascript -e 'tell application "Music" to get {artist of current track, name of current track, player state}'
   # 正常应输出类似：陈奕迅, 富士山下, playing

   # 测试 Spotify
   osascript -e 'tell application "Spotify" to get {artist of current track, name of current track, player state}'
   ```
   如果上面两条任何一条报错或返回空字符串，说明权限没授好，继续下一步。
2. 打开「系统设置 → 隐私与安全性 → 自动化」：
   - 确保「终端 / iTerm」下的 **Music** 和 **Spotify** 勾选框是**绿色勾选**。
   - 如果没有「终端」条目，运行一次上面的 `osascript` 命令让系统触发权限请求弹窗，此时弹窗一定要点「允许」，不要点「拒绝」。
   - 如果已经是勾选状态但仍报错：**取消勾选 → 完全退出终端 → 重新打开终端 → 重新勾选 → 再完全退出终端重开**。权限缓存刷新很顽固。
3. 仍然无效：检查「系统设置 → 通用 → 登录项与扩展」下是否有被禁用的系统扩展；或直接在「终端」上右键「显示简介」，确认没有"以 Rosetta 打开"之类导致权限上下文错位的设置。

### Q2：launchd 加载后 PID 始终是 "-"，Status 非零（启动失败）

A. 直接看日志，launchd 本身不输出错误详情到 shell：

```bash
cat ~/kook-lyric-bot/logs/launchd-stderr.log | tail -n 50
```

常见错误：
- `ModuleNotFoundError: No module named 'khl'` → ProgramArguments 里的 python 路径写错了，没用 venv 的绝对路径。
- `Permission denied` → plist 文件权限必须是 `644`，owner 必须是你自己：
  ```bash
  chmod 644 ~/Library/LaunchAgents/com.kooklyric.*.plist
  chown $(whoami):staff ~/Library/LaunchAgents/com.kooklyric.*.plist
  ```
- `SyntaxError` / `TomlDecodeError` → config/*.toml 填错了语法。先用前台 `python main.py` 跑通再上 launchd。
- `KOOK WebSocket: 401 Unauthorized` → Bot Token 无效。

### Q3：KOOK 能连上、回复正常，但 `.sys` 一直说 "PC 客户端未连接"

A. 四层排查（与 Windows 版完全一致）：

1. reporter 启动日志里是否打印 `AUTH: OK`？
   - 没有 `AUTH: OK`：TCP 连接失败。同机运行的话看 `BOT_SERVER=127.0.0.1` 对不对；远程服务器的话在 Mac 上 `nc -zv bot.example.com 62002`（需要 `brew install netcat`）测端口连通性。
2. `AUTH: FAIL`：两端 BOT_PC_KEY 100% 不一致。把 Mac 的 launchd plist 里的 `<string>替换为与服务器端一致的KEY</string>` 和服务器 config/.env 的逐字符肉眼对比，**千万不能有空格、换行、中文标点、引号等多余字符**。launchd plist 的 string 标签内容就是纯字符串，不要加任何引号包裹。
3. `AUTH: OK` 但 `.sys` 仍说未连接：Bot 端没读到 BOT_PC_KEY 环境变量。因为 launchd 的 EnvironmentVariables 只注入给它启动的子进程，**不会读取 .env 文件**。Bot 端会自动读 `config/.env`，如果你把 BOT_PC_KEY 写在 .zshrc 的 export 里但没写进 config/.env，launchd 启动的 Bot 进程是拿不到的，**必须写进 config/.env**。

### Q4：歌词推送"卡一下 → 变慢 → 慢慢对齐 → 又卡"的周期性循环

A. 如果你的 mac_status_reporter.py 版本号低于 v1.20，先升级到最新版。v1.20 已经彻底修复这个问题（进度滤波去相同y值污染 + TIMER 宽容3句快速补发）。如果已是 v1.20 仍有问题：
1. 换用 Spotify 试一下：Music.app 的 AppleScript `player position` 字段刷新间隔可能长达 1~2 秒，v1.20 的滤波算法虽然能补偿，但 Apple Silicon 上省电模式下系统调度会加重抖动。
2. 把 `_LYRIC_OFFSET_MS` 调到 1800~2000。
3. 开启 `_LYRIC_SYNC_LOG=True`，把日志发给维护者分析，通常一屏日志就能精确定位。

### Q5：Playwright Chromium 下载失败 / 卡片渲染报 "Browser closed unexpectedly"

A. macOS 上 Playwright 常见于 Rosetta 2 下的 Python（x86_64 架构运行在 Apple Silicon 上）导致架构不匹配：
1. 确认 Python 架构：`python3 -c "import platform; print(platform.machine())"` 应输出 `arm64`（Apple Silicon）或 `x86_64`（Intel）。
2. 如果是 arm64 但装了 x86_64 的 Homebrew Python，建议卸载后重新 `arch -arm64 brew install python` 安装原生版本。
3. 切换下载镜像：
   ```bash
   PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright/ \
     python -m playwright install chromium
   ```
4. 仍然失败：手动执行 `python -m playwright install-deps chromium` 安装系统库，然后重新安装浏览器。

### Q6：休眠唤醒后歌词上报连接断开不自动重连

A. mac_status_reporter.py 的 TCP 层已实现心跳 + 断线自动重连 + 指数退避。但 macOS 休眠唤醒后有时 Wi-Fi 重新关联需要 5~10 秒，这期间的重连尝试会失败。脚本内置退避机制最长等待 30 秒后再次尝试，**通常唤醒后 30 秒内会自动恢复 AUTH: OK**。如果超过 1 分钟仍未连接，手动重启服务：
```bash
launchctl kickstart -k gui/$UID/com.kooklyric.reporter
```
或者在 launchd plist 中将 `ThrottleInterval` 从 10 改到 3，让它在网络恢复时更快自动拉起。

---

## 8. 日志与数据目录

与 Windows / Linux 完全一致，所有运行时数据都在项目目录下：

| 路径 | 内容 | 清理策略 |
|------|------|----------|
| `logs/` | 每天一个 `.log` 文件 + launchd 输出文件 | 按需删除早于 7 天的，不会影响运行 |
| `data/img_temp/` | 识图/绘图临时图片（最多保留 48 小时） | 主循环每小时自动清理一次 |
| `data/memory/stm/` | 短时记忆 JSON（每频道一个文件） | 通常不需要清理；数据异常可单独删除对应 json |
| `data/memory/long/` | 长时记忆 Markdown（可人工编辑） | 按需整理，删除后机器人失去对应日期长期记忆 |

备份建议：每周备份一次 `config/` 目录和 `data/memory/long/` 目录到 iCloud Drive / Time Machine / 外置存储。
