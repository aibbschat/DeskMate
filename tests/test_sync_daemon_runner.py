"""常驻运行器单测：断线重拨 / fail-open / 游标推进 / 优雅退出（不 SSH）。

聚焦 SyncNode 的循环逻辑（_ensure_tunnel / _tick_once / serve_forever），用假隧道与
假 daemon 注入，避免任何真实网络。SQLite 部分用 /tmp 临时库（migrate 幂等、可重建）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.sync_daemon_runner import SyncNode, _load_sync_modules  # noqa: E402


def _make_tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="syncnode_test_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE profile_items (id TEXT PRIMARY KEY, layer TEXT, content TEXT)")
    conn.commit()
    conn.close()
    return path


class FakeTunnel:
    """is_alive 可被外部翻转；记录 stop/start 调用次数。"""

    def __init__(self):
        self.alive = True
        self.starts = 0
        self.stops = 0

    def start(self):
        self.alive = True
        self.starts += 1

    def stop(self):
        self.alive = False
        self.stops += 1

    def is_alive(self):
        return self.alive


class FakeDaemon:
    """tick_safe 按序列返回 ok/fail；记录被推的 ops。"""

    def __init__(self, fail_until: int = 0):
        self._calls = 0
        self.fail_until = fail_until
        self.pushed = []

    def tick_safe(self):
        self._calls += 1
        if self._calls <= self.fail_until:
            return {"ok": False, "error": "boom", "pulled": 0, "pushed": 0}
        return {"ok": True, "error": None, "pulled": 1, "pushed": 1}


class FakeServer:
    def __init__(self):
        self.stopped = False
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def url(self):
        return "http://127.0.0.1:0"


@pytest.fixture
def node():
    """构造一个用假组件的 SyncNode（跳过真实 SQLite/SSH 编排）。"""
    db = _make_tmp_db()
    sd, sync_sql, st = _load_sync_modules("mac", REPO, "")
    n = SyncNode(
        "mac", sd, sync_sql, st,
        db_path=db, node="mac", interval=0.05,
        ssh_host="example.invalid", ssh_user="x", identity_file="/nope",
    )
    # 注入假组件
    n.tunnel = FakeTunnel()
    n.daemon = FakeDaemon()
    n.server = FakeServer()
    n._db = db
    yield n
    os.remove(db)


def test_tick_once_resets_fail_counter_on_ok(node):
    node.daemon = FakeDaemon(fail_until=2)
    node._tick_once()  # fail 1
    assert node._consecutive_fail == 1
    node._tick_once()  # fail 2
    assert node._consecutive_fail == 2
    node._tick_once()  # ok
    assert node._consecutive_fail == 0


def test_ensure_tunnel_reconnects_when_dead(node):
    node.tunnel.alive = False
    node._ensure_tunnel()
    assert node.tunnel.alive is True
    assert node.tunnel.starts == 1
    assert node.tunnel.stops == 1  # 先 stop 旧再 start


def test_ensure_tunnel_noop_when_alive(node):
    node.tunnel.alive = True
    before = node.tunnel.starts
    node._ensure_tunnel()
    assert node.tunnel.starts == before  # 未重拨


def test_serve_forever_exits_on_stop_and_shuts_down(node):
    node.daemon = FakeDaemon()  # 永远 ok，避免退避睡眠
    t = threading.Thread(target=node.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    node._stop = True
    t.join(timeout=3)
    assert not t.is_alive(), "serve_forever 应在 _stop 后退出"
    assert node.server.stopped is True
    assert node.tunnel.stops >= 1  # 退出时停下隧道


def test_serve_forever_backoff_does_not_spin_on_failure(node):
    """连续失败时退避封顶，不会空转（用短 interval + fail 验证能跑若干轮不卡死）。"""
    node.daemon = FakeDaemon(fail_until=1000)  # 一直失败
    node.interval = 0.05
    t = threading.Thread(target=node.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    node._stop = True
    t.join(timeout=3)
    # 0.5s 内最多约 1（首轮）+ 退避轮次；不会无限快循环
    assert node.daemon._calls < 50, "失败退避不应让 tick 空转"
