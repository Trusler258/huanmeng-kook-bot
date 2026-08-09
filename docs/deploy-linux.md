# Linux 服务器部署指南

本指南覆盖 Ubuntu 20.04 LTS / Debian 11+ / Rocky Linux 9 / CentOS Stream 9 等主流发行版的全流程服务器部署：

1. 系统准备（Python 3.10+、依赖包、系统用户）
2. 代码拉取、虚拟环境与依赖安装
3. 配置初始化（4 份 config 模板）
4. Systemd 托管 + 开机自启 + 热重载
5. 防火墙与云安全组放行（歌词上报端口 62002/62003 TCP）
6. 日志查看、常见故障排查

服务器端部署**只负责运行机器人本体**。歌词同步上报脚本 (`pc_status_reporter.py` / `mac_status_reporter.py`) 必须运行在**用户本人听歌的 Windows 或 macOS 桌面机**上，主动连接服务器，不能部署在服务器。

---

## 0. 最低系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| OS | Ubuntu 20.04 LTS, Debian 11, Rocky 9, CentOS Stream 9 | Ubuntu 22.04 LTS (x86_64) |
| CPU | 1 核 | 2 核（Playwright 渲染卡片时会短时间占满单核） |
| RAM | 1 GB | 2 GB（Chromium 常驻约 400 MB + Python 进程约 300 MB） |
| 磁盘 | 5 GB 可用空间 | 10 GB SSD（日志 + 依赖包 + Playwright 缓存） |
| 网络 | 可访问 KOOK 官方网关（wss://gateway.kookapp.cn） + 你配置的 LLM API | 独立公网 IP，**入站端口 62002/62003 TCP 必须放行**（歌词上报连接用） |

### 0.1 发行版差异速查

| 操作 | Ubuntu / Debian (apt) | Rocky / CentOS Stream (dnf) |
|------|-----------------------|------------------------------|
| 更新包索引 | `sudo apt update` | `sudo dnf makecache` |
| 安装 Python + pip + venv | `sudo apt install -y python3 python3-pip python3-venv python3-dev git curl ca-certificates` | `sudo dnf install -y python3 python3-pip python3-devel git curl ca-certificates` |
| 安装 Playwright 运行时依赖（必须！） | `sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libxshmfence1 libgtk-3-0 libdbus-1-3 libx11-xcb1 libxcb1 libxcb-dri3-0 libxcb-present0 libxcb-sync1 libx11-6 libxext6 libxxf86vm1 libxfixes3 libxrender1 libgl1-mesa-glx` | `sudo dnf install -y nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage libXfixes libXrandr mesa-libgbm alsa-lib pango cairo at-spi2-core gtk3 dbus-libs libX11-xcb libxcb libX11 libXext libXxf86vm libXrender mesa-dri-drivers glibc-langpack-zh langpacks-zh_CN.UTF-8` |
| 防火墙 | `ufw` (默认) | `firewalld` (默认) |
| Systemd service path | `/etc/systemd/system/` | 相同 |

---

## 1. 系统准备与安全加固

始终使用非 root 用户运行机器人进程。以下所有命令除非特别说明，都在一个具有 `sudo` 权限的普通用户下执行（示例用户名：`botadmin`）。

### 1.1 创建专用系统用户（推荐）

```bash
sudo useradd --create-home --shell /bin/bash kookbot
sudo usermod -aG sudo kookbot
# 如果不希望给 sudo 权限，可以不加第二行，后续 sudo 操作切换到 root 或其他管理员账户执行
sudo su - kookbot
```

之后的步骤假设你已经以 `kookbot` 用户登录，home 目录为 `/home/kookbot`。

### 1.2 安装操作系统级依赖

按你的发行版执行上表 "安装 Python" 和 "安装 Playwright 运行时依赖" 两条命令。Chromium 在 headless 模式下需要大量共享库，缺少任何一个 Playwright 就会报 "Browser closed unexpectedly"，这两条安装命令必须完整执行。

---

## 2. 获取代码与创建虚拟环境

