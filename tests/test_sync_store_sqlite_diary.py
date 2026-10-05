"""个人中心日记 + 留言纳入同步范围（阶段 6，2026-10-05）的端到端测试。

依据 reports/2026-10-05_会话日记同步范围扩大_设计方案.md §2/§4：
- user_posts / comments 与现有 3 表同处 deskmate.db，纳入同一 oplog；
- 两表 schema 照抄 DiaryStore 的真实 DDL（含 archived 列），避免测试与生产漂移。

覆盖本批真实风险（每条都对应一个曾踩或实测到的坑）：
1. schema 前置：两表原本没有 sync 列 → 不迁移则触发器根本建不出来。
2. 半回填行：存量只有 sync_uuid 而 sync_hlc 为 NULL 时，items() 会
   HLC.from_str(None) 崩（实测 AttributeError）→ migrate 必须一并补 hlc。
3. app 直写捕获：DiaryStore 绕过同步层直写业务表 → 必须由触发器进 oplog。
4. 回声防护：引擎写（upsert/delete）不得再被触发器记一份，否则两端互弹。
5. 归档态与心标记：loved/archived 在 business_cols 里，漏了就丢状态。
6. 不误纳入：interview_sessions 不在 SYNC_TABLES → 不同步。
"""

import os
import sqlite3
import tempfile

import pytest

from deskmate.memory.sync_daemon import (
    HLC,
    InMemoryTransport,
    MemoryStore,
    Op,
    SyncDaemon,
    SyncEngine,
    new_uuid,
    reconcile_baseline_oplog,
)
from deskmate.memory.sync_store_sqlite import (
    SYNC_TABLES,
    SQLiteOplogStore,
    SQLiteSyncStore,
    migrate,
)

