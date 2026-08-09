"""
错误报告分析模块
- 检测引用消息中是否包含"错误报告"压缩包
- 下载、解压、读取多点日志：latest.log / 游戏崩溃前的输出.txt / [版本].json
- 忽略 PCL 启动器日志.txt、启动脚本.bat
- 构造特殊提示词发送给LLM
"""

from __future__ import annotations

import httpx
import zipfile
import tempfile
import os
import re
from typing import Optional

from core.logger import get_logger

logger = get_logger("error_report")

# 优先提取的文件名
_PRIORITY_FILES = [
    "latest.log",
    "游戏崩溃前的输出.txt",
    "hs_err_pid",  # Java 崩溃日志 (hs_err_pidXXXXX.log)
]

# 忽略的文件名关键词
_IGNORE_PATTERNS = [
    "PCL 启动器日志.txt",
    "启动脚本.bat",
    "启动脚本.cmd",
    "PCL2_Setup.exe",  # PCL 主程序
]


def check_filename_has_keyword(filename: str, keyword: str = "错误报告") -> bool:
    """检查文件名是否包含指定关键词（支持 URL 编码自动解码）"""
    from urllib.parse import unquote
    decoded = unquote(filename)
    return keyword in decoded


async def download_file(url: str, save_path: str, timeout: float = 30.0) -> bool:
    """下载文件到指定路径（支持 HTTP/HTTPS + 本地文件路径）"""
    try:
        # 本地文件路径：直接复制，不走 HTTP
        if not url.startswith(("http://", "https://")):
            import shutil
            import os
            src = url
            if src.startswith("file://"):
                from urllib.parse import unquote
                src = unquote(src[7:])
            logger.info("本地复制: %s → %s", src, save_path)
            shutil.copy2(src, save_path)
            logger.info("本地复制成功: %s (%d bytes)", save_path, os.path.getsize(save_path))
            return True

        logger.info("下载: %s → %s", url[:80], save_path)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            with open(save_path, 'wb') as f:
                f.write(response.content)
        logger.info("下载成功: %s (%d bytes)", save_path, len(response.content))
        return True
    except Exception as e:
        logger.error("下载/复制失败: %s", e)
        return False


def _read_text(path: str, max_len: int = 8000) -> str:
    """读取文本文件，截断，标注来源文件名"""
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        if len(content) > max_len:
            content = content[:max_len] + f"\n\n[已截断，原{len(content)}字符]"
        return content
    except Exception:
        return ""


def _should_ignore(name: str) -> bool:
    """判断文件名是否应忽略"""
    basename = os.path.basename(name)
    for pat in _IGNORE_PATTERNS:
        if pat in basename:
            return True
    return False


