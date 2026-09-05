"""OPE-27 engine hook: the mid-run trigger, the outbound view, the usage signal, the
failure policy (attended prompt / unattended auto-trim), raw-overflow routing, and the
session persistence round-trip. Scripted providers, tiny forced windows, no network."""

import asyncio

from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    ToolCall,
)
from coworker.providers.base import TokenUsage
from coworker.tools import ToolRegistry

SUMMARY = "## Primary request and intent\nkeep building the report"


class CompactingProvider(ProviderClient):
    """Scripted main turns; summarizer calls (recognized by the compaction system prompt)
    are answered out-of-band so they never consume the main script."""

    def __init__(self, turns, *, summary=SUMMARY, summary_fails=0, main_overflows=0):
        self._turns = list(turns)
        self.summary = summary
        self.summary_fails = summary_fails
        self.main_overflows = main_overflows
        self.summary_calls = []
        self.main_calls = 0

    def complete(self, *, model, messages, tools=None, **settings):
        if messages and "compacting an AI coworker" in str(
            messages[0].get("content", "")
        ):
            self.summary_calls.append({"model": model, "messages": messages})
            if self.summary_fails > 0:
                self.summary_fails -= 1
                raise RuntimeError("summarizer down")
            return AssistantTurn(text=self.summary, finish_reason="stop")
        self.main_calls += 1
        if self.main_overflows > 0:
            self.main_overflows -= 1
            raise RuntimeError(
                "Error 400: maximum context length is 100000 tokens, request used more"
            )
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


class FailoverProvider(CompactingProvider):
    """Like CompactingProvider, but a summarizing call on a model in `fail_models`
    raises — used to exercise the dedicated-compression-model → main-model fallback."""

    def __init__(self, turns, *, fail_models=None, summary=SUMMARY, summary_fails=0):
        super().__init__(turns, summary=summary, summary_fails=summary_fails)
        self.fail_models = set(fail_models or [])

    def complete(self, *, model, messages, tools=None, **settings):
        if messages and "compacting an AI coworker" in str(messages[0].get("content", "")):
            # Record the attempt first (a real provider is still "called" before it
            # fails), so the test can see which models were tried.
            self.summary_calls.append({"model": model, "messages": messages})
            if model in self.fail_models:
                raise RuntimeError(f"dedicated model {model} down")
            return AssistantTurn(text=self.summary, finish_reason="stop")
        return super().complete(model=model, messages=messages, tools=tools, **settings)


def long_history(turns=8, bulk=1500):
    msgs = [{"role": "system", "content": "be helpful"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"request {i}", "ts": 1.0})
        msgs.append(
            {"role": "assistant", "content": f"answer {i} " + "x" * bulk, "ts": 1.0}
        )
    return msgs


def make_engine(tmp_path, provider, *, messages=None, cap=400):
    engine = TurnEngine(
        provider=provider,
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        messages=messages,
    )
    engine.compaction_settings = lambda: {
        "cap_tokens": cap,
        "threshold_pct": 0.8,
        "context_window": 100_000,
    }
    return engine


def collect(engine, text="continue"):
    async def _run():
        return [e async for e in engine.run(text)]

    return asyncio.run(_run())


def test_compacts_before_the_turn_when_estimate_crosses(tmp_path):
    provider = CompactingProvider([AssistantTurn(text="done", finish_reason="stop")])
    engine = make_engine(tmp_path,provider, messages=long_history(), cap=400)
    events = collect(engine)

    assert any(e.type == EventType.COMPACTED for e in events)
    assert not any(e.type == EventType.ERROR for e in events)
    state = engine.compaction_state
    assert state is not None and not state.trimmed
    assert provider.summary_calls[0]["model"] == "gpt-5.5"  # session's own model

    # Outbound view: system survives, the block stands in for the old turns, the
    # canonical transcript is untouched, and the persisted notice marks the spot.
    out = engine._outbound_messages()
    assert out[0]["role"] == "system"
    assert "<compacted-history>" in out[1]["content"]
    assert SUMMARY.splitlines()[-1] in out[1]["content"]
    assert "request 0" in out[1]["content"]  # mechanical user-message list
    assert any("answer 0" in str(m.get("content")) for m in engine.messages)
    assert any(
        m.get("role") == "notice" and m.get("kind") == "compacted"
        for m in engine.messages
    )


