"""小桔自己的声音（deskmate/growth/her_voice.py）单测。

覆盖：
- 从日记正文 / 情绪独白两类来源收集她的原句
- 相关性过滤（2-gram）：搭调的才捞，不搭调的返回 None
全部用临时库，不碰活库。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from deskmate.growth.her_voice import pick_her_line, recent_her_lines


def _write_diary(base: Path, text: str) -> None:
    d = base / "diary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-13.md").write_text(text, encoding="utf-8")


def _write_monologue(base: Path, desc: str) -> None:
    from deskmate.growth.emotion_engine import EmotionEngine

    EmotionEngine(base_dir=base).get_state()  # 自举建表
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        c.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description, conversation_id) "
            "VALUES (?, ?, 'chat', '{}', ?, NULL)",
            ("m1", (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"), desc),
        )


def test_recent_her_lines_from_diary(tmp_path):
    _write_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    lines = recent_her_lines(base_dir=tmp_path, limit=8)
    assert "今天写完一章，心里亮亮的" in lines


def test_recent_her_lines_from_monologue(tmp_path):
    # 独白尾随（原因）→ 须被剥掉，只留第一人称原句
    _write_monologue(tmp_path, "今天写完一章，心里亮亮的（终于收尾了）")
    lines = recent_her_lines(base_dir=tmp_path, limit=8)
    assert "今天写完一章，心里亮亮的" in lines
    assert "终于收尾了" not in "".join(lines)


def test_recent_her_lines_empty_when_no_history(tmp_path):
    assert recent_her_lines(base_dir=tmp_path, limit=8) == []


def test_pick_her_line_relevant(tmp_path):
    _write_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    lines = recent_her_lines(base_dir=tmp_path, limit=8)
    # 用户写「写完一章」→ 共享多个 2-gram → 命中
    got = pick_her_line("今天终于写完了一章，好满足", lines, min_shared=2)
    assert got == "今天写完一章，心里亮亮的"


def test_pick_her_line_no_match_returns_none(tmp_path):
    _write_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
    )
    lines = recent_her_lines(base_dir=tmp_path, limit=8)
    # 「去爬山」与「写完一章」零重叠 → 不硬凑
    assert pick_her_line("今天去爬山了，好累", lines, min_shared=2) is None


def test_pick_her_line_generic_word_not_enough(tmp_path):
    _write_diary(
        tmp_path,
        "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n我有点想你\n",
    )
    lines = recent_her_lines(base_dir=tmp_path, limit=8)
    # 仅共享「今天」这种泛词不足以命中（min_shared 拦住）
    assert pick_her_line("今天天气不错", lines, min_shared=2) is None
