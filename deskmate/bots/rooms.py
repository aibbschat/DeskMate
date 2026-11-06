"""群房间 —— bot 常驻，群随建随撤；发言复用官方 ChatStore。

对照 Hermes Bot Mode 本地源码 ``gateway/hosted_rooms.py`` / ``session_group_controls.py``：
- ``create_room`` / ``disband_room`` / ``list_rooms`` / ``room_state`` 是一等公民；
- ``hosted_rooms.disbanded_at`` 打墓碑 + ``hosted_room_retired_ids`` 永久 retired id
  （room_id 永不复用），消息历史不删；
- RPC 只有 ``groups.create / groups.send / groups.disband / groups.state / groups.log``，
  **没有任何"议题表单"**。

我们的落法（零改动 ``coworker/teams/*`` 上游干净代码）：
- 群消息 / @ 解析 / 未读唤醒全部走官方 ``ChatStore``（``post`` 里
  ``author_role="user"`` 天然唤醒全员，正是「不 @ = 全员」语义）；
- 房间元数据（话题、成员、墓碑）放在同 base 的独立 ``bots.db``，
  不碰官方 ``chat.db`` 的表结构，也不写进 ``teams.json``；
- 不建 team、不建 board item、不设 lead、不排 cron、不自动派活。

``chat.db`` 里现存的 11 个群全部叫 ``圆桌终审委员会·final-review:...`` 且成员
``persona`` 为空串 —— 那是旧圆桌桥造的僵尸群（只能建不能撤）。本模块建的群
**persona 真填**，语义上是「把常驻 bot 拉进这个群」，不是「登记一个 worker 名字」。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from coworker.teams.chat import ChatStore

from deskmate.teams_client import resolve_base

from .discussion import DiscussionLimits, bound_reason, resolve_mentions
from .roster import KNOWN_BOT_IDS, list_bots

logger = logging.getLogger(__name__)

_DEFAULT_RUNTIME: Any = None
_DEFAULT_RUNTIME_LOCK = threading.Lock()


def _default_runtime() -> Any:
    """懒加载真 Agent 运行时：import 期绝不构造 provider / 起 engine。"""
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            from .runtime import BotRuntime

            _DEFAULT_RUNTIME = BotRuntime()
        return _DEFAULT_RUNTIME


@dataclass(frozen=True)
class Room:
    room_id: str
    topic: str
    bot_ids: list[str]
    bot_names: dict[str, str]
    created_at: float
    disbanded_at: Optional[float] = None


class RoomStore:
    """bot 群的元数据 + 发言入口。base 默认与主 app 共享 ``~/.config/coworker``。"""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        chat_store: ChatStore | None = None,
        limits: DiscussionLimits | None = None,
    ) -> None:
        self.base = Path(base_dir).expanduser() if base_dir else resolve_base()
        self.chat_store = chat_store or ChatStore(self.base / "chat.db")
        self.limits = limits or DiscussionLimits()
        self.db_path = self.base / "bots.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_rooms (
                    room_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    bot_ids TEXT NOT NULL,
                    bot_names TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    disbanded_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_bot_rooms_live
                    ON bot_rooms (disbanded_at, created_at);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------ 读

    def get_room(self, room_id: str, *, include_disbanded: bool = False) -> Optional[Room]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bot_rooms WHERE room_id=?", (room_id,)
            ).fetchone()
        if row is None:
            return None
        room = _row_to_room(row)
        if room.disbanded_at is not None and not include_disbanded:
            return None
        return room

    def list_rooms(self, *, include_disbanded: bool = False) -> list[Room]:
        sql = "SELECT * FROM bot_rooms"
        if not include_disbanded:
            sql += " WHERE disbanded_at IS NULL"
        sql += " ORDER BY created_at ASC, room_id ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [_row_to_room(r) for r in rows]

    # ------------------------------------------------------------------ 写

    def create_room(self, topic: str, bot_ids: list[str]) -> Room:
        """按话题拉一个群。**只有话题 + 成员两个入参，没有表单**。"""
        clean_topic = (topic or "").strip()
        if not clean_topic:
            raise ValueError("群话题不能为空")
        ids = list(dict.fromkeys(str(b).strip() for b in (bot_ids or []) if str(b).strip()))
        lim = self.limits
        if not (lim.min_members <= len(ids) <= lim.max_members):
            raise ValueError(
                f"群成员须为 {lim.min_members}~{lim.max_members} 个 bot，当前 {len(ids)}"
            )
        unknown = [b for b in ids if b not in KNOWN_BOT_IDS]
        if unknown:
            raise ValueError(f"未知 bot：{', '.join(unknown)}（bot 必须来自常驻名册）")

        names = {b.id: b.name for b in list_bots()}
        bot_names = {b: names.get(b, b) for b in ids}
        # persona 真填：与旧圆桌 11 个僵尸群（persona 空串）的关键区别。
        members = [
            {"name": b, "persona": b, "role": "worker"} for b in ids
        ]
        group = self.chat_store.create_group(name=clean_topic, members=members)
        room = Room(
            room_id=str(group["group_id"]),
            topic=clean_topic,
            bot_ids=ids,
            bot_names=bot_names,
            created_at=time.time(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO bot_rooms"
                " (room_id, topic, bot_ids, bot_names, created_at, disbanded_at)"
                " VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    room.room_id,
                    room.topic,
                    json.dumps(ids, ensure_ascii=False),
                    json.dumps(bot_names, ensure_ascii=False),
                    room.created_at,
                ),
            )
            self._conn.commit()
        return room

    def disband_room(self, room_id: str) -> dict[str, Any]:
        """撤群：只打墓碑，消息历史保留，room_id 永不复用（幂等）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT disbanded_at FROM bot_rooms WHERE room_id=?", (room_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"未知群 {room_id}")
            if row["disbanded_at"] is not None:
                return {
                    "room_id": room_id,
                    "disbanded_at": float(row["disbanded_at"]),
                    "idempotent": True,
                }
            now = time.time()
            self._conn.execute(
                "UPDATE bot_rooms SET disbanded_at=? WHERE room_id=? AND disbanded_at IS NULL",
                (now, room_id),
            )
            self._conn.commit()
        return {"room_id": room_id, "disbanded_at": now, "idempotent": False}

    def post(
        self,
        room_id: str,
        author: str,
        text: str,
        *,
        author_role: str = "user",
    ) -> dict[str, Any]:
        """在群里说一句人话。@ 解析与未读唤醒由官方 ChatStore 原生承担。"""
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"群 {room_id} 不存在或已解散")
        return self.chat_store.post(
            room_id, author, text, author_role=author_role
        )

    # ---------------------------------------------------------------- 说人话
    def say(
        self,
        room_id: str,
        text: str,
        *,
        runtime: Any = None,
        author: str = "user",
    ) -> dict[str, Any]:
        """群里说一句 → 唤醒 bot → 各自署名回话。**不填表单、不出纪要**。

        - 不 @ = 全员（受 ``max_members`` 约束），``@谁`` 只唤醒谁；
        - 每个 bot 是**真 Agent**：要不要读稿、读哪一章由它自己决定，
          用户不必点「阅读全书」，也不必给路径；
        - 一轮发言有硬上限（``max_messages``）；bot 之间可接话，但不会
          互相递归唤醒（bot 消息的 ``author_role`` 不是 user）；
        - **绝不生成纪要 / 总结 / 主持人收尾**（萧哥 11-06 决策 3）。
        """
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"群 {room_id} 不存在或已解散")
        clean = (text or "").strip()
        if not clean:
            raise ValueError("群里不能发空话")

        rt = runtime if runtime is not None else _default_runtime()
        user_msg = self.chat_store.post(room_id, author, clean, author_role="user")
        woken = list(resolve_mentions(clean, room.bot_ids))[: self.limits.max_members]
        history = list(self.chat_store.messages(room_id))
        replies: list[dict[str, Any]] = []
        bounded: str | None = None

        for bot_id in woken:
            reason = bound_reason(1, len(replies), self.limits)
            if reason:
                bounded = reason
                break
            try:
                reply = rt.speak(bot_id, clean, history=history)
            except Exception as exc:  # noqa: BLE001 —— 一个 bot 挂了不拖垮整群
                logger.warning("bot %s 发言失败：%s", bot_id, exc)
                continue
            if not (reply or "").strip():
                continue
            msg = self.chat_store.post(
                room_id, bot_id, reply, author_role="bot"
            )
            self.chat_store.consume(room_id, bot_id, int(msg["seq"]))
            replies.append(
                {
                    "seq": msg["seq"],
                    "author": bot_id,
                    "name": room.bot_names.get(bot_id, bot_id),
                    "text": reply,
                    "author_role": "bot",
                }
            )
            # 后发言的 bot 看得到前面 bot 刚说的话（真讨论，不是并列的独白），
            # 但不额外增加模型调用数。
            history.append(
                {"author": bot_id, "author_role": "bot", "text": reply}
            )
        return {
            "room_id": room_id,
            "user_message": user_msg,
            "woken": list(woken),
            "replies": replies,
            "bounded": bounded,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_room(row: sqlite3.Row) -> Room:
    disbanded = row["disbanded_at"]
    return Room(
        room_id=str(row["room_id"]),
        topic=str(row["topic"]),
        bot_ids=list(json.loads(row["bot_ids"] or "[]")),
        bot_names=dict(json.loads(row["bot_names"] or "{}")),
        created_at=float(row["created_at"]),
        disbanded_at=float(disbanded) if disbanded is not None else None,
    )
