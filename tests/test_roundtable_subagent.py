"""Tests for Roundtable per-persona isolated history (Spawn Subagent mode).

Verifies that when ``per_persona_history=True`` each persona is prompted with only
its OWN prior turns (plus the global story) — not the full table transcript — so a
verbose speaker can't blow out everyone else's context. Conversely, the default
mode shares the full transcript.
"""
import asyncio

import pytest

from deskmate.roundtable import coordinator as C
from deskmate.roundtable.coordinator import Roundtable
from deskmate.roundtable.personas import PERSONAS


def _fake_llm(monkeypatch, sink: list[str]):
    """Record every prompt verbatim; replies carry a leading '- ' (so the
    divergence parser finds a real分歧点 and the roundtable proceeds to round 2)."""
    monkeypatch.setattr(C, "llm_available", lambda: True)

    async def fake_stream(system, prompt, **kw):
        sink.append(prompt)
        yield f"- 分歧点标记 <<REPLY-{len(sink)}>>"

    monkeypatch.setattr(C, "chat_stream", fake_stream)


def _two_personas():
    by_id = {p.id: p for p in PERSONAS}
    ids = list(by_id)[:2]
    return [by_id[i] for i in ids]


def _round2_persona_prompts(sink: list[str]) -> list[str]:
    """Round-2 persona turn prompts (narrowing-defense framing)."""
    return [p for p in sink if "本轮为收窄式答辩" in p]


def _own_round1_lines(prompt: str) -> int:
    """How many '【第1轮 · <persona>（...）】' speaker blocks appear — i.e. how
    many OTHER personas' round-1 turns leaked into this prompt."""
    return prompt.count("【第1轮 ·")


@pytest.mark.asyncio
async def test_subagent_mode_isolates_round2_history(monkeypatch):
    sink: list[str] = []
    _fake_llm(monkeypatch, sink)
    personas = _two_personas()

    rt = Roundtable(
        topic="测试议题：某明代小说开篇",
        personas=personas,
        max_rounds=2,
        per_persona_history=True,
    )
    await rt.run()

    r2 = _round2_persona_prompts(sink)
    assert r2, "expected round-2 persona prompts"
    # Isolated history: each round-2 prompt shows ONLY that speaker's own round-1
    # block (== 1), never the other persona's.
    for p in r2:
        assert _own_round1_lines(p) == 1, (
            f"subagent mode leaked {_own_round1_lines(p)} round-1 speakers into a prompt"
        )


@pytest.mark.asyncio
async def test_default_mode_shares_round2_transcript(monkeypatch):
    sink: list[str] = []
    _fake_llm(monkeypatch, sink)
    personas = _two_personas()

    rt = Roundtable(
        topic="测试议题：某宋代笔记",
        personas=personas,
        max_rounds=2,
        per_persona_history=False,  # default
    )
    await rt.run()

    r2 = _round2_persona_prompts(sink)
    assert r2, "expected round-2 persona prompts"
    # Default mode shares the full transcript: each round-2 prompt carries BOTH
    # personas' round-1 blocks (== 2).
    assert any(_own_round1_lines(p) >= 2 for p in r2), (
        "default mode should share the full transcript into each speaker's prompt"
    )
