"""Bot Mode 阶段 1 骨架回归：bot 常驻 / 群可建可撤 / 自然语言 / 有界护栏。

形态依据（2026-11-06 核实，见 docs/bot-mode-redesign.md）：
- Grok Bot：bot 是有名字、有持久状态的同事；多 bot 可放进同一群聊自主协商。
- Hermes Bot Mode（本地 ~/.hermes/hermes-agent 源码）：bot = profile（零新存储），
  groups.create / groups.disband 一等公民 RPC，DISCUSSION 上限 6 成员 / 3 轮 / 10 消息，
  不 @ = 全员，@ 谁谁答，bot 可 pass，群解散留墓碑不删消息。

本组用例锁住「不把旧圆桌那套表单/人设/点开始套到新 bot 上」。
"""
from __future__ import annotations

import inspect

import pytest

from deskmate.bots import discussion, rooms, roster


# ---------------------------------------------------------------- roster

def test_roster_maps_registry_entries_without_new_storage():
    """bot = 现成 persona profile；只取 roundtable:true，按稳定序，零新名册存储。"""
    entries = [
        {"id": "reader", "name": "读者视角", "roundtable": True,
         "system_prompt": "R", "tagline": "结构"},
        {"id": "outsider", "name": "无关角色", "roundtable": False,
         "system_prompt": "X"},
        {"id": "historian", "name": "史料考据", "roundtable": True,
         "system_prompt": "H", "roundtable_role": "史实"},
    ]
    bots = roster.bots_from_entries(entries)
    assert [b.id for b in bots] == ["historian", "reader"]
    assert bots[0].name == "史料考据"
    assert bots[0].role == "史实"
    assert bots[0].system_prompt == "H"


def test_roster_entry_is_pure(tmp_path):
    """映射是纯函数：不落任何文件（bot 身份来自 registry，不是新建存储层）。"""
    before = sorted(p.name for p in tmp_path.iterdir())
    roster.bots_from_entries([{"id": "reader", "name": "读者", "roundtable": True}])
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ---------------------------------------------------------------- rooms

def test_room_create_list_disband_and_keep_history(tmp_path):
    """群是临时协调面：可建、可列、可撤；撤群只打墓碑，消息历史不删。"""
    store = rooms.RoomStore(tmp_path)
    room = store.create_room("烧仓还能怎么烧", ["historian", "lit-editor", "reader"])
    assert room.topic == "烧仓还能怎么烧"
    assert room.bot_ids == ["historian", "lit-editor", "reader"]
    assert room.disbanded_at is None

    group = store.chat_store.get_group(room.room_id)
    assert group is not None
    # 关键：成员 persona 必须真填（旧圆桌 11 个僵尸群的 persona 全是空串）
    assert [m["name"] for m in group["members"]] == room.bot_ids
    assert all(m["persona"] for m in group["members"])

    assert [r.room_id for r in store.list_rooms()] == [room.room_id]

    store.post(room.room_id, "user", "我卡在烧仓的动机上了，你们怎么看")
    assert len(store.chat_store.messages(room.room_id)) == 1

    first = store.disband_room(room.room_id)
    assert first["idempotent"] is False
    assert first["disbanded_at"] is not None
    assert store.list_rooms() == []
    assert [r.room_id for r in store.list_rooms(include_disbanded=True)] == [room.room_id]
    assert len(store.chat_store.messages(room.room_id)) == 1
    assert store.disband_room(room.room_id)["idempotent"] is True


def test_room_rejects_out_of_range_members(tmp_path):
    """Hermes 同款硬边界：2~6 个 bot。"""
    store = rooms.RoomStore(tmp_path)
    with pytest.raises(ValueError, match="成员"):
        store.create_room("一个人聊不起来", ["historian"])
    with pytest.raises(ValueError, match="成员"):
        store.create_room("人太多", [f"bot{i}" for i in range(7)])


def test_room_rejects_empty_topic_and_unknown_bot(tmp_path):
    store = rooms.RoomStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_room("   ", ["historian", "reader"])
    with pytest.raises(ValueError, match="bot"):
        store.create_room("有话题", ["historian", "not-a-bot"])


def test_create_room_has_no_form_fields():
    """不许把圆桌那套表单参数套回来：没有稿件路径 / 章节范围 / 讨论方向 / 轮次。"""
    params = inspect.signature(rooms.RoomStore.create_room).parameters
    for forbidden in ("manuscript_path", "chapter_range", "direction",
                      "peers", "max_rounds", "context", "focus"):
        assert forbidden not in params


def test_post_after_disband_is_refused(tmp_path):
    store = rooms.RoomStore(tmp_path)
    room = store.create_room("旧话题", ["historian", "reader"])
    store.disband_room(room.room_id)
    with pytest.raises(ValueError, match="解散"):
        store.post(room.room_id, "user", "还在吗")


# ---------------------------------------------------------------- discussion

def test_mentions_default_all_and_targeted():
    handles = ["historian", "lit-editor", "reader"]
    assert discussion.resolve_mentions("大家觉得呢", handles) == tuple(handles)
    assert discussion.resolve_mentions("@historian 你怎么看", handles) == ("historian",)
    assert discussion.resolve_mentions("@all 集合", handles) == tuple(handles)
    assert discussion.resolve_mentions("@everyone 集合", handles) == tuple(handles)
    assert discussion.resolve_mentions("@nobody 有人吗", handles) == ()


def test_discussion_hard_bounds():
    limits = discussion.DiscussionLimits()
    assert (limits.max_members, limits.max_rounds, limits.max_messages) == (6, 3, 10)
    assert discussion.bound_reason(1, 3, limits) is None
    assert discussion.bound_reason(3, 2, limits) == "max_rounds"
    assert discussion.bound_reason(1, 11, limits) == "max_messages"


def test_discussion_takes_only_natural_language():
    """发言入参只有 text / author；没有表单字段。"""
    params = inspect.signature(discussion.resolve_mentions).parameters
    for forbidden in ("topic", "path", "chapter_range", "direction", "criteria"):
        assert forbidden not in params
