"""主动产出管道 · 台账层 单测（P0 正确性验证）。

覆盖：
- 确定性 id 合并（同锚点/同内容 → 同 id，对应 48 条身份分裂教训）
- status 只前进不后退（跨端收敛，防反 nag 失效）
- 本地对话式对接（pending → 开场白；ack 后不再递）

测试一律用 /tmp 副本库，不碰活库。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from deskmate.memory.sync_daemon import (
    HLC,
    Op,
    Record,
    SyncEngine,
    _lww_winner,
    _status_advances,
)
from deskmate.memory.sync_store_sqlite import (
    SQLiteOplogStore,
    SQLiteSyncStore,
    migrate,
)
from deskmate.proactivity.ledger import (
    STATUS_ORDER,
    acknowledge_ledger,
    build_handoff_opening,
    pending_items,
    record_proactive,
)


def _fresh_base(node: str) -> Path:
    """建临时基目录 + deskmate.db 并建表+迁移 proactive_ledger，返回 base 目录。

    migrate 只处理「已存在」的表，故先经 ledger._ensure_table 自举建表
    （CREATE + 加同步列 + 触发器），再让 migrate 接管同步列与触发器。
    """
    base = Path(tempfile.mkdtemp(prefix="pt_proactive_"))
    db = base / "deskmate.db"
    conn = sqlite3.connect(str(db))
    from deskmate.proactivity.ledger import _ensure_table

    _ensure_table(conn)
    conn.close()
    return base


def _store(db: str) -> SQLiteSyncStore:
    return SQLiteSyncStore(sqlite3.connect(db, check_same_thread=False), "proactive_ledger")


def _rec(status: str, hlc_s: str, tombstone: bool = False) -> Record:
    return Record(
        record_id="rid1",
        data={"status": status, "content": "c"},
        hlc=HLC.from_str(hlc_s),
        tombstone=tombstone,
        valid_until=None,
    )


def _status_of(db: str, rid: str) -> str:
    with sqlite3.connect(db, check_same_thread=False) as c:
        r = c.execute(
            "SELECT status FROM proactive_ledger WHERE id=?", (rid,)
        ).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------------------
# 1. 确定性 id 合并
# ---------------------------------------------------------------------------
def test_deterministic_id_merges_same_anchor():
    from deskmate.proactivity.ledger import _deterministic_id

    # 同锚点、不同 content → 同 id（合并，不重复）
    rid1 = _deterministic_id("cloud_audit", "plot_contradiction", "ch7", "内容A")
    rid2 = _deterministic_id("cloud_audit", "plot_contradiction", "ch7", "内容B")
    assert rid1 == rid2
    # 无锚点、同 content → 同 id
    rid3 = _deterministic_id("cloud_audit", "plot_contradiction", None, "内容A")
    rid4 = _deterministic_id("cloud_audit", "plot_contradiction", None, "内容A")
    assert rid3 == rid4
    # 不同锚点 → 不同 id
    assert rid1 != rid3


# ---------------------------------------------------------------------------
# 2. 落库 + 本地对接
# ---------------------------------------------------------------------------
def test_record_and_handoff():
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base,
        source="cloud_audit",
        kind="plot_contradiction",
        content="伏笔X悬了9节",
        evidence="第七章",
        anchor="ch7",
    )
    items = pending_items(base)
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    opening = build_handoff_opening(base)
    assert opening is not None
    assert "伏笔X悬了9节" in opening["text"]
    # ack 后不再 pending（不重复递）
    rid = items[0]["id"]
    assert acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert pending_items(base) == []


def test_acknowledge_no_downgrade():
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线Y春秋对不上", anchor="tlY",
    )
    # 先 actioned，再尝试 dismiss→ 允许（同终态）；尝试回 pending → 拒绝
    acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert not acknowledge_ledger(base, rid=rid, new_status="pending")  # 降级被拒
    assert _status_of(str(base / "deskmate.db"), rid) == "actioned"


# ---------------------------------------------------------------------------
# 3. status 只前进（纯函数 + LWW）
# ---------------------------------------------------------------------------
def test_status_advances_pure():
    assert _status_advances("pending", "actioned", STATUS_ORDER) is True
    assert _status_advances("actioned", "pending", STATUS_ORDER) is False
    assert _status_advances("pending", "pending", STATUS_ORDER) is False
    assert _status_advances("pending", "unknown", STATUS_ORDER) is False


def test_lww_winner_status_priority():
    ordered = STATUS_ORDER
    # actioned（hlc 早） vs pending（hlc 晚）：status 优先 → actioned 胜
    a_old = _rec("actioned", "0000000000000001.00000000.mac")
    b_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(a_old, b_new, ordered) is a_old

    # 同 status → 退回 hlc LWW
    c_old = _rec("pending", "0000000000000001.00000000.mac")
    d_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(c_old, d_new, ordered) is d_new

    # 无 ordered_status → 纯 hlc
    e_old = _rec("actioned", "0000000000000001.00000000.mac")
    f_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(e_old, f_new) is f_new  # 默认无 status 优先 → hlc 晚者胜


# ---------------------------------------------------------------------------
# 4. _apply_one_to 集成：跨端 status 只前进
# ---------------------------------------------------------------------------
def _payload(status: str) -> dict:
    return {
        "id": "rid1", "source": "cloud_audit", "kind": "plot_contradiction",
        "content": "c", "evidence": "", "understanding": "", "payload": "{}",
        "source_node": "mac", "status": status, "confidence": 0.9,
        "created_at": "t", "acknowledged_at": "",
    }


def test_apply_one_to_status_forward_no_downgrade():
    """本端 actioned（hlc 新）不被对端迟到/更旧的 pending 覆盖。"""
    base = _fresh_base("mac")
    st = _store(str(base / "deskmate.db"))
    eng = SyncEngine(st, SQLiteOplogStore(sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False)), "mac", "proactive_ledger")
    # 本地写 actioned（hlc 较新）
    eng.local_upsert("proactive_ledger", "rid1", _payload("actioned"))
    # 对端迟到 pending（hlc 更早）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    op_pending = Op("proactive_ledger", "rid1", "upsert", old_hlc, _payload("pending"), origin="cloud")
    eng.apply_remote([op_pending])
    assert _status_of(str(base / "deskmate.db"), "rid1") == "actioned"


def test_apply_one_to_status_advances():
    """本端 pending（hlc 较旧）被对端来的新 actioned 覆盖（前进）。"""
    base = _fresh_base("cloud")
    st = _store(str(base / "deskmate.db"))
    eng = SyncEngine(st, SQLiteOplogStore(sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False)), "cloud", "proactive_ledger")
    # 本地写 pending（hlc 较旧）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    st.upsert("rid1", _payload("pending"), old_hlc)  # 直写，绕过 oplog
    # 对端来的 actioned（hlc 新）
    new_hlc = HLC.from_str("0000000000000099.00000000.mac")
    op_actioned = Op("proactive_ledger", "rid1", "upsert", new_hlc, _payload("actioned"), origin="mac")
    eng.apply_remote([op_actioned])
    assert _status_of(str(base / "deskmate.db"), "rid1") == "actioned"


def test_cross_node_status_converge():
    """双向同步后两端都收敛到 actioned（不降级、不分歧）。"""
    base_mac = _fresh_base("mac")
    base_cloud = _fresh_base("cloud")
    db_mac = str(base_mac / "deskmate.db")
    db_cloud = str(base_cloud / "deskmate.db")
    # mac 本地写 actioned（hlc 新）
    eng_mac = SyncEngine(_store(db_mac), SQLiteOplogStore(sqlite3.connect(db_mac, check_same_thread=False)), "mac", "proactive_ledger")
    eng_mac.local_upsert("proactive_ledger", "rid1", _payload("actioned"))
    # cloud 本地写 pending（hlc 旧，模拟更早产生但迟到）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    _store(db_cloud).upsert("rid1", _payload("pending"), old_hlc)
    # cloud 的迟到 pending apply → mac（应保留 actioned）
    eng_mac.apply_remote([Op("proactive_ledger", "rid1", "upsert", old_hlc, _payload("pending"), origin="cloud")])
    assert _status_of(db_mac, "rid1") == "actioned"
    # mac 的 actioned op apply → cloud（应前进到 actioned）
    eng_cloud = SyncEngine(_store(db_cloud), SQLiteOplogStore(sqlite3.connect(db_cloud, check_same_thread=False)), "cloud", "proactive_ledger")
    eng_cloud.apply_remote(eng_mac.oplog.since(0))
    assert _status_of(db_cloud, "rid1") == "actioned"
