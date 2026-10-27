"""QQ 好友通道（私聊版）的离线测试：纯函数 + fake WebSocket 驱动全流程。

不起网络、不碰 NapCat：FakeWS 收到 action 就回 status=ok，
事件用 push() 灌进去，断言发出去的消息。
"""

from __future__ import annotations

import asyncio
import json

from deskmate.connectors.qq_friend import (
    QQFriendChannel,
    emote_image_path,
    split_emotes,
    message_segments_to_text,
    parse_private_message,
    split_for_chat,
)


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        payload = json.loads(data)
        self.sent.append(payload)
        if "echo" in payload:
            self._queue.put_nowait(json.dumps({"status": "ok", "echo": payload["echo"]}))

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)

    def push(self, obj: dict) -> None:
        self._queue.put_nowait(json.dumps(obj, ensure_ascii=False))

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


def _dm(user_id: int, text: str, message_id: int = 1) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "self_id": 20002,
        "message_id": message_id,
        "sender": {"nickname": "萧哥"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def _ready(value):
    return value


def _run(scenario):
    return asyncio.run(scenario())


# -- 纯函数 --


def test_split_for_chat_sentences():
    assert split_for_chat("在的。我在吃白饭。今天三碗。") == ["在的。", "我在吃白饭。", "今天三碗。"]
    assert split_for_chat("") == []
    assert split_for_chat("   ") == []


def test_split_for_chat_merges_short_pieces():
    assert split_for_chat("我在吃白饭。嗯。") == ["我在吃白饭。嗯。"]


def test_split_for_chat_caps_parts():
    long_reply = "第一句话说长一点。第二句话也说长一点。第三句话同样长。第四句话还行。"
    parts = split_for_chat(long_reply, max_parts=2)
    assert len(parts) == 2
    assert "".join(parts) == long_reply


def test_message_segments_to_text():
    segments = [
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"file": "x.png"}},
        {"type": "face", "data": {"id": "1"}},
    ]
    assert message_segments_to_text(segments) == "看这个[图片][表情]"


def test_parse_private_message_ok():
    inbound = parse_private_message(_dm(10001, "小桔在吗？"))
    assert inbound is not None
    assert inbound.user_id == "10001"
    assert inbound.text == "小桔在吗？"
    assert inbound.nickname == "萧哥"


def test_parse_private_message_ignores_group_and_self():
    group = dict(_dm(10001, "群消息"), message_type="group")
    assert parse_private_message(group) is None
    assert parse_private_message(_dm(20002, "自己发的")) is None
    assert parse_private_message({"post_type": "meta_event"}) is None


# -- 全流程（fake WS） --


def test_private_flow_debounces_and_splits_reply():
    async def scenario():
        ws = FakeWS()
        calls: list[str] = []

        async def run_turn(text: str) -> str:
            calls.append(text)
            return "在的。我在吃白饭。今天三碗。"

        channel = QQFriendChannel(
            run_turn,
            owners=["10001"],
            quiet_seconds=0.05,
            max_wait_seconds=1.0,
            reply_delay=(0.0, 0.0),
            poll_seconds=0.01,
            connector=lambda: _ready(ws),
        )
        task = asyncio.create_task(channel.serve())
        await asyncio.sleep(0.05)
        ws.push(_dm(10001, "小桔在吗？", message_id=1))
        await asyncio.sleep(0.02)
        ws.push(_dm(10001, "吃了吗", message_id=2))
        await asyncio.sleep(0.4)
        channel.stop()
        await asyncio.wait_for(task, timeout=2.0)
        return ws, calls

    ws, calls = _run(scenario)
    assert calls == ["小桔在吗？\n吃了吗"], calls
    sends = [p for p in ws.sent if p.get("action") == "send_private_msg"]
    assert len(sends) == 3, sends
    assert all(s["params"]["user_id"] == 10001 for s in sends)
    texts = [s["params"]["message"][0]["data"]["text"] for s in sends]
    assert "".join(texts) == "在的。我在吃白饭。今天三碗。"


def test_stranger_is_ignored():
    async def scenario():
        ws = FakeWS()
        calls: list[str] = []

        async def run_turn(text: str) -> str:
            calls.append(text)
            return "不该出现"

        channel = QQFriendChannel(
            run_turn,
            owners=["10001"],
            quiet_seconds=0.02,
            reply_delay=(0.0, 0.0),
            poll_seconds=0.01,
            connector=lambda: _ready(ws),
        )
        task = asyncio.create_task(channel.serve())
        await asyncio.sleep(0.05)
        ws.push(_dm(99999, "我是陌生人"))
        await asyncio.sleep(0.2)
        channel.stop()
        await asyncio.wait_for(task, timeout=2.0)
        return ws, calls

    ws, calls = _run(scenario)
    assert calls == []
    assert [p for p in ws.sent if p.get("action") == "send_private_msg"] == []


