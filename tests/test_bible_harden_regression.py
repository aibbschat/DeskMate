"""圣经加固回归测试（P0-1/2/3/4 + P1-5/6/7 的确定性验证，不烧 LLM）。

对应 Hermes 巡检报告修复项。所有用例用内存/临时库 + 桩替换 _harvest，
只验证「写库/合并/审阅/落盘」的正确性，不依赖真实模型。
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from deskmate.novel_memory import entities as ent
from deskmate.novel_memory import core as nc


def _mk_record(kind, name, status=ent.STATUS_CONFIRMED, tier=ent.TIER_MANUSCRIPT,
               needs_review=False, author_confirmed=False, evidence="", seq=1):
    r = ent.Record(kind=kind, name=name, status=status, tier=tier,
                   needs_review=needs_review, author_confirmed=author_confirmed,
                   evidence=evidence, source_seq=seq)
    return r


def _fresh_bible():
    d = Path(tempfile.mkdtemp(prefix="bible_harden_"))
    bd = d / ".deskmate"
    bd.mkdir()
    return d, bd


# --------------------------------------------------------------------------- #
# P0-1：build_bible 抽空不清除旧圣经
# --------------------------------------------------------------------------- #
def test_build_bible_empty_passed_keeps_old_data(monkeypatch):
    d, bd = _fresh_bible()
    # 先写一条真实数据（模拟已有圣经）
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "叶鸣秋", tier=ent.TIER_MANUSCRIPT)])
    c.close()

    # 桩：_harvest 返回空（模拟 LLM 全失败/超时）
    monkeypatch.setattr(nc, "_harvest", lambda *a, **k: ([], {"raw": 0}))

    with pytest.raises(ValueError):
        nc.build_bible(str(d))

    # 旧数据必须还在，未被 DELETE 清空
    c = ent.connect(bd)
    rows = c.execute("SELECT * FROM entities").fetchall()
    c.close()
    assert len(rows) == 1, "P0-1 失败：抽空后旧圣经被清空"
    assert rows[0]["name"] == "叶鸣秋"


# --------------------------------------------------------------------------- #
# P0-2：作者批准（accept）收敛，不被后续回流打回待审
# --------------------------------------------------------------------------- #
def test_review_accept_converges_and_sticks(monkeypatch):
    d, bd = _fresh_bible()
    rid = "character:回流丁"
    c = ent.connect(bd)
    # 一条对话回流来的待确认条目
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "回流丁", tier=ent.TIER_DIALOGUE,
                                       needs_review=True, evidence="回流丁登场", seq=1)])
    c.close()

    c = ent.connect(bd)
    assert ent.review_resolve(c, rid, accept=True)
    row = c.execute("SELECT * FROM entities WHERE id=?", (rid,)).fetchone()
    c.close()
    assert row["status"] == ent.STATUS_CONFIRMED
    assert bool(row["author_confirmed"]) is True
    assert bool(row["needs_review"]) is False

    # 模拟后续回流：同一实体被对话批次再次命中（confirmed，dialogue tier）
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "回流丁", tier=ent.TIER_DIALOGUE,
                                       needs_review=True, evidence="回流丁登场", seq=2)])
    row = c.execute("SELECT * FROM entities WHERE id=?", (rid,)).fetchone()
    c.close()
    # 关键：不再被打回待审队列
    assert bool(row["needs_review"]) is False, "P0-2 失败：批准后被回流重新置为待审"
    assert bool(row["author_confirmed"]) is True, "P0-2 失败：author_confirmed 被覆盖"


# --------------------------------------------------------------------------- #
# P0-3：作者驳回（reject）不被后续回流复活
# --------------------------------------------------------------------------- #
def test_review_reject_not_revived(monkeypatch):
    d, bd = _fresh_bible()
    rid = "character:周管事"
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "周管事", tier=ent.TIER_MANUSCRIPT,
                                       evidence="周管事", seq=1)])
    c.close()

    c = ent.connect(bd)
    assert ent.review_resolve(c, rid, accept=False)
    row = c.execute("SELECT * FROM entities WHERE id=?", (rid,)).fetchone()
    assert row["status"] == ent.STATUS_REJECTED
    c.close()

    # 后续回流：新批次核验为 confirmed，旧行已是 rejected → 不应复活
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "周管事", tier=ent.TIER_MANUSCRIPT,
                                       status=ent.STATUS_CONFIRMED, evidence="周管事", seq=2)])
    row = c.execute("SELECT * FROM entities WHERE id=?", (rid,)).fetchone()
    c.close()
    assert row["status"] == ent.STATUS_REJECTED, "P0-3 失败：驳回条目被回流复活"


# --------------------------------------------------------------------------- #
# P0-4：内容回流不刷 last_scan_ts（避免漏抽书稿）
# --------------------------------------------------------------------------- #
def test_reflow_does_not_touch_last_scan_ts(monkeypatch):
    d, bd = _fresh_bible()
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "叶鸣秋", tier=ent.TIER_MANUSCRIPT)])
    ent.set_meta(c, "last_scan_ts", "111.0")
    c.close()

    # 桩 _harvest：返回一条对话回流
    monkeypatch.setattr(nc, "_harvest", lambda *a, **k: (
        [_mk_record(ent.KIND_CHARACTER, "回流戊", tier=ent.TIER_DIALOGUE,
                    needs_review=True, evidence="回流戊", seq=2)], {"raw": 1}))

    nc.upsert_bible_from_text(str(d), "回流戊 出场", source_label="测试产出")
    c = ent.connect(bd)
    assert ent.get_meta(c, "last_scan_ts") == "111.0", "P0-4 失败：回流改写了 last_scan_ts"
    assert ent.get_meta(c, "last_reflow_ts"), "回流应记录 last_reflow_ts"
    c.close()


# --------------------------------------------------------------------------- #
# P1-5：verify_text 骤降时跳过全库复核（不误杀）
# --------------------------------------------------------------------------- #
def test_revoke_guarded_on_short_verify_text():
    d, bd = _fresh_bible()
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "真人物甲", tier=ent.TIER_MANUSCRIPT,
                                       evidence="真人物甲", seq=1)])
    ent.set_meta(c, "last_verify_chars", "100000")  # 上次全文很长
    c.close()

    # 本次 verify_text 骤短（< 50%）→ 应跳过复核，真人物甲不被误杀
    c2 = ent.connect(bd)
    revoked = nc._guard_revoke(c2, "短文本只占几个字")
    c2.close()
    assert revoked == [], "P1-5 失败：骤短 verify_text 仍触发复核"
    c = ent.connect(bd)
    row = c.execute("SELECT * FROM entities WHERE name=?", ("真人物甲",)).fetchone()
    c.close()
    assert row["status"] == ent.STATUS_CONFIRMED, "P1-5 失败：真实人物被骤短误杀"


# --------------------------------------------------------------------------- #
# P1-7：增量更新落盘四份 md（不再停在全量版本）
# --------------------------------------------------------------------------- #
def test_incremental_updates_md_files(monkeypatch):
    d, bd = _fresh_bible()
    c = ent.connect(bd)
    ent.upsert_records(c, [_mk_record(ent.KIND_CHARACTER, "叶鸣秋", tier=ent.TIER_MANUSCRIPT)])
    c.close()
    nc.inject_bible(str(d), nc.read_bible(str(d)))  # 先落一次全量 md

    # 桩 _harvest：返回新角色
    monkeypatch.setattr(nc, "_harvest", lambda *a, **k: (
        [_mk_record(ent.KIND_CHARACTER, "新角色己", tier=ent.TIER_MANUSCRIPT,
                    evidence="新角色己", seq=5)], {"raw": 1}))
    # 放一份书稿文本，避免增量被 mtime/空文本短路；且让它比 last_scan 新
    novel = d / "novel.txt"
    novel.write_text("叶鸣秋 立于阵前。新角色己 自帐后转出。", encoding="utf-8")

    # 强制 last_scan_ts 早于文件 mtime
    c = ent.connect(bd)
    ent.set_meta(c, "last_scan_ts", "1.0")
    c.close()

    nc.incremental_bible_update(str(d))
    # md 必须反映新角色（不再陈旧）
    chars_md = (bd / "CHARACTERS.md").read_text(encoding="utf-8")
    assert "新角色己" in chars_md, "P1-7 失败：增量更新未落盘 md"


# --------------------------------------------------------------------------- #
# P1-6：原子写临时文件名带 uuid（不固定 .tmp）
# --------------------------------------------------------------------------- #
def test_atomic_write_uses_uuid_tmp(monkeypatch):
    import os
    captured = {}
    orig_replace = os.replace

    def spy_replace(src, dst):
        captured["tmp"] = Path(src).name
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    p = Path(tempfile.mkdtemp()) / "CHARACTERS.md"
    nc._atomic_write_text(p, "内容")
    assert ".tmp" in captured["tmp"] and len(captured["tmp"]) > len("CHARACTERS.md.tmp")
    assert p.read_text(encoding="utf-8") == "内容"
