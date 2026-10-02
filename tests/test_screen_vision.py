"""屏幕视觉观察的本地结构化存储与解析测试。

截图和真实视觉模型均打桩：测试只验证隐私边界（不保存 PNG）、结果落盘、
容错 JSON 解析和最近记录读取；不触发 macOS TCC，也不调用外部 API。
"""

from __future__ import annotations

import json
from pathlib import Path

from deskmate.perception import screen_vision as sv


def test_screen_routes_list_and_observe(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    desk_dir = tmp_path / "desk"
    desk_dir.mkdir()
    monkeypatch.setenv("DESKMATE_DIR", str(desk_dir))
    observation = {
        "ts": "2026-10-02T12:00:00",
        "scene": "代码编辑",
        "app": "OpenCode",
        "activity": "正在修改代码",
        "attention": "终端",
        "mood": "work",
        "confidence": 0.8,
    }
    def fake_observe(base_dir):
        path = Path(base_dir) / "agency" / "screen_observations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(observation, ensure_ascii=False) + "\n", encoding="utf-8")
        return observation

    monkeypatch.setattr(sv, "observe_screen", fake_observe)
    client = TestClient(create_app(SessionManager(data_dir=tmp_path / "sessions")))

    listed = client.get("/v1/deskmate/perception/screen")
    assert listed.status_code == 200
    assert listed.json()["observations"] == []

    triggered = client.post("/v1/deskmate/perception/screen/observe")
    assert triggered.status_code == 200
    assert triggered.json()["observation"] == observation

    listed = client.get("/v1/deskmate/perception/screen")
    assert listed.json()["observations"] == [observation]


def test_parse_json_object_accepts_markdown_fence():
    raw = """```json
    {"scene": "代码编辑", "app": "OpenCode", "activity": "正在修代码", "attention": "终端", "mood": "work", "confidence": 0.8}
    ```"""
    parsed = sv._parse_json_object(raw)
    assert parsed["scene"] == "代码编辑"


def test_normalize_observation_clamps_and_defaults():
    obs = sv._normalize_observation({
        "scene": "浏览器查资料",
        "app": "Chrome",
        "activity": "看新闻",
        "attention": "标题",
        "mood": "unexpected",
        "confidence": "9",
    })
    assert obs["mood"] == "unknown"
    assert obs["confidence"] == 1.0


def test_observe_screen_persists_structured_record_without_png(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "capture_screen", lambda: b"fake-png")
    monkeypatch.setattr(sv, "analyze_screen", lambda png: {
        "scene": "文稿写作",
        "app": "Pages",
        "activity": "修改章节",
        "attention": "段落",
        "mood": "writing",
        "confidence": 0.7,
    })

    rec = sv.observe_screen(tmp_path)
    assert rec["ts"]
    assert rec["scene"] == "文稿写作"

    records = sv.list_screen_observations(tmp_path)
    assert len(records) == 1
    assert records[0]["mood"] == "writing"

    images = list(Path(tmp_path).rglob("*.png"))
    assert images == []


def test_list_screen_observations_ignores_bad_lines(tmp_path):
    path = Path(tmp_path) / "agency" / "screen_observations.jsonl"
    path.parent.mkdir(parents=True)
    good = {"ts": "2026-10-02T12:00:00", "scene": "代码编辑"}
    path.write_text("bad line\n" + json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
    assert sv.list_screen_observations(tmp_path) == [good]
