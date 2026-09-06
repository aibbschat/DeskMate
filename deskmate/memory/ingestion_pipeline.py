"""外部信息摄入流水线 —— 从不可信区走到内部记忆的唯一合法通道。

萧哥 2026-09-06 拍板的边界链路：:

    外部信息 → 原始存储（不可信区）→ 筛选/去重/摘要 → 候选记忆 → 验证 → 写入内部记忆

在此之前「流水线」只有去重（content_hash）是真的，其余环节没有生产者，
而 world_ingestion 甚至直接把搜索结果写进 learned_history，绕过不可信区。
本模块把中间环节补齐，并让「写入内部记忆」成为**唯一**的升格出口。

分流规则（萧哥选「高可信自动，低可信问我」）：
- credibility ≥ trust_threshold（默认 0.7）→ 自动升格 digested，并写入内部记忆
- credibility < 阈值 → 停在候选区（summarized），攒一批走 L2 提议卡等人裁决

设计取向：
- **纯规则摘要，零 LLM**。与 diary_inspiration 同构——不为了「显得智能」
  去调模型，规则够用就规则。LLM 摘要可以后加，接口已经留好。
- **fail-open**：任何一步炸了都不阻断主链路，返回已完成的统计。
- **溯源标记**：写入内部记忆时强制带「（外部·来源类型）」后缀，
  避免外部信息在小桔眼里变得和萧哥亲口说的话无法区分。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .external_store import (
    DEFAULT_TRUST_THRESHOLD,
    STATUS_ARCHIVED,
    STATUS_DIGESTED,
    STATUS_RAW,
    STATUS_SUMMARIZED,
    ExternalStore,
)

logger = logging.getLogger(__name__)

# 摘要最大长度（与 world_ingestion 的 _MAX_FACT_LEN 保持同一量级）
_SUMMARY_MAX = 240
# 写入内部记忆的单条事实最大长度
_FACT_MAX = 240
# 内容短于此值视为噪声，直接筛掉
_MIN_CONTENT_LEN = 8
# 单轮流水线最多处理多少条
_DEFAULT_LIMIT = 100


def _rule_summary(item: dict) -> str:
    """规则摘要（零 LLM）：title 优先，没有就截 content。

    刻意不调模型——外部信息量可能很大，为每条都烧一次 token 不划算，
    而且标题+首段对「这条值不值得留」的判断已经够用。
    """
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    if title and content and content != title:
        # 正文里已经带了标题（很多媒体的 snippet 以标题开头）→ 别再拼一遍
        if title in content:
            text = content
        else:
            text = f"{title}：{content}"
    else:
        text = title or content
    text = " ".join(text.split())
    return text[:_SUMMARY_MAX]


def _fact_for_internal(item: dict) -> str:
    """把外部条目转成写进内部记忆的事实文案，带溯源标记。

    溯源标记不是装饰：没有它，外部信息进了 learned_history 之后
    就和小桔自己观察到的事实无从区分——那才是真正的「小桔会乱」。
    """
    summary = (item.get("summary") or "").strip() or _rule_summary(item)
    stype = (item.get("source_type") or "").strip() or "unknown"
    text = " ".join(summary.split())
    if len(text) > _FACT_MAX:
        text = text[:_FACT_MAX] + "…"
    return f"{text}（外部·{stype}）"


def screen_and_summarize(store: ExternalStore, *, limit: int = _DEFAULT_LIMIT) -> dict:
    """第 1 步：筛选 + 去重 + 摘要。raw → summarized（候选记忆）。

    - 内容过短（噪声）→ archived，不占候选区
    - 其余生成规则摘要，状态推进到 summarized = 候选记忆
    """
    result = {"scanned": 0, "summarized": 0, "screened_out": 0}
    try:
        candidates = [
            it for it in store.list_items(status=STATUS_RAW, limit=limit)
            if it.get("ingestion_status") == STATUS_RAW
        ]
    except Exception as e:
        logger.warning("ingestion_pipeline: 扫描 raw 条目失败: %s", e)
        return result

    result["scanned"] = len(candidates)
    for item in candidates:
        try:
            content = (item.get("content") or "").strip()
            if len(content) < _MIN_CONTENT_LEN:
                store.update_status(item["id"], STATUS_ARCHIVED)
                result["screened_out"] += 1
                continue
            summary = _rule_summary(item)
            if store.update_summary(item["id"], summary):
                result["summarized"] += 1
        except Exception as e:
            logger.warning("ingestion_pipeline: 筛选/摘要 %s 失败: %s", item.get("id"), e)
    return result


def verify_and_promote(
    store: ExternalStore,
    *,
    trust_threshold: float = DEFAULT_TRUST_THRESHOLD,
    base_dir: Optional[str | Path] = None,
    limit: int = _DEFAULT_LIMIT,
    auto_write_internal: bool = True,
) -> dict:
    """第 2 步：验证分流。候选记忆 →（达标）digested + 写入内部记忆 /（不达标）留候选区。

    返回：
    {
        "promoted": n,          # 自动升格并写入内部记忆的条数
        "awaiting_review": n,   # 低可信、等人工裁决的条数
        "awaiting_ids": [...],  # 供 L2 生成提议卡
        "written": n,           # 实际写入 learned_history 的条数（去重后）
    }
    """
    result: dict[str, Any] = {
        "promoted": 0, "awaiting_review": 0, "awaiting_ids": [], "written": 0,
    }
    try:
        candidates = [
            it for it in store.list_items(status=STATUS_SUMMARIZED, limit=limit)
            if it.get("ingestion_status") == STATUS_SUMMARIZED
        ]
    except Exception as e:
        logger.warning("ingestion_pipeline: 扫描候选条目失败: %s", e)
        return result

    trusted: list[dict] = []
    for item in candidates:
        cred = float(item.get("credibility") or 0.0)
        if cred >= trust_threshold:
            trusted.append(item)
        else:
            # 低可信：留在候选区，等人裁决，绝不自动升格
            result["awaiting_review"] += 1
            result["awaiting_ids"].append(item["id"])

    if not trusted:
        return result

    # 达标的一批：先写内部记忆，成功多少标记多少
    written = 0
    if auto_write_internal:
        written = _write_to_internal_memory(trusted, base_dir=base_dir)
        result["written"] = written

    for item in trusted:
        try:
            if store.mark_digested(item["id"]):
                result["promoted"] += 1
        except Exception as e:
            logger.warning("ingestion_pipeline: 升格 %s 失败: %s", item.get("id"), e)
    return result


def _write_to_internal_memory(
    items: list[dict],
    *,
    base_dir: Optional[str | Path] = None,
) -> int:
    """把已验证的外部条目写进内部记忆（learned_history）。

    这是外部信息进入内部记忆的**唯一出口**，且只在验证通过后才调用。
    base_dir 必须显式传——老代码 `LearnedStore()` 不带参会忽略 base_dir
    永远写真库，这个坑不能重踩。
    """
    if not items:
        return 0
    facts = [_fact_for_internal(it) for it in items]
    facts = [f for f in facts if f]
    if not facts:
        return 0
    try:
        from .learned_store import LearnedStore

        store = LearnedStore(base_dir=base_dir) if base_dir else LearnedStore()
        return int(store.append(facts) or 0)
    except Exception as e:
        logger.warning("ingestion_pipeline: 写入内部记忆失败: %s", e)
        return 0


def promote_approved(
    item_ids: list[str],
    *,
    base_dir: Optional[str | Path] = None,
    store: Optional[ExternalStore] = None,
) -> dict:
    """人工裁决「执行」：把候选区的指定条目升格为已验证并写入内部记忆。

    对应提议卡的 [执行] 按钮——低可信信息不会自动进内部记忆，
    但萧哥看过觉得可以，就走这条路径。
    """
    st = store or ExternalStore(base_dir=base_dir)
    result = {"approved": 0, "written": 0, "failed": 0}
    items: list[dict] = []
    for iid in item_ids or []:
        try:
            item = st.get_item(iid)
        except Exception:
            item = None
        if not item:
            result["failed"] += 1
            continue
        # 只处理候选区（summarized）——已验证/已归档的不重复处理
        if item.get("ingestion_status") not in (STATUS_SUMMARIZED, STATUS_RAW):
            result["failed"] += 1
            continue
        if item.get("ingestion_status") == STATUS_RAW:
            summary = _rule_summary(item)
            st.update_summary(iid, summary)
        items.append(item)

    if not items:
        return result

    result["written"] = _write_to_internal_memory(items, base_dir=base_dir)
    for item in items:
        try:
            if st.mark_digested(item["id"]):
                result["approved"] += 1
        except Exception as e:
            logger.warning("ingestion_pipeline: 人工升格 %s 失败: %s", item.get("id"), e)
            result["failed"] += 1
    return result


def run_ingestion_pipeline(
    base_dir: Optional[str | Path] = None,
    *,
    trust_threshold: float = DEFAULT_TRUST_THRESHOLD,
    limit: int = _DEFAULT_LIMIT,
    auto_write_internal: bool = True,
    store: Optional[ExternalStore] = None,
) -> dict:
    """跑完整一轮：筛选/去重/摘要 → 验证 → 写入内部记忆。

    返回合并统计（含 screen / verify 两段），供 L2 任务面板展示。
    """
    st = store or ExternalStore(base_dir=base_dir)
    out: dict[str, Any] = {
        "skipped": False, "scanned": 0, "screened_out": 0,
        "summarized": 0, "promoted": 0, "awaiting_review": 0,
        "awaiting_ids": [], "written": 0, "summary": "",
    }
    try:
        screen = screen_and_summarize(st, limit=limit)
        verify = verify_and_promote(
            st,
            trust_threshold=trust_threshold,
            base_dir=base_dir,
            limit=limit,
            auto_write_internal=auto_write_internal,
        )
        out.update({
            "scanned": screen.get("scanned", 0),
            "screened_out": screen.get("screened_out", 0),
            "summarized": screen.get("summarized", 0),
            "promoted": verify.get("promoted", 0),
            "awaiting_review": verify.get("awaiting_review", 0),
            "awaiting_ids": verify.get("awaiting_ids", []),
            "written": verify.get("written", 0),
        })
    except Exception as e:
        logger.warning("ingestion_pipeline: 流水线异常: %s", e)
        out["error"] = str(e)
        return out

    parts = []
    if out["promoted"]:
        parts.append(f"{out['promoted']} 条通过验证并入库（{out['written']} 条新事实）")
    if out["awaiting_review"]:
        parts.append(f"{out['awaiting_review']} 条可信度不足，等你裁决")
    if out["screened_out"]:
        parts.append(f"筛掉 {out['screened_out']} 条噪声")
    out["summary"] = "；".join(parts) or "本轮无可处理信息"
    return out
