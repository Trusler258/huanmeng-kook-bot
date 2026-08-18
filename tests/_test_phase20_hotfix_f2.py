"""Hotfix F2：更新引擎本地 blob 自检逻辑测试。

场景：本地文件被外部改动（blob 与 state 记录不一致）时，
_apply_production 应走全量对齐而非 patch 失败回滚。

通过构造一个最小 fake item + monkeypatch _download_full 验证分支。
"""
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules._auto_update import engine as _eng
from modules._auto_update import safe_update as _su


def fake_blob(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\x00" + content).hexdigest()


async def main():
    # 1. compute_local_blob 基础正确性
    tmp = Path("data_tmp_blob_test.txt")
    tmp.write_bytes(b"hello\n")
    want = hashlib.sha1(b"blob 6\x00hello\n").hexdigest()
    got = _eng.compute_local_blob(Path("."), tmp.name)
    assert got == want, f"compute_local_blob 错误: {got} != {want}"
    print("OK compute_local_blob 计算正确:", got[:12], "…")
    tmp.unlink()

    # 2. 空/缺失文件 → 返回 ""
    assert _eng.compute_local_blob(Path("."), "no_such_file_xyz.py") == ""
    print("OK 缺失文件返回空")

    # 3. blob 不一致时走全量对齐分支
    root = Path("data_tmp_update_root")
    root.mkdir(exist_ok=True)
    (root / "test_mod.py").write_text("print('externally modified')\n", encoding="utf-8")

    item = {
        "filename": "test_mod.py",
        "status": "modified",
        "patch": "--- a/test_mod.py\n+++ b/test_mod.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n",
        "raw_url": "https://raw.githubusercontent.com/x/y/master/test_mod.py",
    }
    state = {"files": {"test_mod.py": {"blob_sha": "some_old_blob"}}}

    calls = {"download": 0}

    async def fake_download_full(root2, item2, state2, head):
        calls["download"] += 1
        (root2 / item2["filename"]).write_text("print('downloaded')\n", encoding="utf-8")
        return True

    _su._download_full = fake_download_full
    res = await _su._apply_production([item], root, "HEAD", state)
    assert calls["download"] == 1, f"应触发全量下载: {calls}"
    assert "test_mod.py" not in res["failed"], f"不应失败: {res['failed']}"
    assert res["ok"] == 1, f"应计数 1: {res}"
    content = (root / "test_mod.py").read_text(encoding="utf-8")
    assert "downloaded" in content, f"应写入下载内容: {content}"
    print("OK blob 不一致 → 全量对齐（非回滚）")

    # 4. blob 一致时走正常 patch（不触发下载）
    same_content = b"print('same')\n"
    (root / "test_same.py").write_bytes(same_content)
    item2 = {
        "filename": "test_same.py",
        "status": "modified",
        "patch": "--- a/test_same.py\n+++ b/test_same.py\n@@ -1 +1 @@\n-print('same')\n+print('same2')\n",
        "raw_url": "x",
    }
    state2 = {"files": {"test_same.py": {"blob_sha": fake_blob(same_content)}}}
    calls["download"] = 0
    res2 = await _su._apply_production([item2], root, "HEAD", state2)
    assert calls["download"] == 0, f"blob 一致不应触发下载: {calls}"
    assert "test_same.py" not in res2["failed"], f"不应失败: {res2['failed']}"
    assert "same2" in (root / "test_same.py").read_text(encoding="utf-8")
    print("OK blob 一致 → 正常 patch 应用")

    # 清理
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    print("\n=== ALL HOTFIX F2 TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
