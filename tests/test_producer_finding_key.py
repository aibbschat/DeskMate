"""方案 D（2026-10-09）：同一处发现不再换措辞就多一封。

台账的确定性 id 早就支持「同 id 合并回原行」，但 ``cloud_audit`` 过去**不传 anchor**，
于是 id 退化成内容指纹——judge 下次把同一处矛盾说成另一句话，就成了新的一封信
（实测：本机 10-08 一天 95 行，``plot_contradiction`` 两天攒 34 封）。
这组测试钉住补上的那条锚点链：judge 出 ``finding_key`` → 归一 → 当 anchor → 合并。
"""

from __future__ import annotations

import sqlite3

from deskmate.proactivity.judge import JudgmentResult
from deskmate.proactivity.ledger import pending_items, run_producer
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


def test_no_anchor_contradiction_rejected(tmp_path):
    """R0：矛盾类（plot_contradiction）无锚点 → 经 producer 产出环节拒收，不落台账、不造重复行。

    这正是「同问题被重判 34 次」的止血点：模型不给 finding_key 时，宁可拒收，
    也不退回 content-hash 把重复搬进库。把关在 run_producer 的条目产出环节
    （见 ledger._ANCHOR_REQUIRED_KINDS）：直接调 record_proactive 的夹具不受影响。
    """
    _deliver(tmp_path, _j("", "第二章末写十月十七日雪，第十二章却说十月初初见雪。"))
    _deliver(tmp_path, _j("", "两处初雪的时间对不上：第二章是十七日，第十二章写的是月初。"))
    # 全部被 producer 拒收 → 不会触发建表；pending_items 对缺表优雅返回 []，等价于 0 行。
    assert len(pending_items(tmp_path)) == 0, "矛盾类无锚点不应落库"


def test_anchor_required_reject_does_not_affect_anchored(tmp_path):
    """有锚点的矛盾类仍正常落库并合并（R0 只拦无锚点的）。"""
    _deliver(tmp_path, _j("ch2-ch12", "第二章末写十月十七日雪，第十二章却说十月初初见雪。"))
    _deliver(tmp_path, _j("ch2-ch12", "两处初雪的时间对不上：第二章是十七日，第十二章写的是月初。"))
    assert len(_rows(tmp_path)) == 1


def test_non_anchor_kind_fallback_preserved(tmp_path):
    """非矛盾类（如 ops 类 task_anomaly）无锚点仍走旧兜底：同内容合并、不同内容各一行。"""
    _deliver(tmp_path, _j("", "定时任务 backup 在 14:00 执行失败", kind="task_anomaly"))
    _deliver(tmp_path, _j("", "定时任务 backup 在 14:00 执行失败", kind="task_anomaly"))
    assert len(_rows(tmp_path)) == 1
    _deliver(tmp_path, _j("", "另一项定时任务 sync 在 15:00 也失败了", kind="task_anomaly"))
    assert len(_rows(tmp_path)) == 2


def test_non_anchor_kind_different_content_stays_separate(tmp_path):
    """非矛盾类无锚点、措辞不同 → 各一行（不模糊合并，避免误杀不同事项）。"""
    _deliver(tmp_path, _j("", "定时任务 backup 在 14:00 执行失败", kind="task_anomaly"))
    _deliver(tmp_path, _j("", "定时任务 backup 于 14:00 跑挂了", kind="task_anomaly"))
    rows = _rows(tmp_path)
    assert len(rows) == 2, f"措辞不同的同 kind 无锚点项应各留一行，实际 {len(rows)} 行"


# ---- 2026-10-10 手机端 kind 白名单（堵 R0 绕过口）----


def test_mobile_kind_whitelist_blocks_contradiction_kinds():
    """**回归**：手机端不得直写矛盾类 kind。

    背景：R0 把「无锚点矛盾类拒收」上移到 run_producer，而 capture 端点的 kind
    直接取自 HTTP body 且无校验 ⇒ 手机端可传 kind=plot_contradiction 直落矛盾类，
    anchor=None，绕过拒收。实测可产出 mobile_voice:plot_contradiction:973f653b… 。
    """
    from deskmate.proactivity.ledger import (
        _ANCHOR_REQUIRED_KINDS,
        MOBILE_KINDS,
        is_mobile_kind_allowed,
    )

    # 白名单与矛盾类清单**必须零交集**——这是本测试的核心断言
    assert not (MOBILE_KINDS & _ANCHOR_REQUIRED_KINDS)
    # 正常 kind 放行
    assert is_mobile_kind_allowed("idea_capture") is True
    assert is_mobile_kind_allowed("topic_note") is True
    # 空值放行（落库口径同：空 → idea_capture）
    assert is_mobile_kind_allowed(None) is True
    assert is_mobile_kind_allowed("") is True
    assert is_mobile_kind_allowed("   ") is True
    # 矛盾类全拒
    for k in _ANCHOR_REQUIRED_KINDS:
        assert is_mobile_kind_allowed(k) is False, k


def test_mobile_kind_whitelist_rejects_unknown_kind():
    """未知 kind 也应拒（白名单而非黑名单：新增矛盾类时自动覆盖）。"""
    from deskmate.proactivity.ledger import is_mobile_kind_allowed

    assert is_mobile_kind_allowed("plot_contradiction") is False
    assert is_mobile_kind_allowed("opening") is False
    assert is_mobile_kind_allowed("draft") is False
    assert is_mobile_kind_allowed("rm -rf /") is False
    assert is_mobile_kind_allowed("IDEA_CAPTURE") is False  # 大小写敏感，不静默归一