# 与 deskmate/growth/diary_store.py::_ensure_schema 的真实 DDL 保持一致
CREATE_USER_POSTS = """CREATE TABLE user_posts (
    id         TEXT PRIMARY KEY,
    author     TEXT NOT NULL DEFAULT 'me',
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    loved      INTEGER NOT NULL DEFAULT 0
)"""
CREATE_COMMENTS = """CREATE TABLE comments (
    id         TEXT PRIMARY KEY,
    moment_id  TEXT NOT NULL,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""
CREATE_INTERVIEW = """CREATE TABLE interview_sessions (
    id         TEXT PRIMARY KEY,
    status     TEXT,
    created_at TEXT
)"""


def _archived_added(conn):
    """archived 列由 DiaryStore 以 ALTER 幂等追加，照抄该形态。"""
    conn.execute(
        "ALTER TABLE user_posts ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
    )


@pytest.fixture
def pair():
    """两端同源快照：各 3 篇日记 + 3 条留言 + 1 条访谈会话，schema 与生产同构。"""
    d = tempfile.mkdtemp(prefix="diary_sync_")
    ca, cb = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    for p in (ca, cb):
        c = sqlite3.connect(p)
        c.execute(CREATE_USER_POSTS)
        _archived_added(c)
        c.execute(CREATE_COMMENTS)
        c.execute(CREATE_INTERVIEW)
        for i in range(3):
            c.execute(
                "INSERT INTO user_posts(id, author, content, created_at, loved) "
                "VALUES(?,?,?,?,0)",
                (f"post{i}", "me", f"日记{i}", f"2026-10-0{i + 1}T09:00:00"),
            )
            c.execute(
                "INSERT INTO comments(id, moment_id, author, text, created_at) "
                "VALUES(?,?,?,?,?)",
                (f"cmt{i}", f"post{i}", "xiaoju", f"留言{i}", f"2026-10-0{i + 1}T10:00:00"),
            )
        c.execute(
            "INSERT INTO interview_sessions(id, status, created_at) "
            "VALUES('iv1','done','x')"
        )
        c.commit()
        c.close()
    for p, node in ((ca, "mac"), (cb, "cloud")):
        c = sqlite3.connect(p)
        migrate(c, node)
        c.close()
    return ca, cb


def _nodes(ca, cb, tables=("user_posts", "comments")):
    cA = sqlite3.connect(ca, check_same_thread=False)
    oA = SQLiteOplogStore(cA)
    sA = {t: SQLiteSyncStore(cA, t) for t in tables}
    eA = SyncEngine(sA, oA, "mac")
    cB = sqlite3.connect(cb, check_same_thread=False)
    oB = SQLiteOplogStore(cB)
    sB = {t: SQLiteSyncStore(cB, t) for t in tables}
    eB = SyncEngine(sB, oB, "cloud")
    dA = SyncDaemon(eA, InMemoryTransport(oB))
    dB = SyncDaemon(eB, InMemoryTransport(oA))
    return dict(cA=cA, cB=cB, oA=oA, oB=oB, sA=sA, sB=sB, eA=eA, eB=eB, dA=dA, dB=dB)


def _uuid_of(db, table, pk):
    c = sqlite3.connect(db)
    v = c.execute(f"SELECT sync_uuid FROM {table} WHERE id=?", (pk,)).fetchone()[0]
    c.close()
    return v


def _uuids(conn):
    out = {}
    for t in ("user_posts", "comments"):
        out[t] = dict(conn.execute(f"SELECT id, sync_uuid FROM {t}").fetchall())
    return out


# ---------------------------------------------------------------------------
# 1. 注册范围
# ---------------------------------------------------------------------------


def test_both_diary_tables_registered():
    assert "user_posts" in SYNC_TABLES
    assert "comments" in SYNC_TABLES


def test_interview_sessions_not_registered():
    """萧哥 10-05 拍板：访谈会话不同步。必须在注册表里缺席。"""
    assert "interview_sessions" not in SYNC_TABLES


def test_business_cols_cover_loved_and_archived():
    """漏了 loved/archived 会让对端丢失心标记与归档状态。"""
    assert "loved" in SYNC_TABLES["user_posts"]["business_cols"]
    assert "archived" in SYNC_TABLES["user_posts"]["business_cols"]
    assert "moment_id" in SYNC_TABLES["comments"]["business_cols"]


def test_text_pk_uses_id_as_sync_key():
    """两表都是 TEXT 主键 → 双端同 id 即同记录，可直接用 id 派生 sync_uuid。"""
    for t in ("user_posts", "comments"):
        spec = SYNC_TABLES[t]
        assert spec["pk_type"] == "text"
        assert spec["sync_key"] == "id"


# ---------------------------------------------------------------------------
# 2. 迁移：补列 + 回填（含半回填行的修复）
# ---------------------------------------------------------------------------


def test_migrate_adds_sync_cols_and_backfills(pair):
    ca, _ = pair
    c = sqlite3.connect(ca)
    for t in ("user_posts", "comments"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
        assert {"sync_uuid", "sync_hlc", "sync_tombstone"} <= cols
        n_null = c.execute(
            f"SELECT COUNT(*) FROM {t} "
            "WHERE sync_uuid IS NULL OR sync_hlc IS NULL"
        ).fetchone()[0]
        assert n_null == 0, f"{t} 仍有 {n_null} 行 sync 列不完整"
    c.close()


def test_migrate_is_idempotent(pair):
    ca, _ = pair
    c = sqlite3.connect(ca)
    before = _uuids(c)
    rep = migrate(c, "mac")
    assert rep["backfilled"]["user_posts"] == 0
    assert rep["backfilled"]["comments"] == 0
    assert before == _uuids(c), "重跑 migrate 不得改动 sync_uuid"
    c.close()


def test_migrate_repairs_half_backfilled_rows(tmp_path):
    """回归：存量只有 sync_uuid 而 sync_hlc 为 NULL 时，items() 会 HLC.from_str(None) 崩。

    这是本批真实存在的行态（2026-10-05 实测 62 行），migrate 必须一并补 hlc。
    """
    db = str(tmp_path / "half.db")
    c = sqlite3.connect(db)
    c.execute(CREATE_USER_POSTS)
    _archived_added(c)
    c.execute(CREATE_COMMENTS)
    c.execute(
        "INSERT INTO user_posts(id,author,content,created_at) VALUES('p1','me','x','t')"
    )
    c.execute(
        "INSERT INTO comments(id,moment_id,author,text,created_at) "
        "VALUES('c1','p1','a','t','t')"
    )
    # 手工造半回填行态：uuid 有、hlc 无（不建触发器，模拟上批手工迁移的产物）
    for t in ("user_posts", "comments"):
        c.execute(f"ALTER TABLE {t} ADD COLUMN sync_uuid TEXT")
        c.execute(f"ALTER TABLE {t} ADD COLUMN sync_hlc TEXT")
        c.execute(f"ALTER TABLE {t} ADD COLUMN sync_tombstone INTEGER DEFAULT 0")
    c.execute("UPDATE user_posts SET sync_uuid='deskmate:user_posts:p1' WHERE id='p1'")
    c.execute("UPDATE comments SET sync_uuid='deskmate:comments:c1' WHERE id='c1'")
    c.commit()

    rep = migrate(c, "mac")
    assert rep["backfilled"]["user_posts"] == 1, "半回填行应被计入回填并补 hlc"
    assert rep["backfilled"]["comments"] == 1
    for t in ("user_posts", "comments"):
        assert (
            c.execute(f"SELECT COUNT(*) FROM {t} WHERE sync_hlc IS NULL").fetchone()[0]
            == 0
        ), f"{t} 的 sync_hlc 未补齐"

    # 关键：不补 hlc 时这里会 AttributeError（NoneType has no attribute split）
    conn = sqlite3.connect(db, check_same_thread=False)
    assert len(SQLiteSyncStore(conn, "user_posts").items()) == 1
    assert len(SQLiteSyncStore(conn, "comments").items()) == 1
    # 且 sync_uuid 必须保持原值，不得被重新派生（否则两端身份漂移）
    assert (
        c.execute("SELECT sync_uuid FROM user_posts WHERE id='p1'").fetchone()[0]
        == "deskmate:user_posts:p1"
    )
    c.close()


def test_backfill_keeps_existing_uuid_and_derives_deterministically(pair):
    """两端同源快照 → 同一条存量必得同一 sync_uuid（基线对账的前提）。"""
    ca, cb = pair
    for i in range(3):
        assert _uuid_of(ca, "user_posts", f"post{i}") == _uuid_of(
            cb, "user_posts", f"post{i}"
        )
        assert _uuid_of(ca, "comments", f"cmt{i}") == _uuid_of(
            cb, "comments", f"cmt{i}"
        )


def test_backfill_does_not_pollute_oplog(pair):
    """存量回填必须在建触发器之前完成，否则 62 行存量会灌进 oplog。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    n = c.execute(
        "SELECT COUNT(*) FROM _sync_oplog "
        "WHERE table_name IN ('user_posts','comments')"
    ).fetchone()[0]
    c.close()
    assert n == 0, f"存量回填污染了 oplog（{n} 行），会拖慢首轮推送"


