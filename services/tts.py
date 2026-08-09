"""
Qwen3-TTS 节点管理 — 接收第二台电脑主动连接,提供单条合成接口
- 节点(第二台电脑)主动连服务器 62003,保持长连接
- 支持并发请求(节点串行合成,但 instruct 生成可并发)
- 30s 心跳检测连接活性
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path

from core.logger import get_logger

logger = get_logger("tts")

_NODE_PORT = int(os.environ.get("TTS_PORT", "62003"))
_NODE_AUTH = os.environ.get("TTS_AUTH", "")
_WAV_DIR = Path(__file__).resolve().parent.parent / "data" / "tts_temp"

_node_writer: asyncio.StreamWriter | None = None
_node_lock = asyncio.Lock()
_pending: dict[str, asyncio.Future] = {}
_write_lock = asyncio.Lock()


async def _handle_node(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global _node_writer
    peer = writer.get_extra_info("peername")

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            writer.close(); return
        auth = line.decode().strip()
        if _NODE_AUTH and (not auth.startswith("AUTH ") or auth[5:] != _NODE_AUTH):
            logger.warning("TTS 节点 AUTH 失败: %s", peer)
            writer.close(); return
        writer.write(b"OK\n")
        await writer.drain()
    except Exception:
        writer.close(); return

    async with _node_lock:
        if _node_writer is not None:
            try:
                _node_writer.close()
            except Exception:
                pass
        _node_writer = writer

    logger.info("TTS 节点已连接: %s", peer)

    async def _heartbeat():
        while True:
            await asyncio.sleep(30)
            try:
                async with _write_lock:
                    writer.write(b'{"ping":1}\n')
                    await writer.drain()
            except Exception:
                return

    hb_task = asyncio.create_task(_heartbeat())

    try:
        while True:
            try:
                # 合成期间不超时,给 5 分钟兜底
                line = await asyncio.wait_for(reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning("TTS 节点 5 分钟无数据,断开重连")
                break
            if not line:
                break
            try:
                resp = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue

            if resp.get("pong"):
                continue

            req_id = resp.get("id", "")
            future = _pending.pop(req_id, None)
            if future is None:
                logger.warning("TTS 收到未知响应: id=%s", req_id)
                continue

            if resp.get("ok"):
                future.set_result(resp)
            else:
                future.set_exception(Exception(resp.get("error", "未知错误")))
    except Exception as e:
        logger.warning("TTS 节点连接异常: %s", e)
    finally:
        hb_task.cancel()
        async with _node_lock:
            if _node_writer is writer:
                _node_writer = None
        for req_id, fut in list(_pending.items()):
            if not fut.done():
                fut.set_exception(Exception("TTS 节点断开"))
        _pending.clear()
        try:
            writer.close()
        except Exception:
            pass
        logger.info("TTS 节点断开: %s", peer)


async def synthesize_voice(
    text: str,
    speaker: str = "Serena",
    instruct: str = "",
    timeout: float = 120.0,
) -> tuple[Path | None, str]:
    """调用 TTS 节点合成语音(支持并发请求,节点串行合成)"""
    if not text.strip():
        return None, "文本为空"

    async with _node_lock:
        writer = _node_writer

    if writer is None:
        return None, "TTS 节点未连接(第二台电脑未启动?)"

    req_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[req_id] = future

    req = json.dumps({
        "id": req_id,
        "text": text,
        "speaker": speaker,
        "instruct": instruct,
    }, ensure_ascii=False) + "\n"

    try:
        async with _write_lock:
            writer.write(req.encode("utf-8"))
            await writer.drain()
    except Exception as e:
        _pending.pop(req_id, None)
        return None, f"发送失败: {e}"

    try:
        resp = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        return None, f"合成超时 ({timeout}s)"
    except Exception as e:
        _pending.pop(req_id, None)
        return None, str(e)

    wav_b64 = resp.get("wav_b64", "")
    duration = resp.get("duration", 0)
    if not wav_b64:
        return None, "返回数据为空"

    try:
        wav_bytes = base64.b64decode(wav_b64)
    except Exception as e:
        return None, f"wav 解码失败: {e}"

    _WAV_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = _WAV_DIR / f"voice_{uuid.uuid4().hex[:8]}.wav"
    wav_path.write_bytes(wav_bytes)

    logger.info("TTS 合成成功: %s (%.1fs 音频, %dKB)",
                wav_path.name, duration, len(wav_bytes) // 1024)
    return wav_path, ""


async def cleanup_wav(wav_path: Path, delay: float = 30):
    await asyncio.sleep(delay)
    try:
        if wav_path.exists():
            wav_path.unlink()
    except Exception:
        pass


def is_node_connected() -> bool:
    return _node_writer is not None


async def start_tts_server(port: int = _NODE_PORT):
    server = await asyncio.start_server(_handle_node, "0.0.0.0", port, limit=4_194_304)
    logger.info("TTS 节点接收端: 0.0.0.0:%d (等待第二台电脑连接...)", port)
    return server
