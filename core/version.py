"""版本号 —— 单一真相源。

改版本**只改这里**。以下位置都从这里读，不再各自硬编码：
  - `bot.py` 的 `Bot.VERSION`（启动日志「幻梦 KOOK Bot vX.Y.Z 正在启动」）
  - `plugins/bg_tasks/main.py` 的 KOOK 动态状态（「正在听 当前版本: X.Y.Z」）
  - `README.md` 的徽章（手工同步）

⚠️ 此前这三处各自硬编码 `2.0.2`，改一处必漏两处（2026-10-16 修正）。
⚠️ 版本号与 QQ bot 对齐（QQ 的在 `config/version.toml`）：两边同号，一眼可知 KOOK 是否跟上了 QQ。
⚠️ 另注：LLM 自我认知回答的版本来自 `data/update_log.md` 的最新 `## vX.Y.Z` 标题，
   发版时这里和 update_log 要一起改。
"""
from __future__ import annotations

VERSION = "2.3.101"
