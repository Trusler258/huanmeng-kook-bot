# 幻梦 KOOK Bot 插件开发指南

> 适用版本：KOOK Bot 2.0+（Phase 13/14 插件系统）
> 插件通过 `PluginContext`（公开 API）与 Core 交互，**禁止**直接 import Core 内部实现、直接访问数据库或修改内部 Runtime 对象。

---

## 0. 五分钟写出你的第一个插件

目标：让机器人回复 `.hello 世界` → `你好，世界！`。

**基本步骤：**

1. 在 `plugins/` 下新建一个插件文件夹 `hello/`
2. 建 `manifest.json`（元数据声明）
3. 建 `main.py`（入口）

```
plugins/
└── hello/
    ├── manifest.json
    └── main.py
```

`manifest.json`：

```json
{
  "name": "hello",
  "version": "0.1.0",
  "runtime": "python",
  "entrypoint": "main.py",
  "description": "我的第一个插件",
  "permissions": ["message.read", "message.send"]
}
```

`main.py`：

```python
class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_load(self):
        # 注册命令：用户发 .hello 触发
        self.ctx.capability.register_command(
            name="hello",
            description="说你好：.hello [名字]",
            handler=self.cmd_hello,
        )

    async def cmd_hello(self, msg):
        name = " ".join(str(a) for a in (msg.get("args") or [])).strip()
        return f"你好，{name or '世界'}！"
```

**让它跑起来：**

1. 把 `hello/` 整个文件夹上传到服务器 `/root/kook_bot/plugins/hello/`
2. 重启：`systemctl restart kook-bot.service`
3. 在 KOOK 发 `.plugin status`，看到 `hello` 为 🟢 生效
4. 发 `.hello 世界` → 机器人回复「你好，世界！」

> 这就是全部。一个插件只需要：**一个 manifest.json + 一个 main.py + 一个带 `cmd_` 方法的类**。下面是这份能力最精简的说明。

---

## 1. 必须知道（基础）

这三节看完就能写绝大多数插件。

### 1.1 manifest.json

每个插件必须有一个 `manifest.json`。必需字段：`name`、`version`、`runtime`、`entrypoint`。

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | ✅ | 插件名，只允许字母、数字、`_`、`-` |
| `version` | ✅ | 版本号，如 `1.0.0` |
| `runtime` | ✅ | `python` 或 `lua`（小写） |
| `entrypoint` | ✅ | 入口文件名，默认 `main.py` |
| `description` | ❌ | 描述 |
| `author` | ❌ | 作者 |
| `permissions` | ❌ | 声明的权限列表，如 `["message.read", "message.send"]` |
| `config` | ❌ | 静态配置字典，可通过 `ctx.config(key, default)` 读取 |

### 1.2 主类与生命周期钩子

Plugin 类名为 `Plugin`，或同时含 `on_load` 与 `on_unload` 即可被识别。创建实例后，依次执行钩子（可以是同步或 `async` 函数）：

| 钩子 | 时机 | 用途 |
|------|------|------|
| `__init__(self, ctx)` | 实例化 | 保存 `ctx` 引用 |
| `on_load()` | 加载后、启用前 | 注册命令、订阅事件、读配置 |
| `on_enable()` | 启用时 | 启动需要随启用而开始的任务 |
| `on_disable()` | 禁用时 | 停止上面启动的任务 |
| `on_unload()` | 卸载时（可选） | 收尾清理 |

完整链条：`discover → validate → load(__init__) → init(on_load) → enable(on_enable)`；卸载走 `disable(on_disable) / unload(on_unload)`。

> `on_load` 是推荐的初始化位置（注册命令/事件）。

### 1.3 命令 Handler 的契约

Handler 签名固定：`async (msg) -> str | None`。

- 返回**字符串** → 机器人自动把这段文本作为回复发送。
- 返回 **`None`** → 表示插件已经自己用 `ctx.message.send` 发了内容，机器人不再额外回复。

`msg` 是命令消息对象，同时支持 dict 访问（`msg.get("args")`）与属性访问（`msg.args`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `args` | list | 命令参数拆成的列表（按空格分隔） |
| `text` | str | 整条原始消息文本 |
| `author` | str/int | 消息作者 ID |
| `sender` | str | 发送者显示名 |
| `is_group` | bool | 是否群聊 |
| `chat_id` | str/int | 会话 ID（群号或私聊对方 ID） |

### 1.4 ctx 能力速查表

`ctx` 提供以下能力，**全部可选，按需使用**：

