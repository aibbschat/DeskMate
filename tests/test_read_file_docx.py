"""read_file must hand the agent prose, not ZIP bytes.

A .docx is a ZIP archive; read as text it starts with `PK\\x03\\x04`. That is exactly
what jigang hit on 2026-10-31 (《夜不收大纲.docx》) and it concluded "this environment
has no shell, I can't read the body" — a whole worker stalled on a file it was one
import away from reading. See PITFALLS §P8.
"""

from __future__ import annotations

import pytest

from coworker.tools.files import file_tools

docx = pytest.importorskip("docx", reason="python-docx required to build fixtures")


def _reader(tmp_path):
    return file_tools(str(tmp_path))[0]


def _make_docx(tmp_path, name="大纲.docx", paragraphs=("第一章 渡汉江", "龙山仓在城外西南十里")):
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    path = tmp_path / name
    doc.save(str(path))
    return path


def test_docx_returns_prose_not_zip_bytes(tmp_path):
    _make_docx(tmp_path)
    out = _reader(tmp_path)("大纲.docx")
    assert "PK" not in out["content"]
    assert "第一章 渡汉江" in out["content"]
    assert "龙山仓在城外西南十里" in out["content"]
    assert out["extracted"] is True


def test_docx_keeps_the_numbered_lines_contract(tmp_path):
    _make_docx(tmp_path, paragraphs=("甲", "乙", "丙"))
    out = _reader(tmp_path)("大纲.docx")
    assert out["total_lines"] == 3
    assert out["start_line"] == 1 and out["end_line"] == 3
    # path:line citations must still be possible — lines are numbered
    assert "     1\t甲" in out["content"]
    assert "     3\t丙" in out["content"]


def test_docx_windows_and_tells_the_agent_to_continue(tmp_path):
    _make_docx(tmp_path, paragraphs=tuple(f"第{i}段" for i in range(1, 8)))
    reader = _reader(tmp_path)
    first = reader("大纲.docx", max_lines=3)
    assert first["end_line"] == 3
    assert "start_line=4" in first.get("note", "")
    second = reader("大纲.docx", start_line=4, max_lines=3)
    assert second["start_line"] == 4
    assert "第4段" in second["content"]


def test_plain_text_is_untouched(tmp_path):
    (tmp_path / "a.txt").write_text("hi there\n", encoding="utf-8")
    out = _reader(tmp_path)("a.txt")
    assert out["content"].endswith("hi there")
    assert "extracted" not in out  # no new key on the ordinary path


def test_docx_outside_the_workspace_is_still_blocked(tmp_path):
    _make_docx(tmp_path)
    out = _reader(tmp_path)("../大纲.docx")
    assert "escapes" in out.get("error", "")


def test_extraction_failure_is_surfaced_not_silently_binary(tmp_path, monkeypatch):
    """python-docx missing => an explicit message, never the ZIP bytes."""
    import coworker.tools.files as files_mod

    monkeypatch.setattr(
        files_mod,
        "_docx_lines",
        lambda _p: "（python-docx 未安装，无法读取 .docx 文件）",
    )
    _make_docx(tmp_path)
    out = _reader(tmp_path)("大纲.docx")
    assert "python-docx" in out["error"]
    assert "content" not in out
