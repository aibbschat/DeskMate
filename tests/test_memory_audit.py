"""deskmate/memory/audit.py 的体检判据测试。

原则：判据必须能**区分新旧实现**（锁目标行为，不是绿了就行）：
- 每个已知坑形态（日记固化 / gap 断粮 / 维度混用 / FTS 落后 / 无日期不衰减）
  都要有一条「种病 → 报出来」的用例；
- 健康库必须零误报；
- 只读保证：体检前后库文件逐字节不变；
- fail-open：坏库文件不让 run_audit 抛异常。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deskmate.memory.audit import run_audit

# ---------------------------------------------------------------------------
# 造库工具：只造体检查询会碰到的最小 schema
# ---------------------------------------------------------------------------
_EXT_SCHEMA = """
CREATE TABLE external_items (
    id TEXT, ingestion_status TEXT,
    embedding BLOB, embedding_model TEXT, embedding_dim INTEGER
);
"""
_PROFILE_SCHEMA = """
CREATE TABLE profile_items (id TEXT, layer TEXT, status TEXT, confidence REAL, valid_until TEXT);
CREATE TABLE cognitive_gaps (id TEXT, layer TEXT, status TEXT);
"""
_HIST_SCHEMA = """
CREATE TABLE articles (id INTEGER);
CREATE TABLE chunks (id INTEGER);
CREATE TABLE chunks_fts (id INTEGER);
"""


def _mk_db(path: Path, schema: str, rows: list[tuple[str, list]] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(schema)
    for sql, params in rows:
        con.execute(sql, params)
    con.commit()
    con.close()
    return path


def _status_of(report: dict, key: str) -> dict:
    return next(c for c in report["checks"] if c["key"] == key)


def _msgs(report: dict) -> list[str]:
    return [p["message"] for p in report["problems"]]


# ---------------------------------------------------------------------------
# 健康基线：零误报
# ---------------------------------------------------------------------------
def test_clean_install_zero_problems(tmp_path: Path):
    base = tmp_path / "dm"
    base.mkdir()
    _mk_db(base / "deskmate.db", _EXT_SCHEMA + _PROFILE_SCHEMA)
    (base / "traces").mkdir()
    (base / "traces" / "inject.jsonl").write_text('{"ts":"t"}\n', encoding="utf-8")
    (base / "MEMORY.md").write_text("§ 偏好 #preference\n- 2026-09-01: 早起写作\n",
                                    encoding="utf-8")
    report = run_audit(base)
    assert report["problems"] == [], json.dumps(report["problems"], ensure_ascii=False)
    assert all(c["status"] == "ok" for c in report["checks"])


def test_all_missing_is_ok_not_crash(tmp_path: Path):
    """空目录（首次安装形态）：不崩，向量/画像层标 fail+note，不算问题。"""
    report = run_audit(tmp_path / "nothing")
    assert "problems" in report
    v = _status_of(report, "vector")
    assert v["status"] == "fail" and v["note"]


# ---------------------------------------------------------------------------
# 第 1 层：向量健康
# ---------------------------------------------------------------------------
def test_digested_without_vectors_flagged(tmp_path: Path):
    """09-22 实锤指纹：digested>0 且 0 向量 = 既搜不到也补不上。"""
    base = tmp_path / "dm"
    _mk_db(base / "deskmate.db", _EXT_SCHEMA, [
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("a", "digested", None, "", 0)),
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("b", "digested", None, "", 0)),
    ])
    report = run_audit(base)
    assert any("既搜不到也补不上" in m for m in _msgs(report))
    assert _status_of(report, "vector")["status"] == "error"


def test_dim_mixing_flagged(tmp_path: Path):
    """换后端未重刷全表 → 维度混用 → cosine 静默返 0。"""
    base = tmp_path / "dm"
    blob = b"\x00" * 8
    _mk_db(base / "deskmate.db", _EXT_SCHEMA, [
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("a", "digested", blob, "bge", 384)),
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("b", "digested", blob, "openai", 512)),
    ])
    report = run_audit(base)
    assert any("维度混用" in m for m in _msgs(report))


def test_healthy_vectors_not_flagged(tmp_path: Path):
    base = tmp_path / "dm"
    blob = b"\x00" * 4
    _mk_db(base / "deskmate.db", _EXT_SCHEMA, [
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("a", "digested", blob, "bge-small-zh-v1.5", 384)),
    ])
    report = run_audit(base)
    assert _status_of(report, "vector")["status"] == "ok"


# ---------------------------------------------------------------------------
# 第 2 层：画像健康
# ---------------------------------------------------------------------------
def test_zero_open_gaps_flagged_as_starvation(tmp_path: Path):
    """gap 全 0 = 访谈断粮的静默失败信号（conflict_gap.py 顶部说明）。"""
    base = tmp_path / "dm"
    _mk_db(base / "deskmate.db", _PROFILE_SCHEMA, [
        ("INSERT INTO profile_items VALUES (?,?,?,?,?)",
         ("i1", "cognitive", "active", 0.8, None)),
    ])
    report = run_audit(base)
    assert any("无燃料" in m for m in _msgs(report))


def test_open_gaps_present_not_flagged(tmp_path: Path):
    base = tmp_path / "dm"
    _mk_db(base / "deskmate.db", _PROFILE_SCHEMA, [
        ("INSERT INTO profile_items VALUES (?,?,?,?,?)",
         ("i1", "cognitive", "active", 0.8, None)),
        ("INSERT INTO cognitive_gaps VALUES (?,?,?)",
         ("g1", "cognitive", "open")),
    ])
    report = run_audit(base)
    assert not any("无燃料" in m for m in _msgs(report))


def test_expired_but_active_flagged(tmp_path: Path):
    base = tmp_path / "dm"
    _mk_db(base / "deskmate.db", _PROFILE_SCHEMA, [
        ("INSERT INTO profile_items VALUES (?,?,?,?,?)",
         ("i1", "cognitive", "active", 0.8, "2020-01-01T00:00:00")),
    ])
    report = run_audit(base)
    assert any("valid_until 已过" in m for m in _msgs(report))


# ---------------------------------------------------------------------------
# 第 3 层：全局记忆
# ---------------------------------------------------------------------------
def test_diary_section_regression_flagged(tmp_path: Path):
    """红线：日记绝不落 MEMORY.md——出现 § 日记沉淀 即固化 bug 复发。"""
    base = tmp_path / "dm"
    base.mkdir()
    (base / "MEMORY.md").write_text(
        "§ 工作 #work\n- 2026-09-01: 干活\n\n§ 日记沉淀\n- 2026-09-25: 今天他写了\n",
        encoding="utf-8")
    report = run_audit(base)
    assert any("日记固化" in m for m in _msgs(report))
    assert _status_of(report, "global_memory")["status"] == "error"


def test_undated_decay_entries_flagged(tmp_path: Path):
    """#work 分节下无日期前缀的行按 decay_memory 语义永不衰减。"""
    base = tmp_path / "dm"
    base.mkdir()
    (base / "MEMORY.md").write_text(
        "§ 工作 #work\n- 2026-09-01: 有日期的\n没日期的条目\n",
        encoding="utf-8")
    report = run_audit(base)
    assert any("无日期前缀" in m for m in _msgs(report))
    assert _status_of(report, "global_memory")["detail"]["decay_entries_missing_date"] == 1


