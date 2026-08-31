"""统一键值 Blob 存储——收口散落的 profile 类 JSON。

把 ``growth.json`` / ``style_profile.json`` / ``decision_profile.json`` 这类
「profile 状态」JSON 收进统一库 ``~/.deskmate/deskmate.db`` 的 ``json_blobs`` 表，
作为唯一真相源。

设计要点（呼应「数据库必须可拓展」的长期约束）：
- **单一物理库**：与认知层（CognitiveStore）、日记（DiaryStore）共用 ``deskmate.db``，
  备份是同一原子单元；加新 profile 只是多一个 key，零 DDL 变更。
- **向后兼容**：DB 为空且 legacy JSON 存在时，首次 ``get`` 自动幂等迁移（并把原文件
  重命名为 ``.bak-<ts>`` 备份，绝不删除）；显式传入 path（CLI / 测试）仍走文件。
- **fail-open**：任何异常返回 None / {}，绝不阻断主流程——成长/画像丢了也不能拖垮聊天。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

# 与 CognitiveStore._default_db_path() 保持一致的同一物理文件
_DB_DEFAULT = Path.home() / ".deskmate" / "deskmate.db"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class BlobStore:
    """keyed JSON blobs over deskmate.db（json_blobs 表）。"""

    KEY_GROWTH = "growth"
    KEY_STYLE = "style_profile"
    KEY_DECISION = "decision_profile"
    # 情感层雏形（P2-5 连续情感关怀）：情感时间线 + 关怀去重日志
    # 与 growth 同属「设备本地」语义：收进统一库仅物理统一（备份原子单元），
    # 跨端同步仍只走 style/decision 派生包（见 memory/sync.py），不改变同步行为。
    KEY_EMOTION = "emotion_timeline"
    KEY_CARE_LOG = "care_log"

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else _DB_DEFAULT
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connect().close()
        self._ensure()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS json_blobs (
                    key       TEXT PRIMARY KEY,
                    data      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(
        self, key: str, legacy_path: Optional[str | Path] = None
    ) -> Optional[dict]:
        """读取 key；DB 为空且 legacy 文件存在时自动迁移并返回。

        返回 dict；不存在返回 None。fail-open：异常返回 None。
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT data FROM json_blobs WHERE key=?", (key,)
                ).fetchone()
            if row is not None:
                return json.loads(row[0])
            # DB 无 → 尝试从 legacy 迁移
            if legacy_path:
                self._migrate_legacy(key, Path(legacy_path))
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT data FROM json_blobs WHERE key=?", (key,)
                    ).fetchone()
                if row is not None:
                    return json.loads(row[0])
        except Exception:
            return None
        return None

    def set(self, key: str, data: dict) -> None:
        """upsert（原子写入唯一真相源）。fail-open：异常静默。"""
        try:
            payload = json.dumps(data, ensure_ascii=False)
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO json_blobs(key, data, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                    (key, payload, _now_iso()),
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 迁移（幂等）
    # ------------------------------------------------------------------
    def _migrate_legacy(self, key: str, legacy: Path) -> None:
        """若 DB 无此 key 且 legacy JSON 存在，导入一次并备份原文件。"""
        if not legacy.is_file():
            return
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM json_blobs WHERE key=?", (key,)
            ).fetchone()
        if exists:
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        # 备份原文件（重命名，绝不删除）
        self._backup_legacy(legacy)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO json_blobs(key, data, updated_at) VALUES(?,?,?)",
                (key, json.dumps(data, ensure_ascii=False), _now_iso()),
            )

    @staticmethod
    def _backup_legacy(path: Path) -> None:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(f"{path.name}.bak-{ts}")
        n = 0
        while bak.exists():
            n += 1
            bak = path.with_name(f"{path.name}.bak-{ts}-{n}")
        try:
            path.rename(bak)
        except Exception:
            pass