def test_usage_signal_triggers_between_tool_turns(tmp_path):
    # History too small for the estimate path — only the reported usage crosses the
    # trigger, after iteration 1's round-trip. The compaction runs before iteration 2.
    provider = CompactingProvider(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="nonexistent_tool", arguments={})],
                finish_reason="tool_calls",
                usage=TokenUsage(input=90_000, output=10),
            ),
            AssistantTurn(text="done", finish_reason="stop"),
        ]
    )
    engine = make_engine(tmp_path,provider, messages=long_history(turns=2, bulk=10), cap=400)
    events = collect(engine)
    assert any(e.type == EventType.COMPACTED for e in events)
    assert provider.summary_calls  # driven by usage, not the (tiny) estimate
    assert engine._last_context_tokens is None  # reset once the view shrank


def test_summarizer_failure_unattended_auto_trims(tmp_path):
    provider = CompactingProvider(
        [AssistantTurn(text="done", finish_reason="stop")], summary_fails=99
    )
    engine = make_engine(tmp_path,provider, messages=long_history(), cap=400)
    events = collect(engine)  # is_attended is None → unattended policy

    compacted = [e for e in events if e.type == EventType.COMPACTED]
    assert compacted and "trimmed" in compacted[0].data["text"].lower()
    assert engine.compaction_state is not None and engine.compaction_state.trimmed
    assert len(provider.summary_calls) == 2  # the one unconditional retry, then trim


def test_summarizer_failure_attended_nonblocking_notice_and_cooldown(tmp_path):
    # P0: a failing summarizer no longer pops a blocking Retry prompt every turn.
    # Instead it logs, engages a cooldown, and emits a non-blocking COMPACTED notice
    # while falling back to a token-based trim.
    provider = CompactingProvider(
        [AssistantTurn(text="done", finish_reason="stop")], summary_fails=99
    )
    engine = make_engine(tmp_path, provider, messages=long_history(), cap=400)
    engine.is_attended = lambda: True
    asked = []

    async def asker(args, tool_call_id=None):
        asked.append(args)
        return {"answer": "Retry"}

    engine.question_asker = asker
    events = collect(engine)

    # No blocking Retry prompt (P0-3): the question_asker is never invoked.
    assert asked == []
    # Failure is surfaced as a non-blocking COMPACTED notice, not a modal prompt.
    compacted = [e for e in events if e.type == EventType.COMPACTED]
    assert compacted and "summarizer was unavailable" in compacted[0].data["text"].lower()
    assert engine.compaction_state is not None and engine.compaction_state.trimmed
    # Cooldown engaged so we don't re-prompt (or re-hammer) on the next turn.
    assert engine._compaction_cooldown_until > 0


def test_summarizer_failure_attended_choose_trim(tmp_path):
    provider = CompactingProvider(
        [AssistantTurn(text="done", finish_reason="stop")], summary_fails=99
    )
    engine = make_engine(tmp_path,provider, messages=long_history(), cap=400)
    engine.is_attended = lambda: True

    async def asker(args, tool_call_id=None):
        return {"answer": "Trim oldest 10%"}

    engine.question_asker = asker
    collect(engine)
    assert engine.compaction_state is not None and engine.compaction_state.trimmed


def test_raw_overflow_routes_into_compaction_and_retries(tmp_path):
    # Trigger never fires (huge cap) — the provider 400 is the only signal. The engine
    # must compact (force) and retry the call instead of surfacing the error.
    provider = CompactingProvider(
        [AssistantTurn(text="recovered", finish_reason="stop")], main_overflows=1
    )
    engine = make_engine(tmp_path,provider, messages=long_history(), cap=1_000_000)
    events = collect(engine)

    assert any(e.type == EventType.COMPACTED for e in events)
    assert not any(e.type == EventType.ERROR for e in events)
    finals = [e for e in events if e.type == EventType.ASSISTANT_MESSAGE]
    assert finals and finals[-1].data["text"] == "recovered"
    assert provider.main_calls == 2


def test_non_overflow_provider_errors_still_surface(tmp_path):
    class FailingProvider(CompactingProvider):
        def complete(self, *, model, messages, tools=None, **settings):
            raise RuntimeError("rate limit exceeded")

    engine = make_engine(tmp_path,FailingProvider([]), messages=long_history(turns=1), cap=1_000_000)
    events = collect(engine)
    assert any(e.type == EventType.ERROR for e in events)
    assert not any(e.type == EventType.COMPACTED for e in events)


