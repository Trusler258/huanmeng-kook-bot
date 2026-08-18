"""
Phase 20 Hotfix H：插件工具机制端到端验证
验证链路：register_tool 注册 → 路由发现 → load_fc_schemas 出 Schema → check_permission 放行 → execute_tool 分发到插件 handler
运行: python tests/_test_plugin_tool.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK = "✅ PASS"
FAIL = "❌ FAIL"


def check(name, cond, detail=""):
    print(f"{OK if cond else FAIL} {name}" + (f"  ({detail})" if detail else ""))
    return cond


async def main():
    results = []

    # ── 1. 插件注册一个工具 ─────────────────────────────
    from core.plugin.api import PluginCapability

    pc = PluginCapability("demo")

    async def demo_echo(arguments, user_id, group_id, sender_name, is_group, bot_qq):
        return f"echo:{arguments.get('text', '')}|u={user_id}|g={group_id}"

    pc.register_tool(
        name="demo_echo",
        description="把文本原样复述/echo 出来。用户说 echo 或复述某句话时调用。",
        schema={"type": "function", "function": {
            "name": "demo_echo",
            "description": "把文本原样复述/echo 出来。用户说 echo 或复述某句话时调用。",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string"}},
                           "required": ["text"]},
        }},
        handler=demo_echo,
    )

    # ── 2. 注册表可查到插件工具 ─────────────────────────
    from core.capability.registry import get_capability_registry
    reg = get_capability_registry()
    cap = reg.find_plugin_tool("demo_echo")
    results.append(check("find_plugin_tool 找到插件工具", cap is not None))
    results.append(check("source 以 plugin: 开头", cap and cap.source.startswith("plugin:")))
    schema = reg.get_tool_schema(cap.id) if cap else None
    results.append(check("schema 已绑定", bool(schema)))
    results.append(check("handler 已绑定", reg.get_handler(cap.id) is not None))

    # ── 3. 路由：精准，该调用才调用 ─────────────────────
    from core.capability.router import get_capability_router
    router = get_capability_router()
    # 普通聊天意图 → 不带插件工具（只在 always_on 里）
    chat_caps = router.route("你好呀", "chat", is_group=True)
    chat_has = any(c.id == cap.id for c in chat_caps) if cap else True
    results.append(check("普通聊天不暴露插件工具(精准)", not chat_has))
    # 工具意图 + 命中描述关键词 → 带出插件工具
    tool_caps = router.route("帮我 echo 一下这句话", "tool", is_group=True)
    tool_has = any(c.id == cap.id for c in tool_caps) if cap else False
    results.append(check("相关请求路由出插件工具", tool_has))

    # ── 4. load_fc_schemas 合并插件 Schema ─────────────
    from core.capability.loader import load_fc_schemas
    schemas = load_fc_schemas(tool_caps)
    names = [s.get("function", {}).get("name") for s in schemas]
    results.append(check("fc_schemas 含插件工具", "demo_echo" in names))

    # ── 5. 权限放行 ────────────────────────────────────
    from core.tool_runtime.permission import check_permission
    allowed, reason = check_permission("demo_echo")
    results.append(check("check_permission 放行插件工具", allowed, reason))

    # ── 6. execute_tool 分发到插件 handler ─────────────
    from core.tools import execute_tool
    out = await execute_tool("demo_echo", {"text": "喵"}, 10001, 20002,
                             "测试", True, 12345)
    results.append(check("execute_tool 调用插件 handler", out == "echo:喵|u=10001|g=20002", str(out)))

    # ── 7. 卸载清理 ────────────────────────────────────
    pc.unregister_all()
    results.append(check("unregister_all 后清理", reg.find_plugin_tool("demo_echo") is None))

    print("\n==== 结果:", "全部通过" if all(results) else f"{sum(not r for r in results)} 项失败", "====")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
