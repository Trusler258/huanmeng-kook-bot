"""
Phase 16 代码级更新：安全更新流水线（Huanmeng 2.0）

把现有 patch 引擎升级为"安全代码级更新"，明确禁止收到 commit 后直接覆盖生产。

流程：
    Remote Fetch → Compare → Diff → Code Analysis → Dependency Analysis
    → Risk Assessment → Staging Apply → Test → Health Check
    → Production Apply → Snapshot → Rollback

设计约束：
- 复用现有 engine.py 的 fetch/compare/patch 能力，不重复实现网络层。
- 生产应用前先创建 Snapshot；Test 失败 / 启动失败 / Health Check 失败自动 Rollback。
- 高风险（HIGH）默认要求人工确认，未确认不应用到生产。
- 禁止 LLM 直接生成完整文件覆盖旧文件，始终走 Git Diff + 最小 Patch。
"""
from __future__ import annotations

import asyncio
import difflib
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from core.logger import get_logger
from modules._auto_update import engine as _eng
from modules._auto_update import patcher
from modules._auto_update import snapshot as _snap
from modules._auto_update import analyzer

logger = get_logger("auto_update.safe")

# 审批接口：外部可注入 (files, assessment) -> bool 来决定高风险是否放行
_approve_callback = None


def set_approve_callback(cb) -> None:
    """注入人工/审批确认回调。cb(files, assessment) -> bool。"""
    global _approve_callback
    _approve_callback = cb


def _root() -> Path:
    return _eng._root()


def _skip_prefix(rel_path: str) -> bool:
    return _eng._skip_prefix(rel_path)


def _is_protected(rel_path: str, protect: set[str]) -> bool:
    return _eng._is_protected(rel_path, protect)


# ── 阶段：合并内容（staging，不写盘） ─────────────────────
async def _diff_stats(item: dict, root: Path) -> tuple[int, int]:
    """计算文件新增/删除行数。
    优先用 compare 返回的 additions/deletions；缺失时（全量下载分支）下载远程内容
    与本地文件做 difflib 对比，得到真实 +xxx -xxx。"""
    add = item.get("additions")
    dele = item.get("deletions")
    if add is not None and dele is not None:
        return add, dele
    rel = item.get("filename", "")
    raw_url = item.get("raw_url", "")
    if not raw_url:
        return 0, 0
    try:
        async with _eng.httpx.AsyncClient(timeout=15, verify=False) as dl:
            resp = await dl.get(_eng._normalize_raw_url(raw_url))
            resp.raise_for_status()
        remote = resp.text.splitlines(keepends=True)
    except Exception:
        logger.warning("获取 %s 远程内容失败，无法统计差异", rel)
        return 0, 0
    local = _eng._read_local(root, rel)
    sm = difflib.SequenceMatcher(None, local, remote, autojunk=False)
    add = dele = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            add += j2 - j1
        elif tag == "delete":
            dele += i2 - i1
        elif tag == "replace":
            add += j2 - j1
            dele += i2 - i1
    return add, dele


def _compute_merged(item: dict, root: Path) -> tuple[Optional[list], str, int, int]:
    """计算某文件合并后的行内容（内存态），返回 (lines, status, ok, skip)。"""
    rel = item.get("filename", "")
    status = item.get("status", "")
    patch_text = item.get("patch", "")
    if status == "removed":
        return None, "removed", 0, 0
    if not patch_text:
        # 无 patch → 全量下载（首次/force）
        return None, "download", 0, 0
    hunks = patcher.parse_patch(patch_text)
    if not hunks:
        return None, "noop", 0, 0
    local_lines = _eng._read_local(root, rel)
    merged, aok, sk = patcher.apply_hunks(local_lines, hunks)
    return merged, status, aok, sk


# ── 阶段：AST 代码分析 ───────────────────────────────────
def _code_analysis(files: list[dict], root: Path) -> list[analyzer.FileAnalysis]:
    results = []
    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel) or not analyzer.is_python_path(rel):
            continue
        merged, status, _, _ = _compute_merged(item, root)
        if merged is None:
            continue
        content = "".join(merged)
        fa = analyzer.analyze_python(rel, content)
        fa.status = status
        results.append(fa)
    return results


