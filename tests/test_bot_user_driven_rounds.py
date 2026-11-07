"""用户驱动续轮回归 —— 保质硬前提下的「不自动烧满 3 轮」。

把一轮讨论的控制权交还给用户：发一句只跑第 1 轮，done 带 can_continue /
next_round；点「再聊一轮」走 /continue，基于完整群历史再跑 1 轮串行接话。
**3 轮 / 10 条硬上限不取消，只是不再自动烧满**（docs/bot-mode-redesign.md §19）。

逐条锁死：
1. /stream 默认 1 轮、done 带续轮信号；
2. /continue 不落新 user 消息，且 bot 看得到前一轮所有发言（保质核心）；
3. 轮次/消息数跨续轮累计，到 3 轮 / 10 条收口；
4. 续轮天然串行接话（第 1 轮并发只在 round_no == 1）；
5. 群不撤、历史累积，撤群后续轮被拒；
6. 旧 ``say_stream(rounds=None)`` 仍是 3 轮，向后兼容。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coworker.teams.chat import ChatStore
from deskmate.bots import rooms
from deskmate.bots.discussion import DiscussionLimits
from deskmate.bots.web import create_app


class _RecordingRuntime:
    """记录每次发言看到的 history 与轮号，返回值按轮号区分。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def speak_stream(
        self, bot_id: str, text: str, *, history=None, round_index: int = 1, **kwargs
    ):
        self.calls.append(
            {
                "bot": bot_id,
                "round": round_index,
                "text": text,
                "history": [str(m.get("text") or "") for m in (history or [])],
            }
        )
        out = f"[{bot_id}] 第{round_index}轮接话"
        for ch in out:
            yield {"type": "delta", "text": ch}
        yield {"type": "final", "text": out}


