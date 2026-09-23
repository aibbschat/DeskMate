"""P1-2 时序有效性 / 双时态：profile_items 双时态列 + CognitiveStore.profile_as_of 回放。

Hermetic：所有读写都走调用方传入的临时 db_path / base_dir，绝不触碰真实库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from deskmate.cognitive.models import CognitiveProfile, Stance
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory.learned_store import LearnedStore


class TestTemporalColumns:
    def test_profile_items_has_valid_from_until(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        with store._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(profile_items)").fetchall()}
        assert "valid_from" in cols
        assert "valid_until" in cols

    def test_set_profile_writes_valid_from(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        profile = CognitiveProfile(
            stances=[
                Stance(
                    id="s1",
                    topic="创作观",
                    content="用户偏好简洁回答",
                    first_extracted="2026-06-01T00:00:00",
                )
            ]
        )
        store.persist(profile, "cognitive")
        with store._connect() as conn:
            row = conn.execute(
                "SELECT first_extracted, valid_from, valid_until FROM profile_items WHERE id='s1'"
            ).fetchone()
        assert row is not None
        assert row[0] == "2026-06-01T00:00:00"  # first_extracted
        assert row[1] == "2026-06-01T00:00:00"  # valid_from 默认取 first_extracted
        assert row[2] is None  # valid_until 开放区间 = 至今有效

    def test_list_for_ui_surfaces_valid_window(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO profile_items"
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated, valid_from, valid_until) "
                "VALUES('p1','cognitive','stance','创作观','内容','立场','active',0.8,"
                "'{}','2026-03-01','2026-03-01','2026-03-01',NULL)"
            )
        items = store.list_for_ui()
        assert items[0]["valid_from"] == "2026-03-01"
        assert items[0]["valid_until"] is None


class TestProfileAsOf:
    """双时态核心能力：回到某时点，重建当时有效的认知画像。"""

    def _seed_history(self, store: CognitiveStore) -> None:
        with store._connect() as conn:
            # A：2026-06-01 形成，2026-08-15 偏好由「旧偏好」变为「新偏好」
            conn.execute(
                "INSERT INTO profile_items"
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated, valid_from, valid_until) "
                "VALUES('A','cognitive','stance','创作观','新偏好','立场','active',0.8,"
                "'{}','2026-06-01T00:00:00','2026-08-15T00:00:00','2026-06-01T00:00:00',NULL)"
            )
            conn.executemany(
                "INSERT INTO item_history"
                "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("A", 0, "created", "", "旧偏好", "初始提取", "chat", "2026-06-01T00:00:00"),
                    ("A", 1, "updated", "旧偏好", "新偏好", "访谈澄清", "interview", "2026-08-15T00:00:00"),
                ],
            )
            # B：2026-01-01 形成，无变更，内容恒定
            conn.execute(
                "INSERT INTO profile_items"
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated, valid_from, valid_until) "
                "VALUES('B','cognitive','stance','历史观','恒定偏好','立场','active',0.8,"
                "'{}','2026-01-01T00:00:00','2026-01-01T00:00:00','2026-01-01T00:00:00',NULL)"
            )
            conn.execute(
                "INSERT INTO item_history"
                "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                "VALUES('B',0,'created','','恒定偏好','初始提取','chat','2026-01-01T00:00:00')"
            )

    def test_as_of_before_birth_excludes(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_history(store)
        res = store.profile_as_of("2026-05-01T00:00:00")
        assert res["ok"] is True
        ids = {it["id"] for it in res["items"]}
        assert "A" not in ids  # 2026-06-01 才形成
        assert "B" in ids

    def test_as_of_mid_life_returns_old_value(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_history(store)
        res = store.profile_as_of("2026-07-01T00:00:00")
        a = next(it for it in res["items"] if it["id"] == "A")
        assert a["content"] == "旧偏好"  # 8-15 才变新

    def test_as_of_after_change_returns_new_value(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_history(store)
        res = store.profile_as_of("2026-09-01T00:00:00")
        a = next(it for it in res["items"] if it["id"] == "A")
        assert a["content"] == "新偏好"

    def test_as_of_no_history_returns_current(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_history(store)
        res = store.profile_as_of("2026-07-01T00:00:00")
        b = next(it for it in res["items"] if it["id"] == "B")
        assert b["content"] == "恒定偏好"

    def test_as_of_bad_time(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        res = store.profile_as_of("不是时间")
        assert res["ok"] is False
        assert res["items"] == []


class TestLearnedValidFrom:
    def test_append_sets_valid_from(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        store.append(["用户喜欢用 LaTeX 排版"])
        with store._connect() as conn:
            row = conn.execute(
                "SELECT ts, valid_from, status FROM learned_history WHERE fact=?"
                "ORDER BY id DESC LIMIT 1",
                ("用户喜欢用 LaTeX 排版",),
            ).fetchone()
        assert row is not None
        assert row[0] == row[1]  # 学到即生效：valid_from 默认=记录时间 ts
        assert row[2] == "active"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
