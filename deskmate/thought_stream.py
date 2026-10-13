"""小桔的「想法流」（thought stream）—— 真实优先 · v2 第 4 层。

把自主行为**真实产出**的「想法」增量写一份带时间戳的流，每轮聊天按
「最近发生」召回 1-2 条 —— 让「她最近在想的事」进屋。

为什么需要它
------------
reflection（对旧看法做裁决 + 留「为什么」）与 worldview_reflection
（把看世界的理解沉淀成「我相信」）产出的质量极高，但此前只写回
``item_history`` / ``profile_items``，聊天上下文**读不到** —— 等于
她想通了一些事，却从没在和你说话时带出来。本模块补上这条召回链路。

设计要点（与产品哲学一致）
--------------------------
- **只存她真的产出的**：reflection 的裁决理由、worldview 的「我相信」。
  不注入任何系统替她编的「想法」——零假数据（这是 v2 红线：
  系统只提供真实状态，不提供表达方式）。
- **召回以近因（recency）为主**：她此刻在想什么，比「为贴合当前话题
  去检索」更真实。「相关性召回（语义）」留接口，本期不引入重依赖。
- **fail-open**：建表 / 读写任何异常 → 静默降级，绝不阻断调用方主流程
  （聊天、tick、reflection、worldview 回望）。
- **不进 SYNC_TABLES**：这是她自己的内心活动，像 emotion_state 一样
  不跨端同步（每个副本有自己想法；云端 5 张 SYNC_TABLES 不扩这张）。

存储：``deskmate.db`` 的 ``thought_stream`` 表（随库存在，零独立文件）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 软上限：想法流会随 tick 缓慢累积，超过则清理至 _KEEP_ROWS（避免无限增长）。
_MAX_ROWS = 300
_KEEP_ROWS = 200


def _db_path(base_dir: str) -> str:
    return f"{base_dir}/deskmate.db"


def _connect(base_dir: str) -> sqlite3.Connection:
    """独立连接（WAL）。不依赖 emotion_engine 的 _connect，避免循环/耦合。"""
    conn = sqlite3.connect(_db_path(base_dir), timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS thought_stream ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts TEXT NOT NULL,"
        "kind TEXT NOT NULL,"
        "text TEXT NOT NULL,"
        "about TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thought_stream_ts "
        "ON thought_stream(ts DESC)"
    )


def add_thought(
    base_dir: str,
    text: str,
    kind: str = "thought",
    about: Optional[str] = None,
) -> bool:
    """写一条想法。返回是否成功写入。fail-open：任何异常 → False。

    Args:
        base_dir: 数据根目录（含 deskmate.db）。
        text: 想法正文（自动 strip，截断至 500 字）。
        kind: 来源类型 —— ``reflection`` / ``worldview`` / 其他。
        about: 可选主题（worldview 的「关于 X」）。
    """
    text = (text or "").strip()
    kind = (kind or "thought").strip() or "thought"
    if not text:
        return False
    try:
        with _connect(base_dir) as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO thought_stream (ts, kind, text, about) "
                "VALUES (?, ?, ?, ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), kind, text[:500], about),
            )
            # 软上限清理（偶尔触发，不影响主路径写入）
            cur = conn.execute("SELECT COUNT(*) FROM thought_stream").fetchone()
            if cur and cur[0] > _MAX_ROWS:
                cutoff = conn.execute(
                    "SELECT id FROM thought_stream "
                    "ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (_KEEP_ROWS,),
                ).fetchone()
                if cutoff:
                    conn.execute(
                        "DELETE FROM thought_stream WHERE id <= ?", (cutoff[0],)
                    )
        return True
    except Exception as e:  # fail-open：写库失败不阻断调用方
        logger.warning("thought_stream: 写想法失败: %s", e)
        return False


def recent_thoughts(
    base_dir: str,
    limit: int = 2,
    since_hours: float = 24.0 * 30,
) -> list[str]:
    """最近 N 条想法（按 ts DESC，相邻去重）。fail-open → []。

    Args:
        limit: 返回条数上限（默认 2，对应每轮「最近在想的事」）。
        since_hours: 时间窗（默认 30 天）—— tick 间隔长，窗口放宽以免漏掉。

    返回：想法正文列表（不含 kind/about 噪音）。
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []
    try:
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - max(0.0, since_hours) * 3600.0),
        )
        # 多取一些再做去重，避免去重后不足 limit 条
        fetch = limit * 4
        with _connect(base_dir) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT text FROM thought_stream "
                "WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, fetch),
            ).fetchall()
        out: list[str] = []
        prev = None
        for r in rows:
            t = (r[0] or "").strip()
            if not t or t == prev:
                continue
            out.append(t)
            prev = t
            if len(out) >= limit:
                break
        return out
    except Exception as e:  # fail-open：取数失败不阻断注入
        logger.warning("thought_stream: 读想法失败: %s", e)
        return []
