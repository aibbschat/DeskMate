"""SemanticSearch — 外部信息的语义检索层（embedding + 余弦相似度 + 双路召回）。

与 external_store 配合：external_store 负责存储（含 embedding BLOB），本模块负责
生成向量、算相似度、做语义+关键词混合检索。

设计原则：
- **fail-open**：embedding 不可用（无 API key / 模型不支持 / 网络失败）时，
  语义检索返回空，混合检索自动降级为纯关键词检索，绝不阻断主流程。
- **批量向量化**：``embed_pending_items()`` 批量处理未向量化条目，分批调用
  embedding API（每批最多 64 条，避免 token 超限），失败条目跳过不阻塞。
- **可信度加权**：语义相似度 × 来源可信度，低可信度来源的信息不会仅凭语义
  相似就排到高位。
- **双路召回**：``hybrid_search()`` = 语义检索（alpha 权重）+ 关键词检索
  （1-alpha 权重），去重合并排序。embedding 不可用时 alpha 自动归零。

embedding 模型配置：
- 环境变量 ``DESKMATE_EMBEDDING_MODEL`` 指定模型名（默认 ``text-embedding-3-small``）。
- 复用 ``llm._resolve_provider()`` 的 api_key / base_url，与主对话 provider 同体系。
- 若当前 provider 不支持 /embeddings 端点，调用失败后静默降级。
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Optional

from .external_store import ExternalStore, STATUS_RAW, STATUS_SUMMARIZED

logger = logging.getLogger(__name__)

# embedding 模型：环境变量可覆盖，默认用 OpenAI 兼容的小模型
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# 批量 embedding 每批最大条数（OpenAI /embeddings 单请求上限 2048，留余量）
_EMBED_BATCH_SIZE = 64
# 单条文本最大字符数（超长截断，避免 token 超限）
_EMBED_MAX_CHARS = 8000
# 混合检索中语义检索的权重（0=纯关键词，1=纯语义）
_DEFAULT_ALPHA = 0.7
# ⚠ 此处原有 `_SEMANTIC_CANDIDATE_LIMIT = 200`（「取 top-N 避免排序开销」），
# 但全仓库零引用：`semantic_search()` 实际遍历**全部**已向量化条目算余弦，
# 无任何候选截断。该常量既没生效又误导人以为有 top-200 优化，2026-09-22 删除。
# 若将来 external_items 量级涨到万级、纯 Python 余弦变慢，再引入真正的
# 候选预筛（或向量索引），而不是留一个不接线的数字。

# embedding 失败的可观测性（2026-09-22）：失败**是设计内的** fail-open，
# 但「静默」不是——此前只打 logger.debug，默认级别下等于无人知晓，
# 于是「195 条外部信息 0 条有向量、语义检索永远退化为关键词」这个事实
# 长期不可见。同一类错误节流打点，并保留最近一次原因供诊断查询。
_EMBED_ERR_LOG_INTERVAL = 600.0  # 秒；同一类失败最多每 10 分钟刷一条 warning
_last_error: Optional[str] = None
_last_error_logged_at: float = 0.0


def _note_embed_failure(reason: str) -> None:
    """记录一次 embedding 失败并**节流告警**（不再是静默的 debug）。"""
    global _last_error, _last_error_logged_at
    _last_error = reason
    now = time.time()
    if now - _last_error_logged_at >= _EMBED_ERR_LOG_INTERVAL:
        _last_error_logged_at = now
        logger.warning(
            "semantic_search: embedding 不可用 → 语义检索降级为纯关键词（原因：%s）。"
            "当前模型名=%s；若需语义通道，请确认 provider 支持 /embeddings 且该模型可用"
            "（可用 DESKMATE_EMBEDDING_MODEL 覆盖）。",
            reason,
            _embedding_model(),
        )


def last_embed_error() -> Optional[str]:
    """最近一次 embedding 失败原因（None = 尚未失败过）。供诊断 / MCP 查询。"""
    return _last_error


def _embedding_model() -> str:
    return os.environ.get("DESKMATE_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)


def _embed_text_preprocess(text: str) -> str:
    """embedding 前的文本预处理：截断超长内容，取 title + content + summary 拼接。"""
    text = (text or "").strip()
    if len(text) > _EMBED_MAX_CHARS:
        # 保留前 60% + 后 40%（首尾信息密度通常更高）
        head = int(_EMBED_MAX_CHARS * 0.6)
        tail = _EMBED_MAX_CHARS - head
        text = text[:head] + "\n...\n" + text[-tail:]
    return text


def _call_embedding_api(texts: list[str]) -> Optional[list[list[float]]]:
    """调用 OpenAI 兼容 /embeddings 端点，批量生成向量。

    返回向量列表；失败（无 key / 模型不支持 / 网络错误）返回 None。
    与 llm.py 共享 provider 解析体系。
    """
    if not texts:
        return []
    try:
        from .llm import _resolve_provider
        from openai import OpenAI

        api_key, base_url, _ = _resolve_provider()
        if not api_key:
            _note_embed_failure("无 LLM provider API key")
            return None

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
        resp = client.embeddings.create(
            model=_embedding_model(),
            input=texts,
        )
        # 按 input 顺序返回向量
        vectors = [list(d.embedding) for d in resp.data]
        # 校验返回数量
        if len(vectors) != len(texts):
            logger.warning(
                "semantic_search: embedding 返回数量不匹配（请求 %d，返回 %d）",
                len(texts), len(vectors),
            )
        return vectors
    except Exception as exc:
        # 不再静默：fail-open 是设计（不阻断主流程），但**失败必须可见**，
        # 否则「语义通道其实一直没开」会永远无人知晓（见 _note_embed_failure）。
        _note_embed_failure(f"{type(exc).__name__}: {exc}")
        return None


def embed_text(text: str) -> Optional[list[float]]:
    """单条文本生成向量。失败返回 None（fail-open）。"""
    vectors = _call_embedding_api([_embed_text_preprocess(text)])
    if vectors is None:
        return None
    return vectors[0] if vectors else None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。纯 Python 实现，无 numpy 依赖。维度不一致返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class SemanticSearcher:
    """语义检索器：绑定一个 ExternalStore，提供语义检索 + 混合检索 + 批量向量化。"""

    def __init__(self, store: Optional[ExternalStore] = None) -> None:
        self.store = store or ExternalStore()
        # 向量缓存：{item_id: 完整条目 dict}，避免重复从 DB 加载
        self._vec_cache: dict[str, dict] = {}
        self._vec_cache_loaded_at: float = 0.0
        # 缓存有效期（秒）：外部信息写入频繁，缓存不宜过长
        self._cache_ttl = 300.0
        # 建缓存时的闸门档位；闸门一变就必须重查，不能复用
        self._cache_trusted_only: bool = True

    # ------------------------------------------------------------------
    # 批量向量化管道
    # ------------------------------------------------------------------
    def embed_pending_items(self, *, limit: int = 100, batch_size: int = _EMBED_BATCH_SIZE) -> dict:
        """批量为未向量化的条目生成 embedding 并写入 DB。

        选取 ingestion_status in (raw, summarized) 且无 embedding 的条目，
        分批调用 embedding API，成功后推进状态到 embedded。

        返回 {"processed": n, "embedded": n, "failed": n, "skipped": n}。
        embedding 不可用时 processed=0，调用方应静默降级。
        """
        # 先取待处理条目（无向量、未归档、不过期）
        pending = self.store.list_items(
            status=None,  # 不过滤状态，下面手动筛
            limit=max(1, min(int(limit), 500)),
        )
        pending = [
            it for it in pending
            if not it["has_embedding"]
            and it["ingestion_status"] in (STATUS_RAW, STATUS_SUMMARIZED)
        ][:limit]

        if not pending:
            return {"processed": 0, "embedded": 0, "failed": 0, "skipped": 0, "reason": "无待向量化条目"}

        result = {"processed": len(pending), "embedded": 0, "failed": 0, "skipped": 0}

        # 分批处理
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            texts = [
                _embed_text_preprocess(
                    (it["title"] + "\n" if it["title"] else "") + it["content"]
                )
                for it in batch
            ]
            vectors = _call_embedding_api(texts)
            if vectors is None:
                # embedding 不可用，整批失败，不再重试
                result["failed"] += len(batch)
                result["reason"] = "embedding API 不可用"
                break

            for it, vec in zip(batch, vectors):
                if not vec:
                    result["failed"] += 1
                    continue
                ok = self.store.update_embedding(it["id"], vec, model=_embedding_model())
                if ok:
                    result["embedded"] += 1
                else:
                    result["failed"] += 1

            # 批次间短暂休息，避免 API 限流
            if i + batch_size < len(pending):
                time.sleep(0.5)

        # 写了新向量就整块失效缓存——让它下次从 DB 重新加载完整条目，
        # 而不是往缓存里塞半截数据（老版本塞 (vec, fetched_at) 会丢字段）。
        if result["embedded"] > 0:
            self.invalidate_cache()

        return result

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------
    def semantic_search(
        self,
        query: str,
        *,
        limit: int = 20,
        min_similarity: float = 0.3,
        credibility_weight: float = 0.3,
        trusted_only: bool = True,
    ) -> list[dict]:
        """语义检索：生成 query 向量，与所有已向量化条目算余弦相似度，取 top-k。

        Args:
            query: 查询文本
            limit: 返回条数
            min_similarity: 最低相似度阈值（低于此值的结果丢弃）
            credibility_weight: 可信度在最终分数中的权重（0=纯语义，1=纯可信度）
                最终分数 = similarity * (1 - credibility_weight) + credibility * credibility_weight
            trusted_only: 可信闸门，默认 True = 只检索 ``digested``（已验证）条目。
                传 False 会连未验证的原始外部信息一起召回——仅供调试/人工翻查，
                **绝不能用于给小桔决策**。

        返回 [{id, title, content, summary, similarity, score, credibility, fetched_at, category, source_type}]。
        embedding 不可用时返回空列表（调用方应降级到关键词检索）。
        """
        query = (query or "").strip()
        if not query:
            return []

        # 生成 query 向量
        query_vec = embed_text(query)
        if query_vec is None:
            return []

        # 加载已验证的向量条目（带缓存）
        items = self._load_embeddable_cached(trusted_only=trusted_only)
        if not items:
            return []

        # 算相似度 + 可信度加权
        scored = []
        for it in items:
            sim = cosine_similarity(query_vec, it["embedding"])
            if sim < min_similarity:
                continue
            cred = float(it.get("credibility", 0.5))
            score = sim * (1.0 - credibility_weight) + cred * credibility_weight
            scored.append({
                "id": it["id"],
                "title": it["title"],
                "content": it["content"],
                "summary": it["summary"],
                "similarity": round(sim, 4),
                "score": round(score, 4),
                "credibility": cred,
                "fetched_at": it["fetched_at"],
                "category": it["category"],
                "source_type": it.get("source_type", ""),
                "search_type": "semantic",
            })

        # 按分数降序，取 top-k
        scored.sort(key=lambda x: -x["score"])
        return scored[: max(1, min(int(limit), 100))]

    # ------------------------------------------------------------------
    # 关键词检索（委托给 external_store，加分数归一化）
    # ------------------------------------------------------------------
    def keyword_search(self, query: str, *, limit: int = 20, trusted_only: bool = True) -> list[dict]:
        """关键词检索（委托 external_store.search_items_keyword），加命中分数。

        分数 = 命中关键词数 / 总关键词数（0-1），用于混合检索的归一化。

        trusted_only 默认 True = 可信闸门，只返回已验证条目。
        """
        raw = self.store.search_items_keyword(query, limit=limit, trusted_only=trusted_only)
        if not raw:
            return []
        import re
        keywords = [kw for kw in re.split(r"\s+", query.strip()) if kw]
        total_kw = max(1, len(keywords))
        out = []
        for it in raw:
            searchable = (it["title"] + " " + it["content"] + " " + it["summary"]).lower()
            hits = sum(1 for kw in keywords if kw.lower() in searchable)
            score = round(hits / total_kw, 4)
            out.append({
                "id": it["id"],
                "title": it["title"],
                "content": it["content"],
                "summary": it["summary"],
                "similarity": 0.0,  # 关键词检索无语义相似度
                "score": score,
                "credibility": it["credibility"],
                "fetched_at": it["fetched_at"],
                "category": it["category"],
                "source_type": it["source_type"],
                "search_type": "keyword",
            })
        return out

    # ------------------------------------------------------------------
    # 混合检索（语义 + 关键词双路召回，去重合并）
    # ------------------------------------------------------------------
    def hybrid_search(
        self,
        query: str,
        *,
        limit: int = 20,
        alpha: float = _DEFAULT_ALPHA,
        trusted_only: bool = True,
    ) -> dict:
        """混合检索：语义检索（alpha）+ 关键词检索（1-alpha），去重合并排序。

        embedding 不可用时，语义检索返回空，alpha 自动归零（纯关键词）。
        同一条目在两路都命中时，分数 = alpha * semantic_score + (1-alpha) * keyword_score，
        且标记 search_type="hybrid"。

        trusted_only 默认 True——**可信闸门默认关闭状态**，
        未验证的外部信息不会出现在返回里。

        Returns:
            {"results": [...], "semantic_count": n, "keyword_count": n, "alpha": float,
             "embedding_available": bool}
        """
        query = (query or "").strip()
        if not query:
            return {"results": [], "semantic_count": 0, "keyword_count": 0,
                    "alpha": 0.0, "embedding_available": False}

        sem_results = self.semantic_search(query, limit=limit * 2, trusted_only=trusted_only)
        kw_results = self.keyword_search(query, limit=limit * 2, trusted_only=trusted_only)

        embedding_available = len(sem_results) > 0
        effective_alpha = alpha if embedding_available else 0.0

        # 按 id 合并
        merged: dict[str, dict] = {}
        for r in sem_results:
            merged[r["id"]] = dict(r)
            merged[r["id"]]["_sem_score"] = r["score"]
            merged[r["id"]]["_kw_score"] = 0.0

        for r in kw_results:
            if r["id"] in merged:
                merged[r["id"]]["_kw_score"] = r["score"]
                merged[r["id"]]["search_type"] = "hybrid"
            else:
                merged[r["id"]] = dict(r)
                merged[r["id"]]["_sem_score"] = 0.0
                merged[r["id"]]["_kw_score"] = r["score"]

        # 计算最终分数
        results = []
        for rid, r in merged.items():
            sem_s = r.pop("_sem_score", 0.0)
            kw_s = r.pop("_kw_score", 0.0)
            final_score = round(effective_alpha * sem_s + (1.0 - effective_alpha) * kw_s, 4)
            r["score"] = final_score
            results.append(r)

        # 按最终分数降序
        results.sort(key=lambda x: -x["score"])
        results = results[: max(1, min(int(limit), 100))]

        return {
            "results": results,
            "semantic_count": len(sem_results),
            "keyword_count": len(kw_results),
            "alpha": effective_alpha,
            "embedding_available": embedding_available,
        }

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------
    def _load_embeddable_cached(self, trusted_only: bool = True) -> list[dict]:
        """加载有向量的条目（带 TTL 缓存）。外部信息写入频繁，缓存 5 分钟。

        缓存存的是**完整条目 dict**，不是只存向量。

        ⚠ 这里踩过坑：早期版本缓存只存 (vector, fetched_at)，命中分支重建 dict 时
        title/content/summary 全填空串、credibility 全填 0.5，导致同一实例 5 分钟内
        第 2 次检索拿到一堆空壳，可信度加权直接失效。测试每个用例新建实例只查一次，
        永远走不到缓存分支，所以没被发现。现在缓存整条记录，杜绝字段丢失。
        """
        now = time.time()
        fresh = (now - self._vec_cache_loaded_at) < self._cache_ttl
        # 闸门开关变了也不能复用旧缓存，否则会绕过 trusted_only
        same_gate = self._cache_trusted_only == trusted_only
        if fresh and self._vec_cache and same_gate:
            return list(self._vec_cache.values())

        items = self.store.load_embeddable_items(trusted_only=trusted_only)
        self._vec_cache = {it["id"]: it for it in items}
        self._vec_cache_loaded_at = now
        self._cache_trusted_only = trusted_only
        return items

    def invalidate_cache(self) -> None:
        """手动失效向量缓存（写入新向量后调用）。"""
        self._vec_cache.clear()
        self._vec_cache_loaded_at = 0.0
        self._cache_trusted_only = True


# ---------------------------------------------------------------------------
# 便捷函数（无状态，每次新建 searcher）
# ---------------------------------------------------------------------------
def search_external(
    query: str,
    *,
    limit: int = 20,
    alpha: float = _DEFAULT_ALPHA,
    store: Optional[ExternalStore] = None,
) -> dict:
    """便捷函数：对外部信息做混合检索。

    用法：``from deskmate.memory.semantic_search import search_external``
    ``result = search_external("美联储加息对科技股的影响")``
    """
    searcher = SemanticSearcher(store=store)
    return searcher.hybrid_search(query, limit=limit, alpha=alpha)


def embed_and_store(
    content: str,
    *,
    title: str = "",
    source_type: str = "manual",
    store: Optional[ExternalStore] = None,
    **kwargs,
) -> Optional[dict]:
    """便捷函数：写入一条外部信息并立即生成向量。

    写入成功后调用 embed_text 生成向量并更新到 DB。
    embedding 不可用时，条目仍以 raw 状态写入（后续可由批量管道补向量化）。

    返回条目 dict（含 has_embedding 字段）；失败返回 None。
    """
    st = store or ExternalStore()
    item = st.add_item(content, title=title, source_type=source_type, **kwargs)
    if not item:
        return None
    vec = embed_text((title + "\n" if title else "") + content)
    if vec:
        st.update_embedding(item["id"], vec, model=_embedding_model())
        item = st.get_item(item["id"])
    return item
