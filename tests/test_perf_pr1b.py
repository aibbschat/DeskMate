"""PR1b 护栏：闲聊轮前置跳过 + 情绪 record 副作用后移。

不变量：
- 前置跳过只省「被 render 丢弃块」的生产成本，渲染输出逐字节不变；
- 组装阶段对 emotion_read_log 只读，记账在 turn 结束写回；
- 两个回滚开关（DESKMATE_CONTEXT_PREFILTER / DESKMATE_EMOTION_READ_DEFER）
  关掉后回到旧行为。
"""
from __future__ import annotations

import sqlite3

import pytest

from deskmate.memory import context_budget as cb
from deskmate.memory.manager import DeskMateMemoryManager


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    # 关掉一切随时间/外部状态变化的块，保证两次组装可逐字节比较。
    monkeypatch.setenv("DESKMATE_WEATHER", "0")
    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "0")
    monkeypatch.setenv("DESKMATE_CLOCK", "0")
    monkeypatch.setenv("DESKMATE_CONTEXT_PARALLEL", "0")
    monkeypatch.setenv("DESKMATE_CONTEXT_BUDGET", "1")
    monkeypatch.setenv("DESKMATE_CONTEXT_TOKEN_BUDGET", "100000")
    (tmp_path / "USER.md").write_text("画像" * 500, encoding="utf-8")
    monkeypatch.setattr(
        DeskMateMemoryManager,
        "prefetch",
        lambda self, q, **_kw: "<memory-context>" + "记忆内容" * 500 + "</memory-context>",
    )
    return DeskMateMemoryManager(base_dir=tmp_path)


def test_light_turn_output_byte_identical_with_prefilter(mgr, monkeypatch):
    """前置跳过开/关，闲聊轮渲染输出必须逐字节一致（只省生产成本）。"""
    monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "0")
    off = mgr.build_full_context_for_turn("早")
    monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "1")
    on = mgr.build_full_context_for_turn("早")
    assert on == off, "前置跳过改变了闲聊轮注入文本"
    assert "记忆内容" not in on


def test_prefilter_skips_prefetch_production_on_light_turn(mgr, monkeypatch):
    """闲聊轮 + 前置跳过：prefetch 根本不被调用（旧行为是先调用再被 render 丢）。"""
    calls = {"n": 0}

    def _spy(self, q, **_kw):
        calls["n"] += 1
        return "<memory-context>记忆内容</memory-context>"

    monkeypatch.setattr(DeskMateMemoryManager, "prefetch", _spy)
    monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "1")
    mgr.build_full_context_for_turn("早")
    assert calls["n"] == 0, "闲聊轮不该生产 prefetch"

    monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "0")
    mgr.build_full_context_for_turn("早")
    assert calls["n"] == 1, "回滚后闲聊轮仍生产 prefetch（再被 render 丢）"


def test_work_turn_still_produces_heavy_blocks(mgr, monkeypatch):
    """工作轮不受前置跳过影响：prefetch 照常生产并注入。"""
    monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "1")
    ctx = mgr.build_full_context_for_turn("帮我改第三章")
    assert "记忆内容" in ctx


def test_emotion_read_deferred_to_turn_end(tmp_path, monkeypatch):
    """组装阶段 record='defer' 不写库；record_turn_read 才写回。"""
    from deskmate.growth.emotion_engine import EmotionEngine

    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "1")
    ee = EmotionEngine(base_dir=tmp_path)
    ctx = ee.describe_turn_context(record="defer")
    assert ctx
    db = ee._db
    with sqlite3.connect(db) as conn:
        n0 = conn.execute("SELECT COUNT(*) FROM emotion_read_log").fetchone()[0]
    assert n0 == 0, "组装阶段不该写 emotion_read_log"
    assert ee.pending_read_record and ee.pending_read_record["context"] == ctx

    ee.record_turn_read(ee.pending_read_record)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT context FROM emotion_read_log ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert row and row[0] == ctx, "turn 结束写回的必须是实际注入原文"


def test_emotion_record_true_still_immediate(tmp_path, monkeypatch):
    """引擎层 record=True 契约不变：立即写库（既有测试依赖）。"""
    from deskmate.growth.emotion_engine import EmotionEngine

    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "1")
    ee = EmotionEngine(base_dir=tmp_path)
    ctx = ee.describe_turn_context(record=True)
    assert ctx
    with sqlite3.connect(ee._db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM emotion_read_log").fetchone()[0]
    assert n == 1
    assert ee.pending_read_record is None

def test_long_light_turn_output_byte_identical(mgr, monkeypatch):
    """≥6 字的「轻闲聊」（is_light_turn=True 但 is_chitchat=False）也必须逐字节一致。

    回归：PR1b 曾用 _skip_heavy(=is_light_turn) 跳过 file_recall 检索，而 _hits
    同时决定 memory_backstop（keep_light=True，真注入）；"辛苦了辛苦了" 这类在
    HEAD 会检索、命中则不注入 backstop，跳过检索会反常注入 → 输出不一致。
    """
    import deskmate.memory.file_search as fs

    # 关键：让文件层真的跑起来（index_healthy=True），否则 file_recall/backstop
    # 整块跳过、测试没有区分度。检索命中后 _hits 非空 → HEAD 不注入 backstop。
    monkeypatch.setattr(fs, "index_healthy", lambda *a, **k: True)
    hit = {"source_path": "/x.md", "section": "S", "content": "命中内容", "score": 0.9}
    monkeypatch.setattr(fs, "search_memory_files", lambda *a, **k: [hit])
    monkeypatch.setattr(fs, "gate_hits", lambda h, *a, **k: h)
    monkeypatch.setattr(
        fs, "backstop_chunks",
        lambda *a, **k: [{"source_path": "/b.md", "section": "B", "content": "兜底内容"}],
    )
    for msg in ("辛苦了辛苦了", "你好呀朋友", "谢谢你了哈"):
        monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "0")
        off = mgr.build_full_context_for_turn(msg)
        monkeypatch.setenv("DESKMATE_CONTEXT_PREFILTER", "1")
        on = mgr.build_full_context_for_turn(msg)
        assert on == off, f"轻闲聊 {msg!r} 前置跳过改变了注入文本"
