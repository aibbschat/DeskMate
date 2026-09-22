"""embedding 后端链 + 向量指纹护栏的单元测试（2026-09-22 接线）。

覆盖：
- `_backend_order()`：auto 本地优先 / 显式 local|api 不兜底 / 本地没装就只试 API
- 后端链记账：谁成功要记在 `last_backend()` / `active_model()` 上（写入侧靠它填字段）
- 本地失败 → 兜底 API；全后端失败 → 返回 None 且把**两边原因都记下来**
- `embed_pending_items` 用**实际**产出向量的模型名写入 `embedding_model`
- 向量指纹护栏：库存 3 维、新批 512 维 → 整批拒绝，不得混写
- 维度不一致的读取侧行为：全量不符 → 返回空 + 告警；部分不符 → 只跳过坏的那几条
- `local_embed` 的可选依赖契约：没装 = available() False + embed() 返回 None（不抛）

⚠ 这些用例**都不需要真的装 fastembed**：本地后端一律 mock，
契约是「装了就更好，没装也一字不变地能跑」。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path):
    from deskmate.memory.external_store import ExternalStore

    return ExternalStore(base_dir=tmp_path / ".deskmate")


@pytest.fixture
def ss():
    import deskmate.memory.semantic_search as mod

    return mod


# =====================================================================
# 后端顺序
# =====================================================================
class TestBackendOrder:
    def test_auto_prefers_local_when_available(self, ss, monkeypatch):
        monkeypatch.delenv("DESKMATE_EMBEDDING_BACKEND", raising=False)
        monkeypatch.setattr(ss.local_embed, "available", lambda: True)
        assert ss._backend_order() == ["local", "api"]

    def test_auto_falls_back_to_api_when_local_missing(self, ss, monkeypatch):
        monkeypatch.delenv("DESKMATE_EMBEDDING_BACKEND", raising=False)
        monkeypatch.setattr(ss.local_embed, "available", lambda: False)
        assert ss._backend_order() == ["api"]

    @pytest.mark.parametrize("mode", ["local", "api"])
    def test_explicit_mode_never_silently_falls_back(self, ss, monkeypatch, mode):
        """显式指定即用户意图：设了 api 就不许偷偷走本地（否则是新的不可观测问题）。"""
        monkeypatch.setenv("DESKMATE_EMBEDDING_BACKEND", mode)
        monkeypatch.setattr(ss.local_embed, "available", lambda: True)
        assert ss._backend_order() == [mode]

    def test_garbage_mode_treated_as_auto(self, ss, monkeypatch):
        monkeypatch.setenv("DESKMATE_EMBEDDING_BACKEND", "  BOGUS ")
        monkeypatch.setattr(ss.local_embed, "available", lambda: False)
        assert ss._backend_order() == ["api"]


# =====================================================================
# 后端链记账与兜底
# =====================================================================
class TestBackendChain:
    def test_local_success_records_backend_and_model(self, ss, monkeypatch):
        monkeypatch.delenv("DESKMATE_EMBEDDING_BACKEND", raising=False)
        monkeypatch.setattr(ss.local_embed, "available", lambda: True)
        monkeypatch.setattr(ss.local_embed, "embed", lambda texts: [[0.1] * 512 for _ in texts])
        monkeypatch.setattr(ss.local_embed, "model_name", lambda: "BAAI/bge-small-zh-v1.5")

        def _boom(texts):  # 本地成功就不该再碰 API
            raise AssertionError("本地已成功，不应回落到 API")

        monkeypatch.setattr(ss, "_call_embedding_api_openai", _boom)

        vecs = ss._call_embedding_api(["甲", "乙"])
        assert len(vecs) == 2 and len(vecs[0]) == 512
        assert ss.last_backend() == "local"
        assert ss.active_model() == "BAAI/bge-small-zh-v1.5"

    def test_local_failure_falls_through_to_api(self, ss, monkeypatch):
        monkeypatch.delenv("DESKMATE_EMBEDDING_BACKEND", raising=False)
        monkeypatch.setattr(ss.local_embed, "available", lambda: True)
        monkeypatch.setattr(ss.local_embed, "embed", lambda texts: None)
        monkeypatch.setattr(ss.local_embed, "last_error", lambda: "模型文件缺失")
        monkeypatch.setattr(ss, "_call_embedding_api_openai", lambda texts: [[0.2] * 1536 for _ in texts])

        vecs = ss._call_embedding_api(["甲"])
        assert len(vecs[0]) == 1536
        assert ss.last_backend() == "api"

    def test_all_backends_fail_records_both_reasons(self, ss, monkeypatch):
        monkeypatch.delenv("DESKMATE_EMBEDDING_BACKEND", raising=False)
        monkeypatch.setattr(ss.local_embed, "available", lambda: True)
        monkeypatch.setattr(ss.local_embed, "embed", lambda texts: None)
        monkeypatch.setattr(ss.local_embed, "last_error", lambda: "模型文件缺失")
        monkeypatch.setattr(ss, "_call_embedding_api_openai", lambda texts: None)

        assert ss._call_embedding_api(["甲"]) is None
        err = ss.last_embed_error() or ""
        # 两个后端的原因都要在——只报最后一个会掩盖「本地没装、API 又 404」的组合故障
        assert "模型文件缺失" in err
        assert "api(" in err

    def test_empty_texts_returns_empty_list(self, ss):
        """契约不变：空输入返回 []，不是 None（调用方据此区分「没内容」与「失败」）。"""
        assert ss._call_embedding_api([]) == []

    def test_backend_status_shape(self, ss, monkeypatch):
        monkeypatch.setattr(ss.local_embed, "available", lambda: False)
        st = ss.backend_status()
        for key in (
            "mode", "order", "local_available", "local_model", "local_cache_dir",
            "api_model", "last_backend", "last_model", "last_dim_skipped", "last_error",
        ):
            assert key in st, key
        assert st["local_available"] is False


# =====================================================================
# 写入侧：模型名如实落库 + 指纹护栏
# =====================================================================
class TestEmbedPendingWiring:
    def test_records_the_actual_model_used(self, ss, store, monkeypatch):
        """写入 embedding_model 的必须是**实际**产出这个向量的模型，不是配置里的默认值。"""
        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: [[0.1] * 512 for _ in texts])
        monkeypatch.setattr(ss, "_last_model", "BAAI/bge-small-zh-v1.5")

        it = store.add_item("待向量化")
        result = ss.SemanticSearcher(store=store).embed_pending_items(limit=5)

        assert result["embedded"] == 1
        assert store.get_item(it["id"])["embedding_model"] == "BAAI/bge-small-zh-v1.5"
        assert store.get_item(it["id"])["embedding_dim"] == 512

    def test_fingerprint_guard_blocks_mixed_write(self, ss, store, monkeypatch):
        """库存 3 维 + 新批 512 维 → 整批拒绝。混写会让语义检索静默失效。"""
        old = store.add_item("老向量", title="老向量")
        store.update_embedding(old["id"], [0.9, 0.1, 0.0], model="mock-3d")

        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: [[0.5] * 512 for _ in texts])
        monkeypatch.setattr(ss, "_last_model", "BAAI/bge-small-zh-v1.5")
        new = store.add_item("新条目", title="新条目")

        result = ss.SemanticSearcher(store=store).embed_pending_items(limit=5)

        assert result["embedded"] == 0
        assert result["failed"] >= 1
        assert "指纹" in result["reason"]
        # 不得写入：新条目仍然没有向量
        assert store.get_item(new["id"])["has_embedding"] is False

    def test_fingerprint_guard_allows_same_model(self, ss, store, monkeypatch):
        """同模型同维度 = 正常增量向量化，护栏不该误伤。"""
        old = store.add_item("老向量", title="老向量")
        store.update_embedding(old["id"], [0.9, 0.1, 0.0], model="mock-3d")

        monkeypatch.setattr(ss, "_call_embedding_api", lambda texts: [[0.9, 0.1, 0.0] for _ in texts])
        monkeypatch.setattr(ss, "_last_model", "mock-3d")

        new = store.add_item("新条目", title="新条目")
        result = ss.SemanticSearcher(store=store).embed_pending_items(limit=5)

        assert result["embedded"] == 1
        assert store.get_item(new["id"])["has_embedding"] is True


# =====================================================================
# 读取侧：维度不一致不许静默返回空
# =====================================================================
class TestDimensionMismatch:
    def _digested(self, store, title, vec, model="mock"):
        it = store.add_item(title + "内容", title=title)
        store.update_embedding(it["id"], vec, model=model)
        store.mark_digested(it["id"])
        return it

    def test_all_mismatched_returns_empty_and_warns(self, ss, store, monkeypatch):
        self._digested(store, "美联储加息", [0.9, 0.1, 0.0])
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.5] * 512)

        searcher = ss.SemanticSearcher(store=store)
        assert searcher.semantic_search("美联储") == []
        assert "维度" in (ss.last_embed_error() or "")

    def test_partial_mismatch_skips_bad_rows_only(self, ss, store, monkeypatch):
        good = self._digested(store, "美联储加息", [0.9, 0.1, 0.0])
        self._digested(store, "异源向量", [0.5] * 512, model="other-model")
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])

        searcher = ss.SemanticSearcher(store=store)
        results = searcher.semantic_search("美联储", limit=10)

        assert [r["id"] for r in results] == [good["id"]]
        assert ss.backend_status()["last_dim_skipped"] == 1

    def test_no_mismatch_no_warning(self, ss, store, monkeypatch):
        self._digested(store, "美联储加息", [0.9, 0.1, 0.0])
        monkeypatch.setattr(ss, "embed_text", lambda text: [0.9, 0.1, 0.0])
        monkeypatch.setattr(ss, "_last_dim_skipped", 0)

        searcher = ss.SemanticSearcher(store=store)
        assert len(searcher.semantic_search("美联储")) == 1
        assert ss.backend_status()["last_dim_skipped"] == 0


# =====================================================================
# local_embed 的可选依赖契约
# =====================================================================
class TestLocalEmbedOptional:
    def test_available_is_cheap_and_boolean(self):
        from deskmate.memory import local_embed

        # 只查包存在，不加载模型（加载会拉 100MB onnxruntime 进进程）
        assert isinstance(local_embed.available(), bool)

    def test_embed_returns_none_without_package(self, monkeypatch):
        from deskmate.memory import local_embed

        local_embed.reset_cache()
        monkeypatch.setattr(local_embed, "available", lambda: False)
        assert local_embed.embed(["甲"]) is None
        assert local_embed.last_error()
        local_embed.reset_cache()

    def test_cache_dir_honours_deskmate_dir_at_call_time(self, monkeypatch, tmp_path):
        """⚠ 必须**调用时**解析：模块级常量会让测试隔离失效、写进真实库（红线）。"""
        from deskmate.memory import local_embed

        monkeypatch.delenv("DESKMATE_EMBEDDING_MODEL_DIR", raising=False)
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "custom-desk"))
        assert local_embed.cache_dir() == tmp_path / "custom-desk" / "models"

    def test_explicit_model_dir_wins(self, monkeypatch, tmp_path):
        from deskmate.memory import local_embed

        monkeypatch.setenv("DESKMATE_EMBEDDING_MODEL_DIR", str(tmp_path / "explicit"))
        assert local_embed.cache_dir() == tmp_path / "explicit"
