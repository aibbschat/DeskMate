"""office_safe_deliver 工具测试：源保护 / 原子替换 / 恢复副本。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = "deskmate.skills.tools.office_safe_deliver"


def _run(args: list[str], cwd=REPO_ROOT) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", TOOL, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        payload = {}
    return proc.returncode, payload


@pytest.fixture
def work(tmp_path: Path):
    d = tmp_path / "docs"
    d.mkdir()
    src = d / "report.docx"
    src.write_text("original content", encoding="utf-8")
    return d, src


def test_protect_creates_bak(work):
    d, src = work
    rc, out = _run(["protect", str(src)])
    assert rc == 0
    assert out["status"] == "protected"
    bak = src.with_suffix(src.suffix + ".bak")
    assert bak.exists()
    assert bak.read_text() == "original content"


def test_protect_rolls_existing_bak(work):
    d, src = work
    bak = src.with_suffix(src.suffix + ".bak")
    bak.write_text("older", encoding="utf-8")
    rc, out = _run(["protect", str(src)])
    assert rc == 0
    assert src.with_suffix(src.suffix + ".bak.1").exists()
    assert bak.read_text() == "original content"


def test_protect_noop_when_missing(tmp_path: Path):
    rc, out = _run(["protect", str(tmp_path / "ghost.docx")])
    assert rc == 0
    assert out["status"] == "noop"


def test_deliver_with_candidate_atomically_replaces(work, tmp_path: Path):
    d, src = work
    cand = tmp_path / "candidate.docx"
    cand.write_text("new content", encoding="utf-8")
    rc, out = _run(["deliver", "--source", str(src), "--candidate", str(cand)])
    assert rc == 0
    assert out["status"] == "delivered"
    assert src.read_text() == "new content"
    # 源保护副本保留原内容
    assert src.with_suffix(src.suffix + ".bak").read_text() == "original content"


def test_deliver_rejects_empty_candidate(work, tmp_path: Path):
    d, src = work
    cand = tmp_path / "empty.docx"
    cand.write_text("", encoding="utf-8")
    rc, _ = _run(["deliver", "--source", str(src), "--candidate", str(cand)])
    assert rc != 0
    assert src.read_text() == "original content"  # 未被覆盖


def test_deliver_without_candidate_just_protects(work):
    d, src = work
    rc, out = _run(["deliver", "--source", str(src)])
    assert rc == 0
    assert src.with_suffix(src.suffix + ".bak").exists()


def test_restore_from_bak(work):
    d, src = work
    _run(["protect", str(src)])
    src.write_text("edited content", encoding="utf-8")
    rc, out = _run(["restore", str(src)])
    assert rc == 0
    assert out["status"] == "restored"
    assert src.read_text() == "original content"
    # 当前版另存为 .broken 防丢
    assert src.with_suffix(src.suffix + ".broken").exists()