| 能力 | 写法 | 用途 |
|------|------|------|
| 注册命令 | `self.ctx.capability.register_command(name=..., description=..., handler=...)` | 新指令 |
| 发送消息 | `await self.ctx.message.send(text, chat_id=..., is_group=...)` | 主动推送 |
| 订阅事件 | `@self.ctx.event.on("my.app.foo")` / `self.ctx.event.subscribe(...)` | 响应事件 |
| 发布事件 | `await self.ctx.event.publish("my.app.foo", {...})` | 通知其它插件 |
| 定时器 | `@self.ctx.timer.every(60)` | 周期任务 |
| 记忆写入 | `await self.ctx.memory.remember(content, memory_type=..., chat_id=...)` | 存知识 |
| 记忆检索 | `rows = await self.ctx.memory.recall(query, chat_id=..., limit=5)` | 查知识 |
| 读配置 | `self.ctx.config("key", default)` | 读 manifest.config |

> `reload`/`unload` 时会自动清理该插件的事件订阅、定时器与命令注册，防止热更新后重复执行。

---

## 2. 完整最小插件（可直接复制）

同时用上命令 + 定时器 + 记忆 + 配置的完整示例：

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
            description="这个插件跑了多久",
            handler=self.cmd_uptime,
        )
        # 每 60 秒把当前时间写入记忆（演示定时器 + 记忆）
        @self.ctx.timer.every(60)
        async def _note():
            await self.ctx.memory.remember(
                f"tick at {time.time():.0f}", memory_type="knowledge")
        self._note = _note  # 保留引用便于调用

    async def cmd_uptime(self, msg):
        secs = int(time.time() - self._started)
        return f"{self.ctx.config('prefix', '')}已运行 {secs} 秒"
```

部署验收：

1. 创建 `plugins/timerbox/` 目录与上述两个文件
2. 上传到服务器 `/root/kook_bot/plugins/timerbox/`
3. `systemctl restart kook-bot.service`
4. KOOK 发 `.plugin status`，`timerbox` 应为 🟢 生效
5. 发 `.uptime` 验证返回运行时长

---

## 3. 进阶能力

### 3.1 发送消息与主动推送

```python
ok = await self.ctx.message.send("这是主动发送的内容", chat_id=chat_id, is_group=True)
```

> 适合定时任务、事件触发等场景。做主动推送时命令 handler 返回 `None`。

### 3.2 订阅与发布事件

```python
# 订阅（装饰器方式）
@self.ctx.event.on("my.app.foo")
async def _on_foo(event):
    data = event.data or {}
    print(data.get("key"))

# 或手动订阅
self.ctx.event.subscribe("my.app.foo", self._on_foo)

# 发布（供其它插件/Core 消费）
await self.ctx.event.publish("my.app.foo", {"key": "value"})
```

内置已发布事件（常量定义在 `core/eventbus.py`）：

| 事件常量 | 值 | 触发源 |
|------|------|------|
| `EVENT_PLUGIN_LOADED` | `plugin.loaded` | 插件启用时 |
| `EVENT_PLUGIN_UNLOADED` | `plugin.unloaded` | 插件卸载时 |
| `EVENT_PLUGIN_ERROR` | `plugin.error` | 插件出错时 |
| `EVENT_TASK_*` | `task.created/completed` | Long Task Runtime |
| `EVENT_TOOL_*` | `tool.called/completed` | Tool Runtime |
| `EVENT_MEMORY_*` | `memory.created/updated` | Memory Engine |
| `EVENT_UPDATE_*` | `update.started/completed` | Update Engine |

> 插件间用**自定义事件名**（如 `my.app.foo`）通信，带上插件前缀避免撞名。

### 3.3 定时器

```python
@self.ctx.timer.every(60)
async def _heartbeat():
    await self.ctx.message.send("心跳", chat_id=chat_id, is_group=True)
```

> 定时器在 `reload`/`unload` 时自动取消。别在长时间任务里无限累计。

### 3.4 记忆读写

```python
await self.ctx.memory.remember("重要知识片段", memory_type="knowledge", chat_id=chat_id)
rows = await self.ctx.memory.recall("关键词", chat_id=chat_id, limit=5)
```

### 3.5 读取静态配置

```python
greeting = self.ctx.config("greeting", "你好")   # manifest.config["greeting"]
```

> 把可调数值放进 `manifest.config`，改配置不用改代码。

### 3.6 Lua 插件（实验性）

Lua 插件与 Python 共用同一套公开 API（经 `bridge` 表暴露），适合命令、自动回复、小游戏、简单自动化。需要服务器安装 `lupa`；沙箱移除了 `os/io/package/require/dofile/loadfile/debug` 等危险库，有单次执行超时（默认 2s）、调用预算、定时器数量上限（默认 10 个）。

```lua
-- plugins/greet/main.lua
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

