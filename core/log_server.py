"""
Web 实时日志控制台 — 端口 62000（KOOK Bot）
- WebSocket 推送日志记录到浏览器
- 彩色暗色主题，支持按级别/关键词过滤
- 支持两种运行模式：
  1. 集成模式：由 bot 进程调用 start(port)，通过 WebSocketLogHandler 实时推送
  2. 独立模式：logweb.py 入口调用 start(port, standalone=True)，
     通过文件 watch 读取 logs/huanmeng.log 新增行并广播（准实时，0.5s 延迟）
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ── 连接池 ────────────────────────────────────────────────
_clients: set[asyncio.Queue] = set()


async def broadcast(record: dict):
    """推送日志记录到所有连接的客户端"""
    for q in list(_clients):
        try:
            q.put_nowait(record)
        except asyncio.QueueFull:
            pass


# ── HTTP Handler ──────────────────────────────────────────

_HTML = ""

def _tail_file(path: Path, n: int) -> str:
    """读取文件尾部 n 行，去除 ANSI"""
    import re
    try:
        with open(path, "rb") as f:
            if f.seek(0, 2) == 0:
                return ""
            buf_size = 8192
            blocks = []
            f.seek(0, 2)
            remaining = f.tell()
            while remaining > 0 and len(blocks) < n:
                read_size = min(buf_size, remaining)
                f.seek(remaining - read_size)
                blocks.append(f.read(read_size).decode("utf-8", errors="ignore"))
                remaining -= read_size
            text = "".join(reversed(blocks))
            text = re.sub(r'\x1b\[[0-9;]*m', '', text)  # 去 ANSI
            lines = text.split("\n")
            return "\n".join(lines[-n:]).strip()
    except Exception:
        return ""


def _load_html() -> str:
    global _HTML
    if _HTML:
        return _HTML
    here = Path(__file__).resolve().parent.parent
    path = here / "data" / "templates" / "console.html"
    if path.exists():
        _HTML = path.read_text(encoding="utf-8")
        return _HTML
    _HTML = _build_html()
    return _HTML


def _load_static(name: str) -> bytes | None:
    here = Path(__file__).resolve().parent.parent
    path = here / "data" / "templates" / name
    if path.exists():
        return path.read_bytes()
    return None


_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".html": "text/html",
}


def _build_html() -> str:
    """全功能 Web 控制台 — 暗色主题 + 来源筛选"""
    return r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>幻梦 QQ Bot · 控制台</title>
<style>
:root{--bg:#0a0a14;--card:rgba(255,255,255,.03);--t1:#e5e7eb;--t2:#9ca3af;--t3:#6b7280;--border:rgba(255,255,255,.07);--pink:#ec4899;--cyan:#06b6d4;--r8:8px;}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--t1);font:13px/1.5 Consolas,monospace;height:100vh;display:flex;flex-direction:column}
.toolbar{display:flex;align-items:center;gap:8px;padding:8px 14px;background:var(--card);border-bottom:1px solid var(--border);flex-wrap:wrap}
.toolbar span{font-size:11px;color:var(--t3)}
.btn{padding:3px 12px;border:1px solid var(--border);border-radius:var(--r8);background:rgba(255,255,255,.03);color:var(--t2);font-size:11px;cursor:pointer;transition:.15s}
.btn:hover{background:rgba(255,255,255,.08);color:var(--t1)}
.btn.on{background:var(--pink);border-color:var(--pink);color:#fff}
.stat{font-size:10px;color:var(--t3);margin-left:auto}
#log{flex:1;overflow-y:auto;padding:6px 8px;font-size:12px}
.line{padding:1px 4px;border-radius:3px;white-space:pre-wrap;word-break:break-all}
.line:hover{background:rgba(255,255,255,.03)}
.ts{color:var(--t3);margin-right:6px}
.lv{font-weight:700;margin-right:4px}
.lv.INFO{color:#34d399}.lv.WARNING{color:#fbbf24}.lv.ERROR,.lv.CRITICAL{color:#ef4444}.lv.DEBUG{color:#6366f1}
.src{color:#6b7280;font-size:10px;margin:0 4px}
.src.pc_status{color:var(--cyan)}
</style></head><body>
<div class=toolbar>
<span>幻梦控制台</span>
<button class="btn on" onclick="setFilter('all',this)">全部</button>
<button class=btn onclick="setFilter('bot',this)">机器人</button>
<button class=btn onclick="setFilter('pc_status',this)">PC状态</button>
<button class=btn onclick="setFilter('llm',this)">LLM</button>
<button class=btn onclick="setFilter('memory',this)">记忆</button>
<span class=stat id=stat></span>
</div>
<div id=log></div>
<script>
let filter='all',logEl=document.getElementById('log'),statEl=document.getElementById('stat');
let lines=[],maxLines=2000;
const BOT_MODS=['dispatcher','pipeline','sender','commands','image'];
const LLM_MODS=['llm','judge'];
const MEM_MODS=['memory','user_profile','context_manager'];

function srcClass(mod){if(mod==='pc_status')return' src pc_status';if(BOT_MODS.includes(mod))return' src bot';if(LLM_MODS.includes(mod))return' src llm';if(MEM_MODS.includes(mod))return' src memory';return''}

function setFilter(f,btn){
  filter=f;
  document.querySelectorAll('.btn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  render();
}

function shouldShow(src){
  if(filter==='all')return true;
  let mod=src.split(':')[0];
  if(filter==='bot')return BOT_MODS.includes(mod)||src.startsWith('bot');
  if(filter==='llm')return LLM_MODS.includes(mod);
  if(filter==='memory')return MEM_MODS.includes(mod);
  if(filter==='pc_status')return mod==='pc_status';
  return true;
}

function escape(s){let d=document.createElement('div');d.textContent=s;return d.innerHTML}

function render(){
  let html='',count=0;
  for(let i=lines.length-1;i>=0;i--){
    let l=lines[i];
    if(!shouldShow(l.src))continue;
    count++;
    html+='<div class=line><span class=ts>'+escape(l.ts)+'</span><span class="lv '+escape(l.lv)+'">'+escape(l.lv)+'</span><span class="'+srcClass(l.src.split(':')[0])+'">'+escape(l.src)+'</span>'+escape(l.msg)+'</div>';
  }
  logEl.innerHTML=html||'<div style="color:var(--t3);padding:8px">暂无日志</div>';
  statEl.textContent='显示 '+count+'/'+lines.length+' 行';
}

function connect(){
  let proto=location.protocol==='https:'?'wss':'ws';
  let ws=new WebSocket(proto+'://'+location.host);
  ws.onmessage=function(e){
    let d=JSON.parse(e.data);
    lines.push(d);
    if(lines.length>maxLines)lines.splice(0,lines.length-maxLines);
    if(shouldShow(d.src)){
      let div=document.createElement('div');
      div.className='line';
      div.innerHTML='<span class=ts>'+escape(d.ts)+'</span><span class="lv '+escape(d.lv)+'">'+escape(d.lv)+'</span><span class="'+srcClass(d.src.split(':')[0])+'">'+escape(d.src)+'</span>'+escape(d.msg);
      logEl.insertBefore(div,logEl.firstChild);
    }
    statEl.textContent=lines.length+' 行';
  };
  ws.onclose=function(){setTimeout(connect,2000)};
}
connect();
</script></body></html>"""



