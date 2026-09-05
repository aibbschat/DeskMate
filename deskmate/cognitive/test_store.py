"""
CognitiveStore / CognitiveProfileManager 测试。

覆盖：
- 真实 cognitive_profile.json 迁移到 deskmate.db（往返一致 + 幂等）
- manager 往返持久化
- 矛盾检测 spawn cognitive_gap 并持久化
- calibrate 解 gap 并持久化
- 访谈骨架（session/qa/last_interview_at）
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.store import CognitiveStore
from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import (
    CognitiveProfile,
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    CognitiveGap,
    SourceRef,
)


def _real_json() -> Path:
    return Path.home() / ".deskmate" / "cognitive_profile.json"


def test_migrate_real_json():
    if not _real_json().exists():
        print("SKIP test_migrate_real_json（无真实 cognitive_profile.json）")
        return
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    store = CognitiveStore(db_path=db, legacy_json=_real_json())
    res = store.migrate_from_legacy(_real_json())
    assert res["migrated"] is True, res
    profile = store.load_profile()
    orig = CognitiveProfile.from_json(_real_json().read_text(encoding="utf-8"))
    assert len(profile.stances) == len(orig.stances), (len(profile.stances), len(orig.stances))
    assert len(profile.argument_patterns) == len(orig.argument_patterns)
    assert len(profile.value_rankings) == len(orig.value_rankings)
    assert len(profile.thinking_frameworks) == len(orig.thinking_frameworks)
    assert len(profile.cognitive_gaps) == len(orig.cognitive_gaps)
    # 幂等：二次迁移应跳过
    res2 = store.migrate_from_legacy(_real_json())
    assert res2["migrated"] is False, res2
    import shutil

    shutil.rmtree(tmp)
    print(f"OK test_migrate_real_json：迁移 {res['items']} 条目 + {res['gaps']} 盲区，往返一致")


def test_roundtrip_manager():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db)
    s, op = pm.add_stance("历史真实性", "大事不虚，小事不拘", SourceRef(type="vault", title="创作谈"))
    assert op == "created"
    pm2 = CognitiveProfileManager(db)
    assert len(pm2.get().stances) == 1
    assert pm2.get().stances[0].content == "大事不虚，小事不拘"
    assert pm2.get().stances[0].sources[0].title == "创作谈"
    import shutil

    shutil.rmtree(tmp)
    print("OK test_roundtrip_manager")


def test_contradiction_spawns_gap():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db)
    pm.add_stance("AI 创作", "AI 创作是工具，值得拥抱", SourceRef(type="vault", title="x"))
    pm.add_stance("AI 创作", "AI 创作必须拒绝，损害原创", SourceRef(type="vault", title="y"))
    assert any("AI 创作" in g.topic for g in pm.get().cognitive_gaps), "矛盾应 spawn gap"
    pm2 = CognitiveProfileManager(db)
    assert any("AI 创作" in g.topic for g in pm2.get().cognitive_gaps)
    import shutil

    shutil.rmtree(tmp)
    print("OK test_contradiction_spawns_gap")


def test_same_template_repeat_no_fake_gap():
    """回归：同模板文本（含否定词）重复提取不得误判为矛盾、不得 spawn 假 gap。

    真实案例：日记规则提取「隐私与安全感」用的是固定模板（含「不愿对外暴露心事」
    的「不」字）。用户写多篇触发同一规则的日记 → 同模板重复 add_stance，
    旧实现因模板带否定词 + 新旧关键词重合被误判 contradiction →
    产生 3 条同款 stance + 1 个僵尸 gap。本用例锁定修复。
    """
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db)
    template = "把小桔日记视为与 AI 共享的私密花园，强调内容只有小桔可见、有安全感；" \
               "重视私密表达空间，不愿对外暴露心事。"
    # 三篇不同日记触发同一规则模板 → 三次 add_stance
    for i in range(3):
        s, op = pm.add_stance(
            "隐私与安全感", template,
            SourceRef(type="diary", title=f"diary/p{i}", excerpt=template[:50], date="2026-09-01"),
            tags=["关系", "隐私", "安全感"],
        )
        assert op == "updated" if i > 0 else op == "created", (i, op)
        assert s.status == "active", f"第 {i+1} 次提取不应被标 questioned"
    # 不 spawn 假 gap
    assert len(pm.get().cognitive_gaps) == 0, f"不应生成伪 gap，实际 {len(pm.get().cognitive_gaps)}"
    # 只有一条 active stance（补充来源而非新增）
    active = [s for s in pm.get().stances if s.status == "active" and s.topic == "隐私与安全感"]
    assert len(active) == 1, f"应只有 1 条 active，实际 {len(active)}"
    assert len(active[0].sources) == 3, "三次提取应累积 3 个来源"
    import shutil

    shutil.rmtree(tmp)
    print("OK test_same_template_repeat_no_fake_gap：同模板重复提取不误判矛盾、不 spawn 伪 gap")


def test_repair_fake_duplicate_gaps():
    """存量清理：合并历史伪矛盾重复立场 + 消解僵尸 gap（幂等，不碰真实矛盾）。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db)
    template = "把小桔日记视为与 AI 共享的私密花园，强调内容只有小桔可见、有安全感；" \
               "重视私密表达空间，不愿对外暴露心事。"

    # 模拟旧版 bug 产生的脏数据：同 topic 三条同内容 stance（2 questioned + 1 active）
    s1, _ = pm.add_stance("隐私与安全感", template,
                          SourceRef(type="diary", title="diary/p1", excerpt=template[:50], date="2026-09-01"))
    # 手动构造：第二条同内容被误判 questioned（复刻旧 bug 产物）
    s2 = Stance(topic="隐私与安全感", content=template, category="立场",
                tags=["关系", "隐私", "安全感"], status="questioned")
    s2.add_source(SourceRef(type="diary", title="diary/p2", excerpt=template[:50], date="2026-09-02"))
    pm.profile.stances.append(s2)
    s3 = Stance(topic="隐私与安全感", content=template, category="立场",
                tags=["关系", "隐私", "安全感"], status="questioned")
    s3.add_source(SourceRef(type="diary", title="diary/p3", excerpt=template[:50], date="2026-09-03"))
    pm.profile.stances.append(s3)
    # 僵尸 gap 指向 s2/s3
    pm.profile.cognitive_gaps.append(CognitiveGap(
        topic="立场矛盾：隐私与安全感",
        reason="伪矛盾", priority=0.8, related_items=[s2.id, s3.id], status="open",
    ))
    # 另加一条真实矛盾（不应被误清理）
    pm.profile.stances.append(Stance(
        topic="真实矛盾", content="AI 创作是工具", category="立场", status="questioned"))
    pm.profile.stances.append(Stance(
        topic="真实矛盾", content="AI 创作必须拒绝", category="立场", status="questioned"))
    pm.profile.cognitive_gaps.append(CognitiveGap(
        topic="立场矛盾：真实矛盾", reason="真矛盾", priority=0.8,
        related_items=[pm.profile.stances[-2].id, pm.profile.stances[-1].id], status="open",
    ))
    pm.save()

    res = pm.repair_fake_duplicate_gaps()
    assert len(res["merged_stances"]) >= 2, res
    assert len(res["resolved_gaps"]) == 1, res
    # 隐私与安全感只剩一条 active 主条目
    act = [s for s in pm.get().stances
           if s.topic == "隐私与安全感" and s.status == "active"]
    assert len(act) == 1, f"应只剩 1 条 active，实际 {len(act)}"
    assert len(act[0].sources) == 3, "主条目应吸收 3 个来源"
    # 僵尸 gap 已消解
    g = [g for g in pm.get().cognitive_gaps if g.topic == "立场矛盾：隐私与安全感"][0]
    assert g.status == "resolved"
    # 真实矛盾原样保留
    real = [g for g in pm.get().cognitive_gaps if g.topic == "立场矛盾：真实矛盾"][0]
    assert real.status == "open", "真实矛盾不得被误清理"
    # 幂等：再跑一次无副作用
    res2 = pm.repair_fake_duplicate_gaps()
    assert res2["merged_stances"] == [] and res2["resolved_gaps"] == [], res2
    import shutil

    shutil.rmtree(tmp)
    print("OK test_repair_fake_duplicate_gaps：存量伪矛盾合并+消解，幂等，不碰真实矛盾")


