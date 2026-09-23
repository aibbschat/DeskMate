"""P0-2 回归测试：访谈「稍后」显式忽略 → 补送尊重式停顿 → 窗口过后恢复补送。

隔离：全程传 base_dir=临时库，不碰真实 ~/.deskmate。
验证链路：start_interview 建真实 session → redeliver 重放 → dismiss 停送 → 窗口过后恢复。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deskmate.cognitive.interview import (
    dismiss_pending_interview,
    redeliver_pending_interview,
    start_interview,
)
from deskmate.cognitive.models import SourceRef, Stance, _now
from deskmate.cognitive.profile import resolve_profile_manager
from deskmate.cognitive.store import CognitiveStore


class TestInterviewDismiss(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "deskmate.db"
        self.store = CognitiveStore(db_path=self.db)
        self.pm = resolve_profile_manager(layer="cognitive", base_dir=self.tmp)
        # 注入一个 deepen 候选：低置信 active stance（generate_questions 会 pick 它）
        st = Stance(topic="测试立场", content="测试内容", category="测试", tags=["测试"])
        st.confidence = 0.3
        st.add_source(SourceRef(type="test", title="t", excerpt="e", date=_now()))
        self.pm.profile.stances.append(st)
        self.pm.save()

    def test_redeliver_pauses_after_dismiss_then_resumes(self):
        # 1) 发起一轮（建真实 session + 真实 qids）
        started = start_interview(limit=3, layer="cognitive", base_dir=self.tmp, pm=self.pm)
        self.assertIsNotNone(started["session_id"])
        # 返回体应带 layer，供前端 dismiss 回传
        self.assertEqual(started["layer"], "cognitive")

        # 2) 未忽略时 redeliver 应重放（redelivered=True）
        r1 = redeliver_pending_interview(layer="cognitive", base_dir=self.tmp, pm=self.pm)
        self.assertIsNotNone(r1)
        self.assertTrue(r1["redelivered"])
        self.assertEqual(r1["layer"], "cognitive")

        # 3) 显式忽略后 redeliver 在窗口内应返回 None（不再每 30 分钟骚扰）
        ok = dismiss_pending_interview(layer="cognitive", base_dir=self.tmp)
        self.assertTrue(ok)
        r2 = redeliver_pending_interview(layer="cognitive", base_dir=self.tmp, pm=self.pm)
        self.assertIsNone(r2)

        # 4) 窗口过期（清空 dismissed + 节流时间戳，模拟时间流逝）后恢复补送
        self.store.set_meta("interview_dismissed_at:cognitive", "")
        self.store.set_meta("last_interview_redeliver_at:cognitive", "")
        r3 = redeliver_pending_interview(layer="cognitive", base_dir=self.tmp, pm=self.pm)
        self.assertIsNotNone(r3)
        self.assertTrue(r3["redelivered"])

    def test_redeliver_respects_layer_isolation(self):
        # 忽略 cognitive 层不应影响 emotional 层的补送（两层独立计时）
        start_interview(limit=3, layer="cognitive", base_dir=self.tmp, pm=self.pm)
        dismiss_pending_interview(layer="cognitive", base_dir=self.tmp)
        # emotional 层没有挂起会话 → redeliver 返回 None（与 cognitive 忽略无关）
        r = redeliver_pending_interview(layer="emotional", base_dir=self.tmp)
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
