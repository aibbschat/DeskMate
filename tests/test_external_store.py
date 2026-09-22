"""ExternalStore + SemanticSearch 单元测试。

覆盖：
- external_sources CRUD
- external_items 写入 / 去重幂等 / 读取 / 列表 / 关键词检索
- ingestion_status 状态推进（raw → summarized → embedded → digested → archived）
- embedding BLOB 写入 / 加载 / 反序列化
- TTL 过期过滤 + archive_expired
- 统计
- cosine_similarity 正确性
- SemanticSearcher 语义检索（mock embed_text）
- 混合检索双路召回 + 去重合并
- embedding 不可用时的 fail-open 降级
"""

from __future__ import annotations

import datetime
import os
import tempfile
from pathlib import Path

import pytest

# 每个测试用独立临时目录，隔离真实 ~/.deskmate
@pytest.fixture
def store(tmp_path):
    from deskmate.memory.external_store import ExternalStore
    # 显式传 base_dir，因为 _DEFAULT_DIR 是模块级计算的，monkeypatch HOME 不生效
    return ExternalStore(base_dir=tmp_path / ".deskmate")


# =====================================================================
# external_sources
# =====================================================================
class TestSources:
    def test_add_and_get(self, store):
        sid = store.add_source("测试RSS", source_type="rss", url="https://x.com/feed", credibility=0.7)
        assert sid is not None
        src = store.get_source(sid)
        assert src["name"] == "测试RSS"
        assert src["type"] == "rss"
        assert src["url"] == "https://x.com/feed"
        assert src["credibility"] == 0.7
        assert src["enabled"] is True

    def test_list_and_filter(self, store):
        store.add_source("A", source_type="rss")
        store.add_source("B", source_type="email", enabled=False)
        store.add_source("C", source_type="api")
        assert len(store.list_sources()) == 3
        assert len(store.list_sources(enabled_only=True)) == 2
        assert len(store.list_sources(source_type="rss")) == 1

    def test_update(self, store):
        sid = store.add_source("旧名", source_type="rss", credibility=0.5)
        ok = store.update_source(sid, name="新名", credibility=0.9, enabled=False)
        assert ok
        src = store.get_source(sid)
        assert src["name"] == "新名"
        assert src["credibility"] == 0.9
        assert src["enabled"] is False

    def test_delete(self, store):
        sid = store.add_source("待删")
        assert store.delete_source(sid) is True
        assert store.get_source(sid) is None


# =====================================================================
# external_items 写入 / 去重
# =====================================================================
class TestItemsWrite:
    def test_add_basic(self, store):
        item = store.add_item("测试内容", title="标题", category="tech")
        assert item is not None
        assert item["content"] == "测试内容"
        assert item["title"] == "标题"
        assert item["category"] == "tech"
        assert item["ingestion_status"] == "raw"
        assert item["content_hash"]  # 非空

    def test_duplicate_idempotent(self, store):
        """同内容重复写入返回已有条目，不创建新行。"""
        item1 = store.add_item("重复内容测试", title="A")
        item2 = store.add_item("重复内容测试", title="B")  # 标题不同但内容相同
        assert item1["id"] == item2["id"]
        assert store.stats()["total"] == 1

    def test_whitespace_insensitive_dedup(self, store):
        """空白差异不影响去重。"""
        item1 = store.add_item("hello   world\n\n")
        item2 = store.add_item("hello world")
        assert item1["id"] == item2["id"]

    def test_source_type_inherited_from_source(self, store):
        """传入 source_id 且未显式指定 source_type 时，从来源记录继承。"""
        sid = store.add_source("财经源", source_type="rss", credibility=0.8)
        item = store.add_item("财经新闻", source_id=sid)
        assert item["source_type"] == "rss"
        assert item["credibility"] == 0.8  # 可信度也继承

    def test_empty_content_rejected(self, store):
        assert store.add_item("") is None
        assert store.add_item("   ") is None


