"""B 类记录级同步 · SQLite 适配器端到端测试（阶段 1 落地验证）。

全部在 /tmp 副本进行，**不碰 ~/.deskmate 活库**。验证口径对齐草案 §6.7 防假绿：
- 按 sync_uuid 逐条对账（不只数总量）
- 断网双写后双向 tick 收敛
- 同 record_id 两端都写 → 后者胜（LWW）
- 删除后旧副本不把记录复活（防遗忘复活）
"""

import os
import sqlite3
import tempfile

import pytest

from deskmate.memory.sync_daemon import (
    SyncEngine,
    SyncDaemon,
    InMemoryTransport,
    MemoryStore,
    OplogStore,
    Op,
    HLC,
    Transport,
    new_uuid,
    reconcile_baseline,
)
from deskmate.memory.sync_store_sqlite import (
    migrate,
    SQLiteSyncStore,
    SQLiteOplogStore,
)

CREATE = """CREATE TABLE profile_items (
    id TEXT PRIMARY KEY,
    layer TEXT DEFAULT 'cognitive',
    dimension TEXT,
    topic TEXT DEFAULT '',
    content TEXT DEFAULT '',
    category TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    confidence REAL DEFAULT 0.5,
    extra_json TEXT DEFAULT '{}',
    first_extracted TEXT,
    last_updated TEXT,
    valid_from TEXT,
    valid_until TEXT
)"""


def _uuid_of(db, orig_id):
    c = sqlite3.connect(db)
    return c.execute(
        "SELECT sync_uuid FROM profile_items WHERE id=?", (orig_id,)
    ).fetchone()[0]


@pytest.fixture
def pair():
    d = tempfile.mkdtemp(prefix="pt_sync_sqlite_")
    ca, cb = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    for p in (ca, cb):
        c = sqlite3.connect(p)
        c.execute(CREATE)
        for i in range(3):
            c.execute(
                "INSERT INTO profile_items(id, topic, content) VALUES(?,?,?)",
                (f"seed{i}", f"topic{i}", f"content{i}"),
            )
        c.commit()
        c.close()
    for p, node in ((ca, "mac"), (cb, "cloud")):
        c = sqlite3.connect(p)
        migrate(c, node)
        c.close()
    return ca, cb


def _nodes(ca, cb):
    cA = sqlite3.connect(ca)
    sA, oA = SQLiteSyncStore(cA), SQLiteOplogStore(cA)
    eA = SyncEngine(sA, oA, "mac")
    cB = sqlite3.connect(cb)
    sB, oB = SQLiteSyncStore(cB), SQLiteOplogStore(cB)
    eB = SyncEngine(sB, oB, "cloud")
    dA = SyncDaemon(eA, InMemoryTransport(oB))  # A 拨出连 B
    dB = SyncDaemon(eB, InMemoryTransport(oA))  # B 拨出连 A
    return dict(cA=cA, cB=cB, sA=sA, sB=sB, eA=eA, eB=eB, dA=dA, dB=dB)


