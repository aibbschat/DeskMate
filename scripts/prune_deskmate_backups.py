#!/usr/bin/env python3
"""prune_deskmate_backups —— 备份保留策略（默认 **dry-run**，需 --apply 才动文件）。

只处理 `~/.deskmate` 下的备份文件：
- `*.bak`、`*.bak.<n>`（历史阶段迁移留下的全量库副本）
- 保留最新 ``--keep`` 份（按 mtime），其余：
  - 默认 gzip 原地压缩（`.bak.gz`），`--delete` 才真正删除；
  - `--keep` 份与 `history/` 目录**绝不动**。

用法：
    python scripts/prune_deskmate_backups.py --keep 2            # 预览
    python scripts/prune_deskmate_backups.py --keep 2 --apply    # 压缩其余
    python scripts/prune_deskmate_backups.py --keep 2 --apply --delete
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
from pathlib import Path


def _candidates(base: Path) -> list[Path]:
    out: list[Path] = []
    # 三种历史命名都收：foo.bak / foo.bak.<n> / foo.bak-<stamp> / foo.bak_<stamp>
    for pattern in ("**/*.bak", "**/*.bak.*", "**/*.bak-*", "**/*.bak_*"):
        for p in base.glob(pattern):
            if not p.is_file():
                continue
            if any(part == "history" for part in p.parts):
                continue  # 史料库目录绝不碰
            if p.name.endswith(".gz"):
                continue
            out.append(p)
    return sorted(set(out), key=lambda p: p.stat().st_mtime, reverse=True)


def _fmt(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="deskmate 备份清理（默认 dry-run）")
    ap.add_argument("--base", default=str(Path.home() / ".deskmate"))
    ap.add_argument("--keep", type=int, default=2, help="保留最近 N 份（默认 2）")
    ap.add_argument("--apply", action="store_true", help="真正执行（否则只打印计划）")
    ap.add_argument("--delete", action="store_true", help="删除而非 gzip（需 --apply）")
    args = ap.parse_args(argv)

    base = Path(args.base).expanduser()
    if not base.is_dir():
        print(f"目录不存在：{base}")
        return 1
    files = _candidates(base)
    keep, rest = files[: max(0, args.keep)], files[max(0, args.keep):]
    print(f"备份候选 {len(files)} 份；保留 {len(keep)} 份，处理 {len(rest)} 份")
    for p in keep:
        print(f"  保留    {_fmt(p.stat().st_size):>7}  {p}")
    reclaimed = 0
    for p in rest:
        size = p.stat().st_size
        reclaimed += size
        action = "删除" if args.delete else "gzip"
        print(f"  {action:<5} {_fmt(size):>7}  {p}")
        if not args.apply:
            continue
        try:
            if args.delete:
                p.unlink()
            else:
                target = p.with_name(p.name + ".gz")
                if target.exists():
                    continue
                with open(p, "rb") as src, gzip.open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                p.unlink()
        except OSError as exc:
            print(f"    失败：{exc}")
    print(f"\n{'已' if args.apply else '将'}回收 {_fmt(reclaimed)}"
          f"{'（dry-run，加 --apply 执行）' if not args.apply else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
