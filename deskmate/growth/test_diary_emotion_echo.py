"""P2-0 契约：日记喂给情绪层的必须是「这一则写了什么」，不是计数。

2026-11-17 之前 ``append_entry`` 传的是
``f"写下了今天第 {seq} 则日记（{len(events)} 条素材）"``——情绪引擎只能读到
计数，于是 ``emotion_log`` 里 ``source='experience'`` 的行全部 ``object_ref=''``、
摘要清一色「写日记把今天的事落成字」。日记本是三个情感入口里最富矿的一个
（有具体的人、具体的事、她的反应），却被降维成「我做完了一件事」。

设计依据：``reports/2026-11-16_小桔_情感层设计.md`` §17.3。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from deskmate.growth.diary import DiaryWriter, _entry_plain_text
from deskmate.growth.emotion_engine import EmotionEngine, _clip_experience


def _make_writer(base: Path) -> DiaryWriter:
    (base / "growth.json").write_text(
        json.dumps(
            {
                "version": 2,
                "intimacy": 50.0,
                "level": 5,
                "total_chats": 100,
                "recent_events": [],
            }
        ),
        encoding="utf-8",
    )
    return DiaryWriter(base)


def test_append_entry_feeds_entry_text_not_counter():
    """传的是正文（有具体的事），不是「第 N 则（M 条素材）」。"""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        writer = _make_writer(base)
        day = "2026-11-17"
        captured: dict = {}

        class FakeEngine:
            def __init__(self, base_dir=None):
                pass

            def update_after_experience(self, text, now=None, note=""):
                captured["text"] = text
                captured["note"] = note

        with mock.patch.dict(os.environ, {"DESKMATE_DIARY_NARRATE": "0"}):
            with mock.patch(
                "deskmate.growth.emotion_engine.EmotionEngine", FakeEngine
            ):
                writer.append_entry(
                    day,
                    [{"ts": f"{day}T09:00:00", "kind": "roundtable"}],
                    [],
                    at="09:10",
                )

        assert "圆桌讨论" in captured["text"], captured["text"]
        assert "则日记" not in captured["text"], "又在喂计数"
        assert "条素材" not in captured["text"], "又在喂计数"
        assert captured["note"], "日记通道必须带 note，否则会被判成「她写完了日记」"


def test_entry_plain_text_strips_markdown_scaffold():
    """标题行、eid、徽章落款不是内容，不进 prompt。"""
    body = (
        "## 19:15 <!--xj:2026-11-16-1915-ff4e53-->\n\n"
        "15:06 你让我顺手核对一遍，到晚上七点多你才把话说透。这话有点刺我。\n"
        "- 默契 100.0/100 · 信任 0.0 · 心情 82.0 · Lv.12\n"
    )
    out = _entry_plain_text(body)
    assert "## 19:15" not in out
    assert "xj:" not in out
    assert "默契" not in out
    assert "Lv.12" not in out
    assert "这话有点刺我" in out


def test_entry_plain_text_keeps_head_and_tail():
    """超长时保头保尾：头是发生了什么，尾是她当时的反应。"""
    body = "开头" + "甲" * 500 + "乙" * 400 + "结尾这话有点刺我"
    out = _entry_plain_text(body, limit=600)
    assert out.startswith("开头")
    assert out.endswith("结尾这话有点刺我")
    assert "……" in out
    assert len(out) < 620


def test_clip_experience_keeps_reaction_at_tail():
    """引擎侧同样不能只截头——她的反应几乎总在末尾。"""
    out = _clip_experience("事" * 700 + "最后她说有点刺我")
    assert out.endswith("最后她说有点刺我")
    assert len(out) < 650


def _reply() -> str:
    return json.dumps(
        {
            "dopamine": 52.0,
            "serotonin": 51.0,
            "oxytocin": 45.0,
            "cortisol": 30.0,
            "description": "被那句话刺了一下",
            "reason": "他说我把考据摆在人物前面",
            "salience": 0.6,
            "object_ref": "萧盛",
            "object_kind": "person",
            "event_summary": "他说我把考据摆在人物前面",
        },
        ensure_ascii=False,
    )


def test_note_swaps_seen_qualifier():
    """有 note 时不再说「你自己做的，不是看到的」。

    日记里读到的新闻也算她经历过的事（被触动本身就是经历）。沿用旧口径
    会把「读新闻被扎到」全判成旁观，等于把这条通道又关掉。
    """
    with TemporaryDirectory() as tmp:
        engine = EmotionEngine(Path(tmp))
        seen: list[str] = []

        def fake_chat(system, user, **kwargs):
            seen.append(user)
            return _reply()

        with mock.patch("deskmate.memory.llm.chat_memory", side_effect=fake_chat):
            with mock.patch("deskmate.memory.llm.llm_available", return_value=True):
                engine.update_after_experience(
                    "19:15 他说我像老学究，这话有点刺我",
                    note="这是她刚写下的一则日记",
                )
                engine.update_after_experience("完成了第 3 次反思")

    assert "这是她刚写下的一则日记" in seen[0]
    assert "不是看到的" not in seen[0]
    assert "不是看到的" in seen[1], "另外两个调用方的旧口径不能被改掉"


def test_blank_experience_skips_llm():
    """空正文不烧调用。"""
    with TemporaryDirectory() as tmp:
        engine = EmotionEngine(Path(tmp))
        with mock.patch("deskmate.memory.llm.chat_memory") as chat:
            with mock.patch("deskmate.memory.llm.llm_available", return_value=True):
                engine.update_after_experience("   \n\n  ")
        chat.assert_not_called()