# ── 阶段：Test（语法编译） ────────────────────────────────
def _staging_test(files: list[dict], root: Path) -> list[str]:
    """对本次改动的 .py 文件做 py_compile 语法检查（staging 内容）。返回错误列表。"""
    errors = []
    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel) or not analyzer.is_python_path(rel):
            continue
        merged, status, _, _ = _compute_merged(item, root)
        if merged is None:
            continue
        content = "".join(merged)
        try:
            compile(content, rel, "exec")
        except SyntaxError as e:
            errors.append(f"[语法错误] {rel} 第{e.lineno}行: {e.msg}")
    return errors


# ── 阶段：Health Check（生产文件落盘校验） ────────────────
def _health_check(files: list[dict], root: Path) -> list[str]:
    """
    生产应用后对磁盘上的实际文件做 Health Check：
    - 改动过的 .py 文件必须能通过 compile（文件可读、写入完整、语法正确）。
    - 新增 import 不能指向本地不存在的模块（避免启动 ImportError）。
    返回错误列表；非空即触发自动回滚。
    """
    errors = []
    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel) or not analyzer.is_python_path(rel):
            continue
        fpath = root / rel
        if not fpath.exists():
            # 文件缺失：_apply_production 已尽力下载/合并，仍未落地说明该文件本次
            # 无法同步。降级为"跳过该文件"而非整体回滚，避免单文件缺失连累其他更新
            # （用户诉求：缺失了跳过即可，不影响其他内容更新，tests/ 本就不同步）。
            logger.warning("健康检查: %s 仍缺失，跳过该文件（不阻断本次更新）", rel)
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
            compile(content, rel, "exec")
        except SyntaxError as e:
            errors.append(f"[健康检查] {rel} 第{e.lineno}行语法错误: {e.msg}")
        except OSError as e:
            errors.append(f"[健康检查] 读取 {rel} 失败: {e}")
        # 新增 import 探测：本地不存在且非 stdlib/三方库 → 警告级，不阻断
        if item.get("status") == "added":
            for imp in analyzer.extract_imports(content):
                base = imp.split(".")[0]
                if not (root / (imp.replace(".", "/") + ".py")).exists() \
                        and not _is_stdlib(base):
                    logger.warning("健康检查: %s 引用可能的第三方/新模块 %s", rel, imp)
    return errors


def _is_stdlib(mod: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


# ── 依赖管理：缺失本地模块阻断 + 新增依赖自动安装 ─────────
def _check_local_imports(files: list[dict], root: Path) -> list[str]:
    """静态校验改动文件 content 里 import 的本地模块文件是否真实存在。

    改动文件合并后新增/引用的本地模块（顶层包在项目根内）若文件缺失、
    且不在本次更新的 added/modified 集合内 → 判为"缺失本地模块"阻断错误，
    可提前拦截 cmd_cards.py 丢失引发的 ModuleNotFoundError。
    """
    errors: list[str] = []
    being_added = {
        f.get("filename", "") for f in files if f.get("status") in ("added", "modified")
    }
    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel) or not analyzer.is_python_path(rel):
            continue
        merged, _, _, _ = _compute_merged(item, root)
        if merged is None:  # removed / download / noop
            continue
        content = "".join(merged)
        for imp in analyzer.extract_imports(content):
            dest = analyzer.resolve_project_module(root, imp)
            if dest is None:
                continue  # 非本地模块（stdlib / 三方库）
            rel_dest = dest.relative_to(root).as_posix()
            if not dest.exists() and rel_dest not in being_added:
                errors.append(f"{rel} 引用本地模块 {imp}（{rel_dest}）不存在")
    return errors


