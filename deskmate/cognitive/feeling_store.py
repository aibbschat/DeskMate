"""FeelingStore — 小桔的情感层（``profile_items.layer='feeling'``）。

设计依据：reports/2026-11-16_小桔_情感层设计.md §三 / §四 R2–R5 / §五 P1。

情感 = 指向某个对象的、由多次经历沉积而成的、会反过来改变她反应方式的判断。
它住在 ``profile_items`` 的 ``feeling`` 层，**不新增表**：

- ``layer='feeling'``，``dimension='stance'``（dimension 是形态，不是领域）；
- ``topic=对象名``，``category=person|work|thing``；
- ``confidence`` 是分量（0–1），不参与任何激素计算；
- ``extra_json`` 存 ``object_ref/object_kind/valence/evidence/...``。

红线：
- **同对象只允许 1 条 active**：id 只由对象派生（``feeling_<sha1[:8]>``），
  valence 不参与 id。被改写时更新同一条，旧内容留在 ``item_history``，
  **不删除、不新增第二条**。
- **禁止** ``CognitiveStore.persist(layer='feeling')``：它是整层 DELETE+重插，
  会把情感史整层抹掉。本模块只做单条 upsert。
- ``content`` 永不删；R5 只缓降 ``confidence``，``status`` 保持 ``active``。
- 只有真实对象（person/work/thing）才建条目；``relation`` 不进本层。
- fail-open：读路径异常返回空，写路径异常向上抛（由调用方决定是否中断）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 允许建情感的对象类型（relation 明确排除）
FEELING_KINDS = ("person", "work", "thing")

#: R2 候选池：同一对象、≥3 条独立事件、跨 ≥7 天、≥3 个不同日期。
#: ``salience >= 0.85`` 特例在 P1 关闭（当前数据里几乎不可用）。
_POOL_SQL = """
SELECT object_ref, object_kind,
       COUNT(*) AS n,
       COUNT(DISTINCT date(ts)) AS days,
       COUNT(DISTINCT event_summary) AS summaries,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM emotion_log
WHERE object_kind IN ('person', 'work', 'thing')
  AND object_ref != ''
  AND event_summary != ''
  AND source NOT IN ('time_decay', 'pending', 'lapsed', 'landed', 'temper_foul')
  AND ts >= date('now', '-90 day')
GROUP BY object_ref, object_kind
HAVING n >= 3 AND days >= 3 AND summaries >= 3
   AND julianday(MAX(ts)) - julianday(MIN(ts)) >= 7
ORDER BY n DESC, days DESC, MAX(ts) DESC
"""

_EVIDENCE_SQL = """
SELECT id, ts, event_summary
FROM emotion_log
WHERE object_ref = ? AND object_kind = ?
  AND event_summary != ''
  AND source NOT IN ('time_decay', 'pending', 'lapsed', 'landed', 'temper_foul')
ORDER BY ts
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def feeling_id(object_kind: str, object_ref: str) -> str:
    """情感条目 id：**只由对象派生**，valence 不参与。

    同一对象的第二次回顾 = 修订同一条（``ON CONFLICT(id) DO UPDATE``）；
    矛盾写进同一条 content + ``valence='mixed'``。
    """
    key = f"{str(object_kind or '').strip()}|{str(object_ref or '').strip()}"
    return "feeling_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _parse_extra(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _date_of(ts: str) -> str:
    return str(ts or "")[:10]