# =====================================================================
# external_items 读取 / 列表 / 过滤
# =====================================================================
class TestItemsRead:
    def test_get_by_id(self, store):
        item = store.add_item("按ID查", title="T")
        got = store.get_item(item["id"])
        assert got is not None
        assert got["id"] == item["id"]

    def test_get_by_hash(self, store):
        item = store.add_item("按hash查")
        got = store.get_item_by_hash(item["content_hash"])
        assert got is not None
        assert got["id"] == item["id"]

    def test_list_filter_by_category(self, store):
        store.add_item("财经1", category="finance")
        store.add_item("科技1", category="tech")
        store.add_item("财经2", category="finance")
        assert len(store.list_items(category="finance")) == 2
        assert len(store.list_items(category="tech")) == 1

    def test_list_pagination(self, store):
        for i in range(5):
            store.add_item(f"内容{i}")
        page1 = store.list_items(limit=2, offset=0)
        page2 = store.list_items(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # 倒序，page1 的 fetched_at 应新于 page2
        assert page1[0]["fetched_at"] >= page2[0]["fetched_at"]


# =====================================================================
# 关键词检索
# =====================================================================
class TestKeywordSearch:
    def test_basic(self, store):
        store.add_item("美联储宣布加息25基点", title="美联储加息")
        store.add_item("苹果发布新iPhone", title="苹果新品")
        results = store.search_items_keyword("美联储 加息", trusted_only=False)
        assert len(results) == 1
        assert "美联储" in results[0]["title"]

    def test_no_match(self, store):
        store.add_item("科技新闻")
        assert store.search_items_keyword("不存在的关键词xyz") == []

    def test_empty_query(self, store):
        store.add_item("内容")
        assert store.search_items_keyword("") == []


# =====================================================================
# ingestion_status 状态推进
# =====================================================================
class TestStatusFlow:
    def test_full_flow(self, store):
        item = store.add_item("状态流转测试")
        assert item["ingestion_status"] == "raw"

        store.update_summary(item["id"], "摘要")
        assert store.get_item(item["id"])["ingestion_status"] == "summarized"

        store.update_embedding(item["id"], [0.1, 0.2, 0.3], model="test")
        got = store.get_item(item["id"])
        assert got["ingestion_status"] == "embedded"
        assert got["has_embedding"] is True
        assert got["embedding_dim"] == 3

        store.mark_digested(item["id"], related_memory_ids=["mem_1"])
        got = store.get_item(item["id"])
        assert got["ingestion_status"] == "digested"
        assert got["related_memory_ids"] == ["mem_1"]

        store.update_status(item["id"], "archived")
        assert store.get_item(item["id"])["ingestion_status"] == "archived"

    def test_invalid_status_rejected(self, store):
        item = store.add_item("测试")
        assert store.update_status(item["id"], "nonexistent") is False


# =====================================================================
# embedding BLOB 写入 / 加载
# =====================================================================
class TestEmbeddingBlob:
    def test_roundtrip(self, store):
        vec = [0.1 * i for i in range(64)]
        item = store.add_item("向量测试")
        store.update_embedding(item["id"], vec, model="test-64d")
        # 条目此时是 embedded（未验证），可信闸门默认不放行。
        # 本用例测的是 BLOB 序列化往返，显式关掉闸门。
        embeddable = store.load_embeddable_items(trusted_only=False)
        assert len(embeddable) == 1
        assert len(embeddable[0]["embedding"]) == 64
        for i, v in enumerate(vec):
            assert abs(embeddable[0]["embedding"][i] - v) < 1e-5

    def test_empty_embedding_rejected(self, store):
        item = store.add_item("空向量")
        assert store.update_embedding(item["id"], []) is False


# =====================================================================
# TTL 过期
# =====================================================================
class TestTTL:
    def test_expired_excluded_from_list(self, store):
        store.add_item("有效内容")
        expired = (datetime.datetime.now() - datetime.timedelta(days=1)).astimezone().isoformat(timespec="seconds")
        store.add_item("过期内容", expires_at=expired)
        assert len(store.list_items()) == 1  # 过期的被过滤

    def test_archive_expired(self, store):
        expired = (datetime.datetime.now() - datetime.timedelta(days=1)).astimezone().isoformat(timespec="seconds")
        item = store.add_item("将过期", expires_at=expired)
        n = store.archive_expired()
        assert n == 1
        assert store.get_item(item["id"])["ingestion_status"] == "archived"

    def test_not_expired_not_archived(self, store):
        future = (datetime.datetime.now() + datetime.timedelta(days=1)).astimezone().isoformat(timespec="seconds")
        store.add_item("未过期", expires_at=future)
        assert store.archive_expired() == 0


# =====================================================================
# 统计
# =====================================================================
class TestStats:
    def test_empty(self, store):
        s = store.stats()
        assert s["total"] == 0
        assert s["embeddable"] == 0

    def test_with_data(self, store):
        store.add_item("财经", category="finance")
        store.add_item("科技", category="tech")
        item = store.add_item("有向量", category="finance")
        store.update_embedding(item["id"], [0.1, 0.2])
        s = store.stats()
        assert s["total"] == 3
        assert s["embeddable"] == 1
        assert s["by_category"]["finance"] == 2
        assert s["by_category"]["tech"] == 1


# =====================================================================
# cosine_similarity
# =====================================================================
class TestCosineSimilarity:
    def test_identical(self):
        from deskmate.memory.semantic_search import cosine_similarity
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal(self):
        from deskmate.memory.semantic_search import cosine_similarity
        assert cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_opposite(self):
        from deskmate.memory.semantic_search import cosine_similarity
        assert abs(cosine_similarity([1, 0], [-1, 0]) - (-1.0)) < 1e-6

    def test_dim_mismatch(self):
        from deskmate.memory.semantic_search import cosine_similarity
        assert cosine_similarity([1, 2], [1, 2, 3]) == 0.0

    def test_empty(self):
        from deskmate.memory.semantic_search import cosine_similarity
        assert cosine_similarity([], []) == 0.0


# =====================================================================
# SemanticSearcher 语义检索（mock）
# =====================================================================
class TestSemanticSearch:
    @pytest.fixture
    def populated_store(self, store):
        """写入6条带mock向量的条目，3维空间区分财经/科技/生活。"""
        data = [
            ("美联储加息", "finance", [0.9, 0.1, 0.0]),
            ("央行降准", "finance", [0.85, 0.05, 0.0]),
            ("苹果MacBook", "tech", [0.1, 0.9, 0.0]),
            ("iPhone评测", "tech", [0.05, 0.95, 0.0]),
            ("科技股上涨", "tech", [0.3, 0.8, 0.0]),
            ("公园散步", "life", [0.0, 0.0, 1.0]),
        ]
        for title, cat, vec in data:
            item = store.add_item(title + "内容", title=title, category=cat)
            store.update_embedding(item["id"], vec, model="mock-3d")
            store.mark_digested(item["id"])
        return store

    def test_semantic_search_finance(self, populated_store, monkeypatch):
        """财经query应排财经内容在前。"""
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=populated_store)
        results = searcher.semantic_search("美联储加息", limit=10)
        assert len(results) >= 2
        # 前两名应是财经内容
        assert "美联储" in results[0]["title"] or "央行" in results[0]["title"]
        assert results[0]["similarity"] > results[-1]["similarity"]

    def test_semantic_search_tech(self, populated_store, monkeypatch):
        """科技query应排科技内容在前。"""
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.1, 0.9, 0.0])

        searcher = ss.SemanticSearcher(store=populated_store)
        results = searcher.semantic_search("苹果手机", limit=10)
        assert len(results) >= 2
        assert "苹果" in results[0]["title"] or "iPhone" in results[0]["title"] or "科技" in results[0]["title"]

    def test_min_similarity_threshold(self, populated_store, monkeypatch):
        """低于阈值的结果被过滤。"""
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=populated_store)
        results = searcher.semantic_search("test", limit=10, min_similarity=0.99)
        # 只有完全相同或极近的才通过
        assert len(results) <= 2

    def test_embedding_unavailable_returns_empty(self, populated_store, monkeypatch):
        """embed_text 返回 None 时语义检索返回空。"""
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: None)

        searcher = ss.SemanticSearcher(store=populated_store)
        assert searcher.semantic_search("test") == []


