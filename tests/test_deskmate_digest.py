"""Tests for deskmate.digest.engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deskmate.digest.engine import (
    DigestCategory,
    DigestItem,
    _cache_path,
    _fetch_one_category,
    _is_spam,
    _read_cache,
    _write_cache,
    get_digest,
)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """把资讯缓存隔离到临时目录，避免污染真实状态。"""
    monkeypatch.setattr("deskmate.digest.engine._base_dir", lambda: tmp_path)


def _make_search_tool(results):
    """构造一个返回固定结果的 web_search tool mock。"""

    def tool(query: str, max_results: int = 5, timelimit=None):
        return {"provider": "mock", "results": results}

    return tool


def _stable_id(category, title):
    import hashlib

    h = hashlib.sha1(f"{category}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"{category}-{h}"


def test_cache_roundtrip():
    items = [
        DigestItem(
            id=_stable_id("culture", "标题一"),
            category="culture",
            title="标题一",
            summary="摘要一",
            query="查询一",
            source_count=3,
            freshness="刚刚",
        )
    ]
    assert _read_cache() is None
    _write_cache(items)
    cached_items, updated_at = _read_cache()
    assert len(cached_items) == 1
    assert cached_items[0].title == "标题一"
    assert updated_at > 0


def test_cache_version_mismatch_ignored():
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 0, "updated_at": 999, "items": []}), encoding="utf-8")
    assert _read_cache() is None


@pytest.mark.asyncio
async def test_get_digest_returns_cached_when_fresh():
    items = [
        DigestItem(
            id=_stable_id("culture", "缓存标题"),
            category="culture",
            title="缓存标题",
            summary="缓存摘要",
            query="缓存查询",
            source_count=2,
            freshness="刚刚",
        )
    ]
    _write_cache(items)

    with patch("deskmate.digest.engine.make_web_search_tool") as mock_tool:
        # 缓存未过期，不应触发搜索
        result = await get_digest()
        mock_tool.assert_not_called()
        assert result["fresh"] is True
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "缓存标题"


@pytest.mark.asyncio
async def test_get_digest_force_refresh():
    # 摘要里带上各分类主题关键词，才能通过进 LLM 前的相关性过滤
    search_results = [
        {"title": "新标题", "snippet": "网文 短剧 AI 开源 风向", "url": "http://example.com"}
    ]
    llm_output = json.dumps(
        [{"title": "刷新标题", "summary": "刷新摘要", "query": "刷新查询"}],
        ensure_ascii=False,
    )

    _write_cache(
        [
            DigestItem(
                id=_stable_id("culture", "旧标题"),
                category="culture",
                title="旧标题",
                summary="旧摘要",
                query="旧查询",
                source_count=1,
                freshness="刚刚",
            )
        ]
    )

    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(search_results)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", return_value=llm_output):
                result = await get_digest(force=True)
                assert result["fresh"] is True
                # 4 个分类共用同一个 mock 搜索工具，每个分类各产 1 条
                assert len(result["items"]) == 4
                assert all(it["title"] == "刷新标题" for it in result["items"])


@pytest.mark.asyncio
async def test_get_digest_falls_back_to_stale_cache_on_refresh_failure():
    items = [
        DigestItem(
            id=_stable_id("culture", "旧标题"),
            category="culture",
            title="旧标题",
            summary="旧摘要",
            query="旧查询",
            source_count=1,
            freshness="刚刚",
        )
    ]
    _write_cache(items)

    with patch("deskmate.digest.engine.make_web_search_tool", side_effect=RuntimeError("network")):
        # 强制刷新但搜索失败，应返回旧缓存
        result = await get_digest(force=True)
        assert result["fresh"] is False
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "旧标题"


@pytest.mark.asyncio
async def test_category_queries_cover_all_categories():
    """每个分类至少配置了一个查询词，避免遗漏。"""
    from deskmate.digest.engine import _CATEGORY_QUERIES

    for cat in DigestCategory:
        assert cat in _CATEGORY_QUERIES
        assert len(_CATEGORY_QUERIES[cat]) > 0


@pytest.mark.asyncio
async def test_llm_drops_stale_recency():
    """LLM 若把某条标记为旧闻，应从结果中剔除（近周时间窗 + 风向护栏）。"""
    search_results = [
        {"title": "新标题", "snippet": "网文 短剧 AI 开源 风向", "url": "http://example.com"}
    ]
    # 一条正常、一条被 LLM 判为旧闻
    llm_output = json.dumps(
        [
            {"title": "近期风向", "summary": "近期摘要", "query": "近期查询", "recency": "近期"},
            {"title": "旧闻一条", "summary": "旧闻摘要", "query": "旧闻查询", "recency": "旧闻"},
        ],
        ensure_ascii=False,
    )

    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(search_results)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", return_value=llm_output):
                result = await get_digest(force=True)
                # 4 个分类各产 1 条（旧闻被丢弃），不应出现「旧闻一条」
                assert len(result["items"]) == 4
                assert all(it["title"] == "近期风向" for it in result["items"])


@pytest.mark.asyncio
async def test_digest_persists_to_learned_history_with_expiry(tmp_path: Path):
    """AI 圈网文观察日记（digest）应直接升格进内部记忆：

    - 带「外部·文化产业」溯源标；
    - 写入 valid_until（过期机制），且重复刷新幂等、不刷新已存在条目的过期时间。
    """
    from deskmate.memory.learned_store import LearnedStore

    search_results = [
        {"title": "新标题", "snippet": "网文 短剧 AI 开源 风向", "url": "http://example.com"}
    ]
    llm_output = json.dumps(
        [{"title": "升格标题", "summary": "升格摘要", "query": "升格查询"}],
        ensure_ascii=False,
    )

    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(search_results)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", return_value=llm_output):
                result = await get_digest(force=True)
                assert result["fresh"] is True
                # 4 个分类各产 1 条，全部升格进记忆
                assert len(result["items"]) == 4

    store = LearnedStore(base_dir=tmp_path)
    snap = store.active_recent(limit=100)
    assert len(snap) == 4
    # 溯源标 + 分类标签正确
    joined = "\n".join(i["fact"] for i in snap)
    assert "（外部·文化产业）" in joined
    assert "网文风向" in joined and "AI圈风向" in joined

    # 全部带有效过期时间（valid_until 非空且 >= 当前）
    import datetime as _dt
    rows = store._connect().execute(
        "SELECT fact, valid_until FROM learned_history WHERE status='active'"
    ).fetchall()
    assert len(rows) == 4
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for fact, valid_until in rows:
        assert valid_until and valid_until >= now, f"缺少有效过期时间: {fact}"

    # 重复刷新：相同 fact 命中 fp，INSERT OR IGNORE → 不新增、不刷新过期
    before = store.active_stats()["total"]
    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(search_results)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", return_value=llm_output):
                await get_digest(force=True)
    after = store.active_stats()["total"]
    assert after == before == 4


@pytest.mark.asyncio
async def test_digest_persist_fail_open_on_memory_error():
    """记忆写入异常时 fail-open：资讯链路不崩，照常返回条目。"""
    search_results = [
        {"title": "新标题", "snippet": "网文 短剧 AI 开源 风向", "url": "http://example.com"}
    ]
    llm_output = json.dumps(
        [{"title": "升格标题", "summary": "升格摘要", "query": "升格查询"}],
        ensure_ascii=False,
    )

    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(search_results)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", return_value=llm_output):
                with patch("deskmate.digest.engine._persist_to_memory", side_effect=RuntimeError("db down")):
                    # 即便落记忆抛异常，刷新本身仍成功
                    result = await get_digest(force=True)
                    assert result["fresh"] is True
                    assert len(result["items"]) == 4


def test_is_spam_detects_adult_and_garbage():
    """_is_spam 应识别成人/垃圾站结果，放过正常结果。"""
    assert _is_spam({"title": "七天探花", "snippet": "网红黑料", "url": "http://aydvjch.cc/category/mrds"}) is True
    assert _is_spam({"title": "Onlyfans 巨乳女神", "snippet": "", "url": "http://x.com"}) is True
    assert _is_spam({"title": "番茄起点平台对比", "snippet": "网文平台分析", "url": "http://news.example.com/a"}) is False
    assert _is_spam({"title": "正常影视资讯", "snippet": "热度趋势", "url": "http://ent.example.com/b"}) is False


@pytest.mark.asyncio
async def test_spam_filtered_before_llm():
    """成人/垃圾搜索结果必须在进 LLM 前被剔除，否则会污染提炼或导致整类失败。"""
    mixed = [
        {"title": "番茄起点平台对比", "snippet": "网文平台分析", "url": "http://good.com/a"},
        {"title": "七天探花最新合集", "snippet": "网红黑料", "url": "http://aydvjch.cc/category/mrds"},
        {"title": "Onlyfans 巨乳女神", "snippet": "成人内容", "url": "http://x.com"},
    ]
    captured: dict = {}

    def fake_chat(system, user, **kw):
        captured["user"] = user
        return json.dumps([{"title": "提炼结果", "summary": "一句话", "query": "查询"}])

    with patch("deskmate.digest.engine.make_web_search_tool", return_value=_make_search_tool(mixed)):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            with patch("deskmate.memory.llm.chat", side_effect=fake_chat):
                items = await _fetch_one_category(DigestCategory.WEBNOVEL)

    # 垃圾结果未进 LLM 提示词
    assert "探花" not in captured["user"]
    assert "aydvjch" not in captured["user"]
    assert "Onlyfans" not in captured["user"]
    # 正常结果进了 LLM，且产出一条卡片
    assert "番茄" in captured["user"]
    assert len(items) == 1
    assert items[0].title == "提炼结果"
