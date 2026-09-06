"""L2 统一任务记录 —— 任务面板「小桔在忙」与提议卡的数据源。

每条自主动作（不论低风险已执行、还是高风险待确认）都落一条记录到
``~/.deskmate/agency/task_log.jsonl``。纯 JSONL 追加，零 LLM，fail-open。

状态机：
- done      ：低风险动作已自主执行完成
- proposed  ：高风险动作已生成提议卡，等用户裁决
- executed  ：用户裁决「执行」后实际跑了
- rejected  ：用户裁决「不用」
- never     ：用户裁决「以后别提」（等同于对该 action_type 临时拉黑，engine 读取后跳过）
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


class TaskStatus:
    DONE = "done"
    PROPOSED = "proposed"
    EXECUTED = "executed"
    REJECTED = "rejected"
    NEVER = "never"


@dataclass
class TaskRecord:
    """一条自主动作记录。"""

    id: str
    ts: str
    action_type: str
    risk: str
    status: str
    topic: str = ""
    summary: str = ""
    detail: str = ""
    count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskRecord":
        return cls(
            id=d.get("id", ""),
            ts=d.get("ts", ""),
            action_type=d.get("action_type", ""),
            risk=d.get("risk", ""),
            status=d.get("status", ""),
            topic=d.get("topic", ""),
            summary=d.get("summary", ""),
            detail=d.get("detail", ""),
            count=int(d.get("count", 0) or 0),
            extra=d.get("extra", {}) or {},
        )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class TaskLog:
    """JSONL 任务记录存储（进程内单例，跨 tick 共享）。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.dir = self.base_dir / "agency"
        self.file = self.dir / "task_log.jsonl"
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            pass

    # ---- 写 ----
    def add(
        self,
        action_type: str,
        risk: str,
        status: str,
        *,
        topic: str = "",
        summary: str = "",
        detail: str = "",
        count: int = 0,
        extra: Optional[dict] = None,
    ) -> TaskRecord:
        rec = TaskRecord(
            id=_new_id(),
            ts=_now_iso(),
            action_type=action_type,
            risk=risk,
            status=status,
            topic=topic,
            summary=summary,
            detail=detail,
            count=count,
            extra=extra or {},
        )
        try:
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    # ---- 读 ----
    def list(self, limit: int = 50, status: Optional[str] = None) -> list[TaskRecord]:
        if not self.file.exists():
            return []
        out: list[TaskRecord] = []
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = TaskRecord.from_dict(json.loads(line))
                    except Exception:
                        continue
                    if status is None or rec.status == status:
                        out.append(rec)
        except Exception:
            return out
        out.sort(key=lambda r: r.ts, reverse=True)
        return out[:limit]

    def get(self, record_id: str) -> TaskRecord | None:
        for rec in self.list(limit=10000):
            if rec.id == record_id:
                return rec
        return None

    # ---- 裁决（高风险提议卡）----
    def resolve(self, record_id: str, decision: str) -> bool:
        """把 proposed 记录裁决为 executed / rejected / never。返回是否找到并改写。"""
        if decision not in (TaskStatus.EXECUTED, TaskStatus.REJECTED, TaskStatus.NEVER):
            return False
        records = self.list(limit=10000)
        target = None
        for rec in records:
            if rec.id == record_id:
                target = rec
                break
        if target is None or target.status != TaskStatus.PROPOSED:
            return False
        target.status = decision
        # 整文件重写（记录量很小，足够；保持 append-only 的简洁性与原子性权衡）
        try:
            tmp = f"{self.file}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            os.replace(tmp, self.file)
            return True
        except Exception:
            return False

    def never_list(self) -> list[str]:
        """被用户「以后别提」的动作类型集合（engine 跳过）。"""
        return [r.action_type for r in self.list(limit=10000, status=TaskStatus.NEVER)]