# =====================================================================
# 混合检索
# =====================================================================
class TestHybridSearch:
    @pytest.fixture
    def populated_store(self, store):
        data = [
            ("美联储加息对股市影响", "finance", [0.9, 0.1, 0.0]),
            ("央行降准释放流动性", "finance", [0.85, 0.05, 0.0]),
            ("苹果发布MacBook", "tech", [0.1, 0.9, 0.0]),
            ("iPhone 16评测", "tech", [0.05, 0.95, 0.0]),
        ]
        for title, cat, vec in data:
            item = store.add_item(title + "详细内容", title=title, category=cat)
            store.update_embedding(item["id"], vec, model="mock-3d")
            store.mark_digested(item["id"])
        return store

    def test_hybrid_with_embedding(self, populated_store, monkeypatch):
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=populated_store)
        result = searcher.hybrid_search("美联储 加息", limit=10)
        assert result["embedding_available"] is True
        assert result["alpha"] == 0.7
        assert result["semantic_count"] > 0
        assert len(result["results"]) > 0
        # 第一名应是美联储相关
        assert "美联储" in result["results"][0]["title"]

    def test_hybrid_failopen_no_embedding(self, populated_store, monkeypatch):
        """embedding不可用时降级为纯关键词，alpha=0。"""
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "embed_text", lambda text: None)

        searcher = ss.SemanticSearcher(store=populated_store)
        result = searcher.hybrid_search("美联储 加息", limit=10)
        assert result["embedding_available"] is False
        assert result["alpha"] == 0.0
        assert result["semantic_count"] == 0
        assert result["keyword_count"] > 0
        assert len(result["results"]) > 0

    def test_hybrid_dedup(self, populated_store, monkeypatch):
        """同一条目在两路都命中时合并为一条，search_type=hybrid。"""
        import deskmate.memory.semantic_search as ss
        # query向量与美联储条目完全相同，且关键词也匹配
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=populated_store)
        result = searcher.hybrid_search("美联储 加息", limit=10)
        ids = [r["id"] for r in result["results"]]
        assert len(ids) == len(set(ids))  # 无重复