async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """极简 HTTP 服务器"""
    try:
        data = await asyncio.wait_for(reader.read(4096), timeout=5)
        request = data.decode("utf-8", errors="ignore")
        lines = request.split("\r\n")

        # WebSocket 升级
        if any("Upgrade: websocket" in l for l in lines):
            key = ""
            for l in lines:
                if l.lower().startswith("sec-websocket-key:"):
                    key = l.split(":", 1)[1].strip()
                    break
            if key:
                import hashlib, base64
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                writer.write(
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                )
                await writer.drain()
                await _ws_handler(reader, writer)
                return

        # API: 历史日志
        if request.startswith("GET /api/logs"):
            import urllib.parse
            qs = urllib.parse.urlparse(lines[0].split()[1]).query
            params = urllib.parse.parse_qs(qs)
            n = int(params.get("lines", [200])[0])
            log_path = Path(__file__).resolve().parent.parent / "logs" / "huanmeng.log"
            if log_path.exists():
                tail = _tail_file(log_path, n)
                body = tail.encode("utf-8")
            else:
                body = b""
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + body
            writer.write(resp)
            await writer.drain()
            return

        # 封锁页 ?blocked=1
        if request.startswith("GET /"):
            import urllib.parse
            qs = urllib.parse.urlparse(lines[0].split()[1]).query
            if urllib.parse.parse_qs(qs).get("blocked") == ["1"]:
                body = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>访问被禁止 · 幻梦控制台</title>
