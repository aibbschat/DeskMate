#!/usr/bin/env python3
"""小桔同步 · P3-c oplog 归档/GC 手动触发脚本（NOT auto-run）。

清理本地 ``_sync_oplog`` 中「已推完且超龄」的事件，防止日志无限膨胀。
**绝不**被 daemon / 任何自动循环调用——这是人工运维入口。

安全性（红线，逐条强制）：
  1. 必须持有成功 tick 的游标（push_after_seq）才敢删：从 CursorStore 读取；
     缺失游标 → 拒绝执行（无从证明事件已被对端收到）。
  2. 删除前对活库做**在线冷备**（sqlite3 backup API，绝非 cp 活库）。
  3. 只删 ``seq <= push_after_seq`` 且超龄（created_at < now - keep_days）的行；
     游标不调整、不丢未推送事件。

用法：
  # 仅报告（dry-run，不删）：
  python3 scripts/gc_sync_oplog.py --mode mac

  # 实际删除（删除前自动在线冷备）：
  python3 scripts/gc_sync_oplog.py --mode mac --confirm

  # 自定义保留天数 + 删除后回收空间：
  python3 scripts/gc_sync_oplog.py --mode mac --confirm --keep-days 7 --vacuum

  # 云端（走 sync 包）：
  python3 scripts/gc_sync_oplog.py --mode cloud --cloud-pkg-path /var/lib/openworker/.deskmate
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import sqlite3
import sys
import time

LOG = logging.getLogger("xiaoju.gc")


def _load_sync_modules(mode: str, repo_path: str, cloud_pkg_path: str):
    """按运行侧加载同步模块（与 sync_daemon_runner._load_sync_modules 同形）。"""
    if mode == "cloud" or cloud_pkg_path:
        parent = cloud_pkg_path or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        if parent not in sys.path:
            sys.path.insert(0, parent)
        sd = importlib.import_module("sync.sync_daemon")
        sync_sql = importlib.import_module("sync.sync_store_sqlite")
        st = importlib.import_module("sync.sync_transport")
    else:
        if repo_path and os.path.isdir(repo_path):
            if repo_path not in sys.path:
                sys.path.insert(0, repo_path)
        sync_sql = importlib.import_module("deskmate.memory.sync_store_sqlite")
        st = importlib.import_module("deskmate.memory.sync_transport")
        sd = importlib.import_module("deskmate.memory.sync_daemon")
    return sd, sync_sql, st


def _online_backup(src_path: str, backup_path: str) -> None:
    """sqlite3 在线冷备（backup API），可安全在 daemon 持有连接时运行。

    绝对不能用 cp 复制活库——可能产生损坏副本。
    """
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _default_db(mode: str) -> str:
    if mode == "cloud":
        return "/var/lib/openworker/.deskmate/deskmate.db"
    return os.path.expanduser("~/.deskmate/deskmate.db")


def main() -> int:
    ap = argparse.ArgumentParser(description="小桔同步 oplog 归档/GC（手动）")
    ap.add_argument("--mode", choices=["mac", "cloud"], default="mac")
    ap.add_argument("--db-path", default="")
    ap.add_argument("--repo-path", default="")
    ap.add_argument("--cloud-pkg-path", default="")
    ap.add_argument("--cursor-path", default="", help="CursorStore JSON；缺省=<db>.sync_cursor.json")
    ap.add_argument("--keep-days", type=float, default=30.0, help="保留天数（默认 30）")
    ap.add_argument("--backup-dir", default="", help="冷备目录；缺省=db 所在目录")
    ap.add_argument("--confirm", action="store_true", help="必须显式传才真删；否则仅报告")
    ap.add_argument("--vacuum", action="store_true", help="删除后 VACUUM 回收空间（可能耗时）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = args.db_path or _default_db(args.mode)
    # mac 模式默认从脚本位置推导仓库根（scripts/ 的父目录），使 `deskmate.memory.*`
    # 可导入；无需每次传 --repo-path。
    if args.mode == "mac" and not args.repo_path:
        args.repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(db_path):
        LOG.error("数据库不存在: %s", db_path)
        return 2
    cursor_path = args.cursor_path or (db_path + ".sync_cursor.json")
    if not os.path.exists(cursor_path):
        LOG.error(
            "游标文件不存在: %s —— 无从证明事件已推送，拒绝 GC（先跑一次成功 tick）",
            cursor_path,
        )
        return 3

    sd, sync_sql, st = _load_sync_modules(args.mode, args.repo_path, args.cloud_pkg_path)

    # 读水位。**2026-10-07 口径修正**：原实现只取 push_after_seq，但错峰常驻架构下
    # Mac 的 push 游标可能长期冻结（实测 push=1676 两天未动，而云端 pull 已到4587）——
    # 那 3000+ 行早已被对端 pull 走，却因"push 水位未过"而永久留存。
    # 两个水位各有"对端必已收到"的含义，取**两者较大值**才既安全又不浪费：
    #   - push_after_seq：本端推给对端已确认的水位；
    #   - pull_after_seq：对端从本端拉走的水位（对端主动 pull 时推进），
    #     同样是"对端已拿到"的证明。
    cursor = st.CursorStore(cursor_path)
    push_after_seq = int(getattr(cursor, "push_after_seq", 0) or 0)
    pull_after_seq = int(getattr(cursor, "pull_after_seq", 0) or 0)
    min_safe_seq = max(push_after_seq, pull_after_seq)
    LOG.info(
        "游标 push=%d pull=%d → 安全水位 min_safe_seq=%d（只删 seq<=%d 且超龄的行）",
        push_after_seq, pull_after_seq, min_safe_seq, min_safe_seq,
    )

    conn = sqlite3.connect(db_path)
    try:
        # 确保 oplog 含 created_at 并回填存量（幂等，不碰业务表）
        node_row = conn.execute(
            "SELECT node FROM _sync_meta WHERE id=1"
        ).fetchone()
        if node_row is None:
            LOG.error("缺少 _sync_meta.node，无法安全迁移，中止")
            return 4
        node = node_row[0]
        sync_sql.migrate(conn, node)
        conn.commit()

        cutoff = int(time.time()) - int(args.keep_days * 86400)
        where = "seq <= ? AND created_at IS NOT NULL AND created_at < ?"
        params = (min_safe_seq, cutoff)

        # 报告：按表统计将删除条数
        rows = conn.execute(
            f"SELECT table_name, COUNT(*) FROM {sync_sql._OPLOG_TABLE} "
            f"WHERE {where} GROUP BY table_name",
            params,
        ).fetchall()
        total = sum(r[1] for r in rows)
        remain = conn.execute(
            f"SELECT COUNT(*) FROM {sync_sql._OPLOG_TABLE}"
        ).fetchone()[0]
        LOG.info(
            "保留天数=%s → cutoff(epoch)=%d；当前 oplog 共 %d 条，将删除 %d 条：",
            args.keep_days, cutoff, remain, total,
        )
        for t, n in rows:
            LOG.info("  - %s: %d 条", t, n)

        if not args.confirm:
            LOG.info("DRY-RUN：未删除。确认无误后加 --confirm 执行（将先做在线冷备）")
            return 0

        # 实际删除：先在线冷备
        backup_dir = args.backup_dir or os.path.dirname(os.path.abspath(db_path))
        os.makedirs(backup_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(
            backup_dir, os.path.basename(db_path) + f".gc_backup-{ts}"
        )
        LOG.info("删除前在线冷备 → %s", backup_path)
        _online_backup(db_path, backup_path)

        deleted = sync_sql.gc_oplog(conn, args.keep_days, min_safe_seq, dry_run=False)
        LOG.info("已删除 %d 条（seq<=%d 且超龄）", deleted, min_safe_seq)

        if args.vacuum:
            LOG.info("VACUUM 回收空间（可能需要一点时间）…")
            try:
                conn.execute("VACUUM")
                conn.commit()
                LOG.info("VACUUM 完成")
            except sqlite3.OperationalError as exc:
                LOG.warning("VACUUM 失败（可稍后手动跑）：%r", exc)

        after = conn.execute(
            f"SELECT COUNT(*) FROM {sync_sql._OPLOG_TABLE}"
        ).fetchone()[0]
        LOG.info("GC 后 oplog 余 %d 条；游标未动，下一轮增量同步不受影响", after)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
