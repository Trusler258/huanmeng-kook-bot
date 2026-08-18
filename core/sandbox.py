"""
core/sandbox.py — 沙箱代码执行（Huanmeng 2.0）

在服务器独立临时目录中真实执行用户/LLM 生成的代码，返回真实运行输出与产物文件。
用于根治「声称已执行但实际只发文件」的幻觉：执行结果一律来自真实的子进程输出。

安全边界（不依赖 Prompt 约束，全部做成硬限制）：
- 独立临时目录：tempfile.mkdtemp(prefix="bot_sandbox_")，执行后清理
- 超时强杀：asyncio.wait_for + proc.kill()
- 限内存 / 限 CPU：Linux 用 resource.setrlimit 在 preexec_fn 中限制（Windows 降级跳过）
- 输出截断：stdout/stderr 各截断到 MAX_OUTPUT，避免刷屏
- 产物收集：执行后扫描目录，把生成的附件打包返回，脚本自身不返回
- 默认不开放网络（无代理、无特权）；shell 命令仅管理员/审批后可用（由上层裁决）
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from core.logger import get_logger

logger = get_logger("sandbox")

# 单次运行最大秒数
DEFAULT_TIMEOUT: float = 10.0
# 内存上限（MB），Linux 生效
DEFAULT_MEM_MB: int = 256
# 单路输出最大字符数（截断提示放在末尾）
MAX_OUTPUT: int = 1500
# 最大产物数 / 单个产物最大字节（避免把磁盘整个打包）
MAX_ARTIFACTS: int = 10
MAX_ARTIFACT_BYTES: int = 5 * 1024 * 1024


def _limit_preexec(mem_mb: int, cpu_sec: int) -> callable | None:
    """构造限制子进程资源的 preexec_fn（仅 Linux；Windows 返回 None）。"""
    if os.name != "posix":
        return None
    import resource

    def _apply() -> None:
        try:
            # 地址空间上限（内存）
            resource.setrlimit(resource.RLIMIT_AS,
                               (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
        except Exception:
            pass
        try:
            # CPU 时间上限（秒）
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec + 5))
        except Exception:
            pass

    return _apply


async def _run_proc(cmd: list[str], cwd: Path, timeout: float, mem_mb: int,
                    stdin_data: str = "") -> dict:
    """通用子进程执行：限时、限资源、截断输出。返回 dict。"""
    kwargs: dict = {
        "cwd": str(cwd),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if stdin_data:
        kwargs["stdin"] = asyncio.subprocess.PIPE
    preexec = _limit_preexec(mem_mb, int(timeout) + 5)
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data.encode("utf-8") if stdin_data else None),
                timeout=timeout,
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            stdout, stderr = b"", "[timeout] 运行超时，已强制终止".encode("utf-8")
    except FileNotFoundError as e:
        return {"returncode": -1, "stdout": "",
                "stderr": f"运行环境缺失: {e}", "timed_out": False}
    except Exception as e:
        return {"returncode": -1, "stdout": "",
                "stderr": f"执行异常: {e}", "timed_out": False}

    def _cut(b: bytes) -> str:
        text = b.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…(输出过长，已截断，共 {len(b.decode('utf-8', errors='replace'))} 字符)"
        return text

    return {
        "returncode": getattr(proc, "returncode", -1) if not timed_out else -1,
        "stdout": _cut(stdout or b""),
        "stderr": _cut(stderr or b""),
        "timed_out": timed_out,
    }


def _pick_python() -> str:
    """选择可用的 Python 解释器：优先 python3（Linux 服务器），
    Windows 商店别名 stub（WindowsApps）不可运行，跳过回退 python。"""
    for name in ("python3", "python"):
        path = shutil.which(name)
        if not path:
            continue
        if os.name == "nt" and "WindowsApps" in path:
            continue
        return path
    return "python3"


async def run_python(code: str, timeout: float = DEFAULT_TIMEOUT,
                     mem_mb: int = DEFAULT_MEM_MB,
                     stdin_data: str = "", cwd: Path | None = None) -> dict:
    """在沙箱目录执行 Python 代码，返回运行结果。"""
    tmp = cwd or Path(tempfile.mkdtemp(prefix="bot_sandbox_"))
    script = tmp / "main.py"
    script.write_text(code or "", encoding="utf-8")
    cmd = [_pick_python(), str(script)]
    result = await _run_proc(cmd, tmp, timeout, mem_mb, stdin_data)
    result["tmp_dir"] = str(tmp)
    return result


async def compile_and_run_cpp(files: dict[str, str], timeout: float = DEFAULT_TIMEOUT,
                              mem_mb: int = DEFAULT_MEM_MB,
                              stdin_data: str = "", cwd: Path | None = None) -> dict:
    """在沙箱目录编译并运行 C++（g++）。files: {文件名: 内容}。"""
    tmp = cwd or Path(tempfile.mkdtemp(prefix="bot_sandbox_"))
    for fname, content in files.items():
        (tmp / fname).write_text(content or "", encoding="utf-8")
    if shutil.which("g++") is None:
        return {"returncode": -1, "stdout": "",
                "stderr": "服务器未安装 g++，无法编译 C++", "timed_out": False,
                "tmp_dir": str(tmp)}
    exe = tmp / "a.out"
    srcs = [str(tmp / f) for f in files]
    comp = await _run_proc(
        ["g++", "-std=c++14", "-O2", "-o", str(exe)] + srcs,
        tmp, timeout, mem_mb)
    if comp["returncode"] != 0:
        comp["tmp_dir"] = str(tmp)
        comp["stdout"] = "[编译失败]\n" + (comp["stderr"] or "")
        return comp
    run = await _run_proc([str(exe)], tmp, timeout, mem_mb, stdin_data)
    run["tmp_dir"] = str(tmp)
    return run


async def run_shell(command: str, timeout: float = DEFAULT_TIMEOUT,
                    mem_mb: int = DEFAULT_MEM_MB,
                    cwd: Path | None = None) -> dict:
    """执行 shell 命令（终端模拟，如 `cd / && ls -l`）。仅管理员/审批后调用。"""
    tmp = cwd or Path(tempfile.mkdtemp(prefix="bot_sandbox_"))
    if os.name == "posix":
        cmd = ["bash", "-c", command]
    else:
        cmd = ["cmd", "/c", command]
    result = await _run_proc(cmd, tmp, timeout, mem_mb)
    result["tmp_dir"] = str(tmp)
    return result


# ── 产物收集 ──────────────────────────────────────────────

_SKIP_NAMES = {"main.py", "a.out"}
_SKIP_EXTS = {".pyc", ".o", ".obj"}


def collect_artifacts(tmp_dir: str | Path) -> list[Path]:
    """扫描沙箱目录中用户代码生成的文件（排除脚本自身/编译产物/缓存），按大小降序。"""
    root = Path(tmp_dir)
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if p.name in _SKIP_NAMES or p.suffix in _SKIP_EXTS:
            continue
        if "__pycache__" in rel.parts or rel.name.startswith("."):
            continue
        try:
            if p.stat().st_size == 0:
                continue
            if p.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    out.sort(key=lambda p: p.stat().st_size, reverse=True)
    return out[:MAX_ARTIFACTS]


def cleanup(tmp_dir: str | Path) -> None:
    """删除沙箱临时目录（执行后调用）。"""
    try:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    except Exception:
        pass


def safe_display_path(path: Path) -> str:
    """产物相对路径，用于消息展示。"""
    try:
        return str(path.relative_to(path.parents[-2])) if len(path.parts) > 2 else path.name
    except Exception:
        return path.name