def test_converge_after_offline_writes(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    seed0 = _uuid_of(ca, "seed0")
    seed1 = _uuid_of(cb, "seed1")
    seed2 = _uuid_of(cb, "seed2")

    # A 改 seed0、新增一条；B 改 seed1、删除 seed2 —— 模拟离线各自写
    n["eA"].local_upsert("profile_items", seed0, {"topic": "seed0", "content": "A edited"})
    nA_new = new_uuid()
    n["eA"].local_upsert("profile_items", nA_new, {"topic": "new", "content": "A write"})
    n["eB"].local_upsert("profile_items", seed1, {"topic": "seed1", "content": "B edited"})
    n["eB"].local_delete("profile_items", seed2)

    for _ in range(4):
        n["dA"].tick()
        n["dB"].tick()

    la = {r.record_id: r.data for r in n["sA"].live_items()}
    lb = {r.record_id: r.data for r in n["sB"].live_items()}
    assert set(la) == set(lb), f"uuid 集合不一致: A={set(la)} B={set(lb)}"
    assert len(la) == 3, f"应为 3 条活记录, 实得 {len(la)}"
    assert la[seed0]["content"] == "A edited"
    assert la[seed1]["content"] == "B edited"
    assert la[nA_new]["content"] == "A write"
    assert seed2 not in la


def test_lww_later_wins(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    x = new_uuid()
    h_a = HLC(1000, 0, "mac")
    h_b = HLC(2000, 0, "cloud")  # 更晚
    n["eA"].store.upsert(x, {"content": "A"}, h_a)
    n["eA"].oplog.append(Op("profile_items", x, "upsert", h_a, {"content": "A"}))
    n["eB"].store.upsert(x, {"content": "B"}, h_b)
    n["eB"].oplog.append(Op("profile_items", x, "upsert", h_b, {"content": "B"}))

    n["dA"].tick()
    n["dB"].tick()
    n["dA"].tick()

    assert n["sA"].get(x).data["content"] == "B", "LWW 应为后者(B)胜"
    assert n["sB"].get(x).data["content"] == "B"


def test_no_resurrection_after_delete(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    x = new_uuid()
    # B 先写 X（旧 HLC）
    n["eB"].local_upsert("profile_items", x, {"topic": "X", "content": "B old"})
    n["dB"].tick()
    n["dA"].tick()  # A 拿到 X
    # A 删除 X（HLC 晚于 B 的写）
    n["eA"].local_delete("profile_items", x)
    n["dA"].tick()
    n["dB"].tick()  # B 也删 X
    assert n["sA"].get(x).tombstone, "A 应已删除 X"

    # 模拟 B 把 X 的旧 upsert 事件重推（断网/重发场景）
    old_ops = [
        op for op in n["eB"].oplog.all()
        if op.record_id == x and not op.tombstone
    ]
    n["eA"].apply_remote(old_ops)
    assert n["sA"].get(x).tombstone, "遗忘不应被旧副本复活"


def test_backfill_idempotent(pair):
    ca, _ = pair
    # 再跑一次 migrate 应安全（不报错、不重复回填）
    c = sqlite3.connect(ca)
    r = migrate(c, "mac")
    c.close()
    assert r["backfilled"] == 0, "二次 migrate 不应再回填"
    cA = sqlite3.connect(ca)
    n_sync = cA.execute(
        "SELECT COUNT(*) FROM profile_items WHERE sync_uuid IS NULL"
    ).fetchone()[0]
    assert n_sync == 0, "不应有 sync_uuid 为空的行"
    cA.close()


# ---------------------------------------------------------------------------
# 基线全量对账（草案方案 A；复现二蛋 2026-10-03 实测边界：a_only 不进 oplog）
# ---------------------------------------------------------------------------


@pytest.fixture
def divergent_pair():
    """两端基线不同：A 有 s0/s1/s2，B 只有 s1/s2（s0 是 A 独有的存量）。"""
    d = tempfile.mkdtemp(prefix="pt_sync_base_")
    ca, cb = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    cA = sqlite3.connect(ca)
    cA.execute(CREATE)
    for i in range(3):
        cA.execute(
            "INSERT INTO profile_items(id, topic, content) VALUES(?,?,?)",
            (f"s{i}", f"t{i}", f"c{i}"),
        )
    cA.commit()
    cA.close()
    cB = sqlite3.connect(cb)
    cB.execute(CREATE)
    for i in range(1, 3):  # 只 s1, s2
        cB.execute(
            "INSERT INTO profile_items(id, topic, content) VALUES(?,?,?)",
            (f"s{i}", f"t{i}", f"c{i}"),
        )
    cB.commit()
    cB.close()
    for p, node in ((ca, "mac"), (cb, "cloud")):
        c = sqlite3.connect(p)
        migrate(c, node)
        c.close()
    return ca, cb


def test_baseline_a_only_not_propagated_by_tick_alone(divergent_pair):
    """复现二蛋边界：存量 a_only 没进 oplog，纯增量 tick 不会传播它。"""
    ca, cb = divergent_pair
    n = _nodes(ca, cb)
    s0 = _uuid_of(ca, "s0")
    for _ in range(4):
        n["dA"].tick()
        n["dB"].tick()
    lb = {r.record_id for r in n["sB"].live_items()}
    assert s0 not in lb, "二蛋边界: tick 增量不应传播存量 a_only"


def test_baseline_reconcile_propagates_a_only(divergent_pair):
    """基线对账后，A 独有的 s0 应到 B。"""
    ca, cb = divergent_pair
    n = _nodes(ca, cb)
    s0 = _uuid_of(ca, "s0")
    report = reconcile_baseline(n["sA"], n["sB"], "mac")
    assert report["local_only"] == [s0], report
    assert s0 in {r.record_id for r in n["sB"].live_items()}


def test_baseline_conflict_lww(divergent_pair):
    """共存的同 uuid 冲突：按 HLC LWW，胜者写回两端。"""
    ca, cb = divergent_pair
    n = _nodes(ca, cb)
    x = new_uuid()
    h_a = HLC(1000, 0, "mac")
    h_b = HLC(2000, 0, "cloud")  # 更晚
    n["sA"].upsert(x, {"content": "A"}, h_a)
    n["sB"].upsert(x, {"content": "B"}, h_b)
    report = reconcile_baseline(n["sA"], n["sB"], "mac")
    assert x in report["conflicts"], report
    assert n["sA"].get(x).data["content"] == "B"
    assert n["sB"].get(x).data["content"] == "B"


def test_baseline_tombstone_propagates(divergent_pair):
    """A 删了 s1（HLC 晚于 migrate 基线），基线对账应把墓碑传播到 B。"""
    ca, cb = divergent_pair
    n = _nodes(ca, cb)
    s1 = _uuid_of(ca, "s1")
    # migrate 基线 HLC ≈ 2026 年 epoch ms(~1.7e12)；删除须用更晚的 HLC 才胜出
    n["sA"].delete(s1, HLC(9_999_999_999_999, 0, "mac"))
    reconcile_baseline(n["sA"], n["sB"], "mac")
    sb = n["sB"].get(s1)
    assert sb is None or sb.tombstone, "B 的 s1 应被墓碑化（防遗忘复活）"


def test_baseline_idempotent(divergent_pair):
    """基线对账可重跑：第二次不应再有缺失/冲突。"""
    ca, cb = divergent_pair
    n = _nodes(ca, cb)
    r1 = reconcile_baseline(n["sA"], n["sB"], "mac")
    r2 = reconcile_baseline(n["sA"], n["sB"], "mac")
    assert r1["local_only"], "首次应发现 a_only"
    assert (
        r2["local_only"] == [] and r2["remote_only"] == [] and r2["conflicts"] == []
    ), r2

def test_sqlite_delete_missing_record_creates_tombstone(pair):
    """回归（review 基线对账发现）：对 SQLite 从未存在过的 record 调 delete，
    必须落墓碑，与 MemoryStore.delete 行为对齐，防止陈旧副本日后重放旧 upsert。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    s = SQLiteSyncStore(c)
    missing = new_uuid()
    s.delete(missing, HLC(9_999_999_999_999, 0, "cloud"))
    row = c.execute(
        "SELECT sync_tombstone FROM profile_items WHERE sync_uuid=?", (missing,)
    ).fetchone()
    assert row is not None and row[0] == 1, "删除不存在的记录必须插入墓碑"
    c.close()

def test_sqlite_oplog_preserves_origin(pair):
    """回归：op 经 SQLite oplog 落盘再 since() 读回，origin（存于 node 列）
    不得丢失——否则真实存储上回声防护失效。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    oplog = SQLiteOplogStore(c)
    op = Op("profile_items", "x1", "upsert", HLC.now("mac"), {"v": 1}, origin="mac")
    oplog.append(op)
    back = oplog.since(0)
    assert len(back) == 1 and back[0].origin == "mac", back
    c.close()


def test_sqlite_oplog_since_carries_seq_and_cursor_uses_max(pair):
    """回归（二蛋 2026-10-03 游标缺陷）：SQLiteOplogStore.since 必须透传真实 seq，
    且 SyncDaemon 按返回 op 的【最大 seq】推进游标，而非 += len(remote_ops)。
    对端 oplog 经 GC 产生空洞/高位 seq 时，旧逻辑会反复重拉同一批、甚至漏同步。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    oplog = SQLiteOplogStore(c)
    # 模拟 GC：先写 5 条（seq 1..5），删低 seq 制造空洞，再写高位新事件
    for i in range(5):
        oplog.append(Op("profile_items", f"old{i}", "upsert", HLC(1000 + i, 0, "cloud"), {"v": i}))
    c.execute("DELETE FROM _sync_oplog WHERE seq <= 3")
    c.commit()
    oplog.append(Op("profile_items", "new1", "upsert", HLC(2000, 0, "cloud"), {"v": 99}))

    ops = oplog.since(0)
    assert ops, "since(0) 应返回剩余 oplog"
    assert all(o.seq is not None for o in ops), "since 必须透传 seq"
    assert sorted(o.seq for o in ops) == [4, 5, 6], "剩余 seq 应为 4/5/6（6=新事件）"

    class _SqlTransport(Transport):
        def pull_since(self, after_seq):
            return oplog.since(after_seq)

        def push(self, ops):
            pass

    store = MemoryStore()
    eng = SyncEngine(store, OplogStore(), "mac")
    d = SyncDaemon(eng, _SqlTransport())
    res = d.tick()
    # 关键：一轮直接到 max seq=6，而非 +=len(3)=3（旧 bug 会让游标停在低位、永久漏同步高风险）
    assert res["pulled"] == 3, res
    assert res["pull_after_seq"] == 6, res
    # 之后新事件 seq=7 立即拉到，不再重拉 4/5/6
    oplog.append(Op("profile_items", "new2", "upsert", HLC(2001, 0, "cloud"), {"v": 100}))
    res2 = d.tick()
    assert res2["pulled"] == 1, res2
    assert res2["pull_after_seq"] == 7, res2
    c.close()


def test_sqlite_oplog_append_backfills_seq():
    """二蛋 2026-10-03 一致性缺陷：SQLiteOplogStore.append 原未回填 op.seq，
    导致「append 后直接读 op.seq」出现 SQLite→None / 内存→本端seq / 远端apply→对端seq
    三态不一致。修复后 append 须回填真实 seq，与内存 OplogStore.append 行为对齐。"""
    d = tempfile.mkdtemp(prefix="pt_sync_append_seq_")
    db = os.path.join(d, "a.db")
    c = sqlite3.connect(db)
    c.execute(CREATE)
    c.commit()
    migrate(c, "mac")
    oplog = SQLiteOplogStore(c)

    op1 = Op("profile_items", new_uuid(), "upsert", HLC(1000, 0, "mac"), {"v": 1})
    assert op1.seq is None, "前置：未落库前 seq 应为 None"
    ret1 = oplog.append(op1)
    # 回填：调用方在 append 后直接读 op.seq 应得到与内存版一致的本端真实 seq
    assert op1.seq == ret1, f"append 应回填 op.seq={ret1}，实得 {op1.seq}"
    assert isinstance(op1.seq, int) and op1.seq > 0

    op2 = Op("profile_items", new_uuid(), "upsert", HLC(1001, 0, "mac"), {"v": 2})
    ret2 = oplog.append(op2)
    assert op2.seq == ret2
    assert op2.seq > op1.seq, "第二条 seq 应严格大于第一条"
    # 旧 op 对象的 seq 不应被后续 append 污染
    assert op1.seq < op2.seq
    c.close()

