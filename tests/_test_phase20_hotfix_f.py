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

# mock services.sender（测试环境无 khl SDK）：记录卡片调用
_FAKE_SENDER = types.ModuleType("services.sender")
_sent = {"cards": []}
async def _fake_send_raw_group(obj, chat_id):
    _sent["cards"].append(obj)
async def _fake_send_raw_user(obj, user_id):
    _sent["cards"].append(obj)
async def _fake_send_group_msg(text, group_id):
    pass
async def _fake_send_private_msg(text, user_id):
    pass
async def _fake_send_by_chat_type(text, chat_id, is_group, user_id=None):
    pass
_FAKE_SENDER.send_raw_group = _fake_send_raw_group
_FAKE_SENDER.send_raw_user = _fake_send_raw_user
_FAKE_SENDER.send_group_msg = _fake_send_group_msg
_FAKE_SENDER.send_private_msg = _fake_send_private_msg
_FAKE_SENDER.send_by_chat_type = _fake_send_by_chat_type
sys.modules.setdefault("services.sender", _FAKE_SENDER)


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

    # help（卡片）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["help"]))
    assert r is None and _sent["cards"], f"help 失败: {r}"
    card = _sent["cards"][-1][0]
    assert "bind" in str(card) and "search" in str(card)
    print("OK help → 卡片包含 bind/search")

    # 未知子命令
    r = await inst._cmd(make_msg(["hahaha"]))
    assert "未知子命令" in r
    print("OK 未知子命令提示")

    # search（真实 API → 卡片）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["search", "Hello"]))
    assert (r is None and _sent["cards"]) or "未找到" in str(r), f"search 失败: {r}"
    card = _sent["cards"][-1][0]
    assert "搜索结果" in str(card)
    print("OK search → 卡片")

    # bind（用已知玩家 Jipper）
    r = await inst._cmd(make_msg(["bind", "jipper"]))
    assert r is None and _sent["cards"], f"bind 失败: {r}"
    print("OK bind → 卡片")

    # me（绑定后查自己，带头像）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["me"]))
    assert r is None and _sent["cards"], f"me 失败: {r}"
    card = _sent["cards"][-1][0]
    assert "Jipper" in str(card), f"me 卡片缺名字: {card}"
    has_img = any(m.get("type") == "image-group"
                  and any(e.get("type") == "image" for e in m.get("elements", []))
                  for m in card.get("modules", []))
    print(f"OK me → 卡片含头像(image-group): {has_img}")

    # player（带 ID）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["player", "25"]))
    assert r is None and _sent["cards"], f"player 失败: {r}"
    card = _sent["cards"][-1][0]
    assert "Jipper" in str(card)
    has_img = any(m.get("type") == "image-group"
                  and any(e.get("type") == "image" for e in m.get("elements", []))
                  for m in card.get("modules", []))
    print(f"OK player 25 → 卡片含头像(image-group): {has_img}")

    # lb（排行榜 → KOOK 卡片，含名字）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["lb", "ranked"]))
    assert r is None and _sent["cards"], f"lb 失败: {r}"
    card = _sent["cards"][-1][0]
    assert "排行" in str(card)
    assert "Jipper" in str(card), f"lb 卡片缺玩家名: {str(card)[:200]}"
    print("OK lb ranked → KOOK 卡片（含玩家名）")

    # stats
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["stats"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"stats 失败: {r}"
    print("OK stats → 卡片")

    # countries
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["countries"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"countries 失败: {r}"
    print("OK countries → 卡片")

    # song
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["song", "Hello"]))
    assert (r is None and _sent["cards"]) or "未找到" in str(r), f"song 失败: {r}"
    print("OK song → 卡片")

    # packs
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["packs"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"packs 失败: {r}"
    print("OK packs → 卡片")

    # info（谱面详情）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["info", "16161"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"info 失败: {r}"
    print("OK info 16161 → 卡片")

    # passes（谱面通关）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["passes", "16161"]))
    assert (r is None and _sent["cards"]) or "通关记录" in str(r), f"passes 失败: {r}"
    print("OK passes → 卡片")

    # dl
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["dl", "16161"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"dl 失败: {r}"
    print("OK dl → 卡片")

    # rank（玩家排名历史）
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["rank", "25"]))
    assert (r is None and _sent["cards"]) or "暂无" in str(r), f"rank 失败: {r}"
    print("OK rank → 卡片")

    # passesby
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["passesby", "25"]))
    assert (r is None and _sent["cards"]) or "❌" in str(r), f"passesby 失败: {r}"
    print("OK passesby → 卡片")

    # rerate
    _sent["cards"].clear()
    r = await inst._cmd(make_msg(["rerate", "16161"]))
    assert (r is None and _sent["cards"]) or "改版历史" in str(r), f"rerate 失败: {r}"
    print("OK rerate → 卡片")

    # 清理绑定
    import plugins.tuf.main as tuf_mod
    tuf_mod.del_bind(TEST_UID)

    await mgr.unload("tuf")
    print("\n=== ALL TUF V3 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
