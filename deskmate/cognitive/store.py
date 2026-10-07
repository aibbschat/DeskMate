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

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 写库来源追踪（D 项心跳 / 定位绕过引擎的直写）：记录最近一次 persist() 调用方。
# 注意：persist 通常由 OpenWorker 主进程调用，与本同步 daemon 不同进程；
# daemon 心跳读不到时返回 n/a（见 sync_daemon_runner._last_persist_source_best_effort）。
_LAST_PERSIST_SOURCE = {"module": "?", "func": "?", "line": 0, "ts": 0.0}


def get_last_persist_source() -> dict:
    """返回最近一次 persist() 调用方信息（只读，供心跳/排查使用）。"""
    return dict(_LAST_PERSIST_SOURCE)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# P1 汇流：锚的确定性主键。分隔符用 US(\x1f)——正文/锚值里不会出现，避免拼接歧义。
# 双端「同一条锚」算出同一 row_key → 同 sync_uuid → 同步自动合并（确定性，禁随机 uuid）。
_ANCHOR_SEP = "\x1f"


def anchor_row_key(item_kind: str, item_id: str, anchor_kind: str, anchor_value: str) -> str:
    """memory_anchors 的确定性主键（跨端一致）。"""
    return _ANCHOR_SEP.join(
        (str(item_kind), str(item_id), str(anchor_kind), str(anchor_value))
    )


# P1 汇流（R3）：顺锚走一跳的邻座上限（防爆炸；P1 量小，5 条足够）。
_ANCHOR_NEIGHBOR_CAP = 5

# P2-R1（2026-10-07）：person 锚自生长转正门槛。运行时发现的新人名先记 candidate，
# 同一名字累计出现 >= 此值才升 active、开始产 person 锚（防「王/李」高频字与路人甲噪声）。
_PERSON_PROMOTE_THRESHOLD = 3

# 锚类型 → 人话标签（anchor_evidence 用，如「同主题：历史真实性」）。
_ANCHOR_KIND_LABELS = {
    "topic": "主题", "book": "书目", "person": "人物",
    "event": "事件", "project": "项目",
}


def _anchor_kind_label(kind: str) -> str:
    return _ANCHOR_KIND_LABELS.get(kind, kind)


