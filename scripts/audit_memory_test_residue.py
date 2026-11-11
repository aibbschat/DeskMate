#!/usr/bin/env python3
"""审计 coworker.db 里的测试残留（只读，不删除）。

修正 2026-11-11 首次清理暴露的两个判据边界：

1. ``workspace == "/private/tmp"`` / ``"/tmp"`` 本身也是临时目录，旧判据
   要求带尾部斜杠（``startswith("/private/tmp/")``）会漏掉它们；
2. 仓库内 ``.pytest_tmp/``（pytest 的 basetemp）同样是测试目录，旧判据只认
   ``/var/folders`` 与 ``/private/tmp``，漏了 4 行夹具圣经。

用法：
    python3 scripts/audit_memory_test_residue.py
    python3 scripts/audit_memory_test_residue.py --db /path/to/coworker.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / ".config" / "coworker" / "coworker.db"

_TEMP_ROOTS = (
    "/private/var/folders/",
    "/var/folders/",
    "/private/tmp/",
    "/tmp/",
    "/var/tmp/",
)
_TEMP_EXACT = {"/private/tmp", "/tmp", "/var/tmp"}
_PYTEST_MARKERS = ("/.pytest_tmp/", "/pytest-of-")


def is_temp_workspace(workspace: str | None) -> bool:
    """workspace 是否指向临时/pytest 目录（含精确匹配与仓库内 .pytest_tmp）。"""
    w = (workspace or "").strip()
    if not w:
        return False
    if w in _TEMP_EXACT or w.startswith(_TEMP_ROOTS):
        return True
    return any(m in w for m in _PYTEST_MARKERS)


def is_novel_key(key: str | None) -> bool:
    return (key or "").startswith("deskmate-novel:")


def classify(rows: list[dict]) -> dict[str, list[dict]]:
    novel = [r for r in rows if is_novel_key(r.get("key"))]
    temp = [r for r in rows if is_temp_workspace(r.get("workspace"))]
    both = [r for r in rows if is_novel_key(r.get("key")) and is_temp_workspace(r.get("workspace"))]
    return {
        "total": rows,
        "novel_key": novel,
        "temp_ws": temp,
        "both": both,
        "novel_only": [r for r in novel if r not in both],
        "temp_only": [r for r in temp if r not in both],
        "neither": [r for r in rows if r not in novel and r not in temp],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select id, scope, key, workspace, created_at, content from memories order by id")]
    con.close()

    p = classify(rows)
    print(f"db={args.db}")
    print(f"total={len(p['total'])}  novel_key={len(p['novel_key'])}  "
          f"temp_ws={len(p['temp_ws'])}  intersection={len(p['both'])}")
    print(f"novel_only={len(p['novel_only'])}  temp_only={len(p['temp_only'])}  "
          f"neither={len(p['neither'])}")

    for label in ("novel_only", "temp_only"):
        if not p[label]:
            continue
        print(f"\n-- {label}（弱命中，需人工确认）--")
        for r in p[label]:
            print(f"  [{r['id']}] {r['workspace']}  {(r['content'] or '')[:90]}")


if __name__ == "__main__":
    main()
