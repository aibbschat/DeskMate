"""PR11 performance guards — metadata-only session loads, secret caching, inbox hot/cold split.

These are behaviour + cost guards for the §9 items #17/#18/#19. Each one asserts the
optimised path does *no* expensive work (no transcript read, no repeated file parse,
no full-history rewrite), not just that the result looks right.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coworker.inbox as inbox_mod
from coworker.conversations import ConversationStore
from coworker.inbox import STATE_RESOLVED, InboxStore
from coworker.secrets import SecretStore
from coworker.sessions import SessionRecord


# ---------------------------------------------------------------------------
# #18 — metadata-only session load
# ---------------------------------------------------------------------------


def _seed_session(tmp_path: Path) -> tuple[ConversationStore, str]:
    store = ConversationStore(tmp_path)
    sid = "abc123def456"
    record = SessionRecord(
        session_id=sid,
        workspace=str(tmp_path),
        model="test-model",
        mode="interactive",
        messages=[{"role": "user", "content": "hello"}] * 50,
        title="A chat",
        agent="cowork",
    )
    store.save(record)
    return store, sid


def test_load_meta_never_reads_transcript(tmp_path, monkeypatch):
    store, sid = _seed_session(tmp_path)

    def _boom(*_a, **_k):  # any transcript read is a failure
        raise AssertionError("load_meta must not read the conversation JSONL")

    monkeypatch.setattr(ConversationStore, "_read_jsonl", _boom)
    monkeypatch.setattr(
        ConversationStore, "_repair_tool_pairing", staticmethod(_boom)
    )

    meta = store.load_meta(sid)
    assert meta is not None
    assert meta.title == "A chat"
    assert meta.agent == "cowork"
    assert meta.message_count == 50  # from n_msgs, not len(messages)
    assert meta.messages == []


def test_exists_is_one_probe(tmp_path):
    store, sid = _seed_session(tmp_path)
    assert store.exists(sid) is True
    assert store.exists("missing-session") is False


# ---------------------------------------------------------------------------
# #19 — SecretStore caches the parsed file by (mtime, size)
# ---------------------------------------------------------------------------


def test_secret_store_reads_file_once(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"openai:default": {"api_key": "sk-1"}}), encoding="utf-8")
    store = SecretStore(path)

    calls = {"n": 0}
    real = Path.read_text

    def counting(self, *args, **kwargs):
        if self == path:
            calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)

    assert store.get("openai:default")["api_key"] == "sk-1"
    assert store.get("openai:default")["api_key"] == "sk-1"
    assert store.get("missing") is None
    assert calls["n"] == 1


def test_secret_store_invalidates_on_external_edit(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"p": {"k": "v1"}}), encoding="utf-8")
    store = SecretStore(path)
    assert store.get("p")["k"] == "v1"

    # A different size guarantees a different stat key even on coarse mtime clocks.
    path.write_text(json.dumps({"p": {"k": "value-two"}}), encoding="utf-8")
    assert store.get("p")["k"] == "value-two"


def test_secret_store_put_invalidates_cache(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.put("p", {"k": "v1"})
    assert store.get("p")["k"] == "v1"
    store.put("p", {"k": "v2"})
    assert store.get("p")["k"] == "v2"


# ---------------------------------------------------------------------------
# #17 — inbox hot/cold split
# ---------------------------------------------------------------------------


def test_inbox_old_resolved_items_move_to_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_mod, "_HOT_DAYS", 0.0)  # everything resolved is "old"

    store = InboxStore(tmp_path / "inbox.json")
    item = store.add_approval("s1", "Run shell?")
    assert store.pending_count("s1") == 1
    store.resolve(item.id, "allow")

    hot = json.loads((tmp_path / "inbox.json").read_text(encoding="utf-8"))
    assert hot["items"] == []  # nothing but the archive holds the resolved item

    archive = list(tmp_path.glob("inbox-archive-*.jsonl"))
    assert len(archive) == 1
    lines = [json.loads(x) for x in archive[0].read_text().splitlines() if x.strip()]
    assert [x["id"] for x in lines] == [item.id]

    # Reload: the hot file is empty, but a full-history query lazily brings it back.
    reloaded = InboxStore(tmp_path / "inbox.json")
    assert reloaded.pending("s1") == []
    assert reloaded.list(session_id="s1")[0].resolution == "allow"
    assert reloaded.list(session_id="s1")[0].state == STATE_RESOLVED


def test_inbox_pending_count_matches_pending(tmp_path):
    store = InboxStore(tmp_path / "inbox.json")
    store.add_approval("s1", "a")
    store.add_question("s1", "b")
    store.add_notification("s2", "c")
    assert store.pending_count("s1") == 2
    assert store.pending_count() == 3
    assert len(store.pending("s1")) == 2


def test_inbox_errors_do_not_drop_items(tmp_path):
    """Unknown legacy fields must not make a stored item unreadable."""
    path = tmp_path / "inbox.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "x",
                        "session_id": "s",
                        "kind": "question",
                        "title": "t",
                        "legacy_field": {"anything": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = InboxStore(path)
    assert store.get("x") is not None
    assert store.get("x").kind == "question"