<style>*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:#0a0a14;color:#e5e7eb;font-family:'Noto Sans SC',system-ui,sans-serif;
display:grid;place-items:center;-webkit-font-smoothing:antialiased}
.card{text-align:center;border:1px solid rgba(239,68,68,.35);border-radius:18px;
padding:40px 48px;background:rgba(239,68,68,.06);box-shadow:0 0 60px rgba(239,68,68,.12)}
h1{font-size:20px;font-weight:700;color:#ef4444;margin-bottom:10px}
p{font-size:13px;color:#6b7280;margin-bottom:16px}
code{font-family:'JetBrains Mono',monospace;font-size:11px;color:#f87171;
background:rgba(239,68,68,.1);padding:4px 10px;border-radius:6px}
a{color:#67e8f9;font-size:13px;text-decoration:none;display:inline-block;margin-top:8px}
a:hover{text-decoration:underline}
</style></head><body>
<div class="card">
<h1>⛔ 访问被禁止</h1>
<p>开发者工具未在限期内关闭</p>
<code>ERR_DEVTOOLS_DETECTED</code><br>
<a href="/">← 刷新返回控制台</a>
</div></body></html>""".encode("utf-8")
                resp = (
                    "HTTP/1.1 403 Forbidden\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + body
                writer.write(resp)
                await writer.drain()
                return

        # 静态文件 CSS/JS
        if request.startswith("GET /"):
            path = lines[0].split()[1]
            name = path.lstrip("/")
            if name in ("console.css", "console.js", "guard.js"):
                body = _load_static(name)
                if body:
                    ext = "." + name.rsplit(".", 1)[-1]
                    ct = _CONTENT_TYPES.get(ext, "application/octet-stream")
                    resp = (
                        "HTTP/1.1 200 OK\r\n"
                        f"Content-Type: {ct}; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode() + body
                    writer.write(resp)
                    await writer.drain()
                    return

        # 普通 HTTP → 返回 HTML
        html = _load_html()
        body = html.encode("utf-8")
        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def _ws_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """WebSocket 帧处理"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _clients.add(queue)
    try:
        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=2)
                data = json.dumps(record, ensure_ascii=False)
                await _ws_send(writer, data)
            except asyncio.TimeoutError:
                # 心跳
                await _ws_send(writer, "", opcode=0x9)
    except Exception:
        pass
    finally:
        _clients.discard(queue)
        try:
            writer.close()
        except Exception:
            pass


async def _ws_send(writer: asyncio.StreamWriter, text: str, opcode: int = 0x1):
    """发送 WebSocket 文本帧"""
    payload = text.encode("utf-8")
    length = len(payload)
    frame = bytearray()
    frame.append(0x80 | opcode)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(127)
        frame.extend(length.to_bytes(8, "big"))
    frame.extend(payload)
    try:
        writer.write(bytes(frame))
        await writer.drain()
    except Exception:
        raise


# ── 自定义 Logging Handler ───────────────────────────────

class WebSocketLogHandler(logging.Handler):
    """将日志记录推送到 WebSocket 客户端"""

    def emit(self, record: logging.LogRecord):
        try:
            ts = datetime.now(CST).strftime("%H:%M:%S") + f".{int(record.created % 1 * 1000):03d}"
            msg = self.format(record)
            # 提取 chat ID（群号/QQ号）
            chat = ""
            for pattern in [
                r'chat[:=\s]*(\d{5,})',    # chat=1058782600, chat:1058782600
                r'群(\d{5,})',              # 群1058782600
                r'\[(\d{5,})\]',            # [1058782600]
                r'(\d{5,})',                # 任意 5+ 位数字
            ]:
                m = re.search(pattern, msg)
                if m and m.group(1) not in ('8099', '62000', '62001', '62002', '62003'):  # 排除端口号
                    chat = m.group(1)
                    break
            payload = {
                "time": ts,
                "lv": record.levelname,
                "src": f"{record.module}:{record.funcName}:{record.lineno}",
                "msg": msg,
                "chat": chat,
            }
            # 仅在事件循环运行时推送（避免初始化时崩溃）
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(broadcast(payload))
            except RuntimeError:
                pass  # 事件循环尚未启动，跳过
        except Exception:
            pass


# ── 服务器启动 ───────────────────────────────────────────

_server_task: asyncio.Task | None = None

# 独立模式：日志文件读取偏移量
_file_offset: int = 0


def _parse_log_line(line: str) -> dict | None:
    """解析日志文件中的一行（带 ANSI 颜色码），返回 record dict"""
    # 去 ANSI 颜色码
    clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
    # 文件格式: [2026-08-07 21:45:38.123] [INFO    ] (module:func:line) 消息
    m = re.match(
        r'\[(\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2}\.\d{3})\]\s*\[(\w+)\s*\]\s*\(([^:)]+):([^:)]+):(\d+)\)\s?(.*)',
        clean,
    )
    if not m:
        return None
    _, ts, lv, module, func, lineno, msg = m.groups()
    return {
        "time": ts,
        "lv": lv.strip(),
        "src": f"{module}:{func}:{lineno}",
        "msg": msg,
        "chat": "",
    }


async def _file_watcher(log_path: Path, interval: float = 0.5):
    """独立模式：定时读取日志文件新增行并广播给 WebSocket 客户端"""
    global _file_offset
    # 等待日志文件出现
    while not log_path.exists():
        await asyncio.sleep(1)
    # 从文件末尾开始读（只推送启动后的新日志）
    _file_offset = log_path.stat().st_size
    while True:
        try:
            if not log_path.exists():
                await asyncio.sleep(interval)
                continue
            size = log_path.stat().st_size
            # 文件被截断（轮转）
            if size < _file_offset:
                _file_offset = 0
            if size > _file_offset:
                with open(log_path, "rb") as f:
                    f.seek(_file_offset)
                    data = f.read(size - _file_offset)
                    _file_offset = size
                for raw_line in data.decode("utf-8", errors="ignore").splitlines():
                    if not raw_line.strip():
                        continue
                    record = _parse_log_line(raw_line)
                    if record:
                        await broadcast(record)
        except Exception:
            pass
        await asyncio.sleep(interval)


async def start(port: int = 62000, standalone: bool = False):
    """
    启动日志 Web 服务器。

    Args:
        port: 监听端口（默认 62000）
        standalone: 独立模式 — 启动文件 watcher，读取 logs/huanmeng.log 新增行并广播。
                    适用于 logweb.py 独立进程（不依赖 bot 进程的 WebSocketLogHandler）。
    """
    global _server_task
    server = await asyncio.start_server(_http_handler, "0.0.0.0", port)
    addr = server.sockets[0].getsockname()
    logging.getLogger("huanmeng").info("🌐 日志控制台已启动 → http://0.0.0.0:%d", addr[1])

    if standalone:
        log_path = Path(__file__).resolve().parent.parent / "logs" / "huanmeng.log"
        asyncio.ensure_future(_file_watcher(log_path))
        logging.getLogger("huanmeng").info("📂 独立模式：监听日志文件 %s", log_path)

    _server_task = asyncio.current_task()
    async with server:
        await server.serve_forever()