```bash
# 1. 克隆项目
cd ~
git clone <your-repo-url> ~/kook-lyric-bot
cd ~/kook-lyric-bot

# 2. 创建虚拟环境（只执行一次）
python3 -m venv .venv

# 3. 激活虚拟环境（每次登录 / 每次开新终端都必须执行一次！）
source .venv/bin/activate
# 激活后，命令行提示符会出现 (.venv) 前缀。此时 python 和 pip 都指向 venv 内的版本。

# 4. 升级 pip + setuptools + wheel
pip install --upgrade pip setuptools wheel
```

---

## 3. 安装 Python 依赖

中国大陆服务器请全程使用清华大学开源镜像站，海外服务器直接用官方源即可。

```bash
# === 仅中国大陆服务器：清华 PyPI 加速（任选其一，推荐第二种） ===
# 方式A：单次命令参数（每条 pip 都要加）
pip install -r requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple

# 方式B：永久写入 pip 配置（推荐，之后所有 pip install 自动走清华源）
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt

# === 海外服务器：直接安装 ===
# pip install -r requirements.txt
```

### 3.1 安装 Playwright + Chromium（卡片渲染必装）

Playwright 安装 Chromium 浏览器二进制大约 300 MB：

```bash
# 中国大陆服务器：腾讯云镜像加速
PLAYWRIGHT_DOWNLOAD_HOST=https://mirrors.cloud.tencent.com/playwright/ \
  python -m playwright install --with-deps chromium

# 如腾讯云镜像返回 404：改用 npmmirror
PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright/ \
  python -m playwright install --with-deps chromium

# 海外服务器：
# python -m playwright install --with-deps chromium
```

`--with-deps` 参数会自动通过 apt/dnf 再安装一遍系统级依赖，和你 1.2 步手动装的是同一份，不会冲突，推荐加上以防有遗漏。

### 3.2 验证安装

```bash
python -c "import khl, openai, playwright, toml, dotenv; print('Core deps: OK')"
python -m playwright install --dry-run chromium
```

两行都无 ImportError，且第二行显示 "Already downloaded" 即表示依赖已成功安装。

---

## 4. 初始化配置文件

```bash
cd ~/kook-lyric-bot

cp config/example.env              config/.env
cp config/example.bot_config.toml  config/bot_config.toml
cp config/example.adapter_config.toml config/adapter_config.toml
cp config/example.roles.toml       config/roles.toml

# 关键：将 .env 文件权限收紧为只有属主可读，防止其他系统用户偷看 API Key
chmod 600 config/.env
```

### 4.1 必填字段

用 `nano` 或 `vim` 分别编辑这 4 个文件。Windows 部署文档的 "必须修改的字段" 表格完全适用于 Linux，不再重复，仅强调几点服务器端特有的：

