#!/usr/bin/env python3
"""deskmate_doctor —— `~/.deskmate` 体检（只读，不删任何东西）。

用法:
    python scripts/deskmate_doctor.py [--base ~/.deskmate] [--json]

输出：
- 各子目录体积（Top N）
- 主库各表行数（Top N）
- 备份目录清单与份数
- 行为日志行数 / 体积
- 仓内超大文件（>1500 行）Top N

对应文档 #27「数据目录卫生」第 1 条；备份清理见 prune_deskmate_backups.py。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _du(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _fmt(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024.0
    return f"{n:.1f}P"


def scan(base: Path, repo: Path) -> dict:
    report: dict = {"base": str(base)}
    if not base.is_dir():
        report["error"] = "目录不存在"
        return report
    dirs = []
    for child in sorted(base.iterdir()):
        if child.is_dir():
            dirs.append({"name": child.name, "bytes": _du(child)})
        else:
            try:
                dirs.append({"name": child.name, "bytes": child.stat().st_size})
            except OSError:
                pass
    dirs.sort(key=lambda d: d["bytes"], reverse=True)
    report["entries"] = dirs

    db = base / "deskmate.db"
    tables = []
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                names = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                for name in names:
                    try:
                        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    except sqlite3.Error:
                        continue
                    tables.append({"name": name, "rows": int(n)})
            finally:
                conn.close()
        except sqlite3.Error as exc:
            report["db_error"] = str(exc)
    tables.sort(key=lambda t: t["rows"], reverse=True)
    report["tables"] = tables

    backups = list(base.glob("**/*.bak")) + list(base.glob("**/*.bak.*"))
    report["backups"] = [{"path": str(p), "bytes": p.stat().st_size} for p in backups if p.is_file()]
    report["backups_compressed"] = [
        {"path": str(p), "bytes": p.stat().st_size} for p in base.glob("**/*.bak.gz")
    ]

    blog = base / "behavior_log.jsonl"
    if blog.is_file():
        size = blog.stat().st_size
        lines = 0
        try:
            with open(blog, "r", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    lines += 1
        except OSError:
            pass
        report["behavior_log"] = {"lines": lines, "bytes": size}

    big = []
    if repo.is_dir():
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "target", "dist"}]
            for name in files:
                if not name.endswith((".py", ".ts", ".tsx")):
                    continue
                fp = Path(root) / name
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        n = sum(1 for _ in f)
                except OSError:
                    continue
                if n > 1500:
                    big.append({"path": str(fp.relative_to(repo)), "lines": n})
    big.sort(key=lambda x: x["lines"], reverse=True)
    report["huge_files"] = big[:12]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="deskmate 数据目录体检（只读）")
    ap.add_argument("--base", default=str(Path.home() / ".deskmate"))
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = scan(Path(args.base).expanduser(), Path(args.repo))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"# {report['base']}")
    if report.get("error"):
        print(report["error"])
        return 1
    print("\n## 目录/文件体积（Top）")
    for e in report["entries"][:12]:
        print(f"  {_fmt(e['bytes']):>8}  {e['name']}")
    print("\n## 主库表行数（Top）")
    for t in report["tables"][:12]:
        print(f"  {t['rows']:>9,}  {t['name']}")
    print(f"\n## 备份：{len(report['backups'])} 份未压缩 / {len(report['backups_compressed'])} 份已压缩")
    for b in sorted(report["backups"], key=lambda x: -x["bytes"])[:8]:
        print(f"  {_fmt(b['bytes']):>8}  {b['path']}")
    if report.get("behavior_log"):
        bl = report["behavior_log"]
        print(f"\n## 行为日志：{bl['lines']:,} 行 / {_fmt(bl['bytes'])}")
    print("\n## 仓内超大文件（>1500 行）")
    for f in report["huge_files"]:
        print(f"  {f['lines']:>6} 行  {f['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
