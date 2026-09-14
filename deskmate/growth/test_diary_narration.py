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
from deskmate.growth.diary_inspiration import backfill_learned_texts as backfill
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

    def test_only_depth_backfilled_returns_none(self):
        """只有系统补录 (depth_backfilled) → 不调（不是真实事件）。

        2026-09-07：闸门从「必须带 reason/note」退到「events 非空 + 非补录」——
        日常 chat/writing/roundtable 等纯标签事件**也让 narrator 跑**（具体
        写不写由 LLM 自己用 prompt 里的"素材不足"弃权）。这是萧哥"narrator
        大半时候根本没跑"的根因修复。
        """
        events = [{"ts": "2026-09-03T10:00:00+00:00", "kind": "depth_backfilled"}]
        self.assertFalse(dn._has_substance(events))
        with mock.patch("deskmate.memory.llm.chat_memory") as m:
            self.assertIsNone(dn.narrate(events))
        m.assert_not_called()

    def test_chat_only_now_runs_narrator(self):
        """chat-only 不再被门挡——这是 2026-09-07 修复。

        旧版：chat-only 因 _has_substance 闸门失败，整日整日落 fallback 规则版。
        新版：闸门通过，让 narrator 跑；LLM 自己用 prompt 的"素材不足"判定。
        """
        events = [{"ts": "2026-09-03T10:00:00+00:00", "kind": "chat"}]
        self.assertTrue(dn._has_substance(events))
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value=_GOOD
        ) as m:
            self.assertEqual(dn.narrate(events), _GOOD)
        m.assert_called_once()

    def test_skill_name_without_reason_still_runs(self):
        """「技能又精进了」没有原因 → 旧版不算素材；新版让 narrator 跑（旧设计意图废）。

        旧版意图是「别让 LLM 把空壳扩写成废话」。新版让 LLM 自己看见"技能名没
        reason"，由 prompt 决定写不写。测试只验 narrator 被调用、narrator 不
        抛异常即可。
        """
        events = [{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "skill_evolved",
            "skill_name": "novel-review",
        }]
        self.assertTrue(dn._has_substance(events))
        with mock.patch(
            "deskmate.memory.llm.memory_llm_available", return_value=True
        ), mock.patch(
            "deskmate.memory.llm.chat_memory", return_value=_GOOD
        ):
            self.assertEqual(dn.narrate(events), _GOOD)

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

    def test_hardcoded_correction_placeholder_runs_narrator(self):
        """用户纠正占位「用户明确纠正」= 没拿到原话。

        旧版按 note 是否占位判素材——这导致"记到了 wrong_reason 但没原话"
        的纠正事件被门挡。新规则（2026-09-07）：闸门只看是否有真实事件，
        占位与否交给 prompt 自己处理（叙事时不必猜原因）。
        """
        events = [{
            "ts": "2026-09-03T10:00:00+00:00",
            "kind": "user_corrected",
            "note": "用户明确纠正",
        }]
        self.assertTrue(dn._has_substance(events))

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
        p = dn._build_prompt(["09:10 我记下了：x"], [], 1, "09:10")
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


