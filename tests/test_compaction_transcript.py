"""OPE-186 change 3 (restored 2026-11-18): the compacted turns stay readable — the
verbatim transcript is written to the spill folder and the compacted block tells the
model where it is. Scripted provider, tiny forced window, no network.

The original 65ba3943 also wired a config-level `compaction_cap_tokens`; in this tree
the cap lives in prefs via the manager's compaction_settings, so only the transcript
contract is tested here."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coworker import compaction as C
from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.tools import ToolRegistry

SUMMARY = "## Primary request and intent\nkeep building the report"


class CompactingProvider(ProviderClient):
    def __init__(self, turns, *, summary_fails=0):
        self._turns = list(turns)
        self.summary_fails = summary_fails

    def complete(self, *, model, messages, tools=None, **settings):
        if messages and "compacting an AI coworker" in str(messages[0].get("content", "")):
            if self.summary_fails > 0:
                self.summary_fails -= 1
                raise RuntimeError("summarizer down")
            return AssistantTurn(text=SUMMARY, finish_reason="stop")
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _history(turns=8, bulk=1500):
    msgs = [{"role": "system", "content": "be helpful"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"request {i}", "ts": 1.0})
        msgs.append(
            {
                "role": "assistant",
                "content": f"answer {i} " + "x" * bulk,
                "ts": 1.0,
                "reasoning": "private thinking " + "r" * 200,
                "tool_calls": [{"id": f"c{i}", "function": {"name": "run_shell", "arguments": '{"command": "ls -la"}'}}],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"listing {i}: file_{i}.txt", "ts": 1.0})
    return msgs


def _collect(engine):
    async def go():
        return [e async for e in engine.run("continue")]

    return asyncio.run(go())


def test_compaction_writes_transcript_and_tells_the_model(tmp_path):
    spill = tmp_path / "spill"
    engine = TurnEngine(
        provider=CompactingProvider([AssistantTurn(text="done", finish_reason="stop")]),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        messages=_history(),
        tool_result_spill_dir=spill,
    )
    engine.compaction_settings = lambda: {"cap_tokens": 400, "threshold_pct": 0.8, "context_window": 100_000}
    events = _collect(engine)
    assert any(e.type == EventType.COMPACTED for e in events)
    state = engine.compaction_state
    assert state is not None and state.transcript_path
    files = list(spill.glob("compacted-transcript-upto-*.md"))
    assert len(files) == 1 and str(files[0]) == state.transcript_path
    text = files[0].read_text(encoding="utf-8")
    # Verbatim content of the compacted turns: text, tool calls with arguments, results.
    assert "request 0" in text and "answer 0" in text
    assert "tool call: run_shell" in text and '"command": "ls -la"' in text
    assert "listing 0: file_0.txt" in text
    # Private reasoning is not written out.
    assert "private thinking" not in text
    # The compacted block the model receives names the file.
    out = engine._outbound_messages()
    assert state.transcript_path in out[1]["content"]
    assert "read that file" in out[1]["content"]
    # The record round-trips with the path.
    assert C.CompactionState.from_dict(state.as_dict()).transcript_path == state.transcript_path


def test_no_spill_dir_means_no_transcript_and_no_mention(tmp_path):
    engine = TurnEngine(
        provider=CompactingProvider([AssistantTurn(text="done", finish_reason="stop")]),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        messages=_history(),
    )
    engine.compaction_settings = lambda: {"cap_tokens": 400, "threshold_pct": 0.8, "context_window": 100_000}
    _collect(engine)
    state = engine.compaction_state
    assert state is not None and state.transcript_path == ""
    assert "saved at" not in engine._outbound_messages()[1]["content"]


def test_trim_fallback_still_writes_the_transcript(tmp_path):
    """The exact shape of the 2026-11-18 incident: the summarizer is down, the trim
    fallback drops turns with no summary — the transcript file must remain as the only
    lifeline, and the compacted block must tell the model to read it instead of redoing
    the work from scratch."""
    spill = tmp_path / "spill"
    engine = TurnEngine(
        provider=CompactingProvider(
            [AssistantTurn(text="done", finish_reason="stop")], summary_fails=99
        ),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        messages=_history(),
        tool_result_spill_dir=spill,
    )
    engine.compaction_settings = lambda: {"cap_tokens": 400, "threshold_pct": 0.8, "context_window": 100_000}
    _collect(engine)  # is_attended is None → unattended auto-trim
    state = engine.compaction_state
    assert state is not None and state.trimmed
    assert state.transcript_path
    text = Path(state.transcript_path).read_text(encoding="utf-8")
    assert "request 0" in text and "listing 0: file_0.txt" in text
    block = engine._outbound_messages()[1]["content"]
    assert state.transcript_path in block
    assert "trimmed WITHOUT a summary" in block


def test_render_transcript_is_readable_and_skips_system():
    text = C.render_transcript(_history(turns=2), upto=7)
    assert text.startswith("# Compacted transcript")
    assert "be helpful" not in text  # system prompt is not part of the compacted span
    assert "## [1] user" in text and "## [2] assistant" in text and "## [3] tool" in text
