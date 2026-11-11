"""四段·检索权：只读检索工具 + 闲聊门 + 相关性闸。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from deskmate.memory import file_search
from deskmate.memory.file_search import gate_hits, is_chitchat
from deskmate.tools import memory_search as ms
from deskmate.tools.registry import FACTORIES, build_tools


# ---------------------------------------------------------------------------
# 只读保证
# ---------------------------------------------------------------------------
def test_tool_is_read_only():
    """工具源码里不得出现写盘/写库调用——写入权不放开是四段的硬边界。"""
    src = Path(ms.__file__).read_text(encoding="utf-8")
    body = re.sub(r'""".*?"""', "", src, flags=re.S)  # 去掉文档字符串
    for bad in ("write_text", "open(", "sqlite3", "append_to_section", "save_user", ".commit("):
        assert bad not in body, f"memory_search 出现了写操作痕迹：{bad}"


def test_registry_builds_default_tool(tmp_path):
    tools = build_tools(base=tmp_path)
    assert [t.__name__ for t in tools] == ["memory_search"]
    assert "memory_search" in FACTORIES
    assert tools[0].__coworker_schema__["function"]["name"] == "memory_search"


# ---------------------------------------------------------------------------
# 层过滤 / 日期过滤 / base 固化
# ---------------------------------------------------------------------------
@pytest.fixture()
def spy(monkeypatch):
    calls: list[dict] = []

    def fake_search(query, **kwargs):
        calls.append({"query": query, **kwargs})
        return [
            {"content": "2026-11-04：他喜欢御姐音", "source_path": "USER.md",
             "section": "偏好与事实", "score": 0.9},
            {"content": "很久以前的事（无日期）", "source_path": "MEMORY.md",
             "section": "创作", "score": 0.5},
        ]

    monkeypatch.setattr(ms, "search_memory_files", fake_search)
    return calls


def test_layer_maps_to_kinds_and_section(tmp_path, spy):
    tool = ms.make_memory_search_tool(base=tmp_path)
    out = tool("我喜欢什么音色", layer="preference")
    assert spy[0]["kinds"] == ["user"]
    assert spy[0]["section_contains"] == "偏好与事实"
    assert out["count"] == 2


def test_unknown_layer_rejected(tmp_path, spy):
    tool = ms.make_memory_search_tool(base=tmp_path)
    out = tool("x", layer="whatever")
    assert "error" in out and out["count"] == 0
    assert spy == []


def test_date_filter_keeps_undated(tmp_path, spy):
    tool = ms.make_memory_search_tool(base=tmp_path)
    out = tool("音色", since="2026-11-01", until="2026-11-30")
    dates = [r["date"] for r in out["results"]]
    assert "2026-11-04" in dates
    # 无日期条目不受过滤影响（宁多勿漏）
    assert "" in dates and out["count"] == 2


def test_base_is_frozen_at_build_time(tmp_path, spy, monkeypatch):
    tool = ms.make_memory_search_tool(base=tmp_path)
    monkeypatch.setenv("DESKMATE_MEMORY_DIR", "/tmp/some-other-dir")
    tool("声")
    assert str(spy[0]["base"]) == str(tmp_path)


# ---------------------------------------------------------------------------
# 闲聊门
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expect", [
    ("早", True), ("早上好！", True), ("谢谢", True), ("好的", True), ("在吗", True),
    ("哈哈", True), ("早写完了吗", False), ("今天天气怎么样", False),
    ("好的，把第三章改成倒叙", False), ("", False), ("嗯", True),
])
def test_is_chitchat(text, expect):
    assert is_chitchat(text) is expect


# ---------------------------------------------------------------------------
# 相关性闸
# ---------------------------------------------------------------------------
def _hit(score, channels, vec=0.0):
    return {"score": score, "channels": channels, "vec_score": vec, "id": str(score)}


def test_gate_keeps_top_and_drops_weak_tail():
    hits = [_hit(0.09, ["vector"], 0.55), _hit(0.06, ["keyword"]),
            _hit(0.03, ["vector"], 0.10), _hit(0.02, ["vector"], 0.05)]
    out = gate_hits(hits)
    # 头部 + 双通道（虽然分低但被两路印证）；尾部两条弱相关被挡
    assert len(out) == 2
    assert out[0]["score"] == 0.09


def test_gate_multi_channel_survives():
    hits = [_hit(0.09, ["vector"], 0.5), _hit(0.02, ["vector", "keyword"], 0.1)]
    assert len(gate_hits(hits)) == 2


def test_gate_empty():
    assert gate_hits([]) == []


# ---------------------------------------------------------------------------
# 指代扩展（pref-02c / pref-04c）
# ---------------------------------------------------------------------------
def test_anaphora_query_uses_previous_turn():
    from deskmate.memory.manager import _expand_query

    prev = "念稿的声音我不喜欢，太播音腔了"
    out = _expand_query("要换成哪一种", prev)
    assert "念稿" in out and out.endswith("要换成哪一种")


def test_plain_long_query_not_expanded():
    from deskmate.memory.manager import _expand_query

    q = "《夜不收》第三章的改稿要求有哪些，逐条列出来"
    assert _expand_query(q, "上一轮在聊别的") == q


def test_no_prev_turn_keeps_query():
    from deskmate.memory.manager import _expand_query

    assert _expand_query("那一条", "") == "那一条"
