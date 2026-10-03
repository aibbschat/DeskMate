"""小桔记忆 · B 类记录级同步 · 真实 SQLite 存储适配器（阶段 1 落地）。

把 ``sync_daemon`` 协议核心的鸭子类型 store 接口接到 ``deskmate.db`` 内 B 类表
（首批 ``profile_items``）。只**新增** ``sync_uuid`` 三列与一个 ``_sync_oplog`` 表，
**不删除、不修改既有列**；存量幂等回填（重跑安全）。

安全约束（红线）：
- 任何 ALTER / 回填前，调用方必须先 ``sqlite3 .backup`` 冷备活库（禁 cp 活库）；
- 本模块不 import 真实 base_dir，连接由上层传入；测试一律用 /tmp 副本。

线程安全（2026-10-03 发现并修复）：
- ``HttpOplogServer`` 在独立守护线程里 serve，会跨线程使用本模块持有的 sqlite 连接；
  持有长连接跨线程使用会抛 ``SQLite objects created in a thread...`` 并导致对端连接被关闭。
- 修复：store 不跨线程持有长连接，而是**每次操作开一个指向同文件的新短连接**
  （``check_same_thread=False``，连接仅在调用线程内存活）。无论 daemon 主线程还是
  HTTP server 守护线程调用，都在各自线程内用独立连接，SQLite 文件锁串行化写、WAL
  保证读可见，彻底规避跨线程崩溃。内存库（``:memory:``）无文件、短连接无意义，则回退
  持有原连接（仅单线程测试使用，不跨线程）。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid as _uuid
from typing import Any, Optional

from .sync_daemon import HLC, Record, Op

# profile_items 业务列（写入时由 data 提供，读取时还原为 Record.data）
_BUSINESS_COLS = (
    "layer",
    "dimension",
    "topic",
    "content",
    "category",
    "status",
    "confidence",
    "extra_json",
    "valid_from",
    "valid_until",
)

_OPLOG_TABLE = "_sync_oplog"


def _table_cols(conn: Any, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: Any, node: str, table: str = "profile_items") -> dict:
    """对 B 类表做同步迁移（幂等，可重跑）。

    返回 ``{"backfilled": int}``。幂等：已存在 sync 列则跳过 ALTER；存量
    sync_uuid IS NULL 的行才回填。oplog 表用 CREATE TABLE IF NOT EXISTS。
    """
    cur = conn.cursor()
    cols = _table_cols(conn, table)

    # 加三列（SQLite ALTER 不幂等，先探测）
    if "sync_uuid" not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN sync_uuid TEXT")
    if "sync_hlc" not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN sync_hlc TEXT")
    if "sync_tombstone" not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN sync_tombstone INTEGER DEFAULT 0")
    cur.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_sync_uuid "
        f"ON {table}(sync_uuid)"
    )

    # oplog 表（append-only，seq 自增 = 增量游标）
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {_OPLOG_TABLE} (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            op TEXT NOT NULL,
            hlc TEXT NOT NULL,
            payload_json TEXT,
            tombstone INTEGER DEFAULT 0,
            node TEXT
        )"""
    )

    # 存量回填：为 sync_uuid IS NULL 的行生成 uuid + 单调 HLC。
    # 关键：存量记录的两端身份必须**确定性一致**（同一原 id → 同一 sync_uuid），
    # 否则 Mac 与云端首次同步时同一条存量会对不齐。用 uuid5 基于原 id 派生，
    # 而非随机 uuid4（新插入的记录靠 op 传播的 record_id 对齐，不受影响）。
    ns = _uuid.uuid5(_uuid.NAMESPACE_URL, f"deskmate:{table}")
    rows = cur.execute(
        f"SELECT id FROM {table} WHERE sync_uuid IS NULL"
    ).fetchall()
    base = time.time_ns() // 1_000_000
    for i, (rid,) in enumerate(rows):
        hlc = HLC(base, i, node)  # 存量无冲突，单调 HLC 仅作稳定基线
        sync_uuid = str(_uuid.uuid5(ns, rid))
        cur.execute(
            f"UPDATE {table} SET sync_uuid=?, sync_hlc=?, sync_tombstone=0 WHERE id=?",
            (sync_uuid, hlc.to_str(), rid),
        )
    conn.commit()
    return {"backfilled": len(rows)}


