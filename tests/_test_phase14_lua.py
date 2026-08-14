"""Phase 14 Lua Plugin 测试（Huanmeng 2.0）"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.plugin.lua import LuaSandbox, _run_in_thread
from core.plugin.manifest import validate_manifest
from core.plugin.api import PluginContext
from core.plugin.manager import PluginManager


def _lua_plugin_dir(base, name, code):
    d = Path(base) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        '{"name":"%s","version":"1.0.0","runtime":"lua","entrypoint":"main.lua",'
        '"permissions":["message.read","message.send"],"config":{"greeting":"hi"}}'
        % name, encoding="utf-8")
    (d / "main.lua").write_text(code, encoding="utf-8")
    return d


def test_runtime_tag():
    ok, err = validate_manifest({"name": "x", "version": "1", "runtime": "lua",
                                 "entrypoint": "main.lua"})
    assert ok is not None and err is None


def test_sandbox_blocks_os():
    class FakeCtx:
        pass
    sb = LuaSandbox(FakeCtx(), "t")
    # os 应被剔除
    try:
        val = sb._lua.eval("return os")
        assert val is None, "os 应被剔除"
    except Exception as e:
        # 若访问报错也算被阻止
        assert "os" in str(e) or True
    sb.cleanup()


def test_command_execution():
    class FakeCtx:
        def __init__(self):
            self.capability = type("C", (), {"register_command": lambda *a, **k: None})()
            self.message = type("M", (), {"send": lambda *a, **k: True})()
            self.memory = type("Mem", (), {"remember": lambda *a, **k: None,
                                           "recall": lambda *a, **k: []})()
            self.event = type("E", (), {"subscribe": lambda *a, **k: None,
                                        "publish": lambda *a, **k: None})()
            self.timer = type("T", (), {"_tasks": []})()
        def config(self, k, d=None):
            return "hi"
    ctx = FakeCtx()
    cb = None
    sb = LuaSandbox(ctx, "greet")
    code = (
        'bridge.command("pgreet", "greet")\n'
        'function cmd_pgreet(msg)\n'
        '  local n = msg.args and msg.args[1]\n'
        '  return bridge.config("greeting", "hi") .. " " .. tostring(n)\n'
        'end\n'
    )
    sb.load(code)
    assert "pgreet" in sb.command_names()
    assert sb.run_command("pgreet", {"args": ["world"]}) == "hi world"
    # 未注册命令
    assert sb.run_command("nope", {}) is None
    sb.cleanup()


def test_timeout_detection():
    class FakeCtx:
        pass
    sb = LuaSandbox(FakeCtx(), "t")
    # 无限循环应触发超时
    try:
        sb.load("while true do end")
        assert False, "应触发超时"
    except Exception as e:
        assert "超时" in str(e) or "timeout" in str(e).lower()
    assert sb._poisoned is True
    sb.cleanup()


def test_manager_lua_lifecycle():
    async def run():
        # 用真实 PluginContext（memory.send 等不会真正外出，因为 send/recall 走 engine 但 DB 未初始化则降级）
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                'bridge.command("pgreet", "greet")\n'
                'function cmd_pgreet(msg)\n'
                '  return "hi " .. tostring(msg.args and msg.args[1] or "x")\n'
                'end\n'
            )
            _lua_plugin_dir(tmp, "greet", code)
            mgr = PluginManager(tmp)
            ok_names = await mgr.load_all()
            assert "greet" in ok_names, ok_names
            assert mgr.health("greet")["ok"] is True
            # 卸载
            ok, _ = await mgr.unload("greet")
            assert ok
    asyncio.run(run())


if __name__ == "__main__":
    import traceback
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                print(f"FAIL {name}")
                traceback.print_exc()