"""thought_stream 模块 + 第4层注入（真实优先·v2）测试。

断言「想法能存储 / 能按近因召回进聊天 / fail-open」——不断言她「应该」
想什么具体内容（那会重新变成约束，违反真实优先哲学）。
"""
import pytest

from deskmate.growth.emotion_engine import EmotionEngine
from deskmate.thought_stream import add_thought, recent_thoughts


def test_add_and_recent_roundtrip(tmp_path):
    """核心：写一条想法 → 近因召回能拿到。"""
    base = str(tmp_path)
    assert add_thought(base, "我相信真诚比技巧重要", kind="worldview", about="做人")
    out = recent_thoughts(base, limit=2)
    assert "我相信真诚比技巧重要" in out


def test_recent_dedup_adjacent(tmp_path):
    """相邻重复的想法须跳过（数据清洗，非导演）。"""
    base = str(tmp_path)
    add_thought(base, "同一句想法")
    add_thought(base, "同一句想法")  # 相邻重复须跳过
    add_thought(base, "另一句想法")
    out = recent_thoughts(base, limit=5)
    # 去重后只留两条；顺序不依赖（批量写入 ts 同秒，ORDER BY ts 的 tiebreak 不稳）
    assert set(out) == {"同一句想法", "另一句想法"}
    assert len(out) == 2


def test_fail_open_bad_db():
    """坏 base_dir：写返回 False、读返回 []，绝不抛异常。"""
    assert add_thought("/nonexistent/path/xyz", "想法") is False
    assert recent_thoughts("/nonexistent/path/xyz", limit=2) == []


def test_soft_cap_keeps_recent(tmp_path, monkeypatch):
    """超过软上限后清理至 _KEEP_ROWS，且保留最近（ts 最大）的。"""
    import deskmate.thought_stream as ts

    monkeypatch.setattr(ts, "_MAX_ROWS", 5)
    monkeypatch.setattr(ts, "_KEEP_ROWS", 3)
    base = str(tmp_path)
    # 批量写（远超软上限），多次触发清理；最终只保留最近 _KEEP_ROWS 条
    for i in range(20):
        add_thought(base, f"想法{i}")
    out = recent_thoughts(base, limit=10, since_hours=24 * 365)
    # 软上限生效：写入 20 条但最终远少于 20，并封顶在 _MAX_ROWS（5）
    assert len(out) <= 5
    assert "想法0" not in out  # 最早的条目已被清理（保留最近的）
    assert "想法19" in out     # 最近写入的仍在


def test_injection_in_describe_turn_context(tmp_path, monkeypatch):
    """核心集成：想法流近因召回 → describe_turn_context 含「你最近在想的事」。"""
    ee = EmotionEngine(base_dir=str(tmp_path))
    # 灌一条真实想法（模拟 reflection/worldview 已产出）
    add_thought(
        str(tmp_path),
        "他上次说我写得太顺，反而没了筋骨——这话我记着",
        kind="reflection",
    )
    # 其余状态用假数据，只验证想法段进屋
    monkeypatch.setattr(ee, "get_state", lambda: {
        "dopamine": 50.0, "serotonin": 50.0, "oxytocin": 40.0, "cortisol": 30.0,
        "description": "刚醒来", "updated_at": "2026-10-13T09:00:00",
    })
    monkeypatch.setattr(ee, "get_mood_label", lambda: {
        "label": "平静", "detail": "心里没什么波澜", "intensity": 0.0, "face": "smile",
    })
    monkeypatch.setattr(ee, "get_description", lambda: "")
    monkeypatch.setattr(ee, "recent_log_descriptions", lambda since_hours=24.0, limit=3: [])

    ctx = ee.describe_turn_context()
    assert "你最近在想的事" in ctx
    assert "他上次说我写得太顺，反而没了筋骨" in ctx
    # 旧导演指令仍不可出现（第1层已删，第4层不往回加）
    assert "请在回复的语气和措辞里自然流露出这种心情" not in ctx


