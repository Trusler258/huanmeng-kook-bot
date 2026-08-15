"""
Phase 20 Part11 新增：.listening（机器人"正在听"状态）命令测试

覆盖：
- cmd_listening 各子命令（设置 / off / status / 空）的解析与返回
- COMMAND_MAP 注册（listening / 正在听 别名）
- set_music 构造 payload 正确（data_type=2 / software / singer / music_name）
- 无 token 时优雅降级（不抛异常）
运行: python tests/_test_phase20_listening.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 避免真实 KOOK 请求：mock _get_token 返回空 → set_music 应优雅失败不抛异常
from services.music_status import set_music, clear_music, current_status


def test_payload_construction():
    """set_music 应构造 data_type=2 的 payload（通过 mock _post 捕获）。"""
    from services import music_status as ms
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return True, "操作成功"

    async def run():
        with patch.object(ms, "_get_token", return_value="tkn"), \
             patch.object(ms, "_post", side_effect=fake_post):
            ok, msg = await set_music("有点甜", singer="汪苏泷、BY2", software="cloudmusic")
        return ok, msg

    ok, msg = asyncio.run(run())
    assert ok, msg
    assert captured["payload"]["data_type"] == 2
    assert captured["payload"]["music_name"] == "有点甜"
    assert captured["payload"]["singer"] == "汪苏泷、BY2"
    assert captured["payload"]["software"] == "cloudmusic"
    assert captured["url"].endswith("/game/activity")
    print("OK test_payload_construction (data_type=2/singer/music_name/software 正确)")


def test_no_token_graceful():
    """无 token 时 set_music 应返回失败信息而非抛异常。"""
    async def run():
        from services import music_status as ms
        with patch.object(ms, "_get_token", return_value=""):
            ok, msg = await set_music("有点甜")
        return ok, msg
    ok, msg = asyncio.run(run())
    assert ok is False
    assert "token" in msg
    print("OK test_no_token_graceful (无 token 优雅降级)")


def test_current_status():
    """current_status 未设置时返回空 dict。"""
    from services import music_status as ms
    ms._last.clear()
    assert ms.current_status() == {}
    print("OK test_current_status (初始为空)")


def test_command_wiring():
    """确认 listening / 正在听 已注册到 COMMAND_MAP（读源码文本，避免重依赖导入）。"""
    src = Path(__file__).resolve().parent.parent / "modules" / "commands.py"
    text = src.read_text(encoding="utf-8")
    assert '"listening":' in text and 'cmd_listening' in text
    assert '"正在听":' in text and 'cmd_listening' in text
    # cmd_listening 定义存在
    assert "async def cmd_listening" in text
    print("OK test_command_wiring (listening/正在听 已注册到 COMMAND_MAP)")


async def test_cmd_listening_branches():
    """cmd_listening 各分支返回（通过源码 `exec` 提取函数，绕开重依赖导入）。"""
    from services import music_status as ms

    # 从源码提取 cmd_listening 函数体（其内部依赖在其作用域内 import）
    src_file = Path(__file__).resolve().parent.parent / "modules" / "commands.py"
    text = src_file.read_text(encoding="utf-8")
    start = text.index("async def cmd_listening(")
    end = text.index("\n\nasync def ", start)
    fn_src = text[start:end]

    ns: dict = {}
    exec(compile(fn_src, "cmd_listening", "exec"), ns)
    cmd_listening = ns["cmd_listening"]

    # 空 → 查看状态
    with patch.object(ms, "current_status", return_value={}):
        r = await cmd_listening([], 1, 2, "u", False, 0)
    assert "正在听" in r or "当前没有" in r

    # off → 结束（mock 成功）
    with patch.object(ms, "clear_music", return_value=(True, "操作成功")):
        r = await cmd_listening(["off"], 1, 2, "u", False, 0)
    assert "已结束" in r

    # 设置 → 成功
    with patch.object(ms, "set_music", return_value=(True, "操作成功")):
        r = await cmd_listening(["有点甜", "汪苏泷、BY2"], 1, 2, "u", False, 0)
    assert "正在听 有点甜" in r
    print("OK test_cmd_listening_branches (空/off/设置 分支)")

if __name__ == "__main__":
    test_payload_construction()
    test_no_token_graceful()
    test_current_status()
    test_command_wiring()
    asyncio.run(test_cmd_listening_branches())
    print("\n=== ALL Phase20 LISTENING TESTS PASSED ===")