| 字段 | Linux 部署特别说明 |
|------|--------------------|
| `config/bot_config.toml` `[bot] bot的qq号` | 遗留字段名（旧 QQ 项目命名，当前 KOOK 项目与腾讯 QQ 无任何关系）。**保持默认值 0 即可**，启动后会自动从 KOOK 网关拉取机器人自身的 KOOK 用户 ID，绝对不要填入任何 QQ 号码。 |
| `config/bot_config.toml` `[bot] admin_qq` | 遗留字段名，实际存储 **KOOK 用户数字 ID**（超级管理员）。去 KOOK 客户端对自己头像右键「复制用户 ID」获取；或 Bot 上线后私聊机器人发 `.kook_user_id @你自己` 获取。 |
| `config/roles.toml` `admin_qq` / `op_qqs` / `qq_name_map` | 全部为遗留命名，与 QQ 无关，实际存储 KOOK 数字 ID。`admin_qq` 必须与 bot_config.toml 的 `[bot] admin_qq` 完全一致；`op_qqs` 是全局 OP 的 KOOK 用户 ID 数组；`qq_name_map` 是 KOOK 用户 ID 到显示昵称的映射字典。 |
| `config/.env` 中的 `BOT_PC_KEY` | **必须设置**。歌词上报客户端连接服务器时会发送 `AUTH <BOT_PC_KEY>`，两端必须完全一致。生成强随机密钥：`python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `config/bot_config.toml` `[bot] 调试模式` | 服务器上建议改成 `false`。调试模式日志极多，会让 `journalctl` 日志量暴增。 |
| `config/adapter_config.toml` `[kook] group_list` | 服务器生产环境**强烈建议填写白名单**。机器人一旦 Token 泄露，如果没有群白名单，任何人只要知道你的 Bot 的邀请链接都能把它拉进自己的服务器并触发 LLM 调用消耗你的 API Key 额度。 |

### 4.2 可选：环境变量覆盖配置项

所有 `config/.env` 里设置的变量，以及 `example.env` 列出的所有键，在 `bot.py` 启动时会被 `python-dotenv` 加载为进程环境变量，优先级高于任何默认值。你也可以在 Systemd unit 文件中用 `EnvironmentFile=/home/kookbot/kook-lyric-bot/config/.env` 统一注入（这是我们下面要采用的方式），避免把密钥直接写进 service 文件。

---

## 5. 前台启动验证（必做！）

在写 Systemd 服务之前，先在前台启动一次确认配置正确，无任何报错、消息通路可达：

```bash
cd ~/kook-lyric-bot
source .venv/bin/activate
python main.py --debug
```

正常启动末尾应看到：
```
[INFO] [bot] KOOK 客户端连接成功，当前账号：你的Bot名 (ID=xxxx)
[INFO] [bot] 共加载 xx 个指令模块
[INFO] [pc_status] PC 状态 TCP 接收端: 0.0.0.0:62002
[INFO] [tts] TTS TCP 接收端: 0.0.0.0:62003
```

在 KOOK 里测试：
1. 向机器人发 `@Bot .ping` → 有回复
2. 发 `@Bot .help` → 返回卡片
3. 发 `@Bot .sys` → 虽然此时还没有连上 PC 客户端，但应返回 "PC 客户端未连接，请先确保运行 pc_status_reporter.py" 的**提示文字**（这表示 TCP Server 已经正常在监听了）。

全部通过后按 `Ctrl+C` 优雅停止进程，进入下一步写 Systemd 服务。

---

## 6. Systemd 托管（生产环境标准做法）

### 6.1 创建 Systemd Unit 文件

用 sudo 权限编辑 `/etc/systemd/system/kook-lyric-bot.service`：

```ini
[Unit]
Description=KOOK Lyric Bot - LLM-powered group chat bot with realtime lyric sync
Documentation=https://github.com/<your-username>/<your-repo>
After=network-online.target
Wants=network-online.target

[Service]
# === 运行身份 ===
Type=simple
User=kookbot
Group=kookbot
UMask=0027

# === 工作目录与可执行文件 ===
WorkingDirectory=/home/kookbot/kook-lyric-bot
ExecStart=/home/kookbot/kook-lyric-bot/.venv/bin/python main.py
ExecReload=/usr/bin/env systemctl kill --signal=SIGUSR1 -s SIGUSR1 $MAINPID    # .reload: 热重载配置，不重启进程
ExecStop=/bin/kill -s SIGTERM $MAINPID                                          # 停止: 发送 SIGTERM，让 bot 内部 handler 正常收尾
Restart=always
RestartSec=5s
TimeoutStartSec=60
TimeoutStopSec=30

# === 环境变量注入：从 .env 文件读取，避免密钥写死在这里 ===
EnvironmentFile=/home/kookbot/kook-lyric-bot/config/.env

# === 安全加固（systemd 240+ 均支持，Ubuntu 20.04/22.04 默认可用） ===
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/kookbot/kook-lyric-bot /tmp /var/tmp
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictRealtime=yes
RestrictNamespaces=yes
MemoryDenyWriteExecute=yes

# 日志
StandardOutput=append:/home/kookbot/kook-lyric-bot/logs/systemd-stdout.log
StandardError=append:/home/kookbot/kook-lyric-bot/logs/systemd-stderr.log
SyslogIdentifier=kook-lyric-bot

[Install]
WantedBy=multi-user.target
```

**重要说明：**
1. `EnvironmentFile` 指向我们刚才 `chmod 600` 的 `.env` 文件。这样你在 service 文件里看不到任何明文密钥。
2. `ExecReload` 发送 `SIGUSR1`，Bot 的 `main.py` 已注册 handler，收到后会调用 `bot.handle_reload()` 热重载所有 TOML + env，不重启进程。
3. `ProtectSystem=strict` + `ProtectHome=read-only` 把整个文件系统默认只读，只在 `ReadWritePaths` 里列的目录有写权限，降低 RCE 提权风险。
4. 如果你需要 Bot 执行更高权限的服务器管理操作（例如 `/.restart` 重启其他 systemd 服务），需要去掉 `ProtectSystem=strict` 等安全加固行，或额外配置 Polkit 规则。

### 6.2 启动并验证 Systemd 单元

```bash
# 1. 让 systemd 重新读取新写的 unit 文件
sudo systemctl daemon-reload