def test_empty_owners_means_nobody():
    async def scenario():
        ws = FakeWS()
        calls: list[str] = []

        async def run_turn(text: str) -> str:
            calls.append(text)
            return "不该出现"

        channel = QQFriendChannel(
            run_turn,
            owners=[],
            quiet_seconds=0.02,
            reply_delay=(0.0, 0.0),
            poll_seconds=0.01,
            connector=lambda: _ready(ws),
        )
        task = asyncio.create_task(channel.serve())
        await asyncio.sleep(0.05)
        ws.push(_dm(10001, "小桔在吗？"))
        await asyncio.sleep(0.2)
        channel.stop()
        await asyncio.wait_for(task, timeout=2.0)
        return calls

    assert _run(scenario) == []


# -- 配置 --


def test_build_qq_channel_from_config(tmp_path, monkeypatch):
    from deskmate.connectors.qq_friend import build_qq_channel, qq_channel_enabled

    monkeypatch.delenv("QQ_FRIEND_ENABLE", raising=False)
    monkeypatch.delenv("QQ_FRIEND_WS_URL", raising=False)
    monkeypatch.delenv("QQ_FRIEND_OWNERS", raising=False)
    missing = tmp_path / "nope.json"
    assert build_qq_channel(None, config_path=missing) is None
    assert qq_channel_enabled(missing) is False

    cfg = tmp_path / "qq_friend.json"
    cfg.write_text(
        json.dumps(
            {
                "ws_url": "ws://127.0.0.1:3001",
                "access_token": "t",
                "owners": ["346102848"],
            }
        ),
        encoding="utf-8",
    )
    channel = build_qq_channel(None, config_path=cfg)
    assert channel is not None
    assert qq_channel_enabled(cfg) is True
    assert channel.running is False


def test_split_for_chat_english_period():
    text = "Let me check that file. It should be done soon. Ping me later."
    parts = split_for_chat(text, max_parts=4)
    assert parts == [
        "Let me check that file.",
        "It should be done soon.",
        "Ping me later.",
    ], parts
    assert split_for_chat("version 3.5 is out") == ["version 3.5 is out"]


# -- 表情 --


def test_split_emotes_both_syntaxes():
    text = "在的 ![暗中观察](emote:peek) 我看看 [[emote:tea]]"
    assert split_emotes(text) == [
        ("text", "在的 "),
        ("emote", "peek"),
        ("text", " 我看看 "),
        ("emote", "tea"),
    ]
    assert split_emotes("纯文字") == [("text", "纯文字")]
    assert split_emotes("![x](emote:peek)") == [("emote", "peek")]


def test_emote_image_path_resolves_repo_asset():
    path = emote_image_path("peek")
    assert path is not None and path.is_file()
    assert path.suffix == ".webp"
    assert emote_image_path("不存在的表情") is None


def test_reply_sends_emote_as_image():
    async def scenario():
        ws = FakeWS()

        async def run_turn(text):
            return ""

        channel = QQFriendChannel(run_turn, owners=["10001"], reply_delay=(0.0, 0.0),
                                 poll_seconds=0.01, connector=lambda: _ready(ws))
        channel._ws = ws
        listen = asyncio.create_task(channel._listen())
        await asyncio.sleep(0.01)
        await channel.send_reply("10001", "给你看 ![暗中观察](emote:peek) 就是这样")
        listen.cancel()
        return ws

    ws = _run(scenario)
    sends = [p for p in ws.sent if p.get("action") == "send_private_msg"]
    kinds = [s["params"]["message"][0]["type"] for s in sends]
    assert "image" in kinds, sends
    assert "text" in kinds, sends
    for s in sends:
        seg = s["params"]["message"][0]
        if seg["type"] == "text":
            assert "emote:" not in seg["data"]["text"]


def test_emote_sent_as_base64():
    async def scenario():
        ws = FakeWS()

        async def run_turn(text):
            return ""

        channel = QQFriendChannel(run_turn, owners=["10001"], reply_delay=(0.0, 0.0),
                                 poll_seconds=0.01, connector=lambda: _ready(ws))
        channel._ws = ws
        listen = asyncio.create_task(channel._listen())
        await asyncio.sleep(0.01)
        await channel.send_reply("10001", "![暗中观察](emote:peek)")
        listen.cancel()
        return ws

    ws = _run(scenario)
    images = [
        s["params"]["message"][0]["data"]["file"]
        for s in ws.sent
        if s.get("action") == "send_private_msg"
        and s["params"]["message"][0]["type"] == "image"
    ]
    assert images, ws.sent
    assert images[0].startswith("base64://")
    assert len(images[0]) > 1000
