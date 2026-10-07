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


# ---------------------------------------------------------------------------
# 日历型 tick（2026-10-07）：对齐云端 :02 错峰窗口
# ---------------------------------------------------------------------------


class _CalRunner:
    """抽出 _next_slot_delta 做纯逻辑验证（不起 daemon/不碰真库）。"""
    interval = 15.0

    def __init__(self, slots):
        self.window_slots = slots


def _load_delta():
    import re
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_daemon_runner.py"),
        encoding="utf-8",
    ).read()
    m = re.search(r"def _next_slot_delta\(self.*?\n(?=    def )", src, re.S)
    ns = {}
    exec(m.group(0), ns)
    return ns["_next_slot_delta"]


def test_next_slot_delta_targets_window_minute():
    """tick 落在「每小时的 :02:04」：等待按小时内偏移算，不是每日钟点。"""
    import datetime

    f = _load_delta()
    d = _CalRunner(["02:04"])
    # 01:00:00 → 本小时 :02:04 = 2 分 4 秒
    assert f(d, datetime.datetime(2026, 10, 7, 1, 0, 0)) == 2 * 60 + 4
    # 01:02:00 → 还差 4 秒
    assert f(d, datetime.datetime(2026, 10, 7, 1, 2, 0)) == 4
    # 恰好差 1 秒
    assert f(d, datetime.datetime(2026, 10, 7, 1, 2, 3)) == 1


def test_next_slot_delta_rolls_over_to_next_hour():
    """过了本小时的时刻 → 回绕到下一小时同一刻；任何时刻等待都 < 1 小时（每小时都打）。"""
    import datetime

    f = _load_delta()
    d = _CalRunner(["02:04"])
    # 01:02:05 已过点 → 距下一小时 :02:04
    assert f(d, datetime.datetime(2026, 10, 7, 1, 2, 5)) == 3600 - 125 + 124
    # 每小时都打：任意时刻的等待都落在 (0, 3600]
    for h in range(24):
        delta = f(d, datetime.datetime(2026, 10, 7, h, 30, 0))
        assert 0 < delta <= 3600, (h, delta)


def test_next_slot_delta_multiple_slots_picks_nearest():
    """多时刻表（每小时 :02:04 与 :04:30）→ 取本小时内最近的那个未过时刻。"""
    import datetime

    f = _load_delta()
    d = _CalRunner(["02:04", "04:30"])
    # 01:00:00 → 最近的是 :02:04
    assert f(d, datetime.datetime(2026, 10, 7, 1, 0, 0)) == 2 * 60 + 4
    # 01:02:05 → :02:04 已过，下一个是 :04:30
    assert f(d, datetime.datetime(2026, 10, 7, 1, 2, 5)) == (4 * 60 + 30) - (2 * 60 + 5)


def test_next_slot_delta_invalid_config_falls_back_to_interval():
    """时刻表写坏时不崩，退回固定间隔（fail-safe）。"""
    import datetime

    f = _load_delta()
    d = _CalRunner(["坏格式"])
    assert f(d, datetime.datetime(2026, 10, 7, 1, 0, 0)) == d.interval
    d2 = _CalRunner([])
    assert f(d2, datetime.datetime(2026, 10, 7, 1, 0, 0)) == d2.interval
    d3 = _CalRunner(["99:99"])  # 超出一小时范围 → 忽略
    assert f(d3, datetime.datetime(2026, 10, 7, 1, 0, 0)) == d3.interval


def test_argparser_exposes_window_calendar_flags():
    """CLI 暴露 --window-calendar / --window-slots，且默认值对齐云端窗口。"""
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "sync_daemon_runner.py"
    )
    spec = importlib.util.spec_from_file_location("_sdr_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = mod._build_argparser().parse_args(
        ["--side", "mac", "--db-path", "/tmp/x.db", "--window-calendar"]
    )
    assert args.window_calendar is True
    assert args.window_slots == "02:04"
    # 默认不开日历型（保持旧行为，显式开启才生效）
    args2 = mod._build_argparser().parse_args(
        ["--side", "mac", "--db-path", "/tmp/x.db"]
    )
    assert args2.window_calendar is False
