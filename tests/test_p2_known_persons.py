"""P2-R1 · person 锚源：DB 注册表 known_persons + 模式 B 自生长。

覆盖：seed 直接 active、词边界不误吞长名、env override、模式 B 门槛转正
（前 2 次 candidate 不产锚 / 第 3 次转正 / 第 4 次起产锚）、fail-open、
SYNC 注册与 migrate 补列 + 触发器落 oplog。
"""

from __future__ import annotations

import json

import pytest

from deskmate.cognitive.extractor import CognitiveExtractor, extract_anchors
from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory import sync_store_sqlite as sss


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_PERSON_NAMES", raising=False)
    monkeypatch.delenv("DESKMATE_ANCHOR_RECALL", raising=False)
    return tmp_path


def _db(env):
    return env / "deskmate.db"


def _store(env) -> CognitiveStore:
    store = CognitiveStore(db_path=_db(env))
    with store._connect() as conn:
        sss.migrate(conn, "mac", "profile_items")
        sss.migrate(conn, "mac", "known_persons")
        sss.migrate(conn, "mac", "memory_anchors")
    return store


def _person_anchor_values(store) -> list[str]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT anchor_value FROM memory_anchors WHERE anchor_kind='person'"
        ).fetchall()
    return [r[0] for r in rows]


def _patch_llm(monkeypatch, payloads: list[dict]) -> None:
    import deskmate.memory.llm as llmmod

    it = iter(payloads)
    monkeypatch.setattr(llmmod, "llm_available", lambda: True)
    monkeypatch.setattr(llmmod, "chat", lambda **kw: json.dumps(next(it), ensure_ascii=False))


# ---------------------------------------------------------------------------
# store：seed / list_active
# ---------------------------------------------------------------------------
def test_seed_makes_active_and_idempotent(env):
    store = _store(env)
    assert store.seed_known_persons(["朱元璋", "张居正"], "seed") == 2
    assert {"朱元璋", "张居正"} <= set(store.list_active_persons())

    with store._connect() as conn:
        rows = conn.execute("SELECT name, source, status FROM known_persons").fetchall()
    d = {r[0]: (r[1], r[2]) for r in rows}
    assert d["朱元璋"] == ("seed", "active")

    # 幂等：重复 seed 不新增
    assert store.seed_known_persons(["朱元璋"], "seed") == 0


# ---------------------------------------------------------------------------
# 中文子串匹配 + 排除被更长名包含（R6b 修复词边界 BLOCKER）
# ---------------------------------------------------------------------------
def test_no_partial_match_when_longer_name_known(env):
    """active 含「张居正」「张居业」时，文本『张居业的笔记』只产张居业、不产张居。"""
    store = _store(env)
    store.seed_known_persons(["张居正", "张居业"], "seed")
    anchors = extract_anchors("张居业的笔记", "某文", "article", [{"topic": "x"}], store=store)
    names = [v for k, v in anchors if k == "person"]
    assert "张居业" in names
    assert "张居" not in names, "「张居」是更长已知名（张居正/张居业）的片段，不该产"


def test_full_name_followed_by_cjk_matches(env):
    """中文无词边界：全名后跟汉字应命中（词边界口径的 BLOCKER 已修）。"""
    store = _store(env)
    store.seed_known_persons(["张居正"], "seed")
    anchors = extract_anchors("张居正，明朝首辅。", "某文", "article", [], store=store)
    assert ("person", "张居正") in anchors
    # 后面紧跟汉字，也应命中（不再被 \w 边界拒绝）
    anchors2 = extract_anchors("张居正推行改革", "某文", "article", [], store=store)
    assert ("person", "张居正") in anchors2


def test_substring_chinese_full_name(env):
    """独立实测（ROUND-06b §四）：『朱元璋建立了明朝』命中所 seed 的朱元璋。"""
    store = _store(env)
    store.seed_known_persons(["朱元璋"], "seed")
    anchors = extract_anchors("朱元璋建立了明朝", "文", "article", [], store=store)
    assert ("person", "朱元璋") in anchors


def test_env_override_beats_db(env, monkeypatch):
    store = _store(env)
    store.seed_known_persons(["朱元璋"], "seed")   # DB 里有朱元璋
    monkeypatch.setenv("DESKMATE_PERSON_NAMES", "萧盛")   # env 强制 override
    anchors = extract_anchors("萧盛，作家。朱元璋，明太祖。", "某文", "article", [], store=store)
    names = [v for k, v in anchors if k == "person"]
    assert "萧盛" in names
    assert "朱元璋" not in names, "env 配了就绕过表，只认 env 名单"


