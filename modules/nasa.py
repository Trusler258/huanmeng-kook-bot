"""
NASA APOD 指令
.nasa [日期]  获取 NASA 每日天文图片
"""
import asyncio

async def cmd_nasa(args, user_id, group_id, sender_name, is_group, bot_kook):
    from services.nasa import get_apod
    from services.sender import send_group_msg, send_private_msg

    date = args[0] if args else ""

    try:
        data = get_apod(date)
    except Exception as e:
        return f"NASA API 请求失败: {e}"

    title = data.get("title", "未知")
    explanation = data.get("explanation", "")[:500]
    hdurl = data.get("hdurl", data.get("url", ""))
    media = data.get("media_type", "image")
    apod_date = data.get("date", date or "today")

    lines = [f"[NASA APOD] {apod_date}: {title}", ""]
    if explanation:
        lines.append(explanation[:500])

    text = "\n".join(lines)

    # 图片直接发送
    if hdurl and media == "image":
        cq_img = f"[img:{hdurl}]"
        if is_group:
            await send_group_msg(f"{text}\n{cq_img}", group_id=group_id)
        else:
            await send_private_msg(f"{text}\n{cq_img}", user_id)
        return None
    elif hdurl and media == "video":
        return f"{text}\n\n视频链接: {hdurl}"
    else:
        return text