# 2. 启动服务 + 设为开机自启
sudo systemctl enable --now kook-lyric-bot.service

# 3. 查看状态，必须显示 active (running)
sudo systemctl status kook-lyric-bot.service
```

典型输出如下，绿色 `active (running)` 表示成功：

```
● kook-lyric-bot.service - KOOK Lyric Bot
     Loaded: loaded (/etc/systemd/system/kook-lyric-bot.service; enabled; vendor preset: enabled)
     Active: active (running) since Mon 2026-08-10 14:05:11 CST; 22s ago
       Docs: https://github.com/...
   Main PID: 411281 (python)
      Tasks: 22 (limit: 9491)
     Memory: 389.1M
        CPU: 2.4s
     CGroup: /system.slice/kook-lyric-bot.service
             └─411281 /home/kookbot/kook-lyric-bot/.venv/bin/python main.py
```

### 6.3 日常管理命令

```bash
# 查看状态
sudo systemctl status kook-lyric-bot.service

# 启动 / 停止 / 重启
sudo systemctl start    kook-lyric-bot.service
sudo systemctl stop     kook-lyric-bot.service
sudo systemctl restart  kook-lyric-bot.service

# ★ 热重载配置（不重启进程，改了 bot_config.toml / adapter_config.toml / roles.toml 后执行）
sudo systemctl reload kook-lyric-bot.service

# 设置 / 取消开机自启
sudo systemctl enable  kook-lyric-bot.service
sudo systemctl disable kook-lyric-bot.service

# 查看日志（journalctl，推荐日常排查首选）
journalctl -u kook-lyric-bot.service -f          # 实时追日志（类似 tail -f）
journalctl -u kook-lyric-bot.service --since today   # 只看今天
journalctl -u kook-lyric-bot.service -n 200      # 只看最近 200 行
journalctl -u kook-lyric-bot.service -p err      # 只看错误级别

# 也可以直接看项目 logs/ 目录下的文件
tail -f ~/kook-lyric-bot/logs/systemd-stdout.log
tail -f ~/kook-lyric-bot/logs/systemd-stderr.log
```

---

## 7. 防火墙与云安全组放行（必做！）

服务器需要**入站 TCP 62002**（PC 状态/歌词上报）和 **入站 TCP 62003**（TTS 服务）。KOOK WebSocket 本身是 Bot 主动向外连接 443 端口，不需要任何入站规则。**只放需要的端口，别贪图方便开 0.0.0.0/0 all。**

### 7.1 系统层面防火墙

#### Ubuntu 默认 UFW

```bash
# 仅允许你的歌词上报客户端 IP 访问这两个端口（推荐！更安全）
#   用你家/办公室的公网出口 IPv4 替换下面的 x.x.x.x
sudo ufw allow proto tcp from x.x.x.x to any port 62002 comment "KOOK Lyric Reporter"
sudo ufw allow proto tcp from x.x.x.x to any port 62003 comment "KOOK TTS Reporter"

# 如果你在多个地点听歌，或 IP 不固定，只能暂时放宽为全局允许（不推荐）
# sudo ufw allow 62002/tcp comment "KOOK Lyric Reporter (any)"
# sudo ufw allow 62003/tcp comment "KOOK TTS Reporter (any)"

# 启用防火墙（如果之前没启用过）
sudo ufw enable
# 确认规则
sudo ufw status numbered
```

#### RHEL 系默认 Firewalld

```bash
# 临时放行（重启失效，用于先测试）
sudo firewall-cmd --add-port=62002/tcp
sudo firewall-cmd --add-port=62003/tcp

# 永久放行 + 重载生效
sudo firewall-cmd --permanent --add-port=62002/tcp --add-port=62003/tcp
sudo firewall-cmd --reload

