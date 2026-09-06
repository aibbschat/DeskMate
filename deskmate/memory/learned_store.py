"""LearnedStore：长期记忆（「越用越懂你」）的 SQLite 持久层。

取代原 ``memory/learned_history.jsonl`` 单文件追加读，收口进统一库
``~/.deskmate/deskmate.db``（与认知层 CognitiveStore、日记 DiaryStore、
profile 类 JSON 同库），**备份即同一原子单元**。

为什么收（2026-09-02 实测数据）：
- 长期记忆**只增不减**：211 条 / 25 个活跃日 ≈ 8.4 条/天，外推 ~2500 条/年。
- 原实现每次读取都要 ``read_text()`` 全量进内存再逐行 ``json.loads``，
  且 **一次请求解析两遍**（取最近 N 条一遍、全量统计又一遍）。
  体量小时无感，但这是只增不减的数据，解析成本随条数线性上涨。
- 统计（total / by_category）本该是 ``COUNT(*) + GROUP BY`` 的活，
  全量解析纯属浪费。

表：
- ``learned_history(id, ts, fact, category, fp, source)``
  - ``fp`` = fact 归一化后的 md5，``UNIQUE`` → 重复记忆天然幂等。
  - ``source`` 标记来源（``legacy`` = 从 jsonl 迁移，``live`` = 运行时新学）。

契约（**改这里必须同步改前端**）：
- ``recent()`` 返回的 items 是**时间正序（最老在前）**，前端
  UnderstandingCard 会自行 reverse 后展示。
- 排序 tie-break 用 ``id``：ts 是秒精度，同一秒学到的多条靠 id 保持
  学到的先后顺序。

迁移：``migrate_from_legacy()`` 幂等导入 jsonl（INSERT OR IGNORE，靠 fp
去重）。导入后**校验条数**——DB 去重条数必须 ≥ 文件去重条数，否则视为
失败、不归档原文件。校验通过才由 ``archive_legacy()`` 把 jsonl 改名
``.bak-<ts>``（**绝不删除**）。

与 growth.json 的教训：本 store **不做 legacy 回灌**。归档后代码只读 DB。
若 DB 被清空，数据不会"自己又长回来"——那是比丢失更诡异的行为。安全网
是保留的 ``.bak`` 文件，需要时用 ``migrate_from_legacy(path)`` 手动导回。

fail-open：任何 DB 异常都不向上抛，保证主聊天链路不被记忆存储拖垮。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path.home() / ".deskmate"
_DB_NAME = "deskmate.db"
_LEGACY_REL = Path("memory") / "learned_history.jsonl"


def _now_iso() -> str:
    # 与原实现一致：秒精度，不带时区（既有数据全是这个格式，保持可比较）
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fingerprint(fact: str) -> str:
    """事实指纹：忽略空白差异，用于去重。"""
    return hashlib.md5(re.sub(r"\s+", "", fact or "").encode("utf-8")).hexdigest()


# 与 app._classify_learned_fact 保持一致的启发式归类。
# 收进 store 是为了让「分类」在数据侧就定型，而不是每次读取时重算。
_PREFERENCE_KEYS = (
    "偏好", "喜欢", "倾向", "爱用", "常用", "希望", "想要", "期望",
    "不愿", "讨厌", "避免", "prefer", "like", "hate",
)
_HABIT_KEYS = (
    "习惯", "通常", "一般", "每天", "每次", "经常", "往往", "固定",
    "habit", "usually", "always",
)
_TOOL_KEYS = (
    "工具", "用", "使用", "命令", "快捷键", "软件", "编辑器", "ide",
    "tool", "use", "command",
)


def classify_fact(fact: str) -> str:
    """启发式归类一条学到的记忆（用于「了解度」统计）。"""
    t = (fact or "").lower()
    if any(k in t for k in _PREFERENCE_KEYS):
        return "preference"
    if any(k in t for k in _HABIT_KEYS):
        return "habit"
    if any(k in t for k in _TOOL_KEYS):
        return "tool"
    return "other"


def _extract_entity(fact: str) -> str:
    """抽取事实的主体（用于矛盾检测的「同实体」判定）。

    只在**高置信**场景才返回实体，避免误判把不相关事实标成矛盾：
    - 书名/文献：``《...》`` / ``「...」`` / ``"..."`` 括起来的 → ``书:书名``
    - 其它（人名/泛指/无括实体）→ 返回空串，靠 fp 去重即可，不强做矛盾配对。

    这是「《宋会要辑稿》在史料库」与「《宋会要辑稿》不在史料库」能被识别为
    同一实体相反极性的关键；泛主体不抽，宁可漏判也不误判。
    """
    m = re.search(r"[《「『\"“](.+?)[》」』\"”]", fact or "")
    if m:
        inner = m.group(1).strip()
        if inner:
            return "书:" + inner
    return ""


# 否定标记：命中即视为「否认/不存在」类断言，与「断言/存在」类相反极性。
# 只覆盖明确否定词，避免把「不在意细节」这类中性句误判（其 entity 为空也不做配对）。
_DENY_RE = re.compile(
    r"(不在|没(有|在|用)|未(必|曾|收录|在)|无|否(则|定|认|有)|并非|不是|"
    r"没有|从不|绝不|别(说|提|用)|并未|没有收录)"
)


def _extract_polarity(fact: str) -> str:
    """抽取事实的极性：``deny``（否认/不存在）或 ``assert``（断言/存在）。"""
    return "deny" if _DENY_RE.search(fact or "") else "assert"


class LearnedStore:
    """长期记忆的 SQLite 持久层。短连接 + 自动提交（WAL 支持并发读）。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base = Path(base_dir) if base_dir else _DEFAULT_DIR
        self.db_path = self.base / _DB_NAME
        self.legacy_path = self.base / _LEGACY_REL
        self.base.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _add_column(self, table: str, col: str, col_type: str) -> None:
        """兼容已存在的库：缺失列才补（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
        try:
            with self._connect() as conn:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.Error:
            pass

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_history (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    fact     TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    fp       TEXT NOT NULL,
                    source   TEXT NOT NULL DEFAULT 'live'
                )
                """
            )
            # 兼容已存在的库：补齐矛盾检测 / 时效失效所需列
            # （SQLite 不支持 ADD COLUMN IF NOT EXISTS，逐个判断后补）
            self._add_column("learned_history", "entity", "TEXT")
            self._add_column("learned_history", "polarity", "TEXT")
            self._add_column("learned_history", "status", "TEXT NOT NULL DEFAULT 'active'")
            self._add_column("learned_history", "superseded_by", "INTEGER")
            self._add_column("learned_history", "valid_until", "TEXT")
            # fp 唯一 → 重复学到的同一事实天然幂等（INSERT OR IGNORE 即可）
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_fp "
                "ON learned_history(fp)"
            )
            # 取最近 N 条：ORDER BY ts DESC, id DESC
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_ts "
                "ON learned_history(ts DESC, id DESC)"
            )
            # 分类统计走覆盖索引，不再全表解析
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_cat "
                "ON learned_history(category)"
            )
            # 矛盾检测：按 (实体, 状态, 极性) 定位旧条
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_entity "
                "ON learned_history(entity, status, polarity)"
            )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def append(
        self,
        facts: list[str],
        ts: str | None = None,
        valid_until: str | None = None,
    ) -> int:
        """追加若干条学到的事实，返回**实际新增**条数（重复的被 fp 挡掉）。

        Args:
            valid_until: 可选过期时间（ISO 秒精度）。外部/趋势类信息带过期，
                到点的条目在 ``active_recent`` / ``relevant`` 中自动失效剔除。
                为 ``None`` 时永久有效（运行时新学的用户事实默认不过期）。

        矛盾检测（2026-09-03 补）：写入一条事实时，若库里已有一条**同实体、
        相反极性**的活跃事实（如「《宋会要辑稿》在史料库」vs「《宋会要辑稿》
        不在史料库」），把旧条标记 ``superseded``（记 ``valid_until`` + 指向新条
        ``superseded_by``），而非让矛盾两条并存被一起相信。实体只在书名/引号等
        **高置信**场景抽取，避免误判泛主体。

        注意：冲突走 ``INSERT OR IGNORE``，已存在的 fp 不会被更新——因此重复
        写入同一条事实不会刷新其 ``valid_until``，过期语义稳定。
        """
        clean: list[tuple[str, str, str, str, str, str, str | None]] = []
        stamp = ts or _now_iso()
        for raw in facts:
            fact = str(raw).strip() if raw else ""
            if not fact:
                continue
            entity = _extract_entity(fact)
            clean.append(
                (
                    stamp,
                    fact,
                    classify_fact(fact),
                    _fingerprint(fact),
                    entity,
                    _extract_polarity(fact),
                    valid_until,
                )
            )
        if not clean:
            return 0
        try:
            with self._connect() as conn:
                inserted = 0
                for stamp_, fact_, cat_, fp_, entity_, polarity_, valid_until_ in clean:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO learned_history"
                        "(ts, fact, category, fp, source, entity, polarity, status, valid_until) "
                        "VALUES (?, ?, ?, ?, 'live', ?, ?, 'active', ?)",
                        (stamp_, fact_, cat_, fp_, entity_, polarity_, valid_until_),
                    )
                    if not (cur.rowcount and cur.rowcount > 0):
                        continue  # 重复（同 fp）→ 跳过，也不做矛盾处理
                    inserted += 1
                    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    if entity_:
                        # 把同实体、相反极性的旧活跃事实标记为已被本条取代
                        conn.execute(
                            "UPDATE learned_history SET status='superseded', "
                            "valid_until=?, superseded_by=? "
                            "WHERE status='active' AND entity=? AND polarity!=? AND id!=?",
                            (stamp_, rid, entity_, polarity_, rid),
                        )
                return inserted
        except sqlite3.Error:
            return 0

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """最近 limit 条，**时间正序（最老在前）**——前端会 reverse 后展示。"""
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        # SQL 取的是倒序（最新在前），reverse 成正序，与旧文件版契约一致
        return [
            {"ts": r[0], "fact": r[1], "category": r[2]} for r in reversed(rows)
        ]

    def stats(self) -> dict[str, Any]:
        """总量 + 分类计数。走 SQL 聚合，不再全量解析文件。"""
        try:
            with self._connect() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM learned_history"
                ).fetchone()[0]
                by_cat = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT category, COUNT(*) FROM learned_history GROUP BY category"
                    ).fetchall()
                }
        except sqlite3.Error:
            return {"total": 0, "by_category": {}}
        return {"total": int(total), "by_category": by_cat}

    def snapshot(self, limit: int = 30, order: str = "time") -> dict[str, Any]:
        """一次返回 {items, stats}——路由直接吃这个。

        - ``order="time"``（默认）：items 为**活跃**事实、时间正序（最老在前），
          与前端 UnderstandingCard 的 reverse 契约一致；已被新事实取代的旧条
          不出现，避免矛盾两条并存展示。
        - ``order="relevance"``：items 按「新鲜度 × 频次」降序，带 ``score``
          字段，供叙事/上下文注入使用（recency 加权，见 :meth:`relevant`）。
        - stats 只计**当前有效(active)**事实，被取代的旧条不计入「了解度」。
        """
        if order == "relevance":
            items = self.relevant(limit)
        else:
            items = self.active_recent(limit)
        return {"items": items, "stats": self.active_stats()}

    def active_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """活跃事实，时间正序（最老在前），排除已失效/被取代的。供 UI 展示。"""
        limit = max(1, min(int(limit), 5000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history "
                    "WHERE status='active' AND (valid_until IS NULL OR valid_until >= ?) "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (_now_iso(), limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {"ts": r[0], "fact": r[1], "category": r[2]} for r in reversed(rows)
        ]

    def active_stats(self) -> dict[str, Any]:
        """当前有效(active)事实的总量与分类计数（「了解度」只数有效信念）。"""
        try:
            with self._connect() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM learned_history WHERE status='active'"
                ).fetchone()[0]
                by_cat = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT category, COUNT(*) FROM learned_history "
                        "WHERE status='active' GROUP BY category"
                    ).fetchall()
                }
        except sqlite3.Error:
            return {"total": 0, "by_category": {}}
        return {"total": int(total), "by_category": by_cat}

    def relevant(self, limit: int = 30) -> list[dict[str, Any]]:
        """按「新鲜度 × 频次」排序的活跃事实——供注入上下文/叙事用（recency 加权）。

        新鲜度 = 1/(1+距今天数)；频次 = 同实体(无实体则同 fp)的事实条数
        （同一件事被反复学到 → 越重要）。返回降序，每条带 ``score`` 字段。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, fact, category, entity, fp FROM learned_history "
                    "WHERE status='active' AND (valid_until IS NULL OR valid_until >= ?) "
                    "ORDER BY ts DESC, id DESC",
                    (_now_iso(),),
                ).fetchall()
        except sqlite3.Error:
            return []
        now = datetime.datetime.now()
        freq: dict[str, int] = {}
        parsed: list[dict[str, Any]] = []
        for r in rows:
            rid, ts, fact, cat, entity, fp = r
            key = (entity or fp) or f"#{rid}"
            freq[key] = freq.get(key, 0) + 1
            try:
                dt = datetime.datetime.fromisoformat(ts)
            except ValueError:
                dt = now
            age_days = max(0, (now - dt).days)
            recency = 1.0 / (1.0 + age_days)
            score = round(recency * (1.0 + 0.5 * (freq[key] - 1)), 4)
            parsed.append(
                {"ts": ts, "fact": fact, "category": cat, "score": score}
            )
        parsed.sort(key=lambda x: -x["score"])
        return parsed[: max(1, min(int(limit), 5000))]

    def conflicts_report(self) -> list[dict[str, Any]]:
        """返回被取代（矛盾）的事实及其取代者——供 UI 标注「已更新」。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT o.fact, o.ts, n.fact, n.ts FROM learned_history o "
                    "JOIN learned_history n ON o.superseded_by = n.id "
                    "WHERE o.status='superseded' ORDER BY n.ts DESC, n.id DESC"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "superseded_fact": r[0],
                "superseded_at": r[1],
                "replaced_by_fact": r[2],
                "replaced_at": r[3],
            }
            for r in rows
        ]

    def all_items(self) -> list[dict[str, Any]]:
        """全量导出（时间正序）。供迁移校验 / 数据导出用。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, fact, category FROM learned_history ORDER BY ts ASC, id ASC"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [{"ts": r[0], "fact": r[1], "category": r[2]} for r in rows]

    # ------------------------------------------------------------------
    # 迁移：learned_history.jsonl → DB
    # ------------------------------------------------------------------
    def _read_legacy(self) -> list[dict[str, Any]]:
        """解析 legacy jsonl（容错跳过坏行）。文件不存在返回 []。"""
        if not self.legacy_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.legacy_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                fact = str(rec.get("fact", "")).strip()
                if not fact:
                    continue
                out.append(
                    {
                        "ts": rec.get("ts") or _now_iso(),
                        "fact": fact,
                        # 文件里已存了 category 就用它的（尊重历史分类），
                        # 缺失才重算——避免迁移改变既有统计口径
                        "category": rec.get("category") or classify_fact(fact),
                    }
                )
        except OSError:
            return []
        return out

    def migrate_from_legacy(self, archive: bool = True) -> dict[str, Any]:
        """幂等导入 legacy jsonl。

        Args:
            archive: 校验通过是否把原文件改名 ``.bak-<ts>``。

        Returns:
            ``{ok, imported, in_db, in_file, archived, reason}``
        """
        rows = self._read_legacy()
        result: dict[str, Any] = {
            "ok": False,
            "imported": 0,
            "in_db": 0,
            "in_file": len(rows),
            "archived": False,
            "reason": "",
        }
        if not rows:
            result["ok"] = True
            result["reason"] = "无 legacy 数据（文件不存在或为空）"
            return result

        payload = [
            (r["ts"], r["fact"], r["category"], _fingerprint(r["fact"]))
            for r in rows
        ]
        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO learned_history(ts, fact, category, fp, source) "
                    "VALUES (?, ?, ?, ?, 'legacy')",
                    payload,
                )
                in_db = conn.execute(
                    "SELECT COUNT(*) FROM learned_history"
                ).fetchone()[0]
        except sqlite3.Error as exc:  # 导入失败绝不改原文件
            result["reason"] = f"导入失败：{exc}"
            return result

        result["in_db"] = int(in_db)
        # 校验：DB 里的总量必须覆盖文件里的去重条数。
        # 用「去重后的 fp 数」比对，避免文件里本身有重复导致误判。
        uniq_in_file = len({p[3] for p in payload})
        if in_db < uniq_in_file:
            result["reason"] = (
                f"校验失败：DB 只有 {in_db} 条 < 文件去重 {uniq_in_file} 条，"
                f"疑似导入不全，保留原文件"
            )
            return result

        result["ok"] = True
        result["imported"] = int(in_db)
        result["reason"] = f"导入完成，DB 现有 {in_db} 条"
        if archive:
            result["archived"] = self.archive_legacy()
        return result

    def archive_legacy(self) -> bool:
        """把 legacy jsonl 改名 ``.bak-<ts>``（绝不删除）。改名后代码只读 DB。"""
        if not self.legacy_path.is_file():
            return False
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.legacy_path.with_name(f"{self.legacy_path.name}.bak-{stamp}")
        try:
            self.legacy_path.rename(target)
            return True
        except OSError:
            return False