# =====================================================================
# 批量向量化管道（mock）
# =====================================================================
class TestEmbedPending:
    def test_embed_pending(self, store, monkeypatch):
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])

        store.add_item("待向量化1")
        store.add_item("待向量化2")
        store.add_item("待向量化3")

        searcher = ss.SemanticSearcher(store=store)
        result = searcher.embed_pending_items(limit=10)
        assert result["processed"] == 3
        assert result["embedded"] == 3
        assert store.stats()["embeddable"] == 3

    def test_embed_pending_api_unavailable(self, store, monkeypatch):
        import deskmate.memory.semantic_search as ss
        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: None)

        store.add_item("待向量化")
        searcher = ss.SemanticSearcher(store=store)
        result = searcher.embed_pending_items()
        assert result["embedded"] == 0
        assert result["failed"] >= 1
        # 条目仍以 raw 状态保留，不丢失
        assert store.stats()["total"] == 1


# =====================================================================
# 可信闸门（2026-09-06 萧哥拍板：未验证的外部信息一律不进检索）
# =====================================================================
class TestTrustedGate:
    def test_unverified_not_searchable(self, store):
        """raw / summarized / embedded 都属于不可信区，不该被检索到。"""
        for t in ["未验证的甲", "未验证的乙"]:
            it = store.add_item(t, title=t)
            store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        # 闸门默认开 → 语义候选集为空
        assert store.load_embeddable_items() == []
        # 关键词检索同理
        assert store.search_items_keyword("未验证") == []
        # 显式关闸门才可见（仅供调试/人工翻查，不应进对话上下文）
        assert len(store.load_embeddable_items(trusted_only=False)) == 2
        assert len(store.search_items_keyword("未验证", trusted_only=False)) == 2

    def test_digested_is_searchable(self, store):
        it = store.add_item("已验证的丙", title="已验证的丙")
        store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        store.mark_digested(it["id"])
        assert len(store.load_embeddable_items()) == 1

    def test_low_credibility_blocked_until_digested(self, store):
        """低可信来源：只要没 digested 就一律拦下，不靠「分数低但照样出现」放行。"""
        low = store.add_source("野鸡源", source_type="webpage", credibility=0.1)
        it = store.add_item("野鸡说法", title="野鸡说法", source_id=low)
        store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        assert store.load_embeddable_items() == []

    def test_hybrid_search_respects_gate(self, store, monkeypatch):
        """混合检索（含关键词兜底）也必须守闸门。"""
        import deskmate.memory.semantic_search as ss

        it = store.add_item("未验证的丁", title="未验证的丁")
        store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=store)
        assert searcher.hybrid_search("未验证")["results"] == []
        # 关掉闸门才召回得到
        assert len(searcher.hybrid_search("未验证", trusted_only=False)["results"]) == 1


