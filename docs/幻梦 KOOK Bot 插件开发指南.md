# 幻梦 KOOK Bot 插件开发指南

> 适用版本：KOOK Bot 2.0+（Phase 13/14 插件系统）
> 插件通过 `PluginContext`（公开 API）与 Core 交互，**禁止**直接 import Core 内部实现、直接访问数据库或修改内部 Runtime 对象。

---

## 1. 插件是什么

插件是一份自包含的扩展，能在不修改机器人核心源码的前提下添加能力，例如：新指令、定时任务、事件订阅、自动回复、小游戏、规则与简单自动化。

**隔离与安全原则：**

- 插件只能通过 `PluginContext` 访问公开能力：消息、记忆、事件、定时器、命令注册、配置读取。
- 不直接暴露数据库、Core 内部 Runtime、文件系统、网络、进程执行。
- 单个插件崩溃不影响 Core 与其他插件（Graceful Degradation）。
- `reload` / `unload` 时会自动清理该插件的事件订阅、定时器与命令注册，防止热更新后重复执行。

---

## 2. 目录结构

插件放在 `plugins/` 目录下，每个插件一个独立文件夹：

```
plugins/
├── echo/                    # 插件名（与 manifest.name 一致，推荐）
│   ├── manifest.json        # 元数据声明，必需
│   └── main.py              # entrypoint（Python 插件）
└── greet/
    ├── manifest.json
    └── main.lua             # entrypoint（Lua 插件，实验性）
```

> **重要**：`plugins/` 已加入 `.update` 的跳过列表，**不会**随自动更新同步。新增/修改插件需要手动上传到服务器 `/root/kook_bot/plugins/<插件名>/`，然后重启 `kook-bot.service` 才能生效。这样可以避免插件被 `.update` 覆盖或误同步。

---

## 3. manifest.json

每个插件都必须有 `manifest.json`。必需字段是 `name`、`version`、`runtime`、`entrypoint`。

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | ✅ | 插件名，只允许字母、数字、`_`、`-` |
| `version` | ✅ | 版本号，如 `1.0.0` |
| `runtime` | ✅ | `python` 或 `lua`（小写） |
| `entrypoint` | ✅ | 入口文件名，默认 `main.py` |
| `description` | ❌ | 描述 |
| `author` | ❌ | 作者 |
| `dependencies` | ❌ | 依赖列表（预留） |
| `permissions` | ❌ | 声明的权限列表，如 `["message.read", "message.send"]` |
| `config` | ❌ | 静态配置字典，可通过 `ctx.config(key, default)` 读取 |

最小 Python 插件 manifest：

```json
{
  "name": "hello",
  "version": "0.1.0",
  "runtime": "python",
  "entrypoint": "main.py",
  "description": "示例插件",
  "author": "me",
  "permissions": ["message.read", "message.send"],
  "config": {
    "greeting": "你好"
  }
}
```

---

## 4. Python 插件

### 4.1 最小可运行示例

```python
# plugins/hello/main.py
class Plugin:
    """插件主类：CLF 识别名为 Plugin，或含 on_load+on_unload 钩子的类。"""

    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        # 在 on_load 里注册命令能力
        self.ctx.capability.register_command(
            name="hello",                 # 用户输入 .hello 触发
            description="说你好：.hello [名字]",
            handler=self._handle_hello,   # async (msg) -> str|None
        )

    async def _handle_hello(self, msg):
        name = " ".join(str(a) for a in (msg.get("args") or [])).strip()
        greeting = self.ctx.config("greeting", "你好")
        return f"{greeting}，{name or '世界'}！"
```

### 4.2 生命周期钩子

创建插件实例后，`PluginManager` 依次执行以下钩子（钩子可以是普通函数或 `async` 函数）：

| 钩子 | 时机 | 用途 |
|------|------|------|
| `__init__(self, ctx)` | 实例化 | 保存 `ctx` 引用 |
| `on_load()` | 文件加载后、启用前 | 注册命令、订阅事件、读配置 |
| `on_enable()` | 启用时 | 启动需随启用而开始的任务 |
| `on_disable()` | 禁用时 | 停止上一步启动的任务 |
| `on_unload()` | 卸载时（可选） | 收尾清理 |

生命周期完整链条：`discover → validate → load(__init__) → init(on_load) → enable(on_enable)`；卸载走 `disable(on_disable) / unload(on_unload)`。

> `on_load` 是推荐的初始化位置。`manager.load_all()` 启动时会对所有插件执行 `load → init → enable`。

### 4.3 命令 Handler

命令 handler 签名固定：`async (msg) -> str | None`。

- 返回**字符串**：该文本会被当作机器人的回复发送出去。
- 返回 `None`：表示插件已经自己通过 `ctx.message.send` 发送了内容，无需额外回复。

`msg` 是插件系统传入的消息对象，同时支持 `dict` 访问与属性访问，字段如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `args` | list | 命令参数拆成的列表（`/` 空格分隔） |
| `text` | str | 整条原始消息文本 |
| `author` | str/int | 消息作者 ID |
| `sender` | str | 发送者显示名 |
| `is_group` | bool | 是否群聊 |
| `chat_id` | str/int | 会话 ID（群号或私聊对方 ID） |

