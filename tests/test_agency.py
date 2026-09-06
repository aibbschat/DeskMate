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
    """拦截内部记忆写入，用于断言「外部信息不会自动内化」。"""
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
    # 搜索结果只落不可信区（external_items），count 是收下的条数
    assert done[0]["count"] >= 1
    # 关键：外部信息可信度 0.5 < 阈值 0.7 → **绝不自动写进内部记忆**
    assert fake_store.appended == [], "未验证的外部信息不该自动内化"


def test_world_search_stores_in_untrusted_zone(monkeypatch, tmp_path, fake_search):
    """搜索结果必须落进 external_items（不可信区），且默认 7 天过期。"""
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import _reset_cache_for_test
    from deskmate.memory.external_store import ExternalStore

    _reset_cache_for_test()
    eng = AgencyEngine(base_dir=str(tmp_path))
    eng.tick()

    store = ExternalStore(base_dir=str(tmp_path))
    raw_items = store.list_items(status=None)
    assert len(raw_items) >= 1, "搜索结果应落进不可信区"
    # 每一条都必须有过期时间（news 类 → 7 天）
    for it in raw_items:
        assert it["expires_at"], "外部信息必须带过期时间，不能永不过期"
        assert it["credibility"] < 0.7, "搜索来源默认可信度应低于自动升格阈值"


def test_low_credibility_creates_review_proposal(monkeypatch, tmp_path, fake_search):
    """可信度不足的外部信息 → 生成高风险提议卡，等人工裁决（不自动执行）。"""
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import _reset_cache_for_test
    from deskmate.agency.task_log import TaskStatus

    _reset_cache_for_test()
    eng = AgencyEngine(base_dir=str(tmp_path))
    res = eng.tick()

    proposals = eng.log.list(limit=50, status=TaskStatus.PROPOSED)
    review = [p for p in proposals if p.action_type == "external_review"]
    assert review, "可信度不足的外部信息应生成裁决提议卡"
    # 高风险 → 只提议，不执行
    assert review[0].status == TaskStatus.PROPOSED
    assert review[0].risk == "high"
    # 提议卡里带着待裁决的条目 id，供人工放行时使用
    assert len(review[0].extra.get("item_ids") or []) >= 1
    # world_search 本身仍是 done（低风险照常执行）
    assert any(
        r["action_type"] == "world_search" and r["status"] == "done"
        for r in res["executed"]
    )


def test_approve_review_writes_internal_memory(monkeypatch, tmp_path, fake_search, fake_store):
    """人工裁决「执行」后，待裁决的外部信息才写进内部记忆。"""
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    from deskmate.agency.engine import AgencyEngine
    from deskmate.agency.settings import _reset_cache_for_test
    from deskmate.agency.task_log import TaskStatus

    _reset_cache_for_test()
    eng = AgencyEngine(base_dir=str(tmp_path))
    eng.tick()
    prop = [
        p for p in eng.log.list(limit=50, status=TaskStatus.PROPOSED)
        if p.action_type == "external_review"
    ][0]

    assert fake_store.appended == [], "裁决前不该进内部记忆"
    ok = eng.resolve_proposal(prop.id, TaskStatus.EXECUTED)
    assert ok is True
    assert len(fake_store.appended) >= 1, "人工放行后应写入内部记忆"
    # 写入的事实必须带溯源标记，不能和萧哥亲口说的话混同
    assert all("（外部·" in f for f in fake_store.appended)


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
