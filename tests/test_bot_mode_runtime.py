"""Bot Mode 阶段 2 回归 —— 真 Agent / 自然语言读稿 / 群随建随撤 / 不出纪要。

萧哥 11-06 三个决策：
1. 先做新 bot、建群测试，成功后圆桌才退役；「先读《夜不收》」必须是自然语言，
   bot 自己决定去读，不做「阅读全书」工具按钮。
2. 7 位圆桌专家此前只是人设卡（coordinator 用 chat_stream 纯 prompt 注入），
   必须验证它们能起真 Agent（engine + 工具 + 自主决策）。
3. 不要纪要。

对应 docs/bot-mode-redesign.md 阶段 2。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from deskmate.bots import reading, rooms, roster


# ---------------------------------------------------------------- 书稿只读层

@pytest.fixture()
def shelf_dir(tmp_path: Path) -> Path:
    """临时书库：目录名带编号前缀，模拟 ~/Documents/已出版/16.夜不收。"""
    book = tmp_path / "16.夜不收"
    book.mkdir()
    (book / "第三章 烧仓.txt").write_text(
        "叶鸣秋推开柴门，见董文正立在廊下，手里攥着一封未拆的信。", encoding="utf-8"
    )
    (book / "第四章 夜行.md").write_text("槐树巷的更鼓敲过三下。", encoding="utf-8")
    (book / "脑洞汇总.md").write_text("段 1353：烧仓前提不成立。", encoding="utf-8")
    (tmp_path / "17.明朝的裂变").mkdir()
    (tmp_path / "17.明朝的裂变" / "序.txt").write_text("序章。", encoding="utf-8")
    return tmp_path


def test_parse_book_title_from_natural_language() -> None:
    """「先读《夜不收》，然后说说烧仓」不是表单，书名从人话里抽。"""
    assert reading.parse_book_title("先读《夜不收》，然后说说烧仓还能怎么写") == "夜不收"
    # 不带书名号时对照书架已知书名匹配（仍是人话，不是表单）
    assert reading.parse_book_title("大家先把夜不收读一遍", known=["夜不收"]) == "夜不收"
    assert reading.parse_book_title("随便聊聊", known=["夜不收"]) is None


def test_shelf_read_is_readonly_and_guarded(shelf_dir: Path) -> None:
    shelf = reading.BookShelf(shelf_dir, max_chars=8000)
    assert "夜不收" in shelf.list_books()
    text = shelf.read("《夜不收》", chapter="烧仓")
    assert "叶鸣秋" in text and "第三章" in text
    # 只读：调用前后原稿逐字节不变，目录里也没多出文件
    p = shelf_dir / "16.夜不收" / "第三章 烧仓.txt"
    assert p.read_text(encoding="utf-8") == "叶鸣秋推开柴门，见董文正立在廊下，手里攥着一封未拆的信。"
    assert sorted(x.name for x in (shelf_dir / "16.夜不收").iterdir()) == [
        "第三章 烧仓.txt", "第四章 夜行.md", "脑洞汇总.md"
    ]
    # 越界 / 不存在一律空，不抛异常、不猜路径
    assert shelf.read("../../etc/passwd") == ""
    assert shelf.read("不存在的书") == ""


def test_reading_tools_expose_only_readonly_functions(shelf_dir: Path) -> None:
    shelf = reading.BookShelf(shelf_dir)
    tools = reading.make_reading_tools(shelf)
    assert {f.__name__ for f in tools} == {"list_books", "read_book"}
    # 工具函数必须是可注册进官方 ToolRegistry 的普通 callable（有签名 + docstring）
    for f in tools:
        assert inspect.signature(f)
        assert f.__doc__


# ---------------------------------------------------------------- 真 Agent 运行时

class _ScriptedProvider:
    """离线 provider：先让模型自主发起一次 read_book，再给出最终发言。

    用来证明 bot 是**真 Agent**（模型自己决定调工具），而不是人设卡或外部解析规则。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.step = 0

    def complete(self, *, model, messages, tools=None, **settings):
        from coworker.providers.base import AssistantTurn, ToolCall

        self.calls.append({"model": model, "messages": messages, "tools": tools})
        self.step += 1
        if self.step == 1:
            return AssistantTurn(
                text=None,
                tool_calls=[ToolCall(id="c1", name="read_book", arguments={"title": "《夜不收》"})],
            )
        return AssistantTurn(text="我读了《夜不收》，烧仓这段的底本建议从仓场则例切入。")

    def capabilities(self, model):
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def stream(self, *, model, messages, tools=None, **settings):
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def test_bot_engine_is_real_agent_with_readonly_tools(shelf_dir: Path, tmp_path: Path) -> None:
    """7 位专家起的是官方 TurnEngine，工具面只读，provider 不跟随主对话网关。"""
    from deskmate.bots.runtime import BotRuntime

    rt = BotRuntime(
        books_root=shelf_dir,
        workspace_root=tmp_path / "ws",
        provider=_ScriptedProvider(),
        model="test-model",
    )
    for bot_id in roster.BOT_ORDER:
        eng = rt.engine(bot_id)
        names = set(eng.registry.names())
        assert "read_book" in names and "list_books" in names
        forbidden = {
            "write_file", "apply_patch", "apply_unified_diff", "replace_in_file",
            "run_shell", "shell_task_kill", "save_skill", "a2a_call", "web_fetch",
        }
        assert not (names & forbidden), f"{bot_id} 暴露了非只读工具: {names & forbidden}"
    rt.close()


