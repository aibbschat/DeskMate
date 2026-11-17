#!/usr/bin/env python3
"""把「稿类事件」回填到具体作品对象上（一次性，可回滚）。

## 为什么要有这一步

情感层的对象必须是**具体的那一本**，不能是「稿子」。但 `emotion_log` 里
写稿类事件的 `object_ref` 当时是空的或是类别泛称（「稿件」「第五部」），
所以 `object_kind='work'` 全库只有 5 行、散在 5 个名字上各 1 条，永远过不了
候选池门槛（n>=3 且 days>=3 且跨度>=7）。结果是「稿子里的一句话 → 现实里的
人」这个跳跃在生产里根本不可达，只活在构造测试里。

回填的**依据**只有一个可信来源：**那天你确实动过这本书的书稿文件**（mtime）。
摘要里出现书名只有 0.2% 命中，回查聊天原文的 `conversation_id` 全空，而按日记
提书名去锚当天所有稿类事件会明显错归（「密码被拆」被算成《大清钱王》）。

## 三道闸（缺一条就不回填）

1. **锚定日唯一**：那天只有一本书的稿子被改过；多本同改 = 有歧义，整天跳过。
2. **摘要指向文本实体**：命中「第X章/卷/回」或 稿/大纲/章纲/梗概/正文/字数/
   错字/删字/定稿…，且不含「方案/台账/日报/核算/评审/任务」这类任务交付词。
   第一版把「交付」「验收」当写作信号，结果把「压力装置表交付完成」也算成
   这本书的事——那是她执行任务，不是萧盛的小说。
3. **按天限流**（与候选池同口径）：同一天最多留 6 条、日内摘要去重。一天里
   的一批改稿不是一批独立的事；不限流会让一天的密集协作压过一个月。

回填后仍要过候选池门槛才进池——本脚本只负责让「数据里到底有没有这本书」这件事
成立，不负责替她判断情感。

## 用法

    # 只看会改什么，不动库
    python scripts/backfill_work_objects_20261117.py --dry-run

    # 真改（先自己 sqlite3 .backup，脚本不代替你备份）
    python scripts/backfill_work_objects_20261117.py --apply

回滚清单默认写到 `reports/2026-11-17_作品回填_回滚清单.json`，每行记着改前的
`object_ref` / `object_kind`，可用 `--rollback <清单>` 原样退回。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmate.cognitive.feeling_store import _cap_per_day  # noqa: E402

DEFAULT_DB = Path.home() / ".deskmate" / "deskmate.db"
DEFAULT_BOOKS_ROOT = Path.home() / "OpenWorker"
DEFAULT_LOG = (
    Path(__file__).resolve().parents[1] / "reports" / "2026-11-17_作品回填_回滚清单.json"
)

DEGENERATE_SOURCES = ("time_decay", "pending", "lapsed", "landed", "temper_foul")

# 闸 2：摘要必须指向**文本实体本身**。
#
# 这里刻意**不收**「交付」「验收」「结构」「返工」「出处」这类过程泛词：2026-11-17
# 第一版把它们当写作信号，结果把「压力装置表交付完成」「方案交付、核算理平」
# 「日报按时交付」「圣典重建落库」全归进了《夜不收》——那些是她执行任务的交付，
# 不是萧盛的小说。拿假证据长出来的情感判断，比没有判断更坏。
#
# 只认「第X章/卷/回」与章稿类实体词。宁可少收（11-16「挖到出处」那条因为不含
# 实体词被放过），也不要把任务交付混进书稿。
TEXTUAL = re.compile(
    r"第[0-9一二三四五六七八九十百千两]+[章卷回节折]"
    r"|稿|大纲|章纲|梗概|正文|字数|错字|删字|定稿|交稿|改稿|开篇|结尾"
)
# 任务/工具域：出现就不算书写事件（防止把 agent 自己的活算成稿子的事）
BAD = re.compile(
    r"日记|收工|白卷|情绪|睡了|吃饭|天气|雨|雾|新闻|帖子|密码|插件|链接"
    r"|方案|台账|日报|核算|评审|预算|排期|信源|圣典|装置|文书|任务"
)

BOOK_SUFFIXES = (".md", ".txt", ".docx", ".json")
SKIP_DIRS = {".deskmate", "node_modules", ".git", "__pycache__"}


def _surface_map(conn: sqlite3.Connection) -> dict[str, str]:
    """书名/别名 → 正名。长的先匹配，避免短名吃掉长名。"""
    surface: dict[str, str] = {}
    for row in conn.execute("SELECT title, aliases FROM works"):
        title = str(row["title"] or "").strip()
        if not title:
            continue
        surface[title] = title
        for alias in str(row["aliases"] or "").split(","):
            alias = alias.strip()
            if alias:
                surface[alias] = title
    return dict(sorted(surface.items(), key=lambda kv: len(kv[0]), reverse=True))


def _anchored_days(surface: dict[str, str], root: Path, days: int) -> tuple[dict[str, str], dict]:
    """闸 1：按书稿文件 mtime 定「那天在改哪本书」，只认唯一的那天。"""
    cutoff = time.time() - days * 86400
    per_day: dict[str, set[str]] = collections.defaultdict(set)
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(BOOK_SUFFIXES):
                continue
            book = next((surface[k] for k in surface if k in name), None)
            if not book:
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            per_day[time.strftime("%Y-%m-%d", time.localtime(mtime))].add(book)
            scanned += 1
    unique = {d: next(iter(books)) for d, books in per_day.items() if len(books) == 1}
    stats = {
        "files_matched": scanned,
        "days_touched": len(per_day),
        "days_unique": len(unique),
        "days_ambiguous": sum(1 for b in per_day.values() if len(b) > 1),
    }
    return unique, stats


def _plan(conn: sqlite3.Connection, unique: dict[str, str], days: int) -> tuple[dict, list[dict]]:
    """按三道闸算出要改哪些行（只读，不写库）。"""
    rows = conn.execute(
        """
        SELECT id, ts, source, object_ref, object_kind, event_summary
        FROM emotion_log
        WHERE object_kind IN ('none', 'person', 'work', 'thing')
          AND event_summary != ''
          AND source NOT IN ({placeholders})
          AND ts >= date('now', ?)
        ORDER BY ts
        """.format(placeholders=",".join("?" * len(DEGENERATE_SOURCES))),
        (*DEGENERATE_SOURCES, f"-{days} day"),
    ).fetchall()

    picked: dict[str, list[dict]] = collections.defaultdict(list)
    skipped = collections.Counter()
    for row in rows:
        day = str(row["ts"] or "")[:10]
        book = unique.get(day)
        if not book:
            skipped["不在唯一锚定日"] += 1
            continue
        summary = str(row["event_summary"])
        if not TEXTUAL.search(summary):
            skipped["摘要不指向文本实体"] += 1
            continue
        if BAD.search(summary):
            skipped["摘要属任务/工具域"] += 1
            continue
        picked[book].append(
            {
                "id": str(row["id"]),
                "ts": day,
                "summary": summary,
                "old_ref": str(row["object_ref"] or ""),
                "old_kind": str(row["object_kind"] or ""),
            }
        )

    accepted: dict[str, list[dict]] = {}
    for book, items in picked.items():
        # 回填是**修数据事实**：这 N 条事件确实属于这本书。门槛按闸 1+2 之后的
        # 原始条数判。限流是读时策略（build_pool 里做），这里只报「进池后还剩几条」，
        # 免得把「数据少」和「被限流」混成一件事。
        days_hit = sorted({i["ts"] for i in items})
        span = (
            (date.fromisoformat(days_hit[-1]) - date.fromisoformat(days_hit[0])).days
            if len(days_hit) > 1
            else 0
        )
        kept = _cap_per_day(
            [{"id": i["id"], "ts": i["ts"], "summary": i["summary"]} for i in items]
        )
        if len(items) >= 3 and len(days_hit) >= 3 and span >= 7:
            for row in items:
                row["book"] = book
            accepted[book] = items

    summary = {
        "books": {
            book: {
                "rows": len(rows),
                "pool_rows": len(
                    _cap_per_day(
                        [{"id": r["id"], "ts": r["ts"], "summary": r["summary"]} for r in rows]
                    )
                ),
                "days": len({r["ts"] for r in rows}),
                "span_days": (
                    (
                        date.fromisoformat(max(r["ts"] for r in rows))
                        - date.fromisoformat(min(r["ts"] for r in rows))
                    ).days
                    if len({r["ts"] for r in rows}) > 1
                    else 0
                ),
            }
            for book, rows in accepted.items()
        },
        "rejected_books": {
            book: len(rows) for book, rows in picked.items() if book not in accepted
        },
        "skipped": dict(skipped),
        "total_rows": sum(len(v) for v in accepted.values()),
    }
    return summary, [r for rows in accepted.values() for r in rows]


def _apply(conn: sqlite3.Connection, plan: list[dict], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "emotion_log.object_ref backfill（work）",
        "rows": plan,
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "UPDATE emotion_log SET object_ref=?, object_kind='work' WHERE id=?",
            [(r["book"], r["id"]) for r in plan],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(plan)


def _rollback(conn: sqlite3.Connection, log_path: Path) -> int:
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "UPDATE emotion_log SET object_ref=?, object_kind=? WHERE id=?",
            [(r["old_ref"], r["old_kind"], r["id"]) for r in rows],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--books-root", default=str(DEFAULT_BOOKS_ROOT))
    ap.add_argument("--days", type=int, default=95, help="回填窗口（天）")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="清单", help="按清单原样退回")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 2
    conn = sqlite3.connect(args.db, timeout=15)
    conn.row_factory = sqlite3.Row

    if args.rollback:
        n = _rollback(conn, Path(args.rollback))
        print(f"已退回 {n} 行（清单 {args.rollback}）")
        return 0

    surface = _surface_map(conn)
    unique, stats = _anchored_days(surface, Path(args.books_root), args.days)
    summary, plan = _plan(conn, unique, args.days)

    print(f"作品登记 {len(surface)} 个 surface 名；书稿文件命中 {stats['files_matched']} 个")
    print(f"近 {args.days} 天动过稿子的日子 {stats['days_touched']} 天，"
          f"唯一可锚定 {stats['days_unique']} 天，多本同改 {stats['days_ambiguous']} 天")
    print("\n过门槛：")
    for book, info in summary["books"].items():
        print(f"  {book:24s} 回填 {info['rows']:3d} 条 / {info['days']:2d} 天 / "
              f"跨度 {info['span_days']:3d} 天 ⇒ 进池后剩 {info['pool_rows']:3d} 条")
    if summary["rejected_books"]:
        print("不够门槛（不回填）：", summary["rejected_books"])
    if summary["skipped"]:
        print("逐行跳过：", summary["skipped"])
    print(f"\n合计 {summary['total_rows']} 行")

    if args.dry_run or not args.apply:
        print("\n（dry-run：未改库。加 --apply 才写）")
        return 0

    n = _apply(conn, plan, Path(args.log))
    print(f"已回填 {n} 行，回滚清单：{args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