def extract_report_contents(zip_path: str, extract_dir: str) -> dict:
    """
    扫描 zip 中的有用文件，返回 {来源标签: 内容} 字典。
    
    提取策略:
    1. 优先匹配 latest.log / 游戏崩溃前的输出.txt / hs_err_pid*.log
    2. 再捞其他 .log 或 .txt 文件
    3. 最后捞 .json（版本信息、mod 列表等，取最大的）
    4. 忽略 PCL 启动器日志、启动脚本
    
    Returns:
        {"latest.log": "内容...", "游戏崩溃前的输出.txt": "内容...", ...}
    """
    try:
        logger.info("扫描压缩包: %s", zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            all_names = zf.namelist()
            logger.info("压缩包内含 %d 个文件", len(all_names))

            found = {}  # {标签: 解压后路径}
            
            # 第1轮：匹配优先文件
            for name in all_names:
                if _should_ignore(name):
                    continue
                basename = os.path.basename(name)
                for pf in _PRIORITY_FILES:
                    if pf in basename:
                        if pf not in found:  # 只取第一个
                            zf.extract(name, extract_dir)
                            found[pf] = os.path.join(extract_dir, name)
                            logger.info("  优先文件: %s", name)
                        break

            # 第2轮：捞剩余的 .log / .txt（排除已取 + 忽略的）
            for name in all_names:
                if _should_ignore(name):
                    continue
                basename = os.path.basename(name)
                if basename.lower().endswith(('.log', '.txt')):
                    label = f"[txt] {basename}"
                    if label not in found:
                        zf.extract(name, extract_dir)
                        found[label] = os.path.join(extract_dir, name)
                        logger.info("  文本文件: %s", name)

            # 第3轮：识别版本 json（只看文件名，不读内容省 token）
            json_candidates = []
            for name in all_names:
                if _should_ignore(name):
                    continue
                if name.lower().endswith('.json'):
                    info = zf.getinfo(name)
                    json_candidates.append((info.file_size, name))
            if json_candidates:
                json_candidates.sort(reverse=True)
                _, best = json_candidates[0]
                basename = os.path.basename(best)
                # 只记录文件名作为版本标识，不读内容
                found["[json] 版本文件"] = basename
                logger.info("  JSON文件(仅文件名): %s", basename)

        # 读取所有内容
        result = {}
        version_info = None  # 从 json 文件名提取的版本号
        for label, path in found.items():
            if label == "[json] 版本文件":
                version_info = path  # path 就是文件名 str，如 "1.8.9.json"
                continue
            content = _read_text(path, max_len=8000)
            if content:
                result[label] = content
                logger.info("  读取: %s → %d字符", label, len(content))
        if version_info:
            result["Minecraft 版本"] = version_info
        return result

    except Exception as e:
        logger.error("解压/读取失败: %s", e)
        return {}


def _build_combined_content(report_data: dict) -> Optional[str]:
    """合并多条日志，日志/崩溃排前，版本信息排最后"""
    if not report_data:
        return None

    parts = []
    version_info = report_data.pop("Minecraft 版本", None)

    for key, content in report_data.items():
        parts.append(f"=== {key} ===")
        parts.append(content)
        parts.append("")

    if version_info:
        # 从文件名提取版本号，如 "1.8.9.json" → "1.8.9"
        ver = version_info.replace(".json", "").replace("_", ".")
        parts.append(f"=== Minecraft 版本 ===")
        parts.append(f"版本: {ver} (来自文件: {version_info})")
        parts.append("")

    combined = "\n".join(parts).strip()
    if len(combined) > 20000:
        combined = combined[:20000] + "\n\n[内容已截断]"
    return combined


async def process_error_report(file_url: str, filename: str, sender_name: str) -> Optional[str]:
    """
    处理错误报告：下载 → 解压 → 读取所有有用文件 → 合并返回
    """
    if not check_filename_has_keyword(filename):
        from urllib.parse import unquote
        logger.debug("文件名不含关键词: %s (decoded: %s)", filename, unquote(filename))
        return None

    logger.info("检测到错误报告: %s (url=%s...)", filename, file_url[:60])

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "error_report.zip")
        if not await download_file(file_url, zip_path):
            return None

        report_data = extract_report_contents(zip_path, tmp_dir)
        if not report_data:
            logger.warning("压缩包中未找到可分析的文件")
            return None

        combined = _build_combined_content(report_data)
        if combined:
            logger.info("错误报告处理: %d文件 → %d字符",
                       len(report_data), len(combined))
        return combined


def build_error_report_prompt(sender_name: str, log_content: str, original_msg: str = "") -> str:
    """构造错误报告分析的提示词"""
    parts = [
        f"@{sender_name} 上传了一份 Minecraft 错误报告，请你帮忙分析。",
        "",
        "【分析要求 — 你必须严格遵守】",
        "1. 用你一贯的人设回复，语气亲切但技术分析必须专业到位",
        "2. 自然引用来源文件：分析每条问题时说明从哪个文件看到的，"
           "例如「从 latest.log 看到...」「游戏崩溃前的输出.txt 里显示...」",
        "3. 快速定位 ERROR / FATAL / WARN / Exception / crash，逐条解释含义",
        "4. 如果含 crash report 堆栈，指出崩溃的类/方法/行号",
        "5. 根因分析：mod 冲突？Forge/Fabric 版本不对？内存溢出？Java/驱动问题？",
        "6. 给出 3~4 条可操作方案，按「最可能 → 备选」排序",
        "7. 若涉及 mod，列出怀疑的 mod 名和排查方法（二分法禁用 mod）",
        "8. 有显卡/OpenGL 错误时提醒检查驱动和 Java 参数",
        "9. 结尾给出鼓励 + 求助建议（MCBBS、CurseForge 评论区）",
        "10. 输出可以长一些（400~600字），把问题讲透彻",
    ]

    if original_msg:
        parts.append(f"\n用户原话：{original_msg}")

    parts.append("\n错误报告内容（多个文件）：")
    parts.append(log_content)
    parts.append("\n现在开始分析：")

    return "\n".join(parts)
