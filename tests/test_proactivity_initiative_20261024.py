"""小桔主动性改造（2026-10-24）单测：阶段一至四的纯逻辑部分。

不调用 LLM、不联网、不碰真实 ~/.deskmate：所有库与文件都在 tmp_path 里。
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from deskmate.proactivity.deferred import defer_judgment
from deskmate.proactivity.health_producers import _writing_activity_producer
from deskmate.proactivity.judge import JudgmentResult
from deskmate.proactivity.ledger import close_loop_pending_text, pending_items
from deskmate.proactivity.preferences import PreferenceStore
from deskmate.proactivity.suggestion_types import (
    normalize_suggestion_type,
    thresholds_for,
)
from deskmate.proactivity.writing_context import (
    build_writing_status,
    resolve_writing_workspace,
)


def test_writing_status_and_resolve(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_WRITING_WORKSPACE", raising=False)
    root = tmp_path / "books"
    book_a = root / "01.国子监秘闻录"
    book_b = root / "02.明朝的裂变"
    book_a.mkdir(parents=True)
    book_b.mkdir(parents=True)
    now = time.time()
    f_a = book_a / "第一章.md"
    f_a.write_text("第一章", encoding="utf-8")
    os.utime(f_a, (now, now))
    f_b = book_b / "正文.md"
    f_b.write_text("正文", encoding="utf-8")
    os.utime(f_b, (now - 5 * 86400, now - 5 * 86400))
    lock = book_a / "~$第一章.md"
    lock.write_text("lock", encoding="utf-8")
    assert resolve_writing_workspace(explicit=root, base_dir=tmp_path) == root
    status = build_writing_status(root)
    assert "国子监秘闻录" in status
    assert "明朝的裂变" in status
    assert "5 天没动笔" in status
    assert build_writing_status(tmp_path / "不存在") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("writing_review", "writing_review"),
        ("writing_block_help", "writing_review"),
        ("review_writing_break", "writing_review"),
        ("user_requested_writing_review", "writing_review"),
        ("writing_breakpoint_check", "writing_review"),
        ("character_consistency_check", "character_inconsistency"),
        ("plot_repetition", "plot_contradiction"),
        ("stall_flag", "writing_stall"),
        ("emotional_care", "emotional_care"),
        ("some_new_invention", "generic"),
        ("", "generic"),
        (None, "generic"),
    ],
)
def test_normalize_suggestion_type(raw, expected):
    assert normalize_suggestion_type(raw) == expected


def test_writing_thresholds_are_lower():
    assert thresholds_for("writing_review") == (0.65, 0.45)
    assert thresholds_for("plot_contradiction") == (0.65, 0.45)
    assert thresholds_for("generic") == (0.8, 0.5)


def test_normalized_type_reaches_preference_threshold(tmp_path):
    store = PreferenceStore(base_dir=tmp_path)
    store.record_accepted("writing_review", "测试1")
    assert store.active_preferences() == []
    store.record_accepted("writing_review", "测试2")
    types = [p["type"] for p in store.active_preferences()]
    assert "writing_review" in types


def _judgment(**kwargs):
    base = dict(
        should_act=True,
        confidence=0.6,
        suggestion_type="writing_review",
        suggestion_content="测试建议内容",
        reasoning="测试依据",
        finding_key="ch1",
    )
    base.update(kwargs)
    return JudgmentResult(**base)


def test_defer_prepare_judgment_and_dedup(tmp_path):
    rid = defer_judgment(tmp_path, _judgment(), "中置信已暂存", original_action="prepare")
    assert rid
    items = pending_items(base_dir=tmp_path)
    assert any(it["id"] == rid for it in items)
    rid2 = defer_judgment(tmp_path, _judgment(), "中置信已暂存", original_action="prepare")
    assert rid2 == rid
    assert len(pending_items(base_dir=tmp_path)) == 1


def test_defer_skips_emotional_and_empty(tmp_path):
    assert defer_judgment(tmp_path, _judgment(suggestion_type="emotional_care"), "x") == ""
    assert defer_judgment(tmp_path, _judgment(suggestion_content="  "), "x") == ""
    assert pending_items(base_dir=tmp_path) == []


def _clear_activity_throttle(tmp_path):
    db = tmp_path / "deskmate.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM proactivity_meta WHERE key='producer:writing_activity'")
        conn.commit()
    finally:
        conn.close()


def test_writing_activity_baseline_then_change(tmp_path, monkeypatch):
    root = tmp_path / "books"
    book = root / "01.国子监秘闻录"
    book.mkdir(parents=True)
    now = time.time()
    f = book / "第一章.md"
    f.write_text("旧", encoding="utf-8")
    os.utime(f, (now - 7200, now - 7200))
    monkeypatch.setenv("SCRIBE_ROOTS", str(root))
    assert _writing_activity_producer(base_dir=tmp_path) == []
    _clear_activity_throttle(tmp_path)
    f.write_text("旧 + 新段落", encoding="utf-8")
    os.utime(f, (now, now))
    items = _writing_activity_producer(base_dir=tmp_path)
    assert len(items) == 1
    assert items[0]["kind"] == "chapter_updated"
    assert "国子监秘闻录" in items[0]["content"]


def test_close_loop_pending_text_filters_old(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir(parents=True)
    now = time.time()
    lines = [
        json.dumps({"status": "proposed", "ts": now, "action_summary": "最近的旧提案"}, ensure_ascii=False),
        json.dumps({"status": "proposed", "ts": now - 40 * 86400, "action_summary": "四十天前的旧提案"}, ensure_ascii=False),
    ]
    (mem / "close_loop_pending.jsonl").write_text("\n".join(lines), encoding="utf-8")
    text = close_loop_pending_text(base_dir=tmp_path)
    assert "最近的旧提案" in text
    assert "四十天前的旧提案" not in text
