"""P0-4 / 8.3 记忆全生命周期透明：引用标注 + 溯源 + 遗忘可见。

覆盖：
- build_full_context_for_turn 给记忆条目打 〔记忆#fp〕 溯源标注，并附 <memory-references>
  摘要块（P0-4 引用回显的基础：LLM 看到来源会自然带出「因为你之前说过……」）。
- 每轮注入记录写入 traces/inject.jsonl，read_trace 可读回（8.3 溯源）。
- decay_memory 归档/删除时写入 traces/forgot.jsonl（8.3 遗忘可见）。
- 非 § 文本（USER 画像等）不标注；无记忆时不写 trace。
"""

from __future__ import annotations

import json
import datetime
from pathlib import Path

import pytest

from deskmate.memory.manager import (
    DeskMateMemoryManager,
    FORGOT_TRACE_FILE,
    INJECT_TRACE_FILE,
)


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    (tmp_path / "memory").mkdir()
    (tmp_path / "projects").mkdir()
    return tmp_path


def _write_mem(base: Path) -> dict[str, str]:
    """写入测试用记忆，并返回各条目原文（供指纹断言动态推导，避免硬编码失效）。

    ⚠ 日期一律相对「今天」计算。早期这里写死 2026-08-05 / 2026-06-01 等绝对日期，
    随着真实时间推移，本以为「未超龄」的条目也会越过 30 天 TTL，于是
    ``archived == 1`` 无端失败（时间炸弹，2026-09-21 已实测复现）。
    """
    today = datetime.date.today()
    recent = (today - datetime.timedelta(days=5)).isoformat()     # 未超龄（work TTL=30）
    stale = (today - datetime.timedelta(days=110)).isoformat()    # 超龄 → 归档
    old_pref = (today - datetime.timedelta(days=60)).isoformat()  # 偏好永不衰减
    mid_pref = (today - datetime.timedelta(days=20)).isoformat()

    entries = {
        "old_pref": f"{old_pref}: 只查正史，排除自媒体来源",
        "mid_pref": f"{mid_pref}: 喜欢精炼简洁的书面语",
        "recent_work": f"{recent}: 正在写《兰陵王传》第三章",
        "stale_work": f"{stale}: 旧的过时工作条目",
    }

    mem = (
        "§ 偏好 #preference\n"
        f"{entries['old_pref']}\n\n"
        f"{entries['mid_pref']}\n\n"
        "§ 工作 #work\n"
        f"{entries['recent_work']}\n\n"
        f"{entries['stale_work']}\n"
    )
    (base / "MEMORY.md").write_text(mem, encoding="utf-8")
    (base / "USER.md").write_text("用户是历史题材创作者", encoding="utf-8")
    return entries


def _expected_fp(entry: str) -> str:
    """按契约独立重算指纹：sha1(条目原文) 前 8 位。

    刻意不调用被测的 ``_entry_fingerprint``——用独立实现才能在算法漂移时报警。
    """
    import hashlib

    return hashlib.sha1(entry.encode("utf-8")).hexdigest()[:8]


class TestInjectAnnotationAndTrace:
    def test_context_annotates_entries_and_adds_references(self, tmp_dir):
        entries = _write_mem(tmp_dir)
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        ctx = mm.build_full_context_for_turn("正史")

        # 条目被标注
        assert "〔记忆#" in ctx
        assert "只查正史" in ctx
        # references 摘要块 + 引用指引
        assert "<memory-references>" in ctx
        assert "因为你之前说过" in ctx
        assert f"〔记忆#{_expected_fp(entries['old_pref'])}〕" in ctx  # 稳定指纹

    def test_inject_trace_written_and_readable(self, tmp_dir):
        entries = _write_mem(tmp_dir)
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        mm.build_full_context_for_turn("正史")

        trace_path = tmp_dir / "traces" / INJECT_TRACE_FILE
        assert trace_path.exists()
        records = mm.read_trace(INJECT_TRACE_FILE, 5)
        assert len(records) == 1
        assert records[0]["n"] == 2
        assert records[0]["refs"][0]["fp"] == _expected_fp(entries["old_pref"])
        assert records[0]["refs"][0]["section"] == "偏好 #preference"

    def test_no_memory_no_trace(self, tmp_dir):
        # 无 MEMORY.md → 没有可溯源的 § 记忆条目 → 不写 inject trace
        # （USER 画像默认占位符仍会注入，但那不是可溯源记忆，不参与标注）
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        mm.build_full_context_for_turn("hello")
        assert not (tmp_dir / "traces" / INJECT_TRACE_FILE).exists()

    def test_annotate_ignores_non_section_text(self, tmp_dir):
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        marked, refs = mm._annotate_memory_blocks("## 用户画像\n纯文本，无 § 分节")
        assert marked == "## 用户画像\n纯文本，无 § 分节"
        assert refs == []


class TestForgotTrace:
    def test_decay_writes_forgot_trace(self, tmp_dir):
        _write_mem(tmp_dir)
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        result = mm.decay_memory()
        # 超龄工作条目（2026-06-01，>30天）被归档
        assert result["archived"] == 1
        assert result["forgot_count"] == 1

        trace_path = tmp_dir / "traces" / FORGOT_TRACE_FILE
        assert trace_path.exists()
        records = mm.read_trace(FORGOT_TRACE_FILE, 5)
        assert len(records) == 1
        assert records[0]["action"] == "archived"
        assert records[0]["section"] == "工作"
        assert "旧的过时工作条目" in records[0]["summary"]
        assert "ts" in records[0]

    def test_decay_preference_never_forgotten(self, tmp_dir):
        # 偏好永不过期：不产生 forgot 记录
        _write_mem(tmp_dir)
        mm = DeskMateMemoryManager(base_dir=tmp_dir)
        result = mm.decay_memory()
        forgot = mm.read_trace(FORGOT_TRACE_FILE, 5)
        # 只有工作条目被归档，偏好不衰减
        assert all(r["section"] == "工作" for r in forgot)


class TestTraceHttpEndpoint:
    def test_trace_endpoint_returns_records(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from coworker.server.app import create_app
        from coworker.server.manager import SessionManager

        # 先造 trace 数据：一次注入 + 一次衰减归档
        _write_mem(tmp_path)
        mm = DeskMateMemoryManager(base_dir=tmp_path)
        mm.build_full_context_for_turn("正史")
        mm.decay_memory()

        monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        client = TestClient(create_app(SessionManager(data_dir=tmp_path / "data")))

        r = client.get("/v1/deskmate/memory/trace")
        assert r.status_code == 200
        data = r.json()
        assert len(data["inject"]) == 1
        assert data["inject"][0]["n"] == 2
        assert len(data["forgot"]) == 1
        assert data["forgot"][0]["action"] == "archived"

        # kind 过滤
        only_forgot = client.get("/v1/deskmate/memory/trace", params={"kind": "forgot"}).json()
        assert "forgot" in only_forgot and "inject" not in only_forgot
