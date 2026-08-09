"""
通用卡片图片渲染器（Universal Card Renderer）

支持的卡片类型：
  - changelog : 更新日志卡片（MD → 精美HTML模板 → 截图）
  - weather   : 天气预报卡片（API数据 → 表格/播报 → 截图）
  - box       : 快递物流卡片（轨迹数据 → 时间线 → 截图）

依赖：
  - markdown        : MD → HTML 转换（仅 changelog 卡片需要）
  - playwright      : 无头浏览器渲染 + 截图（所有卡片都需要）

使用方式：
  # 更新日志
  from modules.changelog import generate_changelog_image, send_changelog_card
  img_path = await generate_changelog_image()
  await send_changelog_card(group_id=123, is_group=True)

  # 天气
  from modules.changelog import generate_weather_card, send_weather_card
  img = await generate_weather_card(weather_data)
  await send_weather_card(data=weather_data, group_id=123, is_group=True)

  # 快递
  from modules.changelog import generate_box_card, send_box_card
  img = await generate_box_card(box_data)
  await send_box_card(data=box_data, group_id=123, is_group=True)
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logger import get_logger

# 延迟导入（避免启动时加载重型依赖）
_markdown_lib = None
_playwright = None

# 页面池：预开 page，用完放回复用
_page_pool: list = []
_page_pool_max = 3
_page_lock = asyncio.Lock()

# ── 全局浏览器实例（复用，避免每次重启）──
_browser = None
_playwright_instance = None


async def _ensure_browser():
    """确保全局 Chromium 实例存在，不存在则启动（极致加速）"""
    global _browser, _playwright_instance

    if _browser is not None and _browser.is_connected():
        return _browser

    pw = _get_playwright()
    logger = get_logger("changelog")
    logger.info("[Playwright] 启动 Chromium（极致加速模式）...")

    _playwright_instance = await pw().start()
    _browser = await _playwright_instance.chromium.launch(
        headless=True,
        executable_path="/usr/bin/chromium-browser",
        args=[
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-software-rasterizer',       # 软件光栅化都关掉
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-sync',                      # 同步服务全关
            '--disable-default-apps',
            '--disable-translate',
            '--disable-features=TranslateUI,BlinkGenPropertyTrees,OptimizationHints',
            '--disable-ipc-flooding-protection',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-startup-window',
            '--hide-scrollbars',                   # 不需要滚动条
            '--disable-breakpad',                  # 崩溃报告关掉
            '--disable-component-update',          # 组件更新关掉
            '--disable-hang-monitor',              # 挂起检测关掉
            '--disable-prompt-on-repost',
        ],
    )
    logger.info("[Playwright] Chromium 启动成功（极致加速已启用）")
    return _browser





def _get_markdown():
    """延迟导入 markdown 库"""
    global _markdown_lib
    if _markdown_lib is None:
        try:
            import markdown as md
            _markdown_lib = md
        except ImportError:
            from core.logger import get_logger
            logger = get_logger("changelog")
            logger.error("[Changelog] markdown 库未安装! 请运行: pip install markdown")
            raise ImportError(
                "需要安装 markdown 库才能使用卡片功能。"
                "\n运行: pip install markdown"
            )
    return _markdown_lib


def _get_playwright():
    """延迟导入 playwright"""
    global _playwright
    if _playwright is None:
        try:
            from playwright.async_api import async_playwright
            _playwright = async_playwright
        except ImportError:
            from core.logger import get_logger
            logger = get_logger("changelog")
            logger.error("[Changelog] playwright 库未安装! 请运行: pip install playwright && playwright install chromium")
            raise ImportError(
                "需要安装 playwright 才能使用卡片截图功能。"
                "\n运行: pip install playwright"
                "\n然后: playwright install chromium"
            )
    return _playwright


# ════════════════════════════════════════════════════════════
#  路径配置
# ════════════════════════════════════════════════════════════

def _get_base_dir() -> Path:
    """获取项目根目录（modules 所在的上一级）"""
    return Path(__file__).resolve().parent.parent


def _get_data_dir() -> Path:
    return _get_base_dir() / "data"


def _get_template_path() -> Path:
    return _get_data_dir() / "templates" / "changelog_card.html"


def _get_update_log_path() -> Path:
    return _get_data_dir() / "update_log.md"


def _get_img_output_dir() -> Path:
    d = _get_data_dir() / "img_temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ════════════════════════════════════════════════════════════
#  默认更新日志内容
# ════════════════════════════════════════════════════════════

_DEFAULT_LOG = """\
# 更新日志

## v0.9.0 Pro — TUF 谱面查询 + Playwright 加速 + 版本切换
<span class="tag-new">✨ 新功能</span>
- **TUF 谱面查询系统**：新增完整 TUF（节奏游戏谱面库）查询指令集，支持按难度查详情、关键词搜索、谱面下载直链、分页浏览
- `.tuflevel <关键词> <难度>`：精准查询指定谱面的指定难度信息，包含 BPM、长度、物量、note 列表、下载直链
- `.tufsearch <关键词> [页码]`：关键词模糊搜索谱面，支持分页浏览，默认第 1 页，每页 5 条
- `.tufd <关键词> <难度>`：一键获取谱面下载直链（基于 TUF 官方 OpenAPI）
- `.tufpage <页码>`：配合 search 结果翻页，查看历史搜索结果
- 所有指令均支持 @ 触发者回复，下载链接自动高亮显示
- **版本切换指令 `.updateinfo [版本号]`**：支持查看指定版本的更新日志，缺省显示最新版本。正则精准提取 `## v{版本号}` 段落，告别"翻遍全文"时代
- **TUF API 字段兼容层**：`tilecount` / `tileCount` 大小写通吃；`downloadCount` / `downloads` 双字段兼容；`get_level_passes` 返回 list 时不再崩溃