Lua DSL：`bridge.command(name, desc)` / `bridge.on_event(name, fn)` / `bridge.every(sec, fn)` / `bridge.send(text, chat_id, is_group)` / `bridge.remember(content, type)` / `bridge.recall(query, limit)` / `bridge.publish(name, data)` / `bridge.config(key, default)`。

> Lua 命令函数名固定为 `cmd_<指令名>`（如 `.pgreet` → `function cmd_pgreet(msg)`）。命令在 Lua 沙箱执行（带超时），请保持函数体简洁。

---

## 4. 插件管理（运维）

因为 `plugins/` 已被 `.update` 排除，插件不走自动更新，由管理员在运行时管理。

### 4.1 指令一览

| 指令 | 作用 | 权限 |
|------|------|------|
| `.plugin status` | 卡片展示所有插件状态（绿=生效/红=异常） | 任何人 |
| `.plugin list` | 纯文字列出插件 + 状态 | 任何人 |
| `.plugin reload <名字>` | 热重载该插件（禁用→卸载→重新加载→启用） | 管理员 |
| `.plugin enable <名字>` | 启用指定插件 | 管理员 |
| `.plugin disable <名字>` | 禁用指定插件 | 管理员 |
| `.plugin unload <名字>` | 卸载指定插件（**需二次确认**） | 管理员 |
| `.plugin confirm <token>` | 确认上一步卸载 | 管理员 |

卸载是危险操作（会清理该插件的事件/定时器/命令注册，且需重启才会自动重新加载），所以 `.plugin unload` 会返回一个临时确认令牌，须再用 `.plugin confirm <token>` 二次确认（120 秒内有效）才会真正执行。

### 4.2 用文件夹前缀永久停用

把某个插件**永久不加载**，最稳的方式是把它的目录改名成 `[DISABLE]` 开头：插件加载器会自动跳过这些文件夹，重启后它们不会出现。

```bash
cd /root/kook_bot/plugins
mv echo "[DISABLE]echo"     # 停用 echo
mv "[DISABLE]echo" echo     # 想要恢复时改回来
systemctl restart kook-bot.service
```

> 这只在**重启后**生效（discover 在启动时扫描目录）。运行中想立刻停用用 `.plugin disable`。

### 4.3 部署流程

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
   └── 🔴 未生效/错误 → 看卡片上的错误信息并修复
