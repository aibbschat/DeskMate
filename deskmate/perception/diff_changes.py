"""文件变更 diff — 对比两个文件版本或目录快照。

截断策略：`max_chars` 默认 8000（喂 LLM prompt / 展示给用户时的安全上限），
传 `None` 则不截断——**持久化入库必须传 None**，否则大章节修订会被腰斩，
事后再也无法完整回放"改了哪几处"。
"""

from __future__ import annotations

import difflib
from collections import Counter
from pathlib import Path

_DEFAULT_MAX_CHARS = 8000


def _clip(result: str, max_chars: int | None) -> str:
    if max_chars is not None and len(result) > max_chars:
        return result[:max_chars] + "\n…（diff 过长，已截断）"
    return result


def diff_files(old_path: str, new_path: str, context_lines: int = 3,
               max_chars: int | None = _DEFAULT_MAX_CHARS) -> str:
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
    return _clip(result, max_chars)


def diff_text(old_text: str, new_text: str, label: str = "变更", context_lines: int = 3,
              max_chars: int | None = _DEFAULT_MAX_CHARS) -> str:
    """对比两段文本的差异。用于内存中的文本对比。

    入库存档请显式传 `max_chars=None` 拿完整 diff。
    """
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
    return _clip(result, max_chars)


def summarize_changes(old_text: str, new_text: str) -> dict[str, int]:
    """统计变更摘要：新增/删除的行数。

    用**多重集**（Counter）而非 set 差集：稿件里大量存在重复行（空行、
    "……"、重复的对白提示），set 差集会把它们压成一条，导致"改了 8 处"
    被报成"改了 1 处"。total_* 也按真实总行数计，不去重。
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    old_c = Counter(old_lines)
    new_c = Counter(new_lines)
    return {
        "added": sum((new_c - old_c).values()),
        "removed": sum((old_c - new_c).values()),
        "total_old": len(old_lines),
        "total_new": len(new_lines),
    }