class FeelingStore:
    """情感层读写（只碰 ``profile_items`` / ``item_history`` / ``memory_anchors``）。"""

    def __init__(self, db_path: Optional[str | Path] = None):
        self._db = Path(db_path) if db_path else (
            Path.home() / ".deskmate" / "deskmate.db"
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # 对象归一：只对齐已有对象，不自动建新人
    # ------------------------------------------------------------------
    def resolve_object(self, object_ref: str, object_kind: str) -> tuple[str, str]:
        """把对象 surface 形式对齐到已知人物/主题；对不上就原样保留。

        依据设计 §3.4：``normalized_ref`` 先过 ``known_persons`` / ``memory_anchors``
        规范化；对不上保留 surface 形式，**不自动建新人**。
        """
        ref = str(object_ref or "").strip()
        kind = str(object_kind or "").strip().lower()
        if not ref or kind not in FEELING_KINDS:
            return "", ""
        try:
            with self._connect() as conn:
                if kind == "person":
                    row = conn.execute(
                        "SELECT name FROM known_persons "
                        "WHERE name = ? OR (',' || aliases || ',') LIKE ? LIMIT 1",
                        (ref, f"%,{ref},%"),
                    ).fetchone()
                    if row:
                        # 别名命中 → 压到正式名；正式名命中 → 原样
                        return str(row["name"]), kind
                else:
                    row = conn.execute(
                        "SELECT anchor_value FROM memory_anchors "
                        "WHERE anchor_kind = 'topic' AND anchor_value = ? LIMIT 1",
                        (ref,),
                    ).fetchone()
                    if row:
                        return str(row["anchor_value"]), kind
        except Exception as e:  # noqa: BLE001 —— 归一是增强，失败保留 surface
            logger.debug("resolve_object 失败（保留 surface）: %s", e)
        return ref, kind

    # ------------------------------------------------------------------
    # R2 候选池
    # ------------------------------------------------------------------
    def build_pool(self, limit: int = 5, evidence_cap: int = 40) -> list[dict]:
        """返回过 R2 门槛的对象及其实证事件（只读）。

        每条 evidence 为 ``{src,id,ts,summary}``：``ts/summary`` 可跨端携带，
        ``id`` 供本地回查。``relation`` 不进池（SQL 里已限定 kind）。
        """
        out: list[dict] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(_POOL_SQL).fetchall()
                for row in rows:
                    ref, kind = str(row["object_ref"]), str(row["object_kind"])
                    evs = conn.execute(_EVIDENCE_SQL, (ref, kind)).fetchall()
                    # 超过上限时均匀取样：保住首尾，跨期不被截断
                    if len(evs) > evidence_cap:
                        step = (len(evs) - 1) / (evidence_cap - 1)
                        evs = [evs[round(i * step)] for i in range(evidence_cap)]
                    evidence = [
                        {
                            "src": "emotion_log",
                            "id": str(e["id"]),
                            "ts": str(e["ts"]),
                            "summary": str(e["event_summary"]),
                        }
                        for e in evs[-evidence_cap:]
                    ]
                    out.append(
                        {
                            "object_ref": ref,
                            "object_kind": kind,
                            "n": int(row["n"]),
                            "days": int(row["days"]),
                            "summaries": int(row["summaries"]),
                            "first_ts": str(row["first_ts"]),
                            "last_ts": str(row["last_ts"]),
                            "span_days": max(
                                0,
                                (
                                    datetime.fromisoformat(_date_of(row["last_ts"]))
                                    - datetime.fromisoformat(_date_of(row["first_ts"]))
                                ).days,
                            ),
                            "evidence": evidence,
                        }
                    )
                    if len(out) >= limit:
                        break
        except Exception as e:  # noqa: BLE001 —— 读路径 fail-open
            logger.warning("build_pool 失败（返回空池）: %s", e)
            return []
        return out

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, item_id: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM profile_items WHERE id=? AND layer='feeling'",
                    (item_id,),
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning("FeelingStore.get 失败: %s", e)
            return None
        if not row:
            return None
        d = dict(row)
        d["extra"] = _parse_extra(d.get("extra_json"))
        return d

    def list_active(self, limit: int = 50) -> list[dict]:
        """active 情感，分量降序。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM profile_items "
                    "WHERE layer='feeling' AND status='active' "
                    "ORDER BY confidence DESC, last_updated DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("FeelingStore.list_active 失败（返回空）: %s", e)
            return []
        out = []
        for row in rows:
            d = dict(row)
            d["extra"] = _parse_extra(d.get("extra_json"))
            out.append(d)
        return out

    def context_for(self, object_ref: str, limit: int = 2) -> list[dict]:
        """某对象的情感（R4 Call B 用）；对象对不上返回空。"""
        ref = str(object_ref or "").strip()
        if not ref:
            return []
        hits = [
            f for f in self.list_active(limit=200)
            if str(f.get("extra", {}).get("object_ref") or f.get("topic") or "") == ref
        ]
        return hits[:limit]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def upsert(
        self,
        *,
        object_ref: str,
        object_kind: str,
        content: str,
        evidence: list[dict],
        reason: str,
        valence: str = "mixed",
        confidence: Optional[float] = None,
        conflict_with: str = "",
        origin: str = "",
        now: Optional[str] = None,
    ) -> dict:
        """写一条情感（同对象幂等：修订同一条，旧内容留 item_history）。

        Args:
            evidence: ``[{src,id,ts,summary}, ...]``，必须真实。
            reason: 必填——没有理由的改写作废（与 ``record_reflection`` 同一硬断言）。
            confidence: ``None`` 表示沿用现值（含已淡过的值），新条目默认 0.4。

        Returns:
            ``{"id","action","object_ref","object_kind","content"}``
        """
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("feeling upsert 需要 reason（没有理由的情感不写）")
        content = str(content or "").strip()
        if not content:
            raise ValueError("feeling upsert 需要 content")

        ref, kind = self.resolve_object(object_ref, object_kind)
        if not ref or kind not in FEELING_KINDS:
            raise ValueError(f"非法情感对象: {object_ref!r}/{object_kind!r}")

        fid = feeling_id(kind, ref)
        now = now or _now_iso()
        clean_ev = [
            {
                "src": str(e.get("src") or ""),
                "id": str(e.get("id") or ""),
                "ts": str(e.get("ts") or ""),
                "summary": str(e.get("summary") or ""),
            }
            for e in (evidence or [])
            if isinstance(e, dict) and (e.get("id") or e.get("summary"))
        ]

        with self._connect() as conn:
            row = conn.execute(
                "SELECT content, confidence, extra_json, first_extracted "
                "FROM profile_items WHERE id=? AND layer='feeling'",
                (fid,),
            ).fetchone()

            old_extra = _parse_extra(row["extra_json"]) if row else {}
            old_content = str(row["content"]) if row else ""

            # 证据累积（按 src+id 去重），不因一次回顾丢掉旧证据
            merged: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for e in list(old_extra.get("evidence") or []) + clean_ev:
                key = (str(e.get("src") or ""), str(e.get("id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(e)
            merged = merged[-40:]

            dates = sorted({_date_of(e.get("ts")) for e in merged if e.get("ts")})
            span_days = 0
            if len(dates) >= 2:
                span_days = (
                    datetime.fromisoformat(dates[-1])
                    - datetime.fromisoformat(dates[0])
                ).days

            if confidence is None:
                confidence = float(row["confidence"]) if row else 0.4
            confidence = max(0.0, min(1.0, float(confidence)))

            action = "revised" if row else "created"
            review_count = int(old_extra.get("review_count") or 0) + (1 if row else 0)
            extra = {
                **old_extra,
                "object_ref": ref,
                "object_kind": kind,
                "valence": str(valence or "mixed"),
                "evidence": merged,
                "conflict_with": str(conflict_with or ""),
                "supersedes": str(old_extra.get("supersedes") or ""),
                "last_touched": now,
                "evidence_count": len(merged),
                "span_days": span_days,
                "review_count": review_count,
            }
            if origin:
                extra["origin"] = origin

            conn.execute(
                "INSERT INTO profile_items"
                "(id, layer, dimension, topic, content, category, status, confidence,"
                " extra_json, first_extracted, last_updated, valid_from)"
                " VALUES(?, 'feeling', 'stance', ?, ?, ?, 'active', ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  content=excluded.content,"
                "  category=excluded.category,"
                "  status='active',"
                "  confidence=excluded.confidence,"
                "  extra_json=excluded.extra_json,"
                "  last_updated=excluded.last_updated,"
                "  valid_until=NULL",
                (
                    fid, ref, content, kind, confidence,
                    json.dumps(extra, ensure_ascii=False), now, now, now,
                ),
            )

            # 留痕：旧内容不删，进 item_history（情感史）
            conn.execute(
                "INSERT OR REPLACE INTO item_history "
                "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                "VALUES(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM item_history "
                "WHERE item_id=?), ?, ?, ?, ?, 'feeling', ?)",
                (fid, fid, action, old_content, content, reason, now),
            )
            conn.commit()

        # 对象锚（memory_anchors 是对象索引视角，不新增表）
        try:
            from deskmate.cognitive.store import CognitiveStore

            anchor_kind = "person" if kind == "person" else "topic"
            CognitiveStore(db_path=self._db).upsert_anchors(
                item_kind="profile_item",
                item_id=fid,
                anchors=[(anchor_kind, ref)],
            )
        except Exception as e:  # noqa: BLE001 —— 锚失败不影响情感本体
            logger.warning("写情感对象锚失败（忽略）: %s", e)

        return {
            "id": fid,
            "action": action,
            "object_ref": ref,
            "object_kind": kind,
            "content": content,
            "confidence": confidence,
        }

    def touch(self, item_id: str, now: Optional[str] = None) -> bool:
        """记一次触碰（新证据 / 被查询 / R4 使用）。

        触碰只更新 ``extra_json.last_touched``，**不动 confidence、不动 content**。
        """
        row = self.get(item_id)
        if not row:
            return False
        extra = dict(row.get("extra") or {})
        extra["last_touched"] = now or _now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE profile_items SET extra_json=? WHERE id=?",
                    (json.dumps(extra, ensure_ascii=False), item_id),
                )
                conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("FeelingStore.touch 失败（忽略）: %s", e)
            return False

    def fade_stale(
        self,
        days: int = 60,
        rate: float = 0.9,
        floor: float = 0.1,
        now: Optional[datetime] = None,
    ) -> int:
        """R5 淡化：≥``days`` 天无触碰 → 分量 ×``rate``，**content 永不删**。

        按月级缓降：同一条 30 天内最多淡一次（``last_faded``），否则每次周更
        都乘 0.9，一个月就会被淡四次——那是把「月级」做成了「周级」。

        Returns:
            实际淡化的条数。
        """
        now = now or datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        faded = 0
        for item in self.list_active(limit=500):
            extra = dict(item.get("extra") or {})
            touched_raw = extra.get("last_touched") or item.get("last_updated")
            try:
                touched = datetime.fromisoformat(str(touched_raw))
            except Exception:
                continue
            if (now - touched) < timedelta(days=days):
                continue
            last_faded = extra.get("last_faded")
            if last_faded:
                try:
                    if (now - datetime.fromisoformat(str(last_faded))) < timedelta(days=30):
                        continue
                except Exception:
                    pass

            old_conf = float(item.get("confidence") or 0.0)
            new_conf = max(floor, round(old_conf * rate, 4))
            if new_conf == old_conf:
                continue
            extra["last_faded"] = now_iso
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE profile_items SET confidence=?, extra_json=?, last_updated=?"
                        " WHERE id=? AND layer='feeling'",
                        (
                            new_conf,
                            json.dumps(extra, ensure_ascii=False),
                            now_iso,
                            item["id"],
                        ),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO item_history "
                        "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                        "VALUES(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM item_history "
                        "WHERE item_id=?), 'faded', ?, ?, ?, 'feeling', ?)",
                        (
                            item["id"], item["id"], str(old_conf), str(new_conf),
                            f"{days} 天没再碰到这件事", now_iso,
                        ),
                    )
                    conn.commit()
                faded += 1
            except Exception as e:  # noqa: BLE001 —— 单条失败不影响其余
                logger.warning("fade_stale 失败（跳过 %s）: %s", item.get("id"), e)
        return faded