参数拆分的优先级由命令分发层决定；示例插件用 `msg.get("args")`（dict 风格）或 `msg.args`（属性风格）均可。

### 4.4 完整能力一览

`ctx` 提供以下能力（全部可选，按需使用）：

#### 命令注册 — `ctx.capability.register_command(...)`

```python
await self.ctx.capability.register_command(
    name="weather",                       # 指令名 .weather
    description="查询天气：.weather <城市>",
    handler=self._cmd_weather,            # 不传则仅声明名称
    permissions=["message.read", "message.send"],
)
```

#### 发送消息 — `ctx.message.send(...)`

```python
ok = await self.ctx.message.send("这是主动发送的内容", chat_id=chat_id, is_group=True)
```

> 适用主动推送场景（定时任务、事件触发等）。

#### 订阅 / 发布事件 — `ctx.event`

```python
# 订阅（装饰器方式）
@self.ctx.event.on("my.app.foo")
async def _on_foo(event):
    data = event.data or {}
    print(data.get("key"))

# 或手动订阅
self.ctx.event.subscribe("my.app.foo", self._on_foo)

# 发布（供其他插件/Core 消费）
await self.ctx.event.publish("my.app.foo", {"key": "value"})
```

内置已发布事件（常量定义在 `core/eventbus.py`）：

| 事件常量 | 值 | 触发源 |
|------|------|------|
| `EVENT_PLUGIN_LOADED` | `plugin.loaded` | 插件启用时发布 |
| `EVENT_PLUGIN_UNLOADED` | `plugin.unloaded` | 插件卸载时发布 |
| `EVENT_PLUGIN_ERROR` | `plugin.error` | 插件出错时发布 |
| `EVENT_TASK_*` | `task.created/completed` | Long Task Runtime |
| `EVENT_TOOL_*` | `tool.called/completed` | Tool Runtime |
| `EVENT_MEMORY_*` | `memory.created/updated` | Memory Engine |
| `EVENT_UPDATE_*` | `update.started/completed` | Update Engine |

> 插件间通过**自定义事件名**（如 `my.app.foo`）通信，避免与内置事件冲突。

#### 定时器 — `ctx.timer.every(...)`

```python
@self.ctx.timer.every(60)
async def _heartbeat():
    await self.ctx.message.send("心跳", chat_id=chat_id, is_group=True)
```

> 定时器在 `reload`/`unload` 时自动取消，避免热更新后重复执行。

#### 记忆 — `ctx.memory.remember / recall`

```python
# 异步写入记忆（不阻塞）
await self.ctx.memory.remember("重要知识片段", memory_type="knowledge", chat_id=chat_id)

# 检索记忆，返回结构化列表
rows = await self.ctx.memory.recall("关键词", chat_id=chat_id, limit=5)
```

#### 读取配置 — `ctx.config(...)`

```python
greeting = self.ctx.config("greeting", "你好")   # 读 manifest.config["greeting"]
```

#### 卸载清理 — `ctx.cleanup()`

`PluginManager` 会在 `disable`/`unload` 时自动调用 `ctx.cleanup()`，依次清理事件订阅、定时器、命令注册。一般无需手动调用。

---

## 5. 完整示例插件（Python）

```python
# plugins/timerbox/manifest.json
# {
#   "name": "timerbox", "version": "1.0.0", "runtime": "python",
#   "entrypoint": "main.py",
#   "description": "命令 + 定时 + 记忆 组合示例",
#   "permissions": ["message.read", "message.send"],
#   "config": {"prefix": "[timerbox] "}
# }
import time


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self._started = time.time()

    async def on_load(self):
        self.ctx.capability.register_command(
            name="uptime",
            description="运行了多久",
            handler=self._cmd_uptime,
        )
        # 每 60 秒把当前时间写入记忆（演示定时器+记忆）
        @self.ctx.timer.every(60)
        async def _note():
            await self.ctx.memory.remember(
                f"tick at {time.time():.0f}", memory_type="knowledge")
        self._note = _note  # 保留引用便于调用

    async def _cmd_uptime(self, msg):
        secs = int(time.time() - self._started)
        return f"{self.ctx.config('prefix', '')}已运行 {secs} 秒"
```

完工后：
1. 创建 `plugins/timerbox/` 目录与上述两个文件。
2. 上传到服务器 `/root/kook_bot/plugins/timerbox/`。
3. 重启服务：`systemctl restart kook-bot.service`。
4. KOOK 里发 `.plugin status`，`timerbox` 应为绿色（生效）。
5. 发 `.uptime` 验证返回时间。

---

## 6. Lua 插件（实验性）

Lua 插件与 Python 插件共用同一套公开 API（经 `bridge` 表暴露），主要用于命令、自动回复、小游戏与简单自动化。**需要服务器安装 `lupa`**，且沙箱移除了 `os / io / package / require / dofile / loadfile / debug` 等危险库，并有单次执行超时（默认 2s）、调用预算、定时器数量上限（默认 10 个）。