def _parse_iso(s: str | None) -> datetime.datetime | None:
    """宽容解析 ISO 时间（支持带/不带 Z、偏移、纯日期），统一返回无时区 datetime。

    用于双时态 as-of 比较（所有时间戳同机同格式，去时区后直接比对）。
    """
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.datetime.fromisoformat(s + "T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# 双时态回放时，哪些 history 动作携带真实内容变更（其余如 questioned/superseded
# 只改状态/置信度，不改 content 取值）。
# 2026-09-23 巡检修复：补 ``calibrated``——用户校准会替换正文
# （profile.py 里 new_value=corrected_content 是真实内容），缺它会导致
# 回放把「已被用户纠正过的话」退回到纠正前的版本。
_CONTENT_ACTIONS = frozenset(
    {"created", "updated", "deepened", "restored", "calibrated"}
)

# 状态字面量：部分 history 行的 new_value 写的是**状态**而非内容
# （典型： repair 流程 ``action="restored", new_value="active"``）。
# 回放时必须忽略这类值，否则会把正文污染成 "active"。
_STATUS_LITERALS = frozenset(
    {"active", "questioned", "superseded", "archived", "forgotten", "draft", "pending"}
)

# 已走到终态的认知（被取代/被遗忘等）：其有效性区间应视为「已闭合」。
_TERMINAL_STATUSES = frozenset({"superseded", "archived", "forgotten"})


def _replay_value(action: str, new_value: str | None) -> str | None:
    """该 history 行的 new_value 能否当作「当时的正文」。

    返回 None 表示不能（纯状态变更 / 空的误写 / 状态字面量），回放应保留上一个内容。
    """
    if action not in _CONTENT_ACTIONS:
        return None
    val = (new_value or "").strip()
    if not val:
        return None  # profile.py 有 3 处 updated 漏写 new_value → 不可当空内容
    if val.lower() in _STATUS_LITERALS:
        return None  # 状态字面量（restored 那一处），不是正文
    return new_value


def _derived_valid_until(status: str | None, last_updated: str | None) -> str | None:
    """生产写入时 valid_until 恒为 NULL（开放区间），但已走到终态的条目
    其实已经「不再有效」——这里按 last_updated 推导出闭合时间，供 UI / 回放展示。

    活跃条目返回 None（= 至今有效），不做任何猜测。
    """
    if (status or "").lower() in _TERMINAL_STATUSES and last_updated:
        return last_updated
    return None

from deskmate.paths import deskmate_db_path, deskmate_legacy_profile_path
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


# ---------------------------------------------------------------------------
# P1-2 范围化注入（2026-09-26）：profile_items 的 FTS5 索引与召回
#
# 与 coworker/history/fts5.py 同一套约定（别发明第二套）：
# 索引侧 content 预处理 = CJK 字符间插空格（unicode61 按空格切单字）；
# 查询侧 = 每段连续 CJK 串 → 引号短语。同步必须与条目写入**同一事务**
# —— 防「记了查不到」（P2-3 的历史坑）。
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
_MULTI_SPACE_RE = re.compile(r"\s+")
_FTS_META_RE = re.compile(r'["*^()]')


def tokenize_cjk(text: str) -> str:
    """在 CJK 字符间插空格（与 fts5.js ``tokenizeCJK`` / build_history_index 等价）。"""
    return _MULTI_SPACE_RE.sub(" ", _CJK_RE.sub(r" \1 ", text or "")).strip()


def _fts_match(query: str, max_parts: int = 40) -> str:
    """把自然语言 query 转成 profile_items_fts 的 MATCH 表达式。

    单字索引上**长短语几乎匹配不到**（整句要求逐字相邻），故长 CJK 串
    拆成滑动二元短语（bigram）OR 连接——与 history 通道的「bigram 档」
    同一思路。纯英文词直接引号；去 FTS5 语法元字符防查询注入。
    无可匹配内容返回空串。
    """
    cleaned = _FTS_META_RE.sub(" ", query or "")
    runs = [r for r in re.split(r"[^0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+", cleaned) if r]
    parts: list[str] = []
    for run in runs:
        if _CJK_RE.search(run):
            chars = "".join(_CJK_RE.findall(run))
            if len(chars) <= 2:
                parts.append('"' + " ".join(chars) + '"')
            else:
                for i in range(len(chars) - 1):
                    parts.append(f'"{chars[i]} {chars[i + 1]}"')
        elif len(run) >= 2:
            parts.append(f'"{run}"')
        if len(parts) >= max_parts:
            break
    # 去重保序
    seen: set[str] = set()
    uniq = [p for p in parts if not (p in seen or seen.add(p))]
    return " OR ".join(uniq[:max_parts])


def _default_db_path() -> Path:
    return deskmate_db_path()


def _default_legacy_json() -> Path:
    return deskmate_legacy_profile_path()


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

    def _add_column(self, table: str, col: str, col_type: str) -> None:
        """兼容已存在的库：缺失列才补（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
        try:
            with self._connect() as conn:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.Error:
            pass

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

                -- P1 汇流最小版（2026-10-07）：共享锚表。
                -- 条目抽取时顺手抽「锚」（人/事/主题/项目/书目），同锚即「邻座」；
                -- 召回命中种子后顺锚走一跳召回邻座。纯文本、无向量列（可进 SYNC_TABLES）。
                -- row_key = 确定性 <kind>\x1f<id>\x1f<anchor_kind>\x1f<value>（双端同锚同 key
                -- → 同 sync_uuid → 自动合并）；四列唯一性另用 UNIQUE 兜底。
                CREATE TABLE IF NOT EXISTS memory_anchors (
                    row_key      TEXT PRIMARY KEY,
                    item_kind    TEXT NOT NULL,
                    item_id      TEXT NOT NULL,
                    anchor_kind  TEXT NOT NULL,
                    anchor_value TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    UNIQUE (item_kind, item_id, anchor_kind, anchor_value)
                );
                -- 同锚邻座召回（反查索引）
                CREATE INDEX IF NOT EXISTS idx_anchor_lookup
                    ON memory_anchors(anchor_kind, anchor_value, item_kind);

                -- P2-R1（2026-10-07）：已知人名注册表（person 锚源）。
                -- 取代原 env 手工逗号列表（对「人物浩如烟海」的历史写作 = 默认沉睡）。
                -- 两路来源：seed（LLM 扫已出版书，直接 active）与 auto（运行时发现）；
                -- auto 走 mode B 自生长——先进 candidate，累计 seen_count>=阈值才转 active。
                CREATE TABLE IF NOT EXISTS known_persons (
                    name       TEXT PRIMARY KEY,
                    aliases    TEXT DEFAULT '',
                    source     TEXT DEFAULT 'seed',    -- 'seed'(扫书) | 'auto'(运行时发现)
                    status     TEXT DEFAULT 'active',  -- 'candidate' | 'active'
                    seen_count INTEGER DEFAULT 0,      -- 运行时发现的累计出现次数
                    created_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_known_persons_status ON known_persons(status);
                CREATE INDEX IF NOT EXISTS idx_known_persons_name   ON known_persons(name);

                -- P2-R3（2026-10-07）：lessons 表——小桔从萧哥纠正/反馈里学到的条目，
                -- 每轮注入对话（江河设计 §4.5 教学层）。scope='permanent' 的规则常驻；
                -- 其余为 recent，按 created_at 取最近 N 条。lesson_id 用内容 md5 去重（幂等）。
                CREATE TABLE IF NOT EXISTS teachings (
                    lesson_id  TEXT PRIMARY KEY,
                    content    TEXT NOT NULL,
                    scope      TEXT DEFAULT 'recent',    -- 'recent' | 'permanent'
                    source     TEXT DEFAULT 'manual',    -- 'manual' | 'feedback'
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_teachings_scope   ON teachings(scope);
                CREATE INDEX IF NOT EXISTS idx_teachings_created ON teachings(created_at);

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
        # 双时态（P1-2）：profile_items 显式有效性区间。
        # valid_from = 该认知自何时持有（写入时取 first_extracted）；
        # valid_until = NULL 表示「至今有效」（开放区间）。
        self._add_column("profile_items", "valid_from", "TEXT")
        self._add_column("profile_items", "valid_until", "TEXT")
        # P1-2 范围化注入：profile_items 的 FTS 索引（unicode61 + 字间空格，
        # 与 coworker/history/fts5.py 同一套约定）。
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS profile_items_fts "
                    "USING fts5(item_id UNINDEXED, layer UNINDEXED, content, "
                    "tokenize='unicode61')"
                )
                # 自愈回填：FTS 与条目**数量不符**即重建。
                # （正常写入走 persist/delete_item 的同事务同步，不会落到这。）
                #
                # 2026-09-26 质检修正：原判据是「FTS 为空才算失步」（n_fts == 0），
                # 漏掉两类真实失步——① 外部脚本直接 INSERT profile_items 绕过
                # persist（如 scripts/restore_cognitive_layer.py）；② 某次 persist
                # 的 _rebuild_fts 抛错被 catch。这两类下 FTS 非空 → 旧判据不重建
                # → 新条目**永久搜不到**（范围化注入静默失效，只剩核心层）。
                # 改为数量比对：多、少、空三种失步都自愈。表很小，重建成本可忽略。
                n_fts = conn.execute("SELECT COUNT(*) FROM profile_items_fts").fetchone()[0]
                n_items = conn.execute("SELECT COUNT(*) FROM profile_items").fetchone()[0]
                if n_items > 0 and n_fts != n_items:
                    self._rebuild_fts(conn)
        except sqlite3.Error:
            pass  # FTS 建表/回填失败不阻断初始化（fail-open：召回退全量）

    @staticmethod
    def _rebuild_fts(conn: sqlite3.Connection) -> None:
        """全量重建 profile_items_fts（须在**同一事务**内随条目写入调用）。"""
        conn.execute("DELETE FROM profile_items_fts")
        rows = conn.execute(
            "SELECT id, layer, COALESCE(topic,''), COALESCE(content,'') "
            "FROM profile_items"
        ).fetchall()
        conn.executemany(
            "INSERT INTO profile_items_fts(item_id, layer, content) VALUES(?, ?, ?)",
            [
                (rid, layer, tokenize_cjk(f"{topic} {content}"))
                for rid, layer, topic, content in rows
            ],
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
    def persist(self, profile: CognitiveProfile, layer: str = _LAYER,
                force: bool = False):
        """持久化某一层（全量覆盖）。

        ⚠ 防清空护栏（2026-09-07 事故后加）：
        ``persist`` 是 ``DELETE FROM profile_items WHERE layer=?`` 全量覆盖，
        一旦调用方手里的 profile 不完整（典型事故：测试误用全局 manager、
        load 失败返回空 profile），一次 save 就会**整层数据归零**——
        09-07 就因为这条丢了 cognitive 层 37 条真实画像。

        因此：库里本层有数据、而要写入的条目为 0 时，默认**拒绝写入**并抛错。
        确有清空需求（如重置某层）请显式传 ``force=True``。
        """
        # D 项：记录最近一次 persist 调用方（定位谁在写认知层）
        try:
            _fr = sys._getframe(1)
            _LAST_PERSIST_SOURCE["module"] = os.path.basename(_fr.f_code.co_filename)
            _LAST_PERSIST_SOURCE["func"] = _fr.f_code.co_name
            _LAST_PERSIST_SOURCE["line"] = _fr.f_lineno
            _LAST_PERSIST_SOURCE["ts"] = time.time()
            logger.debug(
                "persist() 调用方: %s:%s:%d layer=%s",
                _LAST_PERSIST_SOURCE["module"], _LAST_PERSIST_SOURCE["func"],
                _LAST_PERSIST_SOURCE["line"], layer,
            )
        except Exception:
            pass
        incoming = (
            len(profile.stances)
            + len(profile.argument_patterns)
            + len(profile.value_rankings)
            + len(profile.thinking_frameworks)
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM profile_items WHERE layer=?", (layer,)
            ).fetchone()[0]
            if existing > 0 and incoming == 0 and not force:
                logger.error(
                    "cognitive.store: 拒绝整层清空 layer=%s（库里 %s 条，写入 0 条）。"
                    "多半是调用方 profile 不完整（测试误用全局 manager？）。"
                    "确需清空请显式 force=True。",
                    layer, existing,
                )
                raise RuntimeError(
                    f"refuse to wipe layer={layer}: {existing} existing items "
                    f"but 0 incoming (use force=True if intended)"
                )
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

            # 方案1（止血 · 2026-10-04）：persist() 全量 DELETE+重插会抹掉同步身份列
            # （sync_uuid/sync_hlc/sync_tombstone）。重插前先按 id 暂存本层现有同步身份，
            # 重插时原样带回，否则每次 save() 都会把已同步条目的身份清零
            # —— 即 2026-10-04 夜间 80 行丢失同一根因。
            _existing_sync = {}
            try:
                for _r in conn.execute(
                    "SELECT id, sync_uuid, sync_hlc, sync_tombstone "
                    "FROM profile_items WHERE layer=?", (layer,)
                ):
                    _existing_sync[_r[0]] = (_r[1], _r[2], _r[3])
            except sqlite3.Error:
                _existing_sync = {}

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
                    # 带回同步身份：已存在条目用旧身份（不退出同步协议）；
                    # 新条目留 NULL，由 SyncEngine/SyncNode.migrate 的 uuid5(id) 确定性回填。
                    _su, _sh, _st = _existing_sync.get(it.id, (None, None, None))
                    rows.append((
                        it.id, layer, dim, topic, it.content, it.category,
                        it.status, it.confidence, json.dumps(extra, ensure_ascii=False),
                        it.first_extracted, it.last_updated,
                        it.first_extracted, None,  # 双时态：valid_from=首次提取，valid_until=开放
                        _su, _sh, _st,
                    ))
                    for i, s in enumerate(it.sources):
                        src_rows.append((it.id, i, s.type, s.title, s.excerpt, s.date, s.confidence))
                    for j, h in enumerate(it.history):
                        hist_rows.append((
                            it.id, j, h.action, h.old_value, h.new_value,
                            h.reason, h.source, h.timestamp,
                        ))
            # 遗忘墓碑（P1-1）：用户主动遗忘过的条目，任何全量覆盖写都不许再写回。
            # 注意：上面的「防整层清空」护栏统计的是**未过滤**的 incoming 条目数，
            # 因此这里过滤不会让用户「忘光一层后下一次 save 被护栏误判为异常」。
            try:
                dead = {r[0] for r in conn.execute("SELECT item_id FROM forgotten_items")}
            except sqlite3.Error:
                dead = set()  # 表尚未创建（从未遗忘过）→ 无墓碑
            if dead:
                rows = [r for r in rows if r[0] not in dead]
                src_rows = [r for r in src_rows if r[0] not in dead]
                hist_rows = [r for r in hist_rows if r[0] not in dead]

            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO profile_items"
                    "(id, layer, dimension, topic, content, category, status, confidence, extra_json, first_extracted, last_updated, valid_from, valid_until, sync_uuid, sync_hlc, sync_tombstone) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            # P1-2：FTS 与条目**同一事务**同步（防「记了查不到」）。
            # persist 是整层覆盖写，直接全量重建最简单也最不易漂移（表很小）。
            try:
                self._rebuild_fts(conn)
            except sqlite3.Error as e:
                logger.warning("profile_items_fts 同步失败（召回将退回全量注入）: %s", e)

    @staticmethod
    def _item_extra(dim: str, it) -> dict:
        extra = {
            "tags": list(getattr(it, "tags", [])),
            "contexts": list(getattr(it, "contexts", [])),
        }
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
                        contexts=extra.get("contexts", []),
                    ))
                elif dim == "argument_pattern":
                    args.append(ArgumentPattern(
                        id=rid, content=content, category=category, status=status,
                        confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                        contexts=extra.get("contexts", []),
                        pattern_type=extra.get("pattern_type", ""),
                        evidence_preference=extra.get("evidence_preference", ""),
                    ))
                elif dim == "value_ranking":
                    vals.append(ValueRanking(
                        id=rid, domain=topic, content=content, category=category,
                        status=status, confidence=confidence, first_extracted=first_extracted,
                        last_updated=last_updated, sources=sources, history=history,
                        tags=extra.get("tags", []),
                        contexts=extra.get("contexts", []),
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
                        contexts=extra.get("contexts", []),
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

    def delete_item(self, item_id: str) -> bool:
        """用户主动遗忘某条认知条目（Replika 式可控记忆）。

        认知层读取（load_cognitive_profile）不过滤 status，故采用物理删使遗忘
        真正生效；删前在 item_history 留痕 action='forgotten' 以便追溯。
        返回是否命中并删除。
        """
        try:
            with self._connect() as conn:
                # 遗忘墓碑（P1-1「缓存复活」修复）：
                # persist() 的语义是「整层 DELETE + 全量 INSERT」，任何持有旧 profile
                # 的缓存 manager（如全局单例 get_profile_manager()）再 save() 一次，
                # 刚被删除的条目就会原样写回 —— 「遗忘即刻生效」形同虚设。
                # 因此物理删除之外还要落墓碑：persist() 据此拒绝写回，
                # 与「谁在写、缓存多陈旧」无关，从持久化边界上根治。
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS forgotten_items ("
                    "  item_id TEXT PRIMARY KEY,"
                    "  layer   TEXT DEFAULT '',"
                    "  ts      TEXT"
                    ")"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO item_history "
                    "(item_id, seq, action, old_value, new_value, reason, source, ts) "
                    "VALUES(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM item_history "
                    "WHERE item_id=?), 'forgotten', '', '', 'user', ?, ?)",
                    (item_id, item_id, "user", _now_iso()),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO forgotten_items(item_id, layer, ts) "
                    "VALUES(?,?,?)",
                    (item_id, _LAYER, _now_iso()),
                )
                cur = conn.execute("DELETE FROM profile_items WHERE id=?", (item_id,))
                try:
                    conn.execute(
                        "DELETE FROM profile_items_fts WHERE item_id=?", (item_id,)
                    )
                except sqlite3.Error:
                    pass  # FTS 同步失败不阻断遗忘（召回退全量，不会复活条目）
                return bool(cur.rowcount and cur.rowcount > 0)
        except sqlite3.Error:
            return False

    def list_for_ui(self, limit: int = 200) -> list[dict[str, Any]]:
        """供 MemoryViewer 条目级可控：返回认知条目（含 id/layer/dimension/status）。

        双时态（P1-2）：附 ``valid_from`` / ``valid_until`` 展示「自何时持有 / 至今有效」。
        """
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, layer, dimension, topic, content, status, last_updated, "
                    "COALESCE(valid_from, first_extracted), valid_until "
                    "FROM profile_items "
                    "ORDER BY (status='active') DESC, last_updated DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "id": r[0], "layer": r[1], "dimension": r[2], "topic": r[3],
                "content": (r[4] or "")[:200], "status": r[5], "last_updated": r[6],
                # 表里 valid_until 恒为 NULL（开放区间）；已走到终态的条目按
                # last_updated 推导出闭合时间，UI 才能显示「有效到 X」。
                "valid_from": r[7],
                "valid_until": r[8] or _derived_valid_until(r[5], r[6]),
            }
            for r in rows
        ]

    def profile_as_of(self, at_iso: str, layer: str = _LAYER) -> dict[str, Any]:
        """双时态（P1-2）：返回 ``at_iso`` 时点**有效**的认知画像快照。

        实现要点（不改动 upsert 语义）：
        - 每条当前 item 的 ``first_extracted`` 决定它何时「出生」，``at < 出生`` 则当时不存在；
        - 其 ``item_history`` 是完整时序账本（created→updated→…），回放得到 at 时点的内容；
        - 仅采纳携带真实内容变更的动作（created/updated/deepened/restored），
          过滤 questioned/superseded 等纯状态变更。
        """
        at = _parse_iso(at_iso)
        if at is None:
            return {"ok": False, "error": "bad_time", "at": at_iso, "items": []}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, layer, dimension, topic, content, category, status, "
                    "first_extracted, COALESCE(valid_from, first_extracted), last_updated "
                    "FROM profile_items WHERE layer=? ORDER BY id",
                    (layer,),
                ).fetchall()
                items: list[dict[str, Any]] = []
                for r in rows:
                    (rid, ly, dim, topic, content, cat, status,
                     first_extracted, valid_from, last_updated) = r
                    fe = _parse_iso(first_extracted) if first_extracted else None
                    if fe is not None and at < fe:
                        continue  # 该时点此认知尚未形成
                    hist = conn.execute(
                        "SELECT action, new_value, ts FROM item_history "
                        "WHERE item_id=? ORDER BY seq",
                        (rid,),
                    ).fetchall()
                    content_at = content  # 无变更记录时退化为当前内容
                    best_ts: datetime.datetime | None = None
                    for action, new_value, ts in hist:
                        # 2026-09-23 巡检三处 bug 的读取侧收口：
                        #  ① calibrated 曾不在内容白名单 → 用户纠正被回退；
                        #  ② restored 的 new_value 是状态字面量 "active" → 污染正文；
                        #  ③ profile.py 有 3 处 updated 漏写 new_value → 回放成空串。
                        val = _replay_value(action, new_value)
                        if val is None:
                            continue
                        pt = _parse_iso(ts) if ts else None
                        if pt is None or pt > at:
                            continue
                        if best_ts is None or pt >= best_ts:
                            best_ts = pt
                            content_at = val
                    items.append({
                        "id": rid, "layer": ly, "dimension": dim, "topic": topic,
                        "content": content_at, "category": cat, "status": status,
                        "valid_from": valid_from,
                        # 已走到终态（被取代 / 遗忘）的条目，有效性区间视为已闭合
                        "valid_until": _derived_valid_until(status, last_updated),
                    })
                return {"ok": True, "at": at_iso, "items": items}
        except sqlite3.Error:
            return {"ok": False, "error": "db", "at": at_iso, "items": []}

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
    # P1 汇流：锚（共享锚 → 邻座召回）
    # ------------------------------------------------------------------
    def upsert_anchors(
        self,
        item_kind: str,
        item_id: str,
        anchors: Optional[list[tuple[str, str]]] = None,
    ) -> int:
        """把某条目的「锚」写入 memory_anchors（INSERT OR IGNORE，幂等）。

        P1 只写 item_kind='profile_item'。fail-open：任何异常仅告警、不抛——
        抽锚失败绝不影响主抽取链路。返回拟写入的锚条数（重复由主键/唯一约束幂等跳过）。

        Args:
            anchors: [(anchor_kind, anchor_value), ...]；空/None 直接返回 0。
        """
        pairs = [(k, v) for (k, v) in (anchors or []) if k and v]
        if not pairs:
            return 0
        ts = _now_iso()
        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO memory_anchors"
                    "(row_key, item_kind, item_id, anchor_kind, anchor_value, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    [
                        (
                            anchor_row_key(item_kind, item_id, k, v),
                            item_kind, item_id, k, v, ts,
                        )
                        for (k, v) in pairs
                    ],
                )
            return len(pairs)
        except Exception as e:  # noqa: BLE001 —— fail-open：抽锚失败不影响主链路
            logger.warning("upsert_anchors 失败（忽略，不影响主抽取）: %s", e)
            return 0

    # ------------------------------------------------------------------
    # P2-R1：已知人名注册表（person 锚源；seed + 自生长 mode B）
    # ------------------------------------------------------------------
    def list_active_persons(self) -> list[str]:
        """返回 status='active' 的人名列表（供 person 锚词边界匹配）。

        fail-open：库异常/表缺失 → 返回 []（调用方再无 env 则无 person 锚，绝不抛）。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT name FROM known_persons WHERE status='active'"
                ).fetchall()
            return [r[0] for r in rows if r[0]]
        except Exception as e:  # noqa: BLE001 —— fail-open：注册表缺失/查询异常不抛
            logger.warning("list_active_persons 失败（忽略，退回无 person 锚）: %s", e)
            return []

    def seed_known_persons(self, names: list[str], source: str = "seed") -> int:
        """批量灌入人名（seed 直接 active；INSERT OR IGNORE，幂等）。

        seed 来自 LLM 扫已出版书，可信度高 → 直接 active、不经 candidate。
        返回**实际新增**条数（重复由主键幂等跳过）。fail-open：异常仅告警、返回 0。
        """
        clean = sorted({(n or "").strip() for n in (names or []) if (n or "").strip()})
        if not clean:
            return 0
        ts = _now_iso()
        try:
            with self._connect() as conn:
                cur = conn.executemany(
                    "INSERT OR IGNORE INTO known_persons"
                    "(name, aliases, source, status, seen_count, created_at) "
                    "VALUES(?, '', ?, 'active', 0, ?)",
                    [(n, source, ts) for n in clean],
                )
                added = cur.rowcount if cur.rowcount is not None else 0
            return added
        except Exception as e:  # noqa: BLE001 —— fail-open：seed 失败不影响主链路
            logger.warning("seed_known_persons 失败（忽略）: %s", e)
            return 0

    def add_known_person(self, name: str, source: str = "auto") -> None:
        """运行时发现人名 → 自生长（mode B）+ 频次门槛转正（原子操作）。

        - INSERT OR IGNORE 进 candidate（seen_count=0）；
        - seen_count += 1；
        - seen_count >= _PERSON_PROMOTE_THRESHOLD 且尚未 active → 升 active。
        阈值内（前 2 次）保持 candidate、**不产 person 锚**；第 3 次起生效。
        fail-open：任何异常仅告警、不抛（人名自生长绝不影响主认知抽取）。
        """
        n = (name or "").strip()
        if not n:
            return
        ts = _now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO known_persons"
                    "(name, aliases, source, status, seen_count, created_at) "
                    "VALUES(?, '', ?, 'candidate', 0, ?)",
                    (n, source, ts),
                )
                conn.execute(
                    "UPDATE known_persons SET seen_count = seen_count + 1 WHERE name = ?",
                    (n,),
                )
                conn.execute(
                    "UPDATE known_persons SET status = 'active' "
                    "WHERE name = ? AND seen_count >= ? AND status != 'active'",
                    (n, _PERSON_PROMOTE_THRESHOLD),
                )
        except Exception as e:  # noqa: BLE001 —— fail-open：自生长失败不影响主链路
            logger.warning("add_known_person 失败（忽略）: %s", e)

    # ------------------------------------------------------------------
    # P2-R3：lessons——小桔从萧哥纠正/反馈学到的条目（每轮注入）
    # ------------------------------------------------------------------
    def add_lesson(self, content: str, scope: str = "recent", source: str = "manual") -> bool:
        """写入一条 lesson（手动/测试/未来 feedback 调用）。同内容幂等（不重插）。

        scope：'recent'（默认，按时间取最近 N 条注入）| 'permanent'（常驻规则）。
        lesson_id = 内容 md5（去重幂等）：同内容重复调用只留 1 行。
        fail-open：空内容返回 False；写库异常仅告警、返回 False，绝不抛。
        """
        c = (content or "").strip()
        if not c:
            return False
        lid = f"lesson::{hashlib.md5(c.encode()).hexdigest()[:12]}"
        ts = _now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO teachings"
                    "(lesson_id, content, scope, source, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (lid, c, scope, source, ts, ts),
                )
            return True
        except Exception as e:  # noqa: BLE001 —— fail-open：写 lesson 失败不影响主链路
            logger.warning("add_lesson 失败（忽略）: %s", e)
            return False

    def get_lessons_for_injection(
        self, recent_limit: int = 3, char_cap: int = 1200
    ) -> list[str]:
        """返回注入用 lesson 正文列表：全部 permanent + 最近 recent_limit 条 recent。

        限额防挤爆上下文：permanent 通常少、recent 限 recent_limit、总字数 cap char_cap。
        permanent 排在前面（常驻规则优先保）。fail-open：表缺失/异常 → []。
        """
        try:
            with self._connect() as conn:
                perm = [
                    r[0] for r in conn.execute(
                        "SELECT content FROM teachings WHERE scope='permanent' "
                        "ORDER BY created_at DESC"
                    ).fetchall()
                ]
                rec = [
                    r[0] for r in conn.execute(
                        "SELECT content FROM teachings WHERE scope!='permanent' "
                        "ORDER BY created_at DESC LIMIT ?",
                        (recent_limit,),
                    ).fetchall()
                ]
            out: list[str] = []
            used = 0
            for t in perm + rec:
                if not t:
                    continue
                if used + len(t) > char_cap:
                    break
                out.append(t)
                used += len(t)
            return out
        except Exception as e:  # noqa: BLE001 —— fail-open：lessons 缺失返回 []
            logger.warning("get_lessons_for_injection 失败（忽略）: %s", e)
            return []

    def recall_items(
        self,
        query: str,
        layers: tuple[str, ...] = (),
        top_k: int = 8,
        exclude_ids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """按 query 召回 active 认知条目（FTS5，bm25 排序）。

        P1-2 范围化注入的长尾召回入口。fail-open：FTS 不可用 / 空 query /
        任何异常都返回 []（调用方据此退回全量注入，行为与历史版本一致）。

        Args:
            layers: 限定层（如 ("cognitive",)）；空 = 跨全部层。
            exclude_ids: 排除的条目 id（核心层已注入的）。
            top_k: 最多返回条数。
        """
        match = _fts_match(query)
        if not match:
            return []
        sql = (
            "SELECT p.id, p.layer, p.dimension, p.topic, p.content, p.confidence "
            "FROM profile_items_fts f JOIN profile_items p ON p.id = f.item_id "
            "WHERE profile_items_fts MATCH ? AND p.status='active'"
        )
        params: list[Any] = [match]
        if layers:
            sql += f" AND p.layer IN ({','.join('?' * len(layers))})"
            params.extend(layers)
        sql += " ORDER BY bm25(profile_items_fts) LIMIT ?"
        params.append(max(1, min(int(top_k), 50)))
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        excl = exclude_ids or set()
        hits = [
            {"id": r[0], "layer": r[1], "dimension": r[2], "topic": r[3],
             "content": r[4], "confidence": r[5]}
            for r in rows
            if r[0] not in excl
        ]
        # P1 汇流（R3）+ P2-R2 截断修复（Option B）：顺锚走一跳——FTS 命中的 seed
        # 带出同锚「邻座」。纯增益 + fail-open：任何异常都只 warning、退回纯 FTS，绝不空结果。
        neighbors: list[dict[str, Any]] = []
        if os.getenv("DESKMATE_ANCHOR_RECALL", "1") != "0" and hits:
            try:
                neighbors = self._recall_anchor_neighbors(hits, excl)
            except Exception as e:  # noqa: BLE001 —— fail-open：顺锚失败退回纯 FTS
                logger.warning("顺锚召回失败（退回纯 FTS）: %s", e)
                neighbors = []
        # Option B（2026-10-07）：邻居至多 _ANCHOR_NEIGHBOR_CAP 条；有邻居时从 FTS
        # 取数里预留等额配额，让邻居必进结果、FTS 不超额；无邻居时 FTS 不退化
        #（= 历史纯 FTS 行为，修 ROUND-06-QC 已知局限「FTS 满 top_k 时邻居被截掉」）。
        if neighbors:
            keep = max(0, top_k - len(neighbors))
            hits = hits[:keep] + neighbors
        return hits[:top_k]

    def _recall_anchor_neighbors(
        self, seeds: list[dict[str, Any]], excl: set[str]
    ) -> list[dict[str, Any]]:
        """顺 seed 的锚反查同锚「邻座」条目（R3；调用方负责整段 fail-open）。

        - 只认 item_kind='profile_item'（P1 锚范围收敛，对齐 recall_items 查询的表）；
        - 邻座不与 seed / excl 重复；
        - 最多 _ANCHOR_NEIGHBOR_CAP 条（防爆炸）；
        - 附纯增键 via_anchor=True + anchor_evidence（如「同主题：X」），
          既有消费方按 r["id"] 取值，天然兼容。
        """
        seed_ids = [s["id"] for s in seeds if s.get("id")]
        if not seed_ids:
            return []
        with self._connect() as conn:
            # 1) seed 的锚集合（去重）
            qmarks = ",".join("?" * len(seed_ids))
            anchor_rows = conn.execute(
                "SELECT DISTINCT anchor_kind, anchor_value FROM memory_anchors "
                f"WHERE item_kind='profile_item' AND item_id IN ({qmarks})",
                seed_ids,
            ).fetchall()
            if not anchor_rows:
                return []
            # 2) 反查同锚邻座（走 idx_anchor_lookup）
            cond = ",".join("(?,?)" for _ in anchor_rows)
            cond_params = [x for pair in anchor_rows for x in pair]
            sib_rows = conn.execute(
                "SELECT item_id, anchor_kind, anchor_value FROM memory_anchors "
                "WHERE item_kind='profile_item' "
                f"AND (anchor_kind, anchor_value) IN ({cond})",
                cond_params,
            ).fetchall()
            skip = set(seed_ids) | set(excl)
            neighbor_ids: list[str] = []
            anchor_hit: dict[str, str] = {}
            for iid, kind, value in sib_rows:
                if iid in skip or iid in anchor_hit:
                    continue
                anchor_hit[iid] = f"同{_anchor_kind_label(kind)}：{value}"
                neighbor_ids.append(iid)
                if len(neighbor_ids) >= _ANCHOR_NEIGHBOR_CAP:
                    break
            if not neighbor_ids:
                return []
            # 3) 拉邻座正文（只取 active）
            qmarks2 = ",".join("?" * len(neighbor_ids))
            rows = conn.execute(
                "SELECT id, layer, dimension, topic, content, confidence "
                "FROM profile_items "
                f"WHERE status='active' AND id IN ({qmarks2})",
                neighbor_ids,
            ).fetchall()
        by_id = {r[0]: r for r in rows}
        return [
            {
                "id": by_id[i][0], "layer": by_id[i][1], "dimension": by_id[i][2],
                "topic": by_id[i][3], "content": by_id[i][4], "confidence": by_id[i][5],
                "via_anchor": True,
                "anchor_evidence": anchor_hit.get(i, ""),
            }
            for i in neighbor_ids
            if i in by_id
        ]

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

    def expire_active_sessions(self, layer: str = _LAYER, keep: str = "") -> int:
        """把该层**除 keep 之外**的 active 会话标记为 expired，返回清理条数。

        为什么需要（2026-09-22 定位）：访谈会话此前只进不出——
        ``start_interview`` 建一个新的，旧的 active 没人管；而每次发起都刷新
        ``last_interview_at``，用户没答就永远不会再被邀请，于是会话无限堆积
        （真实库曾有 12 个僵尸 active）。发起新一轮前先结掉旧的，语义是
        「上一轮问过但没回音，就不留着占位了」——新会话会重新带着当前
        open gap 去问，不丢信息。

        保留 ``expired`` 而非删除：访谈历史可追溯（谁问过什么、有没有回音），
        且与 done 区分开——「没答完」和「答完了」在回看时不是一回事。
        """
        from .models import _now

        with self._connect() as conn:
            if keep:
                cur = conn.execute(
                    "UPDATE interview_sessions SET status='expired', ended_at=? "
                    "WHERE layer=? AND status='active' AND id<>?",
                    (_now(), layer, keep),
                )
            else:
                cur = conn.execute(
                    "UPDATE interview_sessions SET status='expired', ended_at=? "
                    "WHERE layer=? AND status='active'",
                    (_now(), layer),
                )
            return cur.rowcount or 0

    def latest_active_session(self, layer: str = _LAYER) -> Optional[dict]:
        """该层最近一个仍 active 的会话（无则 None）。

        用途（2026-09-22）：访谈「补送」。邀请是否送达此前没有任何凭证——
        `create_session` 一落库就吃满 3 天冷却，而前端邀请气泡只存在 60 秒；
        用户当时不在窗口前，气泡无声消失，冷却却已消耗，下次 tick 又把
        这个 session 标 expired。结果「发起过很多次，用户一次没见过」。
        补送需要知道「上一轮邀请还挂着没回音」，这是它的查询入口。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, layer, status, started_at, ended_at, gap_ids_json, summary "
                "FROM interview_sessions WHERE layer=? AND status='active' "
                "ORDER BY started_at DESC LIMIT 1",
                (layer,),
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "layer": row[1], "status": row[2], "started_at": row[3],
                "ended_at": row[4], "gap_ids": json.loads(row[5] or "[]"), "summary": row[6]}

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
