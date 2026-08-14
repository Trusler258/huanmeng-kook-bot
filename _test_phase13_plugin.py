"""Phase 13 Plugin Runtime 测试（Huanmeng 2.0）"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from core.plugin.manifest import validate_manifest, RUNTIME_PYTHON, RUNTIME_LUA
from core.plugin.loader import discover_plugins, load_module, locate_plugin_classes
from core.plugin.manager import PluginManager, STATE_ENABLED, STATE_ERROR
from core.eventbus import EventBus, EVENT_PLUGIN_LOADED, EVENT_PLUGIN_UNLOADED


def _make_plugin_dir(base, name, runtime=RUNTIME_PYTHON, entry="main.py"):
    d = Path(base) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        '{"name": "%s", "version": "1.0.0", "runtime": "%s", '
        '"entrypoint": "%s", "permissions": ["message.read", "message.send"], "config": {}}'
        % (name, runtime, entry), encoding="utf-8")
    return d


def test_manifest_validation():
    ok, err = validate_manifest({"name": "good", "version": "1.0.0",
                                 "runtime": "python", "entrypoint": "main.py"})
    assert ok is not None and err is None, (ok, err)
    # 缺字段
    ok, err = validate_manifest({"name": "x"})
    assert ok is None and err
    # 非法 runtime
    ok, err = validate_manifest({"name": "x", "version": "1", "runtime": "go",
                                 "entrypoint": "m.py"})
    assert ok is None and err
    # 非法插件名
    ok, err = validate_manifest({"name": "bad name!!", "version": "1",
                                 "runtime": "python", "entrypoint": "m.py"})
    assert ok is None and err


def test_discover_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        _make_plugin_dir(tmp, "echo")
        (Path(tmp) / "echo" / "main.py").write_text(
            "class Plugin:\n"
            "    def __init__(self, ctx): self.ctx = ctx\n"
            "    async def on_load(self): pass\n"
            "    async def on_message(self, msg): return 'hi'\n",
            encoding="utf-8")
        mfs = discover_plugins(tmp)
        assert len(mfs) == 1 and mfs[0].name == "echo"
        mod = load_module(mfs[0])
        assert mod is not None
        classes = locate_plugin_classes(mod)
        assert len(classes) == 1


def test_eventbus_subscribe_publish():
    bus = EventBus()
    got = []
    bus.subscribe("msg.test", lambda e: got.append(e.data.get("v")))
    bus.publish_sync("msg.test", {"v": 42})
    assert got == [42]
    # 通配
    bus.subscribe("*", lambda e: got.append("wild"))
    bus.publish_sync("other.evt", {})
    assert "wild" in got


def test_manager_lifecycle():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            _make_plugin_dir(tmp, "echo")
            (Path(tmp) / "echo" / "main.py").write_text(
                "class Plugin:\n"
                "    def __init__(self, ctx): self.ctx = ctx\n"
                "    async def on_load(self): self.loaded = True\n"
                "    async def on_enable(self): self.enabled = True\n"
                "    async def on_disable(self): self.enabled = False\n"
                "    async def on_unload(self): self.unloaded = True\n",
                encoding="utf-8")
            bus = EventBus()
            events = []
            bus.subscribe(EVENT_PLUGIN_LOADED, lambda e: events.append("loaded"))
            bus.subscribe(EVENT_PLUGIN_UNLOADED, lambda e: events.append("unloaded"))
            mgr = PluginManager(tmp, bus)
            mgr.discover()
            assert mgr.validate("echo") == (True, "")

            ok, _ = await mgr.load("echo")
            assert ok
            ok, _ = await mgr.init("echo")
            assert ok
            ok, _ = await mgr.enable("echo")
            assert ok
            assert mgr.health("echo")["ok"] is True
            assert "loaded" in events

            ok, _ = await mgr.disable("echo")
            assert ok
            assert mgr.health("echo")["state"] != STATE_ENABLED

            ok, _ = await mgr.enable("echo")
            assert ok
            ok, _ = await mgr.reload("echo")
            assert ok, "reload should succeed"
            assert mgr.health("echo")["ok"] is True

            ok, _ = await mgr.unload("echo")
            assert ok
            assert "unloaded" in events
    asyncio.run(run())


def test_manager_isolates_plugin_error():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            _make_plugin_dir(tmp, "bad")
            _make_plugin_dir(tmp, "good")
            (Path(tmp) / "bad" / "main.py").write_text(
                "class Plugin:\n"
                "    def __init__(self, ctx): raise RuntimeError('boom')\n",
                encoding="utf-8")
            (Path(tmp) / "good" / "main.py").write_text(
                "class Plugin:\n"
                "    def __init__(self, ctx): self.ctx = ctx\n"
                "    async def on_load(self): pass\n",
                encoding="utf-8")
            mgr = PluginManager(tmp)
            ok_names = await mgr.load_all()
            # 坏插件失败不影响好插件
            assert "good" in ok_names and "bad" not in ok_names
            assert mgr.health("bad")["ok"] is False
            assert mgr.health("bad")["state"] == STATE_ERROR
            assert mgr.health("good")["ok"] is True
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