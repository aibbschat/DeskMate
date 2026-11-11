#!/usr/bin/env python3
"""记忆文件索引 CLI（三段检索）。

    python scripts/build_memory_index.py --status
    python scripts/build_memory_index.py --backup --rebuild
    python scripts/build_memory_index.py --sync

- ``--backup``：建表/重建前把 ``deskmate.db`` 一致性地复制到
  ``~/.deskmate/backups/2026-11-11-s3/``（sqlite backup API，含 WAL）。
- ``--rebuild``：清空索引表后全量重建（源文件不动）。
- ``--status``：chunk 数、向量覆盖率、按来源分布、DB 大小。
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deskmate.paths import deskmate_base_dir  # noqa: E402
from deskmate.memory.file_index import FileIndex  # noqa: E402

BACKUP_DIR = "backups/2026-11-11-s3"


def backup_db(base: Path) -> Path:
    src = base / "deskmate.db"
    if not src.is_file():
        raise SystemExit(f"库不存在：{src}")
    out_dir = base / BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = out_dir / f"deskmate.db.before-s3-index.{ts}.bak"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    print(f"已备份：{dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, default=None)
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--embed-pending", type=int, default=0,
                    help="补齐 N 条未向量化 chunk")
    args = ap.parse_args()

    base = args.base or deskmate_base_dir()
    if args.backup:
        backup_db(base)

    idx = FileIndex(base)

    if args.rebuild:
        with idx._connect() as conn:
            conn.execute("DELETE FROM memory_chunks")
            conn.execute("DELETE FROM memory_chunks_fts")
            conn.execute("DELETE FROM memory_file_state")
        idx._matrix_cache = None
        print("已清空索引表，开始全量重建……")

    if args.sync or args.rebuild:
        t0 = time.perf_counter()
        stats = idx.sync(force=bool(args.rebuild))
        elapsed = time.perf_counter() - t0
        print(f"同步：{json.dumps(stats, ensure_ascii=False)}")
        print(f"耗时：{elapsed:.1f}s")
        if stats.get("error"):
            return 2

    if args.embed_pending:
        n = idx.embed_pending(limit=args.embed_pending)
        print(f"补齐向量：{n} 条")

    if args.status or args.rebuild or args.sync:
        st = idx.stats()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        # 停止条件（Spec）：全量 >5 分钟或 DB 增量 >50MB → 报告后再动
        if st["chunks"] and st["coverage"] < 0.99:
            print("警告：向量覆盖率不足 99%，可再跑 --embed-pending", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
