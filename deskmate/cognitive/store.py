"""
认知层持久化存储——可拓展 SQLite（统一库 deskmate.db）。

设计目标（萧哥定）：认知层只是第一步，按设计后面还有情感层等，所以
schema 不能把五维写死，必须"加一层不改表"。

实现：用「层 + 维度」通用表，所有认知条目进同一张 profile_items，
靠 layer 列区分层（cognitive / 未来 emotional），靠 dimension 列区分维度
（stance / argument_pattern / value_ranking / thinking_framework），维度特有的
结构化字段（ranking / steps / tags / pattern_type ...）塞进 extra_json。

加新层 = 往 profile_items 插 layer='emotional' 的行，零 DDL 变更。

表：
- profile_meta         画像级 KV 元数据（version/extraction_count/last_interview_at...）
- profile_items       通用认知条目（层+维度+内容+状态+置信度+extra_json）
- item_sources        条目来源（可追溯）
- item_history        条目变更留痕（created/updated/questioned/superseded/calibrated）
- cognitive_gaps      认知盲区（带 layer 列，情感层盲区同样装）
- interview_sessions  访谈会话（骨架，供未来 InterviewEngine）
- interview_qa        访谈问答（骨架）

真相源 = 本库；markdown（diary_memory.md / 小桔眼中的萧盛.md）只是派生视图。
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .models import (
    CognitiveProfile,
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    CognitiveGap,
    SourceRef,
    HistoryEntry,
)

# 维度 → (dataclass, topic 字段名, extra_json 键)
_DIM_MAP = {
    "stance": ("stance", "topic"),
    "argument_pattern": ("argument_pattern", None),
    "value_ranking": ("value_ranking", "domain"),
    "thinking_framework": ("thinking_framework", "domain"),
}

_LAYER = "cognitive"


def _default_db_path() -> Path:
    return Path(os.path.expanduser("~/.deskmate/deskmate.db"))


def _default_legacy_json() -> Path:
    return Path(os.path.expanduser("~/.deskmate/cognitive_profile.json"))


class CognitiveStore:
    """认知层 SQLite 存储。可拓展（layer + dimension 通用表）。"""

    def __init__(self, db_path: Optional[str | Path] = None, legacy_json: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._legacy_json = Path(legacy_json) if legacy_json else _default_legacy_json()
        self._init_schema()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    @contextmanager
    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS profile_items (
                    id            TEXT PRIMARY KEY,
                    layer         TEXT NOT NULL DEFAULT 'cognitive',
                    dimension     TEXT NOT NULL,
                    topic         TEXT DEFAULT '',
                    content       TEXT DEFAULT '',
                    category      TEXT DEFAULT '',
                    status        TEXT DEFAULT 'active',
                    confidence    REAL DEFAULT 0.5,
                    extra_json    TEXT DEFAULT '{}',
                    first_extracted TEXT,
                    last_updated  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_items_layer_dim_status
                    ON profile_items(layer, dimension, status);
                CREATE INDEX IF NOT EXISTS idx_items_layer_topic
                    ON profile_items(layer, topic);

                CREATE TABLE IF NOT EXISTS item_sources (
                    item_id  TEXT NOT NULL,
                    idx      INTEGER NOT NULL,
                    type     TEXT DEFAULT '',
                    title    TEXT DEFAULT '',
                    excerpt  TEXT DEFAULT '',
                    date     TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.5,
                    PRIMARY KEY (item_id, idx)
                );

                CREATE TABLE IF NOT EXISTS item_history (
                    item_id  TEXT NOT NULL,
                    seq      INTEGER NOT NULL,
                    action   TEXT DEFAULT '',
                    old_value TEXT DEFAULT '',
                    new_value TEXT DEFAULT '',
                    reason   TEXT DEFAULT '',
                    source   TEXT DEFAULT '',
                    ts       TEXT DEFAULT '',
                    PRIMARY KEY (item_id, seq)
                );

                CREATE TABLE IF NOT EXISTS cognitive_gaps (
                    id            TEXT PRIMARY KEY,
                    layer         TEXT NOT NULL DEFAULT 'cognitive',
                    topic         TEXT DEFAULT '',
                    reason        TEXT DEFAULT '',
                    priority      REAL DEFAULT 0.5,
                    related_items_json TEXT DEFAULT '[]',
                    status        TEXT DEFAULT 'open',
                    created_at    TEXT,
                    resolved_at   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_gaps_layer_status
                    ON cognitive_gaps(layer, status);

                CREATE TABLE IF NOT EXISTS interview_sessions (
                    id          TEXT PRIMARY KEY,
                    layer       TEXT NOT NULL DEFAULT 'cognitive',
                    status      TEXT DEFAULT 'created',
                    started_at  TEXT,
                    ended_at    TEXT,
                    gap_ids_json TEXT DEFAULT '[]',
                    summary     TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS interview_qa (
                    id           TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    question_id  TEXT DEFAULT '',
                    question     TEXT DEFAULT '',
                    answer       TEXT DEFAULT '',
                    item_updates_json TEXT DEFAULT '[]',
                    created_at   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qa_session
                    ON interview_qa(session_id, created_at);
                """
            )

    # ------------------------------------------------------------------
    # 迁移（幂等）
    # ------------------------------------------------------------------
    def is_empty(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM profile_items WHERE layer=?", (_LAYER,)
            ).fetchone()
            return (row[0] == 0) if row else True

    def migrate_from_legacy(self, json_path: Optional[str | Path] = None) -> dict:
        """
        幂等迁移：仅当 DB 空且 legacy JSON 存在时执行。
        迁移后把原 JSON 备份为 .bak（不删除，确认无误前保留）。
        返回 {"migrated": bool, "items": int, "gaps": int, "note": str}
        """
        src = Path(json_path) if json_path else self._legacy_json
        if not self.is_empty():
            return {"migrated": False, "items": 0, "gaps": 0, "note": "DB 已有数据，跳过迁移"}
        if not src.exists():
            return {"migrated": False, "items": 0, "gaps": 0, "note": "无 legacy JSON，初始化空库"}

        try:
            profile = CognitiveProfile.from_json(src.read_text(encoding="utf-8"))
        except Exception as e:
            return {"migrated": False, "items": 0, "gaps": 0, "note": f"legacy JSON 解析失败: {e}"}

        self.persist(profile)
        # 备份原 JSON
        try:
            shutil.copy2(src, str(src) + ".bak-migrated")
        except Exception:
            pass
        n_items = (
            len(profile.stances) + len(profile.argument_patterns)
            + len(profile.value_rankings) + len(profile.thinking_frameworks)
        )
        return {
            "migrated": True,
            "items": n_items,
            "gaps": len(profile.cognitive_gaps),
            "note": f"已迁移并备份为 {src}.bak-migrated",
        }

    # ------------------------------------------------------------------
    # 持久化（全量 upsert，规模小、简单且正确）
    # ------------------------------------------------------------------
    def persist(self, profile: CognitiveProfile, layer: str = _LAYER):
        with self._connect() as conn:
            # meta
            meta = {
                "version": profile.version,
                "created_at": profile.created_at,
                "last_updated": profile.last_updated,
                "extraction_count": profile.extraction_count,
                "calibration_count": profile.calibration_count,
                "summary": profile.summary,
            }
            conn.executemany(
                "INSERT INTO profile_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()],
            )

            # 清空本层旧数据再写（规模小，简单可靠）
            conn.execute("DELETE FROM profile_items WHERE layer=?", (layer,))
            conn.execute("DELETE FROM item_sources WHERE item_id NOT IN (SELECT id FROM profile_items)")
            conn.execute("DELETE FROM item_history WHERE item_id NOT IN (SELECT id FROM profile_items)")
            conn.execute("DELETE FROM cognitive_gaps WHERE layer=?", (layer,))

            rows, src_rows, hist_rows, gap_rows = [], [], [], []
            for dim, items in (
                ("stance", profile.stances),
                ("argument_pattern", profile.argument_patterns),
                ("value_ranking", profile.value_rankings),
                ("thinking_framework", profile.thinking_frameworks),
            ):
                for it in items:
                    topic = getattr(it, _DIM_MAP[dim][1] or "", "") if _DIM_MAP[dim][1] else ""
                    extra = self._item_extra(dim, it)
                    rows.append((
                        it.id, layer, dim, topic, it.content, it.category,
                        it.status, it.confidence, json.dumps(extra, ensure_ascii=False),
                        it.first_extracted, it.last_updated,
                    ))
                    for i, s in enumerate(it.sources):
                        src_rows.append((it.id, i, s.type, s.title, s.excerpt, s.date, s.confidence))
                    for j, h in enumerate(it.history):
                        hist_rows.append((
                            it.id, j, h.action, h.old_value, h.new_value,
                            h.reason, h.source, h.timestamp,
                        ))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO profile_items"
                    "(id, layer, dimension, topic, content, category, status, confidence, extra_json, first_extracted, last_updated) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            if src_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO item_sources"
                    "(item_id, idx, type, title, excerpt, date, confidence) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    src_rows,
                )
            if hist_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO item_history"
                    "(item_id, seq, action, old_value, new_value, reason, source, ts) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    hist_rows,
                )
            for g in profile.cognitive_gaps:
                gap_rows.append((
                    g.id, layer, g.topic, g.reason, g.priority,
                    json.dumps(g.related_items, ensure_ascii=False), g.status,
                    g.created_at, getattr(g, "resolved_at", "") or "",
                ))
            if gap_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO cognitive_gaps"
                    "(id, layer, topic, reason, priority, related_items_json, status, created_at, resolved_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    gap_rows,
                )

    @staticmethod
    def _item_extra(dim: str, it) -> dict:
        extra = {"tags": list(getattr(it, "tags", []))}
        if dim == "argument_pattern":
            extra["pattern_type"] = getattr(it, "pattern_type", "")
            extra["evidence_preference"] = getattr(it, "evidence_preference", "")
        elif dim == "value_ranking":
            extra["ranking"] = list(getattr(it, "ranking", []))
            extra["tradeoff_notes"] = getattr(it, "tradeoff_notes", "")
        elif dim == "thinking_framework":
            extra["steps"] = list(getattr(it, "steps", []))
        return extra

    # ------------------------------------------------------------------
    # 读取（重建 CognitiveProfile）
    # ------------------------------------------------------------------
    def load_profile(self, layer: str = _LAYER) -> CognitiveProfile:
        with self._connect() as conn:
            meta = dict(
                (r[0], r[1]) for r in conn.execute("SELECT key, value FROM profile_meta")
            )
            profile = CognitiveProfile(
                version=json.loads(meta.get("version", '"1.0"')),
                created_at=json.loads(meta.get("created_at", '""')),
                last_updated=json.loads(meta.get("last_updated", '""')),
                extraction_count=json.loads(meta.get("extraction_count", "0")),
                calibration_count=json.loads(meta.get("calibration_count", "0")),
                summary=json.loads(meta.get("summary", '""')),
            )

            stances, args, vals, thinks = [], [], [], []
            for row in conn.execute(
                "SELECT id, dimension, topic, content, category, status, confidence, extra_json, "
                "first_extracted, last_updated FROM profile_items WHERE layer=? ORDER BY id",
                (layer,),
            ):
                (rid, dim, topic, content, category, status, confidence, extra_json,
                 first_extracted, last_updated) = row
                extra = json.loads(extra_json or "{}")
                sources = self._load_sources(conn, rid)
                history = self._load_history(conn, rid)
                if dim == "stance":
                    stances.append(Stance(
                        id=rid, topic=topic, content=content, category=category,
                        status=status, confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                    ))
                elif dim == "argument_pattern":
                    args.append(ArgumentPattern(
                        id=rid, content=content, category=category, status=status,
                        confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                        pattern_type=extra.get("pattern_type", ""),
                        evidence_preference=extra.get("evidence_preference", ""),
                    ))
                elif dim == "value_ranking":
                    vals.append(ValueRanking(
                        id=rid, domain=topic, content=content, category=category,
                        status=status, confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                        ranking=extra.get("ranking", []),
                        tradeoff_notes=extra.get("tradeoff_notes", ""),
                    ))
                elif dim == "thinking_framework":
                    thinks.append(ThinkingFramework(
                        id=rid, domain=topic, content=content, category=category,
                        status=status, confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                        steps=extra.get("steps", []),
                    ))
            profile.stances = stances
            profile.argument_patterns = args
            profile.value_rankings = vals
            profile.thinking_frameworks = thinks

            gaps = []
            for row in conn.execute(
                "SELECT id, topic, reason, priority, related_items_json, status, created_at, "
                "resolved_at FROM cognitive_gaps WHERE layer=?", (layer,)
            ):
                (gid, topic, reason, priority, rel_json, status, created_at,
                 resolved_at) = row
                gaps.append(CognitiveGap(
                    id=gid, topic=topic, reason=reason, priority=priority,
                    related_items=json.loads(rel_json or "[]"), status=status,
                    created_at=created_at, resolved_at=resolved_at or "",
                ))
            profile.cognitive_gaps = gaps
            return profile

    @staticmethod
    def _load_sources(conn, item_id) -> list[SourceRef]:
        rows = conn.execute(
            "SELECT type, title, excerpt, date, confidence FROM item_sources "
            "WHERE item_id=? ORDER BY idx", (item_id,)
        ).fetchall()
        return [SourceRef(type=r[0], title=r[1], excerpt=r[2], date=r[3], confidence=r[4]) for r in rows]

    @staticmethod
    def _load_history(conn, item_id) -> list[HistoryEntry]:
        rows = conn.execute(
            "SELECT action, old_value, new_value, reason, source, ts FROM item_history "
            "WHERE item_id=? ORDER BY seq", (item_id,)
        ).fetchall()
        return [HistoryEntry(action=r[0], old_value=r[1], new_value=r[2], reason=r[3], source=r[4], timestamp=r[5]) for r in rows]

    # ------------------------------------------------------------------
    # meta KV（last_interview_at 等）
    # ------------------------------------------------------------------
    def set_meta(self, key: str, value):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO profile_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_meta(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM profile_meta WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row[0])
            except Exception:
                return row[0]

    # ------------------------------------------------------------------
    # 访谈骨架
    # ------------------------------------------------------------------
    def create_session(self, layer: str = _LAYER, gap_ids: Optional[list[str]] = None) -> str:
        import uuid

        sid = f"iv_{uuid.uuid4().hex[:10]}"
        from .models import _now

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO interview_sessions(id, layer, status, started_at, gap_ids_json) "
                "VALUES(?, ?, 'active', ?, ?)",
                (sid, layer, _now(), json.dumps(gap_ids or [], ensure_ascii=False)),
            )
        # 记录最近访谈时间（供 should_interview_now）——按层写，避免情感 session 污染认知 key
        meta_key = f"last_interview_at:{layer}" if layer != _LAYER else "last_interview_at"
        self.set_meta(meta_key, _now())
        return sid

    def finish_session(self, session_id: str, summary: str = ""):
        from .models import _now

        with self._connect() as conn:
            conn.execute(
                "UPDATE interview_sessions SET status='done', ended_at=?, summary=? WHERE id=?",
                (_now(), summary, session_id),
            )

    def add_qa(self, session_id: str, question: str, answer: str,
               question_id: str = "", item_updates: Optional[list[str]] = None) -> str:
        import uuid

        from .models import _now

        qid = f"qa_{uuid.uuid4().hex[:10]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO interview_qa(id, session_id, question_id, question, answer, item_updates_json, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (qid, session_id, question_id, question, answer,
                 json.dumps(item_updates or [], ensure_ascii=False), _now()),
            )
        return qid

    def list_sessions(self, layer: str = _LAYER) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, layer, status, started_at, ended_at, gap_ids_json, summary "
                "FROM interview_sessions WHERE layer=? ORDER BY started_at DESC",
                (layer,),
            ).fetchall()
        return [
            {"id": r[0], "layer": r[1], "status": r[2], "started_at": r[3],
             "ended_at": r[4], "gap_ids": json.loads(r[5] or "[]"), "summary": r[6]}
            for r in rows
        ]

    def get_session(self, session_id: str) -> Optional[dict]:
        """按 id 查单个 session（不过滤 layer）。用于回答时反查真实层。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, layer, status, started_at, ended_at, gap_ids_json, summary "
                "FROM interview_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "layer": row[1], "status": row[2], "started_at": row[3],
                "ended_at": row[4], "gap_ids": json.loads(row[5] or "[]"), "summary": row[6]}

    def get_qa(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, question_id, question, answer, item_updates_json, created_at "
                "FROM interview_qa WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [
            {"id": r[0], "question_id": r[1], "question": r[2], "answer": r[3],
             "item_updates": json.loads(r[4] or "[]"), "created_at": r[5]}
            for r in rows
        ]
