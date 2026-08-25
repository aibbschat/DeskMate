#!/usr/bin/env python3
"""圣经审阅 CLI：列出某书待作者确认的条目，并支持确认 / 驳回。

信任分层落地后，所有「非书稿直引」（对话 / 圆桌回流）的条目都标 needs_review=1，
需作者人工核对史实后再确认或撤销。本脚本是人与圣经之间的手动闸门。

用法：
    python scripts/review_bible.py /path/to/小说目录           # 列出待确认条目
    python scripts/review_bible.py /path/to/小说目录 --accept character:回流乙
    python scripts/review_bible.py /path/to/小说目录 --reject  character:回流乙

fail-open：该书未建圣经或路径无效时只打印提示并退出，不报错中断。
"""
from __future__ import annotations

import argparse
import sys

from deskmate.novel_memory import core as nc
from deskmate.novel_memory import entities as ent


def main() -> int:
    ap = argparse.ArgumentParser(description="圣经审阅：列出 / 确认 / 驳回待确认条目")
    ap.add_argument("folder", help="小说目录或单稿件文件（需已建圣经）")
    ap.add_argument("--accept", metavar="ID", help="确认某条目（使其不再待审阅）")
    ap.add_argument("--reject", metavar="ID", help="驳回并撤销某条目（标记为 rejected）")
    args = ap.parse_args()

    bible_dir = nc._bible_root(args.folder) / nc._BIBLE_DIR
    if not ent.has_data(bible_dir):
        print(f"该书尚未建圣经（无 {bible_dir}），无需审阅。", file=sys.stderr)
        return 0

    conn = ent.connect(bible_dir)
    try:
        if args.accept:
            ok = ent.review_resolve(conn, args.accept, accept=True)
            print("已确认：" if ok else "未找到条目：", args.accept)
            return 0 if ok else 1
        if args.reject:
            ok = ent.review_resolve(conn, args.reject, accept=False)
            print("已撤销：" if ok else "未找到条目：", args.reject)
            return 0 if ok else 1

        report = ent.review_report(conn)
        print(report)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