def test_preference_section_ignored_by_decay_check(tmp_path: Path):
    """非衰减分节（#preference）无日期不算问题——它们本来就不衰减。"""
    base = tmp_path / "dm"
    base.mkdir()
    (base / "MEMORY.md").write_text("§ 偏好 #preference\n偏好深夜写作\n", encoding="utf-8")
    report = run_audit(base)
    assert not any("无日期前缀" in m for m in _msgs(report))


# ---------------------------------------------------------------------------
# 第 4 层：检索可达
# ---------------------------------------------------------------------------
def test_fts_lag_flagged(tmp_path: Path):
    """chunks 比 chunks_fts 多 = 「记了查不到」。"""
    base = tmp_path / "dm"
    db = _mk_db(base / "history" / "private-写作.db", _HIST_SCHEMA)
    con = sqlite3.connect(str(db))
    con.executemany("INSERT INTO chunks VALUES (?)", [(i,) for i in range(5)])
    con.executemany("INSERT INTO chunks_fts VALUES (?)", [(i,) for i in range(3)])
    con.commit()
    con.close()
    report = run_audit(base)
    assert any("记了查不到" in m for m in _msgs(report))
    assert _status_of(report, "reachability")["status"] == "error"


def test_reachability_covers_private_dbs(tmp_path: Path):
    base = tmp_path / "dm"
    _mk_db(base / "history" / "private-写作.db", _HIST_SCHEMA)
    report = run_audit(base)
    assert "private-写作.db" in _status_of(report, "reachability")["detail"]["dbs"]


# ---------------------------------------------------------------------------
# 第 5 层：溯源完整
# ---------------------------------------------------------------------------
def test_missing_traces_flagged(tmp_path: Path):
    report = run_audit(tmp_path)
    assert any("溯源" in m or "inject.jsonl" in m for m in _msgs(report))


def test_empty_inject_trace_flagged(tmp_path: Path):
    base = tmp_path
    (base / "traces").mkdir()
    (base / "traces" / "inject.jsonl").write_text("", encoding="utf-8")
    report = run_audit(base)
    assert any("inject.jsonl" in m for m in _msgs(report))


# ---------------------------------------------------------------------------
# 通用保证
# ---------------------------------------------------------------------------
def test_audit_is_read_only(tmp_path: Path):
    """体检前后库文件逐字节不变（mode=ro 保证）。"""
    base = tmp_path / "dm"
    db = _mk_db(base / "deskmate.db", _EXT_SCHEMA + _PROFILE_SCHEMA, [
        ("INSERT INTO external_items VALUES (?,?,?,?,?)",
         ("a", "raw", None, "", 0)),
    ])
    before = db.read_bytes()
    run_audit(base)
    assert db.read_bytes() == before


def test_fail_open_on_corrupt_db(tmp_path: Path):
    base = tmp_path / "dm"
    base.mkdir()
    (base / "deskmate.db").write_bytes(b"not a sqlite file at all")
    report = run_audit(base)  # 不应抛异常
    keys = {c["key"] for c in report["checks"]}
    assert {"vector", "profile", "global_memory", "reachability", "traceability"} <= keys


def test_base_dir_none_resolves_deskmate_dir(tmp_path: Path, monkeypatch):
    """base_dir=None 时必须调用时解析 DESKMATE_DIR（测试隔离红线）。"""
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "envbase"))
    report = run_audit(None)
    assert report["base_dir"] == str(tmp_path / "envbase")


def test_report_is_json_serializable(tmp_path: Path):
    report = run_audit(tmp_path)
    json.dumps(report, ensure_ascii=False)  # 不抛即通过
