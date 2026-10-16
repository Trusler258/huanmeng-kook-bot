"""公会登记的数据层（`data/gh.json` 读写）—— 核心级共享模块。

背景（2026-10-12 真正插件化）：`modules/gh.py` 拆成两部分——
  · **数据层**（本文件）被两处共用，必须留在核心层：
      1) `plugins/gh/gh_core.py`（插件，.gh 指令）
      2) `modules/leave.py`（核心钩子，/_leave 里 `_fmt_gh` 复用 ~gh list 的格式化输出）
  · **指令逻辑** 已整体搬进 `plugins/gh/`，原 `modules/gh.py` 改后缀退役为 `modules/gh.py.legacy`。
这样 plugin 只依赖一个稳定的核心数据层，反向依赖（核心 import 插件）得以避免。

存储结构：{群号: {"opts": [QQ...], "members": {QQ: {"name": 公会名, "nick": 昵称} | str}}}
"""
from __future__ import annotations

import json
from pathlib import Path

from core.logger import get_logger

logger = get_logger("guild")


def _load(gh_path: Path) -> dict:
    """读 gh.json；含旧格式兼容迁移（members 曾直接挂在 group 下）。"""
    if gh_path.exists():
        try:
            data = json.loads(gh_path.read_text(encoding="utf-8"))
            # 旧格式兼容: members 曾直接挂在 group 下，迁移到 members 字段
            for gid, gd in list(data.items()):
                if isinstance(gd, dict) and "members" not in gd:
                    members = {}
                    opts = []
                    for k, v in list(gd.items()):
                        if k == "opts":
                            opts = v if isinstance(v, list) else []
                        elif isinstance(v, (dict, str)):
                            members[k] = v
                    data[gid] = {"opts": opts, "members": members}
            return data
        except Exception:
            return {}
    return {}


def _save(gh_path: Path, data: dict):
    gh_path.parent.mkdir(parents=True, exist_ok=True)
    gh_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _gkey(group_id: int) -> str:
    return str(group_id)
