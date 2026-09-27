"""P1-1 主体错记检测（learned_store._detect_subject_conflict）测试。

覆盖真实踩过的坑形态：人称颠倒（小桔的活儿记成萧哥做的）、主体记错、
双主体/泛指不误报、书名极性冲突既有行为不回归、裁决落地、fail-open。
"""

from __future__ import annotations

import sqlite3

import pytest

from deskmate.agency.engine import AgencyEngine
from deskmate.agency.task_log import TaskStatus
from deskmate.memory.learned_store import LearnedStore


@pytest.fixture()
def store(tmp_path):
    return LearnedStore(base_dir=tmp_path)


def _statuses(store: LearnedStore) -> dict[str, str]:
    """fact -> status 的映射（全部状态）。"""
    con = sqlite3.connect(store.db_path)
    try:
        rows = con.execute("SELECT fact, status FROM learned_history").fetchall()
    finally:
        con.close()
    return {fact: status for fact, status in rows}


def _pending_conflicts(tmp_path) -> list:
    engine = AgencyEngine(base_dir=tmp_path)
    return [
        r
        for r in engine.log.list(limit=200, status=TaskStatus.PROPOSED)
        if r.action_type == "memory_subject_conflict"
    ]


# ---------------------------------------------------------------------------
# 触发：人称颠倒对
# ---------------------------------------------------------------------------


def test_subject_inversion_marks_new_fact_questioned(tmp_path, store):
    store.append(["小桔整理了史料库"])
    n = store.append(["萧哥整理了史料库"])
    assert n == 1
    st = _statuses(store)
    assert st["小桔整理了史料库"] == "active"  # 旧条不被机器翻案
    assert st["萧哥整理了史料库"] == "questioned"  # 新条不内化


def test_subject_inversion_queues_proposal_card(tmp_path, store):
    store.append(["小桔完成了圆桌纪要归档"])
    store.append(["萧哥完成了圆桌纪要归档"])
    cards = _pending_conflicts(tmp_path)
    assert len(cards) == 1
    extra = cards[0].extra
    assert extra["new_fact"] == "萧哥完成了圆桌纪要归档"
    assert extra["old_fact"] == "小桔完成了圆桌纪要归档"


def test_same_old_row_not_recarded(tmp_path, store):
    """同一旧条只发一张卡（不刷屏），但后续冲突新条仍被降级。"""
    store.append(["小桔写完了圆桌总结"])
    store.append(["萧哥写完了圆桌总结"])
    store.append(["用户写完了圆桌总结"])  # 不同 fp、同谓词、对立主体
    assert len(_pending_conflicts(tmp_path)) == 1
    st = _statuses(store)
    assert st["用户写完了圆桌总结"] == "questioned"


# ---------------------------------------------------------------------------
# 不误报：双主体 / 泛指 / 单字谓词 / 无关事实
# ---------------------------------------------------------------------------


def test_two_subjects_not_flagged(tmp_path, store):
    store.append(["萧盛让小桔整理史料"])
    n = store.append(["用户叫小桔整理史料"])
    assert n == 1
    st = _statuses(store)
    assert st["萧盛让小桔整理史料"] == "active"
    assert st["用户叫小桔整理史料"] == "active"
    assert _pending_conflicts(tmp_path) == []


def test_generic_pronoun_not_flagged(tmp_path, store):
    store.append(["他整理了史料库"])
    store.append(["自己整理了史料库"])
    st = _statuses(store)
    assert all(v == "active" for v in st.values())
    assert _pending_conflicts(tmp_path) == []


def test_single_char_predicate_not_flagged(tmp_path, store):
    """谓词剥完剩单字 → 无判别力，不配对（≥2 字才算证据）。"""
    store.append(["小桔去"])
    store.append(["萧哥去"])
    assert _pending_conflicts(tmp_path) == []
    assert all(v == "active" for v in _statuses(store).values())


