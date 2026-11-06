"""Bot 群「讨论能力」六项硬伤回归。

对应 docs/bot-mode-redesign.md 阶段 2.5 之后的补齐：
1. max_rounds 不再是死代码（真正跑 3 轮 / 10 条硬上限，且不当独白）；
2. 中文名进 format_history（人设里的接话示范才对得上说话人）；
3. @中文名能唤醒对应 bot（UI 显示中文名，@英文 id 是反直觉的）；
4. 显式 pass 不落库、不参与后续轮；
5. engine 按 (room, bot) 隔离，A 群的话不漏进 B 群；
6. 上下文只有一份：当前句不重复，engine.messages 不累积群历史副本。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from coworker.providers.base import AssistantTurn, ModelCapabilities, StreamChunk
from coworker.teams.chat import ChatStore
from deskmate.bots import discussion, rooms, runtime
from deskmate.bots.runtime import BotRuntime, build_prompt, format_history


class _DummyProvider:
    """够 engine 构造用的最小离线 provider。"""

    def capabilities(self, model):
        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **kwargs):
        return AssistantTurn(text="ok")

    def stream(self, *, model, messages, tools=None, **kwargs):
        yield StreamChunk(turn=AssistantTurn(text="ok"))


class _RecordingRuntime:
    """记录每次 speak 的上下文，返回值可配置。"""

    def __init__(self, reply: Any = "默认发言") -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply

    def _text(self, bot_id, kwargs) -> str:
        if callable(self.reply):
            return str(self.reply(bot_id, kwargs))
        return str(self.reply)

    def speak(self, bot_id, text, *, history=None, **kwargs) -> str:
        self.calls.append(
            {"bot_id": bot_id, "text": text, "history": list(history or []), **kwargs}
        )
        return self._text(bot_id, kwargs)

    async def speak_stream(self, bot_id, text, *, history=None, **kwargs):
        self.calls.append(
            {"bot_id": bot_id, "text": text, "history": list(history or []), **kwargs}
        )
        text_out = self._text(bot_id, kwargs)
        yield {"type": "delta", "text": text_out[:2]}
        yield {"type": "final", "text": text_out}


def _pair(tmp_path: Path):
    cs = ChatStore(tmp_path / "chat.db")
    return cs, rooms.RoomStore(tmp_path, chat_store=cs)


def _runtime(tmp_path: Path) -> BotRuntime:
    return BotRuntime(
        books_root=tmp_path,
        workspace_root=tmp_path / "ws",
        provider=_DummyProvider(),
        model="test-model",
    )


# ---------------------------------------------------------------- 1. 多轮

def test_multi_round_runs_three_rounds_and_stops_at_max_rounds(tmp_path) -> None:
    """3 位 bot 全员讨论 → 正好 3 轮 9 条，bounded=max_rounds。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])

    def reply(bot_id, kwargs):
        return f"[{bot_id}] 第{kwargs.get('round_index')}轮"

    fake = _RecordingRuntime(reply)
    out = store.say(room.room_id, "大家讨论", runtime=fake)

    assert out["rounds"] == 3
    assert {c.get("round_index") for c in fake.calls} == {1, 2, 3}
    assert len(out["replies"]) == 9
    assert out["bounded"] == "max_rounds"
    assert len(cs.messages(room.room_id)) == 10  # 1 user + 9 bot
    store.close()
    cs.close()


def test_multi_round_stops_at_max_messages(tmp_path) -> None:
    """6 位 bot 全员 → 消息数先到 10 触发 max_messages（消息数优先于轮次）。"""
    cs, store = _pair(tmp_path)
    room = store.create_room(
        "话题",
        ["historian", "lit-editor", "proofreader", "reader", "publisher", "genre"],
    )
    out = store.say(room.room_id, "大家讨论", runtime=_RecordingRuntime())
    assert len(out["replies"]) == 10
    assert out["bounded"] == "max_messages"
    store.close()
    cs.close()


