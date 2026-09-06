"""L2 治理层后端测试。

全部在 tmp 基目录下进行，mock 掉网络搜索与 learned_history 落库，绝不碰真实数据。
覆盖：
- kill switch 关闭 → tick 跳过、不留记录
- 低风险动作（world_search）自主执行 → 写 task_log(done) + 落库
- 高风险动作 → 不执行、写 task_log(proposed) 提议卡
- 提议卡裁决（executed/rejected/never）+ never 后跳过
- 风险闸分级（low/high/unknown）
- 配置读写 + 原子写 + TTL 缓存
"""

import importlib

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
class _FakeLearnedStore:
    """记录 append 调用、不碰真实 DB。"""

    def __init__(self, *a, **k):
        self.appended = []

    def append(self, facts, ts=None):
        facts = list(facts or [])
        self.appended.extend(facts)
        return len(facts)


def _fake_search_factory(results):
    def _search(query, max_results=5, timelimit=None):
        return {"provider": "fake", "results": results}
    return _search


@pytest.fixture
def fake_search(monkeypatch):
    results = [
        {"title": "明史研究新发现", "snippet": "某墓志揭示明代卫所新制"},
        {"title": "AI 编程进展", "snippet": "新模型支持长上下文代理"},
    ]
    monkeypatch.setattr(
        "coworker.web.tool.make_web_search_tool",
        lambda *a, **k: _fake_search_factory(results),
    )
    return results


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeLearnedStore()
    monkeypatch.setattr(
        "deskmate.memory.learned_store.LearnedStore", lambda *a, **k: store
    )
    return store


# ---------------------------------------------------------------------------
# 风险闸
# ---------------------------------------------------------------------------
def test_risk_classify():
    from deskmate.agency.risk import classify_risk, is_low_risk, requires_approval

    assert classify_risk("world_search") == "low"
    assert is_low_risk("world_search") is True
    assert classify_risk("write_file") == "high"
    assert requires_approval("write_file") is True
    # 未知动作 → fail-safe 当成高风险，绝不放行
    assert classify_risk("totally_unknown") == "unknown"
    assert requires_approval("totally_unknown") is True


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------
def test_kill_switch_off_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("DESKMATE_L2_ENABLED", "0")
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import _reset_cache_for_test

    _reset_cache_for_test()
    eng = AgencyEngine(base_dir=str(tmp_path))
    res = eng.tick()
    assert res["skipped"] is True
    # 不应写任何任务记录
    assert eng.log.list(limit=10) == []


# ---------------------------------------------------------------------------
# 低风险动作自主执行
# ---------------------------------------------------------------------------
def test_low_risk_world_search_runs(monkeypatch, tmp_path, fake_search, fake_store):
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import _reset_cache_for_test

    _reset_cache_for_test()
    eng = AgencyEngine(base_dir=str(tmp_path))
    res = eng.tick()
    assert res["skipped"] is False
    # world_search 是低风险 → 应执行并写 done
    done = [r for r in res["executed"] if r["action_type"] == "world_search"]
    assert done, "world_search 应被执行"
    assert done[0]["status"] == "done"
    assert done[0]["count"] >= 1
    # 任务面板应有记录
    tasks = eng.log.list(limit=10)
    assert any(t.action_type == "world_search" and t.status == "done" for t in tasks)
    # 应落库
    assert len(fake_store.appended) >= 1


# ---------------------------------------------------------------------------
# 高风险动作走提议卡
# ---------------------------------------------------------------------------
def test_high_risk_creates_proposal(monkeypatch, tmp_path):
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import (
        AgencySettings,
        save_agency_settings,
        _reset_cache_for_test,
    )

    _reset_cache_for_test()
    # 白名单放一个高风险动作
    s = AgencySettings(enabled=True, whitelist_actions=["write_file"], topics=[])
    save_agency_settings(s, base_dir=str(tmp_path))
    _reset_cache_for_test()

    eng = AgencyEngine(base_dir=str(tmp_path))
    res = eng.tick()
    assert res["skipped"] is False
    proposed = [r for r in res["proposed"] if r["action_type"] == "write_file"]
    assert proposed, "write_file 应生成提议卡而非执行"
    assert proposed[0]["status"] == "proposed"
    # 任务面板应有 proposed 记录
    assert any(t.status == "proposed" for t in eng.log.list(limit=10))


# ---------------------------------------------------------------------------
# 提议卡裁决
# ---------------------------------------------------------------------------
def test_proposal_resolve_and_never_skip(monkeypatch, tmp_path):
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import (
        AgencySettings,
        save_agency_settings,
        _reset_cache_for_test,
    )
    from deskmate.agency.task_log import TaskStatus

    _reset_cache_for_test()
    s = AgencySettings(enabled=True, whitelist_actions=["write_file"], topics=[])
    save_agency_settings(s, base_dir=str(tmp_path))
    _reset_cache_for_test()

    eng = AgencyEngine(base_dir=str(tmp_path))
    eng.tick()  # 生成 proposed
    prop = eng.log.list(limit=10, status=TaskStatus.PROPOSED)[0]
    # 裁决「以后别提」
    ok = eng.resolve_proposal(prop.id, TaskStatus.NEVER)
    assert ok is True
    assert prop.id in [r.id for r in eng.log.list(limit=10, status=TaskStatus.NEVER)]
    # never 列表包含该动作 → 下次 tick 跳过
    assert "write_file" in eng.log.never_list()
    res2 = eng.tick()
    assert all(r["action_type"] != "write_file" for r in res2["proposed"])


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def test_settings_roundtrip_and_cache(tmp_path):
    from deskmate.agency.settings import (
        AgencySettings,
        load_agency_settings,
        save_agency_settings,
        _reset_cache_for_test,
    )

    _reset_cache_for_test()
    s = AgencySettings(
        enabled=True,
        whitelist_actions=["world_search", "rss_digest"],
        topics=["历史研究", "AI 技术动态"],
    )
    assert save_agency_settings(s, base_dir=str(tmp_path)) is True
    _reset_cache_for_test()
    loaded = load_agency_settings(str(tmp_path))
    assert loaded.enabled is True
    assert loaded.whitelist_actions == ["world_search", "rss_digest"]
    assert loaded.topics == ["历史研究", "AI 技术动态"]
    # 损坏文件 → 回落默认
    (tmp_path / "l2_settings.json").write_text("{bad json", encoding="utf-8")
    _reset_cache_for_test()
    fallback = load_agency_settings(str(tmp_path))
    assert fallback.enabled is True
    assert "world_search" in fallback.whitelist_actions
