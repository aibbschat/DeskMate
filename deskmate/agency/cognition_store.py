"""小桔认知库（cognition store）：把她「观世界」消化出的理解，沉淀为可跨会话检索的长期认知。

设计：
- 数据来源：理解层（understand_observations）每条理解 → ``add_understanding`` 写入，
  带 ``fact_keys`` 溯源（锚定原始世界观察，不编事实的根）。
- 向量检索：复用 ``semantic_search.embed_text`` + ``cosine_similarity``。embedding 不可用
  （无 key / 网络）时自动降级关键词匹配，fail-open，绝不阻断创作建议生成。
- 存储：``{base_dir}/agency/cognition_store.jsonl``，每行一条：
  ``{_k, date, fact_keys, understanding, facts, theme, embedding}``
- 去重：同 (fact_keys 串联 + understanding) 的 md5 不重复写（理解层每轮 tick 跑，防刷屏）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _store_path(base_dir) -> Path:
    return Path(base_dir) / "agency" / "cognition_store.jsonl"


def _key_of(fact_keys, understanding: str) -> str:
    joined = "|".join(fact_keys or []) + "::" + (understanding or "").strip()
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def add_understanding(
    base_dir,
    *,
    date: str,
    fact_keys: list[str],
    understanding: str,
    facts: list[str],
    theme: str = "",
) -> bool:
    """写入一条认知（理解层产出）。去重 + 尝试 embed（fail-open）。返回是否新写入。"""
    understanding = (understanding or "").strip()
    if not understanding:
        return False
    try:
        path = _store_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 去重：同一条理解不重复落库
        k = _key_of(fact_keys, understanding)
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            existing = []
        for line in existing:
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("_k") == k:
                    return False
            except Exception:
                pass
        # 尝试向量化（fail-open：embed 不可用则 embedding=None，检索时降级关键词）
        embedding = None
        try:
            from deskmate.memory.semantic_search import embed_text

            embedding = embed_text(understanding)
        except Exception as exc:  # noqa: BLE001
            logger.debug("认知库 embed 失败（检索将降级关键词）: %s", exc)
        entry = {
            "_k": k,
            "date": date,
            "fact_keys": list(fact_keys or []),
            "understanding": understanding,
            "facts": list(facts or []),
            "theme": theme,
            "embedding": embedding,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("认知库写入失败: %s", exc)
        return False


def _load(base_dir) -> list[dict]:
    try:
        path = _store_path(base_dir)
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("认知库加载失败: %s", exc)
        return []


def search(base_dir, query: str, *, top_k: int = 5, min_similarity: float = 0.25) -> list[dict]:
    """检索与 query（创作主题/概念）最相关的认知。

    返回 ``[{understanding, facts, fact_keys, date, theme, score}]``。
    embedding 可用 → 语义余弦相似度 top-k；不可用 → 降级关键词重叠匹配。
    """
    q = (query or "").strip()
    if not q:
        return []
    entries = _load(base_dir)
    if not entries:
        return []
    try:
        from deskmate.memory.semantic_search import embed_text, cosine_similarity
    except Exception:  # noqa: BLE001
        return _keyword_search(entries, q, top_k)

    query_vec = embed_text(q)
    if query_vec is None:
        return _keyword_search(entries, q, top_k)

    scored = []
    for e in entries:
        vec = e.get("embedding")
        if not vec:
            try:
                vec = embed_text(e.get("understanding", ""))
            except Exception:  # noqa: BLE001
                vec = None
        if not vec:
            continue
        sim = cosine_similarity(query_vec, vec)
        if sim >= min_similarity:
            scored.append((sim, e))
    scored.sort(key=lambda x: -x[0])
    return [_serialize(s, e) for s, e in scored[: max(1, min(int(top_k), 20))]]


def _serialize(score: float, e: dict) -> dict:
    return {
        "understanding": e.get("understanding", ""),
        "facts": list(e.get("facts", []) or []),
        "fact_keys": list(e.get("fact_keys", []) or []),
        "date": e.get("date", ""),
        "theme": e.get("theme", ""),
        "score": round(score, 4),
    }


def _keyword_search(entries: list[dict], query: str, top_k: int) -> list[dict]:
    """embedding 不可用时的降级：中文按字符 2-gram 重叠、英文按单词重叠粗匹配。"""
    q = query.strip().lower()
    # 英文/数字单词
    words = [w for w in q.replace("（", " ").replace("）", " ").split() if len(w) > 1]
    # 中文 2-gram
    import re

    han = "".join(re.findall(r"[一-鿿]", q))
    grams = {han[i : i + 2] for i in range(len(han) - 1)} if len(han) > 1 else set()
    scored = []
    for e in entries:
        text = (e.get("understanding", "") + " " + " ".join(e.get("facts", []) or [])).lower()
        hit_words = sum(1 for w in words if w in text)
        hit_grams = sum(1 for g in grams if g in text)
        score = hit_words + hit_grams * 0.5
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [_serialize(float(s), e) for s, e in scored[: max(1, min(int(top_k), 20))]]
