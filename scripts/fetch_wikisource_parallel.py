#!/usr/bin/env python3
"""并行版维基文库史料下载（并发 4 路，跳过已下载）。

复用 fetch_wikisource_batch 的书单与版本选择逻辑，用线程池并发下载，
显著缩短 85 部史料的整体耗时。已存在的 .txt 直接跳过（幂等续传）。

用法：
    python scripts/fetch_wikisource_parallel.py [书名...]
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_wikisource_batch as fwb  # noqa: E402
from fetch_wikisource import OUT_DIR  # noqa: E402

WORKERS = 4


def download_one(item: tuple) -> tuple[str, str]:
    prefix, title, author, _era, _cat = item
    out = OUT_DIR / f"{title}.txt"
    if out.is_file() and out.stat().st_size > 20000:
        return title, "已存在-跳过"
    try:
        p = fwb.download_best(prefix, title, author)
        if p:
            return title, "成功"
        return title, "跳过(无内容)"
    except Exception as exc:  # noqa: BLE001
        return title, f"失败({type(exc).__name__})"


def main(argv: list[str]) -> int:
    if argv:
        books = [b for b in fwb.BOOKS if any(a in b[0] or a in b[1] for a in argv)]
        if not books:
            books = [(a, a, "未标注", "?", "史书") for a in argv]
    else:
        books = list(fwb.BOOKS)

    print(f"并行下载 {len(books)} 部（{WORKERS} 线程），输出到 {OUT_DIR}\n")
    t0 = time.time()
    results: list[tuple[str, str]] = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(download_one, b): b[1] for b in books}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            title, status = fut.result()
            results.append((title, status))
            print(f"  [{i}/{len(books)}] {title}: {status}", flush=True)

    print(f"\n完成：{len(results)} 部 / 耗时 {time.time()-t0:.0f}s")
    ok = [t for t, s in results if s == "成功"]
    skip = [t for t, s in results if s == "已存在-跳过"]
    fail = [(t, s) for t, s in results if s.startswith("失败")]
    empty = [t for t, s in results if s.startswith("跳过")]
    print(f"  成功 {len(ok)} / 已存在 {len(skip)} / 无内容 {len(empty)} / 失败 {len(fail)}")
    if fail:
        print("  失败清单：")
        for t, s in fail:
            print(f"    ✗ {t}: {s}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
