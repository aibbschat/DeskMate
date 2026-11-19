"""read_paragraphs 工具对口回归（2026-11-19）。

锁住两件事：
1. 段号口径（1-based、空行不占号），一次取全不截断补读；
2. .docx 直接抽取、不产生 shell 转义（从根上消灭 officecli+python 解码的弯路）。
"""
from __future__ import annotations

from pathlib import Path

from deskmate.perception.file_reader import read_paragraphs


def _txt(tmp_path: Path) -> Path:
    p = tmp_path / "稿.txt"
    p.write_text("第一段\n\n第二段\n第三段\n\n第四段\n", encoding="utf-8")
    return p


def test_paragraph_range_1based_empty_lines_skipped(tmp_path):
    out = read_paragraphs(str(_txt(tmp_path)), 2, 4)
    assert "[2] 第二段" in out
    assert "[3] 第三段" in out
    assert "[4] 第四段" in out
    assert "[1]" not in out
    assert "共 4 段" in out


def test_single_paragraph(tmp_path):
    out = read_paragraphs(str(_txt(tmp_path)), 3, 3)
    assert "[3] 第三段" in out
    assert "[2]" not in out
    assert "[4]" not in out


def test_out_of_range_is_graceful(tmp_path):
    out = read_paragraphs(str(_txt(tmp_path)), 2, 99)
    assert "段 2-4 / 共 4 段" in out
    assert "[4] 第四段" in out


def test_invalid_range(tmp_path):
    assert "无效" in read_paragraphs(str(_txt(tmp_path)), 5, 3)
    assert "整数" in read_paragraphs(str(_txt(tmp_path)), "x", 3)  # type: ignore[arg-type]


def test_docx_no_shell_escaping(tmp_path):
    """docx 直接抽取，正文原样出现，不含终端/JSON 转义序列。"""
    try:
        from docx import Document
    except ImportError:
        return  # python-docx 是可选依赖，未装则跳过
    p = tmp_path / "稿.docx"
    doc = Document()
    doc.add_paragraph("第一章原文")
    doc.add_paragraph("中文标点：，。！")
    doc.save(str(p))
    out = read_paragraphs(str(p), 1, 2)
    assert "第一章原文" in out
    assert "中文标点：，。！" in out
    assert "\\u" not in out  # 无 JSON 转义
