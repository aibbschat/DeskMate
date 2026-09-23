"""2026-09-23 巡检修复的回归锁。

所锁的四类缺陷（均由外部巡检实测发现 / 我方核实）：
1. **P1-1 遗忘被复活** —— 认知层 persist() 是整层全量覆盖，缓存 manager 再 save()
   一次就把刚遗忘的条目写回来；用墓碑表在持久化边界根治。
2. **P1-2 as-of 回放三 bug** —— calibrated 不在内容白名单（用户纠正被回退）、
   restored 的 new_value 是状态字面量（正文被污染成 "active"）、
   3 处 updated 漏写 new_value（回放成空串）。
3. **遗忘级联** —— learned_store.recent() 不过滤 status，已遗忘事实仍回填日记素材。
4. **P1-3** —— register() 重置用户暂停；get_registry 单例不分 base_dir。

Hermetic：全部走 tmp_path 传入的显式 db_path / base_dir，不碰真实库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.auto_tasks.models import TaskRegistryEntry
from deskmate.auto_tasks.registry import TaskRegistry, get_registry
from deskmate.cognitive.models import CognitiveProfile, Stance
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory.learned_store import LearnedStore

_NOW = "2026-09-23T00:00:00"


def _profile_of(items) -> CognitiveProfile:
    """items: [(id, topic, content)] → 含这些立场的 CognitiveProfile。"""
    return CognitiveProfile(
        stances=[
            Stance(id=i, topic=t, content=c, category="preference", status="active",
                   confidence=0.8, first_extracted=_NOW, last_updated=_NOW)
            for i, t, c in items
        ]
    )


class TestForgottenNotResurrected:
    """P1-1：遗忘必须挺得住任何一次全量覆盖写。"""

    @staticmethod
    def _seed(store: CognitiveStore, rows):
        with store._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO profile_items "
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated) "
                "VALUES (?, 'cognitive', 'stance', ?, ?, 'preference', 'active', 0.8, "
                "'{}', '2026-01-01', '2026-01-01')",
                rows,
            )

    def test_stale_cache_save_cannot_bring_item_back(self, tmp_path: Path):
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed(store, [("a1", "topic_a", "内容A"), ("a2", "topic_b", "内容B")])

        assert store.delete_item("a1") is True
        assert "a1" not in {it["id"] for it in store.list_for_ui()}

        # 模拟真实众生相：全局单例 manager 手里那份 profile 还留着 a1，随后又 save 了一次
        store.persist(_profile_of([("a1", "topic_a", "内容A"), ("a2", "topic_b", "内容B")]))

        remaining = {it["id"] for it in store.list_for_ui()}
        assert "a1" not in remaining, "被遗忘的条目被全量覆盖写复活了"
        assert "a2" in remaining, "正常条目不应受影响"

    def test_relearning_same_fact_after_forgetting_still_works(self, tmp_path: Path):
        """墓碑按 **id** 抑制，而 id 是 uuid → 同内容被重新提出来应能正常入库。"""
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed(store, [("a1", "topic_a", "内容A")])
        store.delete_item("a1")

        store.persist(_profile_of([("brand-new-id", "topic_a", "内容A")]))
        ids = {it["id"] for it in store.list_for_ui()}
        assert "brand-new-id" in ids


class TestAsOfReplay:
    """P1-2：回放必须还原「当时真正成立的说法」。"""

    @staticmethod
    def _seed_hist(store: CognitiveStore, item_id: str, rows, first="2026-01-01"):
        with store._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profile_items "
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated) "
                "VALUES (?, 'cognitive', 'stance', 't', ?, 'preference', 'active', 0.8, "
                "'{}', ?, ?)",
                (item_id, "占位当前内容", first, first),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO item_history "
                "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                "VALUES (?, ?, ?, '', ?, '', 'test', ?)",
                [(item_id, seq, act, val, ts) for seq, (act, val, ts) in enumerate(rows)],
            )

    def _one(self, store: CognitiveStore, at: str) -> str:
        return store.profile_as_of(at)["items"][0]["content"]

    def test_calibrated_is_treated_as_content_change(self, tmp_path: Path):
        """用户校准更新的就是正文，缺它会导致回放退回纠正前的说法。"""
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_hist(store, "x1", [
            ("created", "原始理解", "2026-01-01"),
            ("calibrated", "用户纠正后的理解", "2026-03-01"),
        ])
        assert self._one(store, "2026-02-01") == "原始理解"
        assert self._one(store, "2026-06-01") == "用户纠正后的理解"

    def test_status_literal_does_not_pollute_content(self, tmp_path: Path):
        """repair 流程写的是 action=restored, new_value="active"——那是状态，不是正文。"""
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_hist(store, "x2", [
            ("created", "真实正文", "2026-01-01"),
            ("restored", "active", "2026-02-01"),
        ])
        content = self._one(store, "2026-06-01")
        assert content == "真实正文"
        assert content != "active"

    def test_updated_without_new_value_does_not_blank_content(self, tmp_path: Path):
        """profile.py 曾有 3 处 updated 漏写 new_value → 回放成空串。"""
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        self._seed_hist(store, "x3", [
            ("created", "正文v1", "2026-01-01"),
            ("updated", "", "2026-02-01"),
        ])
        content = self._one(store, "2026-06-01")
        assert content.strip() == "正文v1", f"回放被空 new_value 抹掉了: {content!r}"

    def test_terminal_item_has_closed_validity_window(self, tmp_path: Path):
        """走到终态（superseded）的条目，有效期应闭合；活跃条目仍是开放区间。"""
        store = CognitiveStore(db_path=tmp_path / "deskmate.db")
        with store._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profile_items "
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated) "
                "VALUES ('s1', 'cognitive', 'stance', 't', '旧说法', 'preference', "
                "'superseded', 0.8, '{}', '2026-01-01', '2026-05-01')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO profile_items "
                "(id, layer, dimension, topic, content, category, status, confidence, "
                "extra_json, first_extracted, last_updated) "
                "VALUES ('s2', 'cognitive', 'stance', 't', '现行说法', 'preference', "
                "'active', 0.8, '{}', '2026-01-01', '2026-09-01')"
            )
        listed = {it["id"]: it for it in store.list_for_ui()}
        assert listed["s1"]["valid_until"] == "2026-05-01"
        assert listed["s2"]["valid_until"] is None


class TestForgettingCascade:
    def test_recent_excludes_forgotten(self, tmp_path: Path):
        store = LearnedStore(base_dir=tmp_path)
        store.append(["用户喜欢喝咖啡", "用户偏好安静环境"])
        target = store.list_for_ui()[0]
        assert store.forget(target["id"]) is True

        facts = [r["fact"] for r in store.recent()]
        assert target["fact"] not in facts, "已遗忘的事实仍被 recent() 带回"
        assert len(facts) == 1


class TestAutoTaskRegistry:
    def test_register_keeps_user_pause(self, tmp_path: Path):
        reg = TaskRegistry(tmp_path / "auto_tasks")
        reg.register(TaskRegistryEntry(
            task_type="t1", task_name="任务一", category="writing", enabled=True,
        ))
        assert reg.set_enabled("t1", False) is True

        # 代码每次启动都会重新 register 一遍任务定义 —— 不能把用户的暂停冲掉
        reg.register(TaskRegistryEntry(
            task_type="t1", task_name="任务一（改了描述）", category="writing", enabled=True,
        ))
        assert reg.get("t1").enabled is False

    def test_get_registry_keyed_by_base_dir(self, tmp_path: Path):
        from deskmate.auto_tasks import registry as reg_mod

        saved = dict(getattr(reg_mod, "_registries", {}))
        try:
            a = get_registry(tmp_path / "A")
            b = get_registry(tmp_path / "B")
            assert a is not b, "不同 base_dir 拿到同一实例 → 会串数据"
            assert get_registry(tmp_path / "A") is a
        finally:
            reg_mod._registries = saved


class TestDowngradePathWritesSeparateEntries:
    """P0-1 降级路径：多条素材必须各成一则，不能挤成一条。"""

    def test_one_call_per_fact_yields_one_entry_each(self, tmp_path: Path):
        from deskmate.growth.diary import DiaryWriter

        writer = DiaryWriter(base_dir=tmp_path)
        today = "2026-09-23"
        before = writer.entry_count(today)

        # 修复后：每条素材各写一次（旧实现是把整批 fresh 一次性丢进去 → 只成一则）
        for fact in ["新闻甲", "新闻乙", "新闻丙"]:
            writer.append_observation(today, [fact])

        assert writer.entry_count(today) - before == 3

    def test_batch_call_still_collapses_into_single_entry(self, tmp_path: Path):
        """对照：一次性传多素材仍然合并成一则（所以调用方必须逐条写）。"""
        from deskmate.growth.diary import DiaryWriter

        writer = DiaryWriter(base_dir=tmp_path)
        today = "2026-09-23"
        res = writer.append_observation(today, ["甲", "乙", "丙"])
        assert res["written"] == 3  # 三条 bullet
        assert writer.entry_count(today) == 1  # 但只有一则


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