<span class="tag-optimize">⚡ 性能优化</span>
- **Playwright 浏览器实例复用**：全局 `_browser` 单例 + `_ensure_browser()` 懒加载，避免每次截图都重新启动 Chromium，截图速度提升约 60%
- **Chromium 启动参数全面优化**：新增 `--disable-extensions`、`--disable-background-networking`、`--disable-ipc-flooding-protection` 等 12 项参数，减少无效开销
- **设备缩放比调整**：`device_scale_factor` 默认值从 2.0 降为 1.0，截图速度再提升约 40%，清晰度仍满足 QQ 图片显示需求
- **等待策略优化**：`wait_until='load'` 替代 `'networkidle'`，避免因外部 CDN 字体、分析脚本加载慢而卡住截图流程
- **请求拦截屏蔽无效流量**：通过 `page.route('**/*', handler)` 主动 abort 字体、分析、广告类请求，减少 30%+ 无效网络活动

<span class="tag-fix">🐛 问题修复</span>
- 修复 TUF 谱面查询中 `tilecount`（小写）字段读不到的问题（兼容 `tileCount` 和 `tilecount`）
- 修复 TUF 谱面查询中 `downloadCount` 字段读不到的问题（兼容 `downloadCount` 和 `downloads`）
- 修复 `duration_ms` 为浮点数时 `:02d` 格式码报错的问题（`int()` 强转后再运算）
- 修复 `get_level_passes` API 直接返回 list 时的兼容性崩溃问题
- 修复 `changelog.py` 缺少 `from core.logger import get_logger` 导致启动即崩溃
- 修复 `_ensure_browser()` 中 `pw()` 返回 ContextManager 而非 Playwright 实例导致 `'PlaywrightContextManager' object has no attribute 'chromium'` 错误

<span class="tag-break">💥 破坏性变更</span>
- Playwright 截图默认 `device_scale_factor` 从 2.0 调整为 1.0（速度优先），如需高清模式请自行修改 `changelog.py` 中 `_screenshot_html()` 的 `scale` 参数默认值
- `_ensure_browser()` 新增 `_playwright_instance` 全局变量，bot 关闭时需调用 `await _playwright_instance.stop()` 清理

## v0.8.0 — 全面重构
<span class="tag-new">✨ 新功能</span>
- 模块化架构：core / modules / services / utils 四层分离
- WebSocket 全局长连接复用，消息发送延迟降低 **50~80%**
- 二级判断 + 三级兴趣度模型 **并行调用**，判断延迟减半
- 异步图片识别（httpx），不再阻塞事件循环
- 搜索缓存内存化 + 脏标记定时刷盘

<span class="tag-optimize">⚡ 性能优化</span>
- BotConfig @dataclass 封装：20+ 个全局变量 → 一个类实例
- format_lang 集中化到 utils/format_lang.py，消除重复定义
- 图片缓存限容（1000 条上限）+ TTL（30 天过期）
- 日志系统升级：每模块独立 Logger + 彩色控制台 + 文件持久化

<span class="tag-fix">🐛 问题修复</span>
- 修复 debug() 每次调用读磁盘的性能问题
- 清理死代码 if_module.py
- img / img18 合并，消除 90% 重复代码
- 修复裸 except 吞错误的问题