def test_single_mention_stays_one_round(tmp_path) -> None:
    """@ 单人只答一句，不自说自话凑 3 轮。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _RecordingRuntime()
    out = store.say(room.room_id, "@historian 你怎么看", runtime=fake)
    assert out["rounds"] == 1
    assert len(out["replies"]) == 1
    assert out["bounded"] is None
    assert [c["bot_id"] for c in fake.calls] == ["historian"]
    store.close()
    cs.close()


def test_all_pass_ends_immediately(tmp_path) -> None:
    """全员 pass → 只跑 1 轮就收口，群里不落任何 bot 消息。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "reader"])
    out = store.say(room.room_id, "大家聊聊", runtime=_RecordingRuntime("我 pass"))
    assert out["rounds"] == 1
    assert out["replies"] == []
    assert out["bounded"] is None
    assert len(out["passed"]) == 2
    assert len(cs.messages(room.room_id)) == 1, "pass 不落库"
    store.close()
    cs.close()


def test_stream_rounds_carry_round_no_and_unique_turn_ids(tmp_path) -> None:
    """流式下每轮有 round_start，turn_id 带轮次（同 bot 跨轮气泡不互相覆盖）。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor"])
    events = asyncio.run(
        _collect(store.say_stream(room.room_id, "大家讨论", runtime=_RecordingRuntime()))
    )
    rounds = [e["round"] for e in events if e["type"] == "round_start"]
    assert rounds == [1, 2, 3]
    turn_ids = [e["turn_id"] for e in events if e["type"] == "bot_start"]
    assert len(turn_ids) == len(set(turn_ids)), "跨轮 turn_id 必须唯一"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["rounds"] == 3 and done["bounded"] == "max_rounds"
    store.close()
    cs.close()


async def _collect(agen):
    return [ev async for ev in agen]


# ---------------------------------------------------------------- 2. 中文名

def test_format_history_uses_chinese_names() -> None:
    history = [
        {"author": "historian", "author_role": "bot", "text": "史实无误"},
        {"author": "user", "author_role": "user", "text": "这句呢"},
    ]
    out = format_history(history, names={"historian": "史料考据"})
    assert "史料考据（bot）: 史实无误" in out
    assert "historian（bot）" not in out


def test_rooms_passes_chinese_names_into_runtime(tmp_path) -> None:
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "reader"])
    fake = _RecordingRuntime("pass")
    store.say(room.room_id, "@historian 聊聊", runtime=fake)
    assert fake.calls
    assert fake.calls[0]["names"]["historian"] == "史料考据"
    assert fake.calls[0]["room_id"] == room.room_id
    store.close()
    cs.close()


# ---------------------------------------------------------------- 3. @中文名

def test_resolve_mentions_accepts_chinese_aliases() -> None:
    handles = ["historian", "lit-editor", "reader"]
    aliases = {"史料考据": "historian", "文学编辑": "lit-editor", "读者视角": "reader"}
    assert discussion.resolve_mentions(
        "@史料考据 你怎么看", handles, aliases=aliases
    ) == ("historian",)
    assert discussion.resolve_mentions(
        "@史料考据 @读者视角 一起看", handles, aliases=aliases
    ) == ("historian", "reader")
    assert discussion.resolve_mentions("大家觉得呢", handles, aliases=aliases) == tuple(handles)
    assert discussion.resolve_mentions("@不存在的人", handles, aliases=aliases) == ()


def test_chinese_mention_wakes_the_right_bot(tmp_path) -> None:
    """端到端：用户 @中文名，不该静默无人应答。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    fake = _RecordingRuntime()
    out = store.say(room.room_id, "@史料考据 这段官职对不对", runtime=fake)
    assert out["woken"] == ["historian"]
    assert [c["bot_id"] for c in fake.calls] == ["historian"]
    store.close()
    cs.close()


# ---------------------------------------------------------------- 4. 显式 pass

