"""「0.0 是合法值」契约（falsy-coalescing 陷阱回归，2026-09-21）。

现场
----
``growth.state.trust`` 真实值 **0.0**（连续纠正压到下限），
但 ``emotion_timeline.record_today`` 写的是 ``float(getattr(st,"trust",0.5) or 0.5)``
→ **0.0 被吞成 0.5**，``diary._state_summary`` 同病 → 日记落款写「信任 0.5」。

后果：情感时间线 surface 出 ``0.44 → 0.34 → … → 0.02 → 0.5 ×17 天``，
看起来像"指标在 09-05 冻结了 17 天"，其实一直是 0.0 —— 一条假的时间线，
还顺便把"成长指标是否在动"这个判断带偏（查情感层活跃度会**看数值序列**）。

判据：**缺省与否看 ``None`` / 空串 / 转换失败，不看真假。**
"""

from __future__ import annotations

from pathlib import Path

from deskmate.coerce import as_float, as_int
from deskmate.growth.diary import DiaryWriter
from deskmate.growth.engine import GrowthEngine


class TestAsFloat:
    def test_zero_is_a_value_not_a_missing_value(self):
        assert as_float(0.0, 0.5) == 0.0
        assert as_float(0, 0.5) == 0.0

    def test_default_only_for_missing(self):
        assert as_float(None, 0.5) == 0.5
        assert as_float("", 0.5) == 0.5
        assert as_float("   ", 0.5) == 0.5
        assert as_float("abc", 0.5) == 0.5

    def test_numeric_strings_and_ints(self):
        assert as_float("0.0", 0.5) == 0.0
        assert as_float("0.75", 0.0) == 0.75
        assert as_float(3, 0.0) == 3.0

    def test_the_old_idiom_would_have_been_wrong(self):
        """把这个坑写进断言里：``v or d`` 与 ``as_float`` 在 v=0.0 时结论相反。"""
        assert (0.0 or 0.5) == 0.5
        assert as_float(0.0, 0.5) == 0.0


class TestAsInt:
    def test_zero_is_a_value(self):
        assert as_int(0, 7) == 0

    def test_default_only_for_missing(self):
        assert as_int(None, 7) == 7
        assert as_int("", 7) == 7
        assert as_int("x", 7) == 7

    def test_float_string(self):
        assert as_int("12.9", 0) == 12


class TestTrustZeroReachesTheSurface:
    def test_diary_state_summary_shows_zero_trust(self, tmp_path: Path):
        """日记落款必须写真实的 0.0，不能抬成 0.5。"""
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.trust = 0.0
        eng.state.intimacy = 100.0
        eng.save()
        w = DiaryWriter(tmp_path)
        st = w._state_summary()
        assert st["trust"] == 0.0, "trust=0.0 被吞成了默认值（falsy-coalescing 回归）"

    def test_diary_state_summary_missing_attr_uses_default(self, tmp_path: Path):
        class _S:
            pass

        class _E:
            state = _S()

        w = DiaryWriter.__new__(DiaryWriter)
        w.engine = _E()  # 只有 state，没有任何关系数值
        st = DiaryWriter._state_summary(w)
        assert st["trust"] == 0.5  # 真的缺省才用默认

    def test_emotion_timeline_records_zero_trust(self, tmp_path: Path):
        """情感时间线的快照同样必须写 0.0（这条假序列就是它撑起来的）。"""
        from deskmate.growth.emotion_timeline import EmotionTimeline

        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.trust = 0.0
        eng.state.intimacy = 100.0
        eng.save()
        tl = EmotionTimeline(tmp_path)
        snap = tl.record_today(eng)
        assert snap["trust"] == 0.0

    def test_emotion_timeline_realistic_sequence_is_not_flat(self, tmp_path: Path):
        """回归意义：0.44→0.34→…→0.0 的下降序列不该在某一格塌成 0.5。"""
        from deskmate.growth.emotion_timeline import EmotionTimeline

        tl = EmotionTimeline(tmp_path)
        eng = GrowthEngine(base_dir=tmp_path)
        for v in (0.44, 0.34, 0.30, 0.22, 0.10, 0.02, 0.0):
            eng.state.trust = v
            snap = tl.record_today(eng)
            assert snap["trust"] == round(v, 2)


class TestMoodZeroReachesTheSurface:
    """``mood=0.0`` 是"低落到谷底"，不是"没记录"。

    同族的第三处（2026-09-21 补）：``round(float(getattr(st,"mood",80.0) or 80.0), 1)``
    —— 一个真的跌到底的心情，会在日记落款里被写成"心情不错（80）"。
    """

    def test_diary_state_summary_shows_zero_mood(self, tmp_path: Path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.mood = 0.0
        eng.save()
        w = DiaryWriter(tmp_path)
        st = w._state_summary()
        assert st["mood"] == 0.0, "mood=0.0 被 `or 80.0` 抬成了 80"

    def test_mood_default_only_when_attr_missing(self):

        class _S:
            pass

        class _E:
            state = _S()

        w = DiaryWriter.__new__(DiaryWriter)
        w.engine = _E()
        st = DiaryWriter._state_summary(w)
        assert st["mood"] == 80.0  # 真的没有 mood 属性才用默认
        assert st["level"] == 1

    def test_level_and_total_chats_zero_are_values(self, tmp_path: Path):
        """等级/聊天数是计数器：0 是合法起点，不是缺省。"""
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.level = 0
        eng.state.total_chats = 0
        eng.save()
        w = DiaryWriter(tmp_path)
        st = w._state_summary()
        assert st["level"] == 0
        assert st["total_chats"] == 0

    def test_old_idiom_would_have_been_wrong_for_mood(self):
        assert (0.0 or 80.0) == 80.0
        assert as_float(0.0, 80.0) == 0.0