def test_two_char_predicate_still_flagged(tmp_path, store):
    """边界对齐：恰好 2 字的谓词有判别力，正常参与配对。"""
    store.append(["小桔赢了"])
    store.append(["萧哥赢了"])
    assert len(_pending_conflicts(tmp_path)) == 1


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def test_kill_switch_disables_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_SUBJECT_GUARD", "0")
    store = LearnedStore(base_dir=tmp_path)
    store.append(["小桔整理了史料库"])
    store.append(["萧哥整理了史料库"])
    st = _statuses(store)
    assert all(v == "active" for v in st.values())
    assert _pending_conflicts(tmp_path) == []


# ---------------------------------------------------------------------------
# 裁决落地
# ---------------------------------------------------------------------------


def test_resolve_keep_new(tmp_path, store):
    store.append(["小桔整理了史料库"])
    store.append(["萧哥整理了史料库"])
    (card,) = _pending_conflicts(tmp_path)
    assert store.resolve_subject_conflict(
        int(card.extra["new_id"]), keep_new=True, old_id=int(card.extra["old_id"])
    )
    st = _statuses(store)
    assert st["萧哥整理了史料库"] == "active"
    assert st["小桔整理了史料库"] == "superseded"


def test_resolve_keep_old(tmp_path, store):
    store.append(["小桔整理了史料库"])
    store.append(["萧哥整理了史料库"])
    (card,) = _pending_conflicts(tmp_path)
    assert store.resolve_subject_conflict(int(card.extra["new_id"]), keep_new=False)
    st = _statuses(store)
    assert st["小桔整理了史料库"] == "active"
    assert st["萧哥整理了史料库"] == "forgotten"


def test_engine_resolve_end_to_end(tmp_path, store):
    """engine.resolve_proposal 全链路：executed / rejected 都能落地。"""
    engine = AgencyEngine(base_dir=tmp_path)
    store.append(["小桔完成了圆桌纪要归档"])
    store.append(["萧哥完成了圆桌纪要归档"])
    (card,) = _pending_conflicts(tmp_path)

    assert engine.resolve_proposal(card.id, TaskStatus.REJECTED)
    st = _statuses(store)
    assert st["小桔完成了圆桌纪要归档"] == "active"
    assert st["萧哥完成了圆桌纪要归档"] == "forgotten"

    # 再来一对，走 executed
    store.append(["小桔完成了周报归档"])
    store.append(["萧哥完成了周报归档"])
    (card2,) = [
        r for r in _pending_conflicts(tmp_path) if r.id != card.id
    ]
    assert engine.resolve_proposal(card2.id, TaskStatus.EXECUTED)
    st = _statuses(store)
    assert st["萧哥完成了周报归档"] == "active"
    assert st["小桔完成了周报归档"] == "superseded"


# ---------------------------------------------------------------------------
# 既有行为不回归：书名极性冲突照旧 supersede
# ---------------------------------------------------------------------------


def test_book_entity_polarity_conflict_unchanged(tmp_path, store):
    store.append(["《宋会要辑稿》在史料库"])
    store.append(["《宋会要辑稿》不在史料库"])
    st = _statuses(store)
    assert st["《宋会要辑稿》在史料库"] == "superseded"
    assert st["《宋会要辑稿》不在史料库"] == "active"


# ---------------------------------------------------------------------------
# fail-open
# ---------------------------------------------------------------------------


def test_fail_open_on_proposal_error(tmp_path, store, monkeypatch):
    """提议卡队列坏了也不影响写入主链路（fail-open）。"""
    from deskmate.memory import learned_store as ls

    def _boom(self, *a, **kw):
        raise RuntimeError("tasklog broken")

    monkeypatch.setattr(ls.LearnedStore, "_queue_subject_proposal", _boom)
    store.append(["小桔整理了史料库"])
    n = store.append(["萧哥整理了史料库"])
    assert n == 1
