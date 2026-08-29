"""Tests for P1-8 learning_graph 动态化（deskmate/memory/learning_graph.py）。

核心：图边权重接入 GrowthEngine 关系数值（信任/亲密度/默契）→「越用越连」；
不传 growth 时全向后兼容（weight 为基础值、无 growth 块）。
"""

from __future__ import annotations

import pytest

from deskmate.memory.learning_graph import (
    _growth_snapshot,
    _memory_skill_edges,
    _skill_skill_edges,
    build_learning_graph,
)


def _make_env(tmp_path, trust=0.5, intimacy=50.0, empathy=50.0, prof=None):
    """造隔离环境：两个共享 tags 的技能 + 含技能 id 的记忆卡 + 成长状态。"""
    (tmp_path / "skills" / "general").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "general" / "写作助手.md").write_text(
        "---\nname: 写作助手\ndescription: 帮你润色润稿子\ntags: [写作, ai]\nuses: 3\n---\n正文",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "general" / "史料考据.md").write_text(
        "---\nname: 史料考据\ndescription: 查证历史事实\ntags: [写作, 历史]\nuses: 1\n---\n正文",
        encoding="utf-8",
    )
    (tmp_path / "MEMORY.md").write_text(
        "## 偏好\n用户喜欢用写作助手处理稿子，写作时要用史料考据。",
        encoding="utf-8",
    )
    # 成长状态（隔离）
    (tmp_path / "growth.json").write_text(
        "{}", encoding="utf-8"
    )  # 占位；用 GrowthEngine 直接写更稳
    from deskmate.growth.engine import GrowthEngine

    eng = GrowthEngine(tmp_path)
    eng.state.trust = trust
    eng.state.intimacy = intimacy
    eng.state.empathy = empathy
    if prof:
        eng.record_skill_use("写作助手", success=True)
    eng.save()
    return tmp_path


class TestEdgesWithWeight:
    def test_skill_skill_edge_weight_is_shared_tag_count(self, tmp_path):
        _make_env(tmp_path)
        nodes = [
            {"id": "a", "tags": ["x", "y"]},
            {"id": "b", "tags": ["x", "y", "z"]},
            {"id": "c", "tags": ["q"]},
        ]
        edges = _skill_skill_edges(nodes)
        assert ("a", "b", 2) in edges
        assert all(len(e) == 3 for e in edges)

    def test_memory_skill_edges_max_4_per_card(self, tmp_path):
        _make_env(tmp_path)
        cards = [
            {
                "source": "memory",
                "title": "很多技能都相关的记忆",
                "body": "写作助手 史料考据 都常用",
            }
            for _ in range(2)
        ]
        # 造一堆技能，验证每卡最多 4 条边
        skills = [
            {"id": f"技能{i}", "description": "描述" + "词" * 6}
            for i in range(10)
        ]
        edges = _memory_skill_edges(cards, skills)
        by_card: dict[str, int] = {}
        for mem, _sid, _w in edges:
            by_card[mem] = by_card.get(mem, 0) + 1
        assert all(cnt <= 4 for cnt in by_card.values())

    def test_memory_skill_edges_weight_is_hit_score(self, tmp_path):
        _make_env(tmp_path)
        cards = [{"source": "memory", "title": "t", "body": "用户常用写作助手"}]
        skills = [{"id": "写作助手", "description": "d"}]
        edges = _memory_skill_edges(cards, skills)
        assert len(edges) == 1
        # 强边 +6
        assert edges[0][2] >= 6.0


class TestDynamicGrowth:
    def test_no_growth_backward_compatible(self, tmp_path):
        _make_env(tmp_path)
        g = build_learning_graph(tmp_path)
        assert "growth" not in g
        assert all("weight" in e for e in g["edges"])

    def test_growth_block_present(self, tmp_path):
        _make_env(tmp_path, trust=0.8, intimacy=80.0, empathy=70.0)
        g = build_learning_graph(tmp_path, growth=_growth_snapshot(tmp_path))
        assert g.get("growth", {}).get("trust") == pytest.approx(0.8, abs=1e-3)
        assert g["growth"]["bond_stage"]
        assert 0.6 <= g["growth"]["relation_factor"] <= 2.0

    def test_memory_skill_edge_scaled_by_relation_factor(self, tmp_path):
        _make_env(tmp_path)
        snap = _growth_snapshot(tmp_path)
        base = build_learning_graph(tmp_path)
        snap_hi = dict(snap, relation_factor=2.0)
        boosted = build_learning_graph(tmp_path, growth=snap_hi)
        w_base = {e["source"]: e["weight"] for e in base["edges"]}
        w_boost = {e["source"]: e["weight"] for e in boosted["edges"]}
        mem_edges = [k for k in w_base if k.startswith("memory:")]
        assert mem_edges, "应存在记忆↔技能边"
        for k in mem_edges:
            assert w_boost[k] == pytest.approx(w_base[k] * 2.0)

    def test_skill_node_has_proficiency(self, tmp_path):
        _make_env(tmp_path, prof=True)
        snap = _growth_snapshot(tmp_path)
        g = build_learning_graph(tmp_path, growth=snap)
        by_id = {n["id"]: n for n in g["nodes"]}
        assert by_id["写作助手"]["proficiency"] > 0.0
        assert by_id["史料考据"]["proficiency"] == 0.0

    def test_no_growth_node_proficiency_zero(self, tmp_path):
        _make_env(tmp_path)
        g = build_learning_graph(tmp_path)
        by_id = {n["id"]: n for n in g["nodes"]}
        assert by_id["写作助手"]["proficiency"] == 0.0


class TestGrowthSnapshot:
    def test_relation_factor_monotonic_with_trust(self, tmp_path):
        _make_env(tmp_path)
        snap_lo = _growth_snapshot(tmp_path)
        # 信任调高 → factor 更高
        from deskmate.growth.engine import GrowthEngine

        eng = GrowthEngine(tmp_path)
        eng.state.trust = 1.0
        eng.save()
        snap_hi = _growth_snapshot(tmp_path)
        assert snap_hi["relation_factor"] > snap_lo["relation_factor"]

    def test_relation_factor_clamped_range(self, tmp_path):
        _make_env(tmp_path, trust=1.0, intimacy=100.0, empathy=100.0)
        snap = _growth_snapshot(tmp_path)
        assert snap["relation_factor"] <= 2.0

    def test_snapshot_fail_open_on_missing_growth(self, tmp_path):
        # 无 growth 状态文件 → 不抛异常（返回带默认值的 dict 或 None）
        snap = _growth_snapshot(tmp_path)
        assert snap is None or isinstance(snap, dict)
