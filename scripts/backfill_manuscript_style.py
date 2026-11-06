#!/usr/bin/env python3
"""把 scholar-shi.db 里萧盛的著作回灌进文风画像的「书稿桶」。

背景（2026-11-01）：文风指纹改为分桶后（manuscript/chat），书稿桶一直是空的——
每日读书任务只沉淀读后感（summary/insight），书的原文从未喂过 style_profile。
萧盛的 7 部专著早已切块入 scholar-shi.db，直接从库里取原文回灌，免 .doc/.docx 抽取。

幂等：书稿样本按内容 sha1 去重（style_profile_store 内建），重跑不刷样本数。
新书经 build_scholar_index.py 入库后重跑本脚本即可增量补充。

用法：
  # 从 scholar-shi.db 回灌（默认）
  cd /Users/xiaocheng/workspace/openworker && .venv/bin/python scripts/backfill_manuscript_style.py

  # 单本补充：直接喂一份正文（txt/md/docx 均可），可跳过已入桶的头部字数
  .venv/bin/python scripts/backfill_manuscript_style.py \
    --title "3.大清钱王·第一部" --text "/path/to/正文.txt"

  # 妖怪录此前只进了前 5 万字，补剩余部分
  .venv/bin/python scripts/backfill_manuscript_style.py \
    --title "11.中国妖怪录" --text "/path/to/原文.txt" --skip-chars 50000
"""
import argparse
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


def _read_text(path: str) -> str:
    """读正文：txt/md 直接读，docx/doc 走 writing 的抽取器（与「读稿给看法」同口径）。"""
    path = os.path.expanduser(path)
    if path.lower().endswith((".txt", ".md")):
        return open(path, encoding="utf-8", errors="replace").read()
    from deskmate.writing import read_text_from_path

    return read_text_from_path(path)


def _ingest_blocks(title: str, text: str, skip_chars: int = 0) -> None:
    """按 4 万字切块喂书稿桶；skip_chars 用于跳过已在桶里的头部。"""
    from deskmate.style_profile_store import update_style_profile

    blocks = [
        text[i : i + BLOCK_CHARS] for i in range(skip_chars, len(text), BLOCK_CHARS)
    ]
    before = _bucket_updates()
    for i, block in enumerate(blocks, 1):
        update_style_profile(
            block, source=f"书稿-{title}·块{i}/{len(blocks)}", bucket="manuscript"
        )
    ingested = _bucket_updates() - before
    print(
        f"- 《{title}》：{len(text)} 字"
        f"{f'（跳过前 {skip_chars} 字）' if skip_chars else ''} / {len(blocks)} 块 "
        f"→ 新增 {ingested}/{len(blocks)} 个样本（重复块自动去重）"
    )


def main() -> int:
    from deskmate.style_profile_store import (
        format_style_fingerprint,
        manuscript_fingerprint_available,
    )

    ap = argparse.ArgumentParser(description="回灌书稿桶")
    ap.add_argument("--text", help="单本正文路径（txt/md/docx）；给了就不再走 scholar-shi.db")
    ap.add_argument("--title", default="", help="书名（配合 --text）")
    ap.add_argument("--skip-chars", type=int, default=0, help="跳过正文头部字数（已在桶里的部分）")
    args = ap.parse_args()

    if args.text:
        if not args.title:
            print("--text 需要配合 --title 使用", file=sys.stderr)
            return 1
        text = _read_text(args.text)
        if len(text) < 500:
            print(f"跳过《{args.title}》：正文过短（{len(text)} 字）")
            return 0
        _ingest_blocks(args.title, text, args.skip_chars)
        print(f"\n书稿桶可用：{manuscript_fingerprint_available()}")
        return 0

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
