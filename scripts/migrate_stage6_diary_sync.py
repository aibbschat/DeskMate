#!/usr/bin/env python3
"""阶段6 迁移 · 把个人中心日记/留言纳入同步范围（user_posts / comments）。

背景（reports/2026-10-05_会话日记同步范围扩大_设计方案.md）：
萧哥 10-05 拍板「日记进云端、访谈会话不同步、chat.db 本期不动」。本脚本负责
把两端 deskmate.db 的两表迁移到位：补 sync 三列 → 回填 sync_uuid/sync_hlc → 建触发器。
同步 daemon 启动时会自动跑同一套 migrate（幂等），本脚本用于**在错峰窗口内、daemon 停机时**
先行完成迁移与验收，避免 daemon 抢写。

为什么必须单独跑（两个实测事实，非想当然）：
1. **两表原本没有 sync 列**，oplog 触发器模板依赖它们 → 不迁移则触发器建不出来、
   真实 app 写入永远不会被捕获（这正是「日记没上云」的根因之一）。
2. **半回填行会让 daemon 启动即崩**：2026-10-05 实测存量 62 行「有 sync_uuid 但
   sync_hlc 为 NULL」，而 sync_store_sqlite.migrate 原先只补 sync_uuid IS NULL 的行 →
   store.items() 走 HLC.from_str(None) 直接 AttributeError。
   migrate() 已修为 sync_uuid IS NULL OR sync_hlc IS NULL，本脚本会复核该状态。

安全红线：
- 动库前强制冷备（sqlite3 backup API，禁 cp 活库），备份路径打印出来；
- 默认 dry-run，只报告不改库；确认后加 --apply 真正执行；
- 不碰 interview_sessions / chat.db（本期不同步）。

用法：
    # 预演（默认，不写库）
    python3 scripts/migrate_stage6_diary_sync.py

    # Mac 本地真库执行
    python3 scripts/migrate_stage6_diary_sync.py --db ~/.deskmate/deskmate.db --node mac --apply

    # 云端（由持钥方在服务器上执行）
    python3 scripts/migrate_stage6_diary_sync.py --db /var/lib/openworker/.deskmate/deskmate.db \\
        --node cloud --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deskmate.memory.sync_store_sqlite import (  # noqa: E402
    SYNC_TABLES,
    SQLiteSyncStore,
    migrate,
)

TARGET_TABLES = ("user_posts", "comments")
DEFAULT_DB = os.path.expanduser("~/.deskmate/deskmate.db")


def cold_backup(db_path: str) -> str:
    """用 sqlite3 backup API 冷备活库（红线：禁 cp 活库，WAL 下 cp 会拿到撕裂快照）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{db_path}.bak-stage6-{ts}"
    src = sqlite3.connect(db_path, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=20000")
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def survey(conn: sqlite3.Connection) -> dict:
    """迁移前体检：每张目标表的行数 / sync 列状态 / 触发器 / oplog 现状。"""
    rep: dict = {"db_tables_present": {}, "sync_cols": {}, "triggers": {}, "rows": {}}
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    trg = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for t in TARGET_TABLES:
        rep["db_tables_present"][t] = t in names
        if t not in names:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        rep["sync_cols"][t] = {
            c: (c in cols) for c in ("sync_uuid", "sync_hlc", "sync_tombstone")
        }
        n, nu, nh = conn.execute(
            f"SELECT COUNT(*), SUM(sync_uuid IS NULL), SUM(sync_hlc IS NULL) FROM {t}"
        ).fetchone()
        rep["rows"][t] = {"total": n, "null_sync_uuid": nu, "null_sync_hlc": nh}
        rep["triggers"][t] = sorted(x for x in trg if x.startswith(f"trg_{t}_"))
    try:
        rep["oplog"] = dict(
            conn.execute(
                "SELECT table_name, COUNT(*) FROM _sync_oplog GROUP BY table_name"
            ).fetchall()
        )
    except sqlite3.Error:
        rep["oplog"] = None
    return rep


def verify(conn: sqlite3.Connection) -> dict:
    """迁移后自检：sync 列无缺、store 读路径可跑（items() 曾因 NULL hlc 崩）。"""
    out: dict = {"ok": True, "checks": []}

    def _add(name: str, ok: bool, detail: str = "") -> None:
        out["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            out["ok"] = False

    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    _add("integrity_check", ic == "ok", str(ic))
    trg_all = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for t in TARGET_TABLES:
        n, nu, nh = conn.execute(
            f"SELECT COUNT(*), SUM(sync_uuid IS NULL), SUM(sync_hlc IS NULL) FROM {t}"
        ).fetchone()
        _add(
            f"{t}.sync 列完整",
            (nu or 0) == 0 and (nh or 0) == 0,
            f"rows={n} null_uuid={nu} null_hlc={nh}",
        )
        kinds = {
            x.rsplit("_", 1)[-1] for x in trg_all if x.startswith(f"trg_{t}_")
        }
        _add(
            f"{t}.触发器齐备",
            {"ai", "au", "ad"} <= kinds,
            ",".join(sorted(x for x in trg_all if x.startswith(f"trg_{t}_"))),
        )
        try:
            items = SQLiteSyncStore(conn, t).items()
            _add(f"{t}.store.items() 可跑", True, f"{len(items)} 条")
        except Exception as exc:  # noqa: BLE001
            _add(f"{t}.store.items() 可跑", False, repr(exc))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="阶段6 日记/留言同步范围迁移")
    p.add_argument("--db", default=DEFAULT_DB, help="deskmate.db 绝对路径")
    p.add_argument("--node", default="mac", help="节点名（mac/cloud）")
    p.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p.add_argument("--no-backup", action="store_true", help="跳过冷备（不推荐）")
    args = p.parse_args(argv)

    db = os.path.abspath(os.path.expanduser(args.db))
    if not os.path.exists(db):
        print(f"[FAIL] 库不存在：{db}")
        return 2

    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        print(f"=== 迁移前体检：{db} (node={args.node}) ===")
        print(json.dumps(survey(conn), ensure_ascii=False, indent=2))

        # 误纳入护栏：本脚本只认这 2 张表
        for t in TARGET_TABLES:
            assert t in SYNC_TABLES, f"{t} 未注册进 SYNC_TABLES，先改注册表"
        assert "interview_sessions" not in SYNC_TABLES, "访谈会话本期不应被纳入"

        if not args.apply:
            print("")
            print("[DRY-RUN] 未写库。确认无误后加 --apply 执行。")
            return 0

        if not args.no_backup:
            dst = cold_backup(db)
            print(f"\n[冷备] {dst}")

        t0 = time.time()
        rep = migrate(conn, args.node)
        conn.commit()
        print(
            f"\n[migrate] {json.dumps(rep, ensure_ascii=False)} ({time.time() - t0:.2f}s)"
        )

        # 存量回填不得进 oplog（否则首轮推送膨胀）
        polluted = conn.execute(
            "SELECT COUNT(*) FROM _sync_oplog "
            "WHERE table_name IN ('user_posts','comments')"
        ).fetchone()[0]
        print(f"[oplog] 两表 oplog 行数 = {polluted}（迁移后应为 0，回填不进 oplog）")

        v = verify(conn)
        print("")
        print("=== 迁移后自检 ===")
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