# 查看
sudo firewall-cmd --list-all
```

### 7.2 云厂商控制台的安全组

**90% 的连接失败都栽在这里。** 云服务器的网络流量先经过云厂商安全组（外层），再到你系统内部的 ufw/firewalld（内层）。**两边都要放。**

| 云厂商 | 入口 |
|--------|------|
| 阿里云 | ECS 控制台 → 网络与安全 → 安全组 → 配置规则 → 入方向 |
| 腾讯云 | CVM 控制台 → 安全组 → 入站规则 |
| 华为云 | ECS 控制台 → 网络 → 安全组 → 入方向规则 |
| Hetzner / Vultr / DO | 各自 Cloud Firewall 页面 |

添加一条入方向规则：

```
协议类型: TCP
端口范围: 62002/62003
授权对象 (源): x.x.x.x/32  (强烈建议填你自己的公网 IP，不要 0.0.0.0/0)
策略: 允许
备注: KOOK Lyric Bot Reporter
```

### 7.3 端口连通性自测

在你要运行歌词上报脚本的 Windows 本机打开 PowerShell：

```powershell
# 62002 必须通
Test-NetConnection bot.example.com -Port 62002
# TcpTestSucceeded : True  ← 成功

# 62003 必须通
Test-NetConnection bot.example.com -Port 62003
# TcpTestSucceeded : True  ← 成功
```

任何一条显示 `False`，回到 7.1 / 7.2 重新检查两边防火墙配置。

### 7.4 （可选）Nginx 反代 + Let's Encrypt

一般不需要。Bot 本身不提供 HTTP(S) 服务。只有当你想把 `console.html`（系统监控控制台）通过域名公网访问时，才需要配 Nginx + HTTPS + Basic Auth。示例配置省略，如有需要参考 Playwright 渲染的任意 HTML 的通用 Nginx 静态站配置即可。

---

## 8. 备份与运维

### 8.1 必须定期备份的目录

| 路径 | 频率 | 大小 | 内容 |
|------|------|------|------|
| `~/kook-lyric-bot/config/` | 每日增量 | < 1 MB | 4 份 TOML 配置 + .env 密钥文件（最重要） |
| `~/kook-lyric-bot/data/memory/long/` | 每周 | 几十 MB | 长时记忆 Markdown |
| `~/kook-lyric-bot/logs/` | 每周 | 几百 MB | 日志（可选备份） |

简单本地 + 远程备份示例（crontab）：

```bash
# crontab -e -u kookbot
# 每天 04:00 备份 config 和长时记忆到 ~/backups，然后同步到远程存储（s3/rclone/另一台服务器）
0 4 * * * tar -czf ~/backups/kookbot-config-$(date +\%Y\%m\%d).tar.gz -C ~/kook-lyric-bot config data/memory/long && rclone copy ~/backups/ remote:backup-bucket/kookbot/ --max-age 24h
```

### 8.2 日志轮转

项目目录里的 `logs/systemd-stdout.log` / `stderr.log` 会无限增长。用 `logrotate` 管理（Ubuntu 默认已装）：

```
# /etc/logrotate.d/kook-lyric-bot  (root 创建)
/home/kookbot/kook-lyric-bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su kookbot kookbot
}
```

`copytruncate` 保证 systemd 继续往原 inode 写，无需重启 bot。

### 8.3 升级版本

```bash
cd ~/kook-lyric-bot
source .venv/bin/activate

# 拉新版本
git pull

# 重新装依赖（requirements.txt 可能变化）
pip install -r requirements.txt

# 数据库迁移 / 配置项变更检查
# （如有大版本升级，看 CHANGELOG.md 的 Breaking Changes）