```

状态判定（`.plugin status` 卡片）：

| 状态 | 卡片标记 | 含义 |
|------|------|------|
| `enabled` 且无 error | 🟢 | 已生效 |
| `error` 或 error 非空 | 🔴 错误 | 加载/启用/运行出错，附错误信息 |
| 其它（如 `disabled`） | 🔴 未生效 | 已发现但未启用 |

### 4.4 插件打包与分享（.hmp）

插件可打包成后缀 `.hmp` 的压缩包（`zip` **仅储存**模式）分享给其它装了这个 Bot 的服务器。所有操作通过 `.plugin` 的 CLI 风格参数完成，下载的包统一保存在 `plugins/_down/`。

| 指令 | 作用 | 权限 |
|------|------|------|
| `.plugin -pack <本地插件名>` | 把 `plugins/<本地插件名>/` 打包为 `plugins/_down/<manifest.name>.hmp` | 管理员 |
| `.plugin -down` | 引用聊天里的 `.hmp`，下载保存到 `_down/` | 管理员 |
| `.plugin -down -load` | 下载并**加载**引用的 `.hmp` | 管理员 |
| `.plugin -load <xxx.hmp>` | 解包+加载 `_down/xxx.hmp` | 管理员 |
| `.plugin -load`（带附件） | 从聊天附件下载并加载 | 管理员 |
| `.plugin -load -list` / `.plugin -down -list` | 列出 `_down/` 里已下载的 `.hmp` | 管理员 |

从聊天加载 `.hmp` 的姿势：**先 `.hmp` 文件发到聊天**，然后**引用该消息**发 `.plugin -down -load`；机器人会自动下载→解包到 `plugins/<名字>/`→`load→init→enable` 接入运行时，无需重启。

说明：

- 下载/解包带严格安全限制：`.hmp` 上限 10MB、单文件下载上限 50MB、拒绝 zip-slip（路径穿越）；插件名只允许 `字母/数字/_/-`。
- `_down/` 目录本身没有 `manifest.json`，不会出现在 `.plugin status` 里，也不会被 discover 误加载。
- 若解包时发现同名插件已存在：未启用会自动补启用；已运行则提示先 `.plugin unload`。

```bash
# 快速分享示例（在你自己的机器上）
#   plugins/echo → plugins/_down/echo.hmp
# 把 echo.hmp 发给有人装了同一 Bot 的群
# 对方引用该文件后发：
.plugin -down -load
```

---

## 5. 调试与最佳实践

- **插件出错只影响自己**：异常会把插件标记为 `error` 并在卡片上标红；Core 与其它插件不受影响。
- **命令 handler 保持返回字符串**用于回复；主动推送时用 `ctx.message.send` 并返回 `None`。
- **异步优先**：钩子与 handler 写成 `async` 函数，避免阻塞事件循环。
- **配置集中管理**：可调数值放进 `manifest.config`，用 `ctx.config()` 读取，改配置不用改代码。
- **事件命名用插件前缀**：如 `myplugin.something`，避免与内置或其它插件撞名。
- **不要信任外部输入**：命令参数 `msg.args` 来自用户，使用前做必要校验。

---

## 6. 常见问题

**Q：`.plugin status` 显示红色「未找到 Plugin 类」？**
A：`main.py` 里缺少名为 `Plugin`（或同时含 `on_load` 与 `on_unload`）的类。

**Q：改插件后 `.update` 没同步？**
A：符合预期。`plugins/` 被 `.update` 排除，需手动上传 + 重启。

**Q：Lua 插件报「Lua runtime 未就绪」？**
A：服务器缺 `lupa`。`pip install lupa` 后重启。

**Q：命令 handler 返回字符串但机器人没回复？**
A：确认 manifest `runtime=python`、入口类名为 `Plugin`、且 `.plugin status` 显示 🟢。字符串返回值由命令分发层转成回复。

**如何让命令带多个参数（如 `.weather beijing today`）？**
A：`msg.args` 按空格拆好，`msg.args[0]`、`msg.args[1]` 直接取；要合并全部参数用 `" ".join(str(a) for a in msg.get("args"))`。

**Q：想永久停用插件但没有卸载指令怎么办？**
A：用 `[DISABLE]` 前缀改目录名后重启（见 4.2）。

---

## 7. 内部架构（进阶阅读）

> 说明实现原理；写插件一般不需要关心这里。

- **发现 discover**：扫描 `plugins/` 每个子目录，跳过 `[DISABLE]` 前缀目录和缺 `manifest.json` 的目录，读取并校验 manifest。
- **加载 load**：为 Python 插件动态导入 entrypoint 模块，定位 `Plugin` 类并实例化；Lua 插件走 LuaRuntime 沙箱。
- **启停 enable/disable**：调用 `on_enable` / `on_disable` 钩子，发布 `plugin.loaded` / `plugin.unloaded` 事件。
- **热更新 reload**：先 disable → unload → 从 records 移除 → 重新 discover → load → init → enable，并自动清理旧订阅/定时器/命令。
- **健康 health_all**：按“state==enabled 且无 error”判定生效，供 `.plugin status` 卡片使用。
- **命令分发**：插件用 `ctx.capability.register_command` 注册进 `CapabilityRegistry`；分发器在静态 `COMMAND_MAP` 未命中时查询插件动态命令并用兼容消息对象调用。

### 7.1 相关源码索引

| 模块 | 作用 |
|------|------|
| `core/plugin/manifest.py` | `manifest.json` 校验与 `PluginManifest` |
| `core/plugin/loader.py` | 目录扫描 `discover`（含 `[DISABLE]` 跳过）、Python 模块 `load_module`、定位 `Plugin` 类 |
| `core/plugin/manager.py` | 生命周期管理、`get_plugin_manager()`、`health_all()` |
| `core/plugin/api.py` | `PluginContext` 与各能力实现 |
| `core/plugin/lua.py` | Lua 沙箱与 `bridge` DSL |
| `core/capability/registry.py` | 能力注册中心（命令/技能/工具） |
| `core/eventbus.py` | 事件总线与内置事件常量 |
| `modules/commands.py` | 命令分发层、`.plugin` 管理指令 |