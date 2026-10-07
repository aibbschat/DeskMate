"""小桔记忆 · B 类记录级同步 · 真实 SQLite 存储适配器（阶段 1/2 落地）。

把 ``sync_daemon`` 协议核心的鸭子类型 store 接口接到 ``deskmate.db`` 内 B 类表。
阶段 1 只接 ``profile_items``；阶段 2 扩展为**按表注册**，新增 ``learned_history`` /
``loved_entries``；阶段 6（2026-10-05）新增个人中心日记 ``user_posts`` 与留言 ``comments``。
访谈 ``interview_sessions`` 经萧哥 10-05 拍板**不同步**；主聊天 ``chat.db`` 是独立库、
本期不动（见 reports/2026-10-05_会话日记同步范围扩大_设计方案.md）。

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
import re
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
    # ---- 阶段 6（2026-10-05）：个人中心日记 + 留言纳入同步范围 ----
    # 依据 reports/2026-10-05_会话日记同步范围扩大_设计方案.md §2（萧哥 10-05 拍板：
    # 日记进云端、interview_sessions 不同步、chat.db 本期不动）。
    "user_posts": {
        "pk": "id",
        "pk_type": "text",  # 真实 schema：id TEXT PRIMARY KEY（非自增），双端同 id → 可直接作 sync_key
        "sync_key": "id",  # 与 profile_items 同构：text pk 本身即唯一
        "sync_uuid_kind": "string",  # 与触发器 _trigger_sql 的 COALESCE 表达式同格式
        "business_cols": (
            "id",
            "author",
            "content",
            "created_at",
            "loved",
            "archived",  # 归档标记；不同步会导致对端丢失归档态
        ),
        "conflict_col": None,
    },
    "comments": {
        "pk": "id",
        "pk_type": "text",
        "sync_key": "id",
        "sync_uuid_kind": "string",  # 同 user_posts，与触发器表达式一致
        "business_cols": (
            "id",
            "moment_id",  # → user_posts.id；实测 schema 只有普通索引，无外键约束
            "author",
            "text",
            "created_at",
        ),
        "conflict_col": None,
    },
    # ---- P0（2026-10-06）：主动产出统一台账（「主动产出管道」）----
    # 所有主动产出（云端内容审计 / 手机灵感 / 日后来源）的统一载体，进 SYNC_TABLES 自动跨端收敛。
    # 确定性 id（text pk）由生产者生成（<source>:<kind>:<指纹>），双端同 id → 同 sync_uuid → 自动合并
    # （对应 2026-10-06 48 条 profile_items 身份分裂教训：禁用随机 uuid）。
    # ordered_status：status 只前进不后退（防 nag 失效，见 sync_daemon._lww_winner / _apply_one_to）。
    "proactive_ledger": {
        "pk": "id",
        "pk_type": "text",  # 真实 schema：id TEXT PRIMARY KEY（非自增），双端同 id → 可直接作 sync_key
        "sync_key": "id",  # 与 profile_items 同构：text pk 本身即唯一
        "sync_uuid_kind": "string",  # 与触发器 _trigger_sql 的 COALESCE 表达式同格式
        "business_cols": (
            "id",
            "source",
            "kind",
            "content",
            "evidence",
            "understanding",
            "payload",
            "source_node",
            "status",
            "confidence",
            "created_at",
            "acknowledged_at",
            # 修④：delivered_at 进业务列 → 交付标记经 oplog 跨端共享，日上限两端同计数
            #（§11.2③：计数查 ledger、不落进程内 DisturbManager）。
            "delivered_at",
            # 修⑥：snoozed_until 进业务列 →「稍后」的退避到期时间跨端一致，
            # 到期复活由各端读路径按该列判定（status 保持 snoozed 可审计，§11.2②）。
            "snoozed_until",
        ),
        "conflict_col": None,
        # status 优先级（高→低）：actioned/dismissed/in_progress/acknowledged 都不可被
        # 「稍后」翻案；snoozed（修⑥）只压过 pending——远端迟到的 pending 不得覆盖已搁置
        # 条目（反 nag 失效防护），而用户在任一端的处理（acknowledged 起）跨端生效。
        # 到期复活走读路径（pending_items 按 snoozed_until 判定），status 字段保持
        # snoozed 可审计，这就是 §11.2②「独立轨道」的落地形态。
        "ordered_status": (
            "actioned", "dismissed", "in_progress", "acknowledged", "snoozed", "pending",
        ),
        # P3-a（§11.6 隐私分级）：行级「只在本端、不同步」开关。
        # 仅 proactive_ledger 启用——手机灵感/日记等敏感来源可标 visibility='local'，
        # 该行**不产生 op**（触发器 WHEN 跳过）、**不进快照**（live_items/items 过滤）、
        # **基线不对推送**（reconcile_baseline_oplog 排除）。列名固定 'visibility'
        # （不入 business_cols，不进 oplog；local 行本就不产生 op，故对端无从感知）。
        # 其它表不设这个键 → 全表照常同步（向后兼容）。
        "local_visibility_col": "visibility",
    },
    # ---- 2026-10-06：LLM 通道熔断表（设计文档 §3.3）----
    # 为什么必须跨端共享：记忆里「48 条身份分裂」就是两套状态不一致的产物。
    # 熔断同理——本地熔断了、云端还在撞，是同一类事故的另一种形态。
    # **两端必须看到同一份「哪条通道此刻不可用」**，否则云端会重演那 28 次同路重试。
    "llm_circuit_breaker": {
        "pk": "channel_key",
        "pk_type": "text",  # channel_key = '<provider>:<model>'，双端算出同一个字符串→同sync_uuid
        "sync_key": "channel_key",
        "sync_uuid_kind": "string",
        "business_cols": (
            "channel_key",
            "state",              # closed | open（到期后读路径视作 half_open，允许试一次）
            "kind",               # quota_exhausted | rate_limited | unavailable | transient
            "opened_at",
            "cooldown_until",     # 冷却到期（epoch 秒字符串）：火山可解析精确重置时间
            "last_error_detail",
            "strike_count",
            "source_node",
            "updated_at",
        ),
        "conflict_col": None,
        # ⚠️ **不设 ordered_status**（与 proactive_ledger 不同，故意的）：
        # 熔断是「此刻的事实」，不是「只应前进的状态」——冷却到期后必须**能自动恢复**。
        # 若套用只前进语义，cooldown_until 过期后远端的旧 closed 记录会把它永久钉死。
        # 正确语义是「两端取更新的那份」，交给 HLC LWW + cooldown_until 读路径判定。
    },
}

# ---- 策略①护栏：跨端只同步明文事实、向量各端本地重算 ----
# 小桔语义检索向量由各端本地 BGE/API 重算（见架构文档 §6.6 策略①），**禁止经 oplog 跨端搬运**：
# 否则两端 embedding 后端/维度不一致会触发 _vector_fingerprint 整批拒或余弦静默 0.0。
# 此守卫在模块加载即生效，任何把 embedding/vector 列塞进同步表或 business_cols 的改动都会启动即报错，
# 把「策略①」从文字约定升级为代码不变量。
_EMBED_OR_VECTOR_RE = re.compile(r"(?:^|[_])embed(?:ding)?\b|(?:^|[_])vector\b", re.IGNORECASE)


def _validate_sync_tables_no_vectors() -> None:
    for _t, _spec in SYNC_TABLES.items():
        if "embed" in _t.lower() or "vector" in _t.lower():
            raise RuntimeError(
                f"[sync] SYNC_TABLES 含向量表 {_t!r}：违反「向量各端重算」策略①，"
                f"向量必须各端本地重算、禁止跨端同步"
            )
        for _col in _spec.get("business_cols", ()):
            if _EMBED_OR_VECTOR_RE.search(_col):
                raise RuntimeError(
                    f"[sync] 同步表 {_t!r} 的 business_cols 含向量列 {_col!r}："
                    f"违反「向量各端重算」策略①，向量必须各端本地重算、禁止跨端同步"
                )


_validate_sync_tables_no_vectors()

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


def _trigger_sql(
    table: str,
    cols: tuple,
    sync_key: str = "id",
    local_visibility_col: Optional[str] = None,
) -> list[str]:
    """生成某表的三组触发器（建前先 DROP，幂等重跑安全）。

    - ai：AFTER INSERT，suppress=0 时。先把本行 sync_uuid 补随机值（若 NULL）、
      刷新 sync_hlc、sync_tombstone=0（用 suppress 包裹该 UPDATE，避免它再触发 au
      造成递归），再从本行 SELECT 回读写 upsert op。
    - au：AFTER UPDATE，suppress=0 时。刷新 sync_hlc（suppress 包裹避免递归），
      再从本行回读写 upsert op（payload 取最新业务列）。
    - ad：AFTER DELETE，suppress=0 时。写 tombstone op（record_id=OLD.sync_uuid）。
    HLC 用 SQLite 原生 ``strftime('%s')*1000``（ms）+ ``_sync_meta`` 的 node，
    不依赖任何 Python UDF，所有连接（含 app 连接）都能触发。

    **hlc 必须严格单调（2026-10-05 修）**：counter 不是只读的 ``(SELECT hlc_counter
    FROM _sync_meta)``，而是**每次触发先自增再取值**::

        UPDATE _sync_meta SET hlc_counter = hlc_counter + 1;
        printf(..., CAST(strftime('%s','now')*1000 AS INTEGER),
               (SELECT hlc_counter FROM _sync_meta), (SELECT node FROM _sync_meta))

    修前 counter 恒为 0（从不被写），同一毫秒内发生的 delete→upsert 会拿到**完全相同**
    的 hlc；对端 apply 因"同 HLC 幂等跳过"把后到的 upsert 永久丢弃，记录卡死在墓碑态
    （生产实证：云端 profile_items 的 emotional 层持续增长、Mac 侧 0 条）。
    修后 (physical, counter) 组合严格递增，删后重插必得不同 hlc，从根上消除撞车。
    注意：UPDATE counter 必须放在 ``suppress=1`` 区间内（只改元表、不碰业务表，
    不会递归），但**不可**给 hlc_expr 整体加 suppress 语义——每次触发都要推进。

    为什么不用 BEFORE INSERT 触发器补 sync_uuid：本机 SQLite（3.50.x）实测
    ``SELECT NEW.col = expr`` 形式的 BEFORE 触发器赋值**完全不生效**（被当作比较式，
    列仍为 NULL），且 ``ALTER TABLE ADD COLUMN ... DEFAULT(randomblob)`` 被拒
    （非恒定默认）。故改为 AFTER 触发器内 suppress 包裹的 UPDATE 补列，可靠且零递归。
    """
    # 严格单调 hlc：counter 每次触发自增（修前为只读 → 恒 0 → 同毫秒撞车）
    hlc_expr = (
        "printf('%016x.%08x.%s',"
        " CAST(strftime('%s','now')*1000 AS INTEGER),"
        " (SELECT hlc_counter FROM _sync_meta),"
        " (SELECT node FROM _sync_meta))"
    )
    bump = f"UPDATE {_META_TABLE} SET hlc_counter = hlc_counter + 1;\n  "
    payload = _json_obj_expr(cols)
    # P3-a（§11.6）：local 行不产生 op。触发器 WHEN 加可见性守卫——
    # ai/au 看 NEW.visibility（写/改），ad 看 OLD.visibility（删）。
    # 缺省（NULL）或 'sync' 视为可同步；只有显式 'local' 跳过。
    vis_new = ""
    vis_old = ""
    if local_visibility_col:
        vis_new = (
            f" AND (NEW.{local_visibility_col} IS NULL"
            f" OR NEW.{local_visibility_col} <> 'local')"
        )
        vis_old = (
            f" AND (OLD.{local_visibility_col} IS NULL"
            f" OR OLD.{local_visibility_col} <> 'local')"
        )
    ai = (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_ai AFTER INSERT ON {table}\n"
        f"WHEN (SELECT suppress FROM _sync_meta) = 0{vis_new}\n"
        f"BEGIN\n"
        f"  UPDATE {_META_TABLE} SET suppress=1;\n"
        f"  {bump}"
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
        f"WHEN (SELECT suppress FROM _sync_meta) = 0{vis_new}\n"
        f"BEGIN\n"
        f"  UPDATE {_META_TABLE} SET suppress=1;\n"
        f"  {bump}"
        f"  UPDATE {table} SET sync_hlc = {hlc_expr} WHERE rowid = NEW.rowid;\n"
        f"  UPDATE {_META_TABLE} SET suppress=0;\n"
        f"  INSERT INTO {_OPLOG_TABLE}(table_name, record_id, op, hlc, payload_json, tombstone, node)\n"
        f"  SELECT '{table}', sync_uuid, 'upsert', sync_hlc, {payload}, 0,"
        f" (SELECT node FROM _sync_meta) FROM {table} WHERE rowid = NEW.rowid;\n"
        f"END;"
    )
    ad = (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_ad AFTER DELETE ON {table}\n"
        f"WHEN (SELECT suppress FROM _sync_meta) = 0{vis_old}\n"
        f"BEGIN\n"
        f"  {bump}"
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
            node TEXT,
            created_at INTEGER
        )"""
    )
    # P3-c（§11.x 归档/GC）：oplog 行加 created_at（epoch 秒），供按龄清理。
    # 既有库缺该列 → ALTER 补；存量 NULL 由 hlc 物理时间戳回填
    # （hlc 格式 '%016x.%08x.%s'，前 16 位十六进制为 ms 物理时间——SQLite 无通用
    # 十六进制→十进制转换，故在 Python 内回填，避免 SQL CAST 把 hex 当十进制误算）。
    if "created_at" not in _table_cols(conn, _OPLOG_TABLE):
        cur.execute(f"ALTER TABLE {_OPLOG_TABLE} ADD COLUMN created_at INTEGER")
    _backfill_oplog_created_at(cur)
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

        # P3-a（§11.6）：行级「只在本端」开关列。仅 proactive_ledger 设 local_visibility_col；
        # 其它表无该键 → 不建列（向后兼容）。缺省 'sync' = 照常同步。
        lvc = spec.get("local_visibility_col")
        if lvc and lvc not in existing:
            cur.execute(f"ALTER TABLE {t} ADD COLUMN {lvc} TEXT DEFAULT 'sync'")

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

        # 存量回填：sync_uuid 缺失的行生成确定性 uuid，并补齐 sync_hlc。
        # 两端同源快照 → 同一条存量必得同一 sync_uuid（派生而非随机）。
        ns = _uuid.uuid5(_uuid.NAMESPACE_URL, f"deskmate:{t}")
        sync_key = spec.get("sync_key", spec["pk"])
        kind = spec.get("sync_uuid_kind", "uuid5")
        rows = cur.execute(
            f"SELECT {spec['pk']} FROM {t} WHERE sync_uuid IS NULL OR sync_hlc IS NULL"
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
        for sql in _trigger_sql(t, cols, spec["sync_key"], lvc):
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

    def live_items(self, exclude_local: bool = False) -> list:
        cols = ",".join(self._cols)
        now = time.time_ns() // 1_000_000
        lvc = self._spec.get("local_visibility_col") if exclude_local else None
        with self._connect() as c:
            if lvc:
                rows = c.execute(
                    f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                    f"FROM {self.table} WHERE sync_tombstone=0"
                    f" AND ({lvc} IS NULL OR {lvc} <> 'local')"
                ).fetchall()
            else:
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
            # 行级不同步（P3-a §11.6）：visibility='local' 的行触发器不赋值 sync_uuid/
            # sync_hlc（NULL）——读取时回落到业务主键作本地身份、零 HLC，避免崩溃；
            # 这些行只在本端，不上同步层（exclude_local 已拦截）。
            rid = sync_uuid if sync_uuid is not None else data.get(self._pk)
            hlc = HLC.from_str(hlc_s) if hlc_s else HLC(0, 0, "")
            out.append(
                Record(
                    record_id=rid,
                    data=data,
                    hlc=hlc,
                    tombstone=False,
                    valid_until=vu,
                )
            )
        return out

    def items(self, exclude_local: bool = False) -> list:
        """全部记录（含墓碑）。用于基线对账 / oplog 构造——删除记录以 tombstone=True
        形式从墓碑侧表补回，使「本端删了远端还在」能被对账发现并传播删除。

        ``exclude_local``（P3-a §11.6）：为真且本表设了 ``local_visibility_col`` 时，
        过滤掉 ``visibility='local'`` 的行——它们只在本端、不该被基线推送/快照外发。
        （增量同步由触发器 WHEN 守卫拦截，无需此处；此处只覆盖基线/快照路径。）
        """
        cols = ",".join(self._cols)
        lvc = self._spec.get("local_visibility_col") if exclude_local else None
        with self._connect() as c:
            if lvc:
                rows = c.execute(
                    f"SELECT sync_uuid, sync_hlc, sync_tombstone, {cols} "
                    f"FROM {self.table}"
                    f" WHERE ({lvc} IS NULL OR {lvc} <> 'local')"
                ).fetchall()
            else:
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
            # 见 live_items：local 行 sync_uuid/sync_hlc 为 NULL，回落业务主键 + 零 HLC。
            rid = sync_uuid if sync_uuid is not None else data.get(self._pk)
            hlc = HLC.from_str(hlc_s) if hlc_s else HLC(0, 0, "")
            out.append(
                Record(
                    record_id=rid,
                    data=data,
                    hlc=hlc,
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


def _backfill_oplog_created_at(cur: Any) -> int:
    """把 oplog 中 created_at 为 NULL 的存量行，用 hlc 物理时间回填（epoch 秒）。

    返回回填条数。hlc 格式 ``'%016x.%08x.%s'``：前 16 位十六进制 = ms 物理时间戳。
    SQLite 无通用 hex→dec 转换（CAST 会把 hex 当十进制误算），故在 Python 内逐行回填。
    """
    rows = cur.execute(
        f"SELECT seq, hlc FROM {_OPLOG_TABLE} "
        f"WHERE created_at IS NULL AND hlc IS NOT NULL"
    ).fetchall()
    n = 0
    for seq, hlc in rows:
        # hlc 格式 '%016x.%08x.%s'：首个 '.' 前为 16 位十六进制物理 ms。
        # 用 split('.')[0] 而非 [:16]，避免把分隔点也切进 hex 导致解析失败。
        try:
            ms = int(str(hlc).split(".")[0], 16)
        except ValueError:
            continue  # 异常格式保护：跳过该行，留待人工核查，不崩
        cur.execute(
            f"UPDATE {_OPLOG_TABLE} SET created_at=? WHERE seq=?",
            (ms // 1000, seq),
        )
        n += 1
    return n


def gc_oplog(
    conn: Any, keep_days: float, min_safe_seq: int, dry_run: bool = False
) -> int:
    """P3-c（§11.x 归档/GC）：清理本地 oplog 中「已推完且超龄」的事件。

    删除条件（两者皆满足才删，缺一不可）：
      1. ``seq <= min_safe_seq``：该事件 seq 不高于已推送水位（push_after_seq），
         即对端**必然已收到**——本地副本仅为传输日志、可安全丢弃。
         ``seq > min_safe_seq`` 的事件（含尚未推送的本端新写）一律保留，绝不丢同步。
      2. ``created_at < cutoff``（cutoff = now - keep_days*86400，epoch 秒）：
         仅清理真正陈旧的日志；近期事件即使已推送也保留，供审计/排查。

    安全性（红线）：
      - 本函数**绝不**调整游标（pull_after_seq / push_after_seq）；GC 只删已推送副本，
        游标语义不变，下一轮 ``since(push_after_seq)`` 自然为空、无重推。
      - ``dry_run=True`` 时只统计不删除，返回「将删除」的条数，便于先核对再执行。
      - 调用方职责（CLI 脚本履行）：必须从 CursorStore 取真实 push_after_seq 作
        ``min_safe_seq``；删除前必须对活库做在线冷备（sqlite3/.backup），绝不 cp 活库；
        本函数属手动触发路径，**绝不**被 daemon / 自动循环调用。
    """
    cutoff = int(time.time()) - int(keep_days * 86400)
    where = "seq <= ? AND created_at IS NOT NULL AND created_at < ?"
    params = (int(min_safe_seq), cutoff)
    if dry_run:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {_OPLOG_TABLE} WHERE {where}", params
        ).fetchone()[0]
        return int(n)
    cur = conn.execute(f"DELETE FROM {_OPLOG_TABLE} WHERE {where}", params)
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# §6 租约 / 写权（Lease）—— 2026-10-07 落地「剩余待办第 5 项」
# ---------------------------------------------------------------------------
_LEASE_TABLE = "_sync_lease"


class SQLiteLease(_ShortConnMixin):
    """同步协议六零件之六「租约 / 写权」（草案 §5.6 / §6）：本端唯一写者。

    - 单行 ``(name, holder, expires_at)``；``acquire`` 用单条 UPSERT-WHERE 原子完成
      「不存在即抢占 / 已过期接管 / 同持有者续租」——并发下不会出现双持有者。
    - TTL 过期后任何持有者可接管：持有者进程崩溃**不会永久锁死**（无死锁）。
    - **本端语义**：租约表在各自 deskmate.db 内，保护的是本端多进程写者（如常驻
      daemon 与每小时窗口同时做基线对账 / 未来两进程共存时的强一致写窗口）；
      跨端单写权威由数据三分类规则保证（A 类 Mac 单写），不靠本表。
    - 首个真实用点：``sync_daemon_runner._run_baseline``（基线对账是多语句长写，
      两进程同时跑会互相打爆写锁——写饥饿的同类场景）。
    - fail-open：本类只提供机制，调用方拿不到租约时**跳过本轮**即可，不应抛错。
    """

    def __init__(self, conn: Any) -> None:
        self._init_conn(conn)
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._connect() as c:
            c.execute(
                f"CREATE TABLE IF NOT EXISTS {_LEASE_TABLE} ("
                "name TEXT PRIMARY KEY, "
                "holder TEXT NOT NULL, "
                "expires_at INTEGER NOT NULL)"
            )
            try:
                c.commit()
            except sqlite3.Error:
                pass

    def acquire(self, name: str, holder: str, ttl_s: float = 300.0) -> bool:
        """抢占/续租。返回 True = 本 holder 现持有该租约；False = 被他人持有且未过期。"""
        now = int(time.time())
        expires = now + max(1, int(ttl_s))
        with self._connect() as c:
            cur = c.execute(
                f"INSERT INTO {_LEASE_TABLE}(name, holder, expires_at) VALUES (?, ?, ?) "
                f"ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, "
                f"expires_at=excluded.expires_at "
                f"WHERE {_LEASE_TABLE}.expires_at <= ? OR {_LEASE_TABLE}.holder = ?",
                (name, holder, expires, now, holder),
            )
            try:
                c.commit()
            except sqlite3.Error:
                pass
            return cur.rowcount == 1

    def release(self, name: str, holder: str) -> bool:
        """释放租约（仅持有者本人）。返回 True = 本次确实释放了。"""
        with self._connect() as c:
            cur = c.execute(
                f"DELETE FROM {_LEASE_TABLE} WHERE name = ? AND holder = ?",
                (name, holder),
            )
            try:
                c.commit()
            except sqlite3.Error:
                pass
            return cur.rowcount == 1

    def status(self, name: str) -> Optional[tuple]:
        """返回 (holder, expires_at) 或 None（无人持有/未创建）。已过期也返回，
        由调用方结合 expires_at 判定（acquire 的接管判定是原子的，勿自行分裂判断）。
        """
        with self._connect() as c:
            row = c.execute(
                f"SELECT holder, expires_at FROM {_LEASE_TABLE} WHERE name = ?",
                (name,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def held(self, name: str) -> bool:
        """当前是否被（未过期的）租约持有。"""
        st = self.status(name)
        return bool(st and st[1] > int(time.time()))


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
                f"(table_name, record_id, op, hlc, payload_json, tombstone, node, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    op.table,
                    op.record_id,
                    op.op,
                    op.hlc.to_str(),
                    json.dumps(op.payload, ensure_ascii=False),
                    1 if op.tombstone else 0,
                    op.hlc.node,
                    int(time.time()),  # P3-c：事件落地时间（epoch 秒），供按龄 GC
                ),
            )
            c.commit()
            seq = cur.lastrowid
            op.seq = seq
            return seq

    def gc(self, keep_days: float, min_safe_seq: int, dry_run: bool = False) -> int:
        """P3-c（§11.x）：见模块级 ``gc_oplog``。短连接安全。"""
        with self._connect() as c:
            return gc_oplog(c, keep_days, min_safe_seq, dry_run=dry_run)

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
