"""把小桔「学到的关于用户的事实」+ 近期日记软规律，回灌进对话/主动推理上下文。

背景（2026-10-08 萧哥报）：小桔「库里知道、回答/理解时拿不到」——
- learned_history 同时承载两类事实：外部世界观察（带「（外部·」溯源标，由
  world_context.build_world_context 回灌）与**用户口述事实**（萧哥亲口说的偏好/
  习惯/著作/进度，如「《姑射图》是《国子监秘闻录》第三章」「节假日不写稿」）。
  后者此前两条回灌路径都没接。
- 日记（<base>/diary/YYYY-MM-DD.md）里的第一人称软规律同样没进主动推理上下文。

本模块补这两条缺口，并在 2026-11-10 萧哥终裁（「已写正文是唯一事实基准」）之后
补上三道召回纪律：

1. **来源分层**：正文/口述/裁定进主块；推理/设定/大纲/简报/进度/外部/本地笔记
   只进「参考层」。存量 source=live/legacy 的条目按读侧归一（effective_source）
   判定，不靠改历史数据。
2. **书维度隔离**：从当前 query/最近会话推断当前书，书 A 专属条目不再进书 B 会话；
   书 B 会话里只保留当前书条目 + 通用事实（偏好/习惯/跨书经验）。
3. **冲突/陈旧显式化**：同锚点互斥条目并列时给「冲突提示」强制回正文核；旧进度
   条目超过 TTL 不再注入；近重复条目去重，避免同一句换说法重复灌。

fail-open：任何异常返回空串，绝不阻断主流程（聊天/主动 tick 都比记忆重要）。
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from pathlib import Path
from typing import Any

_DAIRY_DAYS = 7
_DAIRY_SNIPPET_MAX = 80   # 日记片段上限：过长的基本是新闻/观察摘抄，非她自己的笔记
_FETCH_FACTS_CAP = 2000  # 直接全量召回活跃用户事实的上限（库只增不减，但量级可控）
_FACT_CONTEXT_MAX_CHARS = 4000  # 注入文本总字数上限（此前整块灌、无上限）
_REFERENCE_LIMIT = 2      # 参考层（非正文来源）最多给几条，避免喧宾夺主
_REFERENCE_FACT_MAX = 240  # 参考层单条字符上限：本地笔记常 500+ 字，整段会吃光预算
_PROGRESS_TTL_DAYS = 30   # 进度条目 TTL：超过 30 天的推进记录不再注入（仍留在库里）
_SESSION_SCAN = 5         # 没在 query 里认出书名时，回看最近几个 session 文件认当前书
_AMBIGUOUS_BOOKS = 2      # query 里出现 >=2 本不同的可识别书名 → 不猜当前书

# 2026-11-10 萧哥终裁：已写正文事实是唯一事实基准。此前块头写「不是她猜的；
# 优先据此作答」，等于给长期记忆发了事实许可证——记忆里六条互斥裁定并存时，
# 她只能挑一句当真讲。改为「只是线索」+ 冲突须明说，把判断交回正文与工具结果。
_FACT_DISCIPLINE = (
    "【事实纪律】已写正文逐字直引才是唯一基准；推理、设定、大纲、简报、讨论结论"
    "与正文冲突时一律以正文为准。记忆和正文对不上，要直说哪里对不上，"
    "不许据记忆下断言；拿不准就说不确定。"
)
#: 日记模板/占位行（LLM 复盘常写这种没信息量的套话，注入只会占预算）
_DIARY_TEMPLATE_MARKERS = (
    "今天我留意到世界上的一些新动静", "记下来", "今天没有太多动静",
    "今日无事", "例行检查", "暂无", "无更新", "小桔的日记",
    "累计对话", "这份日记由今日真实信号生成", "没有的我不编",
)
# 日记日期标题行（如「2026-11-03 周二 · 小桔的日记」）：模板噪声，不是软规律
_DIARY_HEADING_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[^\n]{0,20}(周[一二三四五六日天]|日记)")
# 日记里混着她读新闻后的摘抄（长、带信源标），这些不是「关于用户的软规律」，召回时剔除。
_DIARY_NEWS_MARKERS = (
    "新闻", "早8点", "夜览", "新华", "川观", "记者从", "预计发送", "早啊",
    "央视", "人民网", "澎湃", "日报", "瞭望", "客户端", "新闻来了",
)
# 不是「书」的项目目录名（projects/ 下混着工作区内部目录）
_JUNK_PROJECTS = frozenset({
    "sources", "default", "tmp", "ws", "desktop", "openworker",
    "正文稿子", "写作营素材库", "xiaomi-voice",
})


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


def _term_weights(query: str) -> dict[str, int]:
    """召回词权重：书名/引号专名（整词命中）高于 2-gram 碎词。

    实测问题：query「夜不收 王炽 粮仓」里 2-gram 会命中别的书里同名人物/词，
    主块混进《大清钱王》《国子监》条目。整词命中（书名/专名）权重 6，
    2-gram 权重 2，让「说的是哪本书/哪个人」压过泛词噪声。
    """
    weights: dict[str, int] = {}
    for title in _titles_in(query or ""):
        weights[title] = 6
    for term in _query_terms(query):
        weights.setdefault(term, 2)
    return weights


# ---------------------------------------------------------------------------
# 书维度：识别「当前书」与「这条事实属于哪本书」
# ---------------------------------------------------------------------------
def _norm_book(name: str) -> str:
    return re.sub(r"[\s《》「」『』:：·・\-—（）()\[\]]+", "", str(name or "")).strip()


def _strip_number_prefix(name: str) -> str:
    """projects/ 目录名常带「16.夜不收」这种出版序号前缀。"""
    return re.sub(r"^\d+[.．、]\s*", "", str(name or "")).strip()


def _looks_like_title(name: str) -> bool:
    """像不像一本书名：2-12 个字符、含中文、无路径分隔符（排除工作区目录名/UUID）。"""
    n = str(name or "").strip()
    if "/" in n or "\\" in n or ":" in n:
        return False
    compact = _norm_book(n)
    return 2 <= len(compact) <= 12 and bool(re.search(r"[一-鿿]", compact))


def _is_bookish(name: str) -> bool:
    n = str(name or "").strip()
    return len(n) >= 2 and bool(re.search(r"[一-鿿]", n)) and n.lower() not in _JUNK_PROJECTS


# 资料路径里的「16.夜不收」「20.国子监秘闻录」这类书目录（含 local_intake source 路径、
# 会话里贴的 docx 路径）。只认「数字.中文名」形状，避免把普通中文目录当书。
_NUMBERED_BOOK_DIR_RE = re.compile(r"(?:^|[/\\])(\d{1,3}[.．、][^/\\:：\s]{1,28})")


def _books_from_path(path: str) -> list[str]:
    out: list[str] = []
    for hit in _NUMBERED_BOOK_DIR_RE.findall(str(path or "")):
        name = _strip_number_prefix(hit)
        if _is_bookish(name) and name not in out:
            out.append(name)
    return out


def _known_books(base_dir) -> list[str]:
    """已知书名集合：projects/ 下的书目录名 + 活跃记忆 source 后缀里的书名。

    只收「像书名的」：避免把 UUID 工作区、素材库、以及正文里的章节名（稻粱谋等）
    误当书维度——误判会导致该进上下文的事实被过滤掉（宁漏勿误）。
    """
    books: list[str] = []

    def _add(name: str) -> None:
        cleaned = _strip_number_prefix(name)
        if _is_bookish(cleaned) and cleaned not in books:
            books.append(cleaned)

    root = Path(base_dir) if base_dir else Path.home() / ".deskmate"
    try:
        proj_root = root / "projects"
        if proj_root.is_dir():
            for d in sorted(proj_root.iterdir()):
                if d.is_dir():
                    _add(d.name)
    except OSError:
        pass
    try:
        from .learned_store import LearnedStore, source_book

        db = LearnedStore(base_dir=base_dir).db_path
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT DISTINCT source FROM learned_history "
                "WHERE status='active' AND source LIKE '%:%'"
            ).fetchall()
        finally:
            con.close()
        for (src,) in rows:
            book = source_book(src)
            if book and ("/" in book or "\\" in book):
                # local_intake:/path/to/20.国子监秘闻录/... → 从路径里抽出书目录名，
                # 让本地笔记类事实也带上书维度（否则 34 条资料笔记永远是「无书」）。
                for name in _books_from_path(book):
                    _add(name)
                continue
            _add(book)
    except Exception:
        pass
    # 最近会话里贴过的资料路径（如 .../16.夜不收/正文/...docx）也补进书单
    try:
        for text in _recent_session_texts(base_dir, limit=8):
            for name in _books_from_path(text):
                _add(name)
    except Exception:
        pass
    return books


def _resolve_book(hint: str, known: list[str]) -> str:
    """把来源后缀/正文书名/资料路径归一成已知书名；认不出返回空串。"""
    h = _norm_book(hint)
    if not h:
        return ""
    for book in known:
        nb = _norm_book(book)
        if nb and (nb in h or h in nb):
            return book
    return ""


def _titles_in(text: str) -> list[str]:
    from .learned_store import fact_titles

    return fact_titles(text)


def _recent_session_texts(base_dir, limit: int = _SESSION_SCAN) -> list[str]:
    root = Path(base_dir) if base_dir else Path.home() / ".deskmate"
    sessions = root / "sessions"
    if not sessions.is_dir():
        return []
    try:
        files = sorted(sessions.glob("*.md"), key=lambda p: p.name, reverse=True)[:limit]
    except OSError:
        return []
    out: list[str] = []
    for p in files:
        try:
            out.append(p.read_text(encoding="utf-8", errors="ignore")[:4000])
        except OSError:
            continue
    return out


def infer_current_book(
    base_dir, query: str | None = None, known: list[str] | None = None
) -> str:
    """推断「当前在聊哪本书」，认不出返回空串（= 不做书过滤，保持老行为）。

    顺序：query 里的书名 → 最近 session 文件里的书名。query 同时出现多本可识别
    的书（跨书讨论）时判为不明确，不猜——宁可不过滤，也不把用户正在对比的书
    单方面筛掉。
    """
    try:
        known = known if known is not None else _known_books(base_dir)
        if not known:
            return ""
        resolved: list[str] = []
        for title in _titles_in(query or ""):
            book = _resolve_book(title, known)
            if book and book not in resolved:
                resolved.append(book)
        if len(resolved) == 1:
            return resolved[0]
        if len(resolved) >= _AMBIGUOUS_BOOKS:
            return ""
        # 真实对话里萧哥常直接说「夜不收」而不带书名号（实测 query「夜不收 王炽 粮仓
        # 哪一章」→ 书维度不生效，书 A/B 记忆照旧混灌）。这里用**已知书单**做包含匹配：
        # 恰好命中一本才认，命中多本（跨书对比）不猜。2026-11-10 Lead 补。
        norm_query = _norm_book(query or "")
        if len(norm_query) >= 2:
            bare_hits: list[str] = []
            for book in known:
                nb = _norm_book(book)
                if len(nb) >= 2 and nb in norm_query and book not in bare_hits:
                    bare_hits.append(book)
            if len(bare_hits) >= _AMBIGUOUS_BOOKS:
                return ""
            if len(bare_hits) == 1 and not resolved:
                return bare_hits[0]
        # query 只提到一本「认得出的书名形状」但不在已知书单里（如已完结的《衙探》/
        # 研究用书）：只拿它做**降权排除**（别把其它已知书的专属记忆灌进来），
        # 不据此过滤无书维度的事实。标题过长（文章名/新闻标题）不当作书。
        raw_titles = [
            t.strip()
            for t in _titles_in(query or "")
            if 2 <= len(_norm_book(t)) <= 8
        ]
        if len(raw_titles) == 1:
            return raw_titles[0]
        if len(raw_titles) >= _AMBIGUOUS_BOOKS:
            return ""
        for text in _recent_session_texts(base_dir):
            for title in _titles_in(text):
                book = _resolve_book(title, known)
                if book:
                    return book
        return ""
    except Exception:
        return ""


def _fact_book(row: dict, known: list[str]) -> str:
    """这条事实属于哪本书：source 后缀（含本地资料路径）优先，其次正文里的已知书名。

    2026-11-10 Lead 补：learned_store.fact_book 只认「正文唯一《书名》」，但真实条目
    常夹带引号专名（“张运才/张运财”…），titles 一多就判空 ⇒ 书维度对所有带引号的
    条目失效（实测 query「夜不收…」仍灌进《大清钱王》进度条）。这里改用**已知书单**
    做正文包含匹配：出现 1-2 本 → 按首次出现归属（混合条按主话题）；>=3 本视为
    跨书综述/生涯回顾，保持通用（不因误判被过滤）。
    """
    from .learned_store import fact_book

    hint = fact_book(row.get("raw_source"), row.get("fact", ""))
    resolved = _resolve_book(hint, known) if hint else ""
    if resolved:
        return resolved
    text = _norm_book(row.get("fact", ""))
    if len(text) < 2:
        return ""
    hits: list[tuple[int, str]] = []
    for book in known:
        nb = _norm_book(book)
        if len(nb) < 2:
            continue
        pos = text.find(nb)
        if pos >= 0 and book not in [b for _, b in hits]:
            hits.append((pos, book))
    if not hits or len(hits) >= 3:
        return ""
    hits.sort()
    return hits[0][1]


def _book_allowed(row: dict, current_book: str, known: list[str]) -> bool:
    if not current_book:
        return True
    fb = _fact_book(row, known)
    if not fb:
        return True  # 通用事实（偏好/习惯/跨书经验）永远保留
    return _norm_book(fb) == _norm_book(current_book) or (
        _norm_book(fb) in _norm_book(current_book)
        or _norm_book(current_book) in _norm_book(fb)
    )


# ---------------------------------------------------------------------------
# 读库
# ---------------------------------------------------------------------------
def _rows(base_dir, limit: int = _FETCH_FACTS_CAP) -> list[dict[str, Any]]:
    """取活跃、未过期的记忆行（含来源/锚点/时间），外部观察在调用侧剔除。"""
    try:
        from .learned_store import LearnedStore, _now_iso, effective_source

        db = LearnedStore(base_dir=base_dir).db_path
        con = sqlite3.connect(str(db))
        try:
            con.execute("PRAGMA journal_mode=WAL")
            rows = con.execute(
                "SELECT id, ts, fact, source, entity, polarity FROM learned_history "
                "WHERE status='active' AND (valid_until IS NULL OR valid_until >= ?) "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (_now_iso(), int(limit)),
            ).fetchall()
        finally:
            con.close()
        out: list[dict[str, Any]] = []
        for rid, ts, fact, source, entity, polarity in rows:
            fact_s = str(fact or "").strip()
            if not _is_user_fact(fact_s):
                continue
            out.append(
                {
                    "id": rid,
                    "ts": str(ts or ""),
                    "fact": fact_s,
                    "raw_source": source,
                    "source": effective_source(source, fact_s),
                    "entity": str(entity or ""),
                    "polarity": str(polarity or ""),
                }
            )
        return out
    except Exception:
        return []


def _active_user_facts(base_dir, limit: int = _FETCH_FACTS_CAP) -> list[tuple[str, str]]:
    """兼容旧签名：返回 [(fact, source)]（内部已按读侧来源归一）。"""
    return [(r["fact"], r["source"]) for r in _rows(base_dir, limit)]


def _source_prefix(source: str) -> str:
    from .learned_store import source_prefix

    return source_prefix(source)


def _age_days(ts: str, now: datetime.datetime) -> float:
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
    except ValueError:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


#: P2 分级衰减（设计文档 §6）：只决定「是否注入」，**绝不删行、不改 status**。
#: setting 不自动过期——按 §3.5「设定/推理不自动过期，只在召回侧按效力降权」，
#: 降权由 learned_store._AUTHORITY 负责；正文/裁定/偏好永久。
_DECAY_TTL_DAYS = {
    "progress": _PROGRESS_TTL_DAYS,
    "reasoning": 90,
    "roundtable": 90,
}


def _is_stale(row: dict, now: datetime.datetime) -> bool:
    """按来源分级判断是否陈旧（超期条目仍在库里，只是不进主块/参考层）。"""
    ttl = _DECAY_TTL_DAYS.get(_source_prefix(row["source"]))
    return bool(ttl and _age_days(row["ts"], now) > ttl)


def _norm_body(text: str) -> str:
    from .learned_store import _fingerprint  # noqa: PLC0415 —— 只用于归一比较

    body = re.sub(r"^\s*[-*]?\s*\d{4}-\d{1,2}-\d{1,2}[:：]\s*", "", str(text or ""))
    body = re.sub(r"[\s，,。.！!？?；;、：:（）()「」『』《》\"'“”]+", "", body)
    return body


def _is_near_dup(base: str, other: str) -> bool:
    """近似重复：归一后相同，或一方是另一方前缀且只差几个字（如「这一卷」/「这一卷/篇」）。"""
    if not base or not other:
        return False
    if base == other:
        return True
    short, long = (base, other) if len(base) <= len(other) else (other, base)
    return len(long) - len(short) <= 4 and long.startswith(short)


def _dedupe(items: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for it in items:
        body = _norm_body(it["fact"])
        if any(_is_near_dup(body, _norm_body(k["fact"])) for k in kept):
            continue
        kept.append(it)
    return kept


#: 极性词剥离后还要抹平的弱功能词（「《X》在史料库」vs「《X》不在史料库」配对用）
_WEAK_FUNCTION_CHARS = re.compile(r"[在有了的是]")


def _strip_polar_words(text: str) -> str:
    """去掉否定词与弱功能词后的归一正文——用于「同一件事的正/反说法」配对。"""
    from .learned_store import _DENY_RE

    return _WEAK_FUNCTION_CHARS.sub("", _DENY_RE.sub("", _norm_body(text)))


def _conflict_pairs(items: list[dict]) -> list[tuple[str, str]]:
    """挑出「同一件事被记成相反判断」且都还 active 的条目对，注入时显式提示。

    只认高置信配对：同 entity + 极性相反 + 去掉否定词后正文等同/近似。
    只靠 entity（如 书:夜不收）不够——同一本书下大量互不相关的条目都会被配成假冲突。
    """
    from .learned_store import _extract_polarity

    by_entity: dict[str, list[dict]] = {}
    for it in items:
        entity = it.get("entity") or ""
        if not entity:
            continue
        by_entity.setdefault(entity, []).append(it)
    pairs: list[tuple[str, str]] = []
    for rows in by_entity.values():
        if len(rows) < 2:
            continue
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if _extract_polarity(a["fact"]) == _extract_polarity(b["fact"]):
                    continue
                ca, cb = _strip_polar_words(a["fact"]), _strip_polar_words(b["fact"])
                if ca and cb and _is_near_dup(ca, cb):
                    pairs.append((a["fact"], b["fact"]))
    return pairs[:3]


# ---------------------------------------------------------------------------
# 日记
# ---------------------------------------------------------------------------
def _read_recent_diary(base_dir, days: int = _DAIRY_DAYS) -> list[tuple[str, str]]:
    """读最近 days 天日记正文（文件名 YYYY-MM-DD.md），返回 [(date, text)]。"""
    try:
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
        if any(m in line for m in _DIARY_TEMPLATE_MARKERS):
            continue  # LLM 模板/占位行，没有信息量
        if _DIARY_HEADING_RE.match(line):
            continue  # 日记日期标题行
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


def _fact_date(row: dict) -> str:
    """条目的「语义日期」：正文开头的 YYYY-MM-DD 优先，其次 ts。

    ts 是入库时间，不等于判断发生的时间（本地笔记 10-04 批量入库，内容日期是 09-24）；
    多版本提示要看语义时间跨度，否则会把 17 天的旧计划/新决定算成 6 天。
    """
    m = re.match(r"^\s*(\d{4}-\d{1,2}-\d{1,2})", row.get("fact") or "")
    if m:
        return m.group(1)
    return (row.get("ts") or "")[:10]


def _multi_version_block(items: list[dict]) -> str:
    """同锚点多条活跃、但文本骨架不同的「多版本」提示。

    实测（lead 2026-11-10）：「投名状 还写不写」主块里同时有旧计划（09-24 计划写）与
    新决定（10-11 决定不写这一卷），_conflict_pairs 要求「同 entity + 反极性 + 去否定
    后近似」，骨架差太多配不上 → 不提示，她仍可能拿旧的当现在。这里补弱提示：
    同锚点 ≥2 条、时间跨度 ≥7 天、文本不是全近似 → 按时间列出，并明说旧的不等于现在。
    """
    groups: dict[str, list[dict]] = {}
    for it in items:
        entity = it.get("entity") or ""
        if entity:
            groups.setdefault(entity, []).append(it)

    best: tuple[int, str, list[dict]] | None = None
    for entity, rows in groups.items():
        if len(rows) < 2:
            continue
        ordered = sorted(rows, key=_fact_date)
        try:
            first = datetime.datetime.fromisoformat(_fact_date(ordered[0]))
            last = datetime.datetime.fromisoformat(_fact_date(ordered[-1]))
            span = max(0, (last - first).days)
        except (ValueError, KeyError):
            span = 0
        bodies = [_norm_body(r["fact"]) for r in ordered]
        if bodies and all(_is_near_dup(bodies[0], b) for b in bodies[1:]):
            continue  # 全是同一句换说法 → 交给近重复去重，不必提示
        if span >= 7 and (best is None or span > best[0]):
            best = (span, entity, ordered)

    if not best:
        return ""
    _span, _entity, ordered = best
    lines = [
        "## 同一主题有多条不同时期的记忆（旧的不等于现在还成立，最后一条是较新的说法）"
    ]
    for row in ordered[:4]:
        lines.append("- [" + (row.get("ts") or "")[:10] + "] " + row["fact"][:160])
    lines.append("旧条可能已被后来的决定推翻；拿不准就回正文/问萧哥，别把旧判断当确定事实。")
    return "\n".join(lines)


def _canon_note(base_dir, row: dict) -> str:
    """P3：正文条附「出处：书名 第N段」；非正文条不附。

    云端没有本地证据索引时返回「仅本地可回查」，不假装能校验。
    异常 fail-open，绝不让出处逻辑拖垮整轮注入。
    """
    try:
        from .learned_store import _fingerprint, canon_provenance

        return canon_provenance(base_dir, _fingerprint(row["fact"]), row["source"])
    except Exception:
        return ""


def _truncate_reference(text: str, limit: int = _REFERENCE_FACT_MAX) -> str:
    """参考层单条截断（本地笔记常整段 500+ 字，不该吃掉注入预算）。"""
    body = str(text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "…（节选）"


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_user_fact_context(
    base_dir: str | Path | None = None,
    query: str | None = None,
    limit: int = 10,
    book: str | None = None,
    now: datetime.datetime | None = None,
) -> str:
    """取活跃、非外部的用户事实 + 近期日记相关片段，格式化为注入文本。

    Args:
        query: 当前对话/卡片主题；传了就按关键词召回最相关，不传取最近。
        limit: 最多条数（用户事实）。
        book: 当前书；None 时自动从 query/最近会话推断，认不出就不做书过滤。
        now: 注入时刻（测试用），默认当前时间。
    Returns:
        「你跟我说过的……」+（可选）「参考层」/「冲突提示」/「你近期的日常……」；无则空串。
    """
    try:
        from .learned_store import is_non_fact_source, source_authority

        now = now or datetime.datetime.now()
        terms = _query_terms(query) if query else []
        rows = _rows(base_dir)
        if not rows and not terms:
            return ""
        known = _known_books(base_dir)
        current_book = str(book or "").strip()
        # 已知书单能归一时用规范名；归一时保留原书名（query 里认得出的「书形状」标题，
        # 如已完结的《衙探》——仍要拿它排除其它书的专属记忆）。
        # manager 传进来的 active_project 可能是工作区目录名（UUID/路径），
        # 既不在书单里、也不像书名 → 当没有书上下文。
        if current_book:
            resolved = _resolve_book(current_book, known)
            if resolved:
                current_book = resolved
            elif not _looks_like_title(current_book):
                current_book = ""
        # P1-B：显式 book 缺失或不可用（UUID/路径）时，必须回退到 query/最近会话推断。
        # P0 原实现是 ``book if book else infer_current_book(...)``：只要 manager 传了
        # 一个非空的 active_project，哪怕它是 UUID 被上面判空，也不会再推断 ⇒
        # query 明明写了《夜不收》，整轮书隔离静默失效。
        if not current_book:
            current_book = infer_current_book(base_dir, query, known)

        rows = [r for r in rows if not _is_stale(r, now)]
        rows = [r for r in rows if _book_allowed(r, current_book, known)]

        # 效力分层：正文/口述/裁定进主块；推理/设定/大纲/简报/进度/外部/本地笔记只进参考层。
        primary = [r for r in rows if not is_non_fact_source(r["source"])]
        reference = [r for r in rows if is_non_fact_source(r["source"])]

        chosen_rows: list[dict] = []
        ref_rows: list[dict] = []
        weights = _term_weights(query or "") if terms else {}
        if terms:
            scored = []
            for row in primary:
                score = sum(w for t, w in weights.items() if t in row["fact"])
                if score > 0:
                    scored.append((score, source_authority(row["source"]), row))
            # 同分时正文优先（正文是唯一事实基准）
            scored.sort(key=lambda x: (-x[0], -x[1]))
            chosen_rows = _dedupe([row for _, _, row in scored])[:limit]

            ref_scored = []
            for row in reference:
                score = sum(w for t, w in weights.items() if t in row["fact"])
                if score > 0:
                    ref_scored.append((score, source_authority(row["source"]), row))
            ref_scored.sort(key=lambda x: (-x[0], -x[1]))
            ref_rows = _dedupe([row for _, _, row in ref_scored])[:_REFERENCE_LIMIT]
        else:
            # 无 query：先保通用事实（偏好/习惯/跨书经验），再按效力、时间排书专属条目。
            # 没有当前书上下文时，书专属条目只做补充——否则「最新的那本书」的裁定
            # 会占满主块（实测修复前无 query 主块 10 条里 6 条是夜不收/龙山仓）。
            chosen_rows = _dedupe(
                sorted(
                    primary,
                    key=lambda r: (
                        1 if _fact_book(r, known) else 0,
                        -source_authority(r["source"]),
                    ),
                )
            )[:limit]
            ref_rows = _dedupe(reference)[:_REFERENCE_LIMIT]

        # 主块/参考层互斥：同一句已经在主块出现，就不再进参考层
        primary_bodies = {_norm_body(r["fact"]) for r in chosen_rows}
        ref_rows = [r for r in ref_rows if _norm_body(r["fact"]) not in primary_bodies]

        conflicts = _conflict_pairs(chosen_rows + ref_rows)

        # 日记相关片段（软规律，如假期不写稿）；跨天重复行去重（实测同一模板行 3 天
        # 三条都进注入）
        diary_lines: list[str] = []
        if terms:
            seen_diary: set[str] = set()
            for _date, txt in _read_recent_diary(base_dir):
                for line in _diary_snippets(txt, terms):
                    body = _norm_body(line)
                    if not body or body in seen_diary:
                        continue
                    seen_diary.add(body)
                    diary_lines.append(line)
            diary_lines = diary_lines[:6]

        if not chosen_rows and not ref_rows and not diary_lines:
            return ""

        blocks: list[str] = []
        if chosen_rows:
            header = "## 你跟我说过的（长期记忆条目，只是线索，不是事实基准）"
            blocks.append(
                header + "\n" + _FACT_DISCIPLINE + "\n"
                + "\n".join(
                    "- " + r["fact"] + _canon_note(base_dir, r)
                    for r in chosen_rows
                )
            )
        if ref_rows:
            blocks.append(
                "## 参考层（非正文来源：推理／设定／大纲／简报／进度／本地笔记，"
                "不得当作已写事实）\n"
                + "\n".join("- " + _truncate_reference(row["fact"]) for row in ref_rows)
            )
        if conflicts:
            lines = [
                "## 冲突提示（同锚点记忆互相矛盾，不许挑一句当真讲，必须先回正文/资料核）"
            ]
            for a, b in conflicts[:3]:
                lines.append("- 说法一：" + a[:120])
                lines.append("  说法二：" + b[:120])
            lines.append("回正文核过再回答；回忆不起就如实说记忆里有冲突。")
            blocks.append("\n".join(lines))
        else:
            multi = _multi_version_block(chosen_rows + ref_rows)
            if multi:
                blocks.append(multi)
        if diary_lines:
            dheader = (
                "## 你近期的日常（小桔从自己日记里读到的、关于你的软规律；"
                "比如写作节奏/假期安排，引用时当这是她观察到的，别当成硬规则）"
            )
            blocks.append(dheader + "\n" + "\n".join("- " + d for d in diary_lines))

        return "\n\n".join(blocks)[:_FACT_CONTEXT_MAX_CHARS]
    except Exception:
        return ""