def test_bot_decides_to_read_by_itself(shelf_dir: Path, tmp_path: Path) -> None:
    """说一句人话 → 模型自主调 read_book → 读到的正文进入上下文 → 署名发言。"""
    from deskmate.bots.runtime import BotRuntime

    prov = _ScriptedProvider()
    rt = BotRuntime(
        books_root=shelf_dir, workspace_root=tmp_path / "ws",
        provider=prov, model="test-model",
    )
    reply = rt.speak("historian", "先读《夜不收》，然后说说烧仓这段还能怎么写", history=[])
    assert "《夜不收》" in reply
    assert prov.step == 2, "应当发生一次自主工具调用 + 一次最终发言"
    tool_msgs = [
        m for turn in prov.calls for m in turn["messages"] if m.get("role") == "tool"
    ]
    assert any("叶鸣秋" in str(m.get("content")) for m in tool_msgs), "书稿正文没进模型上下文"
    assert prov.calls[0]["tools"], "engine 必须把只读工具 schema 交给模型决定"
    rt.close()


def test_bot_runtime_uses_paid_channel_not_main_gateway(tmp_path: Path, monkeypatch) -> None:
    """默认 provider 必须是付费通道组，不能裸连 opencode-go（400 MissingSessionID）。

    conftest 把 COWORKER_STATE_DIR 指到 tmp，真机 SecretStore 读不到；这里注入一条
    假凭证，验证的是「走 llm_channels 付费组路由」这条路径本身。
    """
    from deskmate.bots.runtime import default_provider
    from deskmate.llm_channels import Channel

    monkeypatch.setattr(
        "deskmate.llm_channels.paid_channels",
        lambda: [Channel("ark-agent-plan-cn", "deepseek-v4-flash")],
    )
    monkeypatch.setattr(
        "deskmate.memory.llm._creds_for",
        lambda provider, model: ("fake-key", "https://ark.example.test/api/v3", model),
    )
    p = default_provider()
    base = str(getattr(p, "_base_url", "") or "")
    assert base and "opencode.ai" not in base, f"误用主对话网关: {base}"
    assert "ark.example.test" in base


# ---------------------------------------------------------------- 群：随建随撤 / 无表单 / 无纪要

class _FakeRuntime:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, bot_id: str, text: str, *, history: list[dict]) -> str:
        self.spoken.append(bot_id)
        return f"[{bot_id}] 我看了书，说一句。"


@pytest.fixture()
def store(shelf_dir: Path, tmp_path: Path):
    from coworker.teams.chat import ChatStore

    cs = ChatStore(tmp_path / "chat.db")
    s = rooms.RoomStore(tmp_path, chat_store=cs)
    yield s
    s.close()
    cs.close()


def test_say_wakes_only_mentioned_bot_and_posts_signed_replies(store) -> None:
    fake = _FakeRuntime()
    room = store.create_room("烧仓怎么往下写", ["historian", "lit-editor", "reader"])
    out = store.say(room.room_id, "@historian 先读《夜不收》，说说烧仓", runtime=fake)
    assert fake.spoken == ["historian"], "@ 谁谁答，不该全员烧钱"
    assert len(out["replies"]) == 1
    msgs = store.chat_store.messages(room.room_id)
    assert msgs[0]["author_role"] == "user"
    assert msgs[-1]["author"] == "historian" and msgs[-1]["author_role"] == "bot"
    assert "《夜不收》" in msgs[0]["text"]


def test_no_summary_is_ever_generated(store) -> None:
    """萧哥明确不要纪要：任何一条群消息都不能是主持/总结/纪要。"""
    fake = _FakeRuntime()
    room = store.create_room("下半部走向", ["historian", "lit-editor"])
    store.say(room.room_id, "大家聊聊接下来怎么写", runtime=fake)
    msgs = store.chat_store.messages(room.room_id)
    assert all(m["author_role"] in ("user", "bot") for m in msgs)
    assert not any(
        kw in m["text"] for m in msgs for kw in ("《圆桌纪要》", "纪要", "本轮总结", "主持人")
    )
    assert not any(m["author"] in ("host", "moderator", "summary") for m in msgs)


def test_say_is_bounded(store) -> None:
    fake = _FakeRuntime()
    room = store.create_room("话题", ["historian", "lit-editor", "reader", "proofreader"])
    out = store.say(room.room_id, "大家都说说", runtime=fake)
    assert len(out["replies"]) <= store.limits.max_messages
    assert len(fake.spoken) <= store.limits.max_members


def test_disband_keeps_history_and_room_id_never_reused(store) -> None:
    room = store.create_room("临时话题", ["historian", "reader"])
    store.post(room.room_id, "user", "先读《夜不收》")
    first = store.disband_room(room.room_id)
    assert first["idempotent"] is False
    assert store.get_room(room.room_id) is None
    assert store.get_room(room.room_id, include_disbanded=True) is not None
    assert store.chat_store.messages(room.room_id), "撤群不得删消息"
    new = store.create_room("临时话题", ["historian", "reader"])
    assert new.room_id != room.room_id
    with pytest.raises(ValueError):
        store.say(room.room_id, "还在吗", runtime=_FakeRuntime())


def test_no_form_fields_on_new_api(store) -> None:
    """硬锁：新 API 绝不把旧圆桌的表单字段套回来。"""
    from deskmate.bots.runtime import BotRuntime

    banned = {"manuscript_path", "chapter_range", "direction", "criteria", "peers", "focus", "folder"}
    for fn in (rooms.RoomStore.create_room, rooms.RoomStore.say, BotRuntime.speak):
        params = set(inspect.signature(fn).parameters)
        assert not (params & banned), f"{fn.__qualname__} 混入了表单字段 {params & banned}"
    src = (Path(rooms.__file__).parent / "web.py").read_text(encoding="utf-8")
    assert not any(k in src for k in ("manuscript_path", "chapter_range", "direction", "criteria"))