class _ShortConnMixin:
    """从传入连接提取 db 路径；文件库每次操作开短连接，内存库回退原连接。"""

    def _init_conn(self, conn: Any) -> None:
        # 用 PRAGMA database_list 取 'main' 库文件路径（内存库 file 为 ''），
        # 不依赖 Connection.database 属性（部分 Python 构建无此属性，getattr 会得 None
        # 从而误判为内存库、回退持有主线程长连接，导致 HTTP server 子线程跨线程崩溃）。
        path = None
        try:
            for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
                if name == "main":
                    path = file or None
        except Exception:
            path = None
        if path is None:
            # 内存库：短连接无意义，持有原连接（仅单线程测试用，不跨线程）
            self._db_path = None
            self._fixed_conn = conn
        else:
            self._db_path = path
            self._fixed_conn = None

    def _connect(self) -> Any:
        if self._db_path is None:
            return self._fixed_conn
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        # 防御性硬化：WAL 下 daemon 写与主应用/HTTP server 写可能撞锁，给 5s 等待
        # 而非默认 0（撞锁即抛 database is locked）。不改变库的 journal_mode。
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        return conn


class SQLiteSyncStore(_ShortConnMixin):
    """接 profile_items 的 B 类记录存储；接口对齐 MemoryStore。跨线程安全（短连接）。"""

    def __init__(self, conn: Any, table: str = "profile_items") -> None:
        self.table = table
        self._init_conn(conn)

    def get(self, record_id: str) -> Optional[Record]:
        cols = ",".join(_BUSINESS_COLS)
        with self._connect() as c:
            row = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                f"FROM {self.table} WHERE sync_uuid=?",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        sync_uuid, hlc_s, tomb, *biz = row
        data = dict(zip(_BUSINESS_COLS, biz))
        return Record(
            record_id=sync_uuid,
            data=data,
            hlc=HLC.from_str(hlc_s),
            tombstone=bool(tomb),
            valid_until=_parse_ms(data.get("valid_until")),
        )

    def live_items(self) -> list:
        cols = ",".join(_BUSINESS_COLS)
        now = time.time_ns() // 1_000_000
        with self._connect() as c:
            rows = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                f"FROM {self.table} WHERE sync_tombstone=0"
            ).fetchall()
        out = []
        for row in rows:
            sync_uuid, hlc_s, tomb, *biz = row
            data = dict(zip(_BUSINESS_COLS, biz))
            vu = _parse_ms(data.get("valid_until"))
            if vu is not None and vu < now:
                continue
            out.append(
                Record(
                    record_id=sync_uuid,
                    data=data,
                    hlc=HLC.from_str(hlc_s),
                    tombstone=False,
                    valid_until=vu,
                )
            )
        return out

    def items(self) -> list:
        cols = ",".join(_BUSINESS_COLS)
        with self._connect() as c:
            rows = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} FROM {self.table}"
            ).fetchall()
        out = []
        for row in rows:
            sync_uuid, hlc_s, tomb, *biz = row
            data = dict(zip(_BUSINESS_COLS, biz))
            out.append(
                Record(
                    record_id=sync_uuid,
                    data=data,
                    hlc=HLC.from_str(hlc_s),
                    tombstone=bool(tomb),
                    valid_until=_parse_ms(data.get("valid_until")),
                )
            )
        return out

    def upsert(
        self,
        record_id: str,
        data: dict,
        hlc: HLC,
        valid_until: Optional[int] = None,
    ) -> None:
        data = dict(data)
        if valid_until is not None:
            data["valid_until"] = str(valid_until)
        cols = _BUSINESS_COLS
        with self._connect() as c:
            existing = c.execute(
                f"SELECT 1 FROM {self.table} WHERE sync_uuid=?", (record_id,)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{c2}=?" for c2 in cols)
                vals = [data.get(c2) for c2 in cols]
                vals.append(hlc.to_str())
                vals.append(record_id)
                c.execute(
                    f"UPDATE {self.table} SET {sets}, sync_hlc=? WHERE sync_uuid=?",
                    vals,
                )
            else:
                all_cols = ["id", "sync_uuid", "sync_hlc", "sync_tombstone"] + list(cols)
                placeholders = ", ".join("?" for _ in all_cols)
                vals = [record_id, record_id, hlc.to_str(), 0] + [
                    data.get(c2) for c2 in cols
                ]
                c.execute(
                    f"INSERT INTO {self.table} ({', '.join(all_cols)}) "
                    f"VALUES ({placeholders})",
                    vals,
                )
            c.commit()

    def delete(self, record_id: str, hlc: HLC) -> None:
        # 已有行：打墓碑。
        with self._connect() as c:
            cur = c.execute(
                f"UPDATE {self.table} SET sync_tombstone=1, sync_hlc=? "
                f"WHERE sync_uuid=?",
                (hlc.to_str(), record_id),
            )
            if cur.rowcount == 0:
                # 行不存在（对账时收到一条本端从未有过的删除墓碑）：必须**插入墓碑**，
                # 与 MemoryStore.delete 行为对齐——否则陈旧副本日后重放该记录的旧
                # upsert 时本端无墓碑可挡，被删记录可能被重新引入。
                c.execute(
                    f"INSERT INTO {self.table} (id, sync_uuid, sync_hlc, sync_tombstone) "
                    f"VALUES (?, ?, ?, 1)",
                    (record_id, record_id, hlc.to_str()),
                )
            c.commit()


class SQLiteOplogStore(_ShortConnMixin):
    """接 _sync_oplog 的 append-only 日志；接口对齐 OplogStore（seq 游标）。跨线程安全。"""

    def __init__(self, conn: Any) -> None:
        self._init_conn(conn)

    def append(self, op: Op) -> int:
        with self._connect() as c:
            cur = c.execute(
                f"INSERT INTO {_OPLOG_TABLE}"
                f"(table_name, record_id, op, hlc, payload_json, tombstone, node) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    op.table,
                    op.record_id,
                    op.op,
                    op.hlc.to_str(),
                    json.dumps(op.payload, ensure_ascii=False),
                    1 if op.tombstone else 0,
                    op.hlc.node,
                ),
            )
            c.commit()
            seq = cur.lastrowid
            # 回填真实 seq，使调用方在 append 后直接读 op.seq 与内存版/对端 apply 行为一致。
            # 二蛋 2026-10-03 指出：原未回填 → append 后读 op.seq 出现
            # SQLite→None / 内存→本端 seq / 远端 apply→对端 seq 三态不一致。
            op.seq = seq
            return seq

    def since(self, after_seq: int) -> list:
        with self._connect() as c:
            rows = c.execute(
                f"SELECT seq, record_id, op, hlc, payload_json, tombstone, table_name, node "
                f"FROM {_OPLOG_TABLE} WHERE seq>?",
                (after_seq,),
            ).fetchall()
        out = []
        for seq, record_id, op, hlc_s, payload, tomb, table_name, node in rows:
            out.append(
                Op(
                    table=table_name,
                    record_id=record_id,
                    op=op,
                    hlc=HLC.from_str(hlc_s),
                    payload=json.loads(payload or "{}"),
                    tombstone=bool(tomb),
                    origin=node,
                    seq=seq,  # 透传真实 seq，供客户端按 max(seq) 推进游标（二蛋 2026-10-03 缺陷）
                )
            )
        return out

    def watermark(self) -> int:
        with self._connect() as c:
            return c.execute(
                f"SELECT COALESCE(MAX(seq), 0) FROM {_OPLOG_TABLE}"
            ).fetchone()[0]

    def all(self) -> list:
        return self.since(0)


def _parse_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
