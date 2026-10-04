"""回归：persist() 全量覆盖不得抹掉同步身份列（2026-10-04 根因 B 项止血）。

根因：store.persist() 用 INSERT OR REPLACE 重插时列清单不含 sync_uuid/sync_hlc/
sync_tombstone，每次 save() 都把已同步条目的身份清零（即当晚 80 行丢失的同一根因）。

本文件锁死不变量：
  - 已带同步身份的行，经 persist() 往返（含连续两次）后身份保持不变；
  - 不出现「内容在、sync_uuid 被退化为 NULL」的行（昨晚的失效模式）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from deskmate.cognitive.models import SourceRef
from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.store import CognitiveStore


def _add_sync_cols(store: CognitiveStore):
    for col, ctype in (
        ("sync_uuid", "TEXT"),
        ("sync_hlc", "TEXT"),
        ("sync_tombstone", "INTEGER"),
    ):
        store._add_column("profile_items", col, ctype)


def _seed_synced_rows(store: CognitiveStore):
    """直接插 2 行已带同步身份的数据（模拟已与云端同步过的状态）。"""
    ids = ["stance_seed_a", "stance_seed_b"]
    uuids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with store._connect() as conn:
        for rid, su in zip(ids, uuids):
            conn.execute(
                "INSERT INTO profile_items"
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated, sync_uuid, sync_hlc, sync_tombstone) "
                "VALUES(?, 'cognitive', 'stance', '话题', '内容', '立场', 'active', 0.9, "
                "'{}', '2026-10-03', '2026-10-03', ?, '20261003T000000', 0)",
                (rid, su),
            )
    return dict(zip(ids, uuids))


def test_persist_preserves_sync_identity(tmp_path: Path):
    db = tmp_path / "sync_id.db"
    mgr = CognitiveProfileManager(profile_path=str(db), layer="cognitive")
    store = mgr.store
    _add_sync_cols(store)

    seeded = _seed_synced_rows(store)
    mgr._load()  # 触发 persist 往返
    assert len(mgr.profile.stances) == 2, "预置的 2 行应被 load 出来"
    mgr.save()

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, sync_uuid, sync_tombstone FROM profile_items WHERE layer='cognitive'"
        ).fetchall()
    assert len(rows) == 2, f"行数应仍为 2，实际 {len(rows)}"
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    for rid, su in seeded.items():
        assert by_id[rid][0] == su, f"行 {rid} 的 sync_uuid 被抹掉：原 {su} 现 {by_id[rid][0]}"
    nulls = sum(1 for r in rows if r[1] is None)
    assert nulls == 0, f"存在 {nulls} 行 sync_uuid 变 NULL（即昨晚失效模式）"


def test_persist_twice_still_preserves(tmp_path: Path):
    """连续两次 persist 仍应保留身份（防回归：首次 OK、二次漏）。"""
    db = tmp_path / "sync_id2.db"
    mgr = CognitiveProfileManager(profile_path=str(db), layer="cognitive")
    store = mgr.store
    _add_sync_cols(store)
    seeded = _seed_synced_rows(store)
    mgr._load()
    mgr.save()
    mgr.save()  # 第二次
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, sync_uuid FROM profile_items WHERE layer='cognitive'"
        ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    for rid, su in seeded.items():
        assert by_id.get(rid) == su, f"第二次 persist 后 {rid} 身份丢失"


def test_new_row_gets_null_sync_identity(tmp_path: Path):
    """新增行（map 里没有）sync_uuid 留 NULL，由 migrate 后续回填——行为不变、不退化。"""
    db = tmp_path / "sync_id3.db"
    mgr = CognitiveProfileManager(profile_path=str(db), layer="cognitive")
    store = mgr.store
    _add_sync_cols(store)
    _seed_synced_rows(store)
    mgr._load()
    mgr.add_stance(
        topic="新话题", content="新内容",
        source=SourceRef(type="test", title="t", excerpt="e", date="2026-10-04", confidence=0.8),
    )
    mgr.save()
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, sync_uuid FROM profile_items WHERE layer='cognitive'"
        ).fetchall()
    assert len(rows) == 3, f"应有 3 行（2 预置 + 1 新增），实际 {len(rows)}"
    new = [r for r in rows if not r[0].startswith("stance_seed")]
    assert new and new[0][1] is None, "新增行 sync_uuid 应为 NULL（待 migrate 回填）"
