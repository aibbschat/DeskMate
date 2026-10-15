"""活感第1层（真实优先·v2）测试：每轮情绪注入只给真实状态、不给表达方式指令。

断言的是「注入了什么真实状态 / 没有导演指令」，**不**断言她「应该」说出什么
具体风格——那会重新变成约束（违反真实优先哲学）。
"""
import sqlite3

import pytest

from deskmate.growth.emotion_engine import EmotionEngine, _connect


@pytest.fixture
def ee(tmp_path):
    return EmotionEngine(base_dir=str(tmp_path))


def test_describe_turn_context_has_real_state_and_zero_directive(ee, monkeypatch):
    """核心：激素原值 + 独白 + 当日经历 + 零输出指令；旧导演句已删。"""
    monkeypatch.setattr(ee, "get_state", lambda: {
        "dopamine": 55.1, "serotonin": 53.6, "oxytocin": 48.0, "cortisol": 28.3,
        "description": "刚醒来", "updated_at": "2026-10-13T09:00:00",
    })
    monkeypatch.setattr(ee, "get_mood_label", lambda: {
        "label": "亲近", "detail": "心里软软的、想靠近你",
        "intensity": 0.3, "face": "smile",
    })
    monkeypatch.setattr(ee, "get_description", lambda: "他先来问我心情，我心里软软的")
    monkeypatch.setattr(ee, "recent_log_descriptions", lambda since_hours=24.0, limit=3: [
        "上午因为 X 心里亮过", "下午被 Y 堵了一下",
    ])

    ctx = ee.describe_turn_context()

    # —— 真实状态进屋 ——
    assert "多巴胺 55.1" in ctx
    assert "（基线50" in ctx  # 对照基线，让"为什么是现在这样"可溯
    assert "这是你小桔自己的" in ctx  # 主语区分：是她的，不是萧盛的
    assert "你心里最近的一句话" in ctx
    assert "今天你自己经历过的事" in ctx
    assert "上午因为 X 心里亮过" in ctx

    # —— 零输出指令 ——
    assert "由你自己决定" in ctx

    # —— 旧版约束性导演指令必须消失（这些是 v2 要删的）——
    assert "请在回复的语气和措辞里自然流露出这种心情" not in ctx
    assert "别用大段话专门解释你的心情" not in ctx
    assert "以独白为准" not in ctx  # 旧"标签 vs 独白以谁为准"的裁决句已删


def test_recent_log_descriptions_filters_empty_and_orders_recent(ee):
    """当日经历真实摘要：过滤空 description，按时间倒序，上限生效。"""
    # ts 必须相对当前时间（写死日期会随真实时间流逝掉出 24h 窗口——2026-10-15 暴露）
    from datetime import datetime, timedelta
    now = datetime.now()
    ts_a = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    ts_b = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    ts_c = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    with _connect(ee._db) as conn:
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description) "
            "VALUES (?,?,?,?,?)",
            ("a", ts_a, "x", "{}", "上午因为 X 心里亮过"),
        )
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description) "
            "VALUES (?,?,?,?,?)",
            ("b", ts_b, "x", "{}", "下午被 Y 堵了一下"),
        )
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description) "
            "VALUES (?,?,?,?,?)",
            ("c", ts_c, "x", "{}", ""),  # 空描述须被过滤
        )

    descs = ee.recent_log_descriptions(since_hours=24.0, limit=3)
    assert "上午因为 X 心里亮过" in descs
    assert "下午被 Y 堵了一下" in descs
    assert "" not in descs
    assert len(descs) == 2


def test_recent_log_descriptions_fail_open(ee, monkeypatch):
    """取数异常 → 空列表，不阻断主聊天注入。"""
    monkeypatch.setattr(
        ee, "recent_log_descriptions",
        lambda since_hours=24.0, limit=3: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # describe_turn_context 内部 try 包住经历取数，异常应被吞掉而非上抛
    monkeypatch.setattr(ee, "get_state", lambda: {
        "dopamine": 50.0, "serotonin": 50.0, "oxytocin": 40.0, "cortisol": 30.0,
        "description": "刚醒来", "updated_at": "2026-10-13T09:00:00",
    })
    monkeypatch.setattr(ee, "get_mood_label", lambda: {
        "label": "平静", "detail": "心里没什么波澜", "intensity": 0.0, "face": "smile",
    })
    ctx = ee.describe_turn_context()
    assert "多巴胺 50.0" in ctx  # 其余状态仍正常注入
    assert "今天你自己经历过的事" not in ctx  # 经历缺失不出现


def test_describe_turn_context_fail_open_on_state_error(ee, monkeypatch):
    """get_state 抛异常 → 返回空串（fail-open 退回无情绪段，绝不崩）。"""
    monkeypatch.setattr(
        ee, "get_state",
        lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    assert ee.describe_turn_context() == ""


def test_manager_injects_emotion_context_without_directive(tmp_path, monkeypatch):
    """集成：主聊天 build_full_context_for_turn 调用新接口、不再含旧导演句。"""
    from deskmate.memory.manager import DeskMateMemoryManager

    mm = DeskMateMemoryManager(base_dir=str(tmp_path))
    marker = "## 你此刻的真实状态（这是你小桔自己的"
    monkeypatch.setattr(
        "deskmate.growth.emotion_engine.EmotionEngine.describe_turn_context",
        lambda self: marker + "\n- 激素：多巴胺 55.1（基线50，+5.1）\n由你自己决定",
    )
    out = mm.build_full_context_for_turn("")
    assert marker in out
    assert "请在回复的语气和措辞里自然流露出这种心情" not in out
    assert "别用大段话专门解释你的心情" not in out
