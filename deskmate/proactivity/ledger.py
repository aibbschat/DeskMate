"""主动产出管道 · 台账层（2026-10-06 P0）。

所有主动产出（云端内容审计 / 手机灵感 / 日后来源）的统一落库 + 同步 + 本地对接。
生产者只调用 :func:`record_proactive`，其余（跨端同步、本地对话式对接）由管道统一解决。

设计见 ``docs/云端主动与本地对接设计_2026-10-06.md``：
- 确定性 id（``<source>:<kind>:<指纹>``）→ 双端同 id 自动合并（禁随机 uuid，对应 48 条分裂教训）。
- status 只前进不后退（``SYNC_TABLES['proactive_ledger']['ordered_status']`` + sync_daemon LWW 三处）。
- 本地对接 = 开新会话时查 pending → 注入上下文 → 对话式开场白（非浮层）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEDGER_TABLE = "proactive_ledger"

# status 优先级（高→低），与 SYNC_TABLES['proactive_ledger']['ordered_status'] 同源。
# 同步层（sync_daemon LWW）据此强制只前进；应用层 acknowledge_ledger 也兜底。
STATUS_ORDER = ("actioned", "dismissed", "in_progress", "acknowledged", "pending")


def _resolve_base(base_dir: Optional[Path]) -> Path:
    """解析 DESKMATE 基目录（调用时解析，禁模块级常量 / import 期副作用）。"""
    if base_dir is not None:
        return Path(base_dir)
    try:
        from deskmate.paths import deskmate_base_dir

        return Path(deskmate_base_dir())
    except Exception:
        return Path.home() / ".deskmate"


def _ensure_table(conn: sqlite3.Connection) -> None:
    """自举建表 + 接入同步（加三列 + 触发器，幂等）。

    不依赖其它 store 的初始化顺序；测试用 /tmp 副本库也可直接跑。
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
        f"id TEXT PRIMARY KEY, source TEXT, kind TEXT, content TEXT, evidence TEXT,"
        f" understanding TEXT, payload TEXT, source_node TEXT, status TEXT,"
        f" confidence REAL, created_at TEXT, acknowledged_at TEXT)"
    )
    try:
        from deskmate.memory.sync_store_sqlite import migrate

        node = "mac"
        try:
            r = conn.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
            if r and r[0]:
                node = r[0]
        except Exception:
            pass
        migrate(conn, node, LEDGER_TABLE)
    except Exception as e:  # noqa: BLE001  fail-open：建表成功即可落库，同步列缺失仅影响跨端
        logger.warning("proactive ledger migrate 失败（同步列/触发器可能缺失）: %s", e)


def _deterministic_id(source: str, kind: str, anchor: Optional[str], content: str) -> str:
    """确定性 id = ``<source>:<kind>:<指纹>``。

    指纹优先用生产者提供的稳定锚点（如证据章节定位）；缺失时基于 content 截断 hash 兜底，
    使同内容合并。双端同 id → 同 sync_uuid → 自动合并（禁用随机 uuid）。
    """
    if anchor:
        finger = anchor
    else:
        finger = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{kind}:{finger}"


def record_proactive(
    base_dir: Optional[Path] = None,
    *,
    source: str,
    kind: str,
    content: str,
    evidence: str = "",
    understanding: Optional[str] = None,
    anchor: Optional[str] = None,
    payload: Optional[dict] = None,
    source_node: Optional[str] = None,
    confidence: float = 0.0,
) -> str:
    """落一条主动产出到 ``proactive_ledger``（status=pending），经触发器自动进 oplog 同步。

    同 ``source/kind/anchor`` 重复调用 → 合并（更新非 status 字段，status 不降级）。
    返回确定性 id。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    rid = _deterministic_id(source, kind, anchor, content)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    und = understanding if understanding is not None else ""
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        _ensure_table(c)
        if source_node is None:
            try:
                sn = c.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
                source_node = sn[0] if sn and sn[0] else "mac"
            except Exception:
                source_node = "mac"
        cols = (
            "id, source, kind, content, evidence, understanding, payload,"
            " source_node, status, confidence, created_at, acknowledged_at"
        )
        try:
            c.execute(
                f"INSERT INTO {LEDGER_TABLE} ({cols})"
                f" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, source, kind, content, evidence, und, payload_json,
                    source_node, "pending", confidence, now, None,
                ),
            )
        except sqlite3.IntegrityError:
            # 同 id 已存在：更新非 status 字段（content/evidence/understanding/payload…），
            # status 不降级（防反 nag 失效）。
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET content=?, evidence=?, understanding=?,"
                f" payload=?, source_node=?, confidence=?, created_at=? WHERE id=?",
                (content, evidence, und, payload_json, source_node, confidence, now, rid),
            )
        c.commit()
    return rid


def pending_items(base_dir: Optional[Path] = None) -> list[dict]:
    """查所有 ``status='pending'`` 的台账项（created_at 倒序），供本地对接注入会话上下文。"""
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at FROM {LEDGER_TABLE}"
                f" WHERE status='pending' ORDER BY created_at DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # 表尚未建（首次运行前）→ 无 pending
    keys = ("id", "source", "kind", "content", "evidence", "understanding", "status", "confidence", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def build_handoff_opening(base_dir: Optional[Path] = None) -> Optional[dict]:
    """本地对接：生成开新会话时的对话式开场白（非浮层硬弹）。

    返回 ``{"text": 开场白, "items": [完整项…]}``；无 pending 返回 None。
    三件套：注入上下文（items 含 evidence+understanding）、稳定 id 句柄（回查用 items[i].id）、
    证据随行（防编造）。
    """
    items = pending_items(base_dir)
    if not items:
        return None
    lines = []
    for i, it in enumerate(items, 1):
        ev = f"（依据：{it['evidence']}）" if it.get("evidence") else ""
        un = f"\n    我的理解：{it['understanding']}" if it.get("understanding") else ""
        lines.append(f"{i}. {it['content']}{ev}{un}")
    text = (
        "我替你盯着这个家，发现几件事还没收：\n"
        + "\n".join(lines)
        + "\n你要先处理哪个？（回复序号或点开即可）"
    )
    return {"text": text, "items": items}


def acknowledge_ledger(
    base_dir: Optional[Path] = None,
    rid: Optional[str] = None,
    *,
    new_status: str,
) -> bool:
    """用户处理台账项：流转 status（pending→acknowledged/in_progress→actioned/dismissed）。

    仅允许前进或终态（不降级）；应用层兜底，同步层在跨端收敛时强制。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    if rid is None:
        return False
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        _ensure_table(c)
        cur = c.execute(f"SELECT status FROM {LEDGER_TABLE} WHERE id=?", (rid,)).fetchone()
        if cur is None:
            return False
        cur_status = cur[0]
        if (
            cur_status in STATUS_ORDER
            and new_status in STATUS_ORDER
            and STATUS_ORDER.index(new_status) > STATUS_ORDER.index(cur_status)
        ):
            return False  # 降级尝试（目标优先级更低）→ 拒绝
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        if new_status in ("actioned", "dismissed", "acknowledged", "in_progress"):
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET status=?, acknowledged_at=? WHERE id=?",
                (new_status, now, rid),
            )
        else:
            c.execute(f"UPDATE {LEDGER_TABLE} SET status=? WHERE id=?", (new_status, rid))
        c.commit()
    return True