async def _ensure_dependencies(files: list[dict], root: Path) -> list[str]:
    """requirements.txt 变化时，自动安装新增依赖。返回错误列表（空 = 成功）。"""
    req = next((f for f in files if f.get("filename") == "requirements.txt"), None)
    if not req:
        return []
    raw_url = req.get("raw_url", "")
    if not raw_url:
        return ["requirements.txt 无 raw_url，无法安装依赖"]
    try:
        async with _eng.httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as dl:
            resp = await dl.get(_eng._normalize_raw_url(raw_url))
            resp.raise_for_status()
        req_text = resp.text
    except Exception as e:
        return [f"下载 requirements.txt 失败: {e}"]
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write(req_text)
            tmp = tf.name
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", tmp],
            capture_output=True, text=True, timeout=240,
        )
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-300:] + (proc.stderr or "")[-300:]
            return [f"pip install 返回异常:\n{tail}"]
        return []
    except subprocess.TimeoutExpired:
        return ["pip install 超时（>240s），需手动安装依赖后重试"]
    except Exception as e:
        return [f"pip install 执行异常: {e}"]
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Health Check 升级：真实启动冒烟测试（subprocess 隔离） ──
def _smoke_startup(files: list[dict], root: Path, timeout: int = 60) -> list[str]:
    """在子进程里导入本次改动的全部本地模块，模拟一次真实启动。

    仅把 ImportError / ModuleNotFoundError / SyntaxError 判为阻断错误
    （正是"更新后 ModuleNotFoundError / 直接崩溃"的根因）；其他运行时异常
    不阻断，避免误伤合法更新。子进程独立执行，即便某模块 import 级有副作用
    也不会影响正在运行的 Bot。返回阻断错误列表（空 = 通过）。
    """
    mods: list[str] = []
    for item in files:
        rel = item.get("filename", "")
        if not rel or _skip_prefix(rel) or not analyzer.is_python_path(rel):
            continue
        if rel in ("main.py", "__main__.py") or rel.rstrip("/").endswith("__main__.py"):
            continue  # 入口脚本会拉起 Bot，跳过 import 冒烟
        mods.append(rel[:-3].replace("/", "."))
    if not mods:
        return []
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"mods = {repr(mods)}\n"
        "block = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except (ImportError, ModuleNotFoundError, SyntaxError) as e:\n"
        "        block.append((m, repr(e)))\n"
        "    except Exception:\n"
        "        pass\n"
        "if block:\n"
        "    for m, e in block:\n"
        "        print('BLOCK', m, e, flush=True)\n"
        "    sys.exit(1)\n"
        "print('OK')\n"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"启动冒烟测试超时（>{timeout}s），未完成 import 校验"]
    except Exception as e:
        return [f"启动冒烟测试执行失败: {e}"]
    blocks: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("BLOCK"):
            parts = line.split(" ", 2)
            mod = parts[1] if len(parts) > 1 else "?"
            err = parts[2] if len(parts) > 2 else ""
            blocks.append(f"{mod}: {err}")
    return blocks


# ── 主入口：安全更新 ──────────────────────────────────────
async def _report(progress, msg: str) -> None:
    """若注入了进度回调则发送一条进度消息（best-effort，失败不阻断）"""
    if progress is None:
        return
    try:
        await progress(msg)
    except Exception as e:
        logger.debug("进度上报失败: %s", e)


