"""深度访谈引擎（③b）hermetic 测试——不碰真实 ~/.deskmate。

隔离方式：把 CognitiveStore 默认指向临时库、重置 manager 单例（用缓存单例模拟
真实环境的全局单例，避免 stub 提取与 resolve 用不同 manager 实例互相全量覆盖）、
stub 掉真实 CognitiveExtractor（explore 模式提取新认知时写临时库），每个用例用完还原。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import deskmate.cognitive.store as storemod
import deskmate.cognitive.profile as pmod
import deskmate.cognitive.interview as ival
from deskmate.cognitive.models import CognitiveProfile, Stance, CognitiveGap, SourceRef, _now


def _hermetic():
    """把认知层全部默认指向临时库，返回 saved 供还原。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"

    class _TmpStore(storemod.CognitiveStore):
        def __init__(self, *args, **kwargs):
            kwargs["db_path"] = db
            super().__init__(*args, **kwargs)

    saved = {
        "store_CognitiveStore": storemod.CognitiveStore,
        "profile_CognitiveStore": pmod.CognitiveStore,
        "profile_default_manager": pmod._default_manager,
        "profile_gpm": pmod.get_profile_manager,
        "ival_CognitiveStore": ival.CognitiveStore,
        "ival_gpm": ival.get_profile_manager,
        "ival_get_extractor": ival.get_extractor,
    }
    storemod.CognitiveStore = _TmpStore
    pmod.CognitiveStore = _TmpStore
    pmod._default_manager = None

    # 缓存单例：模拟真实环境的全局 _default_manager，确保 process_response 内的
    # manager 与 stub extract 内的 manager 是同一实例（不会互相全量覆盖）
    def _gpm():
        if pmod._default_manager is None:
            pmod._default_manager = pmod.CognitiveProfileManager()
        return pmod._default_manager

    pmod.get_profile_manager = _gpm
    ival.CognitiveStore = _TmpStore
    ival.get_profile_manager = _gpm

    class _StubExtractor:
        """explore 模式提取新认知：直接 add 一条新 stance 到临时库（规则等价）。"""

        def extract_from_text(self, text, **kw):
            pm = pmod.get_profile_manager()
            pm.add_stance(
                topic="访谈新知", content=text,
                source=SourceRef(type="interview", title="深度访谈", excerpt=text[:80]),
            )
            return {"items": 1}

    ival.get_extractor = lambda: _StubExtractor()
    pmod.get_profile_manager().save()  # 初始化空库
    return saved


def _restore(saved):
    storemod.CognitiveStore = saved["store_CognitiveStore"]
    pmod.CognitiveStore = saved["profile_CognitiveStore"]
    pmod._default_manager = saved["profile_default_manager"]
    pmod.get_profile_manager = saved["profile_gpm"]
    ival.CognitiveStore = saved["ival_CognitiveStore"]
    ival.get_profile_manager = saved["ival_gpm"]
    ival.get_extractor = saved["ival_get_extractor"]


def _make_profile():
    """构造含三类薄弱点的测试画像：questioned 条目 / 两个 open gap（有/无 related_items）/ 低置信 active。"""
    p = CognitiveProfile()
    s1 = Stance(topic="矛盾立场", content="旧说法", status="questioned")
    p.stances.append(s1)
    g1 = CognitiveGap(topic="矛盾待澄清", reason="矛盾", status="open", related_items=[s1.id])
    p.cognitive_gaps.append(g1)
    g2 = CognitiveGap(topic="新领域", reason="从未提及", status="open", related_items=[])
    p.cognitive_gaps.append(g2)
    low = Stance(topic="深化主题", content="原内容", status="active", confidence=0.3)
    p.stances.append(low)
    return p, s1, g1, g2, low


class TestInterview(unittest.TestCase):
    def test_generate_questions_covers_three_modes(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            qs = ival.SocraticInterviewEngine().generate_questions(p, limit=10)
            modes = {q["mode"] for q in qs}
            self.assertIn("clarify", modes)
            self.assertIn("explore", modes)
            self.assertIn("deepen", modes)
            # 去重：同 topic 只一条
            self.assertEqual(len({q["topic"] for q in qs}), len(qs))
        finally:
            _restore(saved)

    def test_clarify_calibrates_and_resolves_gap(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            qid = ival._encode_question_id(s1.id, g1.id, "clarify")
            ival.SocraticInterviewEngine().process_response(
                pm.profile, qid, "我真正的立场是 X，只是情境不同才显得矛盾")
            pm2 = pmod.get_profile_manager()
            s1_now = [s for s in pm2.profile.stances if s.id == s1.id][0]
            self.assertEqual(s1_now.status, "active")
            g1_now = [g for g in pm2.profile.cognitive_gaps if g.id == g1.id][0]
            self.assertEqual(g1_now.status, "resolved")
        finally:
            _restore(saved)

    def test_explore_extracts_and_resolves_gap(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            qid = ival._encode_question_id(None, g2.id, "explore")
            ival.SocraticInterviewEngine().process_response(
                pm.profile, qid, "我写作时特别在意人物动机是否合乎历史逻辑")
            pm2 = pmod.get_profile_manager()
            self.assertTrue(any(st.topic == "访谈新知" for st in pm2.profile.stances))
            g2_now = [g for g in pm2.profile.cognitive_gaps if g.id == g2.id][0]
            self.assertEqual(g2_now.status, "resolved")
        finally:
            _restore(saved)

    def test_deepen_adds_source_no_fake_gap(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            before = len(pmod.get_profile_manager().profile.cognitive_gaps)
            qid = ival._encode_question_id(low.id, None, "deepen")
            ival.SocraticInterviewEngine().process_response(
                pm.profile, qid, "深化补充：我更看重人物的内在挣扎")
            pm2 = pmod.get_profile_manager()
            low_now = [s for s in pm2.profile.stances if s.id == low.id][0]
            self.assertGreaterEqual(len(low_now.sources), 1)
            self.assertEqual(low_now.status, "active")
            # 关键：deepen 不 spawn 假 gap（绕开 add_stance 矛盾检测）
            self.assertEqual(len(pm2.profile.cognitive_gaps), before)
        finally:
            _restore(saved)

    def test_should_interview_now_conservative(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            eng = ival.SocraticInterviewEngine()
            # 已有 2 个 open gap → 应触发
            self.assertTrue(eng.should_interview_now(p))
            # 设 last_interview_at=今天 → 冷却期内不触发
            storemod.CognitiveStore().set_meta("last_interview_at", _now())
            self.assertFalse(eng.should_interview_now(p))
        finally:
            _restore(saved)

    def test_start_answer_roundtrip(self):
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            started = ival.start_interview(limit=3)
            self.assertTrue(started["session_id"])
            self.assertGreaterEqual(started["total"], 1)
            # 逐题回答，最后一题答完应标记 session_done
            last = None
            for q in started["questions"]:
                last = ival.answer_interview(started["session_id"], q["id"], "访谈回答内容")
            self.assertTrue(last["session_done"])
            sess = [s for s in storemod.CognitiveStore().list_sessions()
                    if s["id"] == started["session_id"]][0]
            self.assertEqual(sess["status"], "done")
        finally:
            _restore(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
