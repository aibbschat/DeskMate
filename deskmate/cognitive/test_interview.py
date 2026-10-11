"""深度访谈引擎（③b）hermetic 测试——不碰真实 ~/.deskmate。

隔离方式：把 CognitiveStore 默认指向临时库、重置 manager 单例（用缓存单例模拟
真实环境的全局单例，避免 stub 提取与 resolve 用不同 manager 实例互相全量覆盖）、
stub 掉真实 CognitiveExtractor（explore 模式提取新认知时写临时库），每个用例用完还原。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import deskmate.cognitive.store as storemod
import deskmate.cognitive.profile as pmod
import deskmate.cognitive.interview as ival
import deskmate.memory.llm as llm_mod
from deskmate.cognitive.models import CognitiveProfile, Stance, CognitiveGap, SourceRef, _now


def _hermetic():
    """把认知层**与统一根目录**全部指向临时库，返回 saved 供还原。"""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "deskmate.db"

    class _TmpStore(storemod.CognitiveStore):
        def __init__(self, *args, **kwargs):
            kwargs["db_path"] = db
            super().__init__(*args, **kwargs)

    saved = {
        "store_CognitiveStore": storemod.CognitiveStore,
        "profile_CognitiveStore": pmod.CognitiveStore,
        # ⚠ 必须是**快照副本**，不能是原对象引用：_hermetic 期间 _gpm 会往这个 dict
        # 里塞 manager 实例；若存的是引用，_restore 再赋回去时旧实例（指向前一个用例的
        # 临时库）会泄漏到下一个用例——致使 start_interview/answer_interview 经未打补丁的
        # resolve_profile_manager→get_profile_manager 命中 stale manager，会话写进旧临时库、
        # answer_interview 读新临时库 → session 查不到 → session_done 永远 False。
        "profile_default_managers": dict(pmod._default_managers),
        "profile_gpm": pmod.get_profile_manager,
        "ival_CognitiveStore": ival.CognitiveStore,
        "ival_gpm": ival.get_profile_manager,
        "ival_get_extractor": ival.get_extractor,
        # 让整个用例模块**真正离线**：本机若配了 provider 凭证，llm_available() 会返回 True，
        # 于是 extract_cognitive_items 真去调 LLM、落「立场」类条目，而非规则兜底的「访谈」
        # 类条目——两个 test_explore_* 断言的正是规则兜底路径，会因此误红。强制 False 使
        # 模块确定走规则兜底（与用例注释「不依赖 LLM / 降级为规则兜底」一致），不再随环境飘。
        "llm_llm_available": llm_mod.llm_available,
        "env_DESKMATE_DIR": os.environ.get("DESKMATE_DIR"),
    }

    # ⚠ 只 patch 认知层**不够**（2026-09-22 实测）：
    # ``answer_interview`` 末段还会调 ``EmotionTimeline().record_statement()``，它走
    # ``deskmate.paths.deskmate_base_dir()`` 读 ``DESKMATE_DIR``——认知层被 _TmpStore
    # 挡住了，情感层却**落进真实库**。指纹很好认：真实库 ``json_blobs.emotion_timeline``
    # 的 statements 里躺着测试字面量「访谈回答内容」「我的回答」，而 ``interview_qa``
    # 是干净的（它走被隔离的 CognitiveStore）。
    # 走 pytest 时 conftest 已设 DESKMATE_DIR 兜住了；但本文件末尾有 ``unittest.main()``
    # 直跑入口，直跑时 conftest **完全不参与** → 只能靠这里兜住。
    os.environ["DESKMATE_DIR"] = str(tmp)

    # 强制 LLM 不可用：本模块所有用例都应在「无 LLM」下确定走规则兜底，
    # 不依赖本机是否恰好配了 provider 凭证（见 saved["llm_llm_available"] 注释）。
    llm_mod.llm_available = lambda: False

    storemod.CognitiveStore = _TmpStore
    pmod.CognitiveStore = _TmpStore

    # 缓存单例：模拟真实环境的全局 _default_managers（cognitive 层），确保
    # process_response 内的 manager 与 stub extract 内的 manager 是同一实例
    # （不会互相全量覆盖）
    def _gpm(profile_path=None, layer="cognitive"):
        if profile_path is not None:
            return pmod.CognitiveProfileManager(profile_path, layer=layer)
        key = layer or "cognitive"
        if key not in pmod._default_managers:
            pmod._default_managers[key] = pmod.CognitiveProfileManager(layer=key)
        return pmod._default_managers[key]

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
    pmod._default_managers = saved["profile_default_managers"]
    pmod.get_profile_manager = saved["profile_gpm"]
    llm_mod.llm_available = saved["llm_llm_available"]
    ival.CognitiveStore = saved["ival_CognitiveStore"]
    ival.get_profile_manager = saved["ival_gpm"]
    ival.get_extractor = saved["ival_get_extractor"]
    _old_dir = saved.get("env_DESKMATE_DIR")
    if _old_dir is None:
        os.environ.pop("DESKMATE_DIR", None)
    else:
        os.environ["DESKMATE_DIR"] = _old_dir


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
            # 规则吸收：回答并入画像（category=访谈），不再依赖 stub extractor
            self.assertTrue(any(st.category == "访谈" for st in pm2.profile.stances))
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

    def test_answer_feeds_emotion_timeline(self):
        """F1 闭环：访谈回答要回流**当日心情**，不能只进认知画像。

        真实 bug：此前 ``answer_interview`` 只回灌画像，情感时间线完全看不到。
        他说「情绪低落时我会看电影」，系统只当那是条画像条目（回答"他是什么人"），
        并不知道他那天低落（回答"他今天怎么样"）——两条腿缺一条就走不动。

        ⚠️ 只测 record_statement 本身锁不住这条接线，必须测到调用这一环。
        """
        saved = _hermetic()
        try:
            import deskmate.growth.emotion_timeline as tl_mod

            captured = []
            orig_cls = tl_mod.EmotionTimeline

            class _SpyTimeline:
                def __init__(self, base_dir=None):
                    pass

                def record_statement(self, engine, text, day=None):
                    captured.append(text)
                    return {}

            tl_mod.EmotionTimeline = _SpyTimeline
            try:
                p, s1, g1, g2, low = _make_profile()
                pm = pmod.get_profile_manager()
                pm.profile = p
                pm.save()
                started = ival.start_interview(limit=3)
                self.assertTrue(started["session_id"])
                ival.answer_interview(
                    started["session_id"],
                    started["questions"][0]["id"],
                    "情绪低落时，我会看电影。",
                )
            finally:
                tl_mod.EmotionTimeline = orig_cls

            self.assertEqual(
                captured, ["情绪低落时，我会看电影。"],
                "访谈回答必须回流情感时间线，否则他说过的话被当成没说过",
            )
        finally:
            _restore(saved)

    def test_explore_rules_absorb_no_llm(self):
        """真实 extractor 路径（不 stub）：explore 回答规则吸收进画像 + gap resolved。

        回归 ③b 遗留断点：原实现给 extract_from_text 传不存在的 use_llm 参数，
        必然 TypeError 被吞 → explore 回答只消 gap 不进画像。此用例锁定修复。
        """
        saved = _hermetic()
        try:
            ival.get_extractor = saved["ival_get_extractor"]  # 还原真实 extractor
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            before_gaps = len(pm.profile.cognitive_gaps)
            qid = ival._encode_question_id(None, g2.id, "explore")
            ival.SocraticInterviewEngine().process_response(
                pm.profile, qid, "我写作时特别在意人物动机是否合乎历史逻辑")
            pm2 = pmod.get_profile_manager()
            # 规则吸收：回答以「访谈」category 进画像（不依赖 LLM / stub）
            self.assertTrue(any(st.category == "访谈" for st in pm2.profile.stances))
            g2_now = [g for g in pm2.profile.cognitive_gaps if g.id == g2.id][0]
            self.assertEqual(g2_now.status, "resolved")
            # 不 spawn 新假 gap
            self.assertLessEqual(len(pm2.profile.cognitive_gaps), before_gaps)
        finally:
            _restore(saved)

    def test_answer_persists_question_text(self):
        """访谈 QA 落库应含问题文本（此前硬编码空串，历史不可追溯）。"""
        saved = _hermetic()
        try:
            p, s1, g1, g2, low = _make_profile()
            pm = pmod.get_profile_manager()
            pm.profile = p
            pm.save()
            started = ival.start_interview(limit=3)
            self.assertTrue(started["session_id"])
            q = started["questions"][0]
            ival.answer_interview(started["session_id"], q["id"], "我的回答")
            qa = storemod.CognitiveStore().get_qa(started["session_id"])
            self.assertTrue(qa)
            self.assertNotEqual(qa[0]["question"], "")
            self.assertTrue(qa[0]["answer"])
        finally:
            _restore(saved)


if __name__ == "__main__":
    # 直跑时 conftest 不参与：先自己把统一根目录挪到临时目录，兜住 _hermetic() 之外
    # 的一切（模块级代码 / 将来忘了包隔离的用例），避免写进真实 ~/.deskmate。
    if not os.environ.get("DESKMATE_DIR"):
        os.environ["DESKMATE_DIR"] = tempfile.mkdtemp(prefix="deskmate-selftest-")
    unittest.main(verbosity=2)