async def safe_check_and_update(
    check_only: bool = False,
    force: bool = False,
    require_approval: bool = True,
    progress=None,
) -> str:
    """Phase 16 安全更新流水线。返回面向用户的文本报告。

    progress: 可选 async 回调 `async def progress(msg: str)`，用于在更新各阶段
    向用户实时上报进度。
    """
    root = _root()
    state = _eng.load_state(root)

    # 1. Remote Fetch
    await _report(progress, "正在获取远程版本…")
    head = await _eng._get_head_sha()
    if not head:
        return "无法连接 GitHub，请检查网络"

    # 2 & 3. Compare + Diff
    stored = state.get("remote_sha", "")
    if not force and stored == head:
        return "已是最新"
    base = stored if stored and not force else ""
    files = await _eng._fetch_compare(base, head)
    if not files:
        if stored:
            state["remote_sha"] = head
            state.pop("files", None)
            _eng.save_state(root, state)
            files = await _eng._fetch_all_files(head)
        else:
            files = await _eng._fetch_all_files(head)
        if not files:
            return "无法获取完整文件列表，请检查网络"

    commit_log = await _eng._fetch_commit_log(base, head) if base else ""
    protect = _eng._load_protect_list()

    # 过滤掉保护/跳过文件
    actionable = [
        f for f in files
        if f.get("filename", "") and not _skip_prefix(f["filename"])
        and not _is_protected(f["filename"], protect)
    ]
    if not actionable:
        return "无可用更新（全部为受保护/跳过文件）"
    await _report(progress, f"发现 {len(actionable)} 个待更新文件，开始分析…")

    # 4. Code Analysis
    analyses = _code_analysis(actionable, root)
    python_files = [f for f in actionable if analyzer.is_python_path(f["filename"])]

    # 5. Dependency Analysis
    dep_issues = analyzer.analyze_dependencies(actionable, root)
    dep_blockers = [d for d in dep_issues if d.level == "ERROR"]

    # 6. Risk Assessment
    assessment = analyzer.assess_risk(actionable)
    await _report(progress, f"分析完成，风险等级 {assessment.level}")

    # check_only：只报告，不应用
    if check_only:
        parts = [f"待更新 {len(actionable)} 个文件（风险等级: {assessment.level}）"]
        # 每个文件显示 +新增 -删除 行数，最多展示前 20 个，其余折叠
        MAX_SHOW = 20
        shown = actionable[:MAX_SHOW]
        for f in shown:
            rel = f["filename"]
            add, dele = await _diff_stats(f, root)
            tag = assessment.by_file.get(rel, "?")
            parts.append(f"  [{tag}] {rel}  +{add} -{dele}")
        hidden = len(actionable) - len(shown)
        if hidden > 0:
            parts.append(f"  … 其余 {hidden} 个文件已折叠")
        if dep_blockers:
            parts.append("依赖阻断:")
            parts.extend(f"  ! {d.message}" for d in dep_blockers)
        for m in _check_local_imports(actionable, root):
            parts.append(f"  ! [缺失本地模块] {m}")
        if python_files:
            parts.append(f"含 {len(python_files)} 个 Python 文件（将做 AST/语法分析）")
        if commit_log:
            parts.append(commit_log)
        return "\n".join(parts)

    # 7. 依赖硬阻断：缺失本地模块 / 被删模块仍被引用 → 直接阻断更新（P0）
    for m in _check_local_imports(actionable, root):
        dep_blockers.append(analyzer.DependencyIssue("ERROR", f"[缺失本地模块] {m}"))
    if dep_blockers:
        blocked = "\n".join(f"  ! {d.message}" for d in dep_blockers[:20])
        if len(dep_blockers) > 20:
            blocked += f"\n  … 其余 {len(dep_blockers) - 20} 项"
        return ("更新被阻止：检测到依赖/缺失模块问题，未应用任何改动，未推进版本。\n"
                + blocked)

    # 8. Risk gate：高风险默认要求人工确认
    if require_approval and assessment.level == "HIGH":
        if _approve_callback is None:
            return (f"更新被拒绝：检测到 {len(assessment.high_files)} 个高风险文件，"
                    f"未配置人工审批回调。\n{assessment.reason}")
        if asyncio.iscoroutinefunction(_approve_callback):
            approved = await _approve_callback(actionable, assessment)
        else:
            approved = _approve_callback(actionable, assessment)
        if not approved:
            return f"更新已取消（高风险未获审批）：{assessment.reason}"

    # 8. Test（staging 语法检查）
    await _report(progress, "正在做语法检查…")
    test_errors = _staging_test(actionable, root)
    if test_errors:
        return "更新中止：语法检查未通过，未应用任何改动。\n" + "\n".join(test_errors)

    # 9. 依赖安装：requirements.txt 变化时自动补装新增依赖，失败则阻断（P0）
    if "requirements.txt" in {f.get("filename", "") for f in actionable}:
        await _report(progress, "正在安装新增依赖…")
        dep_install_errors = await _ensure_dependencies(actionable, root)
        if dep_install_errors:
            return "更新被阻止：新增依赖安装失败，未应用任何改动，未推进版本。\n" \
                + "\n".join(dep_install_errors)

    # 10. Snapshot（生产应用前）
    await _report(progress, "已创建快照，准备应用…")
    snap = _snap.create_snapshot(actionable, head)

    # 11. Production Apply（走 Diff + 最小 Patch，沿用现有 patcher）
    res = await _apply_production(actionable, root, head, state, progress)

    # P0: 任何文件失败（未落地 / 下载失败）→ 整体回滚，视为事务失败，绝不推进版本
    if res["failed"]:
        _snap.rollback(snap, reason="更新失败（文件未落地）")
        blocked = "\n".join(f"  - {f}" for f in res["failed"])
        return ("更新失败，已整体回滚，未推进版本。\n失败文件:\n" + blocked)

    # P0: 存在 hunk 跳过（本地改动冲突）→ 保留已合并内容，但不假装成功、不推进版本
    if res["partial"]:
        return ("更新部分应用（存在本地冲突跳过）：已保留已合并内容，但未提交更新、未推进版本。\n"
                "请解决本地与远程的冲突后重试。\n"
                + _format_skip_details(res["skip_details"]))

    # 12. Health Check（P0 升级）：语法编译 + 缺失本地模块 + 真实启动冒烟测试
    await _report(progress, "正在健康检查…")
    _health_errors = _health_check(actionable, root)
    for m in _check_local_imports(actionable, root):
        _health_errors.append(f"[缺失本地模块] {m}")
    _block_errors = _smoke_startup(actionable, root)
    if _block_errors:
        _health_errors.extend(f"[启动冒烟] {b}" for b in _block_errors)
    if _health_errors:
        _snap.rollback(snap, reason="Health Check 失败")
        return ("更新已应用但 Health Check 失败，已自动回滚，未推进版本。\n"
                + "\n".join(_health_errors))

    # 13. 全部成功 → 提交更新（重新计算基线，推进 remote_sha 到本次 commit）
    _rebuild_baseline(state, actionable, head)
    _eng.save_state(root, state)
    await _report(progress, "更新完成")
    parts = [f"已安全更新 {len(actionable)} 个文件（{res['ok']} 处成功, {res['skip']} 处跳过）"]
    if res["skip"]:
        parts.append(_format_skip_details(res["skip_details"]))
    if commit_log:
        parts.append(commit_log)
    return "\n".join(parts)