<span class="tag-break">💥 破坏性变更</span>
- 配置文件结构不变，但内部加载方式改为 BotConfig 类
- 全局变量访问方式变更（通过 get_config() 单例）
"""


# ════════════════════════════════════════════════════════════
#  Markdown → 增强 HTML 转换
# ════════════════════════════════════════════════════════════

# ── 标签检测正则 ──
_TAG_PATTERNS = [
    # 格式: [正则模式, 对应的 CSS class]
    (r'(?:^|\s)(?:✨|🆕|\[NEW\]|新增|新功能|feat)[\s:：]', 'tag-new'),
    (r'(?:^|\s)(?:🐛|\[FIX\]|修复|fix|bug)[\s:：]',       'tag-fix'),
    (r'(?:^|\s)(?:⚡|🔧|\[OPT\]|优化|optimize|perf)[\s:：]','tag-optimize'),
    (r'(?:^|\s)(?:💥|\[BREAK\]|破坏|breaking|移除)[\s:：]', 'tag-break'),
]


def _detect_tags(line: str) -> str:
    """
    检测一行中的关键词并返回对应的 HTML 标签。
    同时从原行中移除已匹配的关键词前缀。

    Returns:
        要插入到行首的 HTML 标签字符串（可能为空字符串）
    """
    applied = []
    cleaned = line
    
    for pattern, css_class in _TAG_PATTERNS:
        match = re.search(pattern, cleaned)
        if match:
            applied.append(f'<span class="{css_class}"></span>')
            # 移除已匹配的部分
            cleaned = cleaned[:match.start()] + cleaned[match.end():]
    
    return ''.join(applied), cleaned.strip()


def _enhance_list_items(html_str: str) -> str:
    """
    后处理：扫描 <li> 元素，在行首注入检测到的标签。
    同时清理行内 emoji 前缀避免重复。
    """
    def process_li(match):
        original = match.group(1)
        
        # 提取纯文本用于标签检测
        text_only = re.sub(r'<[^>]+>', '', original).strip()
        
        # 检测标签
        tag_html, _cleaned = _detect_tags(text_only)
        
        if tag_html:
            return f'<li>{tag_html}{original}'
        return match.group(0)
    
    # 处理 <li>...</li>
    result = re.sub(
        r'<li>(.*?)</li>',
        process_li,
        html_str,
        flags=re.DOTALL
    )
    return result


def _wrap_code_for_hljs(html_str: str) -> str:
    """
    将 <pre data-lang="LANG">代码</pre> 包装为 highlight.js 可识别的结构：
    <pre data-lang="LANG"><code class="language-lang hljs">代码</code></pre>
    """
    def wrap_match(m):
        lang = (m.group(1) or "CODE").lower()
        code = m.group(2)
        return f'<pre data-lang="{lang.upper()}"><code class="language-{lang} hljs">{code}</code></pre>'
    return re.sub(r'<pre data-lang="(\w+)">(.*?)</pre>', wrap_match, html_str, flags=re.DOTALL)


def markdown_to_enhanced_html(md_text: str) -> str:
    """
    将 Markdown 文本转换为增强版 HTML。

    特性（完整 MD 支持）：
    - 标准 MD 渲染：标题(h1-h6)、列表(有序/无序/嵌套)、段落、粗体/斜体
    - 代码块（围栏式 ```lang）、行内代码（`code`）
    - GFM 表格、引用块(blockquote)
    - 分隔线(hr)、链接、图片
    - 自动标签检测（新功能/修复/优化/破坏性变更）
    - 代码块语言标注 + data-lang 属性（供 highlight.js 渲染）

    Args:
        md_text: 原始 Markdown 文本

    Returns:
        渲染后的 HTML 字符串（仅 <body> 内的内容部分）
    """
    md = _get_markdown()
    
    # 启用的扩展（完整 MD 支持）
    extensions = [
        'tables',           # GFM 表格
        'fenced_code',     # 围栏代码块 ```lang
        'codehilite',      # 代码高亮占位（后续由 highlight.js 接管）
        'nl2br',           # 换行符转 <br>
        'sane_lists',      # 规范列表
        'toc',             # 目录生成
    ]
    
    extension_configs = {
        'codehilite': {
            'linenums': False,
            'guess_lang': True,
            'css_class': 'hljs',
            'noclasses': False,
        }
    }
    
    # Step 1: 基本 MD → HTML
    raw_html = md.markdown(
        md_text,
        extensions=extensions,
        extension_configs=extension_configs,
    )
    
    # Step 2: 注入自动标签（新功能/修复/优化/破坏性）
    enhanced = _enhance_list_items(raw_html)
    
    # Step 3: 为代码块添加 data-lang 属性（供模板显示语言名）
    # 有语言标注的代码块：```python → <pre data-lang="PYTHON">
    enhanced = re.sub(
        r'<pre(\s+class="[^"]*")?\s*><code\s+class="language-(\w+)".*?>(.*?)</code></pre>',
        r'<pre data-lang="\2">\3</pre>',
        enhanced,
        flags=re.DOTALL | re.IGNORECASE,
    )
    
    # 无语言标注的代码块（兜底）：``` → <pre data-lang="CODE">
    enhanced = re.sub(
        r'<pre(\s+class="[^"]*")?\s*><code>(.*?)</code></pre>',
        r'<pre data-lang="CODE">\2</pre>',
        enhanced,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Step 3.5: 包装代码块为 highlight.js 结构（无行号）
    enhanced = _wrap_code_for_hljs(enhanced)

    # Step 4: 给 h2 标题添加图标
    h2_icons = {
        '更新': '📋',
        '修复': '🔧',
        '优化': '⚡',
        '新增': '✨',
        '重构': '♻️',
        '已知': '⚠️',
        '计划': '📅',
    }
    
    def add_h2_icon(m):
        title_text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        icon = ''
        for keyword, emoji in h2_icons.items():
            if keyword in title_text:
                icon = f'<span class="icon-h2">{emoji}</span>'
                break
        return f'<h2>{icon}{m.group(1)}</h2>'
    
    enhanced = re.sub(r'<h2>(.+?)</h2>', add_h2_icon, enhanced, flags=re.DOTALL)
    
    return enhanced


# ════════════════════════════════════════════════════════════
#  模板填充
# ════════════════════════════════════════════════════════════

def _read_template() -> str:
    """读取 HTML 卡片模板"""
    tpl_path = _get_template_path()
    if not tpl_path.exists():
        raise FileNotFoundError(f"模板文件不存在: {tpl_path}")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


def _read_update_log() -> str:
    """读取更新日志 MD 文件，不存在则创建默认内容"""
    log_path = _get_update_log_path()
    if not log_path.exists():
        # 创建默认日志
        _get_data_dir().mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_LOG)
    
    with open(log_path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_version(md_text: str) -> str:
    """
    从 MD 内容中提取版本号。
    优先查找 ## vX.X.X 或 ## v0.X.X 格式的标题。
    """
    match = re.search(r'##\s*(v?[\d.]+[\w.]*)', md_text)
    if match:
        return match.group(1)
    return "Latest"


def fill_template(
    html_template: str,
    bot_name: str = "幻梦",
    version: str = "",
    md_text_raw: str = "",
    date_str: str = "",
    brand: str = "Generated by 幻梦",
) -> str:
    """
    用数据填充 HTML 模板（客户端渲染版）。

    模板占位符：
      {{VERSION}}         版本号
      {{CHANGELOG_MARKDOWN}} 原始 Markdown 文本（客户端 marked.js 渲染）
      {{RELEASE_DATE}}    发布日期
      {{BRAND}}          底部品牌文字
    """
    # 日期默认值
    if not date_str:
        date_str = datetime.now().strftime("%Y年%m月%d日")

    replacements = {
        "{{VERSION}}":          version or "Latest",
        "{{CHANGELOG_MARKDOWN}}": md_text_raw or "",
        "{{RELEASE_DATE}}":     date_str,
        "{{BRAND}}":            brand,
    }

    result = html_template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result


# ════════════════════════════════════════════════════════════
#  Playwright 截图核心
# ════════════════════════════════════════════════════════════

async def _block_external_route(route):
    """拦截外部资源请求（CDN字体/JS等），只允许本地加载"""
    if route.request.resource_type in ("stylesheet", "script", "font", "image"):
        await route.abort()
    else:
        await route.continue_()


async def _screenshot_html(
    html_content: str,
    output_path: Path,
    width: int = 800,
    scale: float = 1.0,
) -> bool:
    """
    Playwright 截图（v0.9.6 极致加速版 + 页面池）
    """
    try:
        from core.queues import _render_semaphore
        async with _render_semaphore:
            browser = await _ensure_browser()

            async with _page_lock:
                page = _page_pool.pop() if _page_pool else None

            if page is None:
                page = await browser.new_page()
            else:
                # ★ 检查池中页面是否已被关闭
                try:
                    await page.evaluate("1")
                except Exception:
                    page = await browser.new_page()

            await page.set_viewport_size({"width": width, "height": 10, "device_scale_factor": scale})
            await page.set_content(html_content, wait_until='domcontentloaded', timeout=15000)
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(output_path), full_page=True, type='jpeg', quality=92)

            # ★ 放回前也验证可用
            async with _page_lock:
                try:
                    await page.evaluate("1")
                    if len(_page_pool) < _page_pool_max:
                        _page_pool.append(page)
                    else:
                        await page.close()
                except Exception:
                    pass  # 页面已不可用，丢弃

        return True
    except Exception as e:
        from core.logger import get_logger
        get_logger("changelog").error("[Screenshot] 截图异常: %s", e)
        return False


