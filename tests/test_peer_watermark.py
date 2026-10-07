"""方案甲①「对端已拉水位」捎带链路测试（2026-10-07 萧哥拍板）。

链路：SyncDaemon.tick 在 push 前把本端 pull_after_seq 交给 transport →
HttpOplogTransport 以 X-Local-Pull-After-Seq 头 POST → 对端 do_POST 单调记入
cursor.peer_pull_after_seq → GC 用 max(push, peer_pull)（同为本端 seq 空间）。
动机：错峰窗口下 Mac push 冻结（16:02 实测云窗口仅 11s、Mac 首 tick 存量 ~5600 条
结构性完不成），GC 需要「对端已拉」这一独立证明；本端自己的 pull_after_seq 属对端
空间，绝不参与（复核意见 P2）。

全部 loopback/临时文件，**不碰活库**。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from deskmate.memory.sync_daemon import HLC, Op, OplogStore, SyncDaemon, SyncEngine  # noqa: E402
from deskmate.memory.sync_transport import (  # noqa: E402
    CursorStore,
    HttpOplogServer,
    HttpOplogTransport,
)


def _op(i: int = 1) -> Op:
    return Op(
        "profile_items", f"r{i}", "upsert",
        HLC(1_700_000_000_000 + i, 0, "cloud"),
        payload={"id": f"r{i}"}, origin="cloud",
    )


@pytest.fixture
def server(tmp_path):
    cs = CursorStore(str(tmp_path / "peer_cursor.json"))
    srv = HttpOplogServer(OplogStore(), host="127.0.0.1", port=0, cursor=cs)
    srv.start()
    yield srv, cs
    srv.stop()


def test_cursor_peer_pull_roundtrip(tmp_path):
    """peer_pull_after_seq 随游标 JSON 持久化；旧文件缺字段/损坏时回落 0。"""
    p = str(tmp_path / "c.json")
    cs = CursorStore(p)
    assert cs.peer_pull_after_seq == 0
    cs.push_after_seq = 1676
    cs.peer_pull_after_seq = 5198
    cs.save()
    d = json.load(open(p, encoding="utf-8"))
    assert d["peer_pull_after_seq"] == 5198
    cs2 = CursorStore(p)
    assert cs2.peer_pull_after_seq == 5198 and cs2.push_after_seq == 1676
    open(p, "w").write("{oops")
    cs3 = CursorStore(p)
    assert cs3.peer_pull_after_seq == 0


def test_push_carries_header_and_server_records_monotonic(server):
    """transport 捎带头 → 对端单调记 peer_pull_after_seq（只增不减、落盘）。"""
    srv, cs = server
    t = HttpOplogTransport(srv.url())
    t.local_pull_after_seq = 4321
    t.push([_op(1)])
    assert cs.peer_pull_after_seq == 4321
    assert json.load(open(cs.path, encoding="utf-8"))["peer_pull_after_seq"] == 4321
    t.local_pull_after_seq = 4200
    t.push([_op(2)])
    assert cs.peer_pull_after_seq == 4321
    t.local_pull_after_seq = None
    t.push([_op(3)])
    assert cs.peer_pull_after_seq == 4321
    assert len(srv.oplog.since(0)) == 3


def test_server_without_cursor_ignores_header():
    """server 未挂 cursor（旧部署/测试）→ 收到头也只忽略，绝不影响收 ops。"""
    srv = HttpOplogServer(OplogStore(), host="127.0.0.1", port=0)  # cursor=None
    srv.start()
    try:
        t = HttpOplogTransport(srv.url())
        t.local_pull_after_seq = 777
        t.push([_op(9)])  # 不应抛异常
        assert len(srv.oplog.since(0)) == 1
    finally:
        srv.stop()


def test_tick_sets_pull_watermark_on_transport_before_push(tmp_path):
    """SyncDaemon.tick 在 push 前把 pull_after_seq 写上 transport（捎带值的来源）。"""
    srv_cursor = CursorStore(str(tmp_path / "srv_cursor.json"))
    srv = HttpOplogServer(OplogStore(), host="127.0.0.1", port=0, cursor=srv_cursor)
    srv.start()
    try:
        my_cursor = CursorStore(str(tmp_path / "my_cursor.json"))
        my_cursor.pull_after_seq = 77
        my_cursor.save()
        t = HttpOplogTransport(srv.url())
        engine = SyncEngine({"profile_items": _MemStore()}, OplogStore(), "mac")
        d = SyncDaemon(engine, t, my_cursor)
        d.tick()
        assert t.local_pull_after_seq == 77, (
            "tick 必须在 push 前把 pull_after_seq 交给 transport 捎带"
        )
    finally:
        srv.stop()


class _MemStore:
    """最小内存 store：本测 oplog 为空，tick 不触碰业务读写。"""

    def __init__(self):
        self._d = {}

    def get(self, rid):
        return self._d.get(rid)

    def items(self, exclude_local=False):
        return list(self._d.values())

    def live_items(self, exclude_local=False):
        return [r for r in self._d.values() if not r.tombstone]
