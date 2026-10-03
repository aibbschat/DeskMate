"""小桔记忆 · B 类记录级 oplog 同步（阶段 1 骨架，供 review）。

定位（重要，避免与既有 ``deskmate/memory/sync.py`` 混淆）：
- 既有 ``sync.py`` 已实现 **文件级** 跨设备同步（vault/projects/preferences 的
  ``*.md`` + style/decision 派生包），基于 mtime + sha256 + last-write-wins，
  粒度是「文件」，对应草案里的知识/画像层。
- 本模块面向 **B 类记忆表的记录级** 同步（``profile_items`` / 日记等
  ``deskmate.db`` 内表），按草案 §7/§8：UUID + HLC + oplog + 墓碑 + LWW 收敛。
  两者 **互补不竞争** —— 文件级管知识/画像文件，记录级管 db 内 B 类事实流；
  A 类原稿仍走 Syncthing / 单向镜像，B 类（本模块）走自建 oplog over SSH 反隧道。

本骨架只实现「同步协议核心 + 可测逻辑」，**不碰任何生产库**：
- 不 import 真实 ``~/.deskmate`` / ``deskmate.db`` / ``BlobStore``；
- 用内存 dict 模拟本地 store、用 ``InMemoryTransport`` 模拟对端；
- 真实 DB adapter 已在 ``sync_store_sqlite.py`` 落地（阶段 1）；
- SSH 反隧道 transport / HTTP oplog 端点 / 游标持久化已在 ``sync_transport.py``
  落地（阶段 1 第②项 + 第③项配套），``SyncDaemon`` 经 ``cursor`` 参数接入。

核心零件（对应草案 §6）：
1. 全局身份：每个记录一个 ``record_id``（UUIDv7 或确定性 uuid）
2. 版本时钟：``HLC``（混合逻辑时钟，物理时间 + 计数器 + 节点，不依赖两端严格对时）
3. 变更日志：``Op``（表 + record_id + op + hlc + 明文 payload + 墓碑）
4. 冲突检测 + 合并：同 record_id 比 HLC，后者胜（LWW）；字段级 merge 预留钩子
5. 墓碑：delete 记 tombstone，防止「遗忘被旧副本复活」
6. 租约：少数强一致写场景预留（本骨架不实现，留接口）

生产包装须 fail-open（任何异常捕获、同步失败不影响主流程）；本骨架为便于测试
断言，协议层显式 raise，由上层 daemon 包裹 fail-open。
"""

from __future__ import annotations

import json
import time
import uuid as _uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 2. 版本时钟：HLC（混合逻辑时钟）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HLC:
    """混合逻辑时钟：物理时间(ms) + 计数器 + 节点 id。

    比较顺序：(physical, counter, node)，保证全局单调且可定序，不依赖两端严格对时。
    """

    physical: int  # epoch 毫秒
    counter: int
    node: str

    def __lt__(self, other: "HLC") -> bool:
        if not isinstance(other, HLC):
            return NotImplemented
        return (self.physical, self.counter, self.node) < (
            other.physical,
            other.counter,
            other.node,
        )

    def __le__(self, other: "HLC") -> bool:
        return self < other or self == other

    def to_int(self) -> int:
        """用于调试/排序的整数表示（node 不参与数值，仅作最后 tiebreak）。"""
        return self.physical * 1_000_000 + self.counter

    def to_str(self) -> str:
        return f"{self.physical:016x}.{self.counter:08x}.{self.node}"

    @classmethod
    def from_str(cls, s: str) -> "HLC":
        phys, cnt, node = s.split(".")
        return cls(int(phys, 16), int(cnt, 16), node)

    @classmethod
    def now(cls, node: str, last: Optional["HLC"] = None) -> "HLC":
        """取当前 HLC，保证相对 ``last`` 严格单调。"""
        phys = time.time_ns() // 1_000_000
        if last is not None and phys <= last.physical:
            return cls(last.physical, last.counter + 1, node)
        return cls(phys, 0, node)