def _rebuild_baseline(state: dict, files: list[dict], head: str) -> None:
    """更新成功后重新计算基线：推进 remote_sha 到本次应用的 commit，
    并清理已删除文件的 blob 追踪，保证下次只对比增量差异。"""
    state["remote_sha"] = head
    removed = {f["filename"] for f in files if f.get("status") == "removed"}
    if removed:
        state.setdefault("files", {})
        for rel in removed:
            state["files"].pop(rel, None)


async def _download_via_api(root: Path, item: dict, state: dict, head: str) -> bool:
    """通过 api.github.com contents API 下载单文件（服务器可直连，raw 被墙时兜底）。

    与 engine._get_blob_sha 同走 api.github.com 通道，返回的 base64 解码后即仓库
    原始内容（LF 行尾），可直接落盘。成功返回 True（已写盘并更新 blob 追踪）。
    """
    rel = item.get("filename", "")
    if not rel or not _eng.GITHUB_API:
        return False
    import base64
    url = f"{_eng.GITHUB_API}/contents/{_eng._normalize_rel_path(rel)}?ref={head}"
    try:
        async with _eng.httpx.AsyncClient(timeout=20, verify=False) as dl:
            resp = await dl.get(url, headers=_eng._gh_headers())
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data.get("content", "") or "").decode("utf-8", errors="replace")
        _eng._write_local(root, rel, content.splitlines(keepends=True))
        try:
            blob = await _eng._get_blob_sha(rel, head)
            _eng.set_file_blob(state, rel, blob, 1, 0)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("API 下载 %s 失败: %s", rel, e)
        return False


async def _download_full(root: Path, item: dict, state: dict, head: str) -> bool:
    """全量下载并落盘一个文件（本地缺失 / 无 patch / 冲突对齐时使用）。成功返回 True。

    raw.githubusercontent 优先；raw 被墙/超时自动回退 api.github.com contents API
    （与 _get_blob_sha 同通道，服务器可直连），两者都失败返回 False。
    """
    rel = item.get("filename", "")
    raw_url = item.get("raw_url", "")
    if raw_url:
        try:
            async with _eng.httpx.AsyncClient(timeout=15, verify=False) as dl:
                resp = await dl.get(_eng._normalize_raw_url(raw_url))
                resp.raise_for_status()
            _eng._write_local(root, rel, resp.text.splitlines(keepends=True))
            try:
                blob = await _eng._get_blob_sha(rel, head)
                _eng.set_file_blob(state, rel, blob, 1, 0)
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning("raw 下载 %s 失败，回退 API: %s", rel, e)
    return await _download_via_api(root, item, state, head)


