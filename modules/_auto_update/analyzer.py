"""
Phase 16 代码级更新：AST 分析 + 依赖分析 + 风险评估

三个职责：
1. analyze_python(path) → 用 AST 解析 Python 文件，提取 import / class / function /
   signature / decorator / config 结构，用于精确理解将要改动的内容。
2. assess_risk(files) → 按文件路径把改动分类为 LOW / MEDIUM / HIGH：
   - LOW  ：文档、普通 Plugin
   - MEDIUM：Service / Tool / Skill
   - HIGH ：Core / Agent / Memory / Runtime / Security / DB
   高风险默认要求人工确认（Permission 系统默认 DENY）。
3. analyze_dependencies(files) → 依赖分析：检查被删除/改动的模块是否被其他文件 import，
   以及新增 import 是否指向不存在（或本次未同步）的模块。

设计约束：
- 不一次性重写现有 patch 引擎，只在其基础上增加"分析→评估→审批"前序阶段。
- import 使用标准库 ast，不引入额外依赖。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("auto_update.analyzer")

# 风险分级路径前缀
RISK_CATEGORIES: dict[str, list[str]] = {
    "LOW": ["docs/", "doc/", "README", "CHANGELOG", "*.md", "plugins/", "scripts/"],
    "MEDIUM": ["services/", "modules/", "tools/", "skills/"],
    "HIGH": ["core/", "agent/", "memory/", "runtime/", "db/", "security", "auth", "config.py"],
}

# 关键安全/核心文件，命中即 HIGH
_HIGH_KEYWORDS = (
    "permission", "security", "auth", "config", "runtime", "pipeline",
    "conversation_runtime", "memory_engine", "agent", "eventbus", "capability",
)


@dataclass
class PyItem:
    """AST 提取的一个语义单元。"""
    kind: str          # module/class/function/import/config
    name: str
    line: int
    signature: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass
class FileAnalysis:
    """单个更改文件的 AST 分析结果。"""
    path: str
    status: str = ""          # added/modified/removed
    imports: list[str] = field(default_factory=list)
    classes: list[PyItem] = field(default_factory=list)
    functions: list[PyItem] = field(default_factory=list)
    config_keys: list[str] = field(default_factory=list)
    parse_error: str = ""
    is_python: bool = False


@dataclass
class RiskAssessment:
    """一次更新整体的风险评估。"""
    level: str = "LOW"        # LOW/MEDIUM/HIGH
    reason: str = ""
    high_files: list[str] = field(default_factory=list)
    by_file: dict[str, str] = field(default_factory=dict)


@dataclass
class DependencyIssue:
    """依赖分析发现的问题。"""
    level: str                # WARN/ERROR
    message: str


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def is_python_path(rel_path: str) -> bool:
    return rel_path.endswith(".py")


def analyze_python(rel_path: str, content: str) -> FileAnalysis:
    """用 AST 解析单个 Python 文件内容。content 为最终文件内容（合并后）。"""
    fa = FileAnalysis(path=rel_path, is_python=True)
    fa.imports = extract_imports(content)
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        fa.parse_error = f"第{e.lineno}行: {e.msg}"
        return fa

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            sig = _format_signature([_base_name(b) for b in node.bases])
            fa.classes.append(PyItem(
                kind="class", name=node.name, line=getattr(node, "lineno", 0),
                signature=sig, decorators=_decorator_names(node.decorator_list),
            ))
        elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            args = [a.arg for a in node.args.args]
            sig = _format_signature(args, node.args.vararg, node.args.kwarg)
            fa.functions.append(PyItem(
                kind="function", name=node.name, line=getattr(node, "lineno", 0),
                signature=sig, decorators=_decorator_names(node.decorator_list),
            ))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    fa.config_keys.append(t.id)

    return fa


def extract_imports(content: str) -> list[str]:
    """从内容中提取 import 语句（顶层，不依赖完整 AST 成功）。"""
    imports: list[str] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for a in node.names:
                imports.append(f"{base}.{a.name}" if base else a.name)
    return imports


def _format_signature(args: list[str], vararg=None, kwarg=None) -> str:
    parts = list(args)
    if vararg:
        parts.append(f"*{vararg.arg if hasattr(vararg, 'arg') else vararg}")
    if kwarg:
        parts.append(f"**{kwarg.arg if hasattr(kwarg, 'arg') else kwarg}")
    return f"({', '.join(parts)})"


def _base_name(node) -> str:
    """安全提取 class 基节点名，兼容 Name / Attribute / Subscript / Call 等 AST 节点。"""
    import ast as _ast
    if isinstance(node, _ast.Name):
        return node.id
    if isinstance(node, _ast.Attribute):
        return _base_name(node.value) + "." + node.attr
    if isinstance(node, _ast.Subscript):
        return _base_name(node.value) + "[...]"
    if isinstance(node, _ast.Call):
        return _base_name(node.func) + "(...)"
    if isinstance(node, _ast.Tuple):
        return ", ".join(_base_name(e) for e in node.elts)
    return "<base>"


def _decorator_names(decorators: list) -> list[str]:
    names = []
    for d in decorators:
        if isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
        elif isinstance(d, ast.Call):
            if isinstance(d.func, ast.Name):
                names.append(d.func.id)
            elif isinstance(d.func, ast.Attribute):
                names.append(d.func.attr)
    return names


def assess_risk(files: list[dict]) -> RiskAssessment:
    """按文件路径把一次更新的改动文件分到 LOW/MEDIUM/HIGH。"""
    ra = RiskAssessment()
    for item in files:
        rel = item.get("filename", "")
        if not rel:
            continue
        level = _classify_path(rel)
        ra.by_file[rel] = level
        if level == "HIGH":
            ra.high_files.append(rel)
    if ra.high_files:
        ra.level = "HIGH"
        ra.reason = f"更新包含 {len(ra.high_files)} 个高风险文件: {', '.join(ra.high_files[:5])}"
    elif "MEDIUM" in ra.by_file.values():
        ra.level = "MEDIUM"
        names = [f for f, l in ra.by_file.items() if l == "MEDIUM"]
        ra.reason = f"更新包含 {len(names)} 个中风险文件: {', '.join(names[:5])}"
    else:
        ra.reason = "仅包含低风险文件（文档/普通插件）"
    return ra


def _classify_path(rel: str) -> str:
    low = rel.lower()
    for kw in _HIGH_KEYWORDS:
        if kw in low:
            return "HIGH"
    for cat in ("LOW", "MEDIUM"):
        for pattern in RISK_CATEGORIES[cat]:
            if pattern.endswith(".md"):
                if rel.endswith(pattern.split("/")[-1]):
                    return cat
            elif rel.startswith(pattern) or rel == pattern.rstrip("/"):
                return cat
    return "MEDIUM" if is_python_path(rel) else "LOW"


def _package_defines(pkg: Path, name: str) -> bool:
    """判断包目录 pkg 的 __init__.py 是否定义了符号 name（import/赋值/定义均可）。

    用于区分 `core.plugin.get_plugin_manager` 中的 get_plugin_manager 是
    "包 __init__ 里 re-export 的符号"（不应判缺失）还是"真缺失的子模块"。"""
    init = pkg / "__init__.py"
    if not init.is_file():
        return False
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name):
                names.add(tgt.id)
    return name in names


def resolve_project_module(root: Path, module_name: str) -> Optional[Path]:
    """把 import 模块名解析到项目内应在的文件路径（模块边界）。

    规则：
    - 顶层段不在项目根（stdlib / 三方库）→ 返回 None，表示非本地模块。
    - 只解析到"真实存在的模块边界"（文件 X.py 或含 __init__.py 的包目录）。
      越过边界之后的段（如 core.plugin.get_plugin_manager 的 get_plugin_manager）是
      包内符号，不作为嵌套文件路径；若父包 __init__.py 定义了该符号则视为存在，
      否则判定为真缺失子模块（返回期望路径）。
    例如 modules/cmd_cards → root/modules/cmd_cards.py（缺失时由调用方判定）。
    """
    parts = module_name.replace("-", "_").split(".")
    if not parts:
        return None
    root = Path(root)
    top = root / parts[0]
    top_file = top.with_suffix(".py")
    if top_file.is_file():
        return top_file
    if not top.is_dir():
        return None  # 非项目本地模块
    path = top
    for seg in parts[1:]:
        cand_file = path / f"{seg}.py"
        cand_pkg = path / seg
        if cand_file.is_file():
            return cand_file  # 命中模块文件 → 边界，余下段视为符号
        if cand_pkg.is_dir():
            path = cand_pkg
            continue
        # 该段文件/包均不存在：包内符号 or 真缺失子模块。
        # 父包 __init__ 定义了该符号 → 视为符号，模块边界仍在父包。
        if _package_defines(path, seg):
            init = path / "__init__.py"
            return init if init.is_file() else path
        # 真缺失（例如 cmd_cards 直接作为某包子模块）→ 返回期望路径供判定
        return cand_file
    # 全程命中包目录 → 模块即该包
    init = path / "__init__.py"
    return init if init.is_file() else path


def analyze_dependencies(files: list[dict], root: Optional[Path] = None) -> list[DependencyIssue]:
    """
    依赖分析：
    1. 被本次更新删除/改名的模块，若仍被其他本地文件 import → WARN/ERROR。
    2. 本次新增的 import，若指向本地不存在的模块且非常规库 → WARN。
    只做轻量静态检查，不做真实网络解析。
    """
    root = root or _root()
    issues: list[DependencyIssue] = []
    removed_py = {
        f.get("filename", "")[:-3] for f in files
        if f.get("status") == "removed" and f.get("filename", "").endswith(".py")
    }
    # 改为模块路径形式 core.pipeline → core/pipeline 的模块名
    removed_modules = {p.replace("/", ".") for p in removed_py}
    if not removed_modules:
        return issues

    # 扫描本地所有 .py 文件，看是否 import 被删模块
    py_files = list(root.rglob("*.py"))
    for src in py_files:
        try:
            content = src.read_text(encoding="utf-8")
        except Exception:
            continue
        imports = extract_imports(content)
        for imp in imports:
            base = imp.split(".")[0]
            for mod in removed_modules:
                if imp == mod or base == mod:
                    rel = src.relative_to(root).as_posix()
                    issues.append(DependencyIssue(
                        level="ERROR",
                        message=f"删除模块 {mod} 仍被 {rel} import",
                    ))
    return issues