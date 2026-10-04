"""小桔记忆 · B 类记录级同步 · 真实 SQLite 存储适配器（阶段 1/2 落地）。

把 ``sync_daemon`` 协议核心的鸭子类型 store 接口接到 ``deskmate.db`` 内 B 类表。
阶段 1 只接 ``profile_items``；阶段 2 扩展为**按表注册**，新增 ``learned_history`` /
``loved_entries``（访谈 ``interview_sessions``、日记 ``user_posts`` 留待后续批次）。

所有 B 类表共用一个 append-only ``_sync_oplog``（带 ``table_name`` 列区分来源），
每张表额外加 ``sync_uuid`` / ``sync_hlc`` / ``sync_tombstone`` 三列 + 确定性存量回填。

DB 触发器（阶段 2 新增）：对每张同步表建 ``AFTER INSERT/UPDATE/DELETE`` 触发器，
自动把变更写进 ``_sync_oplog``，从而**捕获真实 app 的写**（LearnedStore / DiaryStore /
cognitive 等都不必改）。这同时补上阶段 1 的边界缺口——app 直写 ``profile_items``
之前不进 oplog，现在经触发器自动进。触发器用 SQLite 原生 ``randomblob`` + ``strftime``
生成 HLC，**不依赖 app 连接注册 UDF**，对所有写路径零侵入。

触发器补列机制（重要，2026-10 实测定坑）：本机 SQLite 3.50.x 的 BEFORE 触发器
``SELECT NEW.col = expr`` 赋值**完全不生效**（被当比较式、列仍 NULL），且
``ALTER TABLE ADD COLUMN ... DEFAULT(randomblob)`` 被拒（非恒定默认）。故改用
**AFTER 触发器内 suppress 包裹的 UPDATE** 补 ``sync_uuid``（NULL 时随机）/ 刷新
``sync_hlc``，再 SELECT 回读写 oplog op；suppress 标志阻断该 UPDATE 再次触发 au，
杜绝递归。存量回填（migrate）在触发器建立前完成，不受影响。

回声防护（关键）：同步引擎 ``apply_remote`` 把远端事件写回本表时，必须从触发器里
「隐身」——否则每应用一条远端 op 又生成一条新本地 op，两端无限互弹。做法：本模块
``_sync_meta.suppress`` 标志位；``upsert`` / ``delete`` 始终置 ``suppress=1``
（引擎写入的 op 已由其 ``oplog.append`` 统一落盘），所有 op 触发器
``WHEN (SELECT suppress)=0`` 不触发；写毕复位。app 正常写时 ``suppress=0`` → 触发器照常捕获。

安全约束（红线）：
- 任何 ALTER / 回填前，调用方必须先 ``sqlite3 .backup`` 冷备活库（禁 cp 活库）；
- 本模块不 import 真实 base_dir，连接由上层传入；测试一律用 /tmp 副本。

线程安全（2026-10-03 发现并修复）：
- ``HttpOplogServer`` 在独立守护线程里 serve，会跨线程使用本模块持有的 sqlite 连接；
  持有长连接跨线程使用会抛 ``SQLite objects created in a thread...``。
- 修复：store 不跨线程持有长连接，而是**每次操作开一个指向同文件的新短连接**
  （``check_same_thread=False``，连接仅在调用线程内存活）。内存库（``:memory:``）无文件、
  短连接无意义，则回退持有原连接（仅单线程测试使用，不跨线程）。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid as _uuid
from typing import Any, Optional

from .sync_daemon import HLC, Record, Op

# ---------------------------------------------------------------------------
# 按表注册：B 类表 → 业务列 / 主键 / 远端 insert 是否保留原主键 / 冲突列
# ---------------------------------------------------------------------------
# - business_cols：同步进 oplog 的明文业务列（向量/blob 列一律排除，各端本地重算）。
# - pk / pk_type：业务主键列与类型；INT 自增表且无入边外键时，远端 insert 省略 pk
#   （让 sqlite 自派本地 id），sync_uuid 作跨设备稳定键，避免双端整数 id 碰撞。
# - conflict_col：可选，远端 insert 撞唯一约束时按此列.merge（learned_history 的 fp）。
SYNC_TABLES: dict[str, dict] = {
    "profile_items": {
        "pk": "id",
        "pk_type": "text",  # 真实 schema：id TEXT PRIMARY KEY（非自增），远端 insert 须显式置 id
        "sync_key": "id",  # 业务唯一键（text pk 本身即唯一）；sync_uuid 派生源
        "sync_uuid_kind": "uuid5",  # 已上线格式，保持与存量一致（阶段 1 即此）
        "business_cols": (
            "id",
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
        ),
        "conflict_col": None,
    },
    "learned_history": {
        "pk": "id",
        "pk_type": "int",
        "sync_key": "fp",  # 业务唯一键（UNIQUE 约束）；int 自增 id 双端从1起会撞，必须用 fp 派生
        "sync_uuid_kind": "string",  # SQL 触发器无 uuid5，统一用确定性字符串，与 migrate 一致
        "business_cols": (
            "ts",
            "fact",
            "category",
            "fp",
            "source",
            "entity",
            "polarity",
            "status",
            "superseded_by",
            "valid_until",
            "valid_from",
        ),
        # fp 唯一：双端独立学到同一事实会撞 UNIQUE → 按 fp merge 而非丢。
        "conflict_col": "fp",
    },
    "loved_entries": {
        "pk": "id",
        "pk_type": "int",
        "sync_key": "moment_id",  # 业务唯一键；int 自增 id 双端从1起会撞，必须用 moment_id 派生
        "sync_uuid_kind": "string",  # 与触发器一致的确定性字符串格式
        "business_cols": (
            "moment_id",
            "content",
            "day",
            "created_at",
            "tags",
        ),
        "conflict_col": None,
    },
}

_OPLOG_TABLE = "_sync_oplog"
_META_TABLE = "_sync_meta"
# 墓碑侧表：同步删除（含「本端从未见过」的删除）只在此落墓碑，业务表**物理删除**，
# 绝不残留 sync_tombstone=1 行（否则 app 直读 learned_history/loved_entries 不感知该列会读到幽灵行）。
# 侧表供 _apply_one_to 判定「迟到/陈旧 upsert 不得复活已删记录」（LWW 防复活）。
_SYNC_TOMBSTONE_TABLE = "_sync_tombstones"


def _table_cols(conn: Any, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _json_obj_expr(cols: tuple) -> str:
    """构造 trigger 里用的 ``json_object('c1', c1, ...)``（在 SELECT FROM 表 中用裸列名）。

    注：本构建依赖在 AFTER 触发器里先 UPDATE 本行再 SELECT 回读，故用裸列名而非 NEW.c。
    """
    parts = ", ".join(f"'{c}', {c}" for c in cols)
    return f"json_object({parts})"


def _trigger_sql(table: str, cols: tuple, sync_key: str = "id") -> list[str]:
    """生成某表的三组触发器（建前先 DROP，幂等重跑安全）。

    - ai：AFTER INSERT，suppress=0 时。先把本行 sync_uuid 补随机值（若 NULL）、
      刷新 sync_hlc、sync_tombstone=0（用 suppress 包裹该 UPDATE，避免它再触发 au
      造成递归），再从本行 SELECT 回读写 upsert op。
    - au：AFTER UPDATE，suppress=0 时。刷新 sync_hlc（suppress 包裹避免递归），
      再从本行回读写 upsert op（payload 取最新业务列）。
    - ad：AFTER DELETE，suppress=0 时。写 tombstone op（record_id=OLD.sync_uuid）。
    HLC 用 SQLite 原生 ``strftime('%s')*1000``（ms）+ ``_sync_meta`` 的 node，
    不依赖任何 Python UDF，所有连接（含 app 连接）都能触发。

    为什么不用 BEFORE INSERT 触发器补 sync_uuid：本机 SQLite（3.50.x）实测
    ``SELECT NEW.col = expr`` 形式的 BEFORE 触发器赋值**完全不生效**（被当作比较式，
    列仍为 NULL），且 ``ALTER TABLE ADD COLUMN ... DEFAULT(randomblob)`` 被拒
    （非恒定默认）。故改为 AFTER 触发器内 suppress 包裹的 UPDATE 补列，可靠且零递归。
    """
    hlc_expr = (
        "printf('%016x.%08x.%s',"
        " CAST(strftime('%s','now')*1000 AS INTEGER),"
        " (SELECT hlc_counter FROM _sync_meta),"
        " (SELECT node FROM _sync_meta))"
    )
    payload = _json_obj_expr(cols)
    ai = (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_ai AFTER INSERT ON {table}\n"
        f"WHEN (SELECT suppress FROM _sync_meta) = 0\n"
        f"BEGIN\n"
        f"  UPDATE {_META_TABLE} SET suppress=1;\n"
        f"  UPDATE {table} SET sync_uuid = COALESCE(sync_uuid,"
        f" 'deskmate:' || '{table}' || ':' || COALESCE({sync_key}, 'row' || NEW.rowid)),"
        f" sync_hlc = {hlc_expr}, sync_tombstone = 0 WHERE rowid = NEW.rowid;\n"
        f"  UPDATE {_META_TABLE} SET suppress=0;\n"
        f"  INSERT INTO {_OPLOG_TABLE}(table_name, record_id, op, hlc, payload_json, tombstone, node)\n"
        f"  SELECT '{table}', sync_uuid, 'upsert', sync_hlc, {payload}, 0,"
        f" (SELECT node FROM _sync_meta) FROM {table} WHERE rowid = NEW.rowid;\n"
        f"END;"
    )
    au = (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_au AFTER UPDATE ON {table}\n"
        f"WHEN (SELECT suppress FROM _sync_meta) = 0\n"
        f"BEGIN\n"
        f"  UPDATE {_META_TABLE} SET suppress=1;\n"
        f"  UPDATE {table} SET sync_hlc = {hlc_expr} WHERE rowid = NEW.rowid;\n"
        f"  UPDATE {_META_TABLE} SET suppress=0;\n"
        f"  INSERT INTO {_OPLOG_TABLE}(table_name, record_id, op, hlc, payload_json, tombstone, node)\n"
        f"  SELECT '{table}', sync_uuid, 'upsert', sync_hlc, {payload}, 0,"
        f" (SELECT node FROM _sync_meta) FROM {table} WHERE rowid = NEW.rowid;\n"
        f"END;"
    )
    ad = (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_ad AFTER DELETE ON {table}\n"
        f"WHEN (SELECT suppress FROM _sync_meta) = 0\n"
        f"BEGIN\n"
        f"  INSERT INTO {_OPLOG_TABLE}(table_name, record_id, op, hlc, payload_json, tombstone, node)\n"
        f"  VALUES ('{table}', OLD.sync_uuid, 'delete', {hlc_expr}, '{{}}', 1, (SELECT node FROM _sync_meta));\n"
        # app 直写删除也必须落墓碑侧表，否则 has_tombstone() 查不到 → 迟到 upsert 复活
        # （引擎路径在 Python 内直写侧表；此处补齐触发器路径，两路语义一致）。
        f"  INSERT INTO {_SYNC_TOMBSTONE_TABLE}(table_name, record_id, hlc)\n"
        f"  SELECT '{table}', OLD.sync_uuid, {hlc_expr} WHERE OLD.sync_uuid IS NOT NULL\n"
        f"  ON CONFLICT(table_name, record_id) DO UPDATE SET hlc=excluded.hlc\n"
        f"  WHERE excluded.hlc > {_SYNC_TOMBSTONE_TABLE}.hlc;\n"
        f"END;"
    )
    return [ai, au, ad]


def migrate(conn: Any, node: str, table: Optional[str] = None) -> dict:
    """对 B 类表做同步迁移（幂等，可重跑）。

    默认迁移所有已注册表（阶段 2 多表）；传 ``table`` 则只迁移该表（向后兼容
    阶段 1 单表调用 / 测试）。返回 ``{"backfilled": {表: 数}, "tables": [...]}``。

    顺序铁律：先加列 + 存量回填（UPDATE，此时触发器尚未建 → 不污染 oplog），
    再建触发器。这样存量不会进 oplog（与阶段 1 边界一致），只有迁移后的新写才进。
    """
    tables = [table] if table else list(SYNC_TABLES)
    cur = conn.cursor()
    backfilled: dict[str, int] = {}
    skipped: list[str] = []

    # 共享 oplog + 元信息表
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
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {_META_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            node TEXT NOT NULL,
            hlc_counter INTEGER NOT NULL DEFAULT 0,
            suppress INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # 元信息只种一次（node 决定两端身份；重跑保留既有 node）
    cur.execute(
        f"INSERT OR IGNORE INTO {_META_TABLE}(id, node, hlc_counter, suppress) "
        f"VALUES (1, ?, 0, 0)",
        (node,),
    )
    # 墓碑侧表（阶段 2 P0 修复）：同步删除只在此落墓碑，业务表物理删除，避免
    # NOT NULL 撞列 + 幽灵行泄漏。与 _sync_oplog/_sync_meta 同生命周期，一次即可。
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_SYNC_TOMBSTONE_TABLE} ("
        f"table_name TEXT NOT NULL, record_id TEXT NOT NULL, hlc TEXT NOT NULL, "
        f"PRIMARY KEY (table_name, record_id))"
    )

    for t in tables:
        # 表不存在（如临时测试库未建该表）→ 跳过，不崩（fail-safe，向前兼容）。
        # 真实 deskmate.db 含全部已注册表；最小化库（单表）只迁存在的表。
        exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if not exists:
            skipped.append(t)
            continue
        spec = SYNC_TABLES[t]
        cols = spec["business_cols"]
        existing = _table_cols(conn, t)

        # 加三列（SQLite ALTER 不幂等，先探测）
        if "sync_uuid" not in existing:
            cur.execute(f"ALTER TABLE {t} ADD COLUMN sync_uuid TEXT")
        if "sync_hlc" not in existing:
            cur.execute(f"ALTER TABLE {t} ADD COLUMN sync_hlc TEXT")
        if "sync_tombstone" not in existing:
            cur.execute(f"ALTER TABLE {t} ADD COLUMN sync_tombstone INTEGER DEFAULT 0")
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{t}_sync_uuid "
            f"ON {t}(sync_uuid)"
        )

        # 存量回填：sync_uuid IS NULL 的行生成确定性 uuid + 单调 HLC。
        # 两端同源快照 → 同一条存量必得同一 sync_uuid（uuid5 派生，非随机）。
        ns = _uuid.uuid5(_uuid.NAMESPACE_URL, f"deskmate:{t}")
        sync_key = spec.get("sync_key", spec["pk"])
        kind = spec.get("sync_uuid_kind", "uuid5")
        rows = cur.execute(
            f"SELECT {spec['pk']} FROM {t} WHERE sync_uuid IS NULL"
        ).fetchall()
        base = time.time_ns() // 1_000_000
        for i, (rid,) in enumerate(rows):
            hlc = HLC(base, i, node)
            # sync_uuid 派生源必须是业务唯一键（sync_key），不能是 int 自增 pk——
            # 两端各自从 1 自增会让「不同业务记录、同 id」撞同一 sync_uuid（阶段 2 实测
            # fpA1/id=1 与 fpB1/id=1 同 uuid 导致基线对账误覆盖）。
            kv = cur.execute(
                f"SELECT {sync_key} FROM {t} WHERE {spec['pk']}=?", (rid,)
            ).fetchone()[0]
            if kind == "string":
                # 确定性字符串：SQL 触发器无 uuid5，统一此格式使「双端同 sync_key 值→同 sync_uuid」
                sync_uuid = f"deskmate:{t}:{kv}"
            else:
                sync_uuid = str(_uuid.uuid5(ns, str(kv)))
            cur.execute(
                f"UPDATE {t} SET sync_uuid=?, sync_hlc=?, sync_tombstone=0 "
                f"WHERE {spec['pk']}=?",
                (sync_uuid, hlc.to_str(), rid),
            )
        backfilled[t] = len(rows)

        # 触发器（建前 DROP，幂等重跑安全）——必须在存量回填之后
        for name in (
            f"trg_{t}_ai",
            f"trg_{t}_au",
            f"trg_{t}_ad",
        ):
            cur.execute(f"DROP TRIGGER IF EXISTS {name}")
        for sql in _trigger_sql(t, cols, spec["sync_key"]):
            cur.execute(sql)

    conn.commit()
    return {
        "backfilled": backfilled,
        "tables": [t for t in tables if t not in skipped],
        "skipped": skipped,
    }


class _ShortConnMixin:
    """从传入连接提取 db 路径；文件库每次操作开短连接，内存库回退原连接。"""

    def _init_conn(self, conn: Any) -> None:
        path = None
        try:
            for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
                if name == "main":
                    path = file or None
        except Exception:
            path = None
        if path is None:
            self._db_path = None
            self._fixed_conn = conn
        else:
            self._db_path = path
            self._fixed_conn = None

    def _connect(self) -> Any:
        if self._db_path is None:
            return self._fixed_conn
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        return conn


class SQLiteSyncStore(_ShortConnMixin):
    """接某张 B 类表的记录存储；接口对齐 MemoryStore。跨线程安全（短连接）。"""

    def __init__(self, conn: Any, table: str = "profile_items") -> None:
        self.table = table
        self._spec = SYNC_TABLES[table]
        self._cols = self._spec["business_cols"]
        self._pk = self._spec["pk"]
        self._pk_type = self._spec["pk_type"]
        self._conflict_col = self._spec["conflict_col"]
        self._init_conn(conn)

    def get(self, record_id: str) -> Optional[Record]:
        cols = ",".join(self._cols)
        with self._connect() as c:
            row = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                f"FROM {self.table} WHERE sync_uuid=?",
                (record_id,),
            ).fetchone()
            if row is not None:
                sync_uuid, hlc_s, tomb, *biz = row
                data = dict(zip(self._cols, biz))
                return Record(
                    record_id=sync_uuid,
                    data=data,
                    hlc=HLC.from_str(hlc_s),
                    tombstone=bool(tomb),
                    valid_until=_parse_ms(data.get("valid_until")),
                )
            # 业务表无此行 → 查墓碑侧表（物理删除后墓碑只在此）
            trow = c.execute(
                f"SELECT hlc FROM {_SYNC_TOMBSTONE_TABLE} "
                f"WHERE table_name=? AND record_id=?",
                (self.table, record_id),
            ).fetchone()
            if trow is not None:
                return Record(
                    record_id=record_id,
                    data={},
                    hlc=HLC.from_str(trow[0]),
                    tombstone=True,
                    valid_until=None,
                )
        return None

    def live_items(self) -> list:
        cols = ",".join(self._cols)
        now = time.time_ns() // 1_000_000
        with self._connect() as c:
            rows = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                f"FROM {self.table} WHERE sync_tombstone=0"
            ).fetchall()
        out = []
        for row in rows:
            sync_uuid, hlc_s, tomb, *biz = row
            data = dict(zip(self._cols, biz))
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
        """全部记录（含墓碑）。用于基线对账 / oplog 构造——删除记录以 tombstone=True
        形式从墓碑侧表补回，使「本端删了远端还在」能被对账发现并传播删除。"""
        cols = ",".join(self._cols)
        with self._connect() as c:
            rows = c.execute(
                f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                f"FROM {self.table}"
            ).fetchall()
            seen = {r[0] for r in rows}
            tomb_rows = c.execute(
                f"SELECT record_id, hlc FROM {_SYNC_TOMBSTONE_TABLE} "
                f"WHERE table_name=?",
                (self.table,),
            ).fetchall()
        out = []
        for row in rows:
            sync_uuid, hlc_s, tomb, *biz = row
            data = dict(zip(self._cols, biz))
            out.append(
                Record(
                    record_id=sync_uuid,
                    data=data,
                    hlc=HLC.from_str(hlc_s),
                    tombstone=bool(tomb),
                    valid_until=_parse_ms(data.get("valid_until")),
                )
            )
        for rid, hlc_s in tomb_rows:
            if rid in seen:
                continue  # 业务表已有（理论不会，物理删除后无该行；保险跳过）
            out.append(
                Record(
                    record_id=rid,
                    data={},
                    hlc=HLC.from_str(hlc_s),
                    tombstone=True,
                    valid_until=None,
                )
            )
        return out

    def upsert(
        self,
        record_id: str,
        data: dict,
        hlc: HLC,
        valid_until: Optional[int] = None,
        suppress_triggers: bool = True,
    ) -> None:
        # 引擎写入（local_upsert / apply_remote / 基线对账）**永远 suppress 触发器**：
        # 该事件的 oplog 由引擎的 ``oplog.append`` 统一落盘，若再让 AFTER INSERT/UPDATE
        # 触发器写一份，会产生重复 op（同 hlc），虽幂等但污染 oplog。真实 app 写
        # （LearnedStore / DiaryStore 等）绕开本 store，由触发器捕获——互不冲突。
        # suppress_triggers 参数保留仅为向后兼容，本方法始终 suppress。
        data = dict(data)
        if valid_until is not None:
            data["valid_until"] = str(valid_until)
        cols = self._cols
        with self._connect() as c:
            c.execute(f"UPDATE {_META_TABLE} SET suppress=1")
            try:
                existing = c.execute(
                    f"SELECT 1 FROM {self.table} WHERE sync_uuid=?", (record_id,)
                ).fetchone()
                if existing:
                    sets = ", ".join(f"{c2}=?" for c2 in cols)
                    vals = [data.get(c2) for c2 in cols]
                    vals.append(hlc.to_str())
                    vals.append(record_id)
                    c.execute(
                        f"UPDATE {self.table} SET {sets}, sync_hlc=? "
                        f"WHERE sync_uuid=?",
                        vals,
                    )
                else:
                    # 远端 insert：sync 三列 + 业务列（统一）。
                    # - int 自增表（learned_history/loved_entries）：business_cols 不含 'id'
                    #   → 省略业务 pk，sqlite 自派本地 id；sync_uuid 作跨端稳定键。
                    # - text pk 表（profile_items：id TEXT PRIMARY KEY 是业务主键）：
                    #   business_cols 含 'id' → INSERT 时 id 列取 data['id']（业务值，双端一致）。
                    #   绝不能用 record_id(sync_uuid) 覆盖，否则对端推来的新记录业务 id 丢失。
                    if self._pk_type == "text" and not data.get("id"):
                        # 防御：理论上面经层数据 data 必含 'id'；缺失时回退 sync_uuid 不崩
                        data["id"] = record_id
                    all_cols = ["sync_uuid", "sync_hlc", "sync_tombstone"] + list(cols)
                    vals = [record_id, hlc.to_str(), 0] + [
                        data.get(c2) for c2 in cols
                    ]
                    placeholders = ", ".join("?" for _ in all_cols)
                    try:
                        c.execute(
                            f"INSERT INTO {self.table} ({', '.join(all_cols)}) "
                            f"VALUES ({placeholders})",
                            vals,
                        )
                    except sqlite3.IntegrityError:
                        # 唯一约束冲突（如 learned_history 的 fp）：按冲突列 merge。
                        # 注：R1 已保证「同 sync_key 值 ⇒ 同 sync_uuid」，正常流里远端 insert 的
                        # sync_uuid 与本地既有行一致，不会触发此分支（它只防人工造脏数据/遗留脏行）；
                        # 命中时按冲突列就地更新业务列，sync_uuid 保持不变（不丢身份、不产生假回推 op）。
                        if self._conflict_col:
                            cset = ", ".join(f"{c2}=?" for c2 in cols)
                            cvals = [data.get(c2) for c2 in cols]
                            cvals.append(hlc.to_str())
                            cvals.append(data.get(self._conflict_col))
                            c.execute(
                                f"UPDATE {self.table} SET {cset}, sync_hlc=? "
                                f"WHERE {self._conflict_col}=?",
                                cvals,
                            )
                        # 无冲突列则静默跳过（单作者低并发下罕见；fail-safe）
            finally:
                c.execute(f"UPDATE {_META_TABLE} SET suppress=0")
            c.commit()

    def delete(self, record_id: str, hlc: HLC, suppress_triggers: bool = True) -> None:
        # 与 upsert 一致：引擎写入（local_delete / apply_remote / 基线对账）**永远 suppress
        # 触发器**。否则本方法的 DELETE 会触发 AFTER DELETE 触发器写一份同 hlc 的 tombstone op，
        # 远端先 apply 该 tombstone 清掉业务行……（对端无此行则落墓碑侧表，幂等），逻辑上 OK，
        # 但 suppress 仍保留以杜绝任何回声路径。suppress_triggers 参数仅为向后兼容，本方法始终 suppress。
        # 关键（阶段 2 P0 修复）：**物理删除业务行 + 墓碑落独立侧表**，绝不插 sync_tombstone=1
        # 占位行——否则生产库 NOT NULL 列（learned_history.ts/fact/fp、loved_entries.content/day、
        # profile_items.dimension 等）会直接 IntegrityError，且 app 直读不感知该列会读到幽灵行。
        with self._connect() as c:
            c.execute(f"UPDATE {_META_TABLE} SET suppress=1")
            try:
                # 业务表物理删除（无论本端是否见过该行，DELETE 对不存在的行是 no-op，不报错）。
                c.execute(
                    f"DELETE FROM {self.table} WHERE sync_uuid=?", (record_id,)
                )
                # 墓碑落侧表：供对端/本端日后迟到 upsert 判定跳过（LWW 防复活）。
                # 同 (table,record_id) 只留最大 hlc 的删除，单调推进。
                c.execute(
                    f"INSERT INTO {_SYNC_TOMBSTONE_TABLE}(table_name, record_id, hlc) "
                    f"VALUES (?, ?, ?) "
                    f"ON CONFLICT(table_name, record_id) DO UPDATE SET hlc=excluded.hlc "
                    f"WHERE excluded.hlc > {_SYNC_TOMBSTONE_TABLE}.hlc",
                    (self.table, record_id, hlc.to_str()),
                )
            finally:
                c.execute(f"UPDATE {_META_TABLE} SET suppress=0")
            c.commit()

    def has_tombstone(self, record_id: str, hlc: HLC) -> bool:
        """本端是否已有 (table,record_id) 的删除墓碑，且其 hlc >= 给定 hlc。

        供 SyncEngine._apply_one_to 在「业务表无此行」时判定：该记录已被删且删除不比
        给定 upsert 旧 → 跳过该 upsert（防陈旧/迟到事件复活已删记录）。
        """
        with self._connect() as c:
            row = c.execute(
                f"SELECT hlc FROM {_SYNC_TOMBSTONE_TABLE} "
                f"WHERE table_name=? AND record_id=?",
                (self.table, record_id),
            ).fetchone()
        if row is None:
            return False
        return HLC.from_str(row[0]) >= hlc


class SQLiteOplogStore(_ShortConnMixin):
    """接 _sync_oplog 的 append-only 日志；接口对齐 OplogStore（seq 游标）。跨线程安全。

    阶段 2：``since`` 支持按 ``table`` 过滤，使「每表一个引擎共享一个 oplog」
    不会误应用其它表的事件。
    """

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
            op.seq = seq
            return seq

    def since(self, after_seq: int, table: Optional[str] = None) -> list:
        with self._connect() as c:
            if table is None:
                rows = c.execute(
                    f"SELECT seq, record_id, op, hlc, payload_json, tombstone, "
                    f"table_name, node FROM {_OPLOG_TABLE} WHERE seq>?",
                    (after_seq,),
                ).fetchall()
            else:
                rows = c.execute(
                    f"SELECT seq, record_id, op, hlc, payload_json, tombstone, "
                    f"table_name, node FROM {_OPLOG_TABLE} "
                    f"WHERE seq>? AND table_name=?",
                    (after_seq, table),
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
                    seq=seq,
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