class TestBackfillLearnedTexts(unittest.TestCase):
    """存量素材回补：库里有原文，账本里只有 count，靠时间戳搬回来。

    契约：**这是搬运不是创作**。库里没有的必须留空（宁可写不出，
    也不许回填任何推测内容），且整个过程幂等、失败不改坏素材。
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _store(rows: list[dict]):
        """造一个假 LearnedStore：recent() 返回给定记录。"""
        return mock.MagicMock(recent=mock.MagicMock(return_value=rows))

    def _patch_store(self, rows):
        store_cls = mock.MagicMock(return_value=self._store(rows))
        return mock.patch.dict(
            "sys.modules",
            {"deskmate.memory.learned_store": mock.MagicMock(LearnedStore=store_cls)},
        )

    def test_matches_within_window(self):
        """事件 00:42:55Z ↔ 记忆库本地 08:42:55（同一时刻）→ 命中。"""
        items = [{"ts": "2026-09-03T00:42:55+00:00", "kind": "memory_learned", "count": 1}]
        rows = [{"ts": "2026-09-03T08:42:55", "fact": "2026-09-03: 太学三舍制设定确认"}]
        with self._patch_store(rows):
            n = backfill(items, base_dir=self.base)
        self.assertEqual(n, 1)
        # 日期前缀要剥掉，否则日记里会写「2026-09-03: 太学三舍制…」
        self.assertEqual(items[0]["texts"], ["太学三舍制设定确认"])

    def test_outside_window_no_match(self):
        """差 10 分钟不认——那是两件事，硬凑会张冠李戴。"""
        items = [{"ts": "2026-09-03T00:42:55+00:00", "kind": "memory_learned", "count": 1}]
        rows = [{"ts": "2026-09-03T08:52:55", "fact": "另一件事"}]
        with self._patch_store(rows):
            n = backfill(items, base_dir=self.base)
        self.assertEqual(n, 0)
        self.assertNotIn("texts", items[0])

    def test_one_fact_claimed_once(self):
        """相邻两个事件时间窗重叠 → 同一条事实只发一次，不许重复计数。"""
        items = [
            {"ts": "2026-09-03T00:42:55+00:00", "kind": "memory_learned", "count": 1},
            {"ts": "2026-09-03T00:43:00+00:00", "kind": "memory_learned", "count": 1},
        ]
        rows = [{"ts": "2026-09-03T08:42:57", "fact": "同一件事"}]
        with self._patch_store(rows):
            n = backfill(items, base_dir=self.base)
        self.assertEqual(n, 1)
        self.assertEqual(items[0]["texts"], ["同一件事"])
        self.assertNotIn("texts", items[1])

    def test_idempotent(self):
        """已有 texts 的跳过；重复调用结果不变。"""
        items = [{"ts": "2026-09-03T00:42:55+00:00", "kind": "memory_learned",
                  "texts": ["早就有了"]}]
        rows = [{"ts": "2026-09-03T08:42:55", "fact": "库里的"}]
        with self._patch_store(rows):
            self.assertEqual(backfill(items, base_dir=self.base), 0)
            self.assertEqual(backfill(items, base_dir=self.base), 0)
        self.assertEqual(items[0]["texts"], ["早就有了"])

    def test_store_failure_leaves_items_intact(self):
        """库查不动 / 记录为空 → 原样返回，绝不把素材改成空壳。"""
        items = [{"ts": "2026-09-03T00:42:55+00:00", "kind": "memory_learned", "count": 3}]
        with mock.patch.dict("sys.modules", {"deskmate.memory.learned_store": None}):
            self.assertEqual(backfill(items, base_dir=self.base), 0)
        self.assertNotIn("texts", items[0])
        self.assertEqual(items[0]["count"], 3)

    def test_non_memory_events_untouched(self):
        """只补 memory_learned，别的事件类型一律不碰。"""
        items = [{"ts": "2026-09-03T00:42:55+00:00", "kind": "skill_evolved",
                  "skill_name": "outline"}]
        rows = [{"ts": "2026-09-03T08:42:55", "fact": "无关事实"}]
        with self._patch_store(rows):
            self.assertEqual(backfill(items, base_dir=self.base), 0)
        self.assertNotIn("texts", items[0])


class TestRecentMemories(unittest.TestCase):
    """2026-09-07：narrator 喂「最近记住他的事」作叙事底色（萧哥"理解我的她"）。

    契约：
    - 只取用户口述事实，排除世界观察（「（外部·」前缀/后缀标）——那是她看到的
      新闻，不是"他的事"，不该混进"懂他"的底色。
    - fail-open：库查不到 / base_dir 空 → 返回 []，绝不阻断叙事。
    - 背景块进 prompt，且明确禁止把"已有的了解"当今天新发生的事写。
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, facts):
        from deskmate.memory.learned_store import LearnedStore
        return LearnedStore(base_dir=str(self.base)).append(facts)

    def test_excludes_world_facts(self):
        self._seed([
            "萧盛正在写《国子监秘闻录》",
            "他考据习惯要标具体出处",
            "（外部·news）美伊在霍尔木兹海峡对峙升级",
            "碳酸锂一周跌了15%摘要（外部·finance）",
        ])
        mems = dn._recent_memories(str(self.base))
        joined = " ".join(mems)
        self.assertIn("国子监", joined)          # 用户口述事实进底色
        self.assertNotIn("霍尔木兹", joined)      # 前缀式世界观察排除
        self.assertNotIn("碳酸锂", joined)        # 后缀式世界观察排除

    def test_empty_without_base(self):
        self.assertEqual(dn._recent_memories(None), [])

    def test_empty_when_no_memories(self):
        self.assertEqual(dn._recent_memories(str(self.base)), [])

    def test_prompt_includes_memories_block(self):
        p = dn._build_prompt(
            ["09:10 你们聊了一阵"], [], 1, "09:10",
            memories=["他在写《国子监秘闻录》"],
        )
        self.assertIn("【我早就知道的事】", p)   # 背景块标题只在有 memories 时出现
        self.assertIn("国子监", p)
        self.assertIn("不许当成今天新发生的事写", p)

    def test_prompt_without_memories_has_no_block(self):
        p = dn._build_prompt(["09:10 我们聊了一阵"], [], 1, "09:10")
        self.assertNotIn("【我早就知道的事】", p)