def test_is_pass_recognizes_short_opt_out_only() -> None:
    assert discussion.is_pass("pass")
    assert discussion.is_pass("我 pass。")
    assert discussion.is_pass("这题我不发言")
    assert discussion.is_pass("跳过")
    assert not discussion.is_pass("")
    assert not discussion.is_pass("我不接受这个观点，理由有三条……")
    assert not discussion.is_pass(
        "我不同意史料考据的结论，从仓场则例看还有另一种可能，需要再核一遍出处"
    )


def test_explicit_pass_is_not_persisted(tmp_path) -> None:
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "reader"])
    out = store.say(room.room_id, "大家聊聊", runtime=_RecordingRuntime("我 pass。"))
    assert out["passed"] == ["historian", "reader"]
    assert out["replies"] == []
    assert [m["author_role"] for m in cs.messages(room.room_id)] == ["user"]
    store.close()
    cs.close()


# ---------------------------------------------------------------- 5. 跨群隔离

def test_engine_is_isolated_per_room(tmp_path) -> None:
    rt = _runtime(tmp_path)
    a = rt.engine("historian", room_id="room-a")
    b = rt.engine("historian", room_id="room-b")
    assert a is not b
    assert a.messages is not b.messages
    a.messages.append({"role": "user", "content": "A 群才有的秘密"})
    assert all("A 群才有的秘密" not in str(m.get("content")) for m in b.messages)
    # 旧的无 room_id 调用仍落进默认桶，不报错
    assert rt.engine("historian") is rt.engine("historian")
    rt.close()


def test_speak_does_not_leak_between_rooms(tmp_path) -> None:
    rt = _runtime(tmp_path)
    rt.speak("historian", "A 群的话题", history=[], room_id="room-a", round_index=1)
    eng_b = rt.engine("historian", room_id="room-b")
    joined = " ".join(str(m.get("content") or "") for m in eng_b.messages)
    assert "A 群的话题" not in joined
    rt.close()


# ---------------------------------------------------------------- 6. 上下文去重

def test_current_utterance_not_duplicated_in_history(tmp_path) -> None:
    """当前这句单独进 prompt，群历史里不该再出现一次。"""
    cs, store = _pair(tmp_path)
    room = store.create_room("话题", ["historian", "reader"])
    fake = _RecordingRuntime("pass")
    store.say(room.room_id, "这句是全新的", runtime=fake)
    assert fake.calls
    text, hist = fake.calls[0]["text"], fake.calls[0]["history"]
    assert text == "这句是全新的"
    assert hist == [], "空群首次发言，排除当前句后群历史应为空"
    store.close()
    cs.close()


def test_build_prompt_round2_does_not_repeat_user_text() -> None:
    history = [{"author": "historian", "author_role": "bot", "text": "前文观点"}]
    p1 = build_prompt("用户原话", history, round_index=1)
    assert p1.count("用户原话") == 1
    assert p1.count("前文观点") == 1
    p2 = build_prompt("用户原话", history, round_index=2)
    assert "用户原话" not in p2, "第 2 轮靠群历史接话，不重复用户原话"
    assert "第 2 轮接话" in p2
    assert p2.count("前文观点") == 1


def test_engine_messages_not_accumulate_history_copies(tmp_path) -> None:
    """每轮开始前清空 engine.messages，群历史只有一份，不会越滚越大 / 复读。"""
    rt = _runtime(tmp_path)
    history = [{"author": "historian", "author_role": "bot", "text": "上一轮说过的话"}]
    rt.speak("historian", "第一句", history=history, room_id="r1", round_index=1)
    rt.speak("historian", "第二句", history=history, room_id="r1", round_index=2)
    eng = rt.engine("historian", room_id="r1")
    joined = " ".join(str(m.get("content") or "") for m in eng.messages)
    assert "第一句" not in joined, "上一轮的 prompt 副本不应留到下一轮"
    assert "第 2 轮接话" in joined
    assert joined.count("上一轮说过的话") == 1, "群历史只应有一份，不累积副本"
    rt.close()
