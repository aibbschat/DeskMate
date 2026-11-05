"""进度跟踪逐字保真门禁的回归测试。

锁的是 2026-11-05 事故：模型用 write_file 复述新增正文，把
「第十一章：烧粮仓」抄成「第三章」、「连栋」抄成「连栎」、「地狱」抄成
「地狑」、「贩夫走卒」抄成「赀夫走卒」。任何一道门禁松了，这类错就会
再次混进文风桶 / 圣典 / 简报。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "progress_feed.py"

# 事故当天的真实字样（来自 officecli 原始输出，正确版本）
RAW = (
    "[/body/p[@paraId=13BE769E]] \n"
    "[/body/p[@paraId=55764EF1]] 第十一章：烧粮仓\n"
    "[/body/p[@paraId=44509413]] 三月初三晚，天上又飘起了小雪。\n"
    "[/body/p[@paraId=5C99BB07]] 仓廒连栋成片，每栋有几十间仓，"
    "仓与仓、栋与栋之间皆有过道，四通八达。\n"
    "[/body/p[@paraId=0D26D182]] 凡是仗义的都是没读过书的贩夫走卒。\n"
    "[/body/p[@paraId=AAAA1111]] 那夯货虽然一副不太聪明的样子。\n"
    "[/body/p[@paraId=BBBB2222]] 一脚走进了他们认为的地狱。\n"
)
PLAIN = (
    "\n第十一章：烧粮仓\n三月初三晚，天上又飘起了小雪。\n"
    "仓廒连栋成片，每栋有几十间仓，仓与仓、栋与栋之间皆有过道，四通八达。\n"
    "凡是仗义的都是没读过书的贩夫走卒。\n"
    "那夯货虽然一副不太聪明的样子。\n"
    "一脚走进了他们认为的地狱。\n"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


def test_extract_is_byte_identical(tmp_path: Path) -> None:
    """落盘内容必须逐字等于剥标记结果，一个字都不许改写。"""
    src = tmp_path / "raw.txt"
    out = tmp_path / "新增正文.txt"
    src.write_text(RAW, encoding="utf-8")
    r = _run("extract", "--src", str(src), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "OK extract:" in r.stdout and "byte-identical" in r.stdout
    assert out.read_text(encoding="utf-8") == PLAIN


def test_strip_markers_keeps_rare_glyphs(tmp_path: Path) -> None:
    """生僻字 / 章名 / 成语不得被替换成另一个合法汉字。"""
    sys.path.insert(0, str(SCRIPT.parent.parent))
    from scripts.progress_feed import strip_para_markers  # noqa: PLC0415

    text, unmatched = strip_para_markers(RAW)
    assert text == PLAIN
    assert unmatched == []
    for w in ("第十一章：烧粮仓", "连栋", "每栋", "过道", "贩夫走卒",
              "那夯货", "地狱"):
        assert w in text
    for bad in ("第三章：烧粮仓", "连栎", "每甑", "甬道", "赀夫走卒",
                "屴货", "地狑"):
        assert bad not in text


def test_unmatched_line_is_rejected(tmp_path: Path) -> None:
    """工具输出格式变了必须报错，不能静默丢段落。"""
    src = tmp_path / "raw.txt"
    src.write_text(RAW + "这行没有 paraId 标记\n", encoding="utf-8")
    out = tmp_path / "新增正文.txt"
    r = _run("extract", "--src", str(src), "--out", str(out))
    assert r.returncode != 0
    assert "FAIL extract" in r.stderr
    assert not out.exists(), "校验失败时不该留下半截文件"


def test_verify_file_catches_single_glyph_drift(tmp_path: Path) -> None:
    """反向验证：把「连栋」改成「连栎」必须 FAIL（事故原样）。"""
    src = tmp_path / "raw.txt"
    good = tmp_path / "good.txt"
    bad = tmp_path / "bad.txt"
    src.write_text(RAW, encoding="utf-8")
    good.write_text(PLAIN, encoding="utf-8")
    assert _run("verify-file", "--src", str(src), "--file", str(good)).returncode == 0
    bad.write_text(PLAIN.replace("连栋", "连栎"), encoding="utf-8")
    r = _run("verify-file", "--src", str(src), "--file", str(bad))
    assert r.returncode != 0
    assert "FAIL verify-file" in r.stderr
    assert "首个差异" in r.stderr


def test_verify_quotes_catches_fake_chapter_title(tmp_path: Path) -> None:
    """反向验证：假章名「第三章：烧粮仓」必须被门禁挡下。"""
    src = tmp_path / "raw.txt"
    src.write_text(RAW, encoding="utf-8")
    ok = _run("verify-quotes", "--src", str(src),
              "--quote", "第十一章：烧粮仓", "--quote", "仓廒连栋成片")
    assert ok.returncode == 0 and "OK quotes: 2/2" in ok.stdout
    bad = _run("verify-quotes", "--src", str(src),
               "--quote", "第三章：烧粮仓", "--quote", "仓廒连栎成片")
    assert bad.returncode != 0
    assert "FAIL quotes: 2/2" in bad.stderr
    assert "第三章：烧粮仓" in bad.stderr


def test_verify_quotes_rejects_empty_gate(tmp_path: Path) -> None:
    """没传引文不许伪装成通过。"""
    src = tmp_path / "raw.txt"
    src.write_text(RAW, encoding="utf-8")
    r = _run("verify-quotes", "--src", str(src))
    assert r.returncode != 0 and "门禁形同虚设" in r.stderr


def test_quotes_file_and_line_range(tmp_path: Path) -> None:
    """从全稿导出按行号截取 + 引文文件批量校验。"""
    src = tmp_path / "full.txt"
    qf = tmp_path / "quotes.txt"
    src.write_text("[/body/p[@paraId=0]] 别章旧文\n" + RAW, encoding="utf-8")
    qf.write_text("# 章名与关键器物\n第十一章：烧粮仓\n仓廒连栋成片\n",
                  encoding="utf-8")
    out = tmp_path / "slice.txt"
    r = _run("extract", "--src", str(src), "--start-line", "2",
             "--end-line", "8", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == PLAIN
    r2 = _run("verify-quotes", "--src", str(src), "--start-line", "2",
              "--end-line", "8", "--quotes-file", str(qf))
    assert r2.returncode == 0 and "OK quotes: 2/2" in r2.stdout


@pytest.mark.parametrize("bad_range", [("0", "5"), ("9", "99"), ("5", "3")])
def test_invalid_line_range_fails(tmp_path: Path, bad_range: tuple[str, str]) -> None:
    src = tmp_path / "full.txt"
    src.write_text(RAW, encoding="utf-8")
    r = _run("extract", "--src", str(src), "--start-line", bad_range[0],
             "--end-line", bad_range[1], "--out", str(tmp_path / "o.txt"))
    assert r.returncode != 0