class TestPronounSpaceContract(unittest.TestCase):
    """素材与输出必须共用**一套**人称（我=小桔，你=萧盛），不得再做翻转。

    回归 2026-09-14：旧版素材块用「你=小桔、他=萧盛」，输出规则却要求
    「我=小桔、你=萧盛」——同一份 prompt 里「你」指两个人，模型每次都得先翻转。
    素材一旦没主语（「《衙探》读到第7章末」）就翻车：她的每日读书被写成
    「你（萧盛）安静地看着《衙探》」。

    钉死的契约：
    - 素材行（``_facts`` / ``_signals``）里**不出现第三人称「他」**，指代只用「我/你」；
    - 指示性文字里提萧盛写**专名**「萧盛」，避免与"对模型说话的「你」"撞车；
    - 素材块抬头声明人称已与日记对齐。
    """

    def _events(self) -> list[dict]:
        return [
            {"ts": "2026-09-14T02:26:47+00:00", "kind": "memory_learned",
             "texts": ["2026-09-14: 《衙探》读到第7章末"]},
            {"ts": "2026-09-14T02:27:30+00:00", "kind": "user_corrected",
             "note": "流程要先读配置"},
            {"ts": "2026-09-14T02:28:00+00:00", "kind": "skill_evolved",
             "skill_name": "novel-review", "reason": "每日读书"},
            {"ts": "2026-09-14T02:29:00+00:00", "kind": "skill_used",
             "skill_name": "office", "reason": "出一份报告"},
            {"ts": "2026-09-14T02:30:00+00:00", "kind": "creation_rework",
             "note": "标题太平"},
            {"ts": "2026-09-14T02:31:00+00:00", "kind": "creation_approved",
             "note": "定稿"},
            {"ts": "2026-09-14T02:32:00+00:00", "kind": "roundtable"},
            {"ts": "2026-09-14T02:33:00+00:00", "kind": "writing"},
            {"ts": "2026-09-14T02:34:00+00:00", "kind": "report"},
            {"ts": "2026-09-14T02:35:00+00:00", "kind": "chat"},
        ]

    def test_material_has_no_third_person(self):
        for line in dn._facts(self._events()) + dn._signals(self._events()):
            self.assertNotIn("他", line, f"素材行混入第三人称：{line}")

    def test_skill_use_is_her_own_action(self):
        """技能库与熟练度都归她，技能被用是**她**做的——不能算到萧盛头上。"""
        facts = dn._facts([
            {"ts": "2026-09-14T02:27:30+00:00", "kind": "skill_used",
             "skill_name": "novel-review"},
        ])
        self.assertTrue(facts)
        self.assertIn("我用「novel-review」技能", facts[0])

    def test_instructions_use_xiaosheng_by_name(self):
        p = dn._build_prompt(["09:10 我记下了：x"], [], 1, "09:10")
        self.assertIn("称萧盛为「你」", p)
        self.assertNotIn("他", p.split("【怎么写】", 1)[1])

    def test_material_header_declares_single_pronoun_space(self):
        p = dn._build_prompt(["09:10 我记下了：x"], [], 1, "09:10")
        self.assertIn("同一套", p)

    def test_memory_fact_keeps_her_frame(self):
        """记忆类素材不写「关于他的一件事」——无主语事实正是从这句被误读的。"""
        facts = dn._facts([
            {"ts": "2026-09-14T02:26:47+00:00", "kind": "memory_learned",
             "texts": ["《衙探》读到第7章末"]},
        ])
        self.assertTrue(facts)
        self.assertIn("我记下了：", facts[0])
        self.assertIn("《衙探》读到第7章末", facts[0])
        self.assertNotIn("他的一件事", facts[0])


if __name__ == "__main__":
    unittest.main()