def test_set_compaction_settings_validates_and_round_trips(tmp_path):
    from coworker.server.manager import SessionManager

    class Provider(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return AssistantTurn(text="hi")

        def capabilities(self, model):
            return ModelCapabilities()

    mgr = SessionManager(workspace=tmp_path, provider=Provider())
    out = mgr.set_compaction_settings(
        threshold_pct=0.5, cap_tokens=100_000, model="gpt-4o-mini"
    )
    assert out["ok"] and out["threshold_pct"] == 0.5 and out["cap_tokens"] == 100_000
    assert mgr.compaction_settings()["model"] == "gpt-4o-mini"
    # validation: out-of-range % and non-numeric cap are rejected, tiny caps clamp up
    assert mgr.set_compaction_settings(threshold_pct=0.05)["ok"] is False
    assert mgr.set_compaction_settings(cap_tokens="lots")["ok"] is False
    assert mgr.set_compaction_settings(cap_tokens=1)["cap_tokens"] == 10_000
    # the flat /v1/settings names
    payload = mgr.compaction_settings_payload()
    assert payload["compaction_threshold_pct"] == 0.5
    assert payload["compaction_model"] == "gpt-4o-mini"


def test_compaction_state_survives_save_and_rebuild(tmp_path):
    from coworker.compaction import CompactionState
    from coworker.server.manager import SessionManager

    class Provider(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return AssistantTurn(text="hi", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    mgr = SessionManager(workspace=tmp_path, provider=Provider())
    sid = "compact-persist"
    engine = mgr.get_engine(sid, agent="cowork", workspace=str(tmp_path))
    assert callable(engine.compaction_settings)  # live Settings getter is wired
    assert engine.compaction_settings()["threshold_pct"] == 0.8

    engine.messages += long_history(turns=3)[1:]
    engine.compaction_state = CompactionState(
        boundary_index=3, summary_text="the gist", working_state="", user_messages=["u"]
    )
    mgr.save(sid, engine)
    mgr._engines.pop(sid)

    rebuilt = mgr.get_engine(sid, agent="cowork", workspace=str(tmp_path))
    assert rebuilt.compaction_state == engine.compaction_state


def test_compacting_signal_precedes_the_compacted_marker(tmp_path):
    # The transient-progress contract: COMPACTING fires before the (slow) summarizer
    # call, COMPACTED after — surfaces key the "Compacting context…" spinner on it.
    provider = CompactingProvider([AssistantTurn(text="done", finish_reason="stop")])
    engine = make_engine(tmp_path, provider, messages=long_history(), cap=400)
    events = collect(engine)

    types = [e.type for e in events]
    assert EventType.COMPACTING in types
    assert types.index(EventType.COMPACTING) < types.index(EventType.COMPACTED)
    # The signal is not persisted — only the compacted marker lands in the transcript.
    assert not any(
        m.get("role") == "notice" and m.get("kind") == "compacting"
        for m in engine.messages
    )


async def test_cooldown_suppresses_repeat_compaction_within_window(tmp_path):
    # P0-1: once a summarizer failure engages the cooldown, a subsequent auto-compaction
    # inside the window must NOT retry the summarizer or re-prompt — it bails out cleanly.
    import time

    provider = CompactingProvider(
        [AssistantTurn(text="done", finish_reason="stop")], summary_fails=99
    )
    engine = make_engine(tmp_path, provider, messages=long_history(), cap=400)
    engine.is_attended = lambda: True
    asked = []

    async def asker(args, tool_call_id=None):
        asked.append(args)
        return {"answer": "Retry"}

    engine.question_asker = asker

    n1 = await engine._compact_now()
    # First call fails → token-based trim fallback + cooldown, no blocking prompt.
    assert n1 is not None and "trimmed" in n1.lower()
    assert asked == []
    assert len(provider.summary_calls) == 2  # the two unconditional build attempts
    assert engine._compaction_cooldown_until > time.time()

    n2 = await engine._compact_now()  # still inside the cooldown window
    # Bailed out before touching the summarizer or the prompt again.
    assert n2 is None
    assert len(provider.summary_calls) == 2  # no extra summarizer calls
    assert asked == []  # no re-prompt


async def test_compression_model_failover_to_main(tmp_path):
    # P1-2: when a dedicated compression model is configured but fails, the engine
    # falls back to the main model rather than dropping straight to a trim.
    provider = FailoverProvider(
        [AssistantTurn(text="done", finish_reason="stop")],
        fail_models={"deepseek-v4-pro"},  # dedicated compression model fails
    )
    engine = make_engine(tmp_path, provider, messages=long_history(), cap=400)
    engine.compaction_settings = lambda: {
        "cap_tokens": 400,
        "threshold_pct": 0.8,
        "context_window": 100_000,
        "model": "deepseek-v4-pro",  # dedicated compression model configured
    }

    n = await engine._compact_now()
    # The summary succeeded via the main-model fallback — no trim.
    assert n is not None and "compacted" in n.lower()
    assert engine.compaction_state is not None and not engine.compaction_state.trimmed
    assert engine.compaction_state.model_used == "gpt-5.5"  # fell back to main
    # Both models were attempted: the dedicated one failed, the main one succeeded.
    tried = [c["model"] for c in provider.summary_calls]
    assert "deepseek-v4-pro" in tried and "gpt-5.5" in tried


async def test_compression_model_failover_both_fail_trims(tmp_path):
    # P1-2: if both the dedicated compression model AND the main model fail, the
    # engine still falls back to a (non-blocking) token trim rather than looping.
    provider = FailoverProvider(
        [AssistantTurn(text="done", finish_reason="stop")],
        fail_models={"deepseek-v4-pro", "gpt-5.5"},  # both fail
    )
    engine = make_engine(tmp_path, provider, messages=long_history(), cap=400)
    engine.compaction_settings = lambda: {
        "cap_tokens": 400,
        "threshold_pct": 0.8,
        "context_window": 100_000,
        "model": "deepseek-v4-pro",
    }
    engine.is_attended = lambda: True
    asked = []

    async def asker(args, tool_call_id=None):
        asked.append(args)
        return {"answer": "Retry"}

    engine.question_asker = asker

    n = await engine._compact_now()
    assert n is not None and "trimmed" in n.lower()
    assert engine.compaction_state is not None and engine.compaction_state.trimmed
    assert asked == []  # no blocking Retry prompt (P0-3)
    # Both models were tried before the trim fallback.
    tried = [c["model"] for c in provider.summary_calls]
    assert tried.count("deepseek-v4-pro") >= 1 and "gpt-5.5" in tried


def test_ineffective_compaction_triggers_antithrash(tmp_path):
    # P1-1: if the context fails to shrink after compaction (real token count does not
    # drop), two consecutive ineffective events pause auto-compaction (anti-thrash).
    import time

    engine = make_engine(tmp_path, CompactingProvider([AssistantTurn(text="done")]), cap=400)
    # Simulate: a compaction recorded a pre-compaction real token count of 100k.
    engine._compaction_pre_tokens = 100_000
    # Next main turn reports 100k again (no shrink) → ineffective #1.
    engine._check_compaction_effectiveness(100_000)
    assert engine._compaction_ineffective_streak == 1
    assert engine._compaction_throttled_until == 0.0
    # Second ineffective → streak=2 → anti-thrash throttle engaged.
    engine._compaction_pre_tokens = 100_000
    engine._check_compaction_effectiveness(100_000)
    assert engine._compaction_ineffective_streak == 2
    assert engine._compaction_throttled_until > time.time()
    # _compaction_due() must now return False (auto-compaction paused).
    assert engine._compaction_due() is False
    # A real shrink resets the streak.
    engine._compaction_throttled_until = 0.0
    engine._compaction_pre_tokens = 100_000
    engine._check_compaction_effectiveness(40_000)
    assert engine._compaction_ineffective_streak == 0


def test_effectiveness_check_skips_without_baseline(tmp_path):
    # P1-1: no trusted baseline (e.g. first compaction, or usage never reported) →
    # the cross-turn check is a no-op, never counts as ineffective.
    engine = make_engine(tmp_path, CompactingProvider([AssistantTurn(text="done")]), cap=400)
    engine._compaction_pre_tokens = None
    engine._check_compaction_effectiveness(50_000)
    assert engine._compaction_ineffective_streak == 0
    assert engine._compaction_throttled_until == 0.0