# ---------------------------------------------------------------------------
# 3. 变更日志：Op
# ---------------------------------------------------------------------------


@dataclass
class Op:
    """一条变更事件。明文 payload，**不含向量字段**（向量各端本地重算）。"""

    table: str
    record_id: str
    op: str  # 'upsert' | 'delete'
    hlc: HLC
    payload: dict = field(default_factory=dict)  # 明文业务字段
    tombstone: bool = False  # delete 时为 True
    origin: Optional[str] = None  # 产生该变更的节点 id（消除回声，见 SyncEngine/SyncDaemon）

    def to_json(self) -> str:
        return json.dumps(
            {
                "table": self.table,
                "record_id": self.record_id,
                "op": self.op,
                "hlc": self.hlc.to_str(),
                "payload": self.payload,
                "tombstone": self.tombstone,
                "origin": self.origin,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_obj(cls, d: dict) -> "Op":
        return cls(
            table=d["table"],
            record_id=d["record_id"],
            op=d["op"],
            hlc=HLC.from_str(d["hlc"]),
            payload=d.get("payload", {}) or {},
            tombstone=bool(d.get("tombstone", False)),
            origin=d.get("origin"),
        )

    @classmethod
    def from_json(cls, s) -> "Op":
        """兼容字符串（旧调用）与已解析 dict（HTTP 数组元素是对象，非字符串）。"""
        d = json.loads(s) if isinstance(s, (str, bytes, bytearray)) else s
        return cls.from_obj(d)


# ---------------------------------------------------------------------------
# 内存 store（模拟本端 B 类 db 表，不碰生产库）
# ---------------------------------------------------------------------------


@dataclass
class Record:
    record_id: str
    data: dict
    hlc: HLC
    tombstone: bool = False
    valid_until: Optional[int] = None  # 双时态失效（epoch ms）；None = 不过期


class MemoryStore:
    """本端 B 类记录的内存存储（骨架用，真实实现接 deskmate.db 对应表）。"""

    def __init__(self) -> None:
        self._recs: dict[str, Record] = {}

    def get(self, record_id: str) -> Optional[Record]:
        return self._recs.get(record_id)

    def live_items(self) -> list[Record]:
        """返回未墓碑且未过期的记录。"""
        now = time.time_ns() // 1_000_000
        out = []
        for r in self._recs.values():
            if r.tombstone:
                continue
            if r.valid_until is not None and r.valid_until < now:
                continue
            out.append(r)
        return out

    def upsert(self, record_id: str, data: dict, hlc: HLC, valid_until: Optional[int] = None) -> None:
        self._recs[record_id] = Record(
            record_id=record_id, data=data, hlc=hlc, tombstone=False, valid_until=valid_until
        )

    def delete(self, record_id: str, hlc: HLC) -> None:
        # 墓碑：保留 record_id 但标记删除，HLC 记录删除时刻
        existing = self._recs.get(record_id)
        self._recs[record_id] = Record(
            record_id=record_id,
            data=existing.data if existing else {},
            hlc=hlc,
            tombstone=True,
            valid_until=existing.valid_until if existing else None,
        )

    def items(self) -> list[Record]:
        return list(self._recs.values())


# ---------------------------------------------------------------------------
# 本端 oplog 存储（增量游标用）
# ---------------------------------------------------------------------------


class OplogStore:
    """本端 append-only oplog。每条事件在追加时获得一个稳定的 **单调整数 seq**。

    增量同步的游标用这个 seq（watermark），而不是 HLC：HLC 只负责事件之间的
    LWW 因果定序，不能用来回答「对端哪几条我已经拉过」。若用全局 HLC 最大值当
    拉取起点，在两端时钟有偏差（或旧设备补传）时，一条 physical 更小但尚未拉过
    的远端事件会被永久跳过——实测可复现的丢更新缺陷。
    """

    def __init__(self) -> None:
        self._ops: list[Op] = []

    def append(self, op: Op) -> int:
        self._ops.append(op)
        return len(self._ops)  # 1-based seq

    def since(self, after_seq: int) -> list[Op]:
        """返回 seq 严格大于 ``after_seq`` 的事件（保序）。0 = 从头。"""
        return list(self._ops[after_seq:])

    def watermark(self) -> int:
        """本 oplog 当前最新 seq；空为 0。"""
        return len(self._ops)

    def all(self) -> list[Op]:
        return list(self._ops)


# ---------------------------------------------------------------------------
# 4/5. 同步引擎：合并核心（LWW + 墓碑 + 防遗忘复活）
# ---------------------------------------------------------------------------


class SyncEngine:
    """B 类记录级同步引擎：本地变更 → oplog；远端 oplog → 应用（LWW 收敛）。"""

    def __init__(self, store: MemoryStore, oplog: OplogStore, node: str) -> None:
        self.store = store
        self.oplog = oplog
        self.node = node
        self._last: Optional[HLC] = None  # 仅用于本节点 HLC 单调；与同步游标无关

    # ---- 本地写（生成 oplog 事件） ----
    def local_upsert(
        self, table: str, record_id: str, data: dict, valid_until: Optional[int] = None
    ) -> HLC:
        hlc = HLC.now(self.node, self._last)
        self._last = hlc
        self.store.upsert(record_id, data, hlc, valid_until)
        self.oplog.append(Op(table, record_id, "upsert", hlc, dict(data), origin=self.node))
        return hlc

    def local_delete(self, table: str, record_id: str) -> HLC:
        hlc = HLC.now(self.node, self._last)
        self._last = hlc
        self.store.delete(record_id, hlc)
        self.oplog.append(Op(table, record_id, "delete", hlc, tombstone=True, origin=self.node))
        return hlc

    # ---- 远端事件应用（LWW 收敛） ----
    def apply_remote(self, ops: list[Op]) -> None:
        for op in ops:
            self._apply_one(op)

    def _apply_one(self, op: Op) -> None:
        # 回声防护：本节点产生的事件经对端转发回来，直接跳过（自己本来就有）。
        # origin 为 None 的旧/存量事件不拦截，保持兼容。
        if op.origin is not None and op.origin == self.node:
            return
        existing = self.store.get(op.record_id)
        # 幂等：本端已有同 HLC 的该记录 → 跳过
        if existing is not None and existing.hlc == op.hlc:
            return
        # 防遗忘复活：本端已是墓碑（删除），且远端 op 的 HLC 更早或相等 → 不覆盖
        if existing is not None and existing.tombstone and op.hlc <= existing.hlc:
            return
        # 本端有更新的记录（HLC 更晚）→ 远端更旧，跳过（LWW 后者胜）
        if existing is not None and op.hlc < existing.hlc:
            return
        # 远端更新（或本端无记录）→ 应用
        if op.tombstone or op.op == "delete":
            self.store.delete(op.record_id, op.hlc)
        else:
            self.store.upsert(op.record_id, op.payload, op.hlc)

    # ---- 按 uuid 逐条对账（防假绿验收口径） ----
    def reconcile(self, other: "MemoryStore") -> dict:
        """逐 uuid 对比本端与对端（仅活记录），返回缺失/分歧报告。"""
        local = {r.record_id: r for r in self.store.live_items()}
        remote = {r.record_id: r for r in other.live_items()}
        missing_local = [rid for rid in remote if rid not in local]
        missing_remote = [rid for rid in local if rid not in remote]
        divergent = [
            rid
            for rid in local
            if rid in remote and (local[rid].data != remote[rid].data)
        ]
        return {
            "count_local": len(local),
            "count_remote": len(remote),
            "missing_local": missing_local,
            "missing_remote": missing_remote,
            "divergent": divergent,
            "converged": not (missing_local or missing_remote or divergent),
        }


# ---------------------------------------------------------------------------
# 6. 传输层抽象（拓扑无关；SSH 反隧道实现见 sync_transport.py）
# ---------------------------------------------------------------------------


class Transport(ABC):
    @abstractmethod
    def pull_since(self, after_seq: int) -> list[Op]:
        """拉取对端 seq 严格大于 ``after_seq`` 的增量 oplog 事件。"""

    @abstractmethod
    def push(self, ops: list[Op]) -> None:
        """把本端 oplog 事件推到对端。"""


class InMemoryTransport(Transport):
    """测试用传输：持有对端 OplogStore，模拟「只拨出连对端」的单向通道。

    真实实现（阶段 1 落地）：over 现有 SSH 反向隧道，Mac 常驻 daemon 拉/推
    云端 worker 的 oplog；两端不公网监听（对应 §7.3）。
    """

    def __init__(self, remote_oplog: OplogStore) -> None:
        self._remote_oplog = remote_oplog

    def pull_since(self, after_seq: int) -> list[Op]:
        return self._remote_oplog.since(after_seq)

    def push(self, ops: list[Op]) -> None:
        for op in ops:
            self._remote_oplog.append(op)


# ---------------------------------------------------------------------------
# 编排：SyncDaemon（Mac 侧常驻；云端经隧道拉/推）
# ---------------------------------------------------------------------------


class SyncDaemon:
    """一轮同步：拉对端增量 → 应用；推本端增量 → 对端。更新游标。

    ``cursor``（可选，如 ``sync_transport.CursorStore``）提供整数 seq 游标持久化：
    重拨/重启后从落盘位置继续，不重拉、不重推。游标loads 失败则 fail-open 归零。
    """

    def __init__(
        self, engine: SyncEngine, transport: Transport, cursor: object = None
    ) -> None:
        self.engine = engine
        self.transport = transport
        self.cursor = cursor
        # 两个独立的 **整数 seq 游标**，方向分开，绝不混用：
        # - pull_after_seq：对端 oplog 已拉到的 seq（只被实际拉到的远端事件推进）；
        # - push_after_seq：本端 oplog 已推完的 seq。
        # 用 HLC 当拉取起点会在时钟偏差时漏掉远端事件（见 OplogStore 文档）。
        if cursor is not None:
            self.pull_after_seq = int(getattr(cursor, "pull_after_seq", 0) or 0)
            self.push_after_seq = int(getattr(cursor, "push_after_seq", 0) or 0)
        else:
            self.pull_after_seq = 0
            self.push_after_seq = 0

    def tick(self) -> dict:
        # 1) 拉对端增量（按对端 seq），应用到本端，再推进【拉取】游标
        remote_ops = self.transport.pull_since(self.pull_after_seq)
        self.engine.apply_remote(remote_ops)
        self.pull_after_seq += len(remote_ops)  # 对端 oplog append-only 且保序
        # 2) 把本端新事件（按本端 seq）推过去，推进【推送】游标。
        #    回声防护：只推 origin==本节点 的事件；对端转发进来的（origin=对端）
        #    也在本端 oplog 里（HTTP POST 追加），但绝不能再推回去，否则两端 oplog
        #    会无限往返膨胀。游标仍按 oplog 水位推进（不漏 seq）。
        recent = self.engine.oplog.since(self.push_after_seq)
        local_new = [
            op for op in recent if op.origin is None or op.origin == self.engine.node
        ]
        if local_new:
            self.transport.push(local_new)
        self.push_after_seq = self.engine.oplog.watermark()
        if self.cursor is not None:
            self.cursor.pull_after_seq = self.pull_after_seq
            self.cursor.push_after_seq = self.push_after_seq
            self.cursor.save()
        return {
            "pulled": len(remote_ops),
            "pushed": len(local_new),
            "pull_after_seq": self.pull_after_seq,
            "push_after_seq": self.push_after_seq,
        }

    def tick_safe(self) -> dict:
        """fail-open 封装：同步异常（隧道断、对端宕）绝不应拖垮主进程。

        返回 ``{"ok": False, "error": ...}`` 而非 raise；守护层据 ``ok`` 决定是否重拨。
        """
        try:
            return {**self.tick(), "ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001  (守门：任何同步失败都归为可恢复)
            return {
                "ok": False,
                "error": repr(exc),
                "pulled": 0,
                "pushed": 0,
                "pull_after_seq": self.pull_after_seq,
                "push_after_seq": self.push_after_seq,
            }

    def reconcile_baseline(self, remote_store: "MemoryStore") -> dict:
        """首连前置：对端 item 快照已就位时，做一次性全量对账。

        见模块级 ``reconcile_baseline``。本方法只是把本端 store 与对端 store
        对接的薄封装；生产环境由 SSH 反隧道把对端 item 快照拉到本端后调用。
        """
        return reconcile_baseline(self.engine.store, remote_store, self.engine.node)


# ---------------------------------------------------------------------------
# 7. 首连基线全量对账（草案方案 A：一次性、store-to-store、不污染 oplog）
# ---------------------------------------------------------------------------


def _write_record(target: "MemoryStore", rec: "Record") -> None:
    """把一条记录写进 target：墓碑则 delete、活则 upsert，保留原 HLC。"""
    if rec.tombstone:
        target.delete(rec.record_id, rec.hlc)
    else:
        target.upsert(rec.record_id, rec.data, rec.hlc, rec.valid_until)


def reconcile_baseline(local: "MemoryStore", remote: "MemoryStore", node: str) -> dict:
    """一次性全量对账（首连前置，草案方案 A）。

    背景（二蛋 2026-10-03 实测边界）：存量迁移只回填了 uuid、没有把存量写进
    ``_sync_oplog``（oplog=0 行），因此若两端存量本身不一致，后续纯 oplog 增量
    不会收敛——A 独有的存量记录永远到不了 B。本函数在「首连」时补一次全量对账，
    之后才切回纯 oplog 增量。

    做法（按 sync_uuid 逐条比两端 ``items()``，含墓碑，覆盖「本端删了远端还在」）：
    - 缺失侧：把记录写进缺失端（墓碑则 delete、活则 upsert，保留原 HLC）；
    - 共存在但 HLC 不同的：按 LWW 把胜者写回两端；
    - 幂等可重跑；不污染 oplog（直接写 store，不经 ``engine.local_*``）。

    验收按 sync_uuid 逐条比（集合差集 + 冲突 uuid 的 HLC 实测），不只比 COUNT。
    """
    L = {r.record_id: r for r in local.items()}
    R = {r.record_id: r for r in remote.items()}
    report = {"local_only": [], "remote_only": [], "conflicts": [], "resolved": 0}
    # 缺失：本端有远端无 -> 推远端；远端有本端无 -> 拉本端
    for rid, rec in L.items():
        if rid not in R:
            _write_record(remote, rec)
            report["local_only"].append(rid)
    for rid, rec in R.items():
        if rid not in L:
            _write_record(local, rec)
            report["remote_only"].append(rid)
    # 冲突：两边都有的按 HLC LWW，胜者写回两端
    for rid in set(L) & set(R):
        a, b = L[rid], R[rid]
        if a.hlc == b.hlc:
            continue
        winner = a if a.hlc >= b.hlc else b
        _write_record(local, winner)
        _write_record(remote, winner)
        report["conflicts"].append(rid)
        report["resolved"] += 1
    return report


def new_uuid() -> str:
    """记录全局身份（UUIDv7 优先；无第三方时用 uuid4 占位，真实落地换 uuid7 库）。"""
    return str(_uuid.uuid4())
