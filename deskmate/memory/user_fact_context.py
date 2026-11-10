"""把小桔「学到的关于用户的事实」+ 近期日记软规律，回灌进对话/主动推理上下文。

背景（2026-10-08 萧哥报）：小桔「库里知道、回答/理解时拿不到」——
- ``learned_history`` 同时承载两类事实：外部世界观察（带「（外部·」溯源标，由
  ``world_context.build_world_context`` 回灌）与**用户口述事实**（萧哥亲口说的偏好/
  习惯/著作/进度，如「《姑射图》是《国子监秘闻录》第三章」「节假日不写稿」）。
  后者此前两条回灌路径都没接：``build_world_context`` 显式 ``is_world_fact`` 排除，
  ``understanding._gather_memory`` 只搜 ``history/*.db`` 不碰 ``learned_history``。
- 日记（``<base>/diary/YYYY-MM-DD.md``）里的第一人称软规律（如「国庆假期不写就不写」）
  同样没进主动推理上下文，导致「六天没动笔」卡片判断成「是不是卡住了」而非「假期休息」。

本模块补这两条缺口：取活跃、非外部的用户事实 + 近期日记相关片段，按 query 召回。
fail-open：任何异常返回空串，绝不阻断主流程（聊天/主动 tick 都比记忆重要）。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

_DAIRY_DAYS = 7
_DAIRY_SNIPPET_MAX = 80   # 日记片段上限：过长的基本是新闻/观察摘抄，非她自己的笔记
_FETCH_FACTS_CAP = 2000  # 直接全量召回活跃用户事实的上限（库只增不减，但量级可控）
_FACT_CONTEXT_MAX_CHARS = 4000  # 注入文本总字数上限（此前整块灌、无上限）
_REFERENCE_LIMIT = 3      # 参考层（非正文来源）最多给几条，避免喧宾夺主

# 2026-11-10 萧哥终裁：已写正文事实是唯一事实基准。此前块头写「不是她猜的；
# 优先据此作答」，等于给长期记忆发了事实许可证——记忆里六条互斥裁定并存时，
# 她只能挑一句当真讲。改为「只是线索」+ 冲突须明说，把判断交回正文与工具结果。
_FACT_DISCIPLINE = (
    "【事实纪律】已写正文逐字直引才是唯一基准；推理、设定、大纲、简报、讨论结论"
    "与正文冲突时一律以正文为准。记忆和正文对不上，要直说哪里对不上，"
    "不许据记忆下断言；拿不准就说不确定。"
)
# 日记里混着她读新闻后的摘抄（长、带信源标），这些不是「关于用户的软规律」，召回时剔除。
_DIARY_NEWS_MARKERS = (
    "新闻", "早8点", "夜览", "新华", "川观", "记者从", "预计发送", "早啊",
    "央视", "人民网", "澎湃", "日报", "瞭望", "客户端", "新闻来了",
)


def _is_user_fact(fact: str) -> bool:
    """一条 learned_history 事实是否「用户口述事实」（非外部世界观察）。"""
    from .world_context import is_world_fact

    t = (fact or "").strip()
    if not t:
        return False
    return not is_world_fact(t)


def _query_terms(query: str) -> list[str]:
    """从 query 抽取召回词：书名/引号内专名（高权）+ 中文 2-gram。"""
    q = (query or "").strip()
    if not q:
        return []
    names = re.findall(r"[《「『\"“](.+?)[》」』\"”]", q)
    clean = re.sub(r"[^\w一-鿿]", "", q)
    grams = [clean[i:i + 2] for i in range(len(clean) - 1)] if len(clean) >= 2 else []
    return [n for n in names] + grams


def _active_user_facts(base_dir, limit: int = _FETCH_FACTS_CAP) -> list[tuple[str, str]]:
    """取**活跃、非外部**的用户事实，返回 ``[(fact, source)]``。

    ``ORDER BY ts DESC, id DESC``（2026-11-10 补）：此前无 ORDER BY，SQLite 默认
    rowid 升序 ⇒ 无 query 时取到的其实是**最老**的 10 条，旧错条长期驻留。
    ``source`` 一并取出供效力分层，避免把设定/推理当成用户口述事实。
    """
    try:
        from .learned_store import LearnedStore, _now_iso

        db = LearnedStore(base_dir=base_dir).db_path
        con = sqlite3.connect(str(db))
        try:
            con.execute("PRAGMA journal_mode=WAL")
            rows = con.execute(
                "SELECT fact, source FROM learned_history WHERE status='active' "
                "AND (valid_until IS NULL OR valid_until >= ?) "
                "ORDER BY ts DESC, id DESC "
                f"LIMIT {int(limit)}",
                (_now_iso(),),
            ).fetchall()
        finally:
            con.close()
        return [(r[0], r[1]) for r in rows if _is_user_fact(r[0])]
    except Exception:
        return []


def _read_recent_diary(base_dir, days: int = _DAIRY_DAYS) -> list[tuple[str, str]]:
    """读最近 days 天日记正文（文件名 ``YYYY-MM-DD.md``），返回 ``[(date, text)]``。

    fail-open：目录缺失/读取异常返回 []。
    """
    try:
        import datetime

        root = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        diary_dir = root / "diary"
        if not diary_dir.is_dir():
            return []
        today = datetime.date.today()
        out = []
        for p in sorted(diary_dir.glob("*.md")):
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.md$", p.name)
            if not m:
                continue
            try:
                d = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if (today - d).days > days:
                continue
            try:
                out.append((m.group(1), p.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
        return out
    except Exception:
        return []


def _diary_snippets(text: str, terms: list[str], max_chars: int = _DAIRY_SNIPPET_MAX) -> list[str]:
    """从一篇日记里挑出与 terms 相关的行片段（按行匹配，命中即取该行）。

    剔除新闻/观察摘抄（过长或带信源标），只留她自己的短笔记，避免把世界新闻
    误当成「关于用户的软规律」灌进理解/对话。
    """
    if not terms:
        return []
    picked: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) > max_chars:
            continue
        if any(m in line for m in _DIARY_NEWS_MARKERS):
            continue
        if any(t in line for t in terms):
            snip = re.sub(r"^[#\-\*\s]+", "", line)
            if snip:
                picked.append(snip[:max_chars])
    seen = set()
    uniq = []
    for s in picked:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def build_user_fact_context(
    base_dir: str | Path | None = None,
    query: str | None = None,
    limit: int = 10,
) -> str:
    """取活跃、非外部的用户事实 + 近期日记相关片段，格式化为注入文本。

    Args:
        query: 当前对话/卡片主题；传了就按关键词召回最相关，不传取最近。
        limit: 最多条数（用户事实）。
    Returns:
        「## 你跟我说过的……」+（可选）「## 你近期的日常……」段落；无则空串。
    """
    try:
        from .learned_store import is_non_fact_source, source_authority

        terms = _query_terms(query) if query else []
        facts = _active_user_facts(base_dir)
        if not facts and not terms:
            return ""

        # 效力分层：正文/口述/裁定进主块；推理/设定/大纲/简报/进度/外部只进参考层。
        # 参考层默认不出现在主块，避免被当成「已经写了的事实」。
        primary = [(f, s) for f, s in facts if not is_non_fact_source(s)]
        reference = [(f, s) for f, s in facts if is_non_fact_source(s)]

        chosen: list[str] = []
        ref_chosen: list[str] = []
        if terms:
            scored = []
            for f, s in primary:
                score = sum(2 if t in f else 0 for t in terms)
                if score > 0:
                    scored.append((score, source_authority(s), f))
            # 同分时正文优先（正文是唯一事实基准）
            scored.sort(key=lambda x: (-x[0], -x[1]))
            chosen = [f for _, _, f in scored[:limit]]

            ref_scored = [
                (sum(2 if t in f else 0 for t in terms), f) for f, _s in reference
            ]
            ref_chosen = [
                f for score, f in sorted(ref_scored, key=lambda x: -x[0])
                if score > 0
            ][:_REFERENCE_LIMIT]
        else:
            # 稳定排序：先按效力降序，同效力保持 SQL 的 ts DESC（最新在前）
            chosen = [
                f for f, _s in sorted(primary, key=lambda x: -source_authority(x[1]))[:limit]
            ]
            ref_chosen = [f for f, _s in reference[:_REFERENCE_LIMIT]]
        chosen = [c for c in chosen if c]
        ref_chosen = [c for c in ref_chosen if c]

        # 日记相关片段（软规律，如假期不写稿）
        diary_lines: list[str] = []
        if terms:
            for _date, txt in _read_recent_diary(base_dir):
                diary_lines.extend(_diary_snippets(txt, terms))
            diary_lines = diary_lines[:6]

        if not chosen and not ref_chosen and not diary_lines:
            return ""

        blocks: list[str] = []
        if chosen:
            header = "## 你跟我说过的（长期记忆条目，只是线索，不是事实基准）"
            blocks.append(
                header + "\n" + _FACT_DISCIPLINE + "\n"
                + "\n".join(f"- {c}" for c in chosen)
            )
        if ref_chosen:
            blocks.append(
                "## 参考层（非正文来源：推理／设定／大纲／简报／进度，不得当作已写事实）\n"
                + "\n".join(f"- {c}" for c in ref_chosen)
            )
        if diary_lines:
            dheader = (
                "## 你近期的日常（小桔从自己日记里读到的、关于你的软规律；"
                "比如写作节奏/假期安排，引用时当这是她观察到的，别当成硬规则）"
            )
            blocks.append(dheader + "\n" + "\n".join(f"- {d}" for d in diary_lines))

        return "\n\n".join(blocks)[:_FACT_CONTEXT_MAX_CHARS]
    except Exception:
        return ""
