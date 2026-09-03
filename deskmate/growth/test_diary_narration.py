"""日记叙事润色（L3）+ 素材透传（L1）测试。

锁定的核心契约：**润色是可选增强，任何一道闸门不过都必须静默退回规则渲染。**
日记挂在 tick 后台线程上，润色层抛异常 = 拖垮主动推理，这是不可接受的。

另一半契约是「情绪必须有锚点」——没有可观测信号的抒情就是编造，
本文件用素材打包与信号提取的用例把它钉住。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from deskmate.growth import diary_narrator as dn
from deskmate.growth.diary import DiaryWriter
from deskmate.growth.engine import GrowthEngine, _clip, _clip_list

# 一条合格润色结果的样本（长度在闸门内、无弃权语）
_GOOD = (
    "上午你把《国子监》第三章收了尾，中途为了宋代太学的隶属关系卡了很久，"
    "翻了《宋史·职官志》才定下来。我记住了你在意的那个细节——"
    "你说制度类的考据，宁可慢一点也要有出处。"
)


def _rich_events() -> list[dict]:
    """一批**有分量**的素材（L1 透传后应有的形态）。"""
    return [
        {
            "ts": "2026-09-03T06:00:00+00:00",
            "kind": "memory_learned",
            "count": 2,
            "texts": ["萧盛在写《国子监秘闻录》第三章", "他考据习惯要标出处"],
        },
        {
            "ts": "2026-09-03T08:00:00+00:00",
            "kind": "user_corrected",
            "note": "不是洪武三年，是洪武十五年",
        },
        {
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "skill_evolved",
            "skill_name": "novel-review",
            "reason": "考据类问题必须给具体出处，不能只给结论",
        },
    ]


# ----------------------------------------------------------------------
# 闸门
# ----------------------------------------------------------------------
class TestNarrationGates(unittest.TestCase):
    """三道闸门：不过就返回 None，绝不抛、绝不返回半成品。"""

    def test_empty_events_returns_none(self):
        self.assertIsNone(dn.narrate([]))

    def test_label_only_events_returns_none(self):
        """只有标签、没有详情 → 不调模型（白烧一次调用换「素材不足」）。"""
        events = [{"ts": "2026-09-03T10:00:00+00:00", "kind": "roundtable"}]
        # chat_memory 是函数内 import 的，只能 patch 源头模块
        with mock.patch("deskmate.memory.llm.chat_memory") as m:
            self.assertIsNone(dn.narrate(events))
        m.assert_not_called()

    def test_skill_name_without_reason_is_not_substance(self):
        """「技能又精进了」没有原因 → 正是萧哥说的废话，不该进润色。"""
        events = [{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "skill_evolved",
            "skill_name": "novel-review",
        }]
        self.assertFalse(dn._has_substance(events))
        self.assertIsNone(dn.narrate(events))

    def test_no_provider_returns_none(self):
        """闸门 1：没配 provider 就不调。"""
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=False
        ), mock.patch("deskmate.memory.llm.chat_memory") as m:
            self.assertIsNone(dn.narrate(_rich_events()))
        m.assert_not_called()

    def test_model_bails_out_returns_none(self):
        """闸门 3：模型自己说素材不足 → 丢弃，用规则版。"""
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value="素材不足"
        ):
            self.assertIsNone(dn.narrate(_rich_events()))

    def test_too_short_returns_none(self):
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value="嗯。"
        ):
            self.assertIsNone(dn.narrate(_rich_events()))

    def test_exception_is_swallowed(self):
        """最要紧的一条：模型炸了也不能拖垮 tick。"""
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(dn.narrate(_rich_events()))

    def test_import_failure_is_swallowed(self):
        """llm 模块不存在（裁剪版部署）也要静默。"""
        with mock.patch.dict("sys.modules", {"deskmate.memory.llm": None}):
            self.assertIsNone(dn.narrate(_rich_events()))

    def test_happy_path_returns_text(self):
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value=_GOOD
        ):
            self.assertEqual(dn.narrate(_rich_events()), _GOOD)

    def test_uses_background_channel_with_short_timeout(self):
        """跑在 tick 线程池里，必须用短超时，否则慢请求会堆积。"""
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value=_GOOD
        ) as m:
            dn.narrate(_rich_events())
        kw = m.call_args.kwargs
        self.assertLessEqual(kw.get("timeout", 999), 30.0)
        self.assertEqual(kw.get("temperature"), 0.7)


class TestNarrationKillSwitch(unittest.TestCase):
    """总开关：出问题能一键退回纯规则。"""

    def test_env_off_disables_narration(self):
        with mock.patch.dict(os.environ, {"DESKMATE_DIARY_NARRATE": "0"}):
            with mock.patch.object(dn, "narrate") as m:
                from deskmate.growth.diary import DiaryWriter
                self.assertIsNone(
                    DiaryWriter._narrate(_rich_events(), seq=1, at="09:10")
                )
        m.assert_not_called()

    def test_env_on_enables_narration(self):
        with mock.patch.dict(os.environ, {"DESKMATE_DIARY_NARRATE": "1"}):
            with mock.patch.object(dn, "narrate", return_value=_GOOD) as m:
                self.assertEqual(
                    DiaryWriter._narrate(_rich_events(), seq=1, at="09:10"),
                    _GOOD,
                )
        m.assert_called_once()


# ----------------------------------------------------------------------
# 素材打包与信号锚点
# ----------------------------------------------------------------------
class TestMaterialPacking(unittest.TestCase):
    """模型看到的东西必须**全是真的**，且情绪要有可观测依据。"""

    def test_learned_texts_enter_prompt(self):
        facts = dn._facts(_rich_events())
        self.assertTrue(
            any("《国子监秘闻录》第三章" in f for f in facts),
            facts,
        )

    def test_correction_quote_enters_prompt(self):
        facts = dn._facts(_rich_events())
        self.assertTrue(
            any("洪武十五年" in f for f in facts), facts
        )

    def test_skill_reason_enters_prompt(self):
        facts = dn._facts(_rich_events())
        self.assertTrue(any("具体出处" in f for f in facts), facts)

    def test_hardcoded_correction_placeholder_is_skipped(self):
        """note 还是占位的「用户明确纠正」= 没拿到原话，不算素材。"""
        events = [{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "user_corrected",
            "note": "用户明确纠正",
        }]
        self.assertFalse(dn._has_substance(events))

    def test_time_prefix_present(self):
        facts = dn._facts(_rich_events())
        self.assertTrue(any(f[:5].count(":") == 1 for f in facts), facts)

    def test_signals_from_rework(self):
        events = [
            {"ts": "2026-09-03T10:00:00+00:00", "kind": "creation_rework"},
            {"ts": "2026-09-03T11:00:00+00:00", "kind": "creation_rework"},
        ]
        sig = dn._signals(events)
        self.assertTrue(any("打回了 2 次" in s for s in sig), sig)

    def test_signals_from_late_night(self):
        """深夜是最可靠的情绪底色：忙到 23 点 → 可以写「熬得挺晚」。"""
        events = [{"ts": "2026-09-03T15:00:00+00:00", "kind": "writing"}]
        sig = dn._signals(events)
        self.assertTrue(any("夜里" in s for s in sig), sig)

    def test_no_signals_when_nothing_observable(self):
        self.assertEqual(dn._signals([{"ts": "", "kind": "chat"}]), [])

    def test_prompt_forbids_fabrication(self):
        """prompt 必须明确禁止编造与台账句——这是红线的文字载体。"""
        p = dn._build_prompt(["09:10 你记住了他的一件事：x"], [], 1, "09:10")
        self.assertIn("不许出现上面素材之外", p)
        self.assertIn("台账", p)
        self.assertIn("素材不足", p)  # 允许弃权


# ----------------------------------------------------------------------
# L1 素材透传（engine 侧）
# ----------------------------------------------------------------------
class TestDetailPassthrough(unittest.TestCase):
    """L1：调用方手里的原文必须能进到事件里，不能只落个计数。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _engine(self) -> GrowthEngine:
        return GrowthEngine(base_dir=self.base)

    def test_memory_learned_carries_texts(self):
        eng = self._engine()
        eng.record_memory_learned(2, texts=["在写《国子监》第三章", "考据要标出处"])
        ev = eng.state.recent_events[-1]
        self.assertEqual(ev["count"], 2)
        self.assertIn("在写《国子监》第三章", ev["texts"])

    def test_memory_learned_without_texts_still_works(self):
        """向后兼容：老调用方不传 texts 不能炸。"""
        eng = self._engine()
        eng.record_memory_learned(3)
        self.assertEqual(eng.state.recent_events[-1]["texts"], [])

    def test_correction_carries_quote(self):
        eng = self._engine()
        eng.record_correction(note="不是洪武三年，是洪武十五年")
        self.assertEqual(
            eng.state.recent_events[-1]["note"], "不是洪武三年，是洪武十五年"
        )

    def test_correction_default_note_unchanged(self):
        eng = self._engine()
        eng.record_correction()
        self.assertEqual(eng.state.recent_events[-1]["note"], "用户明确纠正")

    def test_skill_evolved_carries_reason(self):
        eng = self._engine()
        eng.record_skill_evolved("novel-review", reason="考据要给出处")
        ev = eng.state.recent_events[-1]
        self.assertEqual(ev["skill_name"], "novel-review")
        self.assertEqual(ev["reason"], "考据要给出处")

    def test_skill_evolved_without_reason_backward_compatible(self):
        eng = self._engine()
        eng.record_skill_evolved("novel-review")
        self.assertEqual(eng.state.recent_events[-1]["reason"], "")

    def test_clip_truncates_long_detail(self):
        """详情页只供日记讲事，不能让 state 文件无限膨胀。"""
        from deskmate.growth.engine import _MAX_DETAIL_CHARS

        self.assertEqual(len(_clip("x" * 500)), _MAX_DETAIL_CHARS)

    def test_clip_list_caps_items_and_dedupes(self):
        from deskmate.growth.engine import _MAX_DETAIL_ITEMS

        out = _clip_list(["a", "a", "b", "c", "d", "e", "f"])
        self.assertEqual(len(out), _MAX_DETAIL_ITEMS)
        self.assertEqual(out[0], "a")

    def test_clip_handles_none(self):
        self.assertEqual(_clip(None), "")
        self.assertEqual(_clip_list(None), [])