async def _rebuild_from_patch(root: Path, item: dict, state: dict, head: str) -> bool:
    """用 new-file patch 在空内容上重建本地缺失文件（纯本地，不依赖网络下载）。

    仅处理 @@ -0,0 +1,N @@ 新增文件 hunk：本地无旧内容可保留，直接整块写入。
    成功落地返回 True（已写盘并更新 blob 追踪）；无新增 hunk 或全部 skip 返回 False，
    由调用方回退到全量下载。这样 raw 下载被墙/超时时新增文件仍能通过 patch 落地。
    """
    rel = item.get("filename", "")
    patch_text = item.get("patch", "")
    if not patch_text:
        return False
    hunks = patcher.parse_patch(patch_text)
    new_hunks = [h for h in hunks if h.old_start == 0 and h.old_count == 0]
    if not new_hunks:
        return False
    try:
        merged, aok, sk, _skd = patcher.apply_hunks_detailed([], new_hunks)
    except AttributeError:
        # 兼容旧版 patcher.py（服务器尚未同步 apply_hunks_detailed）
        merged, aok, sk = patcher.apply_hunks([], new_hunks)
    if aok <= 0:
        return False
    _eng._write_local(root, rel, merged)
    try:
        blob = await _eng._get_blob_sha(rel, head)
        _eng.set_file_blob(state, rel, blob, aok, sk)
    except Exception:
        pass
    return True


