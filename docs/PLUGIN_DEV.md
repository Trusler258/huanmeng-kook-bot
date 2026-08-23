# 幻梦 QQ Bot — 插件开发指南

## 目录
1. [快速开始](#快速开始)
2. [指令格式](#指令格式)
3. [注册指令](#注册指令)
4. [调用 LLM 发消息](#调用-llm-发消息)
5. [发送图片卡片](#发送图片卡片)
6. [配置与语言文件](#配置与语言文件)
7. [完整示例](#完整示例)

---

## 快速开始

在 `modules/` 下新建一个 `.py` 文件，写一个异步函数，然后在 `commands.py` 注册即可。

```
modules/
  my_plugin.py   ← 你的插件
```

---

## 指令格式

所有指令以 `.` 开头。用户在群或私聊中发送：

```
.mycmd 参数1 参数2 ...
```

你的处理函数会收到：

```python
async def cmd_mycmd(args, user_id, group_id, sender_name, is_group, bot_kook):
    """
    args:        list[str]  指令参数 ["参数1", "参数2"]
    user_id:     int        发送者的 QQ 号
    group_id:    int        群号（私聊时为 0）
    sender_name: str        发送者显示名
    is_group:    bool       是否群聊
    bot_kook:      int        机器人 QQ 号
    
    返回值:
        str          纯文本 → 自动发送
        None         不回复
        dict         需要手动发送（见下文）
    """
```

返回值可以是：
- `"文本消息"` → 框架自动发送
- `None` → 不发送任何东西
- `dict` → 你自己调 sender 发（图片、语音等）

---

## 注册指令

编辑 `modules/commands.py`，找到 `COMMAND_MAP` 字典，添加你的指令：

```python
# commands.py

from modules.my_plugin import cmd_mycmd   # 导入

COMMAND_MAP = {
    # ... 已有的指令 ...
    "mycmd": cmd_mycmd,   # .mycmd → cmd_mycmd
}
```

规则：
- key = 指令名（不含 `.`）
- value = 异步函数

---

## 权限控制

```python
from core.config import get_config

async def cmd_mycmd(args, user_id, group_id, sender_name, is_group, bot_kook):
    cfg = get_config()
    
    # 仅主人
    if not cfg.is_admin(user_id, group_id):
        return "权限不足喵~"
    
    # 仅群聊
    if not is_group:
        return "这个指令只能在群里用喵~"
    
    # ...
```

- `cfg.is_admin(user_id, group_id)` — 含主人 + 分群 OP
- `user_id == cfg.admin_qq` — 仅主人

---

## 调用 LLM / 判断

```python
from services.llm import call_llm
from services.judge import judge_interest

# 简单 LLM 调用
result = await call_llm(
    system="你是一个助手",
    user_prompt="帮我分析这段文字",
    model="deepseek-chat"  # 可选，默认用主模型
)
```

---

## 发送图片卡片

用 Playwright 渲染 HTML → 截图 → 发群：

```python
from modules.changelog import render_card_to_image
from services.sender import send_group_msg

html = "<div style='padding:20px; background:#1a1a2e; color:white;'>..."
path = await render_card_to_image(html, prefix="mycard")
if path:
    cq = f"[CQ:image,file=file:///{path}]"
    await send_group_msg(cq, group_id)
```

模板文件放在 `data/templates/` 下，用 `.html` 格式。

---

## 配置与语言文件

### 读取自定义配置

在 `config/bot_config.toml` 中加节：

```toml
[my_plugin]
api_key = "xxx"
max_results = 10
```

代码中读取：

```python
from core.config import get_config
cfg = get_config()
key = cfg.config.get("my_plugin", {}).get("api_key", "")
```

### 语言文件

在 `config/lang.toml` 中加帮助文本：

```toml
[help.detail]
mycmd = "【我的指令 .mycmd】\n.mycmd 参数1 参数2\n详细说明"

[mycmd]
prompt = "请输入参数喵~"
empty = "什么都没有查到喵~"
```

代码中读取：

```python
lang = cfg.lang
text = lang.get("mycmd", {}).get("prompt", "默认文本")
```

---

## 完整示例

这是一个完整的插件示例：**随机猫图**

### 1. `modules/cat.py`

```python
"""随机猫图 .cat"""

import httpx
from core.config import get_config


async def cmd_cat(args, user_id, group_id, sender_name, is_group, bot_kook):
    cfg = get_config()
    
    # 权限：主人或分群 OP
    if not cfg.is_admin(user_id, group_id):
        return "只有主人能召唤猫咪喵~"
    
    # 调用公开 API
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.thecatapi.com/v1/images/search")
            r.raise_for_status()
            data = r.json()
            url = data[0]["url"]
    except Exception:
        return "猫咪今天不想出来喵… 再试一次？"
    
    # 发图片到群
    from services.sender import send_group_msg
    cq = f"[CQ:image,file={url}]"
    await send_group_msg(cq, group_id)
    return None  # 图片已发，不用文本
```

### 2. 注册 (`commands.py` 加两行)

```python
from modules.cat import cmd_cat

COMMAND_MAP = {
    # ...已有指令...
    "cat": cmd_cat,
}
```

### 3. 帮助文本 (`config/lang.toml` 加)

```toml
[help.detail]
cat = "【随机猫图 .cat】\n调用 TheCatAPI 随机返回一只猫咪"
```

### 4. 部署

```bash
scp modules/cat.py root@服务器:/root/bot/modules/
ssh root@服务器 "systemctl restart bot"
```

群内发送 `.cat` 即可。

---

## 注意事项

- 所有处理函数必须是 **async**
- 耗时操作用 `asyncio.to_thread()` 或异步库（httpx/aiohttp）
- 敏感配置（API Key 等）写 `.env` 或 `bot_config.toml`（`.gitignore` 已排除）
- 插件放到 `modules_private/` 可防止提交到开源仓库