def _frames(resp) -> list[dict[str, Any]]:
    return [
        json.loads(line[5:].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]


async def _collect(agen) -> list[dict[str, Any]]:
    return [ev async for ev in agen]


def _client(tmp_path: Path, fake: _RecordingRuntime, ids=("historian", "lit-editor")):
    client = TestClient(create_app(str(tmp_path), runtime=fake))
    rid = client.post(
        "/api/rooms", json={"topic": "烧仓往下写", "bot_ids": list(ids)}
    ).json()["room_id"]
    return client, rid


# ---------------------------------------------------------------- 1. 默认 1 轮

def test_stream_defaults_to_one_round_with_continue_signal(tmp_path) -> None:
    fake = _RecordingRuntime()
    client, rid = _client(tmp_path, fake)
    frames = _frames(client.post(f"/api/rooms/{rid}/stream", json={"text": "聊聊烧仓"}))
    done = frames[-1]

    assert done["type"] == "done"
    assert done["rounds"] == 1, "一次发言不该自动跑满 3 轮"
    assert done["total_rounds"] == 1
    assert done["can_continue"] is True
    assert done["next_round"] == 2
    assert [f["round"] for f in frames if f["type"] == "round_start"] == [1]
    assert sum(f["type"] == "bot_end" for f in frames) == 2, "两位 bot 各署名一条"

    msgs = client.get(f"/api/rooms/{rid}/messages").json()
    assert [m["author_role"] for m in msgs] == ["user", "bot", "bot"]


# ---------------------------------------------------------------- 2. 续轮保质

def test_continue_sees_first_round_and_posts_no_user_message(tmp_path) -> None:
    fake = _RecordingRuntime()
    client, rid = _client(tmp_path, fake)
    _frames(client.post(f"/api/rooms/{rid}/stream", json={"text": "聊聊烧仓"}))
    before = client.get(f"/api/rooms/{rid}/messages").json()
    first_bot_texts = [m["text"] for m in before if m["author_role"] == "bot"]

    frames = _frames(
        client.post(f"/api/rooms/{rid}/continue", json={"round_index": 2})
    )
    done = frames[-1]
    assert done["rounds"] == 1
    assert done["total_rounds"] == 2
    assert done["next_round"] == 3
    assert [f["round"] for f in frames if f["type"] == "round_start"] == [2]
    assert all(f.get("round") == 2 for f in frames if f["type"] == "bot_end")

    after = client.get(f"/api/rooms/{rid}/messages").json()
    assert sum(m["author_role"] == "user" for m in after) == sum(
        m["author_role"] == "user" for m in before
    ), "续轮不得落新 user 消息（否则群历史被污染）"
    assert len(after) == len(before) + 2, "续轮新增两位 bot 的署名发言"

    # 保质核心：续轮时每位 bot 都看到第 1 轮两位的完整发言，不是并列独白。
    round2 = [c for c in fake.calls if c["round"] == 2]
    assert len(round2) == 2
    for t in first_bot_texts:
        assert any(t in c["history"] for c in round2), "续轮没接上前文"


def test_continue_is_serial_not_parallel_first_round(tmp_path) -> None:
    """第 1 轮并发（§16）只在 round_no == 1；续轮必须是串行接话。"""
    fake = _RecordingRuntime()
    client, rid = _client(tmp_path, fake)
    _frames(client.post(f"/api/rooms/{rid}/stream", json={"text": "聊聊"}))
    frames = _frames(
        client.post(f"/api/rooms/{rid}/continue", json={"round_index": 2})
    )
    # 串行分支的事件顺序：bot_start 之后紧跟该 bot 的 delta，不会出现两位 bot 交错。
    starts = [i for i, f in enumerate(frames) if f["type"] == "bot_start"]
    assert len(starts) == 2
    first_end = next(i for i, f in enumerate(frames) if f["type"] == "bot_end")
    assert starts[1] > first_end, "第 2 位不该在第 1 位说完前就开始（并行）"


# ---------------------------------------------------------------- 3. 累计护栏

def test_rounds_cap_accumulates_across_continues(tmp_path) -> None:
    fake = _RecordingRuntime()
    client, rid = _client(tmp_path, fake)
    totals = [_frames(client.post(f"/api/rooms/{rid}/stream", json={"text": "聊聊"}))[-1]]
    for n in (2, 3):
        totals.append(
            _frames(client.post(f"/api/rooms/{rid}/continue", json={"round_index": n}))[-1]
        )
    assert [d["total_rounds"] for d in totals] == [1, 2, 3]
    assert totals[0]["can_continue"] is True
    assert totals[1]["can_continue"] is True
    assert totals[2]["can_continue"] is False, "跑满 3 轮后不能再续"

    # 前端不会发第 4 次，但后端必须挡住（护栏不依赖前端自觉）。
    d4 = _frames(
        client.post(f"/api/rooms/{rid}/continue", json={"round_index": 4})
    )[-1]
    assert d4["bounded"] == "max_rounds"
    assert d4["can_continue"] is False
    assert d4["rounds"] == 0, "到界后一轮都不该跑"


def test_message_cap_accumulates_across_continues(tmp_path) -> None:
    """3 位 bot + max_messages=4：第 2 轮只够再落 1 条就收口。"""
    cs = ChatStore(tmp_path / "chat.db")
    store = rooms.RoomStore(
        tmp_path, chat_store=cs, limits=DiscussionLimits(max_messages=4)
    )
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _RecordingRuntime()

    d1 = asyncio.run(
        _collect(store.say_stream(room.room_id, "聊聊", runtime=fake, rounds=1))
    )[-1]
    assert d1["message_count"] == 3
    assert d1["can_continue"] is True

    d2 = asyncio.run(_collect(store.continue_stream(room.room_id, 2, runtime=fake)))[-1]
    assert d2["message_count"] == 4
    assert d2["bounded"] == "max_messages"
    assert d2["can_continue"] is False
    store.close()
    cs.close()


def test_legacy_default_still_three_rounds(tmp_path) -> None:
    """旧调用方不传 rounds 时行为不变（向后兼容，不破坏 §16 的语义）。"""
    cs = ChatStore(tmp_path / "chat.db")
    store = rooms.RoomStore(tmp_path, chat_store=cs)
    room = store.create_room("话题", ["historian", "lit-editor"])
    fake = _RecordingRuntime()
    done = asyncio.run(_collect(store.say_stream(room.room_id, "聊聊", runtime=fake)))[-1]
    assert done["total_rounds"] == 3
    assert done["bounded"] == "max_rounds"
    store.close()
    cs.close()


# ---------------------------------------------------------------- 4. 群生命周期

def test_room_survives_continues_and_disband_blocks_continue(tmp_path) -> None:
    """跑完不撤群；群与共享材料一直在，只有显式撤群才拦住续轮。"""
    fake = _RecordingRuntime()
    client, rid = _client(tmp_path, fake)
    _frames(client.post(f"/api/rooms/{rid}/stream", json={"text": "聊聊"}))
    _frames(client.post(f"/api/rooms/{rid}/continue", json={"round_index": 2}))

    assert any(r["room_id"] == rid for r in client.get("/api/rooms").json())
    msgs = client.get(f"/api/rooms/{rid}/messages").json()
    assert len(msgs) == 5  # 1 user + 2 + 2

    assert client.delete(f"/api/rooms/{rid}").status_code == 200
    resp = client.post(f"/api/rooms/{rid}/continue", json={"round_index": 3})
    assert resp.status_code == 404, "撤群后续轮必须被拒"
    # 撤群只打墓碑：chat.db 里的历史消息没被物理删除。
    cs = ChatStore(tmp_path / "chat.db")
    assert len(cs.messages(rid)) == 5
    cs.close()
