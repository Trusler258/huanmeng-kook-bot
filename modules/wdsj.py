"""
洛花星雨战绩查询模块
- API 查询 + HTML 卡片渲染
"""
from __future__ import annotations

from pathlib import Path
from core.logger import get_logger

logger = get_logger("wdsj_mod")

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "data" / "templates" / "wdsj_card.html"


def build_card_html(data: dict) -> str:
    """根据 API 返回数据构造 HTML 卡片内容"""
    player = data.get("player", {})
    values = data.get("values", {})
    labels = data.get("labels", {})
    header_cards = data.get("headerCards", [])
    display_name = data.get("displayName", "?")

    parts = []

    # Header
    parts.append("""<div class="header">
      <div class="header-icon">WS</div>
      <div class="header-info">
        <h2>洛花星雨战绩</h2>
        <div class="sub">wdsj.net Nexus API</div>
      </div>
    </div>""")

    # Player + Template row
    player_name = player.get("name", "?")
    uid = player.get("uid", "?")
    parts.append(f"""<div class="player-row">
      <div class="player-card"><div class="label">玩家</div><div class="value">{player_name}</div></div>
      <div class="template-card"><div class="label">模式</div><div class="value">{display_name}</div></div>
    </div>""")

    # Stats grid
    if values:
        parts.append('<div class="stats-grid">')
        for key, val in list(values.items())[:9]:
            label = labels.get(key, key)
            cls = ""
            if key in ("kills", "wins", "score"):
                cls = " highlight"
            elif key in ("deaths", "losses"):
                cls = " danger"
            parts.append(f'<div class="stat-item"><div class="stat-label">{label}</div><div class="stat-value{cls}">{val}</div></div>')
        parts.append('</div>')

    # Info chips
    if header_cards:
        parts.append('<div class="info-cards">')
        for card in header_cards:
            if card["key"] in ("player", "template"):
                continue
            parts.append(f'<div class="info-chip"><span class="chip-label">{card["label"]}</span> <span class="chip-value">{card["value"]}</span></div>')
        parts.append('</div>')

    # Footer
    parts.append('<div class="footer">Powered by wdsj.net Nexus | 幻梦 QQ Bot</div>')

    content = "\n".join(parts)
    tmpl = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return tmpl.replace("${CARD_CONTENT}", content)


async def render_wdsj_card(data: dict) -> str | None:
    """生成战绩卡片图片"""
    html = build_card_html(data)
    from modules.changelog import render_card_to_image
    import uuid
    filename = f"wdsj_{data.get('player',{}).get('name','unknown')}_{uuid.uuid4().hex[:8]}.jpg"
    return await render_card_to_image(html, filename, width=680)
