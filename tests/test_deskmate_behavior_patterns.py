"""Tests for P2-3 长期模式学习——从行为推断偏好（deskmate/proactivity/behavior_patterns.py）。

核心理念：不只记「用户说过的话」，更从「行为轨迹」推断偏好。
零 LLM、fail-open、可观测、可单测。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from deskmate.proactivity import behavior_patterns as BP


def _write_event(desk: Path, kind: str, ts: float, payload: dict | None = None) -> None:
    line = json.dumps({"ts": ts, "kind": kind, "payload": payload or {}}, ensure_ascii=False)
    (desk / "behavior_log.jsonl").open("a", encoding="utf-8").write(line + "\n")


def _weekday_ts(y: int, m: int, d: int, h: int = 10) -> float:
    """构造某年月日小时的 unix 时间戳。"""
    import datetime

    return datetime.datetime(y, m, d, h, 0, 0).timestamp()


class TestLogEvent:
    def test_log_event_writes_jsonl(self, tmp_path):
        BP.log_event("suggestion_accepted", {"type": "x"}, base_dir=tmp_path)
        p = tmp_path / "behavior_log.jsonl"
        assert p.is_file()
        rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert rec["kind"] == "suggestion_accepted"
        assert rec["payload"]["type"] == "x"
        assert isinstance(rec["ts"], float)

    def test_log_reschedule_shape(self, tmp_path):
        BP.log_reschedule("审稿", from_weekday=3, to_weekday=4, base_dir=tmp_path)
        rec = json.loads((tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["kind"] == "reschedule"
        assert rec["payload"] == {"task_type": "审稿", "from_weekday": 3, "to_weekday": 4}

    def test_fail_open_on_bad_dir(self):
        # 不应抛异常（base_dir 指向不可写位置时静默）
        BP.log_event("x", {"a": 1}, base_dir="/nonexistent/deep/path/xyz")
        BP.mine_patterns(base_dir="/nonexistent/deep/path/xyz")


class TestWeekdayCadence:
    def test_detects_dominant_weekday(self, tmp_path):
        # 2026-08-03 是周一；连续 12 个周一都「接受提醒」（>=MIN_EVENTS 且跨多日）
        base = _weekday_ts(2026, 8, 3)  # 周一
        for w in range(12):
            ts = base + w * 7 * 86400
            _write_event(tmp_path, "suggestion_accepted", ts, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "周一" in joined
        assert "接受我的提醒" in joined

    def test_no_false_weekday_when_spread(self, tmp_path):
        # 均匀分布在各星期几 → 不应提炼出「常在周X」
        start = _weekday_ts(2026, 8, 3)  # 周一
        for wd in range(7):
            _write_event(tmp_path, "suggestion_accepted", start + wd * 86400, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert not any("常在周" in p for p in res["patterns"])


class TestHourCadence:
    def test_detects_active_hour(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 9)  # 周一 09:00
        for i in range(24):
            ts = base + i * 86400  # 每天 09:00 接受提醒（>=MIN_EVENTS*2 且跨多日）
            _write_event(tmp_path, "suggestion_accepted", ts, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert any("09:00" in p for p in res["patterns"])


class TestRecurrence:
    def test_detects_stable_interval(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        # 每 7 天一次，连续 5 次（稳定节奏）
        for i in range(5):
            _write_event(tmp_path, "creation_approved", base + i * 7 * 86400)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert any("每 7 天" in p for p in res["patterns"])

    def test_no_recurrence_when_irregular(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for off in [0, 1, 30, 200]:
            _write_event(tmp_path, "creation_approved", base + off * 86400)
        res = BP.mine_patterns(base_dir=tmp_path, days=400)
        assert not any("每" in p and "天一次" in p for p in res["patterns"])


class TestDeferral:
    def test_detects_thursday_to_friday(self, tmp_path):
        # 连续 3 次把「审稿」从周四(3)推到周五(4)
        for _ in range(3):
            BP.log_reschedule("审稿", from_weekday=3, to_weekday=4, ts=time.time(),
                              base_dir=tmp_path)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "周四" in joined and "周五" in joined and "审稿" in joined
        assert "避开周四" in joined

    def test_no_deferral_on_inconsistent(self, tmp_path):
        BP.log_reschedule("审稿", 3, 4, ts=time.time(), base_dir=tmp_path)
        BP.log_reschedule("审稿", 1, 2, ts=time.time(), base_dir=tmp_path)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert not any("避开" in p for p in res["patterns"])


class TestToContext:
    def test_empty_when_no_data(self, tmp_path):
        assert BP.to_context(base_dir=tmp_path) == ""
        res = BP.mine_patterns(base_dir=tmp_path)
        assert res["event_count"] == 0
        assert res["patterns"] == []

    def test_context_formats_patterns(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for w in range(12):
            _write_event(tmp_path, "suggestion_accepted", base + w * 7 * 86400, {"type": "plot"})
        ctx = BP.to_context(base_dir=tmp_path, days=120)
        assert "从你长期行为推断" in ctx
        assert "周一" in ctx

    def test_focus_deep_dedup(self, tmp_path):
        now = time.time()
        BP.log_focus_deep(app="Code", base_dir=tmp_path)  # 记一次
        BP.log_focus_deep(app="Code", base_dir=tmp_path)  # 30s 内应被去重
        lines = (tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert sum(1 for l in lines if json.loads(l)["kind"] == "focus_deep") == 1


class TestP2CreationFeedbackLogging:
    """P2-10：创作定稿/打回应进入行为日志，供长期模式学习。

    验证 creation_approved / creation_rejected 两类事件可被 mine_patterns 识别，
    且对应中文「行为规律」摘要能生成（与 AlterEgoPage 行为规律栏目打通）。
    """

    def test_creation_approved_mined(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for i in range(5):
            _write_event(tmp_path, "creation_approved", base + i * 7 * 86400, {"pipeline": "P1"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "通过创作产出" in joined  # creation_approved 的中文映射

    def test_creation_rejected_mined(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for i in range(5):
            _write_event(tmp_path, "creation_rejected", base + i * 7 * 86400, {"pipeline": "P1"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "打回创作重做" in joined  # creation_rejected 的中文映射


class TestP2ForgettingVisible:
    """P2-11：记忆衰减应真实产生 traces/forgot.jsonl，让「遗忘可见」。

    此前 decay_memory 从未进入每轮流程、traces/forgot.jsonl 永远为空。
    这里验证 cleanup_redundancy（after_action 现每 N 轮调用）确实把超龄 #note 写入遗忘 trace。
    """

    def test_decay_writes_forgot_trace(self, tmp_path):
        from deskmate.memory.manager import DeskMateMemoryManager
        import datetime

        old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        mem = f"§ 笔记 #note\n{old}: 一条过期临时备忘，应被删除\n\n§ 喜好 #preference\n用户偏好安静\n"
        (tmp_path / "MEMORY.md").write_text(mem, encoding="utf-8")

        mgr = DeskMateMemoryManager(tmp_path)
        res = mgr.cleanup_redundancy()
        assert res["decay"]["deleted"] == 1
        assert res["decay"]["forgot_count"] == 1

        forgot = tmp_path / "traces" / "forgot.jsonl"
        assert forgot.exists()
        rec = json.loads(forgot.read_text(encoding="utf-8").splitlines()[0])
        assert rec["action"] == "deleted"
        assert rec["section"] == "笔记"


class TestP2BehaviorLoggingFiveScenarios:
    """P2-10 五处高频场景补全（聊天每轮/圆桌/审稿/主动推送/@mention）。

    此前行为日志仅「创作定稿」1 处（creation_approved/rejected）。这里验证其余 4 处可触发：
    mention_collab / review_done 走真实代码路径（捕获 log_event 调用、不落盘）；
    chat_turn / roundtable_done / suggestion_pushed 走 kind 契约——确保 InsightPanel 与
    behavior-patterns 端点「有米下锅」。
    """

    def test_mention_collab_logged(self, tmp_path, monkeypatch):
        from deskmate.chat import mention_router as MR

        captured = {}
        monkeypatch.setattr(MR, "_mention_enabled", lambda: True)
        monkeypatch.setattr(MR, "unified_roster", lambda: [
            {"id": "historian", "name": "史官", "role": "考据"},
        ])
        monkeypatch.setattr(
            "deskmate.proactivity.behavior_patterns.log_event",
            lambda kind, payload=None, ts=None, base_dir=None: captured.setdefault("kind", kind),
        )
        MR.mention_context(
            [{"role": "user", "content": "请问 @historian 这处史实？"}],
            default_persona_id=None,
        )
        assert captured.get("kind") == "mention_collab"

    def test_review_done_logged(self, tmp_path, monkeypatch):
        from deskmate.tools import review as RV
        import json

        canned = json.dumps({
            "summary": "本章节奏紧凑",
            "analysis": "人物塑造成功",
            "issues": [{"type": "节奏", "excerpt": "中段略拖", "comment": "可压缩"}],
        })
        monkeypatch.setattr(RV, "chat", lambda *a, **k: canned)
        monkeypatch.setattr(RV, "llm_available", lambda: True)
        captured = {}
        monkeypatch.setattr(
            "deskmate.proactivity.behavior_patterns.log_event",
            lambda kind, payload=None, ts=None, base_dir=None: captured.setdefault("kind", kind),
        )
        RV.review_chapter("第一章：测试", "正文内容足够长……")
        assert captured.get("kind") == "review_done"

    def test_five_new_kinds_counted_by_mine_patterns(self, tmp_path):
        from deskmate.proactivity import behavior_patterns as BP

        base = _weekday_ts(2026, 8, 3, 10)
        kinds = ["chat_turn", "roundtable_done", "review_done", "suggestion_pushed", "mention_collab"]
        for i, k in enumerate(kinds):
            _write_event(tmp_path, k, base + i * 86400, {"x": i})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        # 5 种新事件都应被计入 event_count（证明不会因「未知 kind」被丢弃）
        assert res["event_count"] == 5
        # 原始日志确实含全部 5 种 kind——behavior-patterns 端点「有米下锅」
        logged = (tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8")
        for k in kinds:
            assert k in logged


class TestNoiseControl:
    """2026-09-22：降噪 + 「内部键不上屏」白名单。

    实测暴露的两个问题：
    - `suggestion_pushed` 占行为日志 94%（13197/14069 条），却提炼不出任何模式
      （推送时段分散，过不了 dominance 阈值）→ 纯存储噪声；
    - 未登记标签的 kind 会被 `_kind_label` 原样回退成内部键，直接拼进喂给 judge
      的上下文（实测输出过「你常在周一做『roundtable_done』」）。
    """

    def test_hourly_dedup_collapses_burst(self, tmp_path):
        """同小时内同类事件只落一行；跨小时照记（保留分布形状）。"""
        base = _weekday_ts(2026, 8, 3, 9)  # 周一 09:00
        for i in range(50):                # 同一小时内 50 次推送
            BP.log_event("suggestion_pushed", {"type": "t", "key": f"k{i}"},
                         ts=base + i * 30, base_dir=tmp_path, dedup_per_hour=True)
        for h in range(1, 4):              # 之后 3 个不同小时各 1 次
            BP.log_event("suggestion_pushed", {"type": "t", "key": "k"},
                         ts=base + h * 3600, base_dir=tmp_path, dedup_per_hour=True)
        lines = (tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4, f"50 条应压成 1 条 + 3 个新小时，实际 {len(lines)}"

    def test_hourly_dedup_off_by_default(self, tmp_path):
        """默认不去重——correction / focus_deep 等逐条记，行为完全不变。"""
        base = _weekday_ts(2026, 8, 3, 9)
        for i in range(5):
            BP.log_event("correction", {"n": i}, ts=base + i * 30, base_dir=tmp_path)
        lines = (tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

    def test_unlabeled_kind_never_enters_context(self, tmp_path):
        """未登记标签的 kind（技术诊断）不参与提炼 → 内部键不上屏。"""
        base = _weekday_ts(2026, 8, 3, 10)  # 周一
        for i in range(12):
            _write_event(tmp_path, "proactive_tick_http", base + i * 3600, {})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert res["event_count"] == 12            # 全量统计不受影响
        assert res["kinds"] == ["proactive_tick_http"]
        assert res["patterned_kinds"] == []        # 但没有任何事件参与提炼
        assert res["patterns"] == []
        assert "proactive_tick_http" not in res["context"]

    def test_labeled_kind_renders_human_text(self, tmp_path):
        """登记过的 kind 输出人话，绝不出现内部键。"""
        mon = _weekday_ts(2026, 8, 3, 9)
        ts_list = [mon + i * 3600 for i in range(7)]                # 周一 7 条
        ts_list += [mon + 86400 * i + 7200 for i in range(1, 7)]    # 周二..周日各 1 条
        for t in ts_list:
            _write_event(tmp_path, "roundtable_done", t, {})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert res["patterns"], "应提炼出「常在周一做圆桌讨论」"
        assert "roundtable_done" not in res["context"], "内部键不许上屏"
        assert "开圆桌讨论" in res["context"]


class TestPatternThresholds:
    """方案A：避免小样本/单日数据被当成长期规律（P2-10 后续收口）。

    - 某 kind 事件 < MIN_EVENTS(10) 不输出规律
    - 即使 >=10，若只跨 1 个自然日（同天刷 N 次）也不输出「常在周X/HH:00」
    - roundtable_done 同一 topic 30 秒内只记一次（防 run_stream 重入重复记）
    """

    def test_single_day_no_false_pattern(self):
        # 单日12条全周日——此前会输出「常在周日」，现应被跨度保护拦截
        base = _weekday_ts(2026, 8, 30, 14)
        ev = [{"kind": "roundtable_done", "ts": base + i} for i in range(12)]
        assert BP._detect_weekday_cadence(ev) == []
        assert BP._detect_hour_cadence(ev) == []

    def test_cross_week_pattern_emerges(self):
        # 跨3个不同周日各4条=12条，且都周日 → 应输出「常在周日」
        base = _weekday_ts(2026, 8, 30, 14)
        ev = []
        for d in (base, base - 7 * 86400, base - 14 * 86400):
            for i in range(4):
                ev.append({"kind": "roundtable_done", "ts": d + i * 60})
        out = BP._detect_weekday_cadence(ev)
        assert any("周日" in s for s in out)

    def test_roundtable_dedup_same_topic(self):
        from unittest import mock
        import deskmate.roundtable.coordinator as coord

        calls = []
        with mock.patch.object(BP, "log_event", side_effect=lambda k, p=None: calls.append((k, p))):
            coord._ROUNDTABLE_LOG_LAST.clear()
            for _ in range(5):
                coord._log_roundtable_done_once("议题X", 2)
            # 超过去重窗口(30s)后再记应放行
            coord._ROUNDTABLE_LOG_LAST["议题X"] = time.time() - 40
            coord._log_roundtable_done_once("议题X", 2)
        assert len(calls) == 2

