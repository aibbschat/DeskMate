"""跨线程回归：SQLite OplogStore + HttpOplogServer 在同进程 daemon/server 线程下不崩。

覆盖 2026-10-03 发现的盲区：之前单测用内存版 OplogStore（无线程问题），漏掉了
「SQLite 连接跨线程（HttpOplogServer 守护线程使用 OplogStore 连接）」在生产形态下
导致 RemoteDisconnected / SQLite 跨线程崩溃。本测试用真实 SQLite db + 独立 server
线程，验证多轮 tick 收敛且无跨线程崩溃 + 回声不膨胀。
"""
import sqlite3

import pytest

from deskmate.memory import sync_daemon as sd
from deskmate.memory import sync_store_sqlite as ss
from deskmate.memory import sync_transport as st


def _prepare(path, node, n):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE profile_items (
            id TEXT PRIMARY KEY, layer TEXT, dimension TEXT NOT NULL DEFAULT '',
            topic TEXT, content TEXT, category TEXT, status TEXT, confidence REAL,
            extra_json TEXT, valid_from TEXT, valid_until TEXT)"""
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO profile_items (id, layer, dimension, topic, content) "
            "VALUES (?,?,?,?,?)",
            (f"r{i}", "worldview", f"d{i}", f"t{i}", f"c{i}"),
        )
    conn.commit()
    ss.migrate(conn, node)
    conn.close()


def test_sqlite_http_cross_thread_no_crash_and_converges(tmp_path):
    mac_db = tmp_path / "mac.db"
    cloud_db = tmp_path / "cloud.db"
    # 云端是 Mac 的子集（前 7 条），模拟真实「Mac 92 / 云端 88 且云端为子集」
    _prepare(mac_db, "mac", 10)
    _prepare(cloud_db, "cloud", 7)

    mc = sqlite3.connect(str(mac_db))
    cc = sqlite3.connect(str(cloud_db))
    mac_store = ss.SQLiteSyncStore(mc)
    mac_oplog = ss.SQLiteOplogStore(mc)
    cloud_store = ss.SQLiteSyncStore(cc)
    cloud_oplog = ss.SQLiteOplogStore(cc)
    mac_eng = sd.SyncEngine(mac_store, mac_oplog, "mac")
    cloud_eng = sd.SyncEngine(cloud_store, cloud_oplog, "cloud")

    # 同进程：server 守护线程与本端 daemon 共享同一 oplog 连接（生产形态，跨线程）
    mac_srv = st.HttpOplogServer(mac_oplog, port=0)
    mac_srv.start()
    cloud_srv = st.HttpOplogServer(cloud_oplog, port=0)
    cloud_srv.start()
    mac_daemon = sd.SyncDaemon(mac_eng, st.HttpOplogTransport(cloud_srv.url()))
    cloud_daemon = sd.SyncDaemon(cloud_eng, st.HttpOplogTransport(mac_srv.url()))

    # 首连全量对账（同进程两 store）
    rep = sd.reconcile_baseline(mac_store, cloud_store, "mac")
    assert rep["remote_only"] == []  # 云端是 Mac 子集
    assert len(rep["local_only"]) == 3  # r7/r8/r9 仅 Mac 有，推云端
    assert len(rep["conflicts"]) == 7  # r0..r6 两端都有，LWW 解决

    # 增量：Mac 新增 / 云端新增 / Mac 删除一条存量
    tmpl = dict(mac_store.live_items()[0].data)
    mac_eng.local_upsert("profile_items", "new-mac-1", {**tmpl, "content": "mac added", "topic": "tm"})
    cloud_eng.local_upsert("profile_items", "new-cloud-1", {**tmpl, "content": "cloud added", "topic": "tc"})
    del_target = sorted(r.record_id for r in mac_store.live_items())[0]
    mac_eng.local_delete("profile_items", del_target)

    # 多轮 tick（每轮经 HTTP 触发 server 子线程跨线程读/写 oplog）
    mac_w0 = mac_oplog.watermark()
    cloud_w0 = cloud_oplog.watermark()
    for _ in range(6):
        mac_daemon.tick()
        cloud_daemon.tick()

    final_mac = {r.record_id for r in mac_store.items()}
    final_cloud = {r.record_id for r in cloud_store.items()}
    assert final_mac == final_cloud, f"未收敛: mac={len(final_mac)} cloud={len(final_cloud)}"

    # 删除不复活
    dm = mac_store.get(del_target)
    dc = cloud_store.get(del_target)
    assert dm and dc and dm.tombstone and dc.tombstone

    # 新增两端都在
    assert "new-mac-1" in final_mac and "new-cloud-1" in final_mac

    # 回声不膨胀：6 轮后 oplog 水位 = 本端起源 + 接收对端起源（不无限增长）
    assert mac_oplog.watermark() == mac_w0 + 1  # 本端 2 条 + 接收 cloud 的 new-cloud-1
    assert cloud_oplog.watermark() == cloud_w0 + 2  # 本端 1 条 + 接收 mac 的 new-mac-1 + delete
    # 再跑 6 轮，水位应稳定（回声防护：对端转发回的 origin=本端 op 不回写本端 oplog）
    anchor_mac = mac_oplog.watermark()
    anchor_cloud = cloud_oplog.watermark()
    for _ in range(6):
        mac_daemon.tick()
        cloud_daemon.tick()
    assert mac_oplog.watermark() == anchor_mac
    assert cloud_oplog.watermark() == anchor_cloud

    mac_srv.stop()
    cloud_srv.stop()
    mc.close()
    cc.close()


def test_short_conn_sets_busy_timeout(tmp_path):
    """硬化核验：文件库短连接带 busy_timeout=5000（撞锁等待而非立即抛错）。"""
    import sqlite3
    f = str(tmp_path / "b.db")
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE profile_items (id TEXT PRIMARY KEY, layer TEXT, dimension TEXT, topic TEXT, content TEXT, category TEXT, status TEXT, confidence REAL, extra_json TEXT, first_extracted TEXT, last_updated TEXT, valid_from TEXT, valid_until TEXT)")
    c.commit()
    ss.migrate(c, "mac")
    store = ss.SQLiteSyncStore(c)
    with store._connect() as sc:
        assert sc.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    c.close()

