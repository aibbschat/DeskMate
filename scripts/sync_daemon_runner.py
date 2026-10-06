#!/usr/bin/env python3
"""小桔 B 类记忆 · 常驻同步 daemon 运行器（阶段 1 常驻化）。

让 Mac 与云端大脑（81.69）的 ``profile_items`` 持续双向收敛，而非手动触发。

拓扑（与 deskmate/memory/sync_transport.py 一致，两端只拨出、不公网监听）：
- Mac（local-first 主场）：本机 ``127.0.0.1:<local_port>`` 起 oplog HTTP 端点 +
  拨出 SSH 反隧道（Mac 发起，唯一出网通道）。隧道内含两条转发：
    * ``-R 127.0.0.1:<remote_bind_port>:127.0.0.1:<local_port>``：云端回环达 Mac；
    * ``-L 127.0.0.1:<fwd_port>:127.0.0.1:<remote_port>``：Mac 回环达云端。
  Mac daemon 经 ``http://127.0.0.1:<fwd_port>`` 拉/推云端 oplog。
- 云端：本机 ``127.0.0.1:<remote_port>`` 起 oplog HTTP 端点（不拨隧道）；
  cloud daemon 经 ``http://127.0.0.1:<remote_bind_port>``（Mac 的 -R 转发）拉/推 Mac oplog。

安全红线：
- 同步端口只在回环监听；SSH 远端绑定强制 ``127.0.0.1``（绝不 0.0.0.0）。
- fail-open：隧道/对端宕仅记日志、不崩主进程；游标持久化、重启续传。
- **绝不启动 openworker-server**（避免与同步 daemon 双写抢锁）；云端若检测到
  openworker-server 在跑，直接拒绝启动并退出。
- 阶段 1 只动 ``profile_items`` 一张 B 类表；阶段 2 扩展为**按表注册**（SYNC_TABLES，
默认迁移全部已注册表：profile_items / learned_history / loved_entries），不改其他表、不碰上游代码。

捕获机制（阶段 2 关键）：真实 app 写这些表（LearnedStore / DiaryStore / cognitive 等）
经**数据库触发器**自动进 ``_sync_oplog``，运行器不必改 app。触发器写入前把 ``_sync_meta.suppress``
置 1，避免引擎回写远端 op 时回声互弹。

首连基线（阶段 2 关键）：存量迁移只回填 sync_uuid、不进 oplog（与阶段 1 一致），故两端存量
若不一致，纯增量 tick 不会收敛（二蛋 2026-10-03 实测边界）。运行器在**两端游标都还是 0** 时
（即首次启动 / 全新部署）逐表拉对端 ``/snapshot`` 快照，做 ``reconcile_baseline_oplog``
一次性全量对账，使存量双向收敛；之后切回纯增量。幂等可重跑。

已知边界（不藏着）：
- oplog GC / 墓碑业务过滤不在本批（按萧哥决策「先聚焦扩大覆盖」）；遗忘复活防护由引擎 LWW
  + 墓碑逻辑承担，逻辑层不主动 GC 墓碑行。

用法：
  # Mac（控制端，拨隧道）
  python3 scripts/sync_daemon_runner.py --side mac \\
      --db-path ~/.deskmate/deskmate.db --repo-path /Users/xiaocheng/workspace/openworker \\
      --ssh-host 81.69.163.170 --ssh-user ubuntu \\
      --identity-file /Users/xiaocheng/workspace/openworker/.workbuddy/server/aibbs.pem

  # 云端（被控端，只起本地 server + daemon，隧道由 Mac 拨来）
  python3 /var/lib/openworker/.deskmate/sync_runner.py --side cloud \\
      --db-path /var/lib/openworker/.deskmate/deskmate.db \\
      --cloud-pkg-path /var/lib/openworker/.deskmate
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import sqlite3
import sys
import time

LOG = logging.getLogger("xiaoju.sync")


def _load_sync_modules(mode: str, repo_path: str, cloud_pkg_path: str):
    """按运行侧加载同步模块（Mac 走 deskmate.memory 包，云端走 sync 包）。

    返回 (sd, sync_sql, st)：协议核心 / SQLite 适配器 / 传输层。
    """
    if mode == "cloud" or cloud_pkg_path:
        # 云端：sync/ 包所在父目录加入 path，以 ``sync.*`` 导入（保证相对 import 解析）
        parent = cloud_pkg_path or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent not in sys.path:
            sys.path.insert(0, parent)
        sd = importlib.import_module("sync.sync_daemon")
        sync_sql = importlib.import_module("sync.sync_store_sqlite")
        st = importlib.import_module("sync.sync_transport")
    else:
        if repo_path and repo_path not in sys.path:
            sys.path.insert(0, repo_path)
        sd = importlib.import_module("deskmate.memory.sync_daemon")
        sync_sql = importlib.import_module("deskmate.memory.sync_store_sqlite")
        st = importlib.import_module("deskmate.memory.sync_transport")
    return sd, sync_sql, st


def _cloud_server_running(db_path: str) -> bool:
    """云端护栏：若 openworker-server 在跑，双写会抢锁，拒绝启动。"""
    # 仅云端系统有 systemctl；其他环境跳过该检查（返回 False 表示未检出冲突）。
    from subprocess import run

    try:
        r = run(
            ["systemctl", "is-active", "--quiet", "openworker-server.service"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _last_persist_source_best_effort() -> str:
    """尽力读取 cognitive.store 记录的最近一次 persist 调用方。

    persist 通常由 OpenWorker 主进程调用，与同步 daemon 不同进程，daemon 侧
    读不到时如实返回 n/a，避免误导排查。
    """
    try:
        if "deskmate.cognitive.store" in sys.modules:
            st = sys.modules["deskmate.cognitive.store"]
            s = getattr(st, "_LAST_PERSIST_SOURCE", None)
            if s:
                return f"{s.get('module')}:{s.get('func')}:{s.get('line')}"
    except Exception:
        pass
    return "n/a（persist 在其他进程）"


class SyncNode:
    """一侧常驻同步节点：server +（可选）隧道 + daemon 循环。

    循环被拆成 ``_tick_once`` / ``_ensure_tunnel`` 以便单测覆盖重拨逻辑。
    """

    def __init__(
        self,
        side: str,
        sd,
        sync_sql,
        st,
        *,
        db_path: str,
        node: str,
        table: str = "",
        local_port: int = 8765,
        remote_port: int = 8765,
        fwd_port: int = 18766,
        remote_bind_port: int = 18765,
        ssh_host: str = "",
        ssh_user: str = "",
        ssh_port: int = 22,
        identity_file: str = "",
        interval: float = 15.0,
        cursor_path: str = "",
        log_path: str = "",
    ) -> None:
        self.side = side
        self.sd = sd
        self.sync_sql = sync_sql
        self.st = st
        self.db_path = db_path
        self.node = node
        self.table = table
        self.local_port = local_port
        self.remote_port = remote_port
        self.fwd_port = fwd_port
        self.remote_bind_port = remote_bind_port
        self.interval = interval
        self.cursor_path = cursor_path or (db_path + ".sync_cursor.json")
        self._stop = False
        self._consecutive_fail = 0

        # 1) 迁移（默认全部已注册表；库中不存在的表自动跳过，向前兼容最小化库）。
        #    阶段 2：profile_items + learned_history + loved_entries 一并上线。
        conn = sqlite3.connect(db_path)
        try:
            res = sync_sql.migrate(conn, node, table or None)
            conn.commit()
        finally:
            conn.close()
        self.migrated = res["tables"]
        if res.get("skipped"):
            LOG.warning("以下已注册表不在库中、已跳过：%s", res["skipped"])

        # 2) 存储 / 引擎（短连接适配器，跨线程安全）。
        #    多表引擎：{表: SQLiteSyncStore}，共享一个 oplog（按 op.table 路由）。
        conn = sqlite3.connect(db_path, check_same_thread=False)
        oplog = sync_sql.SQLiteOplogStore(conn)
        stores = {t: sync_sql.SQLiteSyncStore(conn, t) for t in self.migrated}
        engine = sd.SyncEngine(stores, oplog, node)

        # 3) 本机 oplog HTTP 端点（仅回环），附带 stores 供 /snapshot 首连基线对账；
        #    engine 在收到对端 push 后立即 apply（「push 即落地」，解耦 apply 与 pull 周期）。
        self.server = st.HttpOplogServer(
            oplog, host="127.0.0.1", port=local_port, stores=stores, engine=engine
        )

        # 4) 传输：Mac 达云端（-L 转发），云端达 Mac（-R 转发）
        if side == "mac":
            self.transport = st.HttpOplogTransport(f"http://127.0.0.1:{fwd_port}")
            self.tunnel = st.SshTunnel(
                host=ssh_host,
                user=ssh_user,
                ssh_port=ssh_port,
                local_port=local_port,
                remote_port=remote_port,
                local_forward_port=fwd_port,
                remote_bind_port=remote_bind_port,
                identity_file=identity_file or None,
            )
        else:
            self.transport = st.HttpOplogTransport(f"http://127.0.0.1:{remote_bind_port}")
            self.tunnel = None

        self.cursor = st.CursorStore(self.cursor_path)
        self.daemon = sd.SyncDaemon(engine, self.transport, self.cursor)
        self.engine = engine

        # 首连基线：两端游标都还是 0 → 需要一次存量全量对账（防二蛋边界）。
        # 基线成功后置 False，之后只跑纯增量；隧道/对端不可达时保持 True 重试。
        self._need_baseline = (
            self.cursor.pull_after_seq == 0 and self.cursor.push_after_seq == 0
        )

    # ---- 生命周期 ----

    def _ensure_tunnel(self) -> None:
        if self.tunnel is None:
            return
        if not self.tunnel.is_alive():
            LOG.warning("SSH 隧道已断，重拨…")
            try:
                self.tunnel.stop()
            except Exception:
                pass
            try:
                self.tunnel.start()
            except Exception as exc:  # noqa: BLE001
                LOG.error("隧道重拨失败: %r", exc)

    def _tick_once(self) -> dict:
        self._ensure_tunnel()
        # 首连基线对账（游标归零时）：逐表拉对端快照，写本端 + 经 oplog 推对端。
        # fail-open：对端不可达则本次跳过，下轮重试，不影响增量同步。
        if self._need_baseline:
            if self._run_baseline():
                self._need_baseline = False
        res = self.daemon.tick_safe()
        if res.get("ok"):
            self._consecutive_fail = 0
        else:
            self._consecutive_fail += 1
            LOG.warning("tick 失败(第%d次): %s", self._consecutive_fail, res.get("error"))
        return res

    def _run_baseline(self) -> bool:
        """首连基线：逐表拉对端快照，reconcile_baseline_oplog 双向收敛。全部成功返 True。"""
        ok = True
        for t in self.migrated:
            try:
                snap = self.transport.pull_snapshot(t)
            except Exception as exc:  # noqa: BLE001  (对端不可达 fail-open)
                LOG.warning("基线对账拉表 %s 快照失败（下轮重试）: %r", t, exc)
                ok = False
                continue
            remote_ms = self._snapshot_to_memory_store(snap)
            try:
                # 修⑤（§11.2② 补账）：基线对账必须带 ordered_status——否则首连/游标归零
                # 时是纯 HLC LWW，actioned 可被更晚的 pending 盖回（反 nag 失效）。
                # ordered_status 从表注册表取（SQLiteSyncStore._spec；替身无 _spec 视为无序）。
                _spec = getattr(self.engine.stores[t], "_spec", None) or {}
                ordered = _spec.get("ordered_status")
                rep = self.sd.reconcile_baseline_oplog(
                    self.engine.stores[t], remote_ms, self.engine, t, self.node, ordered
                )
                LOG.info(
                    "基线对账[%s]: remote_only=%d local_only=%d conflicts=%d",
                    t, len(rep["remote_only"]), len(rep["local_only"]),
                    len(rep["conflicts"]),
                )
            except Exception as exc:  # noqa: BLE001
                LOG.error("基线对账[%s] 失败: %r", t, exc)
                ok = False
        return ok

    def _snapshot_to_memory_store(self, snap: list):
        """把 /snapshot 拉来的记录 dict 列表还原成 MemoryStore（只读快照）。"""
        ms = self.sd.MemoryStore()
        for r in snap:
            hlc = self.sd.HLC.from_str(r["hlc"])
            ms.upsert(r["record_id"], r.get("data", {}), hlc, valid_until=r.get("valid_until"))
            if r.get("tombstone"):
                ms.delete(r["record_id"], hlc)
        return ms

    def serve_forever(self) -> None:
        self.server.start()
        LOG.info("本机 oplog server 已起: %s (node=%s)", self.server.url(), self.node)
        if self.tunnel is not None:
            try:
                self.tunnel.start()
                LOG.info("SSH 反隧道已起（Mac 拨出）")
            except Exception as exc:  # noqa: BLE001
                LOG.error("SSH 隧道启动失败，将周期重拨: %r", exc)
        LOG.info("进入常驻 tick 循环（间隔 %.0fs）", self.interval)
        _tick_count = 0
        _heartbeat_every = 10
        while not self._stop:
            res = self._tick_once()
            _tick_count += 1
            if _tick_count % _heartbeat_every == 0:
                _ok = res.get("ok")
                _src = _last_persist_source_best_effort()
                LOG.info(
                    "心跳#%d 同步状态: ok=%s 连续失败=%d 间隔=%.0fs 最近一次 persist 来源=%s",
                    _tick_count, _ok, self._consecutive_fail, self.interval, _src,
                )
            # 失败后指数退避（封顶 16×），成功后恢复正常间隔
            backoff = self.interval * (2 ** min(self._consecutive_fail, 4)) if self._consecutive_fail else self.interval
            # 可中断的睡眠（便于 SIGTERM 快速退出）
            for _ in range(max(1, int(backoff))):
                if self._stop:
                    break
                time.sleep(1)
        self._shutdown()

    def _shutdown(self) -> None:
        LOG.info("收到停止信号，优雅退出…")
        try:
            self.server.stop()
        except Exception:
            pass
        if self.tunnel is not None:
            try:
                self.tunnel.stop()
            except Exception:
                pass
        LOG.info("已退出")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="小桔 B 类记忆常驻同步 daemon")
    p.add_argument("--side", choices=["mac", "cloud"], required=True)
    p.add_argument("--db-path", required=True, help="deskmate.db 绝对路径")
    p.add_argument("--node", default="", help="节点名（默认取 side）")
    p.add_argument(
        "--table",
        default="",
        help="只同步单张表（调试用）；留空=迁移并同步全部已注册 B 类表",
    )
    p.add_argument("--repo-path", default="", help="Mac 侧：openworker 仓库根（导入 deskmate 包）")
    p.add_argument("--cloud-pkg-path", default="", help="云端侧：含 sync/ 包的父目录")
    p.add_argument("--ssh-host", default="81.69.163.170")
    p.add_argument("--ssh-user", default="ubuntu")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--identity-file", default="")
    p.add_argument("--local-port", type=int, default=8765)
    p.add_argument("--remote-port", type=int, default=8765)
    p.add_argument("--fwd-port", type=int, default=18766)
    p.add_argument("--remote-bind-port", type=int, default=18765)
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--cursor-path", default="")
    p.add_argument("--log-path", default="")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv=None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        filename=args.log_path or None,
    )
    node = args.node or args.side

    if args.side == "cloud" and _cloud_server_running(args.db_path):
        LOG.error("检测到 openworker-server.service 正在运行，双写会抢锁——拒绝启动同步 daemon。")
        return 2

    sd, sync_sql, st = _load_sync_modules(args.side, args.repo_path, args.cloud_pkg_path)
    node_obj = SyncNode(
        args.side,
        sd,
        sync_sql,
        st,
        db_path=args.db_path,
        node=node,
        table=args.table,
        local_port=args.local_port,
        remote_port=args.remote_port,
        fwd_port=args.fwd_port,
        remote_bind_port=args.remote_bind_port,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_port=args.ssh_port,
        identity_file=args.identity_file,
        interval=args.interval,
        cursor_path=args.cursor_path,
        log_path=args.log_path,
    )

    def _handle(signum, _frame):
        node_obj._stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        node_obj.serve_forever()
    except Exception as exc:  # noqa: BLE001
        LOG.exception("常驻循环异常退出: %r", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
