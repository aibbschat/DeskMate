"""sync_transport.py 单测：SSH 反隧道 transport 端到端 + 游标持久化 + fail-open。

仅跑协议与传输逻辑，不碰生产库、不起真 SSH（回环 HTTP 模拟隧道两端）。
"""

import time

from deskmate.memory.sync_daemon import (
    MemoryStore,
    OplogStore,
    SyncDaemon,
    SyncEngine,
    Transport,
)
from deskmate.memory.sync_transport import (
    CursorStore,
    HttpOplogServer,
    HttpOplogTransport,
    SshTunnel,
)


def _pair():
    """起一对回环 HTTP oplog 端点 + 双 daemon（回环即隧道两端的替身）。"""
    mac_oplog, cloud_oplog = OplogStore(), OplogStore()
    mac_store, cloud_store = MemoryStore(), MemoryStore()
    mac_engine = SyncEngine(mac_store, mac_oplog, "mac")
    cloud_engine = SyncEngine(cloud_store, cloud_oplog, "cloud")
    mac_srv = HttpOplogServer(mac_oplog, port=0)
    mac_port = mac_srv.start()
    cloud_srv = HttpOplogServer(cloud_oplog, port=0)
    cloud_port = cloud_srv.start()
    mac_daemon = SyncDaemon(mac_engine, HttpOplogTransport(f"http://127.0.0.1:{cloud_port}"))
    cloud_daemon = SyncDaemon(cloud_engine, HttpOplogTransport(f"http://127.0.0.1:{mac_port}"))
    return {
        "mac": (mac_engine, mac_daemon, mac_srv),
        "cloud": (cloud_engine, cloud_daemon, cloud_srv),
    }


def test_full_loopback_sync_converges():
    grp = _pair()
    mac_engine, mac_daemon, mac_srv = grp["mac"]
    cloud_engine, cloud_daemon, cloud_srv = grp["cloud"]
    try:
        # 两端各写一条 -> 两轮 tick 双向收敛
        mac_engine.local_upsert("mem", "r1", {"v": 1})
        cloud_engine.local_upsert("mem", "r2", {"v": 2})
        mac_daemon.tick()
        cloud_daemon.tick()
        # 收敛验收：按 uuid 逐条比，不只数总量
        assert mac_engine.store.get("r1") is not None
        assert mac_engine.store.get("r2") is not None
        assert cloud_engine.store.get("r1") is not None
        assert cloud_engine.store.get("r2") is not None
        rep = mac_engine.reconcile(cloud_engine.store)
        assert rep["converged"], rep
    finally:
        mac_srv.stop()
        cloud_srv.stop()


def test_delete_propagates_and_stays_gone():
    grp = _pair()
    mac_engine, mac_daemon, mac_srv = grp["mac"]
    cloud_engine, cloud_daemon, cloud_srv = grp["cloud"]
    try:
        mac_engine.local_upsert("mem", "r1", {"v": 1})
        cloud_engine.local_upsert("mem", "r2", {"v": 2})
        mac_daemon.tick()
        cloud_daemon.tick()
        # Mac 删 r1 -> 推到云端 -> 云端也消失
        mac_engine.local_delete("mem", "r1")
        mac_daemon.tick()
        cloud_daemon.tick()
        assert mac_engine.store.get("r1").tombstone is True
        assert cloud_engine.store.get("r1").tombstone is True
        assert "r1" not in {r.record_id for r in mac_engine.store.live_items()}
        assert "r1" not in {r.record_id for r in cloud_engine.store.live_items()}
    finally:
        mac_srv.stop()
        cloud_srv.stop()


def test_lww_resolves_to_later_writer():
    grp = _pair()
    mac_engine, mac_daemon, mac_srv = grp["mac"]
    cloud_engine, cloud_daemon, cloud_srv = grp["cloud"]
    try:
        # 同一 uuid 两端各写一次；cloud 晚 5ms -> HLC 更晚 -> 后者胜
        mac_engine.local_upsert("mem", "rx", {"v": "mac-first"})
        time.sleep(0.005)
        cloud_engine.local_upsert("mem", "rx", {"v": "cloud-later"})
        mac_daemon.tick()
        cloud_daemon.tick()
        assert mac_engine.store.get("rx").data["v"] == "cloud-later"
        assert cloud_engine.store.get("rx").data["v"] == "cloud-later"
    finally:
        mac_srv.stop()
        cloud_srv.stop()


def test_ssh_tunnel_args_are_secure_and_dial_out_only():
    t = SshTunnel(host="81.69.163.170", user="xiaoju")
    args = t.build_args()
    line = " ".join(args)
    # 反隧道：远端只绑回环，绝不 0.0.0.0（防公网监听到同步端口）
    assert "-R" in args
    assert "127.0.0.1:8765:127.0.0.1:8765" in args
    assert "0.0.0.0" not in line
    # 本地转发：也是回环
    assert "-L" in args
    assert "127.0.0.1:8766:127.0.0.1:8765" in args
    # 出网会话属性：只拨出、断线即退、需鉴权目标
    assert "-N" in args
    assert "ExitOnForwardFailure=yes" in line
    assert "ServerAliveInterval=30" in line
    assert args[-1] == "xiaoju@81.69.163.170"


def test_ssh_tunnel_identity_file_and_port():
    t = SshTunnel(
        host="81.69.163.170", user="xiaoju", ssh_port=2222,
        identity_file="~/.ssh/id_ed25519",
    )
    args = t.build_args()
    assert "-p" in args and "2222" in args
    assert "-i" in args and "~/.ssh/id_ed25519" in args


def test_cursor_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "cursor.json")
    c = CursorStore(p)
    c.pull_after_seq = 7
    c.push_after_seq = 4
    c.save()
    c2 = CursorStore(p)
    assert c2.pull_after_seq == 7
    assert c2.push_after_seq == 4


def test_cursor_corrupt_fails_open_to_zero(tmp_path):
    p = str(tmp_path / "cursor.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    c = CursorStore(p)
    assert c.pull_after_seq == 0
    assert c.push_after_seq == 0


class _RaisingTransport(Transport):
    def pull_since(self, after_seq):
        raise ConnectionError("tunnel down")

    def push(self, ops):
        raise ConnectionError("tunnel down")


def test_daemon_tick_safe_is_fail_open():
    engine = SyncEngine(MemoryStore(), OplogStore(), "mac")
    daemon = SyncDaemon(engine, _RaisingTransport())
    res = daemon.tick_safe()
    assert res["ok"] is False
    assert "error" in res and res["error"]
    # 同步失败不能污染本端数据
    assert engine.store.live_items() == []

def test_no_oplog_echo_loop_over_loopback():
    """回归（review SSH transport 发现）：对端 POST 进来的事件带 origin，
    本端 apply 跳过回声、tick 只推本端起源事件；连续多轮两端 oplog 不得增长。"""
    grp = _pair()
    mac_engine, mac_daemon, mac_srv = grp["mac"]
    cloud_engine, cloud_daemon, cloud_srv = grp["cloud"]
    try:
        mac_engine.local_upsert("mem", "r1", {"v": 1})
        for _ in range(6):
            mac_daemon.tick_safe()
            cloud_daemon.tick_safe()
        assert mac_engine.oplog.watermark() == 1
        assert cloud_engine.oplog.watermark() == 1
        assert len(mac_engine.store.live_items()) == 1
        assert len(cloud_engine.store.live_items()) == 1
    finally:
        mac_srv.stop()
        cloud_srv.stop()
