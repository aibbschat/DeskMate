"""curator 后台报告停产契约（2026-11-20 萧哥拍板）。

报告文件零内容读者 ⇒ 不再生产；现场现算的 HTTP 端点 / SkillStore 页面保留。
后台巡检默认关（auto_archive 未开）时直接早退，不因报告文件消失而退化成
每轮全扫；opt-in 时用轻量时间戳节流。
"""

from __future__ import annotations


def test_curate_skills_no_longer_writes_report(tmp_path, monkeypatch):
    from deskmate.skills import curator

    sd = tmp_path / "skills"
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(sd))
    result = curator.curate_skills(tmp_path, auto_archive=False)
    assert result["ok"] is True
    assert not (sd / "curator_report.md").exists()


def test_background_curator_is_noop_when_archive_off(tmp_path, monkeypatch):
    from deskmate.memory import after_action as aa
    from deskmate.skills import curator

    sd = tmp_path / "skills"
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(sd))
    calls: list[int] = []
    monkeypatch.setattr(curator, "auto_archive_enabled", lambda: False)
    monkeypatch.setattr(
        curator, "curate_skills", lambda *a, **k: calls.append(1) or {"ok": True}
    )
    aa._maybe_curate_skills(tmp_path)
    assert calls == []


def test_opt_in_throttle_uses_marker_not_report(tmp_path, monkeypatch):
    from deskmate.skills import curator

    sd = tmp_path / "skills"
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(sd))
    assert curator._should_run(tmp_path) is True
    (sd / ".curator_last_run").write_text("stamp", encoding="utf-8")
    assert curator._should_run(tmp_path) is False