# =====================================================================
# 默认 TTL（2026-09-06：外部信息必须有过期时间）
# =====================================================================
def _expires_in_days(item: dict):
    """把 expires_at 换算成「还剩几天」，None 表示永不过期。"""
    if not item["expires_at"]:
        return None
    exp = datetime.datetime.fromisoformat(item["expires_at"])
    return round((exp - datetime.datetime.now().astimezone()).total_seconds() / 86400)


class TestDefaultTTL:
    def test_auto_ttl_applied_when_not_given(self, store):
        """不传 expires_at 时必须自动算出过期时间，不能留空（空=永不过期）。"""
        it = store.add_item("随便搜来的东西", source_type="webpage")
        assert it["expires_at"], "外部信息默认必须带过期时间"
        assert _expires_in_days(it) == 30

    def test_ttl_by_source_type(self, store):
        assert _expires_in_days(store.add_item("a", source_type="rss")) == 30
        assert _expires_in_days(store.add_item("b", source_type="webpage")) == 30
        assert _expires_in_days(store.add_item("c", source_type="api")) == 90
        # 手动录入是萧哥自己的东西，不该被时效性规则误杀
        assert _expires_in_days(store.add_item("d", source_type="manual")) is None

    def test_category_overrides_source_type(self, store):
        """新闻/财经时效性最强，压到 7 天。"""
        assert _expires_in_days(store.add_item("e", source_type="rss", category="news")) == 7
        assert _expires_in_days(store.add_item("f", source_type="api", category="finance")) == 7

    def test_unknown_source_falls_back_conservative(self, store):
        assert _expires_in_days(store.add_item("g", source_type="新类型")) == 30

    def test_source_level_ttl_wins(self, store):
        sid = store.add_source("长保质期源", source_type="api", default_ttl_days=180)
        it = store.add_item("h", source_id=sid)
        assert _expires_in_days(it) == 180

    def test_explicit_expires_at_not_overridden(self, store):
        """调用方显式指定时以调用方为准，不被分档覆盖。"""
        custom = (
            datetime.datetime.now() + datetime.timedelta(days=3)
        ).astimezone().isoformat(timespec="seconds")
        it = store.add_item("i", source_type="rss", expires_at=custom)
        assert _expires_in_days(it) == 3

    def test_expired_item_excluded_and_archivable(self, store):
        """过期条目既查不到，也能被 archive_expired 归档。"""
        past = (
            datetime.datetime.now() - datetime.timedelta(days=1)
        ).astimezone().isoformat(timespec="seconds")
        it = store.add_item("过期内容", expires_at=past)
        assert store.list_items() == []
        assert store.archive_expired() == 1
        assert store.get_item(it["id"])["ingestion_status"] == "archived"