def test_calibrate_resolves_gap():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db)
    s, _ = pm.add_stance("测试立场", "原内容", SourceRef(type="vault", title="x"))
    sid = s.id
    pm.add_gap("测试盲区", "原因", priority=0.7, related_items=[sid])
    gid = pm.get().cognitive_gaps[0].id
    pm.calibrate(sid, "澄清后的立场", feedback="用户校准")
    assert any(g.id == gid and g.status == "resolved" for g in pm.get().cognitive_gaps)
    pm2 = CognitiveProfileManager(db)
    assert any(g.id == gid and g.status == "resolved" for g in pm2.get().cognitive_gaps)
    import shutil

    shutil.rmtree(tmp)
    print("OK test_calibrate_resolves_gap")


def test_interview_skeleton():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    store = CognitiveStore(db)
    sid = store.create_session(gap_ids=["g1"])
    assert store.get_meta("last_interview_at"), "应写 last_interview_at"
    store.add_qa(sid, "你如何看待真实与好看?", "我认为真实优先", item_updates=["stance_1"])
    assert len(store.list_sessions()) == 1
    assert store.list_sessions()[0]["id"] == sid
    qa = store.get_qa(sid)
    assert len(qa) == 1 and qa[0]["answer"] == "我认为真实优先"
    store.finish_session(sid, "summary")
    assert store.list_sessions()[0]["status"] == "done"
    import shutil

    shutil.rmtree(tmp)
    print("OK test_interview_skeleton")


if __name__ == "__main__":
    test_migrate_real_json()
    test_roundtrip_manager()
    test_contradiction_spawns_gap()
    test_calibrate_resolves_gap()
    test_interview_skeleton()
    print("\nALL PASSED")


# ---------------------------------------------------------------------------
# 回归：无类型论证模式不得被并进第一条
# ---------------------------------------------------------------------------


