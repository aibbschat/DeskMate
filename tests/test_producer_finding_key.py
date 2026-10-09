"""方案 D（2026-10-09）：同一处发现不再换措辞就多一封。

台账的确定性 id 早就支持「同 id 合并回原行」，但 ``cloud_audit`` 过去**不传 anchor**，
于是 id 退化成内容指纹——judge 下次把同一处矛盾说成另一句话，就成了新的一封信
（实测：本机 10-08 一天 95 行，``plot_contradiction`` 两天攒 34 封）。
这组测试钉住补上的那条锚点链：judge 出 ``finding_key`` → 归一 → 当 anchor → 合并。
"""

from __future__ import annotations

import sqlite3

from deskmate.proactivity.judge import JudgmentResult
from deskmate.proactivity.ledger import run_producer
from deskmate.proactivity.tick import _cloud_audit_producer, _finding_anchor


def _j(key: str, content: str, kind: str = "plot_contradiction") -> JudgmentResult:
    return JudgmentResult(
        should_act=True,
        confidence=0.88,
        suggestion_type=kind,
        suggestion_content=content,
        reasoning="依据：章节比对",
        finding_key=key,
    )


def _deliver(base, judgment) -> int:
    """走真链路：注册表里的 cloud_audit producer → record_producer 落库。"""
    return run_producer("cloud_audit", ctx={"judgment": judgment}, base_dir=base)


def _rows(base):
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        return c.execute(
            "SELECT id, content, created_at FROM proactive_ledger"
        ).fetchall()


# -- 归一：模型两次写法不同，锚点必须一样 ------------------------------------------------


def test_finding_anchor_normalizes_writing_variants():
    variants = ["Ch2-Ch12", "ch2 - ch12", " ch2/ch12 ", "ch2、ch12", "ch2_ch12"]
    assert {_finding_anchor(_j(v, "x")) for v in variants} == {"ch2-ch12"}


def test_finding_anchor_falls_back_when_absent():
    assert _finding_anchor(_j("", "x")) is None
    assert _finding_anchor(_j("  ,, ", "x")) is None  # 全是标点也算没给


def test_producer_passes_anchor_through():
    """producer 自己就得带上 anchor（不靠调用方补）。"""
    assert _cloud_audit_producer(_j("Ch2 - Ch12", "两处初雪对不上"))[0]["anchor"] == "ch2-ch12"
    assert _cloud_audit_producer(_j("", "没给 key"))[0]["anchor"] is None


# -- 合并：同一处问题换个说法，只留一行 --------------------------------------------------


def test_same_finding_key_merges_into_one_row(tmp_path):
    assert _deliver(tmp_path, _j("ch2-ch12", "第二章末写十月十七日雪，第十二章却说十月初初见雪。")) == 1
    # 换个措辞、key 写法也不同 → 归一后同 anchor → 同 id → 合并，不新增行
    _deliver(tmp_path, _j("ch2 - ch12", "两处初雪的时间对不上：第二章是十七日，第十二章写的是月初。"))
    rows = _rows(tmp_path)
    assert len(rows) == 1, f"同一处发现被写成了 {len(rows)} 封"
    assert "月初" in rows[0][1]  # 内容更新成最新一次的说法


def test_merge_keeps_first_created_at(tmp_path):
    """合并时 created_at 不刷新——日上限按它分日计数，刷新等于重置额度。"""
    _deliver(tmp_path, _j("ch2-ch12", "第一版说法"))
    before = _rows(tmp_path)[0][2]
    _deliver(tmp_path, _j("ch2-ch12", "第二版说法"))
    assert _rows(tmp_path)[0][2] == before


def test_different_findings_stay_separate(tmp_path):
    for key, kind in (
        ("ch2-ch12", "plot_contradiction"),
        ("ch7:伏笔-玉佩", "missing_foreshadow"),
        ("人物:姑射", "character_inconsistency"),
    ):
        _deliver(tmp_path, _j(key, f"关于 {key} 的发现", kind=kind))
    assert len(_rows(tmp_path)) == 3


def test_no_key_still_behaves_as_before(tmp_path):
    """模型不给 key → 退回内容指纹：同内容仍合并，不同内容仍各一行（不比以前差）。"""
    _deliver(tmp_path, _j("", "完全相同的一句话"))
    _deliver(tmp_path, _j("", "完全相同的一句话"))
    assert len(_rows(tmp_path)) == 1
    _deliver(tmp_path, _j("", "另一句不同的话"))
    assert len(_rows(tmp_path)) == 2
