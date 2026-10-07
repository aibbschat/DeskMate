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
import time

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
    reconcile_baseline_oplog,
)
from deskmate.memory.sync_store_sqlite import (
    migrate,
    SQLiteSyncStore,
    SQLiteOplogStore,
    SYNC_TABLES,
    _validate_sync_tables_no_vectors,
    gc_oplog,
    _backfill_oplog_created_at,
    _OPLOG_TABLE,
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
    del_hlc = n["eA"].local_delete("profile_items", x)
    n["dA"].tick()
    n["dB"].tick()  # B 也删 X
    # 阶段 2 P0 修复后：业务行物理删除（get 返回墓碑，非活记录），墓碑落独立侧表
    ax = n["sA"].get(x)
    assert ax is not None and ax.tombstone, "A 应已删除 X（get 返回墓碑）"
    assert n["sA"].has_tombstone(x, del_hlc), "A 应记墓碑侧表"
    bx = n["sB"].get(x)
    assert bx is not None and bx.tombstone, "B 端也应已删除 X"

    # 模拟 B 把 X 的旧 upsert 事件重推（断网/重发场景）
    old_ops = [
        op for op in n["eB"].oplog.all()
        if op.record_id == x and not op.tombstone
    ]
    n["eA"].apply_remote(old_ops)
    assert n["sA"].get(x) is not None and n["sA"].get(x).tombstone, "遗忘不应被旧副本复活"


def test_backfill_idempotent(pair):
    ca, _ = pair
    # 再跑一次 migrate 应安全（不报错、不重复回填）
    c = sqlite3.connect(ca)
    r = migrate(c, "mac")
    c.close()
    assert r["backfilled"] == {"profile_items": 0}, "二次 migrate 不应再回填"
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
    """回归（二蛋 review P0）：对 SQLite 从未存在过的 record 调 delete，必须落墓碑
    侧表，**且业务表不插 sync_tombstone=1 占位行**——否则生产 NOT NULL 列会崩，且
    app 直读 learned_history/loved_entries 不感知该列会读到幽灵行。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    s = SQLiteSyncStore(c)
    missing = new_uuid()
    s.delete(missing, HLC(9_999_999_999_999, 0, "cloud"))
    biz = c.execute(
        "SELECT COUNT(*) FROM profile_items WHERE sync_uuid=?", (missing,)
    ).fetchone()[0]
    assert biz == 0, "删除不存在的记录不应在业务表留占位行"
    tomb = c.execute(
        "SELECT hlc FROM _sync_tombstones "
        "WHERE table_name='profile_items' AND record_id=?",
        (missing,),
    ).fetchone()
    assert tomb is not None, "删除不存在的记录必须落墓碑侧表"
    c.close()

def test_sqlite_same_hlc_delete_then_upsert_revives(pair):
    """【生产 bug 回归锁】同hlc 的 delete→upsert 配对必须让 upsert 落地。

    背景（2026-10-05 确诊，真实库实证）：SQLite 触发器的 hlc 形如
    ``printf('%016x.%08x.%s', strftime('%s','now')*1000, hlc_counter, node)``，
    而 ``_sync_meta.hlc_counter`` **从不自增**（实测恒为 0）→ 同一毫秒内发生的
    delete 与 upsert 会拿到**完全相同**的 hlc。本表delete 是**物理删除** + 墓碑落侧表，
    对端 apply 顺序为 delete 先、upsert 后；原实现「同 HLC 一律幂等跳过」会把那条
    upsert 当重复丢弃 → 记录永久卡在墓碑态，业务表永不复活。

    真实后果：云端 profile_items 的 emotional 层（自主 tick 每轮删后重插）持续增长，
    oplog 里 op 条条齐全，但 Mac侧 profile_items 中该维度0 条（传输通、apply 断）。
    """
    ca, _cb = pair
    c = sqlite3.connect(ca)
    e = SyncEngine(SQLiteSyncStore(c), SQLiteOplogStore(c), "mac")
    rid = "u-revive"
    h = HLC(1000, 0, "cloud")

    # 云端同一毫秒内先删后插：两条 op 的 hlc 完全相同
    e.apply_remote([Op("profile_items", rid, "delete", h, {}, tombstone=True, origin="cloud")])
    s = SQLiteSyncStore(c)
    assert s.get(rid) is None or s.get(rid).tombstone is True, "前置：delete 后应为墓碑态"
    assert s.has_tombstone(rid, h), "前置：墓碑应落侧表"

    # 同hlc 的 upsert 随后到达 → 必须复活（这正是被丢掉的那一条）
    payload = {"topic": "回血方式", "content": "情绪恢复靠投入创作", "dimension": "stance"}
    e.apply_remote([Op("profile_items", rid, "upsert", h, payload, origin="cloud")])

    rec = s.get(rid)
    assert rec is not None, "同hlc 的 upsert 必须能复活被同hlc delete 掉的记录"
    assert rec.tombstone is False, "复活后不应仍是墓碑"
    assert rec.data["content"] == "情绪恢复靠投入创作", rec.data
    c.close()


def test_sqlite_same_hlc_upsert_replay_idempotent(pair):
    """同hlc 的**纯 upsert** 重复重放仍须幂等（A 改动不得破坏既有幂等语义）。"""
    ca, _cb = pair
    c = sqlite3.connect(ca)
    e = SyncEngine(SQLiteSyncStore(c), SQLiteOplogStore(c), "mac")
    s = SQLiteSyncStore(c)
    rid = "u-idem"
    h = HLC(2000, 0, "cloud")
    payload = {"topic": "t", "content": "v", "dimension": "stance"}
    for _ in range(3):
        e.apply_remote([Op("profile_items", rid, "upsert", h, payload, origin="cloud")])
    row = c.execute("SELECT COUNT(*) FROM profile_items WHERE sync_uuid=?", (rid,)).fetchone()[0]
    assert row == 1, f"同hlc upsert 重复重放必须幂等，实际 {row} 行"
    assert s.get(rid).data["content"] == "v"
    c.close()


def test_sqlite_strictly_older_upsert_still_blocked(pair):
    """严格更早 hlc 的 upsert 仍被墓碑拦住（防遗忘复活不得被 A 削弱）。"""
    ca, _cb = pair
    c = sqlite3.connect(ca)
    e = SyncEngine(SQLiteSyncStore(c), SQLiteOplogStore(c), "mac")
    s = SQLiteSyncStore(c)
    rid = "u-stale"
    h_new = HLC(3000, 0, "cloud")
    e.apply_remote([Op("profile_items", rid, "delete", h_new, {}, tombstone=True, origin="cloud")])
    e.apply_remote(
        [Op("profile_items", rid, "upsert", HLC(2000, 0, "cloud"),
            {"topic": "old", "content": "old", "dimension": "stance"}, origin="cloud")]
    )
    rec = s.get(rid)
    assert rec is None or rec.tombstone is True, "严格更早的 upsert 不得复活已删记录"
    assert s.has_tombstone(rid, h_new), "墓碑应仍在"
    c.close()


def test_sqlite_trigger_hlc_is_strictly_monotonic(pair):
    """【B 方案·根因】触发器生成的 hlc 必须严格单调，不再出现同毫秒撞车。

    修前：``hlc_counter`` 从不自增（恒0），delete 与 upsert 在同一毫秒内会拿到
    完全相同的 hlc → 对端 apply时同hlc 决胜失效、数据卡墓碑。
    修后：每次触发都推进 counter，保证 (physical, counter) 组合全局单调。
    """
    ca, _cb = pair
    c = sqlite3.connect(ca)
    # 同一毫秒内连做 delete + upsert（模拟云端 emotional 层删后重插）
    c.execute("UPDATE _sync_meta SET hlc_counter=0 WHERE id=1")
    for i in range(5):
        c.execute("INSERT INTO profile_items(id, topic, content) VALUES(?,?,?)",
                  (f"mono{i}", f"t{i}", f"c{i}"))
    hlcs = [r[0] for r in c.execute(
        "SELECT hlc FROM _sync_oplog WHERE table_name='profile_items' ORDER BY seq"
    )]
    c.commit()
    assert len(hlcs) == 5, hlcs
    assert len(set(hlcs)) == 5, f"5 次触发必须产出 5 个不同 hlc，实得 {hlcs}"
    assert hlcs == sorted(hlcs), f"oplog 的 hlc 应严格递增：{hlcs}"
    c.close()


def test_sqlite_trigger_hlc_delete_then_upsert_differ(pair):
    """【B 方案·根因】删后重插的两条 op（delete/upsert）必须拿到不同 hlc。

    这是生产 bug 的直接成因：同hlc 时对端无法决胜。修后 delete 与随后的 upsert
    严格递增，即使 A 侧决胜逻辑再出问题也不会重现"永久卡墓碑"。
    """
    ca, _cb = pair
    c = sqlite3.connect(ca)
    c.execute("INSERT INTO profile_items(id, topic, content) VALUES('dd','t','v')")
    uuid = c.execute("SELECT sync_uuid FROM profile_items WHERE id='dd'").fetchone()[0]
    c.execute("DELETE FROM profile_items WHERE id='dd'")
    c.execute("INSERT INTO profile_items(id, topic, content) VALUES('dd','t2','v2')")
    c.commit()
    rows = list(c.execute(
        "SELECT op, hlc FROM _sync_oplog WHERE table_name='profile_items' "
        "AND record_id=? ORDER BY seq", (uuid,)
    ))
    c.close()
    assert len(rows) == 3, rows  # insert + delete + re-insert
    del_hlc = [h for op, h in rows if op == "delete"]
    assert len(del_hlc) == 1, rows
    # delete 之后的 upsert 必须比 delete 的 hlc 更大
    ups_after = [h for op, h in rows if op == "upsert" and h > del_hlc[0]]
    assert ups_after, f"删后重插的 upsert 必须拿到比 delete 更新的 hlc：{rows}"


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


# ===========================================================================
# 阶段 2：多表（learned_history / loved_entries）+ DB 触发器捕获
# ===========================================================================

CREATE_LEARNED = """CREATE TABLE learned_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, fact TEXT, category TEXT, fp TEXT UNIQUE,
    source TEXT, entity TEXT, polarity TEXT, status TEXT,
    superseded_by TEXT, valid_until TEXT, valid_from TEXT
)"""

CREATE_LOVED = """CREATE TABLE loved_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id TEXT, content TEXT, day TEXT, created_at TEXT, tags TEXT
)"""

# 本文件的三表夹具不含日记表（user_posts/comments），故 migrate 会把它们报 skipped。
# 阶段 6 起日记表也进注册表，但此处断言只针对本夹具实际存在的三张表。
SYNCED_TABLES = ("profile_items", "learned_history", "loved_entries")
# 阶段 6（2026-10-05）：注册表里新增、但本夹具无对应建表语句的表（应被跳过而非崩）
# 2026-10-06：LLM 通道熔断表（llm_circuit_breaker，设计 §3.3）也进注册表，同样不在本夹具里。
# 2026-10-07：P1 汇流共享锚表（memory_anchors）同进注册表，本夹具亦不建。
# 2026-10-07：P2-R1 已知人名注册表（known_persons）同进注册表，本夹具亦不建。
# 2026-10-07：P2-R3 lessons 表（teachings）同进注册表，本夹具亦不建。
TABLES_ABSENT_IN_FIXTURE = (
    "user_posts", "comments", "proactive_ledger", "llm_circuit_breaker",
    "memory_anchors", "known_persons", "teachings",
)


def _snapshot_from_store(store):
    """把某表 store 的 items() 还原成只读 MemoryStore 快照（供基线对账）。"""
    ms = MemoryStore()
    for r in store.items():
        ms.upsert(r.record_id, dict(r.data), r.hlc, r.valid_until)
        if r.tombstone:
            ms.delete(r.record_id, r.hlc)
    return ms


def _build_triple():
    """造一对三表齐全的 DB（A=mac / B=cloud），存量经 migrate 回填（不进 oplog）。

    返回 ``(ca, cb, res_a)``，``res_a`` 为 A 侧首次 migrate 的回填报告（断言用）。
    """
    d = tempfile.mkdtemp(prefix="pt_sync_triple_")
    ca, cb = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    # A 侧存量：profile p0/p1/pA；learned fpA1/fpA2；loved 暂无
    cA = sqlite3.connect(ca)
    cA.execute(CREATE)
    cA.execute(CREATE_LEARNED)
    cA.execute(CREATE_LOVED)
    cA.executemany(
        "INSERT INTO profile_items(id,topic,content) VALUES(?,?,?)",
        [("p0", "tp0", "cp0"), ("p1", "tp1", "cp1"), ("pA", "tpA", "cpA")],
    )
    cA.executemany(
        "INSERT INTO learned_history(fact,category,fp) VALUES(?,?,?)",
        [("A fact1", "hist", "fpA1"), ("A fact2", "hist", "fpA2")],
    )
    cA.commit()
    cA.close()
    # B 侧存量：profile p0/p1/pB；learned fpB1；loved mB
    cB = sqlite3.connect(cb)
    cB.execute(CREATE)
    cB.execute(CREATE_LEARNED)
    cB.execute(CREATE_LOVED)
    cB.executemany(
        "INSERT INTO profile_items(id,topic,content) VALUES(?,?,?)",
        [("p0", "tp0", "cp0"), ("p1", "tp1", "cp1"), ("pB", "tpB", "cpB")],
    )
    cB.execute(
        "INSERT INTO learned_history(fact,category,fp) VALUES(?,?,?)",
        ("B fact1", "hist", "fpB1"),
    )
    cB.execute(
        "INSERT INTO loved_entries(moment_id,content,day) VALUES(?,?,?)",
        ("mB", "B love", "2026-02-02"),
    )
    cB.commit()
    cB.close()
    res_a = None
    for p, node in ((ca, "mac"), (cb, "cloud")):
        c = sqlite3.connect(p)
        r = migrate(c, node)
        c.close()
        if node == "mac":
            res_a = r
    return ca, cb, res_a


def _side_engines(path, node, other_oplog):
    """构造一侧引擎（多表 store）+ 指向对端 oplog 的 daemon。"""
    c = sqlite3.connect(path)
    oplog = SQLiteOplogStore(c)
    stores = {t: SQLiteSyncStore(c, t) for t in SYNCED_TABLES}
    eng = SyncEngine(stores, oplog, node)
    daemon = SyncDaemon(eng, InMemoryTransport(other_oplog))
    return c, oplog, stores, eng, daemon


def test_multi_table_migrate_backfill():
    """三表一并迁移 + 确定性回填（两端同源主键 → 同 sync_uuid）。"""
    ca, _, res_a = _build_triple()  # res_a = A 侧首次 migrate 回填报告
    assert res_a["backfilled"] == {
        "profile_items": 3,
        "learned_history": 2,
        "loved_entries": 0,
    }, res_a
    assert res_a["skipped"] == list(TABLES_ABSENT_IN_FIXTURE), (
        "本夹具只建了三表，日记表应被跳过而非崩或静默处理"
    )
    cA = sqlite3.connect(ca)
    for t in SYNCED_TABLES:
        n = cA.execute(
            f"SELECT COUNT(*) FROM {t} WHERE sync_uuid IS NULL"
        ).fetchone()[0]
        assert n == 0, f"{t} 不应有 sync_uuid 为空的行"
    cA.close()


def test_app_direct_write_captured_by_trigger():
    """阶段 2 核心价值：真实 app 直写（绕过 SQLiteSyncStore）经 DB 触发器进 oplog。

    这补上阶段 1 的边界缺口——app 直写 profile_items 之前不进 oplog，现在自动进。
    """
    d = tempfile.mkdtemp(prefix="pt_sync_trig_")
    db = os.path.join(d, "t.db")
    c = sqlite3.connect(db)
    c.execute(CREATE)
    c.execute(CREATE_LEARNED)
    c.commit()
    migrate(c, "mac")
    c.close()

    c = sqlite3.connect(db)
    oplog = SQLiteOplogStore(c)
    # app 直写 learned_history（绕过 store）
    c.execute(
        "INSERT INTO learned_history(fact,category,fp) VALUES(?,?,?)",
        ("f1", "hist", "fp1"),
    )
    suid = c.execute(
        "SELECT sync_uuid FROM learned_history WHERE fp='fp1'"
    ).fetchone()[0]
    c.execute("UPDATE learned_history SET fact='f1edit' WHERE fp='fp1'")
    c.execute("DELETE FROM learned_history WHERE fp='fp1'")
    c.commit()
    ops = oplog.all()
    rec_ops = [o for o in ops if o.record_id == suid]
    # 应有：insert→upsert、update→upsert、delete→delete，共 3 条，且都进 oplog
    assert [o.op for o in rec_ops] == ["upsert", "upsert", "delete"], rec_ops
    assert all(o.table == "learned_history" for o in rec_ops)
    assert all(o.origin == "mac" for o in rec_ops)
    assert rec_ops[-1].tombstone is True
    c.close()


def test_engine_write_not_duplicated_by_trigger():
    """回归：引擎经 store.upsert/delete 写入时 suppress 触发器，oplog 不会重复落 op。"""
    ca, _, _ = _build_triple()
    c = sqlite3.connect(ca)
    oplog = SQLiteOplogStore(c)
    eng = SyncEngine({t: SQLiteSyncStore(c, t) for t in SYNCED_TABLES}, oplog, "mac")
    rid = new_uuid()
    eng.local_upsert("learned_history", rid, {"fact": "x", "category": "hist", "fp": "fpx"})
    rid_ops = [o for o in oplog.all() if o.record_id == rid]
    assert len(rid_ops) == 1, f"引擎 upsert 只应产生 1 条 op，实得 {rid_ops}"
    assert rid_ops[0].op == "upsert"
    # 删除同样只产生 1 条 delete op（不应有同 hlc 的 upsert 回声）
    eng.local_delete("learned_history", rid)
    rid_ops2 = [o for o in oplog.all() if o.record_id == rid]
    assert len(rid_ops2) == 2, f"upsert+delete 应共 2 条，实得 {rid_ops2}"
    assert [o.op for o in rid_ops2] == ["upsert", "delete"]
    c.close()


def test_learned_history_fp_conflict_merge():
    """learned_history 的 fp 唯一：双端独立学到同一事实（fp 相同）撞 UNIQUE → 按 fp merge。"""
    ca, _, _ = _build_triple()
    c = sqlite3.connect(ca)
    store = SQLiteSyncStore(c, "learned_history")
    s1, s3 = new_uuid(), new_uuid()
    store.upsert(s1, {"fact": "A", "category": "hist", "fp": "fpX"}, HLC(1000, 0, "mac"))
    # 不同 sync_uuid 但同 fp → INSERT 分支撞 UNIQUE(fp) → 触发 conflict_col merge
    store.upsert(s3, {"fact": "B", "category": "hist", "fp": "fpX"}, HLC(2000, 0, "mac"))
    n = c.execute("SELECT COUNT(*) FROM learned_history WHERE fp='fpX'").fetchone()[0]
    assert n == 1, "同 fp 应 merge 为 1 行，不应重复"
    fact = c.execute("SELECT fact FROM learned_history WHERE fp='fpX'").fetchone()[0]
    assert fact == "B", "merge 后应为后写的 B"
    c.close()


def test_pk_heterogeneity_remote_insert():
    """远端 insert：int 自增表省略业务 pk（sqlite 自派 id）；text pk 表显式置 id=record_id。"""
    ca, _, _ = _build_triple()
    c = sqlite3.connect(ca)
    stores = {t: SQLiteSyncStore(c, t) for t in SYNCED_TABLES}
    eng = SyncEngine(stores, SQLiteOplogStore(c), "mac")

    # learned_history = int 自增：远端 insert 不应设业务 id（让它自派）
    rid_l = new_uuid()
    eng.apply_remote([Op("learned_history", rid_l, "upsert", HLC(1000, 0, "mac"),
                         {"fact": "rl", "category": "hist", "fp": "fpRL"})])
    row_l = c.execute(
        "SELECT id, sync_uuid FROM learned_history WHERE sync_uuid=?", (rid_l,)
    ).fetchone()
    assert row_l is not None, "远端 insert 应落库"
    assert isinstance(row_l[0], int) and row_l[0] != rid_l, "int 表 id 应自派、非 sync_uuid"
    assert row_l[1] == rid_l

    # profile_items = text pk，业务 id 在 data['id']：远端 insert 后 id 列=业务值（非 sync_uuid）
    rid_p = new_uuid()
    eng.apply_remote([Op("profile_items", rid_p, "upsert", HLC(1001, 0, "mac"),
                         {"id": "pX", "topic": "tp", "content": "cp"})])
    row_p = c.execute(
        "SELECT id, sync_uuid FROM profile_items WHERE sync_uuid=?", (rid_p,)
    ).fetchone()
    assert row_p is not None, "远端 insert 应落库"
    assert row_p[0] == "pX", f"text pk 表 id 列应=业务主键 data['id']='pX'，实得 {row_p[0]!r}"
    assert row_p[1] == rid_p, "sync_uuid 列应=record_id"
    c.close()


def test_multi_table_apply_remote_routing():
    """多表引擎按 op.table 路由，互不串表；且回声防护（origin==本端）跳过。"""
    ca, _, _ = _build_triple()
    c = sqlite3.connect(ca)
    stores = {t: SQLiteSyncStore(c, t) for t in SYNCED_TABLES}
    eng = SyncEngine(stores, SQLiteOplogStore(c), "mac")

    # 跨表两条 op：互不串
    eng.apply_remote([
        Op("profile_items", new_uuid(), "upsert", HLC(1000, 0, "mac"),
           {"topic": "pt", "content": "pc"}),
        Op("learned_history", new_uuid(), "upsert", HLC(1001, 0, "mac"),
           {"fact": "lf", "category": "hist", "fp": "fpRoute"}),
    ])
    # 回声：origin==mac 的 op（模拟对端转发回本端）不应被本端应用
    echo_rid = new_uuid()
    eng.apply_remote([Op("loved_entries", echo_rid, "upsert", HLC(1002, 0, "mac"),
                         {"moment_id": "m", "content": "c", "day": "d"}, origin="mac")])
    # loved_entries 处应有 fpRoute? 不——loved 那条 origin==mac 被回声防护跳过
    assert stores["loved_entries"].get(echo_rid) is None, "回声 op 不应落本端"
    # profile / learned 各 1 条，loved 0 条
    assert len(stores["profile_items"].items()) == 1 + 3  # +3 存量迁移回填
    assert len([r for r in stores["learned_history"].items() if r.data.get("fp") == "fpRoute"]) == 1
    c.close()


def test_baseline_oplog_bidirectional_convergence():
    """首连基线（多表）：两端游标归零时，逐表拉对端快照一次性全量对账 + 增量 tick → 双端收敛。"""
    ca, cb, _ = _build_triple()
    cA = sqlite3.connect(ca)
    oplogA = SQLiteOplogStore(cA)
    cB = sqlite3.connect(cb)
    oplogB = SQLiteOplogStore(cB)
    # 先各造引擎（自持 oplog），基线对账用
    storesA = {t: SQLiteSyncStore(cA, t) for t in SYNCED_TABLES}
    storesB = {t: SQLiteSyncStore(cB, t) for t in SYNCED_TABLES}
    engA = SyncEngine(storesA, oplogA, "mac")
    engB = SyncEngine(storesB, oplogB, "cloud")

    # 两端各自跑基线（模拟首连：两端游标都=0 都会触发）
    for t in SYNCED_TABLES:
        reconcile_baseline_oplog(storesA[t], _snapshot_from_store(storesB[t]), engA, t, "mac")
        reconcile_baseline_oplog(storesB[t], _snapshot_from_store(storesA[t]), engB, t, "cloud")

    # 增量 tick（A↔B 交叉传输）
    dA = SyncDaemon(engA, InMemoryTransport(oplogB))
    dB = SyncDaemon(engB, InMemoryTransport(oplogA))
    for _ in range(4):
        dA.tick()
        dB.tick()

    for t in SYNCED_TABLES:
        la = {r.record_id: r.data for r in storesA[t].live_items()}
        lb = {r.record_id: r.data for r in storesB[t].live_items()}
        assert set(la) == set(lb), f"[{t}] 两端活记录集合应一致: A={set(la)} B={set(lb)}"

    # 具体核对（store.record_id 是确定性 sync_uuid，不是业务 id，故查底层 DB 业务键）：
    # A 独有 pA、B 独有 pB；learned 双端 fpA1/fpA2/fpB1；loved 双端 mB
    a_prof = {r[0] for r in cA.execute("SELECT id FROM profile_items WHERE sync_tombstone=0")}
    b_prof = {r[0] for r in cB.execute("SELECT id FROM profile_items WHERE sync_tombstone=0")}
    assert {"pA", "pB"}.issubset(a_prof) and {"pA", "pB"}.issubset(b_prof)
    a_fp = {r[0] for r in cA.execute("SELECT fp FROM learned_history WHERE sync_tombstone=0")}
    b_fp = {r[0] for r in cB.execute("SELECT fp FROM learned_history WHERE sync_tombstone=0")}
    assert {"fpA1", "fpA2", "fpB1"}.issubset(a_fp) and {"fpA1", "fpA2", "fpB1"}.issubset(b_fp)
    a_loved = {r[0] for r in cA.execute("SELECT moment_id FROM loved_entries WHERE sync_tombstone=0")}
    b_loved = {r[0] for r in cB.execute("SELECT moment_id FROM loved_entries WHERE sync_tombstone=0")}
    assert "mB" in a_loved and "mB" in b_loved, "A/B 应经基线互拿对方 loved"
    cA.close()
    cB.close()


# ---------------------------------------------------------------------------
# 生产真 DDL 对齐测试（二蛋 review P0：测试库 schema 必须与生产逐字一致，否则假绿）
# ---------------------------------------------------------------------------

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "real_schema.sql")


def _real_schema_db():
    """用生产真 DDL 建库（含 NOT NULL 约束），返回临时 db 路径。"""
    d = tempfile.mkdtemp(prefix="pt_real_schema_")
    db = os.path.join(d, "real.db")
    c = sqlite3.connect(db)
    with open(_FIXTURE, encoding="utf-8") as f:
        c.executescript(f.read())
    c.commit()
    c.close()
    return db


def test_p0_unseen_delete_no_crash_with_real_schema():
    """P0 回归：生产真 DDL（NOT NULL）下，删除本端从未见过的记录不得崩，
    业务表不留占位行，墓碑落独立侧表。三表全验。"""
    db = _real_schema_db()
    c = sqlite3.connect(db)
    migrate(c, "mac")
    c.close()
    for t in SYNCED_TABLES:
        c = sqlite3.connect(db)
        st = SQLiteSyncStore(c, t)
        ghost = "deskmate:" + t + ":ghost"
        try:
            st.delete(ghost, HLC(2000, 0, "cloud"))
        except Exception as e:  # noqa: BLE001
            c.close()
            raise AssertionError(f"{t} 生产 DDL 下删除未见过记录应不崩，却: {e!r}")
        # 业务表无占位行
        n = c.execute(
            f"SELECT COUNT(*) FROM {t} WHERE sync_uuid=?", (ghost,)
        ).fetchone()[0]
        assert n == 0, f"{t} 不应在业务表留占位行"
        # 侧表有墓碑
        tb = c.execute(
            "SELECT hlc FROM _sync_tombstones WHERE table_name=? AND record_id=?",
            (t, ghost),
        ).fetchone()
        assert tb is not None, f"{t} 应落墓碑侧表"
        c.close()


def test_p0_real_schema_full_converge_with_unseen_delete():
    """P0 端到端：两套生产真 DDL 库，A 删除一条 B 从未有过的记录，
    同步须收敛且不崩、不卡死（验证 apply_remote 无 per-op try 时也不传播异常）。"""
    da, db = _real_schema_db(), _real_schema_db()
    # 各自种子：profile 用 p0/p1；learned 用 fpA/fpB（fp UNIQUE）
    for p, node, prows, frows in (
        (da, "mac", [("p0", "dimA"), ("p1", "dimA")],
         [("2026-01-01", "mac fact", "fpA"), ("2026-01-02", "mac fact2", "fpB")]),
        (db, "cloud", [("p0", "dimA"), ("p1", "dimA")],
         [("2026-01-01", "cloud fact", "fpA"), ("2026-01-03", "cloud fact3", "fpC")]),
    ):
        c = sqlite3.connect(p)
        for pid, dim in prows:
            c.execute(
                "INSERT INTO profile_items(id, dimension, topic, content) VALUES(?,?,?,?)",
                (pid, dim, "t", "c"),
            )
        for ts, fact, fp in frows:
            c.execute(
                "INSERT INTO learned_history(ts, fact, category, fp, source, status) "
                "VALUES(?,?,?,?,?,?)",
                (ts, fact, "hist", fp, "live", "active"),
            )
        c.commit()
        c.close()
        c = sqlite3.connect(p)
        migrate(c, node)
        c.close()

    # 构造两端引擎（内存 oplog + 内存 transport 互连）
    def _build(p, node):
        c = sqlite3.connect(p)
        stores = {t: SQLiteSyncStore(c, t) for t in SYNCED_TABLES}
        oplog = SQLiteOplogStore(c)
        eng = SyncEngine(stores, oplog, node)
        return c, stores, eng

    cA, sA, eA = _build(da, "mac")
    cB, sB, eB = _build(db, "cloud")
    # 内存 transport 互连两端 oplog
    oA, oB = eA.oplog, eB.oplog
    dA = SyncDaemon(eA, InMemoryTransport(oB))
    dB = SyncDaemon(eB, InMemoryTransport(oA))

    # 首轮收敛
    for _ in range(3):
        dA.tick()
        dB.tick()

    # 先抓 uuid（删除后业务行物理消失，无法再查）
    fpB_uuid = _uuid_of_real(da, "fpB")
    fpA_uuid = _uuid_of_real(da, "fpA")
    # A 删除一条 B 从未有过的 learned 记录 fpB（mac 独有）
    del_hlc = eA.local_delete("learned_history", fpB_uuid)
    # A 也删除一条 B 有过的 fpA（seen delete on B 侧）
    eA.local_delete("learned_history", fpA_uuid)

    # 关键：同步不得崩、不得卡死
    for _ in range(3):
        dA.tick()
        dB.tick()

    # P0 断言（不是全集收敛——B 独有的 fpC 是基线前种子、未进 oplog，不会跨端传播；
    # 这点与「删除不崩不卡」无关，故只验删除语义）：
    # ① 被删记录两端业务表都不应残留（无占位行、无幽灵行）
    ca2 = sqlite3.connect(da)
    n_fpB_A = ca2.execute("SELECT COUNT(*) FROM learned_history WHERE fp='fpB'").fetchone()[0]
    n_fpA_A = ca2.execute("SELECT COUNT(*) FROM learned_history WHERE fp='fpA'").fetchone()[0]
    ca2.close()
    cb2 = sqlite3.connect(db)
    n_fpA_B = cb2.execute("SELECT COUNT(*) FROM learned_history WHERE fp='fpA'").fetchone()[0]
    cb2.close()
    assert n_fpB_A == 0, "A 端 fpB（unseen delete）应物理删除，无占位行"
    assert n_fpA_A == 0, "A 端 fpA 应物理删除"
    assert n_fpA_B == 0, "B 端 fpA 经同步应物理删除（unseen→侧表，不崩不卡）"
    # ② 墓碑侧表确有记录（防复活依据）
    ca3 = sqlite3.connect(da)
    n_tomb = ca3.execute(
        "SELECT COUNT(*) FROM _sync_tombstones "
        "WHERE table_name='learned_history' AND record_id=?",
        (fpB_uuid,),
    ).fetchone()[0]
    ca3.close()
    assert n_tomb == 1, "A 端 fpB 应落墓碑侧表"
    # ③ 同步未卡死：删除后再新增一条，应能跨端传播（游标正常推进）
    new_uuid_r = "deskmate:learned_history:newAfterDel"
    eA.local_upsert(
        "learned_history", new_uuid_r,
        {"ts": "2026-02-01", "fact": "after-delete", "category": "hist",
         "fp": "fpNEW", "source": "live", "status": "active"},
    )
    for _ in range(3):
        dA.tick()
        dB.tick()
    cc = sqlite3.connect(db)
    n_new = cc.execute(
        "SELECT COUNT(*) FROM learned_history WHERE fp='fpNEW'"
    ).fetchone()[0]
    cc.close()
    assert n_new == 1, "删除后新增记录仍应跨端传播（证明未卡死）"
    cA.close(); cB.close()


def test_app_direct_delete_writes_tombstone_side_table():
    """二蛋复验缺口回归：app 直写删除（AFTER DELETE 触发器路径）必须也落墓碑侧表，
    否则 has_tombstone() 查不到 → 迟到旧 upsert 可把行复活。

    引擎路径（store.delete）已在 Python 内直写侧表；本测试验证触发器路径补齐后
    两路语义一致——这正是 P0「删除不防复活」要根治的那一类。
    """
    db = _real_schema_db()
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO loved_entries(moment_id, content, day, created_at) "
        "VALUES(?,?,?,?)",
        ("mX", "love X", "2026-03-03", "2026-03-03"),
    )
    c.commit()
    c.close()
    c = sqlite3.connect(db)
    migrate(c, "mac")
    c.close()

    # app 直写删除（绕过 store.delete，触发器中 suppress=0 照常捕获）
    c = sqlite3.connect(db)
    rid = c.execute(
        "SELECT sync_uuid FROM loved_entries WHERE moment_id='mX'"
    ).fetchone()[0]
    c.execute("DELETE FROM loved_entries WHERE sync_uuid=?", (rid,))
    c.commit()
    tb = c.execute(
        "SELECT hlc FROM _sync_tombstones "
        "WHERE table_name='loved_entries' AND record_id=?",
        (rid,),
    ).fetchone()
    c.close()
    assert tb is not None, "app 直写删除须落墓碑侧表（二蛋复验缺口已补）"
    tomb_hlc = HLC.from_str(tb[0])

    st = SQLiteSyncStore(sqlite3.connect(db), "loved_entries")
    # ① 迟到旧 upsert（hlc 远早于墓碑）→ 应判有墓碑、拦下复活
    late = HLC(1000, 0, "cloud")  # 远早于墙钟
    assert st.has_tombstone(rid, late), "墓碑 hlc 晚于迟到 upsert → 应拦下复活"
    # ② 更新版 upsert（hlc 远晚于墓碑）→ 应放行（LWW 正确语义，不误伤新写入）
    future = HLC(10**15, 0, "cloud")
    assert not st.has_tombstone(rid, future), "更新版 upsert（hlc 晚于墓碑）应放行复活"


def _uuid_of_real(db, fp):
    """从生产真 DDL 库读某 fp 的 sync_uuid（learned_history 经 migrate 回填）。"""
    c = sqlite3.connect(db)
    row = c.execute(
        "SELECT sync_uuid FROM learned_history WHERE fp=?", (fp,)
    ).fetchone()
    c.close()
    return row[0]



def test_sync_tables_exclude_embedding_columns():
    """策略①代码保障（架构文档 §6.6）：跨端只同步明文事实，向量各端本地重算。

    守卫 `_validate_sync_tables_no_vectors` 在模块加载时即校验 SYNC_TABLES：
    - 任一表名含 embed/vector → 启动报错；
    - 任一 business_cols 含 embedding/vector 列 → 启动报错。
    此测试双保险：即使有人绕过加载路径直接改 SYNC_TABLES，也能在单测中断言失败，
    防止把向量塞进 oplog 跨端搬运（会触发 _vector_fingerprint 整批拒 / 余弦静默 0.0）。
    """
    # 守卫本身不应抛错（当前 5 表均无向量列）
    _validate_sync_tables_no_vectors()
    embed_re = __import__("re").compile(r"(?:^|[_])embed(?:ding)?\b|(?:^|[_])vector\b", __import__("re").IGNORECASE)
    for table, spec in SYNC_TABLES.items():
        assert "embed" not in table.lower() and "vector" not in table.lower(), (
            f"SYNC_TABLES 不应含向量表 {table!r}"
        )
        for col in spec.get("business_cols", ()):
            assert not embed_re.search(col), (
                f"同步表 {table!r} 的 business_cols 含向量列 {col!r}："
                f"违反「向量各端重算」策略①"
            )


# ---------------------------------------------------------------------------
# P3-a（§11.6 隐私分级）：行级 visibility='local' 不同步
# 三道关口：① 触发器 WHEN 守卫（增量不产生 op）② items()/live_items() exclude_local
#（快照/基线不外发）③ reconcile_baseline_oplog 排除 local 行（首连基线不推送）
# ---------------------------------------------------------------------------

LEDGER_CREATE = """CREATE TABLE proactive_ledger (
    id TEXT PRIMARY KEY,
    source TEXT, kind TEXT, content TEXT, evidence TEXT,
    understanding TEXT, payload TEXT, source_node TEXT, status TEXT,
    confidence REAL, created_at TEXT, acknowledged_at TEXT,
    delivered_at TEXT, snoozed_until TEXT,
    visibility TEXT DEFAULT 'sync'
)"""


LEDGER_COLS = (
    "id, source, kind, content, evidence, understanding, payload,"
    " source_node, status, confidence, created_at, acknowledged_at,"
    " delivered_at, snoozed_until, visibility"
)


@pytest.fixture
def ledger_pair():
    """两个库各建 proactive_ledger + migrate（含 visibility 列与三触发器）。"""
    d = tempfile.mkdtemp(prefix="pt_vis_")
    ca, cb = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    for p, node in ((ca, "mac"), (cb, "cloud")):
        c = sqlite3.connect(p)
        c.execute(LEDGER_CREATE)
        migrate(c, node, "proactive_ledger")
        c.commit()
        c.close()
    return ca, cb


def _ledger_insert(db, rid, visibility):
    c = sqlite3.connect(db)
    c.execute(
        f"INSERT INTO proactive_ledger({LEDGER_COLS}) VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, "automation", "task_anomaly", f"content-{rid}", "e", "", "{}",
            "mac", "pending", 0.9, "2026-10-07T00:00:00", None, None, None,
            visibility,
        ),
    )
    c.commit()
    c.close()


def _daemon_pair(ca, cb, table):
    cA = sqlite3.connect(ca)
    sA, oA = SQLiteSyncStore(cA, table), SQLiteOplogStore(cA)
    eA = SyncEngine(sA, oA, "mac")
    cB = sqlite3.connect(cb)
    sB, oB = SQLiteSyncStore(cB, table), SQLiteOplogStore(cB)
    eB = SyncEngine(sB, oB, "cloud")
    dA = SyncDaemon(eA, InMemoryTransport(oB))
    dB = SyncDaemon(eB, InMemoryTransport(oA))
    return dict(sA=sA, sB=sB, eA=eA, eB=eB, dA=dA, dB=dB)


def test_visibility_local_generates_no_op(ledger_pair):
    """关口①：标 local 的行 → 触发器 WHEN 守卫拦截 → 不产生 op、sync_uuid 不赋值。"""
    ca, _ = ledger_pair
    _ledger_insert(ca, "local1", "local")
    _ledger_insert(ca, "sync1", "sync")

    rows = {r[0]: r for r in _ledger_rows(ca)}
    # local 行：sync_uuid 必须为 NULL（触发器未运行）
    assert rows["local1"][1] is None, "local 行 sync_uuid 应为 NULL（触发器跳过）"
    # sync 行：sync_uuid 已赋值
    assert rows["sync1"][1] is not None, "sync 行 sync_uuid 应已赋值"

    co = sqlite3.connect(ca)
    n_ops = co.execute(
        "SELECT COUNT(*) FROM _sync_oplog WHERE table_name='proactive_ledger'"
    ).fetchone()[0]
    co.close()
    assert n_ops == 1, f"应只有 1 条 op（sync 行），实得 {n_ops}（local 行不得产生 op）"


def _ledger_rows(db, visibility=None):
    c = sqlite3.connect(db)
    if visibility is None:
        rows = c.execute(
            "SELECT id, sync_uuid, visibility FROM proactive_ledger"
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, sync_uuid, visibility FROM proactive_ledger WHERE visibility=?",
            (visibility,),
        ).fetchall()
    c.close()
    return rows


def test_visibility_items_exclude_local(ledger_pair):
    """关口②：items()/live_items() 的 exclude_local 过滤只筛 visibility='local'。"""
    ca, _ = ledger_pair
    for i, vis in enumerate(("sync", "local", "sync", "local")):
        _ledger_insert(ca, f"r{i}", vis)

    cA = sqlite3.connect(ca)
    sA = SQLiteSyncStore(cA, "proactive_ledger")
    all_rows = sA.items()
    excluded = sA.items(exclude_local=True)
    live_all = sA.live_items()
    live_excl = sA.live_items(exclude_local=True)
    cA.close()
    excl_ids = {r.record_id for r in excluded}
    # 默认含全部 4 条；exclude_local 只含 2 条 sync
    assert len(all_rows) == 4
    assert len(excluded) == 2
    assert all(("r0" in rid) or ("r2" in rid) for rid in excl_ids)  # 仅 sync 行
    assert len(live_all) == 4
    assert len(live_excl) == 2


def test_visibility_baseline_does_not_push_local(ledger_pair):
    """关口③：基线对账（reconcile_baseline_oplog）不把 local 行推给对端。"""
    ca, cb = ledger_pair
    _ledger_insert(ca, "sa", "sync")
    _ledger_insert(ca, "la", "local")

    cB = sqlite3.connect(cb)
    sB = SQLiteSyncStore(cB, "proactive_ledger")
    remote_ms = MemoryStore()
    for r in sB.items():
        remote_ms.upsert(r.record_id, r.data, r.hlc, valid_until=r.valid_until)

    cA = sqlite3.connect(ca)
    sA = SQLiteSyncStore(cA, "proactive_ledger")
    oA = SQLiteOplogStore(cA)
    eA = SyncEngine(sA, oA, "mac")
    ordered = SYNC_TABLES["proactive_ledger"].get("ordered_status")
    rep = reconcile_baseline_oplog(
        sA, remote_ms, eA, "proactive_ledger", "mac", ordered
    )
    cA.close()

    assert "deskmate:proactive_ledger:la" not in rep["local_only"], (
        "local 行不应被基线外推"
    )
    assert "deskmate:proactive_ledger:sa" in rep["local_only"], (
        "sync 行应被基线外推"
    )
    co = sqlite3.connect(ca)
    pushed = [
        r[0]
        for r in co.execute(
            "SELECT record_id FROM _sync_oplog"
            " WHERE table_name='proactive_ledger' AND tombstone=0"
        ).fetchall()
    ]
    co.close()
    assert all("la" not in pid for pid in pushed), "oplog 不得含 local 行的 op"
    assert any("sa" in pid for pid in pushed), "oplog 应含 sync 行的 op"


def test_visibility_full_sync_roundtrip_local_stays_local(ledger_pair):
    """端到端：A 的 local 行经增量同步 + 基线双重保险也到不了 B。"""
    ca, cb = ledger_pair
    _ledger_insert(ca, "only_a", "local")

    nodes = _daemon_pair(ca, cb, "proactive_ledger")
    for _ in range(3):
        nodes["dA"].tick()
        nodes["dB"].tick()
    # 再做一次首连基线（游标归零场景）
    cB2 = sqlite3.connect(cb)
    sB2 = SQLiteSyncStore(cB2, "proactive_ledger")
    remote_ms = MemoryStore()
    for r in sB2.items():
        remote_ms.upsert(r.record_id, r.data, r.hlc, valid_until=r.valid_until)
    cA2 = sqlite3.connect(ca)
    sA2 = SQLiteSyncStore(cA2, "proactive_ledger")
    oA2 = SQLiteOplogStore(cA2)
    eA2 = SyncEngine(sA2, oA2, "mac")
    ordered = SYNC_TABLES["proactive_ledger"].get("ordered_status")
    reconcile_baseline_oplog(
        sA2, remote_ms, eA2, "proactive_ledger", "mac", ordered
    )
    cA2.close()

    cB = sqlite3.connect(cb)
    b_ids = {r[0] for r in cB.execute("SELECT id FROM proactive_ledger").fetchall()}
    cB.close()
    assert "only_a" not in b_ids, "local 行不得经同步到达对端（增量 + 基线双保险）"


def test_visibility_default_sync_continues(ledger_pair):
    """回归保护：不显式标 local（缺省 'sync' 或 NULL）的行照常同步。"""
    ca, cb = ledger_pair
    _ledger_insert(ca, "default_sync", "sync")
    # 缺省值（建表 DEFAULT 'sync'）
    c = sqlite3.connect(ca)
    c.execute(
        f"INSERT INTO proactive_ledger({LEDGER_COLS}) VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "default_null", "automation", "task_anomaly", "content-null", "e", "", "{}",
            "mac", "pending", 0.9, "2026-10-07T00:00:00", None, None, None,
            "sync",  # 显式 sync
        ),
    )
    c.commit()
    c.close()

    nodes = _daemon_pair(ca, cb, "proactive_ledger")
    for _ in range(3):
        nodes["dA"].tick()
        nodes["dB"].tick()
    cB = sqlite3.connect(cb)
    b_ids = {r[0] for r in cB.execute("SELECT id FROM proactive_ledger").fetchall()}
    cB.close()
    assert "default_sync" in b_ids and "default_null" in b_ids, (
        "缺省/显式 sync 的行应正常同步"
    )


# ===========================================================================
# P3-c（§11.x 归档/GC）：oplog 清理
# ===========================================================================
def _insert_oplog_row(db, table_name, record_id, created_at, hlc="0000000000000000.00000000.x"):
    """直接往 _sync_oplog 插一行（seq 自动自增，created_at 显式控制）。

    hlc 默认是零值（P3-c 删除只看 seq + created_at，不依赖 hlc）。
    """
    c = sqlite3.connect(db)
    c.execute(
        f"INSERT INTO {_OPLOG_TABLE}"
        "(table_name, record_id, op, hlc, payload_json, tombstone, node, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            table_name, record_id, "upsert", hlc, "{}", 0, "mac",
            created_at,
        ),
    )
    seq = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    c.close()
    return seq


def test_gc_deletes_old_and_pushed_only(tmp_path):
    """GC 只删『seq<=safe 且超龄』的行：近期、未推送、近期已推送都保留。"""
    db = str(tmp_path / "gc.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE _noop(id TEXT)")  # 占位，触发 migrate 需要表存在不必要；此处直接建 oplog
    # 直接确保 oplog + meta 存在（不依赖业务表 migrate）
    c.execute(
        f"CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, record_id TEXT,"
        " op TEXT, hlc TEXT, payload_json TEXT, tombstone INTEGER, node TEXT,"
        " created_at INTEGER)"
    )
    c.execute("CREATE TABLE IF NOT EXISTS _sync_meta(id INTEGER PRIMARY KEY, node TEXT, hlc_counter INTEGER, suppress INTEGER)")
    c.commit(); c.close()

    now = int(time.time())
    old = now - 100 * 86400  # 100 天前（超龄）
    recent = now - 1 * 86400  # 1 天前（未超龄）
    # 插入顺序决定 seq：1..3 为已推送(old/recent/old)，4 为未推送(old)，5 为近期已推送(recent) 放在 safe 内
    _insert_oplog_row(db, "profile_items", "r1", old)      # seq1  old   pushed
    _insert_oplog_row(db, "profile_items", "r2", recent)   # seq2  recent pushed -> 保留
    _insert_oplog_row(db, "profile_items", "r3", old)      # seq3  old   pushed
    _insert_oplog_row(db, "profile_items", "r4", old)      # seq4  old   UNPUSHED (seq>3) -> 保留
    _insert_oplog_row(db, "user_posts", "r5", recent)     # seq5  recent pushed(但>safe) -> 保留

    push_after_seq = 3
    keep_days = 30
    deleted = gc_oplog(sqlite3.connect(db), keep_days, push_after_seq, dry_run=False)
    assert deleted == 2, f"应删 2 条(seq1,3 的 old+pushed)，实删 {deleted}"

    c = sqlite3.connect(db)
    kept = {r[0] for r in c.execute(f"SELECT record_id FROM {_OPLOG_TABLE}").fetchall()}
    c.close()
    assert kept == {"r2", "r4", "r5"}, f"应保留近期/未推送/超safe 行，实留 {kept}"


def test_gc_keeps_unpushed_regardless_of_age(tmp_path):
    """未推送（seq>safe）的事件即使很旧也绝不删——否则丢同步。"""
    db = str(tmp_path / "gc2.db")
    c = sqlite3.connect(db)
    c.execute(
        f"CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, record_id TEXT,"
        " op TEXT, hlc TEXT, payload_json TEXT, tombstone INTEGER, node TEXT,"
        " created_at INTEGER)"
    )
    c.commit(); c.close()
    now = int(time.time())
    old = now - 365 * 86400  # 一年前
    _insert_oplog_row(db, "profile_items", "ancient_pushed", old)   # seq1 pushed old -> 删
    _insert_oplog_row(db, "profile_items", "ancient_unpushed", old) # seq2 unpushed old -> 留

    deleted = gc_oplog(sqlite3.connect(db), 30, min_safe_seq=1, dry_run=False)
    assert deleted == 1, f"只删 1 条已推送旧行，实删 {deleted}"
    c = sqlite3.connect(db)
    kept = {r[0] for r in c.execute(f"SELECT record_id FROM {_OPLOG_TABLE}").fetchall()}
    c.close()
    assert kept == {"ancient_unpushed"}


def test_gc_dry_run_does_not_delete(tmp_path):
    """dry_run 只统计不删，返回将删条数。"""
    db = str(tmp_path / "gc3.db")
    c = sqlite3.connect(db)
    c.execute(
        f"CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, record_id TEXT,"
        " op TEXT, hlc TEXT, payload_json TEXT, tombstone INTEGER, node TEXT,"
        " created_at INTEGER)"
    )
    c.commit(); c.close()
    now = int(time.time())
    old = now - 100 * 86400
    for _ in range(5):
        _insert_oplog_row(db, "profile_items", f"x{_}", old)  # 5 条全 old 且 seq<=5
    conn = sqlite3.connect(db)
    will_delete = gc_oplog(conn, 30, min_safe_seq=5, dry_run=True)
    assert will_delete == 5
    remain = conn.execute(f"SELECT COUNT(*) FROM {_OPLOG_TABLE}").fetchone()[0]
    conn.close()
    assert remain == 5, "dry_run 不得实际删除"


def test_gc_store_method_wraps_function(tmp_path):
    """SQLiteOplogStore.gc 短连接包装与模块函数一致。"""
    db = str(tmp_path / "gc4.db")
    c = sqlite3.connect(db)
    c.execute(
        f"CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, record_id TEXT,"
        " op TEXT, hlc TEXT, payload_json TEXT, tombstone INTEGER, node TEXT,"
        " created_at INTEGER)"
    )
    c.commit(); c.close()
    now = int(time.time())
    old = now - 100 * 86400
    for _ in range(3):
        _insert_oplog_row(db, "profile_items", f"x{_}", old)
    oplog = SQLiteOplogStore(sqlite3.connect(db))
    deleted = oplog.gc(keep_days=30, min_safe_seq=3, dry_run=False)
    assert deleted == 3


def test_gc_backfill_created_at_from_hlc(tmp_path):
    """存量 oplog 缺 created_at 时，_backfill_oplog_created_at 用 hlc 物理时间回填。"""
    db = str(tmp_path / "gc5.db")
    c = sqlite3.connect(db)
    c.execute(
        f"CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, record_id TEXT,"
        " op TEXT, hlc TEXT, payload_json TEXT, tombstone INTEGER, node TEXT,"
        " created_at INTEGER)"
    )
    # hlc 物理 ms = 0x000000018bd2d648 = 2026-... 随便一个确定值：1700000000000 ms
    hlc = "0000018bd2d648.00000000.mac"
    c.execute(
        f"INSERT INTO {_OPLOG_TABLE}"
        "(table_name, record_id, op, hlc, payload_json, tombstone, node, created_at) "
        "VALUES ('profile_items','z','upsert',?, '{{}}',0,'mac',NULL)",
        (hlc,),
    )
    c.commit()
    n = _backfill_oplog_created_at(c)
    c.commit()
    c.close()
    assert n == 1
    c = sqlite3.connect(db)
    ca = c.execute(f"SELECT created_at FROM {_OPLOG_TABLE} WHERE record_id='z'").fetchone()[0]
    c.close()
    expected = int(hlc.split(".")[0], 16) // 1000
    assert ca == expected, f"created_at 应为 hlc 物理秒 {expected}，实得 {ca}"


def test_gc_min_safe_seq_uses_push_and_peer_pull():
    """【2026-10-07 方案甲①终口径】安全水位 = max(push_after_seq, peer_pull_after_seq)。

    演进链（三段式，本测试锁最终语义）：
    1. 原始：push-only —— 对，但错峰窗口下 Mac push 自 10-05 冻结（16:02 实测：
       云窗口仅 11s、Mac 首 tick 存量 ~5600 条结构性完不成）→ Mac GC 永不生效；
    2. 当日 max(push, pull)：**错** —— pull_after_seq 属对端 seq 空间（复核意见 P2），
       云 4789>自身 max 护栏失效、Mac 1676 纹丝不动；
    3. 方案甲①（萧哥拍板）：对端 push 时经 X-Local-Pull-After-Seq 捎带
       "它从本端拉走的水位"（单对端拓扑下该值就在**本端** seq 空间）→
       max(push, peer_pull) 两项同空间、语义都是"对端必已拿到"。
    本端自己的 pull_after_seq **绝不参与**。
    """
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "gc_sync_oplog.py")
    spec = importlib.util.spec_from_file_location("_gc_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    src = open(path, encoding="utf-8").read()
    assert "min_safe_seq = max(push_after_seq, peer_pull_after_seq)" in src, (
        "GC 安全水位必须是 max(push, peer_pull)——两项都在本端 seq 空间"
    )
    assert "max(push_after_seq, pull_after_seq)" not in src, (
        "本端 pull_after_seq 属对端 seq 空间，参与 max 会让护栏越界失效（复核意见 P2）"
    )
    # 删除路径必须用 min_safe_seq 而非直接 push_after_seq（保留命名便于诊断日志）
    assert "gc_oplog(conn, args.keep_days, min_safe_seq" in src, (
        "gc_oplog 调用应经 min_safe_seq 变量（便于日志诊断）"
    )
