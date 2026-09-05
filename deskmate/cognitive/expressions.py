"""他说过的话——统一表达源。

为什么需要它
------------
2026-09-05 巡检发现：**认知层和情感层的唯一真实输入源都是日记**。

- ``diary_extract`` / ``emotion_extract`` / ``emotion_sense`` 全只读 ``DiaryStore``；
- 访谈回答（``interview_qa``）只进认知画像，**不进情感时间线**；
- ``learned_history`` 227 条（他在聊天里明确说过的话）**两个层都没碰过**。

于是三块功能各自只认自己那一口井：情感时间线看不到访谈，gap 再生只扫日记，
认知画像吃不到聊天。本模块把「他说过的话」收成一个源，让三处共用同一口径。

三个源，可信度不同
------------------
1. ``diary``    日记——主动写的完整叙述，情绪最真，可信度最高；
2. ``interview`` 访谈——针对提问的直接回答，自述性质强，但受问题引导；
3. ``learned``  长期记忆——聊天里提炼出的一句话事实，**已结构化**，
   但可能是事务性的（"用户偏好深色模式"），需要调用方按 category 过滤。

所以每条表达都带 ``source``，调用方按自己需要选源、定优先级。
**不要不分来源地把三源混在一起判**——拿聊天片段去覆盖日记自述是本末倒置。

设计约束
--------
- **零 LLM**：只做读取与归并，理解交给调用方（emotion_sense / extractor）。
- **fail-open**：任一个源读不到就少一路，绝不抛异常拖垮调用方（tick 里跑的）。
- **按 id 幂等**：不同源同一天可能重复（日记写了、访谈也说了），
  id 带源前缀（``d:`` / ``i:`` / ``l:``）防碰撞，供调用方做已处理标记。
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 三个源的标识。调用方用它做过滤与优先级排序。
DIARY = "diary"
INTERVIEW = "interview"
LEARNED = "learned"

ALL_SOURCES = (DIARY, INTERVIEW, LEARNED)

# 可信度从高到低。判情绪时按这个顺序取，前者有结论就不再看后者。
# 日记 > 访谈 > 聊天片段：主动倾诉 > 被动回答 > 系统提炼。
TRUST_ORDER = (DIARY, INTERVIEW, LEARNED)

# learned_history 里偏事务性的分类：这些是工作流水，不是他的心情或主张。
# 情感判定默认排除它们（否则"第四章推进至1022段"会被当成情绪信号）。
TRANSACTIONAL_CATEGORIES = ("tool", "other")


def _day_of(ts: str) -> str:
    """时间戳 → 本地日期（YYYY-MM-DD）。读不出返回空串。"""
    if not ts:
        return ""
    raw = str(ts).strip()
    try:
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except (ValueError, TypeError):
        pass
    # learned_history 的 ts 是秒精度无时区，上面已能解析；
    # 真走到这里说明格式异常，取前 10 字符兜底
    return raw[:10] if len(raw) >= 10 else ""


def _cutoff(days: int) -> str:
    d = max(1, int(days))
    return (datetime.date.today() - datetime.timedelta(days=d - 1)).isoformat()


def _norm(text: str) -> str:
    return str(text or "").strip()


# ----------------------------------------------------------------------
# 三个源的读取（各自 fail-open）
# ----------------------------------------------------------------------
def _read_diary(base: Path, days: int) -> list[dict[str, Any]]:
    """日记正文。只收 author='me'——小桔写的是她的心情。"""
    try:
        from deskmate.growth.diary_store import DiaryStore

        page = DiaryStore(base_dir=base).get_posts(limit=200)
        items = page.get("items", []) if isinstance(page, dict) else []
    except Exception as e:
        logger.debug("表达源：读日记失败，跳过该源: %s", e)
        return []

    cutoff = _cutoff(days)
    out: list[dict[str, Any]] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        if str(p.get("author", "me")) != "me":
            continue
        text = _norm(p.get("content", ""))
        if not text:
            continue
        day = _day_of(str(p.get("createdAt", "")))
        if not day or day < cutoff:
            continue
        out.append({
            "id": f"d:{p.get('id', '')}",
            "day": day,
            "ts": str(p.get("createdAt", "")),
            "text": text,
            "source": DIARY,
            "category": "",
        })
    return out


def _read_interview(base: Path, days: int) -> list[dict[str, Any]]:
    """访谈回答（interview_qa.answer）。问题文本不收——那是我们问的，不是他说的。"""
    db = base / "deskmate.db"
    if not db.is_file():
        return []
    cutoff = _cutoff(days)
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT id, answer, created_at FROM interview_qa ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug("表达源：读访谈失败，跳过该源: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for rid, answer, created in rows:
        text = _norm(answer)
        if not text:
            continue
        day = _day_of(str(created or ""))
        if not day or day < cutoff:
            continue
        out.append({
            "id": f"i:{rid}",
            "day": day,
            "ts": str(created or ""),
            "text": text,
            "source": INTERVIEW,
            "category": "访谈",
        })
    return out


def _read_learned(base: Path, days: int,
                  categories: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """长期记忆（learned_history）。一句话事实，已结构化。

    ⚠ 直接查库而不用 ``LearnedStore.all_items()``：后者不返回 ``id``，
    而按 id 幂等是三源统一去重的前提（fact 可能被改写，id 不会）。

    默认只取非事务性分类（preference / habit）：``tool``/``other`` 占 189 条，
    多是"第四节推进至1022段"这类工作流水，拿来判心情纯属噪声。
    """
    db = base / "deskmate.db"
    if not db.is_file():
        return []
    cutoff = _cutoff(days)
    keep = list(categories) if categories is not None else [
        c for c in ("preference", "habit") if c not in TRANSACTIONAL_CATEGORIES
    ]
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            if keep:
                ph = ",".join("?" * len(keep))
                rows = conn.execute(
                    f"SELECT id, ts, fact, category FROM learned_history "
                    f"WHERE category IN ({ph}) AND status='active' ORDER BY ts, id",
                    tuple(keep),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, fact, category FROM learned_history "
                    "WHERE status='active' ORDER BY ts, id"
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug("表达源：读长期记忆失败，跳过该源: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for rid, ts, fact, cat in rows:
        text = _norm(fact)
        if not text:
            continue
        day = _day_of(str(ts or ""))
        if not day or day < cutoff:
            continue
        out.append({
            "id": f"l:{rid}",
            "day": day,
            "ts": str(ts or ""),
            "text": text,
            "source": LEARNED,
            "category": str(cat or ""),
        })
    return out


# ----------------------------------------------------------------------
# 对外主入口
# ----------------------------------------------------------------------
def collect(
    days: int = 14,
    base_dir: str | Path | None = None,
    sources: Iterable[str] = ALL_SOURCES,
    learned_categories: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """汇总最近 N 天他说过的话，按时间正序。

    Args:
        days: 回看天数（含今天）。
        sources: 要收哪些源，默认全收。传 ``(DIARY, INTERVIEW)`` 可排除聊天。
        learned_categories: 长期记忆只收这些分类；None = 只收非事务性的
            （preference / habit）。传 ``()`` 表示不过滤。

    Returns:
        [{"id", "day", "ts", "text", "source", "category"}, ...] 时间正序。
    """
    base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
    want = tuple(sources) or ALL_SOURCES

    # 每个源单独兜底：一路读挂了只少一路，绝不连累另外两路，
    # 也不许把异常抛给 tick（本模块是在心跳里跑的）。
    # 注意：源函数内部只捕获自己预知的异常，这里兜住剩下的。
    out: list[dict[str, Any]] = []
    readers = (
        (DIARY, lambda: _read_diary(base, days)),
        (INTERVIEW, lambda: _read_interview(base, days)),
        (LEARNED, lambda: _read_learned(base, days, categories=learned_categories)),
    )
    for name, read in readers:
        if name not in want:
            continue
        try:
            out.extend(read())
        except Exception as e:
            logger.debug("表达源：%s 读取失败，跳过该源: %s", name, e)

    out.sort(key=lambda e: (str(e.get("ts", "")), str(e.get("id", ""))))
    return out


def by_day(
    days: int = 14,
    base_dir: str | Path | None = None,
    sources: Iterable[str] = ALL_SOURCES,
    learned_categories: Optional[Iterable[str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """按本地日分组。同组内保持时间正序。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in collect(days=days, base_dir=base_dir, sources=sources,
                     learned_categories=learned_categories):
        grouped.setdefault(str(e["day"]), []).append(e)
    return grouped


def texts_by_day(
    days: int = 14,
    base_dir: str | Path | None = None,
    sources: Iterable[str] = ALL_SOURCES,
) -> dict[str, list[str]]:
    """只要文本，按天分组。给 emotion_timeline / emotion_sense 用。"""
    return {
        d: [str(e["text"]) for e in exprs]
        for d, exprs in by_day(days=days, base_dir=base_dir, sources=sources).items()
    }


def pick_by_trust(exprs: list[dict[str, Any]], sources: Iterable[str]) -> list[dict[str, Any]]:
    """按可信度取表达：sources 里靠前的源若当天有内容，就不再看后面的源。

    例：``pick_by_trust(exprs, (DIARY, INTERVIEW))`` —— 当天写了日记就只看日记，
    日记和访谈合并；没写日记才看访谈。**不是三源混着判**。
    """
    for src in sources:
        got = [e for e in exprs if e.get("source") == src]
        if got:
            return got
    return []
