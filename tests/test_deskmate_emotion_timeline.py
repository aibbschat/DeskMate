"""Tests for P2-5 连续情感关怀（deskmate/growth/emotion_timeline.py）。

核心：情感时间线 + 回顾注入 proactive judge，让 AI 记得用户上周的状态并主动关怀。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from deskmate.growth.emotion_timeline import (
    EmotionTimeline,
    _infer_emotion,
)
from deskmate.growth.engine import GrowthEngine


def _make_engine(tmp_path: Path, mood: float = 80.0, events: list[dict] | None = None) -> GrowthEngine:
    eng = GrowthEngine(base_dir=tmp_path)
    eng.state.mood = mood
    eng.state.trust = 0.7
    eng.state.intimacy = 50.0
    if events:
        eng.state.recent_events = events
    eng.save()
    return eng


def _evt(kind: str, days_ago: int = 0) -> dict:
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    ts = datetime.datetime(d.year, d.month, d.day, 12, 0, tzinfo=datetime.timezone.utc).isoformat()
    return {"ts": ts, "kind": kind}


class TestInferEmotion:
    """行为推断（无日记时的路径）。

    2026-09-05（E2）两条改动：
    - 不再吃 mood 参数（mood 已是情绪的派生结果，拿它判情绪是循环论证）
    - 单次不顺不再一票否决；「低落」靠活跃度与「压力大」区分，不再依赖
      那个恒为 80 的 mood（旧实现的低落分支从未被触发过）
    """

    def test_single_rework_does_not_veto_the_whole_day(self):
        """一天只打回一次，不该把整天判成「压力大」。"""
        state, note = _infer_emotion({"creation_rework": 1})
        assert state == "平静"
        assert "1 次不顺" in note

    def test_repeated_setbacks_with_heavy_day_is_stress(self):
        state, _ = _infer_emotion({"creation_rework": 3, "writing": 2})
        assert state == "压力大"

    def test_single_rework_with_heavy_creating_is_stress(self):
        """创作是他的主业：只打回一次但当天创作量不小，仍算压力。"""
        state, _ = _infer_emotion({"creation_rework": 1, "writing": 2})
        assert state == "压力大"

    def test_quiet_day_with_setbacks_is_low(self):
        """不顺 + 几乎没动静 = 低落（与「压力大」的分界是活跃度）。"""
        state, _ = _infer_emotion({"user_corrected": 1, "suggestion_ignored": 2})
        assert state == "低落"

    def test_setbacks_do_not_erase_a_productive_day(self):
        """两次不顺抵不过两次定稿 —— 旧逻辑在这里会判「压力大」。"""
        state, _ = _infer_emotion(
            {"user_corrected": 1, "suggestion_ignored": 1, "creation_approved": 2}
        )
        assert state == "顺利"

    def test_smooth_positive(self):
        state, note = _infer_emotion({"creation_approved": 2, "suggestion_accepted": 1})
        assert state == "顺利"
        assert "正反馈" in note

    def test_busy_by_creative_work(self):
        state, _ = _infer_emotion({"writing": 2, "roundtable": 1})
        assert state == "忙碌"

    def test_busy_by_volume(self):
        state, _ = _infer_emotion({"chat": 5})
        assert state == "忙碌"

    def test_calm_default(self):
        state, _ = _infer_emotion({})
        assert state == "平静"

    def test_all_five_states_reachable(self):
        """E2 验收：五态全部可达。

        旧实现里「低落」需 mood<65，而 mood 恒为 80 ⇒ 该分支有史以来
        一次都没走到过；「忙碌」也被「有 neg 就判压力大」抢先吃掉。
        """
        cases = {
            "低落": {"user_corrected": 2},
            "压力大": {"creation_rework": 3, "writing": 2},
            "顺利": {"creation_approved": 2},
            "忙碌": {"writing": 2},
            "平静": {},
        }
        got = {want: _infer_emotion(ev)[0] for want, ev in cases.items()}
        assert got == {k: k for k in cases}, got


class TestEmotionTimeline:
    def test_record_today_creates_snapshot(self, tmp_path):
        eng = _make_engine(tmp_path, mood=72.0, events=[_evt("creation_rework"), _evt("writing")])
        tl = EmotionTimeline(base_dir=tmp_path)
        snap = tl.record_today(eng)
        assert snap["state"] == "压力大"
        # mood 是情绪的**派生结果**，不再是 engine 里那个恒 80 的输入值
        assert snap["mood"] == 58.0
        assert snap["source"] == "inferred"
        assert snap["events"]["creation_rework"] == 1
        # 持久化
        tl2 = EmotionTimeline(base_dir=tmp_path)
        assert datetime.date.today().isoformat() in tl2._data

    def test_record_today_idempotent(self, tmp_path):
        """同一天重复结算只覆盖、不追加（且不会因为重复跑而失真）。"""
        eng = _make_engine(tmp_path, mood=80.0)
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_today(eng)
        tl.record_today(eng)
        snaps = tl.snapshots(1)
        assert len(snaps) == 1

    def test_lookback_with_snapshots(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        # 手动造 3 天快照
        for i, (mood, state, note) in enumerate([
            (70.0, "压力大", "1次打回"),
            (85.0, "顺利", "2次定稿"),
            (80.0, "平静", "平稳"),
        ]):
            d = (datetime.date.today() - datetime.timedelta(days=2 - i)).isoformat()
            tl._data[d] = {"date": d, "mood": mood, "trust": 0.7, "intimacy": 50,
                            "events": {}, "event_count": 0, "state": state, "note": note}
        tl._save()
        recap = tl.lookback(7)
        assert "情感回顾" in recap
        assert "压力大" in recap
        assert "顺利" in recap
        assert "关怀提示" in recap

    def test_lookback_empty_returns_empty(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.lookback(7) == ""

    def test_snapshots_filters_days(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        for i in range(10):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            tl._data[d] = {"date": d, "mood": 80.0, "state": "平静", "note": "", "events": {}}
        snaps = tl.snapshots(3)
        assert len(snaps) == 3

    def test_care_cooldown(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.should_care("stress_check")
        tl.mark_cared("stress_check")
        assert not tl.should_care("stress_check", cooldown_days=7)
        # 不同主题可以
        assert tl.should_care("progress_check")


class TestMoodIsDerived:
    """E1：mood 是情绪的**派生结果**，业务代码不许再直接增减它。

    改之前 mood 唯一的写入者是 tick_decay 的「向 80 收敛」，真实库里
    7 天快照恒为 80.0 —— 一个看起来有数据、实际是常量的假数值。
    """

    def test_mood_written_back_from_emotion(self, tmp_path):
        eng = _make_engine(tmp_path, mood=80.0)  # 出厂常量
        tl = EmotionTimeline(base_dir=tmp_path)
        snap = tl.record_today(eng)
        assert snap["state"] == "平静"
        assert eng.state.mood == 78.0        # 平静的基准值，不再是那个恒 80
        assert eng.state.mood_source == "inferred"
        assert eng.state.mood_date == snap["date"]

    def test_mood_actually_moves(self):
        """验收标准：mood 不再是常量 —— 不同日子能落到不同值。"""
        from deskmate.growth.emotion_sense import STATE_MOOD

        vals = {
            STATE_MOOD[_infer_emotion(ev)[0]]
            for ev in (
                {"creation_approved": 2},   # 顺利
                {"user_corrected": 2},      # 低落
                {"writing": 2},             # 忙碌
                {},                         # 平静
            )
        }
        assert len(vals) >= 3, vals

    def test_older_settlement_cannot_overwrite_newer(self, tmp_path):
        eng = GrowthEngine(base_dir=tmp_path)
        assert eng.set_mood_from_emotion(60.0, "stated", "2026-09-05")
        # 回填上周的日记，不许把今天刚算出来的结果盖掉
        assert not eng.set_mood_from_emotion(80.0, "inferred", "2026-09-01")
        assert eng.state.mood == 60.0

    def test_business_actions_no_longer_touch_mood(self, tmp_path):
        """采纳/忽略/打回/纠正都不许再动 mood（旧实现在这四处改它，改出个常量）。"""
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.mood = 70.0
        eng.record_suggestion_feedback(False)
        eng.record_creation_feedback(False)
        eng.record_correction("不是洪武三年，是洪武十五年")
        eng.tick_decay()
        assert eng.state.mood == 70.0


class TestDiarySelfReport:
    """E3：他自己写的日记 > 我们从行为推的。"""

    @staticmethod
    def _diary(tmp_path, text: str, author: str = "me"):
        from deskmate.growth.diary_store import DiaryStore

        return DiaryStore(base_dir=tmp_path).add_user_post(text, author=author)

    def test_diary_overrides_behavior_inference(self, tmp_path, monkeypatch):
        """他写了「写顺了，真好」，那天却有打回 —— 自述必须压过推断。

        真实案例：09-01 他写「下午第三章写顺了，真好」「心里踏实」，
        旧系统因为那天有一次打回，判成了「压力大」。
        """
        from deskmate.growth import emotion_sense

        eng = _make_engine(
            tmp_path,
            events=[_evt("creation_rework")] * 3 + [_evt("writing")] * 2,
        )
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.record_today(eng)["state"] == "压力大"   # 纯推断的结论

        self._diary(tmp_path, "下午第三章写顺了，真好，心里踏实。")
        monkeypatch.setattr(
            emotion_sense,
            "sense",
            lambda texts: {
                "state": "顺利", "mood": 85.0, "confidence": 0.9,
                "evidence": "下午第三章写顺了，真好", "source": "llm",
            },
        )
        snap = tl.record_today(eng)
        assert snap["state"] == "顺利"
        assert snap["source"] == "stated"
        assert snap["confidence"] == 0.9
        assert "写顺了" in snap["evidence"]
        assert eng.state.mood == 85.0
        assert eng.state.mood_source == "stated"

    def test_no_diary_falls_back_to_inference(self, tmp_path):
        eng = _make_engine(tmp_path, events=[_evt("creation_approved")])
        snap = EmotionTimeline(base_dir=tmp_path).record_today(eng)
        assert snap["source"] == "inferred"
        assert snap["confidence"] < 0.5   # 推断的置信度必须低于自述

    def test_unreadable_diary_does_not_fake_a_state(self, tmp_path, monkeypatch):
        """读不出情绪就不表态 —— 硬猜一个比承认不知道更糟。"""
        from deskmate.growth import emotion_sense

        monkeypatch.setattr(emotion_sense, "sense", lambda texts: None)
        eng = _make_engine(tmp_path, events=[_evt("creation_approved")])
        self._diary(tmp_path, "今天把书稿目录整理了一下。")
        snap = EmotionTimeline(base_dir=tmp_path).record_today(eng)
        assert snap["source"] == "inferred"
        assert snap["state"] == "顺利"

    def test_same_diary_is_not_re_asked(self, tmp_path, monkeypatch):
        """内容没变就不必重问模型 —— tick 一天会跑很多次。"""
        from deskmate.growth import emotion_sense

        calls: list[list[str]] = []

        def _spy(texts):
            calls.append(list(texts))
            return {
                "state": "低落", "mood": 50.0, "confidence": 0.8,
                "evidence": "细想就会郁闷", "source": "llm",
            }

        monkeypatch.setattr(emotion_sense, "sense", _spy)
        eng = _make_engine(tmp_path)
        self._diary(tmp_path, "有些事情不能去细想，细想就会郁闷。")
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_today(eng)
        tl.record_today(eng)
        tl.record_today(eng)
        assert len(calls) == 1, calls

    def test_new_diary_entry_triggers_re_sense(self, tmp_path, monkeypatch):
        """他又补写了一则 → 指纹变了 → 重新判。"""
        from deskmate.growth import emotion_sense

        calls: list[int] = []
        monkeypatch.setattr(
            emotion_sense,
            "sense",
            lambda texts: calls.append(1) or {
                "state": "平静", "mood": 78.0, "confidence": 0.6,
                "evidence": "", "source": "llm",
            },
        )
        eng = _make_engine(tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        self._diary(tmp_path, "上午改了一版。")
        tl.record_today(eng)
        self._diary(tmp_path, "下午又改了一版。")
        tl.record_today(eng)
        assert len(calls) == 2

    def test_xiaoju_diary_is_not_his_emotion(self, tmp_path, monkeypatch):
        """小桔写的日记是她的心情，不能拿来给他定色。"""
        from deskmate.growth import emotion_sense

        seen: list[list[str]] = []
        monkeypatch.setattr(
            emotion_sense,
            "sense",
            lambda texts: seen.append(texts) or {
                "state": "顺利", "mood": 90.0, "confidence": 0.9,
                "evidence": "x", "source": "llm",
            },
        )
        eng = _make_engine(tmp_path, events=[_evt("creation_rework")])
        self._diary(tmp_path, "今天陪他改稿，我挺开心的。", author="xiaoju")
        snap = EmotionTimeline(base_dir=tmp_path).record_today(eng)
        assert snap["source"] == "inferred", "小桔的日记不该被当成他的情绪证据"
        assert not seen

    def test_backfill_fills_history_from_diary(self, tmp_path, monkeypatch):
        """补写的日记要能把历史那天的推断标签纠正过来。"""
        from deskmate.growth import emotion_sense
        from deskmate.growth.diary_store import DiaryStore

        monkeypatch.setattr(
            emotion_sense,
            "sense",
            lambda texts: {
                "state": "低落", "mood": 48.0, "confidence": 0.85,
                "evidence": "特别困，根本没法集中精神", "source": "llm",
            },
        )
        eng = GrowthEngine(base_dir=tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        tl._data[day] = {
            "date": day, "state": "平静", "note": "旧推断", "mood": 78.0,
            "events": {}, "source": "inferred",
        }
        ts = datetime.datetime.combine(
            datetime.date.today() - datetime.timedelta(days=1),
            datetime.time(21, 0),
        ).astimezone().isoformat()
        with DiaryStore(base_dir=tmp_path)._connect() as conn:
            conn.execute(
                "INSERT INTO user_posts(id, author, content, created_at, loved) VALUES(?,?,?,?,0)",
                ("old1", "me", "特别困，根本没法集中精神。", ts),
            )
        assert tl.backfill_from_diary(eng, days=14) == 1
        assert tl._data[day]["state"] == "低落"
        assert tl._data[day]["source"] == "stated"


class TestSqliteMigration:
    """情感时间线已收口进统一库 deskmate.db（json_blobs）：legacy JSON 首访自动迁移+备份。"""

    def _write_legacy(self, tmp_path: Path, data: dict, name: str = "emotion_timeline.json") -> Path:
        g = tmp_path / "growth"
        g.mkdir(exist_ok=True)
        p = g / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def test_legacy_json_auto_migrated_to_db(self, tmp_path):
        legacy = {"2026-08-30": {"date": "2026-08-30", "mood": 80.0, "state": "压力大", "events": {}, "note": "3次不顺"}}
        self._write_legacy(tmp_path, legacy)
        tl = EmotionTimeline(base_dir=tmp_path)
        # 数据已进统一库
        assert tl._data == legacy
        # 原 JSON 已备份为 .bak、不再以原文件名存在
        assert not (tmp_path / "growth" / "emotion_timeline.json").exists()
        baks = [p for p in (tmp_path / "growth").iterdir() if p.name.startswith("emotion_timeline.json.bak")]
        assert len(baks) == 1
        # DB json_blobs 里确有该 key
        import sqlite3
        conn = sqlite3.connect(tmp_path / "deskmate.db")
        row = conn.execute("SELECT data FROM json_blobs WHERE key='emotion_timeline'").fetchone()
        assert row and "2026-08-30" in row[0]

    def test_roundtrip_from_db_and_idempotent(self, tmp_path):
        legacy = {"2026-08-31": {"date": "2026-08-31", "mood": 80.0, "state": "压力大", "events": {}, "note": "5次不顺"}}
        self._write_legacy(tmp_path, legacy)
        EmotionTimeline(base_dir=tmp_path)  # 首次迁移
        g = tmp_path / "growth"
        before = sorted(p.name for p in g.iterdir())
        # 重建对象：从 DB 读回，且不重复迁移/不重复备份
        tl2 = EmotionTimeline(base_dir=tmp_path)
        assert "2026-08-31" in tl2._data
        assert sorted(p.name for p in g.iterdir()) == before

    def test_new_data_written_to_db_not_legacy_file(self, tmp_path):
        from deskmate.growth.engine import GrowthEngine
        eng = _make_engine(tmp_path, mood=85.0, events=[_evt("creation_approved")])
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_today(eng)
        # 不再写 growth/emotion_timeline.json
        assert not (tmp_path / "growth" / "emotion_timeline.json").exists()
        # 重建后仍能读回（数据在统一库）
        tl2 = EmotionTimeline(base_dir=tmp_path)
        assert datetime.date.today().isoformat() in tl2._data
        assert tl2._data[datetime.date.today().isoformat()]["state"] == "顺利"


class TestTickIntegration:
    def test_emotion_injected_into_judge_context(self, tmp_path, monkeypatch):
        """proactive tick 的 judge 上下文应含情感回顾。"""
        from deskmate.perception import window_monitor as wm
        from deskmate.proactivity import tick as tick_mod
        from deskmate.proactivity.tick import ProactiveEngine

        # 造一个有压力信号的 growth state
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.mood = 68.0
        eng.state.recent_events = [_evt("creation_rework"), _evt("writing")]
        eng.save()

        class _Win:
            pass
        _Win.app = "Finder"
        _Win.title = "Desktop"
        _Win.url = None
        _Win.bundle_id = ""
        _Win.pid = 0

        captured: dict[str, str] = {}

        def _spy_judge(context_text, model=None):
            captured["text"] = context_text
            from deskmate.proactivity.judge import JudgmentResult
            return JudgmentResult(
                should_act=False, confidence=0.2, suggestion_type="none",
                suggestion_content="", reasoning="test",
            )

        monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
        monkeypatch.setattr(tick_mod, "judge_proactive_action", _spy_judge)
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.tick(user_message="", skip_activity_check=True)
        text = captured.get("text", "")
        assert "情感回顾" in text
        assert "压力大" in text


class TestStatementIngest:
    """F1/F2：访谈回答、聊天里说过的话，也是他亲口说的。

    此前情感时间线只认日记，于是他在访谈里答「情绪低落时我会……」
    只进画像不进当日心情——画像回答"他是什么人"，时间线回答
    "他今天怎么样"，两条腿缺一条就走不动。
    """

    @staticmethod
    def _sense(state, mood, evidence, confidence=0.85):
        return lambda texts: {
            "state": state, "mood": mood, "confidence": confidence,
            "evidence": evidence, "source": "llm",
        }

    def test_statement_becomes_stated_snapshot(self, tmp_path, monkeypatch):
        from deskmate.growth import emotion_sense

        eng = _make_engine(tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.record_today(eng)["source"] == "inferred"

        monkeypatch.setattr(
            emotion_sense, "sense",
            self._sense("低落", 40.0, "情绪低落时，我会看电影、听音乐"),
        )
        snap = tl.record_statement(eng, "情绪低落时，我会看电影、听音乐。")
        assert snap is not None
        assert snap["state"] == "低落"
        assert snap["source"] == "stated"
        assert snap["mood"] == 40.0
        assert eng.state.mood == 40.0, "自述结算后要反写 mood"

    def test_no_feeling_means_no_verdict(self, tmp_path, monkeypatch):
        """读不出情绪 → 不表态，绝不把认知表态硬判成心情。

        「书稿出版后就是商品」是他对创作的判断，不是那天的情绪。
        硬判成平静或低落，都是在替他编心情。
        """
        from deskmate.growth import emotion_sense

        # 造一个「不顺但人还在忙」的推断结论：total>3 才判压力大，
        # 否则 neg>=2 且 total<=3 会被判成「低落」（见 _infer_emotion 的活跃度分界）
        eng = _make_engine(
            tmp_path, events=[_evt("creation_rework")] * 3 + [_evt("writing")] * 3
        )
        tl = EmotionTimeline(base_dir=tmp_path)
        before = tl.record_today(eng)
        assert before["state"] == "压力大"

        monkeypatch.setattr(emotion_sense, "sense", lambda texts: None)
        got = tl.record_statement(eng, "一本书出版后它就是商品，这一点我分得很清楚。")
        assert got is None, "读不出情绪就返回 None"
        after = tl._data[before["date"]]
        assert after["state"] == "压力大", "原判定不许被无根据的自述覆盖"
        # 但自述本身要登记下来，下次再来一句时一起判
        assert after["statements"] == ["一本书出版后它就是商品，这一点我分得很清楚。"]

    def test_repeat_submission_does_not_rejudge(self, tmp_path, monkeypatch):
        from deskmate.growth import emotion_sense

        calls = []

        def _spy(texts):
            calls.append(list(texts))
            return {
                "state": "顺利", "mood": 82.0, "confidence": 0.8,
                "evidence": "挺好的", "source": "llm",
            }
        monkeypatch.setattr(emotion_sense, "sense", _spy)

        eng = _make_engine(tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_statement(eng, "今天挺好的。")
        n = len(calls)
        tl.record_statement(eng, "今天挺好的。")   # 前端重试
        assert len(calls) == n, "同一句重复提交不该再问一次模型"

    def test_merges_with_same_day_diary(self, tmp_path, monkeypatch):
        """当天写了日记又有访谈 → 合并后一起判，不是二选一。"""
        from deskmate.growth import emotion_sense
        from deskmate.growth.diary_store import DiaryStore

        DiaryStore(base_dir=tmp_path).add_user_post("上午改了一版稿子。", author="me")
        seen = {}

        def _spy(texts):
            seen["texts"] = list(texts)
            return {
                "state": "低落", "mood": 45.0, "confidence": 0.9,
                "evidence": "同时又让我感到忧伤", "source": "llm",
            }
        monkeypatch.setattr(emotion_sense, "sense", _spy)

        eng = _make_engine(tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        snap = tl.record_statement(eng, "访谈里他说今天很累。")
        assert snap["state"] == "低落"
        assert any("上午改了一版稿子" in t for t in seen["texts"]), "日记要一起参与判定"
        assert any("访谈里他说今天很累" in t for t in seen["texts"]), "自述也要参与"

    def test_batch_registers_once(self, tmp_path, monkeypatch):
        """批量提交 = 一次判定，不逐条问模型（省 token）。"""
        from deskmate.growth import emotion_sense

        calls = []
        monkeypatch.setattr(emotion_sense, "sense", lambda texts: calls.append(texts) or {
            "state": "平静", "mood": 70.0, "confidence": 0.6,
            "evidence": "还行", "source": "llm",
        })
        eng = _make_engine(tmp_path)
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_statements(eng, ["第一句", "第二句", "第三句"])
        assert len(calls) == 1, f"三条自述应合并成一次判定，实际问了 {len(calls)} 次"
        assert len(calls[0]) == 3

    def test_empty_input_is_noop(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.record_statement(_make_engine(tmp_path), "") is None
        assert tl.record_statements(_make_engine(tmp_path), ["", "  "]) is None

