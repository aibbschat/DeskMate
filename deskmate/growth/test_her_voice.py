"""her_voice 取的是「她写过的一句话」，不是整段、也不是渲染出来的清单。

2026-11-17 修：``_diary_section_bodies`` 原来返回整则正文，于是
``diary._opening(seq>1)`` 把上一则全文当开场白——第二则开头就是第一则整段、
落款还重复一遍（``test_append_two_entries`` 的 ``累计对话`` 数成 2）；
``pick_her_line`` 那边拿半篇日记做 2-gram 匹配，也几乎什么都能命中。

两条长期被当成「既有红」的用例（``test_append_two_entries`` /
``test_second_entry_does_not_repeat_first``）根子都在这里。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.growth.her_voice import recent_her_lines

_GROWTH = {
    "version": 2,
    "intimacy": 50.0,
    "level": 5,
    "total_chats": 100,
    "recent_events": [],
}


def _write_diary(base: Path, day: str, text: str) -> None:
    d = base / "diary"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text(text, encoding="utf-8")


def test_returns_narration_sentences_only():
    """正文里的叙述句留下；清单项、徽章、落款、标题都不要。"""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_diary(
            base,
            "2026-11-17",
            "# 2026-11-17 周二 · 小桔的日记\n\n"
            "## 19:15 <!--xj:2026-11-17-1915-aaaaaa-->\n"
            "15:06 你让我顺手核对一遍。到晚上七点多你才把话说透，这话有点刺我。\n"
            "- 我们一起整理了文稿。\n"
            "- 默契 100.0/100 · 信任 0.0 · 心情 82.0 · Lv.12\n\n"
            "> 累计对话 100 轮。这份日记由今日真实信号生成，没有的我不编。\n",
        )
        joined = "\n".join(recent_her_lines(base_dir=base, limit=10))
        assert "你让我顺手核对一遍。" in joined
        assert "这话有点刺我。" in joined
        assert "整理" not in joined, "渲染出来的清单项不是她的话"
        assert "默契" not in joined
        assert "累计对话" not in joined
        assert "小桔的日记" not in joined


def test_skips_renderer_template_lines():
    """开场句是系统写的固定句，不能当「她的原句」再取回来。"""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_diary(
            base,
            "2026-11-17",
            "# 2026-11-17 周二 · 小桔的日记\n\n"
            "## 09:10 <!--xj:2026-11-17-0910-bbbbbb-->\n"
            "今天我们一起做了些事，我都记在心里。\n"
            "- 我们一起整理了文稿。\n",
        )
        assert recent_her_lines(base_dir=base) == []


def test_recent_monologue_can_be_skipped():
    """``skip_recent_hours`` 能挡住「刚刚这一则产生的独白」。

    补记的开场白要引「她过去说过的话」；同一则刚产生的独白属于当下，
    再引一次就是复述。``test_second_entry_does_not_repeat_first`` 抓的就是它。
    """
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "growth.json").write_text(
            json.dumps(_GROWTH), encoding="utf-8"
        )
        from deskmate.growth.emotion_engine import EmotionEngine

        eng = EmotionEngine(base)
        # ts 必须用和生产同一种格式（isoformat 带 T、本地时区）。
        # 用 SQLite 的 datetime('now') 会写成 UTC + 空格分隔，
        # 与 ``ts >= ?`` 的字符串比较口径不一致。
        now = datetime.now()
        rows = [
            ("fresh", now.isoformat(timespec="seconds"), "刚刚才说过的话"),
            (
                "old",
                (now - timedelta(hours=3)).isoformat(timespec="seconds"),
                "三个小时前说过的话",
            ),
        ]
        conn = sqlite3.connect(base / "deskmate.db")
        try:
            for rid, ts, desc in rows:
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description)"
                    " VALUES (?, ?, 'experience', '{}', ?)",
                    (rid, ts, desc),
                )
            conn.commit()
        finally:
            conn.close()

        got = eng.recent_log_descriptions(since_hours=24, limit=5)
        assert "刚刚才说过的话" in got
        got = eng.recent_log_descriptions(
            since_hours=24, limit=5, skip_recent_hours=1
        )
        assert "刚刚才说过的话" not in got
        assert "三个小时前说过的话" in got
