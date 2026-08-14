"""
Phase 16 测试：GitHub 安全代码级更新
覆盖：AST 分析 / 风险评估 / 依赖分析 / 快照回滚 / Staging 语法测试 / Diff 最小 Patch。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules._auto_update import analyzer
from modules._auto_update import snapshot as snap
from modules._auto_update import patcher


def test_ast_analysis():
    content = (
        "import os\n"
        "from core.config import load_roles_config\n"
        "def greet(name: str) -> str:\n"
        "    return f'hi {name}'\n"
        "class Greeter:\n"
        "    def __init__(self, prefix='hi'):\n"
        "        self.prefix = prefix\n"
        "MY_CONST = 42\n"
    )
    fa = analyzer.analyze_python("service.py", content)
    assert "os" in fa.imports
    assert "core.config.load_roles_config" in fa.imports
    assert any(f.name == "greet" for f in fa.functions)
    assert any(c.name == "Greeter" for c in fa.classes)
    assert "MY_CONST" in fa.config_keys
    assert fa.parse_error == ""
    print("✓ test_ast_analysis")


def test_ast_syntax_error():
    fa = analyzer.analyze_python("bad.py", "def broken(:\n")
    assert fa.parse_error != ""
    print("✓ test_ast_syntax_error")


def test_risk_assessment():
    files = [
        {"filename": "README.md", "status": "modified"},
        {"filename": "plugins/hello/main.py", "status": "modified"},
        {"filename": "services/llm.py", "status": "modified"},
        {"filename": "core/pipeline.py", "status": "modified"},
        {"filename": "core/security.py", "status": "modified"},
    ]
    ra = analyzer.assess_risk(files)
    assert ra.by_file["README.md"] == "LOW"
    assert ra.by_file["plugins/hello/main.py"] == "LOW"
    assert ra.by_file["services/llm.py"] in ("MEDIUM",)
    assert ra.by_file["core/pipeline.py"] == "HIGH"
    assert ra.by_file["core/security.py"] == "HIGH"
    assert ra.level == "HIGH"
    print("✓ test_risk_assessment")


def test_dependency_analysis():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 模拟：本地有文件 import 一个将被删除的模块
        (root / "app.py").write_text("import old_module\n", encoding="utf-8")
        files = [{"filename": "old_module.py", "status": "removed"}]
        issues = analyzer.analyze_dependencies(files, root)
        blockers = [d for d in issues if d.level == "ERROR"]
        assert blockers, "应检测到被删除模块仍被 import"
        print("✓ test_dependency_analysis")


def test_snapshot_rollback():
    from modules._auto_update import snapshot as snap
    import importlib
    importlib.reload(snap)
    sha = "b" * 40
    real_files = [{"filename": "modules/auto_update.py", "status": "modified"}]
    target = snap._root() / "modules/auto_update.py"
    orig = target.read_text(encoding="utf-8")
    try:
        s = snap.create_snapshot(real_files, sha)
        assert s is not None
        target.write_text("# corrupted\n", encoding="utf-8")
        n = snap.rollback(s, reason="test")
        assert n >= 1
        assert target.read_text(encoding="utf-8") == orig
        print("✓ test_snapshot_rollback (restored=%d)" % n)
    finally:
        # 无论成功与否都确保原文件内容恢复
        if target.read_text(encoding="utf-8") != orig:
            target.write_text(orig, encoding="utf-8")


def test_staging_syntax():
    from modules._auto_update import safe_update
    # 构造一个语法错误的 patch，应被 staging test 拦截
    files = [
        {"filename": "x.py", "status": "modified",
         "patch": "@@ -1,1 +1,1 @@\n-VERSION=1\n+VERSION=(\n"},
    ]
    # _compute_merged 会尝试合并；语法错误应在 _staging_test 检测
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "x.py").write_text("VERSION=1\n", encoding="utf-8")
        errors = safe_update._staging_test(files, root)
        assert errors, "语法错误的文件应被 Staging Test 拦截"
    print("✓ test_staging_syntax")


def test_diff_minimal_patch():
    # 验证 patcher 只改变化行，保护未涉及行
    local = ["a=1\n", "b=2\n", "c=3\n"]
    patch_text = "@@ -1,3 +1,3 @@\n a=1\n-b=2\n+B=2\n c=3\n"
    hunks = patcher.parse_patch(patch_text)
    merged, ok, sk = patcher.apply_hunks(local, hunks)
    assert ok == 1
    assert merged == ["a=1\n", "B=2\n", "c=3\n"]
    print("✓ test_diff_minimal_patch")


def main():
    test_ast_analysis()
    test_ast_syntax_error()
    test_risk_assessment()
    test_dependency_analysis()
    test_snapshot_rollback()
    test_staging_syntax()
    test_diff_minimal_patch()
    print("\nPhase 16 全部测试通过 ✓")


if __name__ == "__main__":
    main()