# ---------------------------------------------------------------------------
# 模式 B 自生长：阈值门槛
# ---------------------------------------------------------------------------
def test_mode_b_promotion_direct(env):
    store = _store(env)
    assert store.list_active_persons() == []
    store.add_known_person("朱元璋")
    assert store.list_active_persons() == []      # 第 1 次 candidate
    store.add_known_person("朱元璋")
    assert store.list_active_persons() == []      # 第 2 次仍 candidate
    store.add_known_person("朱元璋")
    assert "朱元璋" in store.list_active_persons()  # 第 3 次转正

    with store._connect() as conn:
        row = conn.execute(
            "SELECT source, status, seen_count FROM known_persons WHERE name='朱元璋'"
        ).fetchone()
    assert row == ("auto", "active", 3)


def test_mode_b_e2e_extract_promotes_on_third(env, monkeypatch):
    """真实抽取链路：前 2 次不产锚，第 3 次轮末转正，第 4 次起产 person 锚。"""
    store = _store(env)
    payload = {
        "stances": [{"topic": "历史真实性", "content": "史料必须交叉比对",
                     "evidence": "e", "confidence": 0.9}],
        "persons": ["朱元璋"],
    }
    _patch_llm(monkeypatch, [payload] * 4)
    pm = CognitiveProfileManager(profile_path=str(_db(env)))
    ext = CognitiveExtractor(pm)

    for i in (1, 2):
        r = ext.extract_from_text("朱元璋，明太祖。", f"文{i}", "article")
        assert r.get("stances") == 1
        assert _person_anchor_values(store) == [], f"第 {i} 次（candidate）不该产 person 锚"

    ext.extract_from_text("朱元璋，明太祖。", "文3", "article")
    assert _person_anchor_values(store) == [], "第 3 次本轮仍不产锚（转正在轮末）"
    assert "朱元璋" in store.list_active_persons()

    ext.extract_from_text("朱元璋，明太祖。", "文4", "article")
    assert "朱元璋" in _person_anchor_values(store), "第 4 次起应产 person 锚"


def test_grow_fail_open_when_store_broken(env, monkeypatch):
    """写库异常不影响主抽取（fail-open）。"""
    store = _store(env)
    _patch_llm(monkeypatch, [{
        "stances": [{"topic": "t", "content": "c", "evidence": "e", "confidence": 0.5}],
        "persons": ["朱元璋"],
    }])

    def _boom(self, name, source="auto"):
        raise RuntimeError("db down")

    monkeypatch.setattr(CognitiveStore, "add_known_person", _boom)
    pm = CognitiveProfileManager(profile_path=str(_db(env)))
    r = CognitiveExtractor(pm).extract_from_text("朱元璋，明太祖。", "文", "article")
    assert r.get("stances") == 1 and "error" not in r


def test_fail_open_no_env_no_db():
    """无 env、无 store → 无 person 锚、绝不抛。"""
    anchors = extract_anchors("朱元璋，明太祖。", "某文", "article", [{"topic": "t"}])
    assert not any(k == "person" for k, _ in anchors)


def test_fail_open_registry_query_error(env, monkeypatch):
    store = _store(env)

    def _boom(self):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(CognitiveStore, "list_active_persons", _boom)
    anchors = extract_anchors("朱元璋，明太祖。", "某文", "article", [], store=store)
    assert not any(k == "person" for k, _ in anchors)


# ---------------------------------------------------------------------------
# SYNC：注册 + migrate 补列 + 触发器落 oplog
# ---------------------------------------------------------------------------
def test_known_persons_in_sync_tables():
    spec = sss.SYNC_TABLES["known_persons"]
    assert spec["pk"] == "name" and spec["sync_key"] == "name"
    assert spec["sync_uuid_kind"] == "string"
    sss._validate_sync_tables_no_vectors()   # 无向量列（启动守卫）


def test_known_persons_migrate_cols_and_oplog(env):
    store = CognitiveStore(db_path=_db(env))
    with store._connect() as conn:
        sss.migrate(conn, "mac", "known_persons")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(known_persons)").fetchall()}
        assert {"sync_uuid", "sync_hlc", "sync_tombstone"} <= cols

    # 迁移后（触发器已建）插入 → 自动补 sync_uuid + 落 oplog
    store.seed_known_persons(["王阳明"], "seed")
    with store._connect() as conn:
        uid = conn.execute(
            "SELECT sync_uuid FROM known_persons WHERE name='王阳明'"
        ).fetchone()[0]
        n_ops = conn.execute(
            "SELECT COUNT(*) FROM _sync_oplog WHERE table_name='known_persons'"
        ).fetchone()[0]
    assert uid and uid.startswith("deskmate:known_persons:")
    assert n_ops >= 1