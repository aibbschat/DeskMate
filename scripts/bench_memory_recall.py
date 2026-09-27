#!/usr/bin/env python3
"""检索 recall 基准（P0-2，对标 MemPalace benchmarks/）。

为什么需要它
------------
「感觉小桔检索变笨了 / 变好了」长期无法量化——记忆层没有任何 recall 度量。
本脚本给现有各检索通道建一套可复现的 R@5 / R@10 评测：同一批 query 跑同一
组通道，出对比表。通道就是**生产真路**（不是重写一份理想实现）：

- corpus=deskmate（统一库 learned_history + external_items）：
  1. ``keyword2gram`` —— 与 vault/manager 同源的 CJK 2-gram + 关键词打分
     （覆盖 learned_history：它没有向量，只有关键词这条路）；
  2. ``semantic``     —— 生产 SemanticSearcher，alpha=1.0（纯语义，
     仅 external_items 有向量）；
  3. ``hybrid``       —— 生产 search_external()，默认 alpha=0.7。
- corpus=history（史料库 / 私人库 ``<base>/history/*.db``）：
  4. ``fts5``         —— 生产 search_across_dbs 四级降级（该 corpus 的唯一在役通道）。

评测集
------
``<base>/bench/recall_cases.jsonl``，每行：
    {"id": "...", "query": "...", "corpus": "deskmate|history",
     "match": "期望出现在 top-K 结果正文里的子串",
     "channels": ["该 case 参与哪些通道的统计"], "auto": false, "note": ""}

- ``channels`` 声明适用通道（不填 = 该 corpus 全通道）。**必须填对**：
  semantic/hybrid 结构上只覆盖 external_items（learned_history 无向量），
  把不适用 case 混进统计会把 R 污染成无意义的低分。
- ``match`` 匹配前两侧都做归一化（去空白/标点），query 里带标点也能对上
  原文分行的条目。

- ``--seed`` 自动抽样生成种子集（自检索式 query，``auto: true``）——**只用来
  打通链路**；要得到有意义的数字，必须人工把 query 改写成你会怎么问的自然问法
  （改完把 auto 置 false）。种子集不进仓库（含真实私人记忆内容）。
- 追加惯例：每次复盘「检索变笨」类问题，顺手把当时的 query 沉淀成一条 case。

用法
----
    python scripts/bench_memory_recall.py --seed     # 生成种子集（已存在则跳过，--force 覆盖）
    python scripts/bench_memory_recall.py            # 跑基准，出人类可读表
    python scripts/bench_memory_recall.py --json     # 机器可读输出

硬约束：除种子集文件外零写入；库连接 mode=ro；评测确定性（同库同集两次结果一致）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# 保证脚本可从仓库任意目录直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmate.paths import deskmate_base_dir  # noqa: E402  调用时解析 DESKMATE_DIR

_K_LIST = (5, 10)


# ---------------------------------------------------------------------------
# 评测集
# ---------------------------------------------------------------------------
def cases_path() -> Path:
    return deskmate_base_dir() / "bench" / "recall_cases.jsonl"


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    if not path.is_file():
        return cases
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if c.get("query") and c.get("corpus") in ("deskmate", "history") and c.get("match"):
            cases.append(c)
    return cases


# ---------------------------------------------------------------------------
# deskmate 语料池（只读）
# ---------------------------------------------------------------------------
def load_deskmate_pool(db_path: Path) -> list[dict]:
    """learned_history + external_items 全量条目。返回 [{id, text, has_vector}]。"""
    rows: list[dict] = []
    if not db_path.is_file():
        return rows
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if _has_table(con, "learned_history"):
            for rid, fact in con.execute("SELECT id, fact FROM learned_history"):
                if fact and str(fact).strip():
                    rows.append({"id": f"l:{rid}", "text": str(fact), "has_vector": False})
        if _has_table(con, "external_items"):
            for rid, title, summary, content in con.execute(
                "SELECT id, title, COALESCE(summary,''), COALESCE(content,'') "
                "FROM external_items"):
                text = "\n".join(x for x in (title, summary, content) if x)
                if text.strip():
                    rows.append({"id": f"e:{rid}", "text": text, "has_vector": True})
    finally:
        con.close()
    return rows


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# 通道 1：keyword2gram（与 manager._extract_keywords / vault bigram 同源思路）
# ---------------------------------------------------------------------------
def _keywords(query: str) -> list[str]:
    try:
        from deskmate.memory.manager import DeskMateMemoryManager
        return DeskMateMemoryManager._extract_keywords(query)
    except Exception:
        return [w for w in re.split(r"[\s，。：；！？、''\"\"()（）]+", query) if len(w) >= 2]


def _cjk_bigrams(text: str) -> list[str]:
    cjk = re.findall(r"[\u4e00-\u9fff]", text or "")
    return [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]


def _kw_score(text: str, kws: list[str], q_bigrams: list[str]) -> float:
    score = 0.0
    for kw in kws:
        if kw and kw in text:
            score += len(kw)  # 长关键词权重大（整词命中 > 碎片命中）
    if q_bigrams:
        hit = sum(1 for bg in q_bigrams if bg in text)
        score += 2.0 * hit / len(q_bigrams)
    return score


def rank_keyword(pool: list[dict], query: str, k: int) -> list[dict]:
    kws = _keywords(query)
    q_bigrams = _cjk_bigrams(query)
    scored = [( _kw_score(r["text"], kws, q_bigrams), r) for r in pool]
    scored.sort(key=lambda x: -x[0])
    return [r for s, r in scored[:k] if s > 0]


# ---------------------------------------------------------------------------
# 通道 2/3：生产语义 / 混合（external_items）
# ---------------------------------------------------------------------------
def rank_semantic(query: str, k: int) -> tuple[list[dict], dict]:
    from deskmate.memory.semantic_search import search_external
    res = search_external(query, limit=k, alpha=1.0)
    return res.get("results", []), res


def rank_hybrid(query: str, k: int) -> tuple[list[dict], dict]:
    from deskmate.memory.semantic_search import search_external
    res = search_external(query, limit=k)
    return res.get("results", []), res


def _ext_row_text(r: dict) -> str:
    return "\n".join(str(r.get(f) or "") for f in ("title", "summary", "content"))


# ---------------------------------------------------------------------------
# 通道 4：生产 FTS5（history 库）
# ---------------------------------------------------------------------------
def history_db_paths(base: Path) -> list[str]:
    hist = base / "history"
    if not hist.is_dir():
        return []
    return sorted(str(p) for p in hist.glob("*.db")
                  if p.is_file() and not p.name.endswith(("-wal", "-shm")))


def rank_fts(base: Path, query: str, k: int) -> list[dict]:
    from coworker.history.fts5 import search_across_dbs
    paths = history_db_paths(base)
    if not paths:
        return []
    return search_across_dbs(paths, query, limit=k)


def _hist_row_text(r: dict) -> str:
    return "\n".join(str(r.get(f) or "") for f in ("title", "snippet"))


# ---------------------------------------------------------------------------
# 跑基准
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """归一化：去空白与标点，只留文字数字——子串匹配在两侧归一化后进行。"""
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE)


def _hit(match: str, texts: list[str], article: str = "",
         rows: Optional[list[dict]] = None) -> bool:
    """命中判定（两级）：

    1. 锚子串：match 归一化后出现在任一结果正文里——最严格判法；
    2. **同文章**（2026-09-26 补）：case 带 ``article``（锚 chunk 所属文章）
       且任一结果的 articleId 命中。同一篇文章的相邻 chunk 常同为合格
       答案（实测：淳于髡 query 返回同篇《古文辞类纂》相邻段），单 chunk
       锚把「答对了」误判成 miss。史料检索的消费方是 LLM，拿到同篇文章
       的相邻段即可作答，按文章计 recall 才符合真实用途。
    """
    m = _norm(match)
    if m and any(m in _norm(t) for t in texts):
        return True
    if article and rows:
        return any(str(r.get("articleId")) == str(article) for r in rows)
    return False


def run_bench(cases: list[dict], base: Path) -> dict:
    pool = load_deskmate_pool(base / "deskmate.db")
    db_cases = [c for c in cases if c["corpus"] == "deskmate"]
    hist_cases = [c for c in cases if c["corpus"] == "history"]

    sem_meta: dict = {}
    try:
        from deskmate.memory.semantic_search import backend_status
        sem_meta.update(backend_status() or {})
    except Exception:
        pass

    def _recall(channel: str, cs: list[dict], k: int) -> tuple[Optional[float], int]:
        """返回 (R@k, 参与统计的 case 数)——只统计声明了该通道的 case。"""
        applicable = [c for c in cs
                      if not c.get("channels") or channel in c["channels"]]
        if not applicable:
            return None, 0
        hits = 0
        for c in applicable:
            texts: list[str] = []
            rows: Optional[list[dict]] = None
            if channel == "keyword2gram":
                top = rank_keyword(pool, c["query"], k)
                texts = [r["text"] for r in top]
            elif channel == "semantic":
                rows, _ = rank_semantic(c["query"], k)
                texts = [_ext_row_text(r) for r in rows]
            elif channel == "hybrid":
                rows, _ = rank_hybrid(c["query"], k)
                texts = [_ext_row_text(r) for r in rows]
            elif channel == "fts5":
                rows = rank_fts(base, c["query"], k)
                texts = [_hist_row_text(r) for r in rows]
            if _hit(c["match"], texts, article=c.get("article", ""), rows=rows):
                hits += 1
        return round(hits / len(applicable), 3), len(applicable)

    channels = {
        "deskmate": ["keyword2gram", "semantic", "hybrid"],
        "history": ["fts5"],
    }
    table = []
    for corpus, chans in channels.items():
        cs = db_cases if corpus == "deskmate" else hist_cases
        for ch in chans:
            row = {"corpus": corpus, "channel": ch}
            for kk in _K_LIST:
                r, n = _recall(ch, cs, kk)
                row[f"R@{kk}"] = r
                row[f"n@{kk}"] = n
            row["cases"] = max(row["n@5"], row["n@10"], 0)
            table.append(row)

    return {
        "base_dir": str(base),
        "pool_deskmate_rows": len(pool),
        "history_dbs": len(history_db_paths(base)),
        "embedding": sem_meta,
        "cases_total": len(cases),
        "cases_auto": sum(1 for c in cases if c.get("auto")),
        "table": table,
        "note": "auto 种子集是自检索 query，数字只证明链路通；改写成自然问法后才有基准意义",
    }


# ---------------------------------------------------------------------------
# 种子集生成（自检索式，auto: true，需人工改写）
# ---------------------------------------------------------------------------
def _first_clause(text: str, lo: int = 6, hi: int = 14) -> str:
    seg = re.split(r"[。！？；\n，,、：]", text.strip())[0].strip()
    if len(seg) < lo:
        seg = re.sub(r"\s+", "", text.strip())[:hi]
    return seg[:hi]


#: learned 事实的记录时间戳前缀（「用户于2026-07-29明确…」/「我于…」）——不剥掉
#: 的话自动抽出的 query 全是裸日期，谁都命中、改写了也没意义。
#: 日期前允许任意非数字引导词（用户/我/AI 记录于…），日期后吞掉紧跟的零散字。
_TS_PREFIX_RE = re.compile(
    r"^\D{0,10}?\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?\s*[:：，,]?\s*"
)


def _seed_query(text: str, min_cjk: int = 4) -> str:
    """从源文本抽自检索 query：剥时间戳前缀 → 取首段；纯日期/过短返回空。"""
    body = _TS_PREFIX_RE.sub("", (text or "").strip())
    q = _first_clause(body)
    # 纯数字/日期的 query 无判别力（任何行都可能含同日期），作废
    if len(re.findall(r"[\u4e00-\u9fff]", q)) < min_cjk:
        return ""
    return q


def make_seed(base: Path, per_corpus: int = 10) -> list[dict]:
    cases: list[dict] = []
    # deskmate：learned 取一半；external 只从**生产可检索集**抽
    # （digested + 有向量）——archived/无向量行被检索闸门排除是应然行为，
    # 拿它们做 case 测的是闸门而不是排序（闸门健康归 audit 管）。
    pool = load_deskmate_pool(base / "deskmate.db")
    learned = [r for r in pool if r["id"].startswith("l:") and len(r["text"]) >= 12]
    ext: list[dict] = []
    db = base / "deskmate.db"
    if db.is_file():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            if _has_table(con, "external_items"):
                for rid, title, summary, content in con.execute(
                    "SELECT id, title, COALESCE(summary,''), COALESCE(content,'') "
                    "FROM external_items "
                    "WHERE embedding IS NOT NULL AND ingestion_status='digested'"):
                    text = "\n".join(x for x in (title, summary, content) if x)
                    if len(text.strip()) >= 12:
                        ext.append({"id": f"e:{rid}", "text": text})
        finally:
            con.close()
    half = max(1, per_corpus // 2)
    seen_q: set[str] = set()
    for r in (learned[:half] + ext[: per_corpus - half]):
        q = _seed_query(r["text"])
        if not q or q in seen_q:  # 重复 query 无判别力，跳过
            continue
        seen_q.add(q)
        # 通道适用性：learned_history 无向量，semantic/hybrid 结构上搜不到它
        chans = (["keyword2gram"] if r["id"].startswith("l:")
                 else ["keyword2gram", "semantic", "hybrid"])
        cases.append({
            "id": f"auto-{r['id']}", "query": q, "corpus": "deskmate",
            "match": q, "channels": chans, "auto": True,
            "note": f"自检索种子（源 {r['id']}）：把 query 改写成自然问法，match 保持指向原条目独特子串",
        })
    # history：每个库抽几条 chunk
    from coworker.history.fts5 import _CJK_RE  # 复用 CJK 判定，避免再写一份
    n = 0
    for p in history_db_paths(base)[:2]:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
            col = next((c for c in ("content", "text", "body") if c in cols), None)
            if not col:
                continue
            for (aid, content,) in con.execute(
                f"SELECT article_id, {col} FROM chunks WHERE LENGTH({col}) >= 50 ORDER BY RANDOM() LIMIT 5"):
                cjk = re.findall(_CJK_RE, content or "")
                if len(cjk) < 8:
                    continue
                q = "".join(cjk[:8])
                cases.append({
                    "id": f"auto-h-{Path(p).stem}-{n}", "query": q, "corpus": "history",
                    "match": q, "channels": ["fts5"], "auto": True,
                    # 锚 chunk 所属文章：同文章相邻 chunk 常同为合格答案，
                    # 命中判法按「锚子串 OR 同文章」计（见 _hit）。
                    "article": str(aid),
                    "note": f"自检索种子（源 {Path(p).stem}）：改写成自然问法",
                })
                n += 1
        finally:
            con.close()
    return cases


def save_seed(cases: list[dict], path: Path, force: bool) -> str:
    if path.exists() and not force:
        return f"评测集已存在（{len(load_cases(path))} 条有效 case），跳过。要重建加 --force"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)  # 原子落盘
    return f"种子集已生成：{path}（{len(cases)} 条，auto=true）——请人工改写 query"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def print_human(report: dict) -> None:
    print(f"检索 recall 基准 · {report['base_dir']}")
    print(f"语料：deskmate 池 {report['pool_deskmate_rows']} 条 · history 库 "
          f"{report['history_dbs']} 个 · case {report['cases_total']} 条"
          f"（auto {report['cases_auto']}）")
    emb = report.get("embedding") or {}
    print(f"embedding：local_available={emb.get('local_available', '?')} "
          f"model={emb.get('local_model') or emb.get('last_model') or '?'}")
    print("=" * 76)
    print(f"{'corpus':<10} {'channel':<14} {'cases':>5} {'R@5':>9} {'R@10':>9}")
    print("-" * 76)
    for row in report["table"]:
        def fmt(v, n):
            return f"{v * 100:.1f}% ({n})" if isinstance(v, float) else "  —"
        print(f"{row['corpus']:<10} {row['channel']:<14} {row['cases']:>5} "
              f"{fmt(row['R@5'], row['n@5']):>9} {fmt(row['R@10'], row['n@10']):>9}")
    print("=" * 76)
    print("R@K 后括号 = 参与统计的 case 数（通道不适用/未声明的 case 不计入）")
    if report["cases_auto"]:
        print(f"⚠ {report['note']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench_memory_recall", description="小桔检索 recall 基准（只读）")
    parser.add_argument("--seed", action="store_true", help="生成种子评测集")
    parser.add_argument("--force", action="store_true", help="重建种子集（覆盖现有）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    base = deskmate_base_dir()
    if args.seed:
        cases = make_seed(base)
        if not cases:
            print("没抽到可用的种子（库为空？）", file=sys.stderr)
            return 1
        print(save_seed(cases, cases_path(), args.force))
        return 0

    cases = load_cases(cases_path())
    if not cases:
        p = cases_path()
        print(f"评测集为空或不存在：{p}\n"
              f"先跑 --seed 生成种子，再把 auto case 的 query 改写成自然问法。",
              file=sys.stderr)
        return 1
    report = run_bench(cases, base)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