def test_reflection_writes_thought_on_verdict(tmp_path, monkeypatch):
    """数据源契约：reflection 成功裁决后，裁决理由写进想法流（接线真闭环）。"""
    import deskmate.agency.behaviors.reflection as refl_mod

    calls = []

    class FakeStore:
        def __init__(self, db_path=None):
            pass

        def _connect(self):
            raise RuntimeError("unused")

        def record_reflection(self, *a, **k):
            return True

        def apply_reflection_verdict(self, *a, **k):
            return None

    monkeypatch.setattr(refl_mod, "_recent_changes", lambda store, limit: [
        {"item_id": "i1", "seq": 1, "action": "created", "old_value": "",
         "new_value": "x", "reason": "当时理由", "ts": "2026-10-01T00:00:00",
         "subject_kind": "profile_item", "dimension": "d", "content": "萧盛认为真诚重要"},
        {"item_id": "i2", "seq": 1, "action": "updated", "old_value": "",
         "new_value": "y", "reason": "当时理由2", "ts": "2026-10-02T00:00:00",
         "subject_kind": "profile_item", "dimension": "d", "content": "另一条"},
        {"item_id": "i3", "seq": 1, "action": "deepened", "old_value": "",
         "new_value": "z", "reason": "当时理由3", "ts": "2026-10-03T00:00:00",
         "subject_kind": "profile_item", "dimension": "d", "content": "第三条"},
    ])
    monkeypatch.setattr(refl_mod, "_is_due", lambda state, cooldown: True)
    monkeypatch.setattr(refl_mod, "_load_json", lambda path: {})
    monkeypatch.setattr(
        "deskmate.cognitive.store.CognitiveStore", FakeStore
    )
    monkeypatch.setattr(
        "deskmate.memory.llm.memory_llm_available", lambda: True
    )
    monkeypatch.setattr(
        "deskmate.memory.llm.chat_memory",
        lambda *a, **k: "编号：1\n裁决：保持\n理由：因为他上次明确说过这事，我记得",
    )
    monkeypatch.setattr(
        "deskmate.thought_stream.add_thought",
        lambda base_dir, text, kind="thought", about=None: calls.append((text, kind)) or True,
    )

    result = refl_mod.run_reflection(str(tmp_path))
    assert result["reflected"] is True
    assert result["verdicts"] >= 1
    # 裁决理由真的写进了想法流
    assert any("因为他上次明确说过" in t for t, k in calls)
    assert any(k == "reflection" for t, k in calls)


def test_worldview_writes_thought_on_belief(tmp_path, monkeypatch):
    """数据源契约：worldview 回望长出「我相信」后，写进想法流（接线真闭环）。"""
    import deskmate.agency.behaviors.worldview_reflection as wv_mod

    calls = []

    class FakeStance:
        id = "s1"

    class FakeProfileMgr:
        def __init__(self, profile_path=None, layer=None):
            pass

        def add_stance(self, *a, **k):
            return FakeStance(), "created"

        def add_value_ranking(self, *a, **k):
            return FakeStance(), "created"

        def add_thinking_framework(self, *a, **k):
            return FakeStance(), "created"

    class FakeSourceRef:
        def __init__(self, **k):
            pass

    class FakeCognitiveStore:
        def __init__(self, db_path=None):
            pass

        def record_reflection(self, *a, **k):
            return True

    monkeypatch.setattr(
        wv_mod, "_load_understanding",
        lambda base_dir: [
            {"body": "外面常有人为流量丢掉底线", "date": "2026-10-01", "entry_seq": i}
            for i in range(5)
        ],
    )
    monkeypatch.setattr(wv_mod, "_is_due", lambda state, cooldown: True)
    monkeypatch.setattr(wv_mod, "_load_grounding", lambda base_dir: [])
    monkeypatch.setattr(
        "deskmate.cognitive.profile.CognitiveProfileManager", FakeProfileMgr
    )
    monkeypatch.setattr(
        "deskmate.cognitive.models.SourceRef", FakeSourceRef
    )
    monkeypatch.setattr(
        "deskmate.cognitive.store.CognitiveStore", FakeCognitiveStore
    )
    monkeypatch.setattr(
        "deskmate.memory.llm.memory_llm_available", lambda: True
    )
    monkeypatch.setattr(
        "deskmate.memory.llm.chat_memory",
        lambda *a, **k: (
            "我相信：真诚比技巧重要，流量留不住人\n"
            "关于：做人\n我看重：真诚 > 流量 > 技巧\n"
            "我习惯这么看：先问动机 → 再看做法 → 最后判断"
        ),
    )
    monkeypatch.setattr(
        "deskmate.thought_stream.add_thought",
        lambda base_dir, text, kind="thought", about=None: calls.append((text, kind, about)) or True,
    )

    result = wv_mod.run_worldview_reflection(str(tmp_path))
    assert result["reflected"] is True
    assert result["written"] >= 1
    # 「我相信」真的写进了想法流
    assert any("真诚比技巧重要" in t for t, k, a in calls)
    assert any(k == "worldview" for t, k, a in calls)
