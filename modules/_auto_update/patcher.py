"""
Unified Diff 补丁解析与行级合并

补丁格式:
  @@ -start,count +start,count @@ context
   context line      ← 两端都有的行，用于定位
  -removed line      ← 旧版有、新版无
  +added line        ← 新版有、旧版无

算法:
  1. 解析 @@ header → old_start, old_count, new_start, new_count
  2. 提取上下文行（不带 +/- 前缀的行）
  3. 在本地文件中滑动匹配上下文 → 精确行号
  4. 上下文完全匹配 → 原地替换 old_count 行为 new 行
  5. 上下文不匹配 → 跳过，计为 skipped
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class PatchHunk:
    old_start: int          # 旧文件起始行号 (1-indexed)
    old_count: int          # 旧文件行数
    new_start: int          # 新文件起始行号
    new_count: int          # 新文件行数
    raw_lines: list[str]    # 原始 hunk 行 (含 @@ header 和尾部)
    ctx_lines: list[str]    # 仅上下文行 (不带 +/- 前缀的纯内容)
    del_lines: list[str]    # 要删除的行内容
    add_lines: list[str]    # 要插入的行内容
    _ctx_idx: list[int] = field(default_factory=list)  # ctx_lines 在 raw 中的索引


def parse_patch(patch_text: str) -> list[PatchHunk]:
    """
    解析 unified diff 补丁文本，返回有序 hunk 列表。

    Args:
        patch_text: GitHub compare API 返回的 .patch 字段内容

    Returns:
        PatchHunk 列表，按文件中出现顺序排列
    """
    lines = patch_text.split("\n")
    hunks: list[PatchHunk] = []
    current_header: str | None = None
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("@@"):
            if current_header is not None and current_lines:
                hunks.append(_make_hunk(current_header, current_lines))
            current_header = line
            current_lines = []
        elif current_header is not None:
            current_lines.append(line)

    # 最后一个 hunk
    if current_header is not None and current_lines:
        hunks.append(_make_hunk(current_header, current_lines))

    return _reorder_hunks(hunks)


def _make_hunk(header: str, body_lines: list[str]) -> PatchHunk:
    """从 @@ header + body 构建 PatchHunk"""
    # 解析 @@ -old_start,old_count +new_start,new_count @@
    import re
    m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", header)
    if not m:
        return PatchHunk(0, 0, 0, 0, [], [], [], [])

    old_start = int(m.group(1))
    old_count = int(m.group(2) or 1)
    new_start = int(m.group(3))
    new_count = int(m.group(4) or 1)

    ctx_lines: list[str] = []
    del_lines: list[str] = []
    add_lines: list[str] = []
    ctx_idx: list[int] = []

    # 移除尾部空行（diff 末尾可能有）
    while body_lines and body_lines[-1] == "":
        body_lines.pop()

    for i, bline in enumerate(body_lines):
        if bline.startswith("-"):
            del_lines.append(bline[1:])
        elif bline.startswith("+"):
            add_lines.append(bline[1:])
        else:
            content = bline[1:] if bline.startswith(" ") else bline
            ctx_lines.append(content)
            ctx_idx.append(i)

    return PatchHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        raw_lines=body_lines,
        ctx_lines=ctx_lines,
        del_lines=del_lines,
        add_lines=add_lines,
        _ctx_idx=ctx_idx,
    )


def _reconstruct_block(hunk: PatchHunk) -> list[str]:
    """从 hunk raw_lines 重建新版本代码块（含换行符）"""
    block = []
    for line in hunk.raw_lines:
        if line.startswith(" "):
            block.append(line[1:] + "\n")
        elif line.startswith("+"):
            block.append(line[1:] + "\n")
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            block.append(line + "\n")  # 空行/无前缀
    return block


def _reorder_hunks(hunks: list[PatchHunk]) -> list[PatchHunk]:
    """确保 hunks 按 old_start 升序排列（处理 diff 中的跨文件乱序）"""
    return sorted(hunks, key=lambda h: h.old_start)


def apply_hunks(
    local_lines: list[str],
    hunks: list[PatchHunk],
    protected_ranges: list[tuple[int, int]] | None = None,
) -> tuple[list[str], int, int]:
    """
    将 hunks 应用到本地文件内容，返回 (合并结果, 成功数, 跳过数)。

    Args:
        local_lines: 本地文件所有行 (0-indexed)
        hunks: 解析后的 hunk 列表
        protected_ranges: 保护区 [(start_line_0idx, end_line_0idx), ...]

    Returns:
        (merged_lines, apply_ok, skipped)
    """
    merged, apply_ok, skipped, _ = apply_hunks_detailed(
        local_lines, hunks, protected_ranges
    )
    return merged, apply_ok, skipped


def apply_hunks_detailed(
    local_lines: list[str],
    hunks: list[PatchHunk],
    protected_ranges: list[tuple[int, int]] | None = None,
) -> tuple[list[str], int, int, list[dict]]:
    """
    将 hunks 应用到本地文件内容，返回 (合并结果, 成功数, 跳过数, 跳过明细)。

    跳过明细为 list[dict]，每个跳过项形如：
        {"old_start": int, "reason": str}
    reason 取值:
        "protected"     与保护区重叠
        "no_context"    上下文滑动匹配不到本地内容
        "out_of_range"  替换目标行号超出文件范围
        "no_payload"    新文件模式下块为空

    Args:
        local_lines: 本地文件所有行 (0-indexed)
        hunks: 解析后的 hunk 列表
        protected_ranges: 保护区 [(start_line_0idx, end_line_0idx), ...]

    Returns:
        (merged_lines, apply_ok, skipped, skip_details)
    """
    if protected_ranges is None:
        protected_ranges = []

    result = local_lines[:]
    apply_ok = 0
    skipped = 0
    skip_details: list[dict] = []
    offset = 0  # 累积行号偏移（插入/删除导致）

    for hunk in hunks:
        # 新增文件（@@ -0,0 +1,N @@）：本地为空、旧计数为 0，直接整块写入。
        # 否则 _find_context_match 会因无上下文返回 hint_pos=-1 而被误判为 skip，
        # 导致新增文件永不落盘（Phase20 健康检查"文件缺失"根因）。
        if not local_lines and hunk.old_start == 0 and hunk.old_count == 0:
            block = _reconstruct_block(hunk)
            if block:
                result = block[:]
                offset = len(block)
                apply_ok += 1
            else:
                skipped += 1
                skip_details.append({"old_start": hunk.old_start, "reason": "no_payload"})
            continue

        # 计算在已调整的本地文件中的位置
        adjusted_start = hunk.old_start - 1 + offset  # → 0-indexed

        # 检查保护区重叠
        if _overlaps_protected(
            adjusted_start, adjusted_start + hunk.old_count, protected_ranges
        ):
            skipped += 1
            skip_details.append({"old_start": hunk.old_start, "reason": "protected"})
            continue

        # 滑动匹配上下文
        match_pos = _find_context_match(result, adjusted_start, hunk)
        if match_pos < 0:
            skipped += 1
            skip_details.append({"old_start": hunk.old_start, "reason": "no_context"})
            continue

        # 应用替换
        old_end = match_pos + hunk.old_count
        if old_end > len(result):
            skipped += 1
            skip_details.append({"old_start": hunk.old_start, "reason": "out_of_range"})
            continue

        # 应用替换：用完整重建的新块替换旧块
        block = _reconstruct_block(hunk)
        result = result[:match_pos] + block + result[old_end:]
        delta = len(block) - hunk.old_count
        offset += delta
        apply_ok += 1

    return result, apply_ok, skipped, skip_details


def _find_context_match(
    result: list[str], hint_pos: int, hunk: PatchHunk
) -> int:
    """
    在 result 中滑动查找与 hunk 旧内容匹配的位置。
    返回匹配的起始行号 (0-indexed)，失败返回 -1。

    旧内容 = 上下文行 + 删除行（按原始顺序），这样即使 hunk 中间有 +/− 变更，
    上下文被分割也能正确匹配（真实 GitHub diff 常见）。
    """
    ctx = _old_block(hunk)
    if not ctx:
        return hint_pos  # 无旧内容 = 盲替换

    plen = len(result)
    clen = len(ctx)

    # 搜索窗口: hint_pos ± 50 行，然后全文件
    search_start = max(0, hint_pos - 50)
    search_end = min(plen - clen + 1, hint_pos + hunk.old_count + 50)

    # 第一轮: 窗口内
    pos = _scan_range(result, ctx, search_start, search_end)
    if pos >= 0:
        return pos

    # 第二轮: 全文件
    return _scan_range(result, ctx, 0, plen - clen + 1)


def _old_block(hunk: PatchHunk) -> list[str]:
    """按原始顺序重建 hunk 的旧内容（上下文行 + 删除行）。"""
    block: list[str] = []
    for line in hunk.raw_lines:
        if line.startswith(" "):
            block.append(line[1:])
        elif line.startswith("-"):
            block.append(line[1:])
        # '+' 行是新增，不属于旧内容；'\' 行忽略
    return block


def _scan_range(
    lines: list[str], ctx: list[str], start: int, end: int
) -> int:
    """在 [start, end) 范围内查找 ctx 的精确匹配"""
    for i in range(start, end):
        if _lines_match_at(lines, i, ctx):
            return i
    return -1


def _lines_match_at(lines: list[str], pos: int, ctx: list[str]) -> bool:
    """检查 lines[pos:pos+len(ctx)] 是否逐行等于 ctx"""
    if pos + len(ctx) > len(lines):
        return False
    for j, ctx_line in enumerate(ctx):
        if lines[pos + j].rstrip("\n").rstrip("\r") != ctx_line.rstrip("\n").rstrip("\r"):
            return False
    return True


def _overlaps_protected(
    start: int, end: int, protected: list[tuple[int, int]]
) -> bool:
    """检查行范围 [start, end) 是否与保护区重叠"""
    for p_start, p_end in protected:
        if start < p_end and end > p_start:
            return True
    return False
