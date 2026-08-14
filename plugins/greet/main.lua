-- 示例 Lua 插件：注册 .pgreet 命令
bridge.command("pgreet", "问候：.pgreet <名字>")

function cmd_pgreet(msg)
    local name = msg.args and msg.args[1]
    if not name then
        return "用法：.pgreet <名字>"
    end
    return bridge.config("greeting", "你好") .. " " .. tostring(name)
end

-- 订阅事件：业务可发布自定义事件触发自动回复
bridge.on_event("greet.ping", function(e)
    bridge.publish("greet.pong", { pong = true })
end)