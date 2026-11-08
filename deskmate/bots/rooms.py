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

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from coworker.teams.chat import ChatStore

from deskmate.teams_client import resolve_base

from .discussion import (
    DiscussionLimits,
    bound_reason,
    is_deep_dig,
    is_pass,
    resolve_mentions,
)
from .materials import RoomMaterials, default_materials
from .reading import default_shelf
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
        materials: RoomMaterials | None = None,
    ) -> None:
        self.base = Path(base_dir).expanduser() if base_dir else resolve_base()
        self.chat_store = chat_store or ChatStore(self.base / "chat.db")
        self.limits = limits or DiscussionLimits()
        # 群级共享材料面：一次备料全群取用，测试可注入临时书架。
        self.materials = materials if materials is not None else default_materials(default_shelf())
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

    def room_previews(self, room_ids: list[str]) -> dict[str, dict[str, Any]]:
        """每个群的「最后一条」摘要，供左栏列表当预览用。

        只读复用官方 ChatStore，**不新增表、不加已读游标**：未读计数需要一个持久的
        ``last_read_seq``，那是另一笔改动。宁可不显示红点，也不拿「最后一条是 bot 说的」
        冒充未读 —— 那种红点用户点进去发现没新东西，比没有更糟。

        ``messages`` 只能正序取（``ORDER BY seq LIMIT``），所以这里按默认上限拉一次
        再取尾部；群本身有界（每轮 ≤10 条），量级不值得为此加索引。
        """
        out: dict[str, dict[str, Any]] = {}
        for rid in room_ids:
            try:
                msgs = self.chat_store.messages(rid)
            except Exception:  # noqa: BLE001 —— 预览取不到不该让整块群列表空掉
                logger.warning("群预览读取失败 room=%s", rid, exc_info=True)
                continue
            last = msgs[-1] if msgs else None
            out[rid] = {
                "last_text": (str(last.get("text") or "")[:48] if last else ""),
                "last_ts": last.get("ts") if last else None,
                "last_author": str(last.get("author") or "") if last else "",
                "last_author_role": str(last.get("author_role") or "") if last else "",
                "count": len(msgs),
            }
        return out

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
        # 群级材料随撤群清掉（消息历史不动，room_id 永不复用）。
        self.materials.drop(room_id)
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

    @staticmethod
    def _parallel_enabled() -> bool:
        """第 1 轮并发开关；设 ``DESKMATE_BOT_PARALLEL_FIRST_ROUND=0`` 一键回到
        全串行（语义存疑时保讨论原样，不必改代码）。默认开。"""
        return os.environ.get("DESKMATE_BOT_PARALLEL_FIRST_ROUND", "1") != "0"

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
        # 深挖判定（2026-11-08 能力常备化后降级为**信号放大器**）：材料默认
        # 已是全书（materials.py）、read_book 全书、迭代 12 —— 能力永远在线，
        # 「无论讨论什么都启动」由能力层保证，不靠这个词。词命中只额外给
        # 强作业指令（build_prompt deep lead），且只作用于第 1 轮。
        deep = is_deep_dig(clean)
        # 群级一次备料（纯本地 IO，零 LLM）：所有人共用同一份**全书**材料。
        pack = self.materials.prepare(room_id, clean)
        user_msg = self.chat_store.post(room_id, author, clean, author_role="user")
        aliases = {name: bot_id for bot_id, name in room.bot_names.items()}
        woken = list(
            resolve_mentions(clean, room.bot_ids, aliases=aliases)
        )[: self.limits.max_members]
        # 一对一作业形态（2026-11-08 萧哥实证「独立专家 > 群聊」后固化）：
        # 本轮只唤醒一位（@ 单独一位，或群本身就只有一位成员）= 作者单独
        # 找它 —— guidance / lead 换作业版，形态约束卸掉；预算与群聊一致。
        # ≥2 位被唤醒才是群聊形态。solo 天然单轮（multi_round=False）。
        solo = len(woken) == 1
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
            deep_this_round = deep and round_no == 1
            # 第 1 轮并发：各 bot 看同一份群历史 + 同一份共享材料，独立出方向。
            # 第 2 轮起仍是原来的串行接话（后发言者看得到完整前文），语义不破。
            if self._parallel_enabled() and round_no == 1 and len(active) > 1:
                spoke = []
                snapshot = list(history)
                results: list[tuple[str, str]] = []
                with ThreadPoolExecutor(max_workers=min(len(active), 6)) as pool:
                    futures = {
                        pool.submit(
                            rt.speak,
                            bot_id,
                            clean,
                            history=snapshot,
                            names=room.bot_names,
                            room_id=room_id,
                            round_index=round_no,
                            materials=pack,
                            deep=deep_this_round,
                            solo=solo,
                        ): bot_id
                        for bot_id in list(active)
                    }
                    for fut in as_completed(futures):
                        bid = futures[fut]
                        try:
                            results.append((bid, fut.result()))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("bot %s 并发发言失败：%s", bid, exc)
                # 谁先好谁先落库；顺序即真实群聊里「谁先说话」。
                for bid, reply in results:
                    reason = bound_reason(completed_rounds, len(replies), self.limits)
                    if reason:
                        bounded = reason
                        break
                    if not (reply or "").strip() or is_pass(reply):
                        passed.append(bid)
                        continue
                    msg = self.chat_store.post(
                        room_id, bid, reply, author_role="bot"
                    )
                    self.chat_store.consume(room_id, bid, int(msg["seq"]))
                    replies.append(
                        {
                            "seq": msg["seq"],
                            "author": bid,
                            "name": room.bot_names.get(bid, bid),
                            "text": reply,
                            "author_role": "bot",
                        }
                    )
                    spoke.append(bid)
                first_round_ids = {b for b, _ in results}
                for row in replies:
                    if row["author_role"] == "bot" and row["author"] in first_round_ids:
                        history.append(
                            {"author": row["author"], "author_role": "bot", "text": row["text"]}
                        )
                completed_rounds += 1
                active = _next_active(spoke, multi_round=multi_round)
                continue
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
                        materials=pack,
                        deep=deep_this_round,
                        solo=solo,
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
        rounds: int | None = None,
        start_round: int = 1,
        post_user: bool = True,
        woken_override: list[str] | None = None,
        initial_message_count: int = 0,
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

        用户驱动续轮（默认 ``rounds=1``）：一次发言只跑一轮就交还控制权，
        ``done`` 带 ``can_continue`` / ``next_round``；用户点「再聊一轮」走
        ``continue_stream``，用 ``start_round`` / ``initial_message_count`` 把
        轮次与消息数累计起来，护栏仍是 3 轮 / 10 条（**上限不取消，只是不自动烧满**）。
        所有新参数都有默认值，旧调用方与回归测试行为不变。
        """
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"群 {room_id} 不存在或已解散")
        clean = (text or "").strip()
        if post_user and not clean:
            raise ValueError("群里不能发空话")

        rt = runtime if runtime is not None else _default_runtime()
        # 深挖判定（与 say() 同源同语义）：能力常备后只是信号放大器——
        # 材料全书、工具全书、迭代 12 恒定；词命中只给第 1 轮强作业指令。
        deep = is_deep_dig(clean)
        # 续轮传空 text：prepare() 沿用该群已备材料，但仍验 mtime 指纹 ——
        # 作者一边写稿一边聊，下一轮自动拿到最新进度（materials.py:164-169）。
        pack = self.materials.prepare(room_id, clean)
        if post_user:
            user_msg: dict[str, Any] = self.chat_store.post(
                room_id, author, clean, author_role="user"
            )
            turn_seq: Any = user_msg["seq"]
        else:
            user_msg = {"seq": None}
            turn_seq = f"c{start_round}"
        aliases = {name: bot_id for bot_id, name in room.bot_names.items()}
        woken = list(
            woken_override
            if woken_override is not None
            else resolve_mentions(clean, room.bot_ids, aliases=aliases)
        )[: self.limits.max_members]
        # 一对一作业形态（与 say() 同源同语义）：本轮只唤醒一位 = 单独对话。
        solo = len(woken) == 1
        # 当前这句单独进 prompt，不在群历史里重复出现；续轮没有新 user 消息，
        # 历史就是完整群历史（含前几轮所有发言）。
        cur_seq = int(user_msg.get("seq") or 0)
        history = [
            m
            for m in self.chat_store.messages(room_id)
            if int(m.get("seq") or 0) != cur_seq
        ]
        replies: list[dict[str, Any]] = []
        passed: list[str] = []
        bounded: str | None = None
        multi_round = len(woken) >= 2 and (rounds is None or rounds > 1)
        active = list(woken)
        completed_rounds = 0

        def _bound() -> str | None:
            """累计轮次 / 累计消息数一起判护栏（跨续轮不重置）。"""
            return bound_reason(
                start_round - 1 + completed_rounds,
                initial_message_count + len(replies),
                self.limits,
            )

        yield {
            "type": "start",
            "room_id": room_id,
            "user_message": user_msg,
            "woken": list(woken),
            "deep": deep,
            "solo": solo,
        }

        while active:
            if rounds is not None and completed_rounds >= rounds:
                break
            reason = _bound()
            if reason:
                bounded = reason
                break
            round_no = start_round + completed_rounds
            deep_this_round = deep and round_no == 1
            yield {"type": "round_start", "room_id": room_id, "round": round_no}
            # 第 1 轮并发：N 条 LLM 流通过事件队列交错推给前端，谁先好谁先落库。
            # 第 2 轮起仍走下面的串行接话，后发言者看得到完整前文。
            if self._parallel_enabled() and round_no == 1 and len(active) > 1:
                spoke = []
                snapshot = list(history)
                queue: asyncio.Queue = asyncio.Queue()

                async def _one(bot_id: str) -> None:
                    nm = room.bot_names.get(bot_id, bot_id)
                    tid = f"{room_id}:{turn_seq}:{round_no}:{bot_id}"
                    sc = {
                        "bot_id": bot_id, "name": nm,
                        "turn_id": tid, "round": round_no,
                    }
                    await queue.put({"type": "bot_start", **sc})
                    final_text = ""
                    failed = False
                    try:
                        async with contextlib.aclosing(
                            rt.speak_stream(
                                bot_id,
                                clean,
                                history=snapshot,
                                names=room.bot_names,
                                room_id=room_id,
                                round_index=round_no,
                                materials=pack,
                                deep=deep_this_round,
                                solo=solo,
                            )
                        ) as events:
                            async for ev in events:
                                if ev.get("type") == "final":
                                    final_text = str(ev.get("text") or "")
                                elif ev.get("type") == "error":
                                    failed = True
                                await queue.put({**ev, **sc})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("bot %s 并发流式发言失败：%s", bot_id, exc)
                        await queue.put({"type": "error", "message": str(exc), **sc})
                        failed = True
                    await queue.put({
                        "type": "_bot_done", **sc,
                        "final": final_text, "failed": failed,
                    })

                tasks = [asyncio.create_task(_one(b)) for b in list(active)]
                finished = 0
                try:
                    while finished < len(tasks):
                        ev = await queue.get()
                        if ev.get("type") != "_bot_done":
                            yield ev
                            continue
                        finished += 1
                        bid = ev["bot_id"]
                        reply = str(ev["final"] or "").strip()
                        sc = {k: ev[k] for k in ("bot_id", "name", "turn_id", "round")}
                        if bounded is None:
                            bounded = _bound()
                        if bounded is not None:
                            continue
                        if ev["failed"] or not reply or is_pass(reply):
                            if is_pass(reply):
                                passed.append(bid)
                            yield {
                                "type": "bot_pass",
                                "reason": "error" if ev["failed"] else (
                                    "pass" if is_pass(reply) else "empty"
                                ),
                                **sc,
                            }
                            continue
                        msg = self.chat_store.post(
                            room_id, bid, reply, author_role="bot"
                        )
                        self.chat_store.consume(room_id, bid, int(msg["seq"]))
                        item = {
                            "seq": msg["seq"],
                            "author": bid,
                            "name": ev["name"],
                            "text": reply,
                            "author_role": "bot",
                        }
                        replies.append(item)
                        yield {
                            "type": "bot_end", **item,
                            "turn_id": ev["turn_id"], "round": round_no,
                        }
                        spoke.append(bid)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                # 第 2 轮接话看得到完整的第 1 轮（与 say() 一致）。
                for row in replies:
                    history.append(
                        {"author": row["author"], "author_role": "bot", "text": row["text"]}
                    )
                completed_rounds += 1
                active = _next_active(spoke, multi_round=multi_round)
                continue
            spoke: list[str] = []
            for bot_id in list(active):
                reason = _bound()
                if reason:
                    bounded = reason
                    break
                name = room.bot_names.get(bot_id, bot_id)
                # turn_id 带轮次：同一个 bot 跨轮发言时气泡不能互相覆盖。
                turn_id = f"{room_id}:{turn_seq}:{round_no}:{bot_id}"
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
                            materials=pack,
                            deep=deep_this_round,
                            solo=solo,
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

        total_rounds = start_round - 1 + completed_rounds
        total_messages = initial_message_count + len(replies)
        # 还能续吗：没到护栏、本轮有人实质发言、也不是被截断收口。
        # 上限不取消，只是不再自动烧满 —— 交给用户按「再聊一轮」。
        can_continue = bounded is None and _bound() is None and bool(replies)
        yield {
            "type": "done",
            "room_id": room_id,
            "replies": replies,
            "passed": passed,
            "bounded": bounded,
            "rounds": completed_rounds,
            "total_rounds": total_rounds,
            "message_count": total_messages,
            "can_continue": can_continue,
            "next_round": start_round + completed_rounds,
            "max_rounds": self.limits.max_rounds,
            "max_messages": self.limits.max_messages,
        }

    # ------------------------------------------------------- 用户驱动续轮
    async def continue_stream(
        self,
        room_id: str,
        round_index: int,
        *,
        runtime: Any = None,
        bot_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """用户点「再聊一轮」：**不落新 user 消息**，基于完整群历史再跑 1 轮接话。

        - 材料包沿用该群已备的那份（``prepare(room_id, "")``），但每次仍验
          mtime 指纹 —— 作者改稿后自动换新，不需要说「重读」。
        - 累计轮次 / 消息数从**群历史**推导（最后一条 user 之后的 bot 消息数），
          不信任前端传的计数，也不新增存储。
        - ``start_round`` 让 ``build_prompt`` 用「第 N 轮接话」引导，且并发只在
          ``round_no == 1`` 生效 ⇒ 续轮天然是串行接话，保质不变量不破。
        """
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"群 {room_id} 不存在或已解散")
        rows = self.chat_store.messages(room_id)
        last_user_seq = 0
        for m in rows:
            if m.get("author_role") == "user":
                last_user_seq = int(m.get("seq") or 0)
        bot_count = sum(
            1
            for m in rows
            if m.get("author_role") == "bot"
            and int(m.get("seq") or 0) > last_user_seq
        )
        woken = list(bot_ids or room.bot_ids)[: self.limits.max_members]
        idx = max(1, int(round_index))
        async for ev in self.say_stream(
            room_id,
            "",
            runtime=runtime,
            rounds=1,
            start_round=idx,
            post_user=False,
            woken_override=woken,
            initial_message_count=bot_count,
        ):
            yield ev

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