# 先热重载试一下
sudo systemctl reload kook-lyric-bot.service
journalctl -u kook-lyric-bot.service -n 50 --since "5 minutes ago"
# 如果有报错，再彻底重启
sudo systemctl restart kook-lyric-bot.service
```

---

## 9. 常见问题 (FAQ)

### Q1：`systemctl status` 显示 activating (auto-restart)，一直循环启动

A. 99% 是启动即崩溃。看详细报错：

```bash
journalctl -u kook-lyric-bot.service -n 100 --no-pager
```

常见错误：
- `Permission denied: '/home/kookbot/kook-lyric-bot/config/.env'` → 检查 .env 的 owner 和 mode：`chown kookbot:kookbot ~/kook-lyric-bot/config/.env && chmod 600 ~/kook-lyric-bot/config/.env`
- `ModuleNotFoundError: No module named 'khl'` → 你用了系统 python 而不是 venv python。检查 ExecStart 必须是 `/home/kookbot/kook-lyric-bot/.venv/bin/python` 的绝对路径，不能只写 `python`。
- `KOOK WebSocket: 401 Unauthorized` → Bot Token 无效。去开发者门户重新复制一份。
- `OSError: [Errno 98] Address already in use` → 62002/62003 端口被别的进程占用了：`ss -tulpn | grep -E '6200(2|3)'` 找到 PID kill 掉。

### Q2：机器人 KOOK 能收到消息、能回复，但是 PC 歌词上报客户端连接不上（AUTH: FAIL 或连接超时）

A. 连接问题四层排查，从上到下：
1. **歌词客户端出口网络** → 能 `Test-NetConnection` 通 62002 吗？
2. **云安全组** → 加规则了吗？
3. **系统防火墙** (ufw/firewalld) → `sudo ufw status numbered`（或 firewalld）里能看到 62002/62003 吗？
4. **AUTH KEY 不匹配** → 同时开两边日志：
   - 服务器：`journalctl -u kook-lyric-bot.service -f | grep -i auth`
   - 客户端 pc_status_reporter.py：看 stderr 的 AUTH: OK / FAIL
   两边字符串肉眼对比，确保没有空格、换行、引号、中文标点全角问题。

### Q3：`.sys card` / 天气卡片 / 更新日志卡片渲染失败，日志报 Playwright Timeout

A. 常见于 1 GB 内存的小服务器。原因：
1. 内存不足，Chromium 被 OOM Killer 杀掉：`dmesg -T | grep oom-killer | tail` 看是否有 python/chromium 被查杀。解决：加 1 GB 虚拟内存 swap：
   ```bash
   sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap defaults 0 0' | sudo tee -a /etc/fstab
   ```
2. Playwright Chromium 安装不完整，缺少系统共享库：
   ```bash
   PLAYWRIGHT_DOWNLOAD_HOST=https://mirrors.cloud.tencent.com/playwright/ \
     python -m playwright install --with-deps chromium
   ```
3. 首次启动下载字体超时（中文卡片需要中文字体）：
   ```bash
   # Ubuntu/Debian 装开源中文字体
   sudo apt install -y fonts-noto-cjk fonts-noto-cjk-extra fonts-wqy-zenhei
   # Rocky/CentOS
   sudo dnf install -y google-noto-sans-cjk-fonts wqy-zenhei-fonts
   ```

### Q4：改了 bot_config.toml 后 `.reload` 没反应？

A. 检查 systemd unit 的 ExecReload 行是否正确。你可以手动发信号验证：
```bash
sudo kill -SIGUSR1 $(systemctl show -p MainPID --value kook-lyric-bot.service)
journalctl -u kook-lyric-bot.service -n 20 | tail
```
应看到 "[bot] 收到 SIGUSR1 信号，热重载配置..." 一行。没有说明 unit 写错了，`daemon-reload` 后再试。

### Q5：如何临时下线维护不被歌词客户端疯狂重试打爆日志？

A. 最简单：改 ufw/安全组临时拒绝 62002/62003 端口，客户端的 TCP connect 会直接失败，不会进入 AUTH 握手日志。维护窗口结束后重新放行规则。或者：
```bash
sudo ufw insert 1 deny proto tcp from any to any port 62002:62003 comment "Temporary maintenance window"
# 维护完成后 sudo ufw delete 1
```

---

## 10. 故障速查速记表

| 现象 | 首要排查 | 次要排查 |
|------|----------|----------|
| KOOK 无连接，journalctl 报 401/403 | Token 错了/过期 | 服务器能否直连 gateway.kookapp.cn:443 |
| 有连接但所有指令无响应 | group_list 白名单没加该频道 / admin ID 填错 | LLM 调用是否报 401 429 余额不足 |
| 歌词客户端连接超时 | 云安全组 + ufw 四层排查 | 服务器是否多网卡绑定了错 IP |
| 歌词 AUTH OK 但不推歌词 | 播放软件是否正确填充到 SMTC | _LYRIC_SYNC_LOG=True 抓详细日志 |
| 卡片渲染空白方块字 | 缺少中文字体包 | Playwright Chromium 共享库缺失 |
| .reload 不生效 | systemd ExecReload 写错 | 权限问题：User/Group 不匹配 |
