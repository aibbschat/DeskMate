"""后台心跳 + judge 上下文指纹去重（2026-11-17）。

两条都是「静默退化」的护栏：
- 心跳：她上次真的思考是什么时候——模型/通道挂了要让用户看得见，而不是只感觉
  「她最近有点安静」；
- 去重：tick 30s 一跳，输入没变时不重判——省额度，也避免同一条发现被反复喂给模型。
"""
from __future__ import annotations

import json
import time


# ---------------------------------------------------------------- heartbeat
def test_heartbeat_roundtrip_records_judge_and_ages(tmp_path):
    from deskmate.proactivity.heartbeat import read_heartbeat, record_judge, record_tick

    record_tick(tmp_path, skipped_reason="上下文未变化", acted=False, model="m")
    record_judge(tmp_path, model="xiaomi:mimo-v2.6-pro")

    hb = read_heartbeat(tmp_path)
    assert hb["model"] == "xiaomi:mimo-v2.6-pro"
    assert hb["last_skip_reason"] == "上下文未变化"
    assert hb["judge_calls_today"] == 1
    assert hb["ticks_today"] == 1
    assert isinstance(hb["judge_age_seconds"], int)
    assert isinstance(hb["tick_age_seconds"], int)


def test_heartbeat_counts_accumulate_and_reset_on_new_day(tmp_path):
    from deskmate.proactivity.heartbeat import read_heartbeat, record_judge, record_tick

    record_tick(tmp_path)
    record_tick(tmp_path)
    record_judge(tmp_path)
    record_judge(tmp_path)
    hb = read_heartbeat(tmp_path)
    assert hb["ticks_today"] == 2
    assert hb["judge_calls_today"] == 2

    # 模拟「昨天」：日切后计数从 1 重新开始，而不是继续累加
    path = tmp_path / "proactive_heartbeat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["judge_date"] = "2000-01-01"
    path.write_text(json.dumps(data), encoding="utf-8")
    record_judge(tmp_path)
    assert read_heartbeat(tmp_path)["judge_calls_today"] == 1


def test_heartbeat_never_created_returns_empty(tmp_path):
    from deskmate.proactivity.heartbeat import read_heartbeat

    hb = read_heartbeat(tmp_path)
    assert hb["judge_age_seconds"] is None
    assert hb["tick_age_seconds"] is None


def test_corrupt_heartbeat_file_is_ignored(tmp_path):
    from deskmate.proactivity.heartbeat import read_heartbeat, record_judge

    (tmp_path / "proactive_heartbeat.json").write_text("{not json", encoding="utf-8")
    record_judge(tmp_path)  # 坏文件不该抛
    hb = read_heartbeat(tmp_path)
    assert hb["judge_calls_today"] == 1


# ---------------------------------------------------------------- dedupe
class _Win:
    app = "Finder"
    title = "Desktop"
    url = None
    bundle_id = ""
    pid = 0


def _engine(tmp_path, monkeypatch, judge_calls: list[int]):
    from deskmate.perception import window_monitor as wm
    from deskmate.proactivity import tick as tick_mod
    from deskmate.proactivity.judge import JudgmentResult

    def fake_judge(*_a, **_k):
        judge_calls[0] += 1
        return JudgmentResult(
            should_act=False,
            confidence=0.1,
            suggestion_type="none",
            suggestion_content="",
            reasoning="test",
        )

    monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
    monkeypatch.setattr(tick_mod, "judge_proactive_action", fake_judge)
    # 指纹只测「输入相同就不重判」，把上下文固定住，避免真实感知字段漂移干扰
    ctx = {"text": "固定的上下文文本 " * 10}
    monkeypatch.setattr(
        tick_mod, "format_context_for_judge", lambda _c: ctx["text"]
    )
    return tick_mod.ProactiveEngine(base_dir=tmp_path), ctx


def test_identical_context_skips_judge(tmp_path, monkeypatch):
    calls = [0]
    engine, _ctx = _engine(tmp_path, monkeypatch, calls)

    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1

    res = engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1, "上下文没变不该再调 judge"
    assert "上下文未变化" in (res.skipped_reason or "")


def test_context_change_rejudges(tmp_path, monkeypatch):
    calls = [0]
    engine, ctx = _engine(tmp_path, monkeypatch, calls)
    engine.tick(skip_activity_check=True, allow_interactive=False)

    # 换一份输入（等价于用户换窗口/剪贴板变化）→ 必须重新判
    ctx["text"] = "换了一份上下文 " * 20
    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 2


def test_rejudge_after_window_even_if_context_same(tmp_path, monkeypatch):
    """长时间静坐（画面一直不动）不会永久静音：窗口到点照常重判一次。"""
    calls = [0]
    engine, _ctx = _engine(tmp_path, monkeypatch, calls)
    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1

    # 指纹以心跳文件为准（两个 engine 实例共享），所以要改落盘时间而不是实例属性
    path = tmp_path / "proactive_heartbeat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["last_judge_at"] = time.time() - 3700  # 超过 1h 重判窗口
    path.write_text(json.dumps(data), encoding="utf-8")

    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 2


def test_pending_self_expression_bypasses_dedupe(tmp_path, monkeypatch):
    """情绪引擎挂着「想说一句」时必须放行：pending 不依赖上下文变化。"""
    import deskmate.growth.emotion_engine as ee_mod

    calls = [0]
    engine, _ctx = _engine(tmp_path, monkeypatch, calls)
    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1

    class _FakeEmotion:
        def __init__(self, base_dir=None):
            pass

        def get_self_expression_pending(self):
            return True

    monkeypatch.setattr(ee_mod, "EmotionEngine", _FakeEmotion)
    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 2, "有 pending 自我表达时不许被指纹去重挡掉"


def test_dedupe_is_shared_across_engines(tmp_path, monkeypatch):
    """前台 tick 与后台 scheduler 是两个 engine 实例——指纹必须共享，否则各判一遍。"""
    from deskmate.proactivity.tick import ProactiveEngine

    calls = [0]
    engine, _ctx = _engine(tmp_path, monkeypatch, calls)
    other = ProactiveEngine(base_dir=tmp_path)

    engine.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1
    res = other.tick(skip_activity_check=True, allow_interactive=False)
    assert calls[0] == 1
    assert "上下文未变化" in (res.skipped_reason or "")
