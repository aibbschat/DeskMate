"""情感层 1→2→3 链路测试：花园情绪定色 → CareToast 画像上下文 → 情感访谈。"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import SourceRef
from deskmate.cognitive.emotion_profile import (
    mood_tint_for_event,
    emotional_context,
    ensure_emotional_gaps,
    care_preferences,
)
from deskmate.cognitive.interview import maybe_trigger_interview


def _seed_emotional_profile(pm):
    """写入情感画像数据（触发点/回血方式）。"""
    pm.add_stance(
        "情感触发·写作受阻",
        "写作卡住、找不到感觉时情绪明显低落，创作受阻是他主要的负面情绪来源。",
        SourceRef(type="diary", title="d1", excerpt="卡住了很烦"),
        tags=["情感层", "emotional_trigger", "negative", "受阻"],
        category="情感触发点",
    )
    pm.add_stance(
        "情感触发·写作顺畅",
        "写作顺利、章节写顺时情绪明显变好，创作进入状态是最愉悦的时刻。",
        SourceRef(type="diary", title="d2", excerpt="写顺了真好"),
        tags=["情感层", "emotional_trigger", "positive", "顺畅"],
        category="情感触发点",
    )
    pm.add_stance(
        "回血方式",
        "情绪恢复靠投入创作/史料：重读旧稿找回感觉、查史料、安静独处。",
        SourceRef(type="diary", title="d3", excerpt="重读史料找回感觉"),
        tags=["情感层", "recovery_pattern", "史料"],
        category="回血方式",
    )
    pm.save()


def _make_env_db():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"
    pm = CognitiveProfileManager(db, layer="emotional")
    _seed_emotional_profile(pm)
    os.environ["DESKMATE_EMOTION_PROFILE_PATH"] = str(db)
    return tmp, db, pm


def test_garden_mood_tint_reads_profile():
    """第1步：花园情绪定色应读画像（有负向触发 → user_corrected 标低落）。"""
    tmp, db, pm = _make_env_db()
    try:
        # user_corrected：画像有负向触发 → 低落（不再写死"成长"）
        assert mood_tint_for_event("user_corrected") == "低落"
        # skill_evolved：画像有正向触发 → 顺利
        assert mood_tint_for_event("skill_evolved") == "顺利"
        # 未桥接事件：兜底默认
        assert mood_tint_for_event("report") == "顺利"
    finally:
        os.environ.pop("DESKMATE_EMOTION_PROFILE_PATH", None)
        import shutil
        shutil.rmtree(tmp)
    print("OK test_garden_mood_tint_reads_profile")


def test_emotional_context_for_care():
    """第2步：情感上下文应包含画像（触发点/回血），供 judge 个性化关怀。"""
    tmp, db, pm = _make_env_db()
    try:
        ctx = emotional_context()
        assert "关于他的情感" in ctx
        assert "写作受阻" in ctx or "创作受阻" in ctx
        assert "回血" in ctx or "史料" in ctx
    finally:
        os.environ.pop("DESKMATE_EMOTION_PROFILE_PATH", None)
        import shutil
        shutil.rmtree(tmp)
    print("OK test_emotional_context_for_care")


def test_emotional_interview_gaps_and_layer():
    """第3步：情感访谈——ensure gaps 后，情感层应有 open gap 且可发起访谈。"""
    tmp, db, pm = _make_env_db()
    try:
        # ensure gaps 幂等补 explore 型 gap（测试显式注入 pm）
        added = ensure_emotional_gaps(pm=pm)
        assert len(added) >= 1, added
        assert len(pm.profile.open_gaps()) >= 1

        # 二次调用幂等（meta 标记后不再补）
        added2 = ensure_emotional_gaps(pm=pm)
        assert len(added2) == 0, added2
    finally:
        os.environ.pop("DESKMATE_EMOTION_PROFILE_PATH", None)
        import shutil
        shutil.rmtree(tmp)
    print("OK test_emotional_interview_gaps_and_layer")


def test_gaps_drive_interview_question():
    """链路验证：有了情感 gap，访谈能产出「关怀偏好」问题（explore 型）。"""
    tmp, db, pm = _make_env_db()
    try:
        ensure_emotional_gaps(pm=pm)
        from deskmate.cognitive.interview import SocraticInterviewEngine
        engine = SocraticInterviewEngine()
        questions = engine.generate_questions(pm.profile, limit=5)
        topics = [q["topic"] for q in questions]
        assert any("关怀" in t for t in topics), topics
        assert any(q["mode"] == "explore" for q in questions)
    finally:
        os.environ.pop("DESKMATE_EMOTION_PROFILE_PATH", None)
        import shutil
        shutil.rmtree(tmp)
    print("OK test_gaps_drive_interview_question")


def test_emotional_answer_roundtrip_to_care_preference():
    """闭环：情感访谈 explore 回答 → 回灌成 care_preference 维度条目 → 可被读取。

    采用 hermetic 模式（mock 全局 store 到临时库），process_response 内部拿到的
    manager 与测试是同一实例。
    """
    import deskmate.cognitive.store as storemod
    import deskmate.cognitive.profile as pmod
    import deskmate.cognitive.interview as ival

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"

    class _TmpStore(storemod.CognitiveStore):
        def __init__(self, *args, **kwargs):
            kwargs["db_path"] = db
            super().__init__(*args, **kwargs)

    saved = {
        "store_CognitiveStore": storemod.CognitiveStore,
        "profile_CognitiveStore": pmod.CognitiveStore,
        "profile_default_managers": pmod._default_managers,
        "profile_gpm": pmod.get_profile_manager,
        "ival_CognitiveStore": ival.CognitiveStore,
        "ival_gpm": ival.get_profile_manager,
    }
    storemod.CognitiveStore = _TmpStore
    pmod.CognitiveStore = _TmpStore
    ival.CognitiveStore = _TmpStore

    def _gpm(profile_path=None, layer="cognitive"):
        if profile_path is not None:
            return pmod.CognitiveProfileManager(profile_path, layer=layer)
        key = layer or "cognitive"
        if key not in pmod._default_managers:
            pmod._default_managers[key] = pmod.CognitiveProfileManager(layer=key)
        return pmod._default_managers[key]

    pmod.get_profile_manager = _gpm
    ival.get_profile_manager = _gpm

    try:
        pm = pmod.get_profile_manager(layer="emotional")
        _seed_emotional_profile(pm)
        pm.save()

        # 初始化情感 gap
        ensure_emotional_gaps(pm=pm)
        engine = ival.SocraticInterviewEngine()
        questions = engine.generate_questions(pm.profile, limit=5)
        # 找到「关怀偏好」的 explore 问题
        care_q = next(q for q in questions if "关怀" in q["topic"] and q["mode"] == "explore")

        # 走真实入口 answer_interview（含 layer 回灌）
        result = ival.answer_interview(
            session_id="iv_test_dummy",
            question_id=care_q["id"],
            response="我其实更喜欢自己待着回血，不用总是来问我进度。",
            layer="emotional",
        )

        # 该 gap 应已 resolved
        gap = next(g for g in pm.profile.cognitive_gaps if g.id == care_q["related_gap_id"])
        assert gap.status == "resolved", gap.status

        # 回答应回灌成 care_preference 维度条目（带 care_preference tag）
        os.environ["DESKMATE_EMOTION_PROFILE_PATH"] = str(db)
        cps = care_preferences()
        assert len(cps) >= 1, cps
        assert any("独处" in c["content"] or "回血" in c["content"] for c in cps), cps
    finally:
        os.environ.pop("DESKMATE_EMOTION_PROFILE_PATH", None)
        storemod.CognitiveStore = saved["store_CognitiveStore"]
        pmod.CognitiveStore = saved["profile_CognitiveStore"]
        pmod._default_managers = saved["profile_default_managers"]
        pmod.get_profile_manager = saved["profile_gpm"]
        ival.CognitiveStore = saved["ival_CognitiveStore"]
        ival.get_profile_manager = saved["ival_gpm"]
        import shutil
        shutil.rmtree(tmp)
    print("OK test_emotional_answer_roundtrip_to_care_preference")


if __name__ == "__main__":
    test_garden_mood_tint_reads_profile()
    test_emotional_context_for_care()
    test_emotional_interview_gaps_and_layer()
    test_gaps_drive_interview_question()
    print("全部情感链路测试通过")
