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

import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from coworker.teams.chat import ChatStore

from deskmate.teams_client import resolve_base

from .discussion import DiscussionLimits, bound_reason, is_pass, resolve_mentions
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
        """群里说一句 → 唤醒 bot → 多轮接话 → 各自署名回话。**不填表单、不出纪要**。

        - 不 @ = 全员（受 ``max_members`` 约束），``@谁`` 只唤醒谁；中文名 @ 也认；
        - 每个 bot 是**真 Agent**：要不要读稿由它自己决定；
        - ``max_rounds`` / ``max_messages`` 真正生效：≥2 位被唤醒才开多轮，
          每轮结束至少还有 2 位在实质发言才继续，全 pass / 独白立即收口；
        - bot 显式 pass 不落库，也不参与后续轮；
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
        aliases = {name: bot_id for bot_id, name in room.bot_names.items()}
        woken = list(
            resolve_mentions(clean, room.bot_ids, aliases=aliases)
        )[: self.limits.max_members]
        # 当前这句会单独进 prompt，从群历史里排除，避免同一句出现两次。
        history = [
            m
            for m in self.chat_store.messages(room_id)
            if int(m.get("seq") or 0) != int(user_msg["seq"])
        ]
        replies: list[dict[str, Any]] = []
        passed: list[str] = []
        bounded: str | None = None
        multi_round = len(woken) >= 2
        active = list(woken)
        completed_rounds = 0

        while active:
            reason = bound_reason(completed_rounds, len(replies), self.limits)
            if reason:
                bounded = reason
                break
            round_no = completed_rounds + 1
            spoke: list[str] = []
            for bot_id in list(active):
                reason = bound_reason(completed_rounds, len(replies), self.limits)
                if reason:
                    bounded = reason
                    break
                try:
                    reply = rt.speak(
                        bot_id,
                        clean,
                        history=history,
                        names=room.bot_names,
                        room_id=room_id,
                        round_index=round_no,
                    )
                except Exception as exc:  # noqa: BLE001 —— 一个 bot 挂了不拖垮整群
                    logger.warning("bot %s 发言失败：%s", bot_id, exc)
                    continue
                if not (reply or "").strip() or is_pass(reply):
                    passed.append(bot_id)
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
                # 后发言者看得到前面所有轮次的发言（真讨论，不是并列独白）。
                history.append(
                    {"author": bot_id, "author_role": "bot", "text": reply}
                )
                spoke.append(bot_id)
            completed_rounds += 1
            active = _next_active(spoke, multi_round=multi_round)

        return {
            "room_id": room_id,
            "user_message": user_msg,
            "woken": list(woken),
            "replies": replies,
            "passed": passed,
            "bounded": bounded,
            "rounds": completed_rounds,
        }

    # ------------------------------------------------------------ 说人话·流式
    async def say_stream(
        self,
        room_id: str,
        text: str,
        *,
        runtime: Any = None,
        author: str = "user",
    ) -> AsyncIterator[dict[str, Any]]:
        """``say()`` 的原生流式版本：逐 token 吐事件，**只在 bot 说完后落库一次**。

        与 ``say()`` 共用同一套语义（不 @ = 全员、@ 谁谁答、后发言者看得到前文、
        各自署名、不出纪要），区别只在传输形态：

        - 旧的 ``_drive`` 把 TurnEngine 的 ``ASSISTANT_DELTA`` 全丢了，前端只能等
          所有 bot 串行跑完拿一次性 JSON；这里改为边生成边 ``delta`` 推给 SSE。
        - 事件序列：``start`` → 每个 bot 的 ``bot_start`` → ``tool``/``delta``*
          → ``bot_end``（或 ``bot_pass``）→ ``done``。
        - ``bot_end`` 时用 ``final`` 的权威文本覆盖气泡，并与 ``chat_store.post``
          落库的文本完全一致（不双写、不留半成品消息）。

        被 ``aclose()``（客户端断开）时，``aclosing`` 会把 GeneratorExit 级联到
        ``runtime.speak_stream`` → ``engine.request_interrupt()`` 真正停掉 LLM 流。
        """
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"群 {room_id} 不存在或已解散")
        clean = (text or "").strip()
        if not clean:
            raise ValueError("群里不能发空话")

        rt = runtime if runtime is not None else _default_runtime()
        user_msg = self.chat_store.post(room_id, author, clean, author_role="user")
        aliases = {name: bot_id for bot_id, name in room.bot_names.items()}
        woken = list(
            resolve_mentions(clean, room.bot_ids, aliases=aliases)
        )[: self.limits.max_members]
        # 当前这句单独进 prompt，不在群历史里重复出现。
        history = [
            m
            for m in self.chat_store.messages(room_id)
            if int(m.get("seq") or 0) != int(user_msg["seq"])
        ]
        replies: list[dict[str, Any]] = []
        passed: list[str] = []
        bounded: str | None = None
        multi_round = len(woken) >= 2
        active = list(woken)
        completed_rounds = 0

        yield {
            "type": "start",
            "room_id": room_id,
            "user_message": user_msg,
            "woken": list(woken),
        }

        while active:
            reason = bound_reason(completed_rounds, len(replies), self.limits)
            if reason:
                bounded = reason
                break
            round_no = completed_rounds + 1
            yield {"type": "round_start", "room_id": room_id, "round": round_no}
            spoke: list[str] = []
            for bot_id in list(active):
                reason = bound_reason(completed_rounds, len(replies), self.limits)
                if reason:
                    bounded = reason
                    break
                name = room.bot_names.get(bot_id, bot_id)
                # turn_id 带轮次：同一个 bot 跨轮发言时气泡不能互相覆盖。
                turn_id = f"{room_id}:{user_msg['seq']}:{round_no}:{bot_id}"
                scope = {
                    "bot_id": bot_id, "name": name,
                    "turn_id": turn_id, "round": round_no,
                }
                yield {"type": "bot_start", **scope}

                final_text = ""
                failed = False
                try:
                    # aclosing 保证断开时 GeneratorExit 一路传到 request_interrupt()
                    async with contextlib.aclosing(
                        rt.speak_stream(
                            bot_id,
                            clean,
                            history=history,
                            names=room.bot_names,
                            room_id=room_id,
                            round_index=round_no,
                        )
                    ) as events:
                        async for ev in events:
                            if ev.get("type") == "final":
                                final_text = str(ev.get("text") or "")
                            elif ev.get("type") == "error":
                                failed = True
                            yield {**ev, **scope}
                except Exception as exc:  # noqa: BLE001 —— 一个 bot 挂了不拖垮整群
                    logger.warning("bot %s 流式发言失败：%s", bot_id, exc)
                    yield {"type": "error", "message": str(exc), **scope}
                    failed = True

                reply = final_text.strip()
                if failed or not reply or is_pass(reply):
                    if is_pass(reply):
                        passed.append(bot_id)
                    yield {
                        "type": "bot_pass",
                        "reason": "error" if failed else ("pass" if is_pass(reply) else "empty"),
                        **scope,
                    }
                    continue

                msg = self.chat_store.post(room_id, bot_id, reply, author_role="bot")
                self.chat_store.consume(room_id, bot_id, int(msg["seq"]))
                item = {
                    "seq": msg["seq"],
                    "author": bot_id,
                    "name": name,
                    "text": reply,
                    "author_role": "bot",
                }
                replies.append(item)
                yield {"type": "bot_end", **item, "turn_id": turn_id, "round": round_no}
                # 后发言者看得到前面所有轮次的发言（真讨论，不是并列独白）。
                history.append({"author": bot_id, "author_role": "bot", "text": reply})
                spoke.append(bot_id)

            completed_rounds += 1
            active = _next_active(spoke, multi_round=multi_round)

        yield {
            "type": "done",
            "room_id": room_id,
            "replies": replies,
            "passed": passed,
            "bounded": bounded,
            "rounds": completed_rounds,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _next_active(spoke: list[str], *, multi_round: bool) -> list[str]:
    """下一轮还要继续吗？

    至少 2 位在上一轮有实质发言才继续 —— 只剩 1 位就是一人独白，全 pass
    更是没有讨论可续，两种情况都立即收口，避免硬凑满 3 轮灌水。
    """
    if not multi_round or len(spoke) < 2:
        return []
    return list(spoke)


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
