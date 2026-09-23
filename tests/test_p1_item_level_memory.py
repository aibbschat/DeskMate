"""P1-1 条目级可控记忆：LearnedStore.forget / list_for_ui + CognitiveStore.delete_item / list_for_ui。

Hermetic：所有读写都走调用方传入的临时 base_dir / db_path，绝不触碰真实库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from deskmate.cognitive.store import CognitiveStore
from deskmate.memory.learned_store import LearnedStore


class TestLearnedStoreItemControl:
    def test_list_for_ui_empty(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        assert store.list_for_ui() == []

    def test_append_then_list_for_ui_active(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        n = store.append(["用户喜欢用 LaTeX 排版", "用户偏好简洁回答"])
        assert n == 2
        items = store.list_for_ui()
        assert len(items) == 2
        assert all(it["status"] == "active" for it in items)
        facts = {it["fact"] for it in items}
        assert "用户喜欢用 LaTeX 排版" in facts

    def test_forget_marks_forgotten(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        store.append(["用户喜欢喝咖啡", "用户偏好安静环境"])
        target = store.list_for_ui()[0]
        assert store.forget(target["id"]) is True
        after = store.list_for_ui()
        # 软删除：该条仍在列表，但状态变 forgotten，且其余仍为 active
        changed = next(it for it in after if it["id"] == target["id"])
        assert changed["status"] == "forgotten"
        assert sum(1 for it in after if it["status"] == "active") == 1

    def test_forget_is_idempotent(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        store.append(["一条事实"])
        tid = store.list_for_ui()[0]["id"]
        assert store.forget(tid) is True
        # 已经是 forgotten，再 forget 不应再命中（WHERE status='active'）
        assert store.forget(tid) is False

    def test_forget_unknown_returns_false(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        assert store.forget(99999) is False

    def test_list_for_ui_limit(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        store.append([f"事实{i}" for i in range(10)])
        assert len(store.list_for_ui(limit=3)) == 3


class TestCognitiveStoreItemControl:
    def _seed(self, store: CognitiveStore, rows):
        with store._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO profile_items "
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated) "
                "VALUES (?, 'cognitive', 'stance', ?, ?, 'preference', 'active', 0.8, "
                "'{}', '2026-01-01', '2026-01-01')",
                rows,
            )

    def test_list_for_ui_empty(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        assert store.list_for_ui() == []

    def test_list_for_ui_active_first(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed(
            store,
            [
                ("a1", "topic_a", "内容A"),
                ("a2", "topic_b", "内容B"),
            ],
        )
        # 把 a1 标记为 forgotten，验证 active 排序在前
        with store._connect() as conn:
            conn.execute(
                "UPDATE profile_items SET status='forgotten' WHERE id='a1'"
            )
        items = store.list_for_ui()
        assert len(items) == 2
        assert items[0]["id"] == "a2"  # active 在前
        assert items[0]["status"] == "active"
        assert items[1]["id"] == "a1"
        assert items[1]["status"] == "forgotten"

    def test_delete_item_removes_and_traces(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed(
            store,
            [
                ("a1", "topic_a", "内容A"),
                ("a2", "topic_b", "内容B"),
            ],
        )
        assert store.delete_item("a1") is True
        remaining = {it["id"] for it in store.list_for_ui()}
        assert remaining == {"a2"}
        # 删除前在 item_history 留痕 action='forgotten'
        with store._connect() as conn:
            row = conn.execute(
                "SELECT action, source FROM item_history WHERE item_id='a1' "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row[0] == "forgotten"
        assert row[1] == "user"

    def test_delete_item_unknown_returns_false(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        assert store.delete_item("nope") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
