"""台账相关性打分（从 ledger.py 机械抽取，PR16/#33）。

域：字面 bigram 主闸 + BGE 语义高置信补充 + BGE 挂死熔断护栏。
行为与抽取前逐字节一致；所有可被测试 monkeypatch 的全局（``_BGE_TIMEOUT_S`` /
``_BGE_EMBED_DISABLED`` / ``_bge_available``）仍经父模块 ``ledger`` 命名空间解析，
故既有 ``monkeypatch.setattr(ledger, ...)`` 用例无需改动即生效。

``ledger.py`` 通过 re-export 保持 ``from deskmate.proactivity.ledger import X`` 兼容。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _L():
    """懒取父模块——避免顶层循环导入，且让 monkeypatch(ledger.X) 对内部调用生效。"""
    from deskmate.proactivity import ledger
    return ledger


# 熔断态对象（进程级）：由 ledger re-export，测试读 ledger._bge_breaker 即此对象。
_bge_breaker = threading.Event()   # set = 已熔断（本进程不再试 BGE）

# BGE 语义相似度高置信门槛（P3-b 校准，实测）：BGE 中文无关句地板约 0.25–0.31、同书但不同线
# 约 0.45、真相关（共享章/人物锚点或强改写）≥0.5。低于此值视为「语义未达可靠相关」，仅取字面
# bigram 分，避免 BGE 地板把无关项误判为相关（设计 §11.4② 反骚扰底线：宁可少递不可乱递）。
BGE_SEMANTIC_PASS = 0.5


def _bigrams(text: str) -> set:
    """字符二元组集合——中文无分词依赖的轻量语义近似（stdlib only），作为 BGE 不可用时的兜底。"""
    t = "".join((text or "").split())
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _relevance_score_bigram(item: dict, context: str) -> float:
    """字面重叠兜底（BGE 不可用时）：bigram 交集 / sqrt(项长)。"""
    ctx = _bigrams(context)
    if not ctx:
        return 0.0
    blob = " ".join(
        str(item.get(k) or "") for k in ("kind", "content", "evidence", "understanding")
    )
    bi = _bigrams(blob)
    if not bi:
        return 0.0
    return len(bi & ctx) / (len(bi) ** 0.5)


def _bge_available() -> bool:
    """本地 BGE（fastembed）是否已可用；仅检查包存在，不加载模型。"""
    try:
        from deskmate.memory import local_embed
        return bool(local_embed.available())
    except Exception:  # noqa: BLE001
        return False


def _bge_breaker_tripped() -> bool:
    """熔断是否已触发（含环境变量一键关）。"""
    return _L()._BGE_EMBED_DISABLED or _bge_breaker.is_set()


def _bge_embed(texts: list) -> Optional[list]:
    """批量 embed；异常/不可用/超时/熔断返回 None。首次调用会加载模型（~185MB）。

    超时：子线程跑 embed，超 _BGE_TIMEOUT_S 未归 → 熔断（本进程不再试），返回 None。
    防的是「每 5min tick 泄漏一个挂死线程」的慢性病——bigram 主闸仍在，行为不退化。
    """
    if _bge_breaker_tripped():
        return None
    try:
        from deskmate.memory import local_embed
        if not local_embed.available():
            return None
    except Exception:  # noqa: BLE001
        return None

    box: dict = {}

    def _run() -> None:
        try:
            from deskmate.memory import local_embed
            box["emb"] = local_embed.embed(texts)
        except Exception:  # noqa: BLE001
            box["emb"] = None

    t = threading.Thread(target=_run, daemon=True, name="bge-embed")
    t.start()
    t.join(_L()._BGE_TIMEOUT_S)
    if t.is_alive():
        _bge_breaker.set()
        logger.warning(
            "BGE embed 超时（%.0fs），本进程熔断 BGE，回落 bigram 字面兜底",
            _L()._BGE_TIMEOUT_S,
        )
        return None
    return box.get("emb")


def _bge_reset_for_test() -> None:
    """清熔断态（**仅供测试**：一个用例熔断后不许污染同进程其他用例）。"""
    _bge_breaker.clear()


def bge_breaker_status() -> dict:
    """熔断状态诊断（供 MCP/排查）。"""
    return {
        "disabled_by_env": _L()._BGE_EMBED_DISABLED,
        "tripped": _bge_breaker.is_set(),
        "timeout_s": _L()._BGE_TIMEOUT_S,
    }


def _cosine(a, b) -> float:
    try:
        from deskmate.memory.semantic_search import cosine_similarity
        return float(cosine_similarity(a, b))
    except Exception:  # noqa: BLE001
        return 0.0


def _relevance_score(item: dict, context: str) -> float:
    """台账项与会话上下文的相关性（0 = 不相关、不递）。

    P3-b：字面 bigram 重叠为**主闸**（设计 §11.4② 证据随行——章/人物名等锚点精确匹配，
    稳定可解释、不受 embedding 版本漂移影响）；BGE 语义相似度仅在**高置信**（≥
    BGE_SEMANTIC_PASS）时补充，捕捉「共享语义但不共享字面」的改写/同义场景。两者取较大值。
    fastembed 未装 / 异常时回落纯 bigram，行为不退化。
    """
    blob = " ".join(
        str(item.get(k) or "") for k in ("kind", "content", "evidence", "understanding")
    )
    ctx = (context or "").strip()
    if not (blob.strip() and ctx):
        return 0.0
    bg = _relevance_score_bigram(item, context)
    if _L()._bge_available():
        emb = _bge_embed([blob, ctx])
        if emb and len(emb) == 2:
            sim = _cosine(emb[0], emb[1])
            if sim >= BGE_SEMANTIC_PASS:
                # 高置信语义相关：取 max（字面有重叠以字面为主，纯语义强相关以语义为主）
                return max(bg, sim)
            # 语义未达高置信：仅取字面分（BGE 地板不污染相关性判定）
            return bg
    return bg