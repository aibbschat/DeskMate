"""diary._opening（活感 v2 §三.3：有她的历史时优先近作开场）。

覆盖：有历史 → 用她的近作；冷启动（无历史）→ 固定 4 句按时段。
"""
from __future__ import annotations

from pathlib import Path

from deskmate.growth.diary import DiaryWriter


def _writer_with_diary(tmp_path: Path, text: str) -> DiaryWriter:
    d = tmp_path / "diary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-13.md").write_text(text, encoding="utf-8")
    return DiaryWriter(base_dir=tmp_path)


def test_opening_uses_her_line_when_history(tmp_path):
    w = _writer_with_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    # seq>1 且有历史 → 返回她的近作，而非固定时段句
    got = w._opening(seq=2, at="10:00")
    assert got == "今天写完一章，心里亮亮的"


def test_opening_fixed_when_no_history(tmp_path):
    w = DiaryWriter(base_dir=tmp_path)  # 无 diary 目录
    got = w._opening(seq=2, at="10:00")
    assert "上午" in got  # 固定时段句（冷启动兜底）