async def _apply_production(
    files: list[dict], root: Path, head: str, state: dict, progress=None,
) -> dict:
    """逐文件应用 patch/diff 到生产（沿用现有 patcher 行级合并）。

    事务化返回 dict：
        ok            成功落地/应用的 hunk 数（含全量下载计 1）
        skip          被跳过的 hunk 数
        skip_details  跳过明细 {file, old_start, reason}
        failed        未能落地的文件（无保留价值、会导致半新半旧）→ 触发整体回滚
        partial       已部分应用（存在 hunk 跳过、保留本地改动）的文件，不推进版本
    """
    ok = 0
    skip = 0
    skip_details: list[dict] = []
    failed: list[str] = []
    partial: list[str] = []
    total = len(files)

    # ── 预检：与远程 base(remote_sha) 失配比例 ──────────────
    # 同项目多机器人/多部署共享仓库时，本机服务器文件可能与仓库长期脱节
    # （历史遗留、其他实例改动、手工覆盖等）。若大面积失配说明本地状态整体
    # 不可信，放弃逐文件 patch，整批以远程为权威全量对齐 HEAD，保证能应用最新更新。
    base_sha = state.get("remote_sha", "")
    base_blobs: dict[str, str] = {}
    mismatch = cmp_total = 0
    if base_sha:
        for item in files:
            rel = item.get("filename", "")
            if not rel or item.get("status") == "removed" or not (root / rel).exists():
                continue
            cmp_total += 1
            try:
                b = await _eng._get_blob_sha(rel, base_sha)
            except Exception:
                b = ""
            if b:
                base_blobs[rel] = b
                if b != _eng.compute_local_blob(root, rel):
                    mismatch += 1
    force_full = cmp_total > 0 and mismatch / cmp_total >= 0.3
    if force_full:
        logger.warning("检测到 %d/%d 个文件与远程 base 不一致，服务器状态不可信，整批全量对齐 HEAD",
                       mismatch, cmp_total)

    for idx, item in enumerate(files, start=1):
        rel = item.get("filename", "")
        status = item.get("status", "")
        patch_text = item.get("patch", "")
        await _report(progress, f"正在应用 {idx}/{total}: {rel}")

        if status == "removed":
            local = root / rel
            if local.exists():
                local.unlink()
            ok += 1
            continue

        # force_full：预检发现大面积失配 → 已存在文件全部以远程为权威对齐 HEAD
        if force_full and (root / rel).exists():
            if await _download_full(root, item, state, head):
                ok += 1
            else:
                failed.append(rel)
            continue

        # 本地文件缺失：diff 的 hunk 只在旧文件存在时才能按上下文定位合并；
        # 空文件上除 new-file（@@ -0,0 +1,N @@）外会全体 skip → 永不落盘
        # （历史更新曾因此让 music_status.py 一直缺失，/listening 报 ModuleNotFoundError）。
        # 缺失文件无本地内容可保留 → 先用 new-file patch 本地重建（不依赖网络），
        # 重建不了再全量下载兜底；两者都失败才记入 failed（触发整体回滚）。
        if not (root / rel).exists():
            if await _rebuild_from_patch(root, item, state, head):
                ok += 1
            elif await _download_full(root, item, state, head):
                ok += 1
            else:
                failed.append(rel)
            continue

        # 本地已存在但无 patch → 本次无内容变更（避免覆盖本地改动），跳过
        if not patch_text:
            continue

        # ★ 本地与远程 base 强制对齐：diff 的 base 是 state.remote_sha，
        # 若本地文件 blob ≠ 远程 base blob（被 scp/手动编辑/历史遗留改过），
        # patch 上下文必然对不上 → 直接以远程为权威全量对齐 HEAD，
        # 保证"外部改动过的核心文件也能应用最新更新"，而不是 patch 失败 → 回滚。
        # base_blobs 来自预检缓存（未缓存的文件预检时不存在/removed，不会走到这里）。
        base_blob = base_blobs.get(rel, "")
        if base_blob and base_blob != _eng.compute_local_blob(root, rel):
            logger.warning("文件 %s 本地与远程 base 不一致（外部改动/历史遗留），强制对齐 HEAD", rel)
            if await _download_full(root, item, state, head):
                ok += 1
                continue
            failed.append(rel)
            continue

        hunks = patcher.parse_patch(patch_text)
        if not hunks:
            # 有 patch 文本却解析不出 hunk（二进制/重命名等）：非新增非删除，
            # 无内容落地，避免误判为失败，仅计数为 skip
            skip += 1
            continue

        try:
            merged, aok, sk, skd = patcher.apply_hunks_detailed(
                _eng._read_local(root, rel), hunks
            )
        except AttributeError:
            # 兼容旧版 patcher.py（服务器尚未同步 apply_hunks_detailed）：
            # 退化为 apply_hunks，仅得到 (merged, aok, sk)，跳过明细记为空。
            merged, aok, sk = patcher.apply_hunks(
                _eng._read_local(root, rel), hunks
            )
            skd = []
        if aok > 0:
            _eng._write_local(root, rel, merged)
            try:
                blob = await _eng._get_blob_sha(rel, head)
                _eng.set_file_blob(state, rel, blob, aok, sk)
            except Exception:
                pass
            ok += aok
            skip += sk
            if sk > 0:
                # 存在 hunk 跳过：若为「上下文不匹配」（本地与远程 base 不一致，
                # 典型如服务器文件停留在更旧版本），hunk 滑动匹配不上 → 用全量下载
                # 对齐远程，避免"半新半旧 + 不推进版本"卡死循环。
                no_ctx = any(d.get("reason") == "no_context" for d in skd)
                if no_ctx and await _download_full(root, item, state, head):
                    ok += 1
                    skip -= sk
                else:
                    partial.append(rel)  # 有 hunk 跳过 → 未完全落地
        else:
            # 有有效 patch 却一个 hunk 都未应用 → 远程代码未落地。
            # 先尝试全量下载对齐（raw→API 双通道），仍失败才判失败。
            if await _download_full(root, item, state, head):
                ok += 1
            else:
                failed.append(rel)
        for d in skd:
            skip_details.append({"file": rel, "old_start": d.get("old_start", 0),
                                 "reason": d.get("reason", "unknown")})
    return {
        "ok": ok, "skip": skip, "skip_details": skip_details,
        "failed": failed, "partial": partial,
    }


def _format_skip_details(skip_details: list[dict], limit: int = 8) -> str:
    """把跳过明细格式化为人类可读文本。

    每项形如 "- <file> 第 <old_start> 行: <原因>"，最多展示 limit 项，
    超出折叠为「… 其余 N 项已折叠」。
    """
    reason_map = {
        "protected": "与保护区重叠",
        "no_context": "上下文不匹配（本地改动较大）",
        "out_of_range": "目标行号超出文件范围",
        "no_payload": "新文件块为空",
        "unknown": "未知原因",
    }
    if not skip_details:
        return "跳过明细: 无"
    lines = [f"跳过 hunks 明细（共 {len(skip_details)} 项）:"]
    shown = skip_details[:limit]
    for d in shown:
        reason = reason_map.get(d.get("reason", ""), d.get("reason", "未知原因"))
        lines.append(f"- {d.get('file', '?')} 第 {d.get('old_start', 0)} 行: {reason}")
    if len(skip_details) > limit:
        lines.append(f"… 其余 {len(skip_details) - limit} 项已折叠")
    return "\n".join(lines)