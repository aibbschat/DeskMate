"""2026-11-10「反应慢」回归：首字前热路径不得再做同步网络 / 重复重活。

三条护栏：
1) weather_context（每轮记忆注入里被调多次）只读缓存，永不自己打网络；
2) semantic_search 的 query 向量与向量条目缓存是**进程级**的（跨实例复用）；
3) proactive 后台 tick 在用户刚聊过的静默窗口内让路。
"""
from __future__ import annotations

import time

import pytest

from deskmate.perception import weather as wx_mod


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_MEMORY_DIR", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER_LOCATION", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER_LAT", raising=False)
    monkeypatch.delenv("DESKMATE_WEATHER_LON", raising=False)
    return tmp_path


_WX = {
    "location": "宁波",
    "condition": "rain",
    "condition_text": "小雨",
    "temp_c": 12.0,
    "feels_like_c": 11.0,
    "humidity": 80,
    "today": {"min_c": 8.0, "max_c": 15.0, "chance_of_rain": 90},
    "provider": "wttr",
    "fetched_at": "2026-11-10T09:00:00",
}


def _save(base, age_sec: float) -> None:
    wx_mod.save_cache(
        base,
        {"data": dict(_WX), "fetched_at": _WX["fetched_at"], "fetched_at_ts": time.time() - age_sec},
    )


def test_weather_context_never_fetches_synchronously(base, monkeypatch):
    """陈旧缓存下 weather_context 必须用旧值 + 排后台刷新，**不能**同步联网。"""
    _save(base, age_sec=3 * 3600)  # 过期但仍在「可当参考」窗口内
    called: list[float] = []
    monkeypatch.setattr(wx_mod, "fetch_weather", lambda *a, **kw: called.append(time.time()))

    t0 = time.perf_counter()
    out = wx_mod.weather_context("今天下雨吗？我要出门", base_dir=base)
    elapsed = time.perf_counter() - t0

    assert out and "宁波" in out, "陈旧缓存仍要注入（陈旧事实 > 没有事实）"
    assert "数据时间" in out, "陈旧数据必须标注数据时间"
    # 热路径不许等网络：耗时必须 << 一次 wttr 往返（实测 wttr 1.7-7s）
    assert elapsed < 0.2, f"热路径不许等网络（实测 {elapsed*1000:.0f} ms）"
    # 刷新发生在**后台线程**：等它把 fetch_weather 调起来即可（不要求完成）
    deadline = time.time() + 2.0
    while not called and time.time() < deadline:
        time.sleep(0.02)
    assert called, "陈旧缓存应排队后台刷新"


def test_weather_context_no_cache_returns_none_fast(base, monkeypatch):
    """完全没有缓存：立刻返回 None（不注入），并排后台刷新。"""
    monkeypatch.setattr(wx_mod, "fetch_weather", lambda *a, **kw: None)
    monkeypatch.setattr(wx_mod, "_REFRESH_MIN_INTERVAL", 0.0)
    t0 = time.perf_counter()
    out = wx_mod.weather_context("今天下雨吗", base_dir=base)
    assert out is None
    assert (time.perf_counter() - t0) < 0.2


def test_weather_context_ancient_cache_not_injected(base, monkeypatch):
    """旧到超过 grace 窗口（6h）就不再当「此刻的天气」讲。"""
    _save(base, age_sec=wx_mod._STALE_GRACE_SEC + 3600)
    monkeypatch.setattr(wx_mod, "fetch_weather", lambda *a, **kw: None)
    assert wx_mod.weather_context("今天下雨吗", base_dir=base) is None


def test_query_embedding_cached(monkeypatch):
    from deskmate.memory import semantic_search as ss

    ss._embed_text_cache.clear()
    calls: list[list[str]] = []

    def fake_call(texts):
        calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(ss, "_call_embedding_api", fake_call)
    assert ss.embed_text("同一句话") == [0.1, 0.2, 0.3]
    assert ss.embed_text("同一句话") == [0.1, 0.2, 0.3]
    assert len(calls) == 1, "同一 query 第二次必须命中缓存（一轮内 context_provider 会被调两次）"
    ss._embed_text_cache.clear()


def test_embeddable_items_cache_shared_across_instances(monkeypatch, tmp_path):
    """world_context 每次 new 一个 SemanticSearcher —— 条目缓存必须跨实例共享。"""
    from deskmate.memory import semantic_search as ss
    from deskmate.memory.external_store import ExternalStore

    ss._cache_invalidate_items()
    store = ExternalStore(base_dir=tmp_path)
    item = {
        "id": "i1", "title": "t", "content": "c", "summary": "s",
        "fetched_at": "2026-11-10", "category": "news", "credibility": 0.8,
        "embedding": [0.1, 0.2, 0.3], "source_type": "search",
    }
    loads: list[int] = []

    def fake_load(trusted_only=True):
        loads.append(1)
        return [dict(item)]

    monkeypatch.setattr(store, "load_embeddable_items", fake_load)
    first = ss.SemanticSearcher(store=store)
    second = ss.SemanticSearcher(store=store)
    assert [i["id"] for i in first._load_embeddable_cached()] == ["i1"]
    assert [i["id"] for i in second._load_embeddable_cached()] == ["i1"]
    assert loads == [1], "第二个实例应复用进程级缓存，而不是再读一遍全表向量"
    ss._cache_invalidate_items()


def test_background_tick_skips_right_after_user_chat(monkeypatch):
    from deskmate.proactivity import background

    monkeypatch.delenv("DESKMATE_PROACTIVE_QUIET_SECONDS", raising=False)
    background._last_user_activity = 0.0
    assert background.should_skip_background_tick() is False
    background.note_user_activity()
    assert background.should_skip_background_tick() is True
    monkeypatch.setenv("DESKMATE_PROACTIVE_QUIET_SECONDS", "0")
    assert background.should_skip_background_tick() is False, "设 0 = 关闭静默窗口（恢复旧行为）"
    background._last_user_activity = 0.0
