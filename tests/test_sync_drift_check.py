"""sync_drift_check 记账链路测试（2026-10-07 复核 P3-①③ 修复的回归锁定）。

- ① `_record_drift` 必须先 import `health_producers` 完成注册，否则 `run_producer` 返回 0、
  零落库（云端实测复现 PRODUCERS={} → "已写入"假成功）；
- ③ run_producer 返回 0 时必须返回 False（不许再无条件报成功）；
- ②（口径）顺带源码断言：本端两侧都走 live_items(exclude_local=True)，与 /snapshot 同口径，
  否则墓碑恒被算成假差（808471a 已修，此处防回退）。

全部在 tmp 目录建台账，**不碰 ~/.deskmate 与云端活库**。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import sync_drift_check as mod  # noqa: E402


def test_record_drift_registers_producer_and_writes(tmp_path, monkeypatch):
    """修①：显式注册 + 真落库 → 返回 True，台账出现 sync_health/drift 行。"""
    monkeypatch.setattr(mod, "DESKMATE_DIR", str(tmp_path))
    ok = mod._record_drift({"profile_items": 3})
    assert ok is True, "应真落库并返回 True"
    db = tmp_path / "deskmate.db"
    assert db.exists(), "record_proactive 应已建台账库"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT source, kind, status FROM proactive_ledger WHERE source='sync_health'"
    ).fetchall()
    conn.close()
    assert rows and rows[0][0] == "sync_health" and rows[0][1] == "drift"


def test_record_drift_reports_failure_when_producer_returns_zero(tmp_path, monkeypatch):
    """修③：run_producer 返回 0（未登记/门槛/节流）必须报 False，绝不假成功。"""
    monkeypatch.setattr(mod, "DESKMATE_DIR", str(tmp_path))
    from deskmate.proactivity import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "run_producer", lambda *a, **k: 0)
    assert mod._record_drift({"profile_items": 3}) is False, "n=0 时不得报成功"


def test_drift_both_sides_use_live_scope():
    """口径防回退（P3-②）：本端必须 live_items(exclude_local=True)；源码断言防墓碑假差回退。"""
    src = open(mod.__file__, encoding="utf-8").read()
    assert "live_items(exclude_local=True)" in src, (
        "本端必须用 live_items(exclude_local=True)（与 /snapshot 同口径），"
        "否则墓碑恒被算成本端独有的假差"
    )
    assert "stores[t].items()" not in src, "本端不得用 items()（含墓碑侧表）"
