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
import importlib
import json
import os
import sqlite3
import sys
import time


def _load_sync_store(pkg_parent: str):
    """按部署形态加载同步适配器（两端包结构不同，别写死一种）。

    - Mac：仓库根下 deskmate.memory.sync_store_sqlite（本脚本在 scripts/，父目录即仓库根）；
    - 云端：sync/ 平铺部署（/var/lib/openworker/.deskmate），由 --pkg-parent 指定其父目录。

    两端代码同源、只是 import 路径不同，故按可用性择一，而非在云端补装整个 deskmate 包。
    """
    if pkg_parent:
        if pkg_parent not in sys.path:
            sys.path.insert(0, pkg_parent)
        try:
            return importlib.import_module("sync.sync_store_sqlite")
        except ModuleNotFoundError:
            pass
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    return importlib.import_module("deskmate.memory.sync_store_sqlite")

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


def verify(conn: sqlite3.Connection, store_cls) -> dict:
    """迁移后自检：sync 列无缺、store 读路径可跑（items() 曾因 NULL hlc 崩）。

    store_cls 显式传入而非模块全局：同步适配器按部署形态动态导入（Mac 的
    deskmate.memory vs 云端平铺的 sync），做成全局会在 verify() 里 NameError。
    """
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
            items = store_cls(conn, t).items()
            _add(f"{t}.store.items() 可跑", True, f"{len(items)} 条")
        except Exception as exc:  # noqa: BLE001
            _add(f"{t}.store.items() 可跑", False, repr(exc))
    return out


def seed_baseline_ops(conn: sqlite3.Connection, sync_store, node: str,
                      tables=TARGET_TABLES) -> dict:
    """把两表存量**显式追加为 oplog 事件**，使基线数据真的能推到对端。

    为什么需要这一步（别当成冗余——这是本批能不能生效的关键）：
    migrate 的存量回填只补 sync_uuid，**刻意不进 oplog**（沿用阶段 1/2 边界，
    免得存量行灌进 oplog）。但「不进 oplog」的另一面是——对端本就缺的这批存量
    永远补不上：首连基线 reconcile 只在**两端游标都归零**时才跑
    （sync_daemon_runner.py::_need_baseline），而实测两端游标已是
    1394/1395/1397 → 基线不触发 → 云端缺失的日记将永久停留（正是本次要修的缺口）。

    故由**权威端（Mac）**把存量补成显式 op，走正常增量路径推给对端：
    对端 apply_remote 按 record_id upsert，幂等；本端无需回写。
    - 幂等：已有同 (table, record_id) 的 op 则跳过，可重跑；
    - 不改业务数据、不动 sync 列，纯追加 oplog；
    - 只在权威端跑（--seed-baseline）；对端跑会把 Mac 已有记录重推，无害但无意义。
    """
    HLC = sync_store.HLC
    Op = sync_store.Op
    oplog = sync_store.SQLiteOplogStore(conn)
    store_cols = sync_store.SYNC_TABLES

    # 已推送过的 record_id：避免重复播种（幂等重跑）
    existing: dict = {}
    for t in tables:
        for (rid,) in conn.execute(
            "SELECT record_id FROM _sync_oplog WHERE table_name=?", (t,)
        ):
            existing.setdefault(t, set()).add(rid)

    out = {}
    for t in tables:
        store = sync_store.SQLiteSyncStore(conn, t)
        cols = list(store_cols[t]["business_cols"])
        rows = conn.execute(
            "SELECT sync_uuid, %s FROM %s WHERE sync_tombstone=0 ORDER BY sync_uuid"
            % (", ".join(cols), t)
        ).fetchall()
        seeded = skipped = 0
        for row in rows:
            rid = row[0]
            data = dict(zip(cols, row[1:]))
            rec = store.get(rid)
            hlc = rec.hlc if rec is not None else HLC.now(node)
            if rid in existing.get(t, set()):
                skipped += 1
                continue
            oplog.append(Op(t, rid, "upsert", hlc, data, origin=node))
            seeded += 1
        out[t] = {"seeded": seeded, "already_present": skipped, "total": len(rows)}
    conn.commit()
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="阶段6 日记/留言同步范围迁移")
    p.add_argument("--db", default=DEFAULT_DB, help="deskmate.db 绝对路径")
    p.add_argument("--node", default="mac", help="节点名（mac/cloud）")
    p.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p.add_argument("--no-backup", action="store_true", help="跳过冷备（不推荐）")
    p.add_argument(
        "--pkg-parent",
        default="",
        help="含 sync/ 包的父目录（云端平铺部署用，如 /var/lib/openworker/.deskmate）；"
        "留空=按 Mac 仓库根导入 deskmate.memory",
    )
    p.add_argument(
        "--seed-baseline",
        action="store_true",
        help="把存量显式追加为 oplog 事件（只在权威端跑，如 Mac）。"
        "迁移只回填 uuid 不进 oplog，若对端本就缺这批存量则纯增量补不上"
        "（基线对账只在两端游标归零时触发，而实测游标已非零）。",
    )
    args = p.parse_args(argv)

    sync_store = _load_sync_store(args.pkg_parent)
    sync_tables = sync_store.SYNC_TABLES
    migrate = sync_store.migrate

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
            assert t in sync_tables, f"{t} 未注册进 SYNC_TABLES，先改注册表"
        assert "interview_sessions" not in sync_tables, "访谈会话本期不应被纳入"

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

        # 存量回填不得进 oplog（否则首轮推送膨胀）——故此处必须为 0；
        # 之后的 --seed-baseline 才会按需追加（那是显式播种，不是回填副作用）。
        polluted = conn.execute(
            "SELECT COUNT(*) FROM _sync_oplog "
            "WHERE table_name IN ('user_posts','comments')"
        ).fetchone()[0]
        print(
            f"[oplog] 两表 oplog 行数 = {polluted}（应为 0：回填不进 oplog，"
            "播种只在 --seed-baseline 时发生）"
        )
        if polluted and not args.seed_baseline:
            print(
                "[WARN] 回填阶段两表已有 oplog 行——若非 --seed-baseline 所为，"
                "说明曾有人手工写库，请核查后再继续。"
            )

        if args.seed_baseline:
            seeded = seed_baseline_ops(conn, sync_store, args.node)
            print(f"\n[seed-baseline] {json.dumps(seeded, ensure_ascii=False)}")
        elif args.apply:
            print(
                "\n[提示] 未加 --seed-baseline：对端本就缺的存量不会被推送"
                "（基线对账只在两端游标归零时触发）。"
            )

        v = verify(conn, sync_store.SQLiteSyncStore)
        print("")
        print("=== 迁移后自检 ===")
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
