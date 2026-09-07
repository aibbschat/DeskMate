"""认知层「防整层清空」护栏测试（2026-09-07 事故后加）。

事故回放：``store.persist`` 是 ``DELETE FROM profile_items WHERE layer=?``
全量覆盖。09-07 因测试误用全局 manager，一次 save 把 cognitive 层 37 条
真实画像清成几条测试数据。护栏把「库里有数据、写入 0 条」这条路堵死。

覆盖：
1. 库里有条目 + 写空画像 → 拒绝（抛错）且原数据分毫不动
2. force=True → 允许清空（保留重置逃生口）
3. manager.save() 走同一条护栏
4. 正常写入（有内容）不受影响
5. 空库写空画像 → 不拦（本就无需保护）
"""

import tempfile
from pathlib import Path

import pytest

from deskmate.cognitive.models import CognitiveProfile, SourceRef
from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def pm(tmp_path):
    db = tmp_path / "deskmate.db"
    m = CognitiveProfileManager(db)
    yield m


def _count(db: Path, layer: str = "cognitive") -> int:
    import sqlite3

    conn = sqlite3.connect(str(db))
    n = conn.execute(
        "SELECT COUNT(*) FROM profile_items WHERE layer=?", (layer,)
    ).fetchone()[0]
    conn.close()
    return n


def test_refuse_wipe_when_incoming_empty(pm, tmp_path):
    """库里有数据、写入空画像 → 拒绝，原数据不动。"""
    db = tmp_path / "deskmate.db"
    pm.add_stance("写作", "坚持考据优先", SourceRef(type="interview", title="x"))
    pm.add_stance("家庭", "家人优先", SourceRef(type="interview", title="x"))
    assert _count(db) == 2

    store = CognitiveStore(db_path=db)
    with pytest.raises(RuntimeError) as e:
        store.persist(CognitiveProfile(), layer="cognitive")
    assert "refuse to wipe" in str(e.value)
    # 原数据分毫不动
    assert _count(db) == 2


def test_force_allows_wipe(pm, tmp_path):
    """显式 force=True 仍可清空（保留重置逃生口）。"""
    db = tmp_path / "deskmate.db"
    pm.add_stance("写作", "坚持考据优先", SourceRef(type="interview", title="x"))
    assert _count(db) == 1

    store = CognitiveStore(db_path=db)
    store.persist(CognitiveProfile(), layer="cognitive", force=True)
    assert _count(db) == 0


def test_manager_save_guarded(pm, tmp_path):
    """manager.save() 走同一条护栏。"""
    db = tmp_path / "deskmate.db"
    pm.add_stance("写作", "坚持考据优先", SourceRef(type="interview", title="x"))
    assert _count(db) == 1

    # 模拟事故：手里的 profile 是空的（load 失败 / 误用全局 manager）
    pm.profile.stances.clear()
    pm.profile.argument_patterns.clear()
    pm.profile.value_rankings.clear()
    pm.profile.thinking_frameworks.clear()

    with pytest.raises(RuntimeError):
        pm.save()
    assert _count(db) == 1, "护栏没拦住，数据会被清空"


def test_normal_write_unaffected(pm, tmp_path):
    """正常写入（有内容）不受护栏影响。"""
    db = tmp_path / "deskmate.db"
    pm.add_stance("写作", "坚持考据优先", SourceRef(type="interview", title="x"))
    pm.add_stance("家庭", "家人优先", SourceRef(type="interview", title="x"))
    pm.add_stance("史学", "正史需交叉比对", SourceRef(type="interview", title="x"))
    assert _count(db) == 3

    store = CognitiveStore(db_path=db)
    loaded = store.load_profile()
    store.persist(loaded, layer="cognitive")   # 正常往返，不抛错
    assert _count(db) == 3


def test_empty_db_write_empty_ok(tmp_path):
    """空库写空画像 → 不拦（本就没什么可保护的）。"""
    db = tmp_path / "deskmate.db"
    store = CognitiveStore(db_path=db)
    store.persist(CognitiveProfile(), layer="cognitive")   # 不抛错
    assert _count(db) == 0


def test_guard_is_per_layer(pm, tmp_path):
    """护栏按层生效：清 worldview 层不会影响 cognitive 层的判定。"""
    db = tmp_path / "deskmate.db"
    pm.add_stance("写作", "坚持考据优先", SourceRef(type="interview", title="x"))
    assert _count(db, "cognitive") == 1
    assert _count(db, "worldview") == 0

    store = CognitiveStore(db_path=db)
    # worldview 层本就空 → 允许写空（不拦）
    store.persist(CognitiveProfile(), layer="worldview")
    # cognitive 层仍有数据
    assert _count(db, "cognitive") == 1
