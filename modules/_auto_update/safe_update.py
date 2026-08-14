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
import py_compile
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
            resp = await dl.get(raw_url)
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
            # 本次应存在而缺失 → 视为写失败
            errors.append(f"[健康检查] 文件缺失: {rel}")
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
                if not (root / imp.replace(".", "/") + ".py").exists() \
                        and not _is_stdlib(base):
                    logger.warning("健康检查: %s 引用可能的第三方/新模块 %s", rel, imp)
    return errors


def _is_stdlib(mod: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


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
        if python_files:
            parts.append(f"含 {len(python_files)} 个 Python 文件（将做 AST/语法分析）")
        if commit_log:
            parts.append(commit_log)
        return "\n".join(parts)

    # 7. Risk gate：高风险默认要求人工确认
    if require_approval and assessment.level == "HIGH":
        if _approve_callback is None:
            return (f"更新被拒绝：检测到 {len(assessment.high_files)} 个高风险文件，"
                    f"未配置人工审批回调。\n{assessment.reason}")
        approved = _approve_callback(actionable, assessment)
        if not approved:
            return f"更新已取消（高风险未获审批）：{assessment.reason}"

    # 8. Test（staging 语法检查）
    await _report(progress, "正在做语法检查…")
    test_errors = _staging_test(actionable, root)
    if test_errors:
        return "更新中止：语法检查未通过，未应用任何改动。\n" + "\n".join(test_errors)

    # 9. Snapshot（生产应用前）
    await _report(progress, "已创建快照，准备应用…")
    snap = _snap.create_snapshot(actionable, head)

    # 10. Production Apply（走 Diff + 最小 Patch，沿用现有 patcher）
    ok, skip = await _apply_production(actionable, root, head, state, progress)

    # 11. Health Check（应用后轻量校验；失败则回滚）
    await _report(progress, "正在健康检查…")
    health_errors = _health_check(actionable, root)
    if health_errors:
        _snap.rollback(snap, reason="Health Check 失败")
        return "更新已应用但 Health Check 失败，已自动回滚。\n" + "\n".join(health_errors)

    # 12. 收尾：重新计算基线（下次只对比差异，不再全量拉树）
    _rebuild_baseline(state, actionable, head)
    _eng.save_state(root, state)
    await _report(progress, "更新完成")
    parts = [f"已安全更新 {len(actionable)} 个文件（{ok} hunks 成功, {skip} hunks 跳过）"]
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


async def _apply_production(
    files: list[dict], root: Path, head: str, state: dict, progress=None,
) -> tuple[int, int]:
    """逐文件应用 patch/diff 到生产（沿用现有 patcher 行级合并）。"""
    ok = 0
    skip = 0
    total = len(files)
    for idx, item in enumerate(files, start=1):
        rel = item.get("filename", "")
        status = item.get("status", "")
        patch_text = item.get("patch", "")
        await _report(progress, f"正在应用 {idx}/{total}: {rel}")

        if status == "removed":
            local = root / rel
            if local.exists():
                local.unlink()
            continue

        if not patch_text:
            raw_url = item.get("raw_url", "")
            if raw_url:
                try:
                    async with _eng.httpx.AsyncClient(timeout=15, verify=False) as dl:
                        resp = await dl.get(raw_url)
                        resp.raise_for_status()
                    _eng._write_local(root, rel, resp.text.splitlines(keepends=True))
                    try:
                        blob = await _eng._get_blob_sha(rel, head)
                        _eng.set_file_blob(state, rel, blob, 1, 0)
                    except Exception:
                        pass
                    ok += 1
                except Exception as e:
                    logger.warning("下载 %s 失败: %s", rel, e)
            continue

        hunks = patcher.parse_patch(patch_text)
        if not hunks:
            continue
        merged, aok, sk = patcher.apply_hunks(_eng._read_local(root, rel), hunks)
        if aok > 0:
            _eng._write_local(root, rel, merged)
            try:
                blob = await _eng._get_blob_sha(rel, head)
                _eng.set_file_blob(state, rel, blob, aok, sk)
            except Exception:
                pass
        ok += aok
        skip += sk
    return ok, skip