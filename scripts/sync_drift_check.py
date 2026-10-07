#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端窗口内的集合级 drift 检测（2026-10-07；原Mac 侧 `_check_drift` 的正确归属）。

## 为什么从 Mac 挪到云端

Mac 侧常驻 tick 只在**对端 daemon 在线时**才有意义，而错峰常驻 A 方案下云端 sync daemon
仅每小时 :02 启动一次（窗口约 54s）。于是 Mac 的 tick 几乎全败：

- 指数退避封顶 240s，与~54s 的窗口互相错开；
- 实测 tick 落点 ``11:59:39 → 12:03:54`` 恰好跳过整个 12:02:00–12:02:54 窗口；
- 游标 ``push_after_seq`` 冻结在 1676 两天零推进；
- **`_check_drift` 挂在 tick 成功之后** → Mac 端 ``sync_health``/``drift`` 生产者实际休眠。

而"做对账"这件事本身，云端窗口才是正确执行方：**它在 pull Mac 全量**（`pull_snapshot`
拿到 Mac 每张表的全部 ``record_id``），Mac 侧只是在被 pull。故drift 判定挪到此处：

- 判据口径**完全不变**：逐表 ``record_id`` 对称差（``remote_ids ^ local_ids``），
  非零即写入 ``sync_health`` 台账（``kind='drift'``）。
- 执行方从"Mac 同步层"换成"云端窗口"，与2026-10-07 日历型 tick 改造同一批落地。

## 用法

在云端同步窗口脚本里（``/usr/local/sbin/xiaoju-sync-window.sh``，游标追平**之后**）调用::

    python3 /var/lib/openworker/.deskmate/sync/sync_drift_check.py

需要 ``sync_transport`` 指向 Mac 侧 oplog server（即 Mac 拨出的反隧道
``127.0.0.1:18765``）。fail-open：任何异常都只记日志、返回非零供调用方感知，**绝不影响窗口主流程**。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

DESKMATE_DIR = os.environ.get("DESKMATE_DIR", "/var/lib/openworker/.deskmate")
LOG_PATH = Path(DESKMATE_DIR) / "agency" / "sync_drift_check.log"
DB_PATH = os.path.join(DESKMATE_DIR, "deskmate.db")
# Mac oplog server（Mac 主动拨出的 SSH 反隧道在本机的回环端口）
PEER_BASE = os.environ.get("DESKMATE_PEER_BASE", "http://127.0.0.1:18765")

LOG = logging.getLogger("sync-drift")


def _load_pkg():
    """import 同步包。云端用平铺 ``sync`` 包，本机演练用 ``deskmate.memory``。

    云端平铺包的模块用**相对导入**（``from .sync_store_sqlite import ...``），必须以包
    方式 import，故 sys.path 指向其父目录；本机（仓库内演练）无 ``sync`` 目录，退回
    ``deskmate.memory``，两者 API 同形。
    """
    pkg_parent = os.path.dirname(DESKMATE_DIR.rstrip("/"))
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)
    try:
        from sync import sync_daemon as sd  # noqa: PLC0415
        from sync import sync_store_sqlite as sql  # noqa: PLC0415
        from sync import sync_transport as st  # noqa: PLC0415
    except ImportError:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from deskmate.memory import sync_daemon as sd  # noqa: PLC0415
        from deskmate.memory import sync_store_sqlite as sql  # noqa: PLC0415
        from deskmate.memory import sync_transport as st  # noqa: PLC0415

    return sd, sql, st


def check_drift(threshold: int = 0) -> dict:
    """逐表做 record_id 对称差；非零表写入 ``sync_health`` 台账。

    返回 ``{"checked": [...], "drift": {...}, "recorded": bool}``；异常时抛给调用方
    由 ``main`` 兜住（fail-open）。
    """
    import sqlite3 as _s

    sd, sql, st = _load_pkg()

    conn = _s.connect(DB_PATH, timeout=15)
    try:
        tables = list(sql.SYNC_TABLES)
        stores = {}
        for t in tables:
            st_ = sql.SQLiteSyncStore(conn, table=t)
            stores[t] = st_
        transport = st.HttpOplogTransport(PEER_BASE)

        drift_map = {}
        checked = []
        for t in tables:
            try:
                snap = transport.pull_snapshot(t)
            except Exception as exc:  # noqa: BLE001 对端不可达 → 该表跳过
                LOG.warning("drift: 拉表 %s 快照失败（跳过）: %r", t, exc)
                continue
            checked.append(t)
            remote_ids = {
                (r.get("record_id") if isinstance(r, dict) else getattr(r, "record_id", None))
                for r in snap
            }
            remote_ids.discard(None)
            local_ids = {r.record_id for r in stores[t].items()}
            delta = len(remote_ids ^ local_ids)
            if delta > threshold:
                drift_map[t] = delta
        LOG.info("drift: 已检查 %d 张表，差异 %s", len(checked), json.dumps(drift_map, ensure_ascii=False))

        recorded = False
        if drift_map:
            recorded = _record_drift(drift_map)
        return {"checked": checked, "drift": drift_map, "recorded": recorded}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _record_drift(drift_map: dict) -> bool:
    """把 drift 写进 ``sync_health`` 台账（复用现有健康生产者口径）。

    云端若装了 ``deskmate.proactivity``（全包部署）就走 ``run_producer``，与Mac/agency
    tick 写的是同一张台账；纯 sync 平铺包无该模块时降级为仅记日志。
    """
    try:
        sys.path.insert(0, "/opt/openworker")
        from deskmate.proactivity.ledger import run_producer

        run_producer("sync_health", ctx={"drift": drift_map}, base_dir=DESKMATE_DIR)
        LOG.info("drift: 已写入 sync_health 台账 %s", drift_map)
        return True
    except Exception as exc:  # noqa: BLE001 无 proactivity 模块 = 预期降级
        LOG.info("drift: proactivity 不可用（纯 sync 包），仅记录日志: %r", exc)
        return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    LOG.info("=== drift 检测开始 (peer=%s) ===", PEER_BASE)
    try:
        res = check_drift()
        LOG.info(
            "=== drift 检测完成: 检查 %d 表, 差异 %s, 已记账=%s ===",
            len(res["checked"]), json.dumps(res["drift"], ensure_ascii=False), res["recorded"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 fail-open：绝不因drift 检测打断窗口
        LOG.exception("drift 检测异常（不影响窗口主流程）: %r", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())