# ----------------------------------------------------------------------
# 规则渲染也要吃 L1 的内容（润色挂了才有的降级版）
# ----------------------------------------------------------------------
class TestRuleRenderUsesDetail(unittest.TestCase):
    """降级版不能退回流水账——这是 L1 独立于 L3 的价值。

    本类专测规则渲染，故关掉润色层：否则断言会撞上模型生成的措辞，
    而那是 L3 的测试范围，不该由这里的用例去锁。
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "growth.json").write_text(
            json.dumps({"version": 2, "intimacy": 50.0, "level": 5,
                        "total_chats": 100, "recent_events": []}),
            encoding="utf-8",
        )
        self.writer = DiaryWriter(self.base)
        self._narrate_off = mock.patch.dict(
            os.environ, {"DESKMATE_DIARY_NARRATE": "0"}
        )
        self._narrate_off.start()

    def tearDown(self):
        self._narrate_off.stop()
        self._tmp.cleanup()

    def _render(self, events, seq=1, at="09:10") -> str:
        return self.writer._render_entry(
            events, [], None, self.writer._state_summary(), seq=seq, at=at
        )

    def test_skill_evolved_shows_reason(self):
        text = self._render([{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "skill_evolved",
            "skill_name": "novel-review",
            "reason": "考据要给具体出处",
        }])
        self.assertIn("考据要给具体出处", text)
        self.assertIn("因为", text)

    def test_skill_evolved_without_reason_falls_back(self):
        text = self._render([{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "skill_evolved",
            "skill_name": "novel-review",
        }])
        self.assertIn("又精进了", text)

    def test_correction_shows_quote(self):
        text = self._render([{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "user_corrected",
            "note": "不是洪武三年，是洪武十五年",
        }])
        self.assertIn("洪武十五年", text)

    def test_memory_learned_shows_facts_not_just_count(self):
        text = self._render([{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "memory_learned",
            "count": 2,
            "texts": ["在写《国子监》第三章", "考据要标出处"],
        }])
        self.assertIn("《国子监》第三章", text)
        self.assertNotIn("我又记住了你 2 件小事", text)

    def test_memory_learned_without_texts_keeps_count(self):
        text = self._render([{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "memory_learned",
            "count": 2,
        }])
        self.assertIn("我又记住了你 2 件小事", text)


# ----------------------------------------------------------------------
# 端到端：润色进日记文件
# ----------------------------------------------------------------------
class TestNarrationEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "growth.json").write_text(
            json.dumps({"version": 2, "intimacy": 50.0, "level": 5,
                        "total_chats": 100, "recent_events": []}),
            encoding="utf-8",
        )
        self.writer = DiaryWriter(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_narrated_body_replaces_bullet_list(self):
        """润色成功时不再列清单，但仍要保留客观数据行。"""
        with mock.patch.dict(os.environ, {"DESKMATE_DIARY_NARRATE": "1"}):
            with mock.patch.object(dn, "narrate", return_value=_GOOD):
                self.writer.append_entry(
                    "2026-09-03", _rich_events(), [], at="09:10"
                )
        text = self.writer.read("2026-09-03")
        self.assertIn(_GOOD, text)
        self.assertNotIn("今天我们一起做了些事", text)  # 模板开场被替换
        self.assertIn("默契 50.0/100", text)            # 客观数据保留
        self.assertIn("## 09:10", text)                 # 小节标题保留

    def test_falls_back_to_bullets_when_narration_fails(self):
        """润色返回 None → 必须回到规则渲染，不能留下空日记。"""
        with mock.patch.dict(os.environ, {"DESKMATE_DIARY_NARRATE": "1"}):
            with mock.patch.object(dn, "narrate", return_value=None):
                self.writer.append_entry(
                    "2026-09-03", _rich_events(), [], at="09:10"
                )
        text = self.writer.read("2026-09-03")
        self.assertIn("洪武十五年", text)   # 规则版照样写出具体内容
        self.assertIn("## 09:10", text)


if __name__ == "__main__":
    unittest.main()
