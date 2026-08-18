"""Phase 20 Hotfix E：工具类命令迁移插件（一命令一插件）验证。

验证 7 个单命令插件（whois / weather / eq / nasa / luck / chou / countdown）：
1. 能被发现并加载启用
2. 命令注册进 CapabilityRegistry
3. handler 直接调用返回正确结果（模拟 msg）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

# 轻量假 LLM（避免真实调用）
_FAKE_LLM = types.ModuleType("services.llm")
async def _fake_call_llm(*a, **kw):
    return "fake"
_FAKE_LLM.call_llm = _fake_call_llm
sys.modules.setdefault("services.llm", _FAKE_LLM)


def make_msg(args, author=656176615, chat_id=12345, is_group=True, sender="Tester"):
    return {
        "args": args, "author": author, "chat_id": chat_id,
        "is_group": is_group, "sender": sender, "bot_qq": 0,
    }


async def main():
    from core.plugin.loader import discover_plugins
    from core.plugin.manager import get_plugin_manager

    manifests = discover_plugins("plugins")
    names = {m.name for m in manifests}
    expect_plugins = ("whois", "weather", "eq", "nasa", "luck", "chou", "countdown")
    for expect in expect_plugins:
        assert expect in names, f"{expect} 未发现"
        print(f"OK discover {expect}")

    mgr = get_plugin_manager("plugins")
    mgr.discover()
    ok_names = await mgr.load_all()
    for expect in expect_plugins:
        assert expect in ok_names, f"{expect} 加载失败: {ok_names}"
        print(f"OK load {expect}")

    # 命令注册验证
    from core.capability import get_capability_registry
    reg = get_capability_registry()
    reg_names = {c.name for c in reg.all() if c.category == "command"}

    expect_cmds = {
        "whois": ("whois", "域名"),
        "weather": ("天气", "weather"),
        "eq": ("eq", "地震"),
        "nasa": ("nasa",),
        "luck": ("luck",),
        "chou": ("抽", "chou"),
        "countdown": ("countdown", "倒计时"),
    }
    for plug, cmds in expect_cmds.items():
        for cmd in cmds:
            assert cmd in reg_names, f"{plug} 命令 {cmd} 未注册"
        print(f"OK {plug} 命令注册 {cmds}")

    # handler 功能验证
    inst = mgr._records["luck"].instance
    r = await inst._cmd(make_msg([]))
    assert isinstance(r, str) and "运气" in r, f"luck 异常: {r}"
    print("OK luck 返回:", r)

    inst = mgr._records["chou"].instance
    r = await inst._cmd(make_msg(["A", "B", "C"]))
    assert isinstance(r, str) and ("A" in r or "B" in r or "C" in r), f"chou 异常: {r}"
    print("OK chou 返回:", r)

    inst = mgr._records["countdown"].instance
    r = await inst._cmd(make_msg(["2026-12-25", "测试节日"]))
    assert "已添加倒计时" in r, f"countdown add 异常: {r}"
    r = await inst._cmd(make_msg(["list"]))
    assert "【倒计时列表】" in r, f"countdown list 异常: {r}"
    r = await inst._cmd(make_msg(["del", "1"]))
    assert "已删除" in r, f"countdown del 异常: {r}"
    print("OK countdown add/list/del")

    inst = mgr._records["whois"].instance
    r = await inst._cmd(make_msg([]))
    assert "用法" in r, f"whois 用法异常: {r}"
    print("OK whois 用法:", r)

    # 卸载（清理）
    for name in expect_plugins:
        await mgr.unload(name)
    print("\n=== ALL PHASE20 HOTFIX E TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
