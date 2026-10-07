"""剩余待办收口测试（2026-10-07）：租约(SQLiteLease) + apply 写节流(写饥饿缓解)。

覆盖三块（对应《小桔_逻辑大脑多副本同步架构设计》十（补）剩余待办）：
- 第 5 项 租约/单写窗口：acquire/release/过期接管/同持有者续租/并发互斥；
- 第 3 项 写饥饿「节流」方案：apply_remote 按 chunk 让出写锁、
  XIAOJU_APPLY_YIELD_MS=0 可关闭、单表与多表路由语义不变；
- 用点回归：runner _run_baseline 的租约互斥（抢不到→跳过；跑完→释放）。

全部在 /tmp 临时库进行，**不碰 ~/.deskmate 活库**。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from deskmate.memory import sync_daemon as sdmod  # noqa: E402
from deskmate.memory.sync_store_sqlite import SQLiteLease  # noqa: E402
from scripts.sync_daemon_runner import SyncNode, _load_sync_modules  # noqa: E402


# ---------------------------------------------------------------------------
# 1. SQLiteLease（协议六零件之六）
# ---------------------------------------------------------------------------


@pytest.fixture
def lease(tmp_path):
    db = str(tmp_path / "lease.db")
    conn = sqlite3.connect(db)
    yield SQLiteLease(conn)
    conn.close()


def test_lease_acquire_release_basic(lease):
    assert lease.acquire("baseline", "mac:1", ttl_s=60) is True
    st = lease.status("baseline")
    assert st is not None and st[0] == "mac:1"
    assert lease.held("baseline") is True
    # 他人在有效期内抢 → 失败
    assert lease.acquire("baseline", "cloud:2", ttl_s=60) is False
    assert lease.status("baseline")[0] == "mac:1"
    # 仅持有者本人可释放；他人释放无效
    assert lease.release("baseline", "cloud:2") is False
    assert lease.acquire("baseline", "mac:1", ttl_s=60) is True  # 持有者续租始终允许
    assert lease.release("baseline", "mac:1") is True
    assert lease.held("baseline") is False
    assert lease.status("baseline") is None  # release 后行删除
    # 释放后他人可接管
    assert lease.acquire("baseline", "cloud:2", ttl_s=60) is True


def test_lease_expiry_takeover(lease):
    assert lease.acquire("k", "holder-a", ttl_s=600) is True
    # 把过期时间改成过去（模拟持有者崩溃后 TTL 到期）
    with lease._connect() as c:
        c.execute("UPDATE _sync_lease SET expires_at = 1 WHERE name = ?", ("k",))
        c.commit()
    # 未过期判定为不持有
    assert lease.held("k") is False
    # 他人可接管（原子 UPSERT-WHERE 过期分支）
    assert lease.acquire("k", "holder-b", ttl_s=600) is True
    assert lease.status("k")[0] == "holder-b"
    # 旧持有者不能再释放新持有者的租约
    assert lease.release("k", "holder-a") is False
    assert lease.held("k") is True


def test_lease_two_instances_same_db_are_mutex(tmp_path):
    """两个 SQLiteLease 实例（模拟两个进程）连同一库：同一时刻只有一个持有者。"""
    db = str(tmp_path / "m.db")
    a = SQLiteLease(sqlite3.connect(db))
    b = SQLiteLease(sqlite3.connect(db))
    assert a.acquire("w", "proc-a", ttl_s=60) is True
    assert b.acquire("w", "proc-b", ttl_s=60) is False
    assert a.release("w", "proc-a") is True
    assert b.acquire("w", "proc-b", ttl_s=60) is True


# ---------------------------------------------------------------------------
# 2. apply_remote 写节流（写饥饿缓解，压测 §10.4②「节流」方案）
# ---------------------------------------------------------------------------


def _make_ops(n, table="profile_items"):
    out = []
    for i in range(n):
        hlc = sdmod.HLC(1_700_000_000_000 + i, 0, "cloud")
        out.append(
            sdmod.Op(table, f"r{i}", "upsert", hlc, payload={"id": f"r{i}"}, origin="cloud")
        )
    return out


def _recording_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(sdmod.time, "sleep", lambda s: calls.append(s))
    return calls


def test_apply_remote_throttle_yields_between_chunks(monkeypatch):
    """chunk=2、5 条 op：应在第 2、4 条后各让出一次写锁，且全部应用不丢不重。"""
    monkeypatch.setenv("XIAOJU_APPLY_CHUNK", "2")
    monkeypatch.setenv("XIAOJU_APPLY_YIELD_MS", "1")
    sleeps = _recording_sleep(monkeypatch)
    store = sdmod.MemoryStore()
    engine = sdmod.SyncEngine(store, sdmod.OplogStore(), "mac")
    ops = _make_ops(5)
    engine.apply_remote(ops)
    assert len(sleeps) == 2  # i+1=2,4；最后一条不睡
    assert all(abs(s - 0.001) < 1e-9 for s in sleeps)
    got = sorted(store.live_ids()) if hasattr(store, "live_ids") else sorted(store._recs)
    assert got == [f"r{i}" for i in range(5)]


def test_apply_remote_throttle_disabled(monkeypatch):
    """XIAOJU_APPLY_YIELD_MS=0 关闭节流：零 sleep、行为与旧版一致。"""
    monkeypatch.setenv("XIAOJU_APPLY_YIELD_MS", "0")
    sleeps = _recording_sleep(monkeypatch)
    store = sdmod.MemoryStore()
    engine = sdmod.SyncEngine(store, sdmod.OplogStore(), "mac")
    engine.apply_remote(_make_ops(7))
    assert sleeps == []
    assert sorted(store._recs) == [f"r{i}" for i in range(7)]


def test_apply_remote_default_fast_path_no_sleep(monkeypatch):
    """默认 chunk=100：常规 tick（<100 条）零 sleep，不影响热路径。"""
    monkeypatch.delenv("XIAOJU_APPLY_CHUNK", raising=False)
    monkeypatch.delenv("XIAOJU_APPLY_YIELD_MS", raising=False)
    sleeps = _recording_sleep(monkeypatch)
    store = sdmod.MemoryStore()
    engine = sdmod.SyncEngine(store, sdmod.OplogStore(), "mac")
    engine.apply_remote(_make_ops(50))
    assert sleeps == []
    assert len(store._recs) == 50


def test_apply_remote_multi_table_routing_with_throttle(monkeypatch):
    """多表路径：节流不改变按 op.table 的路由语义。"""
    monkeypatch.setenv("XIAOJU_APPLY_CHUNK", "1")
    monkeypatch.setenv("XIAOJU_APPLY_YIELD_MS", "1")
    sleeps = _recording_sleep(monkeypatch)
    a = sdmod.MemoryStore()
    b = sdmod.MemoryStore()
    engine = sdmod.SyncEngine({"profile_items": a, "loved_entries": b}, sdmod.OplogStore(), "mac")
    ops = _make_ops(3, "profile_items") + _make_ops(3, "loved_entries")
    engine.apply_remote(ops)
    assert len(sleeps) == 5  # 6 条、chunk=1：除最后一条外各让出一次
    assert sorted(a._recs) == ["r0", "r1", "r2"]
    assert sorted(b._recs) == ["r0", "r1", "r2"]
    # 未知表静默跳过（既有 fail-safe 契约不变）：跳过的 op 没有写库、不占写锁，
    # 也就不需要让出——sleep 计数不变（节流只包住真正写库的路径）。
    engine.apply_remote(_make_ops(2, "interview_sessions"))
    assert len(sleeps) == 5


# ---------------------------------------------------------------------------
# 3. 用点回归：runner _run_baseline 的租约互斥
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """pull_snapshot 记录调用次数后抛错（fail-open 路径），模拟对端不可达。"""

    def __init__(self):
        self.calls = 0

    def pull_snapshot(self, table):
        self.calls += 1
        raise RuntimeError("对端不可达（测试注入）")


@pytest.fixture
def node():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="lease_node_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE profile_items (id TEXT PRIMARY KEY, layer TEXT, content TEXT)")
    conn.commit()
    conn.close()
    sd, sync_sql, st = _load_sync_modules("mac", REPO, "")
    n = SyncNode(
        "mac", sd, sync_sql, st,
        db_path=path, node="mac", interval=0.05,
        ssh_host="example.invalid", ssh_user="x", identity_file="/nope",
    )
    yield n
    os.remove(path)


def test_baseline_lease_skips_when_held_and_releases_after_run(node):
    node.transport = _RecordingTransport()
    # 他人持有租约 → 跳过本轮：pull 不被调用
    conn = sqlite3.connect(node._db) if hasattr(node, "_db") else sqlite3.connect(node.db_path)
    lease = node.sync_sql.SQLiteLease(conn)
    assert lease.acquire("baseline", "other:999", ttl_s=600) is True
    conn.close()
    assert node._run_baseline() is False
    assert node.transport.calls == 0  # 关键：租约互斥生效，根本没进对账
    # 释放后重跑：真正进入基线（对端不可达 → fail-open 返回 False，但跑过了）
    conn = sqlite3.connect(node.db_path)
    lease2 = node.sync_sql.SQLiteLease(conn)
    assert lease2.release("baseline", "other:999") is True
    conn.close()
    assert node._run_baseline() is False
    assert node.transport.calls == 1
    # 跑完（含失败路径 finally）租约必须已释放
    conn = sqlite3.connect(node.db_path)
    lease3 = node.sync_sql.SQLiteLease(conn)
    assert lease3.held("baseline") is False
    conn.close()
