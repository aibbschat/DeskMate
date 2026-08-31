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
