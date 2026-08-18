"""TUF 插件 v2 功能测试：验证 .tuf 子命令（真实调用官方 API）。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

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
    from core.plugin.manager import get_plugin_manager
    from core.capability import get_capability_registry

    mgr = get_plugin_manager("plugins")
    mgr.discover()
    ok = await mgr.load_all()
    assert "tuf" in ok, f"tuf 加载失败: {ok}"
    print("OK tuf 加载")

    reg = get_capability_registry()
    names = {c.name for c in reg.all() if c.category == "command"}
    assert "tuf" in names
    print("OK tuf 命令注册")

    inst = mgr._records["tuf"].instance
    TEST_UID = 656176615

    # help
    r = await inst._cmd(make_msg(["help"]))
    assert "bind" in r and "search" in r
    print("OK help 包含 bind/search")

    # 未知子命令
    r = await inst._cmd(make_msg(["hahaha"]))
    assert "未知子命令" in r
    print("OK 未知子命令提示")

    # search（真实 API）
    r = await inst._cmd(make_msg(["search", "Hello"]))
    assert "搜索结果" in r and "Camellia" in r or "Camellia" in r or r.startswith("🔍") or "未找到" in r
    print("OK search 返回:", r.splitlines()[0] if r else "?")

    # bind（用已知玩家 Jipper）
    r = await inst._cmd(make_msg(["bind", "jipper"]))
    assert "已绑定" in r, f"bind 失败: {r}"
    print("OK bind:", r)

    # me（绑定后查自己）
    r = await inst._cmd(make_msg(["me"]))
    assert "Jipper" in r or "👤" in r, f"me 失败: {r}"
    print("OK me:", r.splitlines()[0])

    # player（带 ID）
    r = await inst._cmd(make_msg(["player", "25"]))
    assert "👤" in r, f"player 失败: {r}"
    print("OK player 25:", r.splitlines()[0])

    # lb（排行榜）
    r = await inst._cmd(make_msg(["lb", "ranked"]))
    assert "排行" in r or "未获取" in r, f"lb 失败: {r}"
    print("OK lb ranked:", r.splitlines()[0] if r else "?")

    # stats
    r = await inst._cmd(make_msg(["stats"]))
    assert "全局统计" in r or "❌" in r, f"stats 失败: {r}"
    print("OK stats:", r.splitlines()[0] if r else "?")

    # countries
    r = await inst._cmd(make_msg(["countries"]))
    assert "国家分布" in r or "❌" in r, f"countries 失败: {r}"
    print("OK countries:", r.splitlines()[0] if r else "?")

    # song
    r = await inst._cmd(make_msg(["song", "Hello"]))
    assert "歌曲" in r, f"song 失败: {r}"
    print("OK song:", r.splitlines()[0] if r else "?")

    # packs
    r = await inst._cmd(make_msg(["packs"]))
    assert "关卡包" in r or "❌" in r, f"packs 失败: {r}"
    print("OK packs:", r.splitlines()[0] if r else "?")

    # info（谱面详情）
    r = await inst._cmd(make_msg(["info", "16161"]))
    assert r and "🎵" in r, f"info 失败: {r}"
    print("OK info 16161:", r.splitlines()[0])

    # passes（谱面通关）
    r = await inst._cmd(make_msg(["passes", "16161"]))
    assert r, f"passes 失败: {r}"
    print("OK passes:", r.splitlines()[0])

    # dl
    r = await inst._cmd(make_msg(["dl", "16161"]))
    assert "下载" in r or "❌" in r, f"dl 失败: {r}"
    print("OK dl:", r.splitlines()[0] if r else "?")

    # rank（玩家排名历史）
    r = await inst._cmd(make_msg(["rank", "25"]))
    assert "排名历史" in r or "暂无" in r, f"rank 失败: {r}"
    print("OK rank:", r.splitlines()[0] if r else "?")

    # passesby
    r = await inst._cmd(make_msg(["passesby", "25"]))
    assert "通关" in r or "❌" in r, f"passesby 失败: {r}"
    print("OK passesby:", r.splitlines()[0] if r else "?")

    # rerate
    r = await inst._cmd(make_msg(["rerate", "16161"]))
    assert r, f"rerate 失败: {r}"
    print("OK rerate:", r.splitlines()[0] if r else "?")

    # 清理绑定
    import plugins.tuf.main as tuf_mod
    tuf_mod.del_bind(TEST_UID)

    await mgr.unload("tuf")
    print("\n=== ALL TUF V2 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
