"""read_chapters 工具契约（2026-11-20，降低跨章比对往返）。

锁住四件事：
1. 一次读多章、正文一字不砍（只加组织，不砍内容）；
2. 【覆盖率】标注讲清"本次加载哪几章 / 其余未加载 / 未加载≠没写"；
3. 单章超限、总量超限都**明确告知、绝不静默截断**；
4. 原 read_chapter / list_chapters 口径不变（1v1 地基红线）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.perception.file_reader import (
    list_chapters as _list_chapters,
    read_chapter as _read_chapter,
    read_chapters,
)


def _novel(tmp_path: Path, sizes: dict[int, int] | None = None) -> Path:
    """造一本 5 章的小说，每章正文是确定性可核对的。"""
    sizes = sizes or {}
    chapters = [
        ("第一章 铜雀", "铜雀台正文。" * (sizes.get(1, 3))),
        ("第二章 渡口村", "渡口村正文。" * (sizes.get(2, 3))),
        ("第三章 翻山", "翻山正文。" * (sizes.get(3, 3))),
        ("第四章 高村", "高村正文。" * (sizes.get(4, 3))),
        ("第五章 夜不收来路", "夜不收来路正文。" * (sizes.get(5, 3))),
    ]
    text = "\n\n".join(f"{title}\n\n{body}" for title, body in chapters)
    p = tmp_path / "归营.txt"
    p.write_text(text, encoding="utf-8")
    return p


# --- 1. 一次读多章、正文一字不砍 -------------------------------------------
def test_reads_multiple_chapters_verbatim(tmp_path):
    p = _novel(tmp_path)
    out = read_chapters(str(p), [1, 3, 4])
    # 三章正文原样出现
    assert "铜雀台正文。" * 3 in out
    assert "翻山正文。" * 3 in out
    assert "高村正文。" * 3 in out
    # 未点名的两章正文不出现
    assert "渡口村正文。" not in out
    assert "夜不收来路正文。" not in out


def test_accepts_title_and_fragment(tmp_path):
    p = _novel(tmp_path)
    out = read_chapters(str(p), ["第三章", "夜不收"])
    assert "翻山正文。" in out
    assert "夜不收来路正文。" in out


# --- 2. 覆盖率标注 ----------------------------------------------------------
def test_coverage_note_lists_loaded_and_unloaded(tmp_path):
    p = _novel(tmp_path)
    out = read_chapters(str(p), [1, 4])
    assert "【覆盖率】" in out
    assert out.count("【覆盖率】") == 1  # 顶部一份权威标注，章内不重复
    assert "本稿共 5 章" in out
    assert "本次加载 2 章（第 1、4 章" in out
    assert "其余 3 章本次未加载" in out
    assert "未加载不等于书里没写" in out


def test_coverage_false_for_internal_consumers(tmp_path):
    p = _novel(tmp_path)
    out = read_chapters(str(p), [1, 3], coverage=False)
    assert "【覆盖率】" not in out
    assert "铜雀台正文。" in out


# --- 3. 超限绝不静默截断 ----------------------------------------------------
def test_single_chapter_over_limit_is_flagged_not_silent(tmp_path):
    p = _novel(tmp_path, sizes={1: 2000})  # 第一章很长
    out = read_chapters(str(p), [1], max_chars_per=120)
    assert "后段未加载" in out or "未加载" in out
    assert "勿据此声称读完本章" in out


def test_total_limit_splits_into_batches(tmp_path):
    p = _novel(tmp_path, sizes={i: 60 for i in range(1, 6)})
    out = read_chapters(str(p), [1, 2, 3, 4, 5], max_total=200)
    assert "分批" in out or "分两批" in out
    # 已加载的章正文出现，未加载的章正文不出现
    assert "铜雀台正文。" in out
    assert "夜不收来路正文。" not in out
    # 明确告知下一批怎么读
    assert "read_chapters" in out


def test_mcp_tool_accepts_integer_chapter_numbers(tmp_path):
    """@mcp.tool 签名不能是 list[str]：pydantic 会拒绝 docstring 教的 [1,3]。"""
    from deskmate.perception.server import read_chapters as mcp_read_chapters

    p = _novel(tmp_path)
    out = mcp_read_chapters(str(p), [1, 3])  # type: ignore[arg-type]
    assert "【覆盖率】" in out
    assert "铜雀台正文。" in out
    assert "翻山正文。" in out


def test_missing_chapter_lists_available(tmp_path):
    p = _novel(tmp_path)
    out = read_chapters(str(p), ["不存在的章"])
    assert "未找到" in out
    assert "第一章" in out


# --- 4. 原工具口径不变 ------------------------------------------------------
def test_original_read_chapter_still_works(tmp_path):
    p = _novel(tmp_path)
    one = _read_chapter(str(p), 1)
    assert "【覆盖率】" in one
    assert "铜雀台正文。" * 3 in one
    toc = _list_chapters(str(p))
    assert "共 5 节" in toc