def test_add_argument_pattern_untyped_not_merged():
    """pattern_type 两边都为空时不能算「同类」。

    回归背景：曾写成 ``p.pattern_type == pattern_type``，两侧同为 "" 时短路为真，
    任何无类型的新模式都会被并进第一条，新内容直接丢失（返回 updated 而非 created）。
    LLM 提取常常不给 pattern_type，所以这条路径是常态。
    """
    tmp = Path(tempfile.mkdtemp())
    pm = CognitiveProfileManager(profile_path=tmp / "deskmate.db")
    src = SourceRef(type="vault", title="t1", excerpt="e1")

    _, op1 = pm.add_argument_pattern(
        "先摆史料，再讲结论，最后留一个开放问题。", source=src, pattern_type=""
    )
    _, op2 = pm.add_argument_pattern(
        "遇到有争议的说法，先查原始出处再下判断。", source=src, pattern_type=""
    )

    assert op1 == "created"
    assert op2 == "created", "第二条不同内容的无类型模式不应被合并进第一条"
    patterns = pm.get().argument_patterns
    assert len(patterns) == 2
    saved = {p.content for p in patterns}
    assert "先摆史料，再讲结论，最后留一个开放问题。" in saved
    assert "遇到有争议的说法，先查原始出处再下判断。" in saved


def test_add_argument_pattern_typed_still_merges():
    """同类 pattern_type 仍然照旧合并（补来源而非新增条目），修复不能矫枉过正。"""
    tmp = Path(tempfile.mkdtemp())
    pm = CognitiveProfileManager(profile_path=tmp / "deskmate.db")
    src = SourceRef(type="vault", title="t1", excerpt="e1")

    _, op1 = pm.add_argument_pattern(
        "先摆史料再讲结论。", source=src, pattern_type="史证优先"
    )
    _, op2 = pm.add_argument_pattern(
        "结论必须能被史料反推回去。", source=src, pattern_type="史证优先"
    )

    assert op1 == "created"
    assert op2 == "updated"
    assert len(pm.get().argument_patterns) == 1


def test_add_argument_pattern_same_content_merges():
    """内容前 20 字一致的无类型模式仍然合并（去重语义不变）。"""
    tmp = Path(tempfile.mkdtemp())
    pm = CognitiveProfileManager(profile_path=tmp / "deskmate.db")
    src = SourceRef(type="vault", title="t1", excerpt="e1")

    content = "遇到有争议的说法，先查原始出处再下判断，不凭二手转述下结论。"
    _, op1 = pm.add_argument_pattern(content, source=src, pattern_type="")
    _, op2 = pm.add_argument_pattern(content, source=src, pattern_type="")

    assert op1 == "created"
    assert op2 == "updated"
    assert len(pm.get().argument_patterns) == 1


# ---------------------------------------------------------------------------
# 回归：变更历史与 gap 澄清时间必须真实落库
# ---------------------------------------------------------------------------


def test_status_change_records_previous_status():
    """历史必须记下变更**前**的状态。

    回归背景：mark_questioned 曾先赋值 self.status 再取 old_value=self.status，
    历史恒为 questioned→questioned，查不出条目原本是什么状态；mark_superseded
    则把 old_value 写死成 "active"，被取代的存疑条目会记错。
    """
    s = Stance(content="史料真实性优先")
    assert s.status == "active"
    s.mark_questioned(reason="与另一条冲突")
    assert s.history[-1].old_value == "active"
    assert s.history[-1].new_value == "questioned"

    s2 = Stance(content="另一条立场")
    s2.mark_questioned(reason="存疑")
    s2.mark_superseded("new-id", reason="立场更新")
    assert s2.history[-1].old_value == "questioned", "被取代的也可能是存疑条目，不能写死 active"
    assert s2.history[-1].new_value == "superseded"


def test_gap_resolved_at_persists():
    """gap 的 resolved_at 必须持久化。

    回归背景：CognitiveGap 没有该字段、persist 硬编码写 ""、load 也不 SELECT，
    调用方（访谈澄清 / 校准）赋值后重启即丢，澄清时间无从追溯。
    """
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(profile_path=db)
    pm.add_gap("写不动的时候怎么回血", reason="从未提及", priority=0.8)

    gap = pm.get().cognitive_gaps[0]
    gap.status = "resolved"
    gap.resolved_at = "2026-09-05T10:00:00"
    pm.save()

    reloaded = CognitiveProfileManager(profile_path=db).get().cognitive_gaps[0]
    assert reloaded.status == "resolved"
    assert reloaded.resolved_at == "2026-09-05T10:00:00", "澄清时间应真落库，而非空串"


def test_gap_without_resolved_at_roundtrips():
    """未澄清的 gap 往返后 resolved_at 仍为空串（不引入脏值）。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(profile_path=db)
    pm.add_gap("深夜写作是否影响次日状态", reason="数据不足", priority=0.6)

    reloaded = CognitiveProfileManager(profile_path=db).get().cognitive_gaps[0]
    assert reloaded.status == "open"
    assert reloaded.resolved_at == ""
