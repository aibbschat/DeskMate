#!/usr/bin/env python3
"""把 scholar-shi.db 里萧盛的著作回灌进文风画像的「书稿桶」。

背景（2026-11-01）：文风指纹改为分桶后（manuscript/chat），书稿桶一直是空的——
每日读书任务只沉淀读后感（summary/insight），书的原文从未喂过 style_profile。
萧盛的 7 部专著早已切块入 scholar-shi.db，直接从库里取原文回灌，免 .doc/.docx 抽取。

幂等：书稿样本按内容 sha1 去重（style_profile_store 内建），重跑不刷样本数。
新书经 build_scholar_index.py 入库后重跑本脚本即可增量补充。

用法：
  cd /Users/xiaocheng/workspace/openworker && .venv/bin/python scripts/backfill_manuscript_style.py
"""
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.expanduser("~/.deskmate/history/scholar-shi.db")
BLOCK_CHARS = 40_000  # 每块作为一个独立样本（update_style_profile 单次截断 5 万字）


def _chunks_of_book(conn: sqlite3.Connection, article_id: int) -> str:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE article_id=? ORDER BY chunk_index, id",
        (article_id,),
    ).fetchall()
    return "\n".join((r[0] or "").strip() for r in rows if r[0] and r[0].strip())


def _bucket_updates() -> int:
    """当前书稿桶已累计的样本数（用于报告真实增量：去重块不计入）。"""
    from deskmate.style_profile_store import DEFAULT_PROFILE_PATH, load_profile

    data = load_profile(DEFAULT_PROFILE_PATH)
    buckets = data.get("buckets") if isinstance(data.get("buckets"), dict) else {}
    entry = buckets.get("manuscript") if isinstance(buckets.get("manuscript"), dict) else {}
    m = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    return int(m.get("n", 0) or 0)


def main() -> int:
    from deskmate.style_profile_store import (
        format_style_fingerprint,
        manuscript_fingerprint_available,
        update_style_profile,
    )

    if not os.path.exists(DB_PATH):
        print(f"scholar-shi.db 不存在：{DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB_PATH)
    books = conn.execute(
        "SELECT a.id, a.title, COUNT(c.id) FROM articles a "
        "JOIN chunks c ON c.article_id = a.id "
        "WHERE a.author LIKE '%萧盛%' AND a.title NOT LIKE '附录%' "
        "GROUP BY a.id ORDER BY a.id"
    ).fetchall()
    if not books:
        print("库里没有萧盛著作（author=萧盛），无从回灌", file=sys.stderr)
        return 1

    total_chars = 0
    for article_id, title, n_chunks in books:
        text = _chunks_of_book(conn, article_id)
        if len(text) < 500:
            print(f"- 跳过《{title}》：正文过短（{len(text)} 字）")
            continue
        blocks = [text[i : i + BLOCK_CHARS] for i in range(0, len(text), BLOCK_CHARS)]
        before = _bucket_updates()
        for i, block in enumerate(blocks, 1):
            update_style_profile(
                block, source=f"书稿-{title}·块{i}/{len(blocks)}", bucket="manuscript"
            )
        ingested = _bucket_updates() - before
        total_chars += len(text)
        print(f"- 《{title}》：{len(text)} 字 / {n_chunks} 块 → 新增 {ingested}/{len(blocks)} 个样本（重复块自动去重）")

    entry_ok = manuscript_fingerprint_available()
    print(f"\n共回灌 {total_chars} 字；书稿桶可用：{entry_ok}")
    print("\n=== 书稿口径量化指纹 ===")
    print(format_style_fingerprint())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"backfill FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