# =====================================================================
# 向量缓存不丢字段（回归：旧版缓存命中时返回空壳）
# =====================================================================
class TestCacheFieldIntegrity:
    def test_second_search_keeps_fields(self, store, monkeypatch):
        """同一实例 5 分钟内第 2 次检索，字段不能变空、可信度不能被抹平。

        旧实现缓存只存 (vector, fetched_at)，命中分支重建 dict 时把
        title/content 填空串、credibility 填 0.5，可信度加权直接失效。
        """
        import deskmate.memory.semantic_search as ss

        sid = store.add_source("权威源", source_type="rss", credibility=0.9)
        items = [
            store.add_item("美联储加息", title="美联储加息", source_id=sid)
            for _ in range(1)
        ]
        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: [[0.9, 0.1, 0.0] for _ in texts])
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=store)
        searcher.embed_pending_items(limit=10)
        for it in items:
            store.mark_digested(it["id"])
        searcher.invalidate_cache()

        first = searcher.semantic_search("美联储", limit=5)
        assert len(first) == 1
        second = searcher.semantic_search("美联储", limit=5)
        assert len(second) == 1
        for res in first + second:
            assert res["title"] == "美联储加息"
            assert res["content"] == "美联储加息"
            assert res["credibility"] == 0.9

    def test_gate_switch_invalidates_cache(self, store, monkeypatch):
        """闸门档位切换后不能复用旧缓存，否则会绕过 trusted_only。"""
        import deskmate.memory.semantic_search as ss

        it = store.add_item("未验证条目", title="未验证条目")
        store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=store)
        # 闸门开：查不到
        assert searcher.semantic_search("未验证", trusted_only=True) == []
        # 闸门关：查得到（缓存档位不同，必须重查而不是复用）
        assert len(searcher.semantic_search("未验证", trusted_only=False)) == 1


# =====================================================================
# 「digested 但没向量」的孤儿条目（2026-09-22 修）
# =====================================================================
class TestDigestedOrphanBackfill:
    """digested 条目必须**既能补向量、又不会因补向量被降级**。

    真实背景：入库流水线（``ingestion_pipeline`` 的自动升格 / ``promote_approved``
    的人工裁决）把 summarized **直接**标成 digested，从不经过 embedded；而老版
    ``embed_pending_items`` 只挑 raw/summarized，老版 ``update_embedding`` 又无条件
    写 embedded —— 两头夹击，一条 digested 且没向量的条目**既搜不到也补不上**
    （真实库 112 条 digested 全躺在这列）。健康检查时「digested 数 > 0 但向量数 = 0」
    就是本缺陷的指纹。
    """

    def test_update_embedding_does_not_demote_digested(self, store):
        it = store.add_item("已验证但没向量", title="已验证但没向量")
        store.mark_digested(it["id"])
        assert store.get_item(it["id"])["ingestion_status"] == "digested"

        assert store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m") is True

        # 补向量后仍是 digested；降级成 embedded 会被检索闸门踢出去（「补了反而搜不到」）
        assert store.get_item(it["id"])["ingestion_status"] == "digested"
        assert len(store.load_embeddable_items()) == 1

    def test_update_embedding_still_advances_raw_to_embedded(self, store):
        """单调 ≠ 不动：raw / summarized 仍要推进到 embedded（未验证不得进检索）。"""
        it = store.add_item("原始条目")
        store.update_embedding(it["id"], [0.9, 0.1, 0.0], model="m")
        assert store.get_item(it["id"])["ingestion_status"] == "embedded"
        assert store.load_embeddable_items() == []

    def test_embed_pending_picks_up_digested_without_vector(self, store, monkeypatch):
        """孤儿条目必须能被批量管道捡起来补向量，补完即可被语义检索到。"""
        import deskmate.memory.semantic_search as ss

        it = store.add_item("孤儿条目", title="孤儿条目")
        store.mark_digested(it["id"])  # 先进 digested，且此时没有向量
        monkeypatch.setattr(
            ss, "_call_embedding_api", lambda texts: [[0.9, 0.1, 0.0] for _ in texts]
        )

        searcher = ss.SemanticSearcher(store=store)
        result = searcher.embed_pending_items(limit=5)

        assert result["processed"] == 1
        assert result["embedded"] == 1
        assert store.get_item(it["id"])["ingestion_status"] == "digested"

        # 端到端：补完就能被语义检索命中（这正是「修改前永远搜不到」的那一步）
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])
        searcher.invalidate_cache()
        hits = searcher.semantic_search("孤儿", limit=5)
        assert [h["id"] for h in hits] == [it["id"]]
