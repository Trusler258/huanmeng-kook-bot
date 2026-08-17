# huanmeng-kook-bot

> A highly customizable, LLM-powered group chat bot for the KOOK voice & text platform. Built on `khl.py` with native KMarkdown support, cross-platform real-time lyric synchronization, multi-modal image recognition, and a modular plugin system. The default persona is only an example; the entire character, reply style, and permissions are fully user-defined.

<p align="center">
  <b>Powered by DeepSeek API · Modular Python Architecture</b>
</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-green" /></a>
  <a href="#"><img alt="Version" src="https://img.shields.io/badge/version-v2.0.2-8B5CF6" /></a>
  <a href="https://github.com/khl-projects-dev/khl.py"><img alt="Adapter" src="https://img.shields.io/badge/adapter-khl.py%20%28KOOK%29-6C5CE7" /></a>
  <a href="https://platform.deepseek.com/"><img alt="LLM Provider" src="https://img.shields.io/badge/LLM-DeepSeek%20API-00BFFF" /></a>
</p>

---

## DISCLAIMER

This project is an **UNOFFICIAL, community-developed third-party bot** for the KOOK platform (https://www.kookapp.cn/).

- It is NOT affiliated with, endorsed by, or sponsored by KOOK, Beijing Chuangxiang Technology Co., Ltd., or any of their partners.
- "KOOK" and related trademarks are the property of their respective owners.
- This project uses the open-source community SDK `khl.py` (https://github.com/khl-projects-dev/khl.py) to communicate with KOOK's public Bot API. It does not use any internal or proprietary interfaces.
- The default bot name and persona in example configs are purely demonstration placeholders. Replace them with your own when deploying.

---

## FEATURES

| Category | Feature | Description |
|----------|---------|-------------|
| **Core Chat** | LLM multi-turn dialogue | System prompt assembly, conversation history management, function calling, multi-sentence streaming replies |
| **Three-Tier Memory** | Instant / Short-term / Long-term | In-context rolling window + JSON rolling buffer (30 entries) + template-compressed Markdown permanent storage with auto-rollover |
| **Favorability System** | 101-level attitude scaling | -100 to +100 per user/chat; auto-reply tone changes, ignore-list auto-enforce at -100 |
| **Auto Web Search** | Triggered intent + DuckDuckGo | Keyword-based trigger detection, optional dual-path (real-time / standard), results injected into LLM context |
| **Visual Recognition** | Native KOOK attachments + quoted images | Sync-wait for image description on reply-worthy messages; recent-image reference buffer for follow-up questions |
| **Real-time Lyric Sync** | Cross-platform (Windows / macOS) + v6.80 strict sync | SMTC (Windows 1809+) / Music.app + Spotify (macOS) native media polling; 3 lyric sources (LRCLIB precise+fuzzy, QQ Music, NetEase Cloud), translation-prioritized scoring; 5-point progress filter, TIMER + tick dual-path delivery, 3-sentence fast-refill tolerance, deque event buffer for zero-loss rapid lines |
| **Games** | Gomoku + Chinese Chess | Full rule sets, LLM AI opponents, game board rendering to cards, ELO tracking, undo/history replay |
| **Rhythm Games** | TUF profile search | TUF (音游) gear lookup, Phigros song database / best scores, direct download links for chart files |
| **Utilities** | Weather / Express / WHOIS / NASA | HTML card rendering via Playwright; 7-day forecasts, express tracking, domain and IP lookups, NASA astronomy picture of the day |
| **Language** | 6-way translation | Chinese / English / Japanese / Korean / French / German bidirectional |
| **Reminders** | Relative + absolute time | Natural-language parsing ("30分钟后"/"明天 14:30"), KOOK native countdown cards with `__COUNTDOWN__` placeholders |
| **Audio** | TTS via Edge | Multiple voices per locale, card voice list |
| **Group Stats** | Daily / weekly reports | Per-channel ranking for message volume, users, peak hours; auto-scheduled push to subscribed channels at 00:01 |
| **Anti-Abuse** | Spam guard + ignore list | Rate-limiting, mute duration per group, temporary user ignore with TTL |
| **Admin Tools** | Config hot-reload + permission roles | Runtime TOML get/set without restart; admin / operator / friend / member four-tier permissions; per-channel and per-user allowlists + blocklists |
| **Card System** | 12 KOOK modules, full KMarkdown syntax | Header / section / divider / context / action-group / image-group / container / file / audio / video / countdown / invite — all supported, with plain-text / kmarkdown / image / button elements |
| **System Monitor** | PC state via TCP reporter | CPU / RAM / GPU / disk / network cards, full process list, playing song & per-line lyric injection (separate client script required) |

---

## COMPLETE COMMAND REFERENCE

Commands are triggered with the `.` prefix in text channels. In private chats no `@` is needed; in group chats either `@` the bot first or set `at_only = false` per channel in `adapter_config.toml`.

```
.help [cmd]             Help menu (image card if generated; fallback KOOK card)
.ping                   Online check (bot will respond)
.info                   Runtime status: system, uptime, memory, disk, CPU, model list
.cost                   Today / total token consumption + cost estimate
.tokens <text>          Token counter + cost estimate for given text
.balance                DeepSeek / provider API balance

--- Search & Knowledge ---
.s / .search <kw>       Web search via DuckDuckGo, injected into next reply
.read <URL>             Fetch and summarize a public web page
.whois <domain|IP>      WHOIS / RDAP lookup
.kook_user_id [@人]      Resolve KOOK user numeric ID
.kook_channel_id         Get current text channel numeric ID

--- Chat Persona & Data ---
.favlist                Favorability ranking for current channel / DM
.luck                   Daily fortune (1-100, stable per user per day)
.抽 / .chou A B C       Random choice between N space- or comma-separated options
.memory [scope] [kw]    Memory inspection: working / short / long / search <keyword>
.preset ...             (Admin) Runtime system-prompt injection / clear
.reset_fav              (Admin) Wipe all favorability data

--- Media & Creation ---
.img [prompt]           Random image generation (agnes module)
.img18 [prompt]         NSFW image generation (age-restricted channels only)
.img2video ...          Image-to-video pipeline (agnes module)
.voice [voice] <text>   Edge TTS synthesis; .voice list lists available voices
.write_code <spec>      Code generation from natural-language spec
.analyze [file]         Minecraft / generic crash-report analyzer (drag logs then run)

--- Games ---
.wzq ...                Gomoku: duel @user / accept / decline / ai <level> / undo / surrender / board / status / history
.xq / cmd_xq ...        Chinese Chess: duel / ai / move / board / undo / surrender

--- Rhythm Games ---
.tufsearch <song>       TUF chart search + details + direct download
.pgr ...                Phigros: login / me / top / song / new (profile integration)
.nasa [YYYY-MM-DD]      NASA Astronomy Picture of the Day

--- Utilities ---
.weather <city>         7-day forecast (HTML card)
.box / .快递 <num>      Express parcel tracking (HTML card)
.tr / .translate <lang> <text>  Translation: en/zh/jp/kr/fr/de (or full locale names)
.countdown ...          Countdown: add YYYY-MM-DD <name> / list / del <index>
.remind ...             Reminder: relative ("30分钟后") / absolute ("明天14:30"); bot @ you when due
.up / .update_info [v]  Changelog viewer: latest / vX.Y.Z / all (HTML card rendered)
.md / .card ...         KMarkdown / card rendering utilities

--- Group Management ---
.wdsj [channel]         Daily / weekly chat stats push
.ignore <userID>        Temporarily ignore user (auto-expires)
.unignore <userID>      Remove from ignore list
.ignore list            Show active ignore entries

--- Admin / System ---
.owner ...              (Admin) Config management: list <bot|adapter|roles> / get <path> / set <path> <val> / data get/set/reset
.reload                 (Admin) Hot-reload all TOML + .env without restarting process
.restart                (Admin) Graceful bot restart (SIGUSR1 on Unix)
.lyric [on|off|query]   Lyric delivery per-channel toggle / force query
.sys [card]             Pull PC hardware status from connected reporter client; .sys card renders a Playwright image card
```

---

## QUICK START

If you are deploying for the first time, please read the full platform-specific guide in `docs/`:

- Windows: [docs/deploy-windows.md](docs/deploy-windows.md)
- Linux (server recommended): [docs/deploy-linux.md](docs/deploy-linux.md)
- macOS: [docs/deploy-macos.md](docs/deploy-macos.md)

### Prerequisites

- Python 3.10, 3.11, or 3.12
- A KOOK account with server (guild) owner or admin permissions
- A KOOK Bot token (create one at https://developer.kookapp.cn/bot)
- At least one LLM API key compatible with the OpenAI chat-completions protocol (DeepSeek recommended; SiliconFlow, ZhiPu, etc. also supported)
- Optional: ZhiPu (GLM-4V) API key for image recognition

### Step 1: Clone & install dependencies

```bash
git clone <your-fork-url> huanmeng-kook-bot
cd huanmeng-kook-bot

# Core dependencies
pip install -r requirements.txt

# Optional, Windows only, required for SMTC lyric & system reporter
pip install pywin32 psutil "winrt-Windows.Media.Control" "winrt-Windows.Storage.Streams"

# Optional, required for ALL HTML card rendering
PLAYWRIGHT_DOWNLOAD_HOST=https://mirrors.cloud.tencent.com/playwright/ \
  python -m playwright install chromium
```

China users: prepend `-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com` to all `pip install` lines for faster downloads.

### Step 2: Initialize configuration

```bash
cp config/example.env              config/.env
cp config/example.bot_config.toml  config/bot_config.toml
cp config/example.adapter_config.toml config/adapter_config.toml
cp config/example.roles.toml       config/roles.toml
```

Edit the following **mandatory fields** before starting the bot:

| File | Field | Description |
|------|-------|-------------|
| `config/.env` | `DEEPSEEK_KEY`, `ZHIPU_KEY` (if using images) | LLM and vision provider API keys; additional providers follow the pattern `{NAME}_KEY` + `{NAME}_URL` |
| `config/bot_config.toml` | `[kook] token` | KOOK Bot token obtained from developer portal |
| `config/bot_config.toml` | `[bot] bot的名字` | Your bot's display name |
| `config/bot_config.toml` | `[bot] bot的qq号` (legacy field name; stores KOOK bot user ID) | Leave as `0` — it is automatically populated from the KOOK gateway on startup. Do NOT manually fill a QQ number here; this project has no relation to QQ / Tencent. |
| `config/bot_config.toml` | `[bot] admin_qq` (legacy field name; stores KOOK numeric user ID) | The KOOK user ID of the **bot owner / super-admin** — this user receives the `[admin]` role tag automatically. See note below on legacy QQ naming. |
| `config/roles.toml` | `admin_qq`, `op_qqs`, `qq_name_map` (all legacy QQ field names; store KOOK numeric IDs) | `admin_qq` = KOOK user ID of the super-admin (must match `[bot] admin_qq` above); `op_qqs` = KOOK user ID array of global operators; `qq_name_map` = KOOK user ID to display-name mapping dictionary. All keys are legacy-named and have nothing to do with Tencent QQ. |
| `config/adapter_config.toml` | `[kook] group_list` | Leave empty to allow ALL channels you invite the bot into; fill with numeric channel IDs to enforce a whitelist |
| `config/adapter_config.toml` | `[kook] enable_private` | Allow / disallow direct-message conversations |

### Step 3: Start the bot

```bash
# Foreground (for testing)
python main.py

# Debug mode (verbose logs)
python main.py --debug
```

On Linux, you almost always want a systemd unit. See `docs/deploy-linux.md` for the complete service definition.

### Step 4: Optional — install the lyric / PC status reporter

The real-time lyric push and `.sys` hardware-status commands require a **separate client-side script** running on the user's machine (Windows or macOS). Bot server-side only listens on TCP ports **62002** (status) and **62003** (TTS); the client actively connects to you.

Environment variables required by the reporter client:

```
BOT_SERVER   = hostname or public IP of the machine running this bot
BOT_PC_PORTS = 62002,62003            (comma-separated list, must match server)
BOT_PC_KEY   = <shared secret string> (must match BOT_PC_KEY env on server)
```

Full reporter installation instructions per platform are in:
- Windows: [docs/deploy-windows.md#pc-status--lyric-reporter](docs/deploy-windows.md#pc-status--lyric-reporter)
- macOS: [docs/deploy-macos.md#mac-status--lyric-reporter](docs/deploy-macos.md#mac-status--lyric-reporter)

---

## PROJECT STRUCTURE

```
huanmeng-kook-bot/
├── main.py                     Entry: arg parse, signal handlers, asyncio loop
├── bot.py                      HuanmengBot class: initialize khl client, 14-step pipeline, TCP servers, scheduled tasks
├── requirements.txt            Core Python dependency pinning (khl.py, OpenAI SDK, Playwright, ddgs, toml, dotenv)
│
├── config/
│   ├── example.env             Provider API keys (DEEPSEEK_KEY / ZHIPU_KEY / custom providers)
│   ├── example.bot_config.toml KOOK token, bot identity, LLM models, persona, search triggers, daily-report channels
│   ├── example.adapter_config.toml  KOOK connection params, per-channel allowlists, per-channel override flags (at_only / reply_threshold)
│   ├── example.roles.toml      Admin / operator ID lists, friendly nickname mappings
│   └── persona_data.json       Per-chat persona override store (optional)
│
├── core/
│   ├── pipeline.py             14-step message processing pipeline (see below)
│   ├── dispatcher.py           KOOK event router: attachments, quotes, mentions, dual-path image recognition
│   ├── config.py               TOML + env loader, hot-reload handler, role resolution, persona assembly
│   ├── context_manager.py      In-memory per-channel context ring buffer + STM rollover
│   ├── logger.py               Colored structured console logger with smart discard on backpressure
│   └── tools.py                Built-in callable tool registry (.info/.sys/.cost etc.)
│
├── services/                   Outbound service wrappers
│   ├── llm.py                  Provider-agnostic OpenAI-completions router, prefix-cache hints, multi-sentence parsing, function calling loop
│   ├── sender.py               KOOK message sender: group / private / image asset upload / native card serialization
│   ├── image_api.py            ZhiPu GLM-4V wrapper, per-channel recent-image buffer
│   ├── pc_status.py            TCP server (port 62002): AUTH handshake, JSON line parser, hardware state cache, lyric delivery ACK
│   └── tts.py                  TCP server (port 62003): Edge TTS synthesis request handler
│
├── modules/                    35+ plugin modules — each registers commands
│   ├── commands.py             Central command router + 40+ cmd_* handlers + HTML help card generator
│   ├── judge.py                Three-tier reply judge (keyword fast-path -> cheap LLM classifier -> precise LLM)
│   ├── fav.py / stm.py / memory.py  Favorability store, short-term rolling JSON, long-term Markdown archive
│   ├── search.py / web_search.py / local_search.py  Intent detection + DuckDuckGo + chat-history search
│   ├── weather.py / remind.py / holiday.py / earthquake.py
│   ├── wzq.py (Gomoku) / chinese_chess.py / tuf_commands.py / pgr.py / nasa.py
│   ├── spam_guard.py / ignore_users.py / op.py / admin.py
│   ├── agnes.py (img/img18/img2video) / voice.py / changelog.py
│   ├── wdsj.py (daily stats) / error_report.py (Minecraft log analyzer) / face_lib.py
│   ├── auto_update.py / preset.py / changelog.py / gh.py / whois_lookup.py / chinese_chess/
│   └── __init__.py
│
├── skills/                     10 system-prompt skill files, assembled at startup
│   ├── 01_bot_identity.md
│   ├── 02_persona_lock.md
│   ├── 03_group_format.md / 04_private_format.md
│   ├── 05_favorability_scale.md
│   ├── 06_fav_tiers.md
│   ├── 07_anti_repeat.md
│   ├── 08_command_tools.md
│   ├── 09_architecture_guide.md
│   └── 10_available_functions.md
│
├── scripts/                    Client-side companion tools (run END-USERS machines, NOT the server)
│   ├── pc_status_reporter.py   v6.80 Windows SMTC status + lyric sync reporter (Windows 1809+)
│   └── mac_status_reporter.py  v1.20 macOS Music.app + Spotify AppleScript status + lyric sync reporter
│
├── utils/                      Language formatting, KMarkdown sanitizers, card builders, LRC lyric parsers, common helpers
├── data/
│   ├── templates/              HTML templates for Playwright card rendering (console.html, weather, express, changelog, system info)
│   ├── wdsj_help.md
│   ├── architecture.mermaid    Architecture diagram (optional)
│   └── img_temp/               Temporary image store (auto-created, should be gitignored)
│
├── docs/
│   ├── deploy-windows.md       Full Windows deployment (bot + reporter)
│   ├── deploy-linux.md         Full Linux server deployment (systemd + firewall)
│   └── deploy-macos.md         Full macOS deployment (launchd + AppleScript permissions)
│
├── .gitignore
├── LICENSE
└── README.md
```

---

## MESSAGE PROCESSING PIPELINE (14 STEPS)

Every text message entering the bot passes through the following 14 stages. Stages 1-8 are pre-flight; stages 9-14 execute only if a reply is required.

```
KOOK raw event → dispatcher (attachments + quote extraction, image-recognize sync/async routing)
       │
       ▼
  1. Favorability auto-registration: first time a user speaks, ensure they have fav entry initialized
  2. Invisible-char cleanup: strip ZWSP / RTL / Bidi overrides that break keyword detection and LLM output
  3. Admin preset injection: messages containing {{...}} are parsed as runtime system-prompt overrides
  4. Quoted-message injection: text from the message being replied-to is prepended to context
  5. Context + short-term memory write: role tag, display name, favorability value appended to rolling buffers
  6. Hard ignore: users at fav <= -100 are discarded BEFORE any further processing
  7. Command interception: messages starting with "." are routed to modules/commands.py; pipeline halts
  8. Context commit: only non-command, plain-text messages are written to LLM context (avoids pollution)
       │
       └── Reply not required? Pipeline halts here.
       │
       ▼
  9. Reply judge: three tiers — @mentioned fast-path → keyword heuristics → cheap LLM classifier → precise LLM if still ambiguous
 10. Per-user ignore / anti-spam enforcement: TTL-based ignore list and per-channel rate limits
 11. Context assembly: memory retrieval, current time/holiday, active presets, image-description buffer, at-list, optional architecture context
 12. Auto-search: auto_search_if_needed scans the assembled context; runs DuckDuckGo in parallel if required
 13. LLM generation: generate_multi_reply_with_tools — multi-sentence output with function-calling tool loop
 14. Post-process + write-back: sentence cleanup / dedupe / @mention resolution / tool-call handling / favorability update / short-term rollover / long-term memory commit / lyric delivery / KOOK card formatting / send
```

Per-channel worker isolation: a slow `.sys card` render (Playwright Chromium subprocess, ~2 s) will never block another channel's `.ping`.

---

## CONFIGURATION & FEATURE TOGGLES

All configuration lives under `config/` and supports **hot reload** via `.reload` (admin) or POSIX `SIGUSR1`.

### Per-channel granularity (`adapter_config.toml[group_settings.<channelId>]`)

| Key | Default | Meaning |
|-----|---------|---------|
| `at_only` | true | If true, group chat only replies when explicitly @'d. Setting to false activates the three-tier reply judge. |
| `reply_threshold` | inherited from `[bot] reply_兴趣` | 0-10, higher = bot replies less often to passive chatter |
| `enabled` | true | Channel-level master switch |
| `daily_stats` | false | Auto-push daily chat ranking to this channel at 00:01 |

### Module-level toggles

Many modules can be disabled entirely by removing their import from `modules/__init__.py` or via their own internal TOML flags (see each module's docstring). Missing optional modules degrade gracefully with clear `ImportError` log lines instead of crashing the bot.

---

## CUSTOM PERSONA

The bot's character is defined in `config/bot_config.toml` under the `[personality]` table. It is assembled verbatim into the system prompt at startup, then merged with skill files. **All three fields are free-form text; define any character you want.**

```toml
[personality]
personality_core = """
Core personality, speech style, behavioral ground rules.
This is the ONLY place you need to define who the bot is. Examples:
- a calm senior engineer explaining CS concepts
- a bookshop owner with encyclopedic literary knowledge
- an excitable high-school student who loves rhythm games
"""

personality_side = """
Optional: subtle secondary traits, quirks, situational modifiers.
E.g. "gets talkative when someone mentions old video games"
     "hates rainy days and complains gently if the weather module reports rain"
"""

identity = """
Canonical identity block, referenced by role-tag resolution in the pipeline:
- Name, apparent age, speaking register
- Relation to role tags: [admin], [op], [friend], [member]
- Hard non-negotiable rules: privacy, safety, anti-abuse
"""
```

Favorability scaling is **orthogonal to persona**: regardless of what character you define, the -100 to +100 favorability value will adjust tone and reply length according to the 7-tier scale in `skills/05_favorability_scale.md` and `skills/06_fav_tiers.md`. You can override those scale files too for full control.

---

## REAL-TIME LYRIC SYNC SYSTEM

This is the project's most heavily engineered module and the primary differentiator over generic LLM bots. It was stabilized in pc_status_reporter v6.80 / mac_status_reporter v1.20 after 12 rounds of P0-level bug fixes and is production-grade.

### Architecture (three components)

```
 ┌──────────────────────────────┐        TCP (AUTH + JSON lines, ports 62002/62003)        ┌──────────────────────────────┐
 │  Client: pc_status_reporter  │ ─────────────────────────────────────────────────────▶ │  Server: services/pc_status  │
 │  (user's Windows / Mac)      │ ◀───────────────────────────────────────────────────── │  (bot, any platform)         │
 │                              │        CMD: LYRIC / CMD: HWINFO / CMD: SHOT_RESULT     │                              │
 └──────────────────────────────┘                                                          └──────────────────────────────┘
               │                                                                                       │
               │ native media query                                                                   │ lyric delivery via:
               ▼                                                                                       │   - services.sender to KOOK
  ┌────────────────────────────────────┐                                                            ▼
  │ Windows: SMTC (ISystemMediaTransportControls)   OR    macOS: AppleScript queries   →   pipeline lyric event queue
  │   Song title, artist, duration, position_ms, isPlaying, player name                    →  3 lyric databases searched in parallel
  └────────────────────────────────────┘                                                →  translation-prioritized scoring
                                                                                         →  5-point linear-predictor progress filter
                                                                                         →  TIMER (per-line, wall-clock) + tick (80 ms) dual-path emit
                                                                                         →  deque<maxlen=64> event buffer for 80 ms rapid consecutive lines
                                                                                         →  diff<=3 fast-refill tolerance to recover from temporary lag
```

### Lyric search strategy (client-side, reporter script)

Three sources, two-stage deadline gate:

- LRCLIB (precise + fuzzy, concurrent)
- NetEase Cloud Music
- QQ Music (exclusive cover art source; NetEase and Kugou covers removed per project policy)

Each request uses `requests` soft timeouts with **2 retries, constant 1.0 s backoff, no circuit breaker**. First-success-or-best-score is returned before T1 = 9 s (stage 1) / T2 = 12 s (stage 2).

### Strict sync algorithm

The reporter process does NOT trust coarse 1-second progress updates. It fuses player-reported position with a 5-point sliding-window linear predictor, clamps the effective playback rate to `[0.90, 1.10]`, and emits each lyric line via two independent paths:

1. **80 ms tick loop** — coarse, always catches up, tolerant of everything
2. **Per-line `threading.Timer`** — precise, fires at `LRC_timestamp + OFFSET` (default OFFSET = 1500 ms, tune per network)

If a precise timer fires up to 3 lines ahead of last-sent index (common after transient progress-stall events), lines 1..N-1 are rapidly refilled through the deque buffer before emitting the timed one exactly on schedule. The result is sub-100 ms drift for normal playback and tolerance of the native 1 Hz SMTC / AppleScript update cadence.

### Debugging

Both reporter scripts support `_LYRIC_SYNC_LOG = True` at the top of the file, which logs every decision, drift value, refill action, and gap analysis inline per lyric line. This is the primary troubleshooting tool and should be the first thing enabled when users report "stuttering lyrics" or "lines skipped / delayed".

---

## THREE-TIER MEMORY SYSTEM

| Tier | Store | Capacity | Rollover | Scope |
|------|-------|----------|----------|-------|
| Instant (working) | RAM: `context_manager.ContextManager` ring buffer | `bot的消息记录长度` (default 100 messages) per chat | FIFO, dropped on process exit | Per channel / per DM |
| Short-term | JSON file under `data/memory/stm/` | 30 entries, rolling | Auto-compressed into long-term when the 31st arrives | Per channel / per DM |
| Long-term | Markdown file under `data/memory/long/` + optional FAISS semantic index | Unlimited, template-compressed, zero-hallucination | Manual or triggered by keywords | Global, can be searched cross-channel |

Memory retrieval at stage 11 of the pipeline uses a hybrid keyword + semantic scorer that always pulls at least the 3 most relevant long-term memories plus, when available, a short chat-history recap summarizing the last few hours.

---

## ADVANCED CONFIGURATION

### System prompt assembly order

At every LLM call the full system prompt is assembled by concatenating, in order:

```
1. [personality] personality_core  (bot_config.toml)
2. Skill files 01-10               (skills/*.md, assembled once per process start, cached)
3. Per-chat active preset          (modules/preset, runtime override)
4. Favorability tier blurb         (based on current user fav value)
5. Self-awareness blurb            (version, bot name, loaded models, runtime flags)
6. Architecture overview           (only injected if user question matches specific keywords: "版本", "架构", "能做什么", etc.)
```

Steps 1-2 are identical across calls and benefit from the DeepSeek provider's **prefix cache** with observed ~89% hit rates on typical workloads, reducing both latency and billed tokens by a comparable margin.

### Provider load balancing / redundancy

Every model slot in `[model.*]` sections of `bot_config.toml` has an independent `provider` key, pointing to the prefix of `{PROVIDER}_KEY` + `{PROVIDER}_URL` environment variables in `config/.env`. This lets you freely mix:

- reply / judge / utility models → all DeepSeek `deepseek-chat` (default, cheap and fast)
- picture / OCR model → ZhiPu `glm-4v-flash` (multimodal)
- optional fallbacks via SiliconFlow / other OpenAI-compatible endpoints

Failover behavior: `services/llm.py` catches all HTTP-level failures and automatically falls through to the next provider with a matching role tag if `_FAILOVER_PROVIDERS` is configured there.

---

## CONTRIBUTING

Pull requests and issues are welcome. Before submitting a PR please ensure:

1. All new modules register their commands in the `commands._CMD_FEATURES` registration table so `.help` picks them up.
2. If your module is optional (requires extra PyPI dependencies not in `requirements.txt`), wrap its imports in `try/except ImportError` and log a single informative warning at startup — never hard-crash the bot when optional deps are missing.
3. Keep persona-related text out of hard-coded Python strings. Put it in `config/*.toml`, `skills/*.md`, or `utils/format_lang`-driven i18n tables so users can customize without editing code.
4. For lyric-sync changes, test against both the TIMER and tick paths; verify `_LYRIC_SYNC_LOG=True` shows no DROP events larger than diff = 3 and no out-of-order deliveries.

---

## LICENSE

MIT License. See [LICENSE](LICENSE) for the full text.

Portions of the KOOK card serializer, LRC lyric parser, and Playwright HTML-card renderer carry their own MIT-compatible licenses; see inline source headers for attribution.

---

## RELATED PROJECTS

- `https://github.com/khl-projects-dev/khl.py` — Community KOOK Bot SDK used by this project
- `https://platform.deepseek.com/` — Recommended LLM API provider
- `https://open.bigmodel.cn/` — Recommended multimodal (image-recognition) API provider
- `https://developer.kookapp.cn/` — KOOK official developer portal, where you create your Bot token
- `https://github.com/microsoft/playwright-python` — Headless browser engine used for all HTML card rendering
