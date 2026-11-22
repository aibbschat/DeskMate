"""出站上下文总预算硬上限的契约测试（实验装置，默认关闭）。

红线（萧哥 2026-11-20 拍板）：
1. 默认关闭，关闭时逐字节不变（不动 prompt-cache 前缀契约）。
2. 不影响答案质量：人格 system / 首条 user / 末条 user / 原文 tool result 全部豁免。
3. 可观测：裁了多少、哪一路、裁了哪几轮都要落在 report 里。
4. fail-open：任何异常原样返回，绝不阻断一轮对话。
"""

from __future__ import annotations

import os

import pytest

from coworker import context_window_budget as cwb


def _msg(role: str, content: str, **extra):
    return {"role": role, "content": content, **extra}


@pytest.fixture
def convo():
    # 原文 tool 保持短，让「中间过程轮次」成为可裁主体——否则豁免底线过高，
    # 预算保护会直接触发 at_exempt_floor，测不到裁剪逻辑本身。
    return [
        _msg("system", "你是小桔。人格底座。"),
        _msg("user", "帮我理归营路线"),
        _msg("assistant", "好的先读原文"),
        _msg("tool", "第一章铜雀渡口村正文"),
        _msg("assistant", "读到第一章继续"),
        _msg("assistant", "让我再想想这里的地形"),
        _msg("assistant", "嗯其实是这样有一段山道"),
        _msg("tool", "第二章翻山到高村正文"),
        _msg("assistant", "还有补充一点"),
        _msg("user", "注意来时走山道"),
        _msg("assistant", "明白"),
        _msg("user", "整理最终路线"),
    ]


def test_disabled_by_default_is_byte_identical(convo, monkeypatch):
    monkeypatch.delenv("COWORKER_CONTEXT_TOTAL_BUDGET", raising=False)
    out, report = cwb.apply(convo)
    assert out == convo
    assert report["enabled"] is False
    assert report["before"] == report["after"]


def test_explicit_zero_is_disabled(convo, monkeypatch):
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "0")
    out, report = cwb.apply(convo)
    assert out == convo
    assert report["enabled"] is False


def test_measure_breaks_down_by_role(convo):
    stats = cwb.measure(convo)
    assert stats["system"] > 0
    assert stats["user"] > 0
    assert stats["assistant"] > 0
    assert stats["tool"] > 0
    assert stats["total"] == stats["system"] + stats["user"] + stats["assistant"] + stats["tool"]
    assert stats["n_messages"] == len(convo)


def test_system_first_and_last_user_are_exempt(convo, monkeypatch):
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "70")
    out, report = cwb.apply(convo)
    assert out[0]["content"] == convo[0]["content"]  # system
    assert out[1]["content"] == convo[1]["content"]  # 首条 user
    assert out[-1]["content"] == convo[-1]["content"]  # 末条 user
    assert "system" in report["exempt_routes"]
    assert "tool" in report["exempt_routes"]


def test_tool_results_exempt_by_default(convo, monkeypatch):
    """原文 tool result 是答案依据，默认绝不裁（质量红线）。"""
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "70")
    out, report = cwb.apply(convo)
    tool_before = [m["content"] for m in convo if m["role"] == "tool"]
    tool_after = [m["content"] for m in out if m["role"] == "tool"]
    assert tool_after == tool_before
    assert "tool" in report["exempt_routes"]


def test_middle_turns_removed_oldest_first(convo, monkeypatch):
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "70")
    out, report = cwb.apply(convo)
    # 中间的 assistant 纯文本轮次被优先移除，原文 tool 与两端 user 保留
    assert len(out) < len(convo)
    assert report["removed_turns"] >= 1
    # 仍然是旧的先走：最早的 assistant 过程轮（"好的先读原文"）应被移除
    assert all("好的先读原文" not in m.get("content", "") for m in out)
    # 两端 user 与原文 tool 都还在
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "整理最终路线"


def test_report_records_before_after_and_routes(convo, monkeypatch):
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "70")
    out, report = cwb.apply(convo)
    assert report["enabled"] is True
    assert report["before"] > report["after"]
    assert report["after"] <= 70
    assert "middle_text" in report["trimmed_routes"]
    assert report["removed_turns"] >= 1


def test_opt_in_tool_trimming(monkeypatch):
    """显式 opt-in 才允许裁 tool result（实验用，非默认）。

    构造只有 system + 大段原文 tool + 两端 user 的场景，确保除了 tool 没有
    别的可裁对象——这样"裁 tool"才会真正发生，不被中间过程轮次抢先。
    """
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "60")
    monkeypatch.setenv("COWORKER_CONTEXT_TRIM_TOOL_RESULTS", "1")
    msgs = [
        _msg("system", "人格"),
        _msg("user", "问题"),
        _msg("tool", "第一章" + "正" * 200),
        _msg("user", "最终问题"),
    ]
    out, report = cwb.apply(msgs)
    assert "tool" in report["trimmed_routes"]
    assert len(out[2]["content"]) < len(msgs[2]["content"])
    assert report["after"] < cwb.measure(msgs)["total"]


def test_budget_below_exempt_floor_is_noop(convo, monkeypatch):
    """预算低于豁免总量时不硬裁核心——宁可超预算也不伤答案。"""
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "1")
    out, report = cwb.apply(convo)
    assert out == convo
    assert report["at_exempt_floor"] is True


def test_fail_open_on_malformed_message(monkeypatch):
    """fail-open 的本质是「不崩、原样返回」——畸形项被安全跳过，不抛异常。"""
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "100")
    bad = [{"role": "system", "content": "人格"}, None, _msg("user", "问题")]
    out, report = cwb.apply(bad)  # 不抛即通过
    assert isinstance(out, list) and len(out) == len(bad)


def test_content_parts_counted(monkeypatch):
    """content 为 list（text+image part）时也要计量，不能漏算导致预算失真。"""
    msgs = [
        _msg("system", "人格"),
        {"role": "user", "content": [{"type": "text", "text": "甲" * 100}, {"type": "image_url", "image_url": {"url": "x"}}]},
        _msg("user", "末条"),
    ]
    stats = cwb.measure(msgs)
    assert stats["user"] >= 100
