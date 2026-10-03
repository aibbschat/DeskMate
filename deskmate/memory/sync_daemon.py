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
- 真实 DB adapter（接 B 类表、补 ``uuid``/``hlc``/``tombstone`` 列后）与 SSH
  隧道 transport 留 TODO，待阶段 1 在 `.backup()` 冷备前提下落地。

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

    def to_json(self) -> str:
        return json.dumps(
            {
                "table": self.table,
                "record_id": self.record_id,
                "op": self.op,
                "hlc": self.hlc.to_str(),
                "payload": self.payload,
                "tombstone": self.tombstone,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, s: str) -> "Op":
        d = json.loads(s)
        return cls(
            table=d["table"],
            record_id=d["record_id"],
            op=d["op"],
            hlc=HLC.from_str(d["hlc"]),
            payload=d.get("payload", {}) or {},
            tombstone=bool(d.get("tombstone", False)),
        )


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
        self.oplog.append(Op(table, record_id, "upsert", hlc, dict(data)))
        return hlc

    def local_delete(self, table: str, record_id: str) -> HLC:
        hlc = HLC.now(self.node, self._last)
        self._last = hlc
        self.store.delete(record_id, hlc)
        self.oplog.append(Op(table, record_id, "delete", hlc, tombstone=True))
        return hlc

    # ---- 远端事件应用（LWW 收敛） ----
    def apply_remote(self, ops: list[Op]) -> None:
        for op in ops:
            self._apply_one(op)

    def _apply_one(self, op: Op) -> None:
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
# 6. 传输层抽象（拓扑无关；SSH 反隧道实现留 TODO）
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
    """一轮同步：拉对端增量 → 应用；推本端增量 → 对端。更新游标。"""

    def __init__(self, engine: SyncEngine, transport: Transport) -> None:
        self.engine = engine
        self.transport = transport
        # 两个独立的 **整数 seq 游标**，方向分开，绝不混用：
        # - pull_after_seq：对端 oplog 已拉到的 seq（只被实际拉到的远端事件推进）；
        # - push_after_seq：本端 oplog 已推完的 seq。
        # 用 HLC 当拉取起点会在时钟偏差时漏掉远端事件（见 OplogStore 文档）。
        self.pull_after_seq = 0
        self.push_after_seq = 0

    def tick(self) -> dict:
        # 1) 拉对端增量（按对端 seq），应用到本端，再推进【拉取】游标
        remote_ops = self.transport.pull_since(self.pull_after_seq)
        self.engine.apply_remote(remote_ops)
        self.pull_after_seq += len(remote_ops)  # 对端 oplog append-only 且保序
        # 2) 把本端新事件（按本端 seq）推过去，推进【推送】游标
        local_new = self.engine.oplog.since(self.push_after_seq)
        if local_new:
            self.transport.push(local_new)
        self.push_after_seq = self.engine.oplog.watermark()
        return {
            "pulled": len(remote_ops),
            "pushed": len(local_new),
            "pull_after_seq": self.pull_after_seq,
            "push_after_seq": self.push_after_seq,
        }


def new_uuid() -> str:
    """记录全局身份（UUIDv7 优先；无第三方时用 uuid4 占位，真实落地换 uuid7 库）。"""
    return str(_uuid.uuid4())
