"""Tests for deskmate.digest.engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deskmate.digest.engine import DigestCategory, DigestItem, _cache_path, _read_cache, _write_cache, get_digest


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """把资讯缓存隔离到临时目录，避免污染真实状态。"""
    monkeypatch.setattr("deskmate.digest.engine._base_dir", lambda: tmp_path)


def _make_search_tool(results):
    """构造一个返回固定结果的 web_search tool mock。"""

    def tool(query: str, max_results: int = 5):
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
    search_results = [
        {"title": "新标题", "snippet": "新摘要", "url": "http://example.com"}
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
