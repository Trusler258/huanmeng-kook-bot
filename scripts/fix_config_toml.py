#!/usr/bin/env python3
"""修复 bot_config.toml — 从 broken 配置取非 identity 键，
   用 0.7.4 的干净 identity 替换，加上禁止用语"""
import re, sys

BROKEN = "/root/bot/config/bot_config.toml.broken"
OLD    = "/root/bot/0.7.4/config/bot_config.toml"
TARGET = "/root/bot/config/bot_config.toml"

with open(BROKEN, 'r') as f:
    broken = f.read()
with open(OLD, 'r') as f:
    old = f.read()

# 从 old 提取 identity 多行字符串
m = re.search(r'(identity = """.*?""")', old, re.DOTALL)
if not m:
    sys.exit("identity not found in old config")
identity_clean = m.group(1)

# 注入禁止用语
identity_clean = identity_clean.replace(
    '【行为底线】',
    '【禁止用语】\n禁止使用以下不符合猫娘16岁女高中生身份的词汇：'
    '好家伙、我去、牛逼、牛啊、绝了、6、不愧是你、这就去、整一个。'
    '请用猫娘特有表达如"呜哇""诶嘿""喵呜"替代。\n\n【行为底线】'
)

# 从 broken 定位 identity 块并替换
# broken 格式: identity = "content"
start = broken.find('identity = "')
if start == -1:
    sys.exit("identity not found in broken config")

# 找结束引号 (行首独立的 ")
pos = start + len('identity = ')
end = -1
escaping = False
for i in range(pos, len(broken)):
    ch = broken[i]
    if escaping:
        escaping = False
        continue
    if ch == '\\':
        escaping = True
        continue
    if ch == '"':
        # 看看后面是不是行尾或新section
        rest = broken[i+1:i+20].lstrip()
        if not rest or rest[0] == '\n' or rest[0] == '[':
            end = i + 1
            break

if end == -1:
    sys.exit(f"cannot find end of identity, start={start}")

# 重建 identity 块（用 TOML 多行字符串格式）
new_identity_block = 'identity = """\n'
inner = identity_clean[len('identity = """\n'):].rstrip('"""').rstrip()
new_identity_block += inner + '\n"""'

fixed = broken[:start] + new_identity_block + broken[end:]
with open(TARGET, 'w') as f:
    f.write(fixed)

print(f"Fixed! identity block: {len(new_identity_block)} chars")
