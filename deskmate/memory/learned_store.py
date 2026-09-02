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

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def append(self, facts: list[str], ts: str | None = None) -> int:
        """追加若干条学到的事实，返回**实际新增**条数（重复的被 fp 挡掉）。"""
        clean: list[tuple[str, str, str, str]] = []
        stamp = ts or _now_iso()
        for raw in facts:
            fact = str(raw).strip() if raw else ""
            if not fact:
                continue
            clean.append((stamp, fact, classify_fact(fact), _fingerprint(fact)))
        if not clean:
            return 0
        try:
            with self._connect() as conn:
                cur = conn.executemany(
                    "INSERT OR IGNORE INTO learned_history(ts, fact, category, fp, source) "
                    "VALUES (?, ?, ?, ?, 'live')",
                    clean,
                )
                return int(cur.rowcount or 0)
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

    def snapshot(self, limit: int = 30) -> dict[str, Any]:
        """一次返回 {items, stats}——路由直接吃这个。"""
        return {"items": self.recent(limit), "stats": self.stats()}

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