`plugins/greet/main.lua`（参考内置示例）：

```lua
-- 声明命令：.pgreet <名字>
bridge.command("pgreet", "问候：.pgreet <名字>")

function cmd_pgreet(msg)
    local name = msg.args and msg.args[1]
    if not name then
        return "用法：.pgreet <名字>"
    end
    return bridge.config("greeting", "你好") .. " " .. tostring(name)
end

-- 订阅业务事件，收到 ping 则发布 pong
bridge.on_event("greet.ping", function(e)
    bridge.publish("greet.pong", { pong = true })
end)
```

Lua 入口可用 DSL：

| 语句 | 作用 |
|------|------|
| `bridge.command("name", "描述")` | 声明命令，实现 `function cmd_name(msg) end` |
| `bridge.on_event("event.name", fn)` | 订阅事件 |
| `bridge.every(seconds, fn)` | 周期定时器 |
| `bridge.send(text, chat_id, is_group)` | 发送消息 |
| `bridge.remember(content, type)` | 异步写记忆 |
| `bridge.recall(query, limit)` | 检索记忆（返回数组） |
| `bridge.publish("event.name", data)` | 发布事件 |
| `bridge.config("key", default)` | 读配置 |

> Lua 命令的函数名固定为 `cmd_<指令名>`（如 `.pgreet` → `function cmd_pgreet(msg)`）。
> 命令由 Lua 沙箱执行（带超时），请保持函数体简洁。

---

## 7. 部署与生效流程

因为 `plugins/` 已被 `.update` 排除，插件走独立的部署路径：

```
本地写好插件
   │
   ▼
上传到服务器 /root/kook_bot/plugins/<插件名>/
   │
   ▼
systemctl restart kook-bot.service
   │
   ▼
KOOK 发 .plugin status 确认状态
   │
   ├── 🟢 生效 → 正常使用
   └── 🔴 未生效/错误 → 查看卡片上的错误信息并修复
```

**状态判定**（`.plugin status` 返回的卡片）：

| 状态 | 卡片颜色/标记 | 含义 |
|------|------|------|
| `enabled` 且无 error | 🟢 | 已生效 |
| `error` 或 error 非空 | 🔴 错误 | 加载/启用/运行出错，附错误信息 |
| `disabled` 等其它 | 🔴 未生效 | 已发现但未启用 |

---

## 8. 调试与最佳实践

- **插件出错只影响自己**：`PluginManager` 会把异常标记为 `error` 状态并在卡片上显示红色；Core 与其它插件不受影响。
- **命令 handler 保持返回字符串** 用于回复；主动推送时用 `ctx.message.send` 并返回 `None`。
- **异步优先**：钩子与 handler 写成 `async` 函数，避免阻塞事件循环。
- **定时器/事件订阅慎重**：它们会在 `reload`/`unload` 时被自动清理，但不要依赖这一点在长时间任务里无限累计。
- **配置集中管理**：把可调数值（前缀、问候语、间隔）放到 `manifest.config`，用 `ctx.config()` 读取，改配置无需改代码。
- **自定义事件注意命名空间**：用带插件前缀的名称（如 `myplugin.something`），避免与内置事件或其它插件撞名。
- **不要信任网络下行数据**：插件未直接开放网络/文件能力；如需访问外部接口，只能通过受控接口并在 manifest 声明。

---

## 9. 常见问题

**Q：`.plugin status` 显示红色"未找到 Plugin 类"？**
A：`main.py` 里缺少名为 `Plugin`（或同时含 `on_load` 与 `on_unload`）的类。

**Q：改插件后 `.update` 没同步？**
A：符合预期。`plugins/` 被 `.update` 排除，需要手动上传 + 重启。

**Q：Lua 插件加载报"Lua runtime 未就绪"？**
A：服务器缺少 `lupa`。安装 `pip install lupa` 后重启。

**Q：命令 handler 返回的是字符串但机器人没回复？**
A：确认 manifest 的 `runtime` 是 `python`、入口类命名为 `Plugin`、且 `.plugin status` 显示该插件为 🟢。字符串返回值由命令分发层转成回复发送。

**Q：如何让命令的参数带多个值（如 `.weather beijing today`）？**
A：`msg.args` 是按空格拆好的列表，直接 `msg.args[0]`、`msg.args[1]` 取即可；需要合并全部参数时用 `" ".join(str(a) for a in msg.get("args"))`。

---

## 10. 相关源码索引

| 模块 | 作用 |
|------|------|
| `core/plugin/manifest.py` | `manifest.json` 校验与 `PluginManifest` |
| `core/plugin/loader.py` | 目录扫描 `discover`、Python 模块 `load_module`、定位 `Plugin` 类 |
| `core/plugin/manager.py` | 生命周期管理、`get_plugin_manager()`、`health_all()` |
| `core/plugin/api.py` | `PluginContext` 与各能力实现 |
| `core/plugin/lua.py` | Lua 沙箱与 `bridge` DSL |
| `core/capability/registry.py` | 能力注册中心（命令/技能/工具） |
| `core/eventbus.py` | 事件总线与内置事件常量 |