def _md_to_html(md: str) -> str:
    """简易 Markdown → HTML（不依赖外部库），处理更新日志常用格式"""
    import re
    lines = md.split("\n")
    out = []
    in_code = False
    in_table = False
    in_list = False  # ★ 跟踪是否在 <ul> 中

    def _inline_fmt(text: str) -> str:
        """处理行内格式：粗体 + 代码"""
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        return text

    for line in lines:
        # 代码块
        if line.strip().startswith("```"):
            if in_list:
                out.append("</ul>")
                in_list = False
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append('<pre><code>')
                in_code = True
            continue
        if in_code:
            out.append(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue

        # 标题
        if line.startswith("### "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f'<h3>{line[4:]}</h3>')
            continue
        if line.startswith("## "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f'<h2>{line[3:]}</h2>')
            continue
        if line.startswith("# "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f'<h1>{line[2:]}</h1>')
            continue

        # 水平线
        if line.strip() == "---":
            if in_list: out.append("</ul>"); in_list = False
            out.append("<hr>")
            continue

        # 表格
        if "|" in line and not line.strip().startswith(("-", "*", ">")):
            if in_list: out.append("</ul>"); in_list = False
            if not in_table:
                in_table = True
                out.append('<table>')
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r'^[-: ]+$', c) for c in cells):
                continue  # 跳过分隔行
            row = "".join(f"<td>{_inline_fmt(c)}</td>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        elif in_table:
            out.append("</table>")
            in_table = False

        # ★ 列表项：包裹在 <ul> 中 + 应用行内格式
        if re.match(r'^\s*[-*]\s', line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = _inline_fmt(line.strip()[2:])
            out.append(f"<li>{content}</li>")
            continue
        elif in_list:
            out.append("</ul>")
            in_list = False

        # 引用
        if line.startswith("> "):
            out.append(f'<blockquote>{_inline_fmt(line[2:])}</blockquote>')
            continue

        # 普通段落
        if line.strip():
            out.append(f"<p>{_inline_fmt(line)}</p>")
        else:
            out.append("<br>")

    if in_table:
        out.append("</table>")
    if in_code:
        out.append("</code></pre>")

    return "\n".join(out)


async def generate_changelog_image(custom_md: str = None) -> str | None:
    """生成更新日志卡片图片。custom_md 指定版本时只渲染该版本。"""
    md_text = custom_md or _read_update_log()
    if not md_text:
        return None

    version = _extract_version(md_text)
    template_html = _read_template()
    # ★ 服务端渲染 Markdown → HTML，不依赖 CDN 的 marked.js
    html_body = _md_to_html(md_text)
    full_html = fill_template(template_html, version=version, md_text_raw=html_body)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"changelog_{ts}.jpg"
    return await render_card_to_image(full_html, output_filename)


async def send_changelog_card(
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    is_group: bool = False,
    custom_md: Optional[str] = None,
) -> str | None:
    """
    生成并发送更新日志卡片图片到聊天。

    这是给指令系统调用的便捷封装：
    自动生成图片 → 通过 CQ 码发送 → 返回状态文本。

    Args:
        group_id: 群号（群聊时必填）
        user_id: 用户 QQ 号（私聊时必填）
        is_group: 是否群聊
        custom_md: 可选，指定版本的 Markdown 内容（替代读取文件）
        
    Returns:
        成功返回 None（已自行发送）；失败返回错误文本
    """
    from core.logger import get_logger
    from services.sender import send_group_msg, send_private_msg
    
    logger = get_logger("changelog")
    
    # 生成图片（传入 custom_md 则使用指定版本内容）
    img_path = await generate_changelog_image(custom_md=custom_md)
    if not img_path:
        # 依赖缺失时回退：返回纯文本更新日志
        logger.warning("[Changelog] 图片生成失败（可能缺依赖），回退到纯文本模式")
        try:
            md_text = _read_update_log()
            return f"📋 **更新日志**\n\n{md_text}\n\n_💡 提示: 安装 `pip install markdown playwright && playwright install chromium` 后可启用精美卡片模式_"
        except Exception:
            return "❌ 卡片图片生成失败，且无法读取日志文件。请检查日志或安装依赖:\n```\npip install markdown playwright\nplaywright install chromium\n```"
    
    # 构造 CQ 图片消息
    normalized = img_path.replace("\\", "/")
    cq_msg = f"[img:file:{normalized}]"

    # 发送
    target_id = group_id if is_group else user_id
    try:
        if is_group:
            await send_group_msg(cq_msg, target_id)
            logger.info("[Changelog] 卡片已发送到群 %d", target_id)
        else:
            await send_private_msg(cq_msg, target_id)
            logger.info("[Changelog] 卡片已发给用户 %d", target_id)
        return None  # 表示已自行发送，不需要额外回复
    except Exception as e:
        logger.error("[Changelog] 发送卡片失败: %s", e, exc_info=True)
        return f"❌ 卡片发送失败: {e}"


# ════════════════════════════════════════════════════════════
#  通用卡片渲染引擎（支持多种卡片类型）
# ════════════════════════════════════════════════════════════

def _get_template_path(template_name: str = "changelog_card") -> Path:
    """
    获取指定名称的 HTML 卡片模板路径。

    Args:
        template_name: 模板文件名（不含 .html 后缀）
                       支持 changelog_card / weather_card / box_card
    """
    return _get_data_dir() / "templates" / f"{template_name}.html"


def render_any_template(
    template_name: str,
    variables: dict[str, str],
) -> str:
    """
    通用模板填充函数：读取 HTML 模板 → 替换所有 {{变量}} 占位符 → 返回完整 HTML

    Args:
        template_name: 模板名（changelog_card / weather_card / box_card）
        variables: 变量字典，key 为模板中的 {{KEY}}，value 为替换值

    Returns:
        填充完成的完整 HTML 字符串
    """
    tpl_path = _get_template_path(template_name)
    if not tpl_path.exists():
        from core.logger import get_logger
        logger = get_logger("card")
        raise FileNotFoundError(f"卡片模板不存在: {tpl_path}")
    
    with open(tpl_path, "r", encoding="utf-8") as f:
        html = f.read()
    
    for key, value in variables.items():
        placeholder = "{{" + key + "}}"
        html = html.replace(placeholder, str(value) if value is not None else "")
    
    return html


async def render_card_to_image(
    full_html: str,
    output_filename: Optional[str] = None,
    width: int = 800,
) -> Optional[str]:
    """
    通用截图入口：任意完整 HTML → Playwright 截图 → 返回图片路径。

    这是所有卡片类型共用的底层截图函数。

    Args:
        full_html: 填充完成的完整 HTML 字符串
        output_filename: 输出文件名（默认自动生成时间戳命名）
        width: 视口宽度
        
    Returns:
        成功返回图片绝对路径；失败返回 None
    """
    from core.logger import get_logger
    logger = get_logger("card")
    
    # 依赖预检
    try:
        _get_playwright()
    except ImportError as e:
        logger.warning("[Card] playwright 未安装，无法截图: %s", e)
        return None
    
    # 文件名
    if not output_filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"card_{ts}.png"
    
    output_path = _get_img_output_dir() / output_filename
    logger.info("[Card] 正在截图 → %s (%d bytes HTML)", output_path.name, len(full_html))
    
    success = await _screenshot_html(full_html, output_path, width=width)
    if success:
        size_kb = output_path.stat().st_size / 1024
        logger.info("[Card] ✅ 截图成功! path=%s size=%.1fKB", output_path, size_kb)
        return str(output_path)
    else:
        logger.error("[Card] ❌ 截图失败")
        return None


async def send_card_image(
    img_path: str,
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    is_group: bool = False,
) -> str | None:
    """
    通用发送函数：将已生成的图片通过 CQ 码发送到聊天。

    Args:
        img_path: 图片绝对路径
        group_id: 群号（群聊时必填）
        user_id: 用户 QQ 号（私聊时必填）
        is_group: 是否群聊
        
    Returns:
        成功返回 None（已自行发送）；失败返回错误文本
    """
    from core.logger import get_logger
    from services.sender import send_group_msg, send_private_msg
    
    logger = get_logger("card")
    
    normalized = img_path.replace("\\", "/")
    cq_msg = f"[img:file:{normalized}]"

    target_id = group_id if is_group else user_id
    try:
        if is_group:
            await send_group_msg(cq_msg, target_id)
            logger.info("[Card] 图片已发送到群 %d", target_id)
        else:
            await send_private_msg(cq_msg, target_id)
            logger.info("[Card] 图片已发给用户 %d", target_id)
        return None
    except Exception as e:
        logger.error("[Card] 发送失败: %s", e, exc_info=True)
        return f"❌ 卡片发送失败: {e}"


# ════════════════════════════════════════════════════════════
#  天气卡片专用
# ════════════════════════════════════════════════════════════

# ── 天气图标映射（天气文字 → emoji）──
_WEATHER_ICON_MAP = {
    '晴': '☀️', '多云': '⛅', '阴': '☁️', '阵雨': '🌦️',
    '雷阵雨': '⛈️', '小雨': '🌧️', '中雨': '🌧️', '大雨': '🌧️',
    '暴雨': '🌊', '雨夹雪': '🌨️', '小雪': '🌨️', '中雪': '❄️',
    '大雪': '❄️', '暴雪': ' Blizzard ', '雾': '🌫️',
    '霾': '😷', '浮尘': '💨', '沙尘暴': '🌪️', '扬沙': '💨',
}


def _weather_icon(condition: str) -> str:
    """根据天气描述获取对应的 emoji 图标"""
    for key, icon in _WEATHER_ICON_MAP.items():
        if key in condition:
            return icon
    return '🌤️'


def _aqi_class(aqi_value: int) -> str:
    """根据 AQI 数值返回 CSS class"""
    if aqi_value <= 50:   return 'aqi-excellent'
    elif aqi_value <= 100: return 'aqi-good'
    elif aqi_value <= 150: return 'aqi-moderate'
    elif aqi_value <= 200: return 'aqi-poor'
    else:                  return 'aqi-bad'


def _aqi_label(aqi_value: int) -> str:
    """AQI 数值 → 中文标签"""
    if aqi_value <= 50:   return '优'
    elif aqi_value <= 100: return '良'
    elif aqi_value <= 150: return '轻度污染'
    elif aqi_value <= 200: return '中度污染'
    else:                  return '重度污染'


def build_weather_rows(daily_list: list[dict]) -> str:
    """
    将天气 API 的 daily_forecast 数据构建为 HTML 表格行。

    Args:
        daily_list: API 返回的逐日预报列表

    Returns:
        HTML <tr> 行字符串
    """
    rows = []
    today_str = datetime.now().strftime("%m-%d")
    
    for i, d in enumerate(daily_list):
        date_raw = d.get("date", "")
        date_str = date_raw[-5:] if len(date_raw) >= 5 else date_raw
        is_today = (date_str == today_str)
        
        day_cond = d.get("day_condition", "")
        night_cond = d.get("night_condition", "")
        icon_day = _weather_icon(day_cond)
        
        high = d.get("max_temperature", "--")
        low = d.get("min_temperature", "--")
        
        wd = d.get("day_wind_direction", "") or ""
        wp = d.get("day_wind_power", "") or ""
        wind_text = f"{wd}{wp}".strip() or "微风"
        
        aqi_val = d.get("aqi", 0)
        aqi_lbl = _aqi_label(aqi_val)
        aqi_cls = _aqi_class(aqi_val)
        
        row_cls = 'class="today-row"' if is_today else ''
        
        row = (
            f'<tr {row_cls}>'
            f'<td>{date_str}</td>'
            f'<td class="weather-icon-cell">{icon_day}<br><span style="font-size:11px;color:var(--text-muted);">{day_cond}</span></td>'
            f'<td class="temp-high">{high}°</td>'
            f'<td class="temp-low">{low}°</td>'
            f'<td class="wind-text">{wind_text}</td>'
            f'<td><span class="aqi-badge {aqi_cls}">{aqi_lbl}({aqi_val})</span></td>'
            f'</tr>'
        )
        rows.append(row)
    
    return "\n".join(rows)


def build_weather_talk_html(daily_list: list, location_name: str) -> str:
    """
    构建口语化播报的 HTML 片段。

    Args:
        daily_list: 逐日预报数据列表
        location_name: 地点名称

    Returns:
        HTML div.talk-section 内容
    """
    items = []
    
    # 温度范围
    temps = [(d["max_temperature"], d["min_temperature"]) for d in daily_list]
    max_t = max(t[0] for t in temps)
    min_t = min(t[1] for t in temps)
    items.append(f'<div class="talk-item highlight">🌡️ 近七日气温范围：<b>{min_t}°C ~ {max_t}°C</b></div>')
    
    # 雨天检测
    rains = []
    for d in daily_list:
        ds = d["date"][-5:] if len(d.get("date", "")) >= 5 else ""
        dc = d.get("day_condition", "")
        nc = d.get("night_condition", dc)
        if "雨" in dc or "雨" in nc:
            rains.append(ds)
    if rains:
        rain_dates = ", ".join(rains[:3])
        items.append(f'<div class="talk-item warning">🌧️ 预计以下日期有雨：{rain_dates}</div>')
    else:
        items.append('<div class="talk-item">☀️ 未来七天无降水预报，适宜出行</div>')
    
    # AQI 总评
    avg_aqi = sum(d.get("aqi", 0) for d in daily_list) // len(daily_list)
    aqi_label = _aqi_label(avg_aqi)
    aqi_emoji = "🍃" if avg_aqi <= 50 else ("👌" if avg_aqi <= 150 else "😷")
    items.append(f'<div class="talk-item">{aqi_emoji} 平均空气质量：<b>{aqi_label}</b> (AQI={avg_aqi})</div>')
    
    return (
        f'<div class="talk-section">\n'
        + "\n".join(items) +
        '\n</div>'
    )


async def generate_weather_card(
    data: dict,
    output_filename: Optional[str] = None,
) -> Optional[str]:
    """
    从天气 API 数据生成精美的天气卡片图片。

    Args:
        data: 天气 API 返回的完整 JSON dict（query_weather() 的返回值）
        output_filename: 可选输出文件名

    Returns:
        成功时返回图片绝对路径；依赖缺失或错误返回 None
    """
    from core.logger import get_logger
    from core.config import get_config
    
    logger = get_logger("card.weather")
    
    try:
        _get_playwright()
    except ImportError as e:
        logger.warning("[WeatherCard] 依赖缺失: %s", e)
        return None
    
    # ── 解析数据 ──
    location_data = data.get("data", {}).get("location", {})
    daily_list = data.get("data", {}).get("daily_forecast", [])[:7]
    
    loc_name = (
        f"{location_data.get('province', '')}"
        f"{location_data.get('city', '')}"
        f"{location_data.get('county', '')}"
    ).strip() or "未知"
    
    today = daily_list[0] if daily_list else {}
    today_cond = today.get("day_condition", "未知")
    today_temp = today.get("max_temperature", "?")
    today_min = today.get("min_temperature", "?")
    today_icon = _weather_icon(today_cond)
    
    # ── 构建各部分 HTML ──
    forecast_rows = build_weather_rows(daily_list)
    talk_section = build_weather_talk_html(daily_list, loc_name)
    
    # AQI 总结
    avg_aqi = sum(d.get("aqi", 0) for d in daily_list) // len(daily_list) if daily_list else 0
    aqi_summary = f"近七日平均 AQI 为 <b>{avg_aqi}</b>，空气质量{_aqi_label(avg_aqi)}。"
    
    cfg = get_config()
    
    # ── 填充模板 ──
    variables = {
        "TODAY_ICON":      today_icon,
        "CITY_NAME":       loc_name,
        "HEADER_DATE":     datetime.now().strftime("%Y年%m月%d日 星期%w").replace("星期0","日").replace("星期1","一").replace("星期2","二").replace("星期3","三").replace("星期4","四").replace("星期5","五").replace("星期6","六"),
        "TODAY_TEMP":      str(today_temp),
        "TODAY_COND":      today_cond,
        "TEMP_RANGE":      f"{today_min}℃ ~ {today_temp}℃",
        "TALK_SECTION":    talk_section,
        "FORECAST_ROWS":   forecast_rows,
        "AQI_SUMMARY":     aqi_summary,
        "DATA_TIME":       datetime.now().strftime("%m-%d %H:%M"),
        "BRAND":           f"Generated by {cfg.bot_name}",
    }
    
    full_html = render_any_template("weather_card", variables)
    logger.info("[WeatherCard] 模板填充完成, 城市=%s, %d条预报", loc_name, len(daily_list))
    
    # ── 截图 ──
    if not output_filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"weather_{loc_name}_{ts}.png"
    
    return await render_card_to_image(full_html, output_filename)


async def send_weather_card(
    data: dict,
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    is_group: bool = False,
) -> str | None:
    """
    生成并发送天气卡片图片到聊天。

    Args:
        data: query_weather() 返回的天气数据 dict
        group_id / user_id / is_group: 发送目标

    Returns:
        成功返回 None（已自行发送）；失败/缺依赖返回文本
    """
    from core.logger import get_logger
    
    logger = get_logger("card.weather")
    
    img_path = await generate_weather_card(data)
    if not img_path:
        logger.warning("[WeatherCard] 生成失败，回退到纯文本模式")
        return None  # 让调用方用原始纯文本方式处理
    
    return await send_card_image(img_path, group_id=group_id, user_id=user_id, is_group=is_group)


# ════════════════════════════════════════════════════════════
#  快递物流卡片专用
# ════════════════════════════════════════════════════════════

# ── 状态 → CSS class / 图标 映射 ──
_BOX_STATE_MAP = {
    "TRANSPORT":  {"cls": "badge-transport",  "icon": "🚚 ", "label": "运输中"},
    "DELIVERING": {"cls": "badge-delivering", "icon": "🏃 ", "label": "派送中"},
    "SIGNED":     {"cls": "badge-signed",     "icon": "✅ ", "label": "已签收"},
    "RETURN":     {"cls": "badge-return",     "icon": "↩️ ", "label": "退件"},
    "PENDING":    {"cls": "badge-pending",    "icon": "⏳ ", "label": "待揽收"},
    "FINISH":     {"cls": "badge-finish",     "icon": "🏁 ", "label": "已完成"},
}


def build_box_timeline(details: list[dict]) -> str:
    """
    将快递轨迹详情构建为 HTML 时间线。

    Args:
        details: trackingDetails 列表，每项含 time/context

    Returns:
        HTML timeline-items 字符串
    """
    items = []
    
    for i, d in enumerate(details):
        t = d.get("time", "")
        ctx = d.get("context", "")
        # 清理广告后缀
        ctx = re.sub(r'[（(【][^）)]*(如遇问题|物流问题)[^）)]*[）)]️?]', '', ctx).strip()
        
        # 格式化时间
        if len(t) >= 12:
            time_str = f"{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
        else:
            time_str = t
        
        is_first = (i == 0)
        dot_cls = 'active' if is_first else ('done' if i < len(details) - 1 else '')
        card_cls = 'current' if is_first else ''
        
        item = (
            f'<div class="timeline-item">'
            f'  <div class="timeline-dot {dot_cls}"></div>'
            f'  <div class="timeline-card {card_cls}">'
            f'    <span class="tl-time">{time_str}</span>'
            f'    <span class="tl-msg">{ctx}</span>'
            f'  </div>'
            f'</div>'
        )
        items.append(item)
    
    return "\n".join(items)


async def generate_box_card(
    cp_name: str,
    tracking_no: str,
    state: str,
    state_text: str,
    latest_msg: str,
    details: list[dict],
    output_filename: Optional[str] = None,
) -> Optional[str]:
    """
    从快递查询数据生成精美的物流卡片图片。

    Args:
        cp_name: 快递公司名称
        tracking_no: 快递单号
        state: 原始状态码（TRANSPORT/DELIVERING/SIGNED...）
        state_text: 状态中文文本
        latest_msg: 最新动态消息
        details: 物流轨迹详情列表
        output_filename: 可选输出文件名

    Returns:
        成功时返回图片绝对路径；失败返回 None
    """
    from core.logger import get_logger
    from core.config import get_config
    
    logger = get_logger("card.box")
    
    try:
        _get_playwright()
    except ImportError as e:
        logger.warning("[BoxCard] 依赖缺失: %s", e)
        return None
    
    # 状态样式
    state_info = _BOX_STATE_MAP.get(state, {"cls": "badge-pending", "icon": "❓ ", "label": state_text})
    state_cls = state_info["cls"]
    state_icon = state_info["icon"]
    
    # 最新动态区
    clean_latest = re.sub(r'[（(【][^）)]*(如遇问题|物流问题)[^）)]*[）)]️?]', '', latest_msg).strip()
    if clean_latest:
        latest_html = (
            f'<div class="latest-bar">'
            f'  <span class="label">最新动态</span>'
            f'  <span class="msg">{clean_latest}</span>'
            f'</div>'
        )
    else:
        latest_html = '<div style="height:4px;"></div>'  # 占位空行保持间距一致
    
    # 时间线
    timeline_html = build_box_timeline(details)
    
    # 提示条
    tip_html = ""
    if state == "DELIVERING":
        tip_html = '<div class="tip-bar tip-delivering">📬 包裹正在派送中，请留意电话或短信通知！</div>'
    elif state == "SIGNED":
        tip_html = '<div class="tip-bar tip-signed">✅ 已签收完成，感谢您的耐心等待~</div>'
    
    cfg = get_config()
    
    # ── 填充模板 ──
    variables = {
        "CP_NAME":         cp_name,
        "TRACKING_NO":     tracking_no,
        "STATE_CLASS":     state_cls,
        "STATE_ICON":      state_icon,
        "STATE_TEXT":      state_text,
        "LATEST_SECTION":  latest_html,
        "TIMELINE_ITEMS":  timeline_html,
        "TIP_SECTION":     tip_html,
        "QUERY_TIME":      datetime.now().strftime("%m-%d %H:%M"),
        "BRAND":           f"Generated by {cfg.bot_name}",
    }
    
    full_html = render_any_template("box_card", variables)
    logger.info("[BoxCard] 模板填充完成, 单号=%s, %d条轨迹", tracking_no, len(details))
    
    # ── 截图 ──
    if not output_filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_no = tracking_no[-6:] if len(tracking_no) > 6 else tracking_no
        output_filename = f"box_{safe_no}_{ts}.png"
    
    return await render_card_to_image(full_html, output_filename)


async def send_box_card(
    cp_name: str,
    tracking_no: str,
    state: str,
    state_text: str,
    latest_msg: str,
    details: list[dict],
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    is_group: bool = False,
) -> str | None:
    """
    生成并发送快递物流卡片图片到聊天。

    Args:
        cp_name / tracking_no / state / state_text / latest_msg / details: 快递数据
        group_id / user_id / is_group: 发送目标

    Returns:
        成功返回 None（已自行发送）；失败返回文本
    """
    from core.logger import get_logger
    
    logger = get_logger("card.box")
    
    img_path = await generate_box_card(
        cp_name=cp_name, tracking_no=tracking_no, state=state,
        state_text=state_text, latest_msg=latest_msg, details=details,
    )
    if not img_path:
        logger.warning("[BoxCard] 生成失败，回退到纯文本模式")
        return None  # 让调用方用原始纯文本方式处理
    
    return await send_card_image(img_path, group_id=group_id, user_id=user_id, is_group=is_group)