def test_triggers_created_for_both_tables(pair):
    ca, _ = pair
    c = sqlite3.connect(ca)
    names = {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    c.close()
    for t in ("user_posts", "comments"):
        for kind in ("ai", "au", "ad"):
            assert f"trg_{t}_{kind}" in names, f"缺触发器 trg_{t}_{kind}"


def test_migrate_skips_missing_table_without_crashing(tmp_path):
    """库里没有 user_posts（老库/最小化库）→ 跳过而非崩。"""
    db = str(tmp_path / "min.db")
    c = sqlite3.connect(db)
    rep = migrate(c, "mac")
    assert "user_posts" in rep["skipped"] and "comments" in rep["skipped"]
    assert "user_posts" not in rep["tables"]
    c.close()


# ---------------------------------------------------------------------------
# 3. app 直写捕获（触发器路径）
# ---------------------------------------------------------------------------


def test_app_direct_write_captured_in_oplog(pair):
    """DiaryStore 绕过同步层直写 → 必须由触发器进 oplog，否则新日记永不上云。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    c.execute(
        "INSERT INTO user_posts(id,author,content,created_at,loved) "
        "VALUES('new1','me','新日记','2026-10-05T12:00:00',0)"
    )
    c.execute(
        "INSERT INTO comments(id,moment_id,author,text,created_at) "
        "VALUES('nc1','new1','xiaoju','留言','2026-10-05T12:00:01')"
    )
    c.commit()
    rows = c.execute(
        "SELECT table_name, record_id, op, tombstone, payload_json FROM _sync_oplog "
        "WHERE table_name IN ('user_posts','comments') ORDER BY seq"
    ).fetchall()
    c.close()
    assert len(rows) == 2, f"应捕获 2 条，实得 {len(rows)}"
    kinds = {(r[0], r[2]) for r in rows}
    assert kinds == {("user_posts", "upsert"), ("comments", "upsert")}
    assert rows[0][1] == "deskmate:user_posts:new1", "sync_uuid 必须由触发器按约定派生"
    assert "新日记" in rows[0][4], "payload 必须含明文内容"


def test_app_update_archived_captured(pair):
    """归档是 UI 操作（UPDATE user_posts SET archived）→ 也必须进 oplog。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    c.execute("UPDATE user_posts SET archived=1 WHERE id='post0'")
    c.commit()
    row = c.execute(
        "SELECT record_id, payload_json FROM _sync_oplog "
        "WHERE table_name='user_posts' AND op='upsert'"
    ).fetchone()
    c.close()
    assert row is not None, "app 直写 UPDATE 未被捕获"
    assert '"archived":1' in row[1].replace(" ", ""), f"payload 缺 archived=1：{row[1]}"


def test_app_direct_delete_writes_tombstone(pair):
    """app 直写删除必须落墓碑侧表，否则迟到 upsert 会复活已删日记。"""
    ca, _ = pair
    c = sqlite3.connect(ca)
    c.execute("DELETE FROM user_posts WHERE id='post0'")
    c.commit()
    op = c.execute(
        "SELECT record_id, op, tombstone FROM _sync_oplog WHERE table_name='user_posts'"
    ).fetchone()
    tomb = c.execute(
        "SELECT hlc FROM _sync_tombstones WHERE table_name='user_posts' AND record_id=?",
        (op[0],),
    ).fetchone()
    c.close()
    assert op[1] == "delete" and op[2] == 1
    assert tomb is not None, "app 直写删除须落墓碑侧表"


def test_engine_write_does_not_echo(pair):
    """回声防护：引擎 upsert/delete 自身置 suppress，不得被触发器再记一份。"""
    ca, _ = pair
    n = _nodes(ca, ca)  # 同一库双引擎，观察 oplog 增量
    before = n["oA"].watermark()
    rid = new_uuid()
    n["eA"].local_upsert("user_posts", rid, {"id": "echo1", "content": "x"})
    after_upsert = n["oA"].watermark()
    assert after_upsert == before + 1, f"引擎 upsert 应只记 1 条，实记 {after_upsert - before}"
    n["eA"].local_delete("user_posts", rid)
    assert n["oA"].watermark() == after_upsert + 1, "引擎 delete 应只记 1 条"


# ---------------------------------------------------------------------------
# 4. 双向收敛
# ---------------------------------------------------------------------------


def test_new_diary_propagates_to_other_side(pair):
    """Mac 新写日记 + 留言 → 云端应逐条落地（本次扩范围的核心目标）。"""
    ca, cb = pair
    n = _nodes(ca, cb)
    n["eA"].local_upsert(
        "user_posts",
        "deskmate:user_posts:brandnew",
        {
            "id": "brandnew",
            "author": "me",
            "content": "刚写的日记",
            "created_at": "2026-10-05T12:00:00",
            "loved": 0,
            "archived": 0,
        },
    )
    n["eA"].local_upsert(
        "comments",
        "deskmate:comments:cn",
        {
            "id": "cn",
            "moment_id": "brandnew",
            "author": "xiaoju",
            "text": "收到",
            "created_at": "2026-10-05T12:00:01",
        },
    )
    for _ in range(3):
        n["dA"].tick()
        n["dB"].tick()
    c = sqlite3.connect(cb)
    got = c.execute("SELECT content FROM user_posts WHERE id='brandnew'").fetchone()
    gotc = c.execute("SELECT text FROM comments WHERE id='cn'").fetchone()
    c.close()
    assert got is not None and got[0] == "刚写的日记", "新日记未落到对端"
    assert gotc is not None and gotc[0] == "收到", "新留言未落到对端"


def test_loved_and_archived_flags_sync(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    rid = _uuid_of(ca, "user_posts", "post0")
    n["eA"].local_upsert(
        "user_posts",
        rid,
        {
            "id": "post0",
            "author": "me",
            "content": "日记0",
            "created_at": "t",
            "loved": 1,
            "archived": 1,
        },
    )
    n["dA"].tick()
    n["dB"].tick()
    n["dA"].tick()
    c = sqlite3.connect(cb)
    row = c.execute("SELECT loved, archived FROM user_posts WHERE id='post0'").fetchone()
    c.close()
    assert row == (1, 1), f"心标记/归档状态未同步，对端为 {row}"


def test_delete_on_one_side_removes_on_other(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    rid = _uuid_of(ca, "user_posts", "post1")
    n["eA"].local_delete("user_posts", rid)
    for _ in range(3):
        n["dA"].tick()
        n["dB"].tick()
    c = sqlite3.connect(cb)
    left = c.execute("SELECT COUNT(*) FROM user_posts WHERE id='post1'").fetchone()[0]
    c.close()
    assert left == 0, "一端删除后对端应物理删除（不得留幽灵行）"


def test_no_resurrection_after_delete(pair):
    ca, cb = pair
    n = _nodes(ca, cb)
    rid = _uuid_of(ca, "user_posts", "post2")
    n["eA"].local_delete("user_posts", rid)
    for _ in range(10):
        n["dA"].tick()
        n["dB"].tick()
    c = sqlite3.connect(cb)
    left = c.execute("SELECT COUNT(*) FROM user_posts WHERE id='post2'").fetchone()[0]
    c.close()
    assert left == 0, "删除后被迟到事件复活（LWW 防复活失效）"


def test_two_sides_converge_on_uuid_sets(pair):
    """逐条对账（不只数行数）——防行数相同但内容不同的假绿。"""
    ca, cb = pair
    n = _nodes(ca, cb)
    rid = _uuid_of(ca, "user_posts", "post0")
    n["eA"].local_upsert(
        "user_posts",
        rid,
        {
            "id": "post0",
            "author": "me",
            "content": "改过的日记0",
            "created_at": "t",
            "loved": 0,
            "archived": 0,
        },
    )
    n["eB"].local_upsert(
        "comments",
        _uuid_of(cb, "comments", "cmt1"),
        {
            "id": "cmt1",
            "moment_id": "post1",
            "author": "xiaoju",
            "text": "云端改的",
            "created_at": "t",
        },
    )
    for _ in range(4):
        n["dA"].tick()
        n["dB"].tick()
    la = {
        (t, r.record_id): r.data
        for t, st in n["sA"].items()
        for r in st.live_items()
    }
    lb = {
        (t, r.record_id): r.data
        for t, st in n["sB"].items()
        for r in st.live_items()
    }
    assert set(la) == set(lb), "两端 (表, sync_uuid) 集合不一致"
    assert la[("user_posts", rid)]["content"] == "改过的日记0"
    assert lb[("user_posts", rid)]["content"] == "改过的日记0"


def test_unregistered_table_ops_are_ignored(pair):
    """引擎收到未注册表的 op（如 interview_sessions）应静默跳过，不崩。"""
    ca, _ = pair
    n = _nodes(ca, ca)
    n["eB"].apply_remote(
        [
            Op(
                "interview_sessions",
                new_uuid(),
                "upsert",
                HLC(9999, 0, "cloud"),
                {"x": 1},
            )
        ]
    )
    c = sqlite3.connect(ca)
    n_iv = c.execute("SELECT COUNT(*) FROM interview_sessions").fetchone()[0]
    c.close()
    assert n_iv == 1, "未注册表的 op 不应改动业务表"


# ---------------------------------------------------------------------------
# 5. 基线对账（存量首次上云走这条路径）
# ---------------------------------------------------------------------------


def test_baseline_reconcile_restores_missing_diary(pair):
    """云端缺一条存量日记 → 基线对账应把它补上（这正是本次要修的缺口）。"""
    ca, cb = pair
    c = sqlite3.connect(cb)
    c.execute("DELETE FROM user_posts WHERE id='post2'")
    c.execute("DELETE FROM _sync_tombstones WHERE table_name='user_posts'")
    c.commit()
    c.close()

    n = _nodes(ca, cb)
    remote = MemoryStore()
    for r in n["sA"]["user_posts"].items():
        remote.upsert(r.record_id, r.data, r.hlc)
    local = SQLiteSyncStore(sqlite3.connect(cb, check_same_thread=False), "user_posts")
    rep = reconcile_baseline_oplog(local, remote, n["eA"], "user_posts", "mac")
    assert len(rep["remote_only"]) == 1, f"应识别出云端缺的 1 条，实得 {rep}"

    c = sqlite3.connect(cb)
    back = c.execute("SELECT COUNT(*) FROM user_posts WHERE id='post2'").fetchone()[0]
    c.close()
    assert back == 1, "基线对账未把缺失日记写回云端"


def test_baseline_conflicts_are_not_silent(pair):
    """两端同记录不同内容 → 对账须显式报冲突数，不静默丢（静默丢即数据丢失）。"""
    ca, cb = pair
    n = _nodes(ca, cb)
    rid = _uuid_of(ca, "user_posts", "post0")
    remote = MemoryStore()
    for r in n["sA"]["user_posts"].items():
        remote.upsert(r.record_id, r.data, r.hlc)
    remote.upsert(rid, {"id": "post0", "content": "远端版本"}, HLC(10**12, 0, "cloud"))
    local = SQLiteSyncStore(sqlite3.connect(cb, check_same_thread=False), "user_posts")
    rep = reconcile_baseline_oplog(local, remote, n["eA"], "user_posts", "cloud")
    assert "conflicts" in rep


def test_snapshot_payload_roundtrip(pair):
    """/snapshot 出口：records 需含 sync_uuid 与业务列，供对端基线重建。"""
    ca, _ = pair
    st = SQLiteSyncStore(sqlite3.connect(ca, check_same_thread=False), "comments")
    items = st.items()
    assert len(items) == 3
    d = items[0].data
    assert set(d) == set(SYNC_TABLES["comments"]["business_cols"])
    assert d["moment_id"].startswith("post")
