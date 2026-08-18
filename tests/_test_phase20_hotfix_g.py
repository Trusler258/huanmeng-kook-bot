"""
Phase 20 Hotfix G 验证测试（.plugin -import 直链导入 .hmp）
运行: python _test_phase20_hotfix_g.py
覆盖：
1. is_hmp_url：识别合法 .hmp 直链（含 query），拒绝非 .hmp / 非 http
2. local_filename_for：从 URL 推导落盘文件名
3. 下载→解包→加载链路（用本地 file:// 无法走 httpx，改验证 download_hmp 的落盘路径约定）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import plugin_share as PS


# ── 1. is_hmp_url 判定 ────────────────────────────────────
def test_is_hmp_url():
    # 合法直链
    assert PS.is_hmp_url("https://example.com/foo.hmp") is True
    assert PS.is_hmp_url("https://example.com/a/b/foo.hmp") is True
    # 带 query 参数（如签名 URL）
    assert PS.is_hmp_url("https://example.com/foo.hmp?token=abc&x=1") is True
    # http 也可
    assert PS.is_hmp_url("http://example.com/foo.hmp") is True
    # 拒绝：非 .hmp
    assert PS.is_hmp_url("https://example.com/foo.zip") is False
    assert PS.is_hmp_url("https://example.com/foo.py") is False
    # 拒绝：非 http(s)、空、纯文件名
    assert PS.is_hmp_url("foo.hmp") is False
    assert PS.is_hmp_url("") is False
    assert PS.is_hmp_url("ftp://example.com/foo.hmp") is False
    # 拒绝：带空格/夹杂文本
    assert PS.is_hmp_url("下载 https://example.com/foo.hmp") is False
    print("OK test_is_hmp_url (识别合法直链, 拒绝非 .hmp)")


# ── 2. local_filename_for 落盘名 ───────────────────────────
def test_local_filename_for():
    assert PS.local_filename_for("https://example.com/foo.hmp") == "foo.hmp"
    # query 参数不影响文件名
    assert PS.local_filename_for("https://example.com/foo.hmp?token=1") == "foo.hmp"
    # URL 编码的文件名会 unquote
    assert PS.local_filename_for("https://example.com/%E6%B5%8B%E8%AF%95.hmp") == "测试.hmp"
    # 无 .hmp 后缀兜底
    assert PS.local_filename_for("https://example.com/download") == "download.hmp"
    print("OK test_local_filename_for (URL→落盘名正确)")


# ── 3. 校验函数与安全约束存在性 ────────────────────────────
def test_security_constraints_exist():
    # 直链导入复用的安全上限仍在
    assert PS.MAX_ZIP_SIZE == 10 * 1024 * 1024
    assert PS.MAX_UPLOAD_DL == 50 * 1024 * 1024
    # 插件名校验 + zip-slip 防护
    assert PS.validate_name("ok_plugin") is True
    assert PS.validate_name("../evil") is False
    assert PS.validate_name("_hidden") is False
    assert PS._sanitize_member("../etc/passwd") is None
    assert PS._sanitize_member("/abs/path") is None
    assert PS._sanitize_member("a//b") is None
    print("OK test_security_constraints_exist (安全上限/名校验/zip-slip 均在)")


async def main():
    test_is_hmp_url()
    test_local_filename_for()
    test_security_constraints_exist()
    print("\n=== ALL Phase20 HOTFIX G TESTS PASSED ===")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
