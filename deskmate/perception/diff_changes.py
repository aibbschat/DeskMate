"""文件变更 diff — 对比两个文件版本或目录快照。"""

from __future__ import annotations

import difflib
from pathlib import Path


def diff_files(old_path: str, new_path: str, context_lines: int = 3) -> str:
    """对比两个文件的内容差异，返回 unified diff。"""
    old = Path(old_path).expanduser().resolve()
    new = Path(new_path).expanduser().resolve()

    try:
        old_lines = old.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return f"（无法读取：{old_path}）"
    try:
        new_lines = new.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return f"（无法读取：{new_path}）"

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=str(old), tofile=str(new),
        n=context_lines,
    )
    result = "".join(diff)
    if not result:
        return "（两个文件内容相同）"

    # 限制输出长度
    if len(result) > 8000:
        result = result[:8000] + f"\n…（diff 过长，已截断）"
    return result


def diff_text(old_text: str, new_text: str, label: str = "变更", context_lines: int = 3) -> str:
    """对比两段文本的差异。用于内存中的文本对比。"""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{label}（旧）", tofile=f"{label}（新）",
        n=context_lines,
    )
    result = "".join(diff)
    if not result:
        return "（无变更）"

    if len(result) > 8000:
        result = result[:8000] + f"\n…（diff 过长，已截断）"
    return result


def summarize_changes(old_text: str, new_text: str) -> dict[str, int]:
    """统计变更摘要：新增/删除/修改的行数。"""
    old_lines = set(old_text.splitlines())
    new_lines = set(new_text.splitlines())
    added = new_lines - old_lines
    removed = old_lines - new_lines
    return {
        "added": len(added),
        "removed": len(removed),
        "total_old": len(old_lines),
        "total_new": len(new_lines),
    }
