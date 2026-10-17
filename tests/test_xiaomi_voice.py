"""小爱音箱语音通道（路线 A：MiService 云端协议，零刷机）测试。

覆盖：
- AskTracker 去重/基线语义（纯逻辑，无 IO）：
  首轮只建基线不重放历史、同一 request_id 只放行一次、旧/等时间戳忽略、
  空提问推进水位但不放行、多问全放行且按时间序。
- VoiceChannel 循环（fake connector + fake 大脑）：
  新提问 → 大脑 → 播报；首轮基线不播报；大脑/播报/连接器异常均不杀循环；
  stop 事件能干净退出。
- 小爱音箱配置加载：缺省值、文件解析、损坏文件回退、路径约定。

设计约束（与 deskmate/connectors/aicircle.py 一致的工程范式）：
- 全部 fail-open：任何单点异常只记日志，不杀循环、不崩主服务。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from deskmate.connectors.xiaomi_speaker import (
    AskTracker,
    XiaomiSpeakerConnector,
    default_config_path,
    load_speaker_config,
)
from deskmate.connectors.xiaomi_voice import VoiceChannel, build_voice_channel


# ---------------------------------------------------------------------- helpers
def _ask(rid: str, ts: int, question: str, answer: str = "") -> dict:
    """归一化后的提问（连接器把 MiNA 原始消息压成这个形状）。"""
    return {
        "request_id": rid,
        "timestamp_ms": ts,
        "question": question,
        "answer": answer,
    }


class FakeConnector:
    """脚本化音箱连接器：按队列返回 latest_asks，记录 speak/stop 调用。

    脚本耗尽后回调 on_exhausted（测试里接 channel.stop）——不需要真实 sleep 轮转。
    """

    def __init__(self, script: list[list[dict]]):
        self._script = list(script)
        self.on_exhausted = None
        self.spoken: list[str] = []
        self.mutes: list[bool] = []
        self.played: list[str] = []
        self.stops = 0
        self.polls = 0
        self.events: list[tuple[str, str]] = []  # (kind, detail) 全局事件序
        self.status_queries = 0
        #: 播放状态探测的注入点（S12 真机实测：在播 status=1，空闲 status=0/3）
        self.play_status: dict = {"status": 0}

    async def latest_asks(self) -> list[dict]:
        self.polls += 1
        if self._script:
            return self._script.pop(0)
        if self.on_exhausted is not None:
            self.on_exhausted()
        return []

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        self.events.append(("speak", text))
        return True

    async def mute_xiaoai(self) -> bool:
        self.mutes.append(True)
        self.events.append(("mute", ""))
        return True

    async def get_player_status(self) -> dict:
        """播放状态（S12 真机实测：在播 status=1；空闲 0/3）。"""
        self.status_queries += 1
        return dict(self.play_status)

    async def play_url(self, url: str) -> bool:
        self.played.append(url)
        return True

    async def stop_playback(self) -> bool:
        self.stops += 1
        return True


class FlakyConnector(FakeConnector):
    """第 2 次轮询抛异常（模拟网络故障），其余行为同 FakeConnector。"""

    async def latest_asks(self) -> list[dict]:
        self.polls += 1
        if self.polls == 2:
            raise RuntimeError("network down")
        if self._script:
            return self._script.pop(0)
        if self.on_exhausted is not None:
            self.on_exhausted()
        return []


def _brain(replies: dict[str, str] | None = None, default: str = "默认回复"):
    """fake 大脑：按提问文本查表回复；缺省返回 default。"""

    async def run_turn(text: str) -> str:
        if replies and text in replies:
            return replies[text]
        return default

    return run_turn


def _channel(
    script: list[list[dict]],
    run_turn=None,
    connector_cls=FakeConnector,
    *,
    thinking_filler: str = "",
    merge_window: float = 0.0,
):
    """装配 通道+假连接器（脚本耗尽自动停），返回 (channel, connector)。

    thinking_filler / merge_window 透传：filler 与聚合相关测试显式传值；
    其余测试缺省 "" / 0.0（快速路径，逐问立即处理）。注意 merge_window
    必须在构造时透传——VoiceChannel 内部的 _AskAggregator 在 __init__
    时就固定了窗口值，事后设 channel._merge_window 字段无效（踩坑）。
    """
    channel = VoiceChannel(
        connector=None,
        run_turn=run_turn or _brain(),
        poll_interval=0.1,
        thinking_filler=thinking_filler,
        merge_window=merge_window,
    )
    conn = connector_cls(script)
    conn.on_exhausted = channel.stop
    channel._connector = conn
    return channel, conn


# ---------------------------------------------------------------------- AskTracker
class TestAskTracker:
    def test_first_poll_only_establishes_baseline(self):
        """启动前的历史提问不重放——否则小桔会对几天前的旧问题突然作答。"""
        t = AskTracker()
        history = [_ask("r1", 100, "旧问题一"), _ask("r2", 200, "旧问题二")]
        assert t.fresh(history) == []
        assert t.fresh(history) == []  # 再次轮询同一批也不重放

    def test_new_question_passes_exactly_once(self):
        t = AskTracker()
        t.fresh([_ask("r1", 100, "旧问题")])
        new = [_ask("r1", 100, "旧问题"), _ask("r2", 200, "小桔你好")]
        assert [a["question"] for a in t.fresh(new)] == ["小桔你好"]
        assert t.fresh(new) == []  # 同一 request_id 不重复放行

    def test_older_or_equal_timestamp_ignored(self):
        t = AskTracker()
        t.fresh([_ask("r1", 100, "旧问题")])
        assert t.fresh([_ask("r0", 50, "更旧")]) == []
        assert t.fresh([_ask("r1", 100, "旧问题")]) == []

    def test_empty_question_advances_watermark_without_speaking(self):
        """无文本提问（误触发）推进水位，防止下一轮把它当新问。"""
        t = AskTracker()
        t.fresh([_ask("r1", 100, "旧问题")])
        assert t.fresh([_ask("r2", 200, "  ")]) == []
        assert t.fresh([_ask("r3", 150, "夹在中间的提问")]) == []

    def test_multiple_new_asks_all_returned_in_time_order(self):
        t = AskTracker()
        t.fresh([_ask("r1", 100, "旧问题")])
        batch = [_ask("r3", 300, "问题三"), _ask("r2", 200, "问题二")]
        got = [a["question"] for a in t.fresh(batch)]
        assert got == ["问题二", "问题三"]

    def test_same_timestamp_different_request_id_passes(self):
        t = AskTracker()
        t.fresh([_ask("r1", 100, "旧问题")])
        got = t.fresh([_ask("r2", 100, "同刻新问")])
        assert [a["question"] for a in got] == ["同刻新问"]


# ---------------------------------------------------------------------- VoiceChannel
class TestVoiceChannel:
    async def test_new_question_routed_to_brain_and_spoken(self):
        channel, conn = _channel(
            [
                [_ask("r1", 100, "启动前的旧问题")],  # 基线轮
                [_ask("r1", 100, "启动前的旧问题"), _ask("r2", 200, "小桔你好")],
            ],
            run_turn=_brain({"小桔你好": "你好呀萧哥"}),
        )
        await channel.run_forever()

        assert conn.spoken == ["你好呀萧哥"]
        assert len(conn.mutes) >= 2  # 发现即静音 + 播报前再静音（双保险，xiaogpt 同款）
        assert conn.polls == 3  # 基线轮 + 新问轮 + 脚本耗尽轮

    async def test_baseline_poll_never_speaks(self):
        channel, conn = _channel([[_ask("r1", 100, "旧问题")]])
        await channel.run_forever()
        assert conn.spoken == []

    async def test_brain_failure_does_not_kill_loop(self):
        """大脑抛异常：本轮静默，循环继续，下一问照常应答。"""

        async def flaky_brain(text: str) -> str:
            if text == "坏问题":
                raise RuntimeError("provider 429")
            return f"回复:{text}"

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "坏问题")],
                [_ask("r3", 300, "好问题")],
            ],
            run_turn=flaky_brain,
            merge_window=0.0,  # 关聚合，逐问独立处理（本测试意图：容错）
        )
        await channel.run_forever()
        assert conn.spoken == ["回复:好问题"]

    async def test_speak_failure_does_not_kill_loop(self):
        async def broken_speak(text: str) -> bool:
            raise RuntimeError("ubus timeout")

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题一")],
                [_ask("r3", 300, "问题二")],
            ],
        )
        conn.speak = broken_speak
        await channel.run_forever()  # 不抛即过
        assert conn.polls == 4  # 循环活到了脚本耗尽

    async def test_connector_failure_does_not_kill_loop(self):
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
            connector_cls=FlakyConnector,
        )
        await channel.run_forever()
        assert conn.spoken == ["默认回复"]

    async def test_empty_brain_reply_not_spoken(self):
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
            run_turn=_brain(default="   "),
        )
        await channel.run_forever()
        assert conn.spoken == []

    async def test_thinking_filler_spoken_before_brain_reply(self):
        """打断小爱后、大脑思考前，先发一句 filler 填补静默。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "小桔你好")],
            ],
            run_turn=_brain({"小桔你好": "你好呀萧哥"}),
            thinking_filler="让我想想",
        )
        await channel.run_forever()
        assert conn.spoken[0] == "让我想想"
        assert conn.spoken[1] == "你好呀萧哥"

    async def test_empty_thinking_filler_skips_fill(self):
        """filler 为空时不发任何提示（向后兼容）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
            thinking_filler="",
        )
        await channel.run_forever()
        assert conn.spoken == ["默认回复"]

    async def test_filler_failure_does_not_kill_loop(self):
        """filler 发送失败：不影响大脑思考和正式播报。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
            thinking_filler="让我想想",
        )
        original_speak = conn.speak

        async def broken_speak(text: str) -> bool:
            if text == "让我想想":
                raise RuntimeError("filler failed")
            return await original_speak(text)

        conn.speak = broken_speak
        await channel.run_forever()
        # filler 失败了，但正式回复仍然播出
        assert conn.spoken == ["默认回复"]

    async def test_stop_ends_loop(self):
        channel = VoiceChannel(
            connector=FakeConnector([]), run_turn=_brain(), poll_interval=0.1
        )
        channel.stop()
        await channel.run_forever()
        assert channel._connector.polls == 0  # stop 已设置，一轮都不跑


# ---------------------------------------------------------------------- config
class TestSpeakerConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = load_speaker_config(tmp_path / "nope.json")
        assert cfg["enabled"] is False  # 未配置 = 不启动（fail-closed）
        assert cfg["did"] == ""
        assert cfg["poll_interval"] == 1.5

    def test_parses_file(self, tmp_path):
        p = tmp_path / "mi_speaker.json"
        p.write_text(
            json.dumps({"enabled": True, "did": "小爱音箱", "poll_interval": 2.0}),
            encoding="utf-8",
        )
        cfg = load_speaker_config(p)
        assert cfg["enabled"] is True
        assert cfg["did"] == "小爱音箱"
        assert cfg["poll_interval"] == 2.0

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "mi_speaker.json"
        p.write_text("{ not json", encoding="utf-8")
        cfg = load_speaker_config(p)
        assert cfg["enabled"] is False

    def test_default_path_under_config_coworker(self):
        assert default_config_path().name == "mi_speaker.json"
        assert default_config_path().parent.name == "coworker"

    def test_build_channel_passes_speak_timeout(self):
        """speak_timeout（ubus 回执等待上限）须从配置透传到通道。"""
        channel = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000", "speak_timeout": 7},
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._speak_timeout == 7.0

    def test_build_channel_speak_timeout_default(self):
        """未配置 speak_timeout 时用默认 10s（覆盖 S12 服务端 40+ 秒回 3012）。"""
        channel = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000"},
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._speak_timeout == 10.0

    def test_build_channel_mute_on_playback_default_on(self):
        """播放状态探测默认开（抢答漏字元凶是记录延迟，探测不等记录）。"""
        channel = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000"},
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._mute_on_playback is True

    def test_build_channel_mute_on_playback_config_off(self):
        """显式 false 关探测（用户不愿误伤小爱放歌等场景时）。"""
        channel = build_voice_channel(
            _brain(),
            config={
                "enabled": True,
                "username": "13400000000",
                "mute_on_playback": False,
            },
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._mute_on_playback is False

    def test_build_channel_merge_window_passthrough(self):
        """merge_window（提问聚合窗口）须从配置透传；缺省 2.5s。"""
        ch1 = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000", "merge_window": 1.5},
            connector=FakeConnector([[]]),
        )
        assert ch1 is not None and ch1._merge_window == 1.5
        ch2 = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000"},
            connector=FakeConnector([[]]),
        )
        assert ch2 is not None and ch2._merge_window == 2.5

    def test_build_channel_passes_thinking_filler(self):
        """thinking_filler 从配置透传到通道。"""
        channel = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000", "thinking_filler": "嗯"},
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._thinking_filler == "嗯"

    def test_build_channel_thinking_filler_default(self):
        """未配置 thinking_filler 时缺省「让我想想」。"""
        channel = build_voice_channel(
            _brain(),
            config={"enabled": True, "username": "13400000000"},
            connector=FakeConnector([[]]),
        )
        assert channel is not None
        assert channel._thinking_filler == "让我想想"


# ---------------------------------------------------------------------- connector
class FakeMinaService:
    """假 MiNAService：记录调用，返回脚本化的 MiNA 原始数据。"""

    def __init__(self, devices=None, records=None, hardware="S12A"):
        self._devices = devices if devices is not None else []
        self._records = records if records is not None else []
        self._hardware = hardware
        self.tts_calls: list[tuple[str, str]] = []
        self.stop_calls: list[str] = []
        self.url_calls: list[tuple[str, str]] = []
        self.pause_calls: list[str] = []
        self.status_queries: list[str] = []
        self.play_status_override: dict | None = None

    async def device_list(self, master: int = 0):
        return self._devices

    async def get_conversations(self, deviceId: str, hardware: str, limit: int = 10):
        """userprofile conversation 接口的 fake（生产走 account 直连 HTTP）。"""
        self.last_hardware = hardware
        return self._records

    async def get_latest_ask(self, deviceId: str):
        return []  # nlp_result_get 路线：S12 老固件实测返 1001 空数据，已弃用

    async def player_get_status(self, deviceId: str):
        """播放状态查询。S12 真机实测：空闲 status=3；在播 status=1
        （xiaogpt 的 ==1 判断）。测试可经 play_status_override 注入。"""
        self.status_queries.append(deviceId)
        if self.play_status_override is not None:
            return self.play_status_override
        return {"status": 3, "volume": 40, "loop_type": 1}

    async def player_pause(self, deviceId: str):
        self.pause_calls.append(deviceId)
        return True

    async def text_to_speech(self, deviceId: str, text: str) -> bool:
        self.tts_calls.append((deviceId, text))
        return True

    async def player_stop(self, deviceId: str) -> bool:
        self.stop_calls.append(deviceId)
        return True

    async def play_by_url(self, deviceId: str, url: str, _type: int = 2) -> bool:
        self.url_calls.append((deviceId, url))
        return True


def _raw_conv(
    rid: str, ts: int, query: str, tts_text: str = "", *, answers: list | None = None
) -> dict:
    """userprofile conversation 记录的真形状（S12 实测 code=0 响应）。

    answers 缺省构一个 TTS answer；传 [] 表示命令类（如「退出」，无 answer）。
    """
    if answers is None:
        answers = (
            [{"type": "TTS", "tts": {"text": tts_text}, "illegalContent": False}]
            if tts_text
            else []
        )
    return {
        "bitSet": [0, 1, 1, 1, 1, 1],
        "answers": answers,
        "time": ts,
        "query": query,
        "requestId": rid,
        "illegalContent": False,
    }


class TestConnector:
    async def test_latest_asks_normalizes_conversation_records(self):
        svc = FakeMinaService(devices=[{"deviceID": "d1", "name": "小爱音箱", "hardware": "S12A"}], records=[_raw_conv("r1", 100, "小桔你好", "你好呀萧哥")])
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        await conn.start()
        asks = await conn.latest_asks()
        assert asks == [
            {
                "request_id": "r1",
                "timestamp_ms": 100,
                "question": "小桔你好",
                "answer": "你好呀萧哥",
            }
        ]

    async def test_latest_asks_filters_commands_and_music(self):
        # 「退出」无 answer → 命令，过滤；音乐播放 TTS+Audio 双 answer → 过滤
        svc = FakeMinaService(
            devices=[{"deviceID": "d1", "name": "小爱音箱", "hardware": "S12A"}],
            records=[
                _raw_conv("r-cmd", 300, "退出", answers=[]),
                _raw_conv(
                    "r-music",
                    200,
                    "播放音乐",
                    answers=[
                        {"type": "TTS", "tts": {"text": "好的"}},
                        {"type": "Audio", "audio": {"url": "x"}},
                    ],
                ),
                _raw_conv("r-ok", 100, "小桔在吗", "在呢萧哥"),
            ]
        )
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        await conn.start()
        asks = await conn.latest_asks()
        assert [a["request_id"] for a in asks] == ["r-ok"]

    async def test_latest_asks_handles_empty_and_malformed(self):
        svc = FakeMinaService(devices=[{"deviceID": "d1", "hardware": "S12A"}], records=[{"requestId": "r1", "time": 5, "query": "q"}, "not-a-dict"])
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        await conn.start()
        asks = await conn.latest_asks()
        assert asks == []

    async def test_speak_and_stop_delegate_to_service(self):
        svc = FakeMinaService()
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        assert await conn.speak("萧哥你好") is True
        assert await conn.stop_playback() is True
        assert await conn.play_url("https://example.com/a.mp3") is True
        assert svc.tts_calls == [("d1", "萧哥你好")]
        assert svc.stop_calls == ["d1"]
        assert svc.url_calls == [("d1", "https://example.com/a.mp3")]

    async def test_resolve_device_by_name(self):
        svc = FakeMinaService(
            devices=[{"deviceID": "111", "name": "小爱音箱", "miotDID": "222"}]
        )
        conn = XiaomiSpeakerConnector({"did": "小爱音箱"}, service=svc)
        assert await conn.start() == "111"

    async def test_resolve_device_by_miot_did(self):
        svc = FakeMinaService(
            devices=[{"deviceID": "111", "name": "小爱音箱", "miotDID": "222"}]
        )
        conn = XiaomiSpeakerConnector({"did": "222"}, service=svc)
        assert await conn.start() == "111"

    async def test_single_device_auto_selected(self):
        svc = FakeMinaService(devices=[{"deviceID": "111", "name": "小爱音箱"}])
        conn = XiaomiSpeakerConnector({"did": ""}, service=svc)
        assert await conn.start() == "111"

    async def test_multiple_devices_without_did_raises(self):
        svc = FakeMinaService(
            devices=[
                {"deviceID": "111", "name": "客厅小爱"},
                {"deviceID": "222", "name": "卧室小爱"},
            ]
        )
        conn = XiaomiSpeakerConnector({"did": ""}, service=svc)
        with pytest.raises(RuntimeError, match="多台设备"):
            await conn.start()

    async def test_unknown_did_raises(self):
        svc = FakeMinaService(devices=[{"deviceID": "111", "name": "小爱音箱"}])
        conn = XiaomiSpeakerConnector({"did": "不存在的设备"}, service=svc)
        with pytest.raises(RuntimeError, match="找不到设备"):
            await conn.start()

    async def test_start_reuses_persisted_token_without_login(self):
        """token 有效 → 不调 login。否则守护进程每次重启都要 OTP，无终端必失败。"""

        class FakeStore:
            async def load_token(self):
                return {"micoapi": ("ssecurity", "serviceToken")}

        class FakeAccount:
            def __init__(self):
                self.token = None
                self.token_store = FakeStore()
                self.login_calls: list[str] = []

            async def login(self, sid: str) -> bool:
                self.login_calls.append(sid)
                return True

        acct = FakeAccount()
        svc = FakeMinaService(devices=[{"deviceID": "111", "name": "小爱音箱"}])
        conn = XiaomiSpeakerConnector({"did": ""}, service=svc)
        conn._account = acct
        assert await conn.start() == "111"
        assert acct.login_calls == []
        assert acct.token["micoapi"] == ("ssecurity", "serviceToken")

    async def test_start_logs_in_when_token_missing_or_stale(self):
        """token 缺失 / 不含 micoapi → 走 login（首次或过期）。"""

        class FakeStore:
            def __init__(self, token):
                self._token = token

            async def load_token(self):
                return self._token

        class FakeAccount:
            def __init__(self, store_token):
                self.token = None
                self.token_store = FakeStore(store_token)
                self.login_calls: list[str] = []

            async def login(self, sid: str) -> bool:
                self.login_calls.append(sid)
                self.token = {sid: ("s", "t")}
                return True

        for store_token in (None, {"xiaomiio": ("s", "t")}):  # 无 token / 无 micoapi
            acct = FakeAccount(store_token)
            svc = FakeMinaService(devices=[{"deviceID": "111", "name": "小爱音箱"}])
            conn = XiaomiSpeakerConnector({"did": ""}, service=svc)
            conn._account = acct
            await conn.start()
            assert acct.login_calls == ["micoapi"]

    def test_build_voice_channel_disabled_returns_none(self):
        assert build_voice_channel(lambda t: _noop(), config={"enabled": False}) is None

    def test_build_voice_channel_enabled_builds(self):
        ch = build_voice_channel(
            lambda t: _noop(),
            config={"enabled": True, "username": "138xxxx", "poll_interval": 2.0},
            connector=FakeConnector([]),
        )
        assert ch is not None
        assert ch._poll_interval == 2.0

    def test_build_voice_channel_no_username_returns_none(self):
        # 启用但没配账号：fail-closed 不启动，避免空账号反复登录刷屏
        assert (
            build_voice_channel(
                lambda t: _noop(),
                config={"enabled": True, "username": ""},
            )
            is None
        )

    def test_build_voice_channel_with_username_builds(self):
        ch = build_voice_channel(
            lambda t: _noop(),
            config={"enabled": True, "username": "138xxxx"},
            connector=FakeConnector([]),
        )
        assert ch is not None


async def _noop(text: str = "") -> str:
    return ""


# ---------------------------------------------------------------------- lifespan 接线
class TestLifespanWiring:
    """app lifespan 的小爱语音通道接线：fail-open + 启停（不碰真实网络/账号）。"""

    def _app(self, tmp_path):
        from coworker.server.app import create_app
        from coworker.server.manager import SessionManager

        return create_app(SessionManager(data_dir=tmp_path / "data"))

    def test_flag_off_starts_clean(self, tmp_path):
        from fastapi.testclient import TestClient

        with TestClient(self._app(tmp_path)) as client:
            assert client.get("/v1/health").status_code == 200

    def test_flag_on_unconfigured_starts_clean(self, tmp_path, monkeypatch):
        """开关打开但配置未启用：不建通道，主服务完好（fail-closed）。"""
        monkeypatch.setenv("XIAOMI_SPEAKER_ENABLE", "1")
        monkeypatch.setattr(
            "deskmate.connectors.xiaomi_voice.load_speaker_config",
            lambda: {"enabled": False, "did": "", "poll_interval": 1.5},
        )
        from fastapi.testclient import TestClient

        with TestClient(self._app(tmp_path)) as client:
            assert client.get("/v1/health").status_code == 200

    def test_flag_on_channel_runs_and_stops(self, tmp_path, monkeypatch):
        """开关打开 + 注入假通道：lifespan 真的把循环跑起来，退出时干净停止。"""
        import time

        monkeypatch.setenv("XIAOMI_SPEAKER_ENABLE", "1")
        holder: dict = {}

        def fake_build(run_turn, *, config=None, connector=None):
            channel = VoiceChannel(
                connector=None,
                run_turn=_brain({"小桔在吗": "在呢萧哥"}),
                poll_interval=0.1,
                thinking_filler="",
            )
            conn = FakeConnector(
                [
                    [_ask("r1", 100, "旧问题")],
                    [_ask("r2", 200, "小桔在吗")],
                ]
            )
            conn.on_exhausted = channel.stop
            channel._connector = conn
            holder["channel"] = channel
            holder["conn"] = conn
            return channel

        monkeypatch.setattr(
            "deskmate.connectors.xiaomi_voice.build_voice_channel", fake_build
        )
        from fastapi.testclient import TestClient

        with TestClient(self._app(tmp_path)):
            # 条件轮询等结果（全量负载下固定 sleep 会 flaky），最多等 5s
            deadline = time.time() + 5.0
            while time.time() < deadline and not holder["conn"].spoken:
                time.sleep(0.05)
        conn = holder["conn"]
        assert conn.spoken == ["在呢萧哥"]
        assert conn.polls >= 2

    def test_flag_on_connector_failure_is_fail_open(self, tmp_path, monkeypatch):
        """通道启动即失败（如登录失败）：主服务照常启动。"""
        monkeypatch.setenv("XIAOMI_SPEAKER_ENABLE", "1")

        def fake_build(run_turn, *, config=None, connector=None):
            channel = VoiceChannel(
                connector=FakeConnector([]), run_turn=_brain(), poll_interval=0.1
            )

            async def boom():
                raise RuntimeError("login failed: no account")

            channel.serve = boom
            return channel

        monkeypatch.setattr(
            "deskmate.connectors.xiaomi_voice.build_voice_channel", fake_build
        )
        from fastapi.testclient import TestClient

        with TestClient(self._app(tmp_path)) as client:
            assert client.get("/v1/health").status_code == 200


# ---------------------------------------------------------------------- 运维可观测性 + 播报误报
class TestChannelLoggingAndSpeakErrors:
    """2026-10-17 真机联调暴露的工程缺陷（两轮迭代）：

    第一轮（11:00 修复）：
    1. uvicorn 只配自己的 logger，root 无 handler → 本模块 INFO 全被
       last-resort(WARNING+) 吞掉，「新提问/已播报」全程不可见，延迟排查只能靠猜。
    2. Mina ubus「远程控制超时」(code 3012) 被记成「播报失败」——真机实测命令
       已送达设备、正常播放。误报会误导后来人加重试逻辑（重试=同一条回答播两遍）。

    第二轮（16:20 修复，真机验收 3-5s 后从日志发现）：
    3. 3012 在 S12 上是**常态**（两轮真机都出现）而非异常：打 WARNING 会给
       「出错了」的错误信号，且「已播报」INFO 行被 except 分支吞掉——
       「新提问→已播报」运维闭环断了一半。
    4. ubus 回执等待无上限：服务端要 40+ 秒才回 3012（15:28:24 提问 →
       15:29:13 才打日志，49s），播放 3-5s 早开始了，daemon 却被回执等待
       卡住停滞轮询。加 speak_timeout 上限，超时按「已送达」处理。
    """

    def test_ensure_channel_logging_attaches_info_handler(self):
        import logging

        from deskmate.connectors import xiaomi_voice as xv

        lg = logging.getLogger("deskmate.connectors.xiaomi_voice")
        before = list(lg.handlers)
        try:
            xv.ensure_channel_logging()
            assert lg.handlers, "应至少有一个 handler（否则 INFO 仍被吞）"
            assert lg.getEffectiveLevel() == logging.INFO
            n = len(lg.handlers)
            xv.ensure_channel_logging()  # 幂等：不重复加
            assert len(lg.handlers) == n
        finally:
            lg.handlers = before  # 不污染其他测试
            lg.setLevel(logging.NOTSET)

    async def test_remote_timeout_logged_as_delivered_and_never_retried(self, caplog):
        """Mina 回执超时（3012）是 S12 常态：按「已播报/命令已送达」记 INFO，
        严禁重试（重试=双播），且不再打 WARNING（会误导成故障）。"""
        import logging

        calls = {"n": 0}

        async def timeout_speak(text: str) -> bool:
            calls["n"] += 1
            raise RuntimeError(
                "Error https://api2.mina.mi.com/remote/ubus: {'code': 101, "
                "'message': 'ubus server or device returned invalid result', "
                "'data': {'device_data': '{\"msg\":\"远程控制超时\",\"code\":3012}', "
                "'reqID': 'app_ios_x'}}"
            )

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
        )
        conn.speak = timeout_speak
        with caplog.at_level(logging.INFO, logger="deskmate.connectors.xiaomi_voice"):
            await channel.run_forever()
        assert calls["n"] == 1, "远程控制超时严禁重试（重试=双播）"
        delivered = [r for r in caplog.records if "已播报" in r.message]
        assert delivered, "3012 路径也应留下「已播报」闭环行"
        assert all(r.levelno == logging.INFO for r in delivered)
        assert any("命令已送达" in r.message for r in caplog.records)
        assert not any("播报失败" in r.message for r in caplog.records)
        assert not any(
            r.levelno >= logging.WARNING and "xiaomi-voice" in r.message
            for r in caplog.records
        ), "3012 常态不得打 WARNING（误报故障信号）"

    async def test_speak_receipt_wait_capped(self, caplog):
        """ubus 回执等待必须有上限：服务端 40+ 秒才回 3012（真机实测 49s 停滞），
        播放 3-5s 早已开始。speak_timeout 到点按「已送达」放行，循环不被卡死。"""
        import asyncio
        import logging

        calls = {"n": 0}

        async def slow_speak(text: str) -> bool:
            calls["n"] += 1
            await asyncio.sleep(30)  # 模拟服务端拖着不回执
            return True

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
        )
        conn.speak = slow_speak
        channel._speak_timeout = 0.2
        t0 = asyncio.get_running_loop().time()
        with caplog.at_level(logging.INFO, logger="deskmate.connectors.xiaomi_voice"):
            await channel.run_forever()
        elapsed = asyncio.get_running_loop().time() - t0
        assert calls["n"] == 1, "超时后严禁重试（重试=双播）"
        assert elapsed < 5, f"回执等待卡死了轮询：{elapsed:.1f}s（应 ~0.2s 放行）"
        assert any("已播报" in r.message for r in caplog.records)
        assert any("命令已送达" in r.message for r in caplog.records)

    async def test_genuine_speak_failure_still_logged_as_failure(self, caplog):
        import logging

        async def broken_speak(text: str) -> bool:
            raise RuntimeError("device offline")

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
        )
        conn.speak = broken_speak
        with caplog.at_level(logging.WARNING, logger="deskmate.connectors.xiaomi_voice"):
            await channel.run_forever()
        assert any("播报失败" in r.message for r in caplog.records)
        assert not any("命令已送达" in r.message for r in caplog.records)


# ---------------------------------------------------------------------- 提问聚合（半句截断防御）
class TestAskMerging:
    """小爱端点检测激进：用户话没说完（思考停顿/换气）就被判「说完了」，
    抢答开始后设备 AEC 还会吞掉后续语音——小桔可能只拿到半句话。

    2026-10-17 真机记录实证（18:04-18:05）：多数轮次 query 完整，但萧哥
    观察「往往没说完就接过去」——间歇性截断。对话记录服务里残句与后续
    可能拆成两条、也可能后续被 AEC 吞掉不进记录。

    对策：聚合窗口。发现提问后等 merge_window 秒，汇合窗口内到达的延续
    记录，按时间序拼接后一次性交给大脑。延迟被思考提示 filler 的播放
    覆盖（用户感知不变）；窗口内没有后续也不亏——只多等一个 filler 的
    时长。窗口值经 mi_speaker.json 可配，0 = 关闭（立即处理）。
    """

    async def test_single_ask_processed_after_window(self):
        """单条提问：等窗口到期后原样处理（不少字、不多字）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "你晓得我最近在写什么吗")],
                [],
                [],
                [],
                [],
            ],
            thinking_filler="",
            merge_window=0.3,
        )
        got: list[str] = []

        async def recording_brain(text: str) -> str:
            got.append(text)
            return "回复"

        channel._run_turn = recording_brain
        await channel.run_forever()
        assert got == ["你晓得我最近在写什么吗"]  # 原样，不拼接不截断
        assert conn.spoken == ["回复"]

    async def test_split_query_merged_within_window(self):
        """核心场景：残句 + 窗口内到达的后续 → 合并成完整句，只答一次。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],  # 基线
                [_ask("r2", 200, "你晓得我最近在写")],  # 残句（端点截断）
                [],
                [],
                [_ask("r3", 260, "什么小说吗")],  # 后续语音（窗口内到达）
                [],
                [],
            ],
            thinking_filler="",
            merge_window=0.5,
        )
        got: list[str] = []

        async def recording_brain(text: str) -> str:
            got.append(text)
            return "回复"

        channel._run_turn = recording_brain
        await channel.run_forever()
        assert got == ["你晓得我最近在写什么小说吗"], f"应合并为完整句，实际: {got}"
        assert conn.spoken == ["回复"]  # 只播一次（不是两条各答一次）

    async def test_asks_beyond_window_processed_separately(self):
        """超出窗口的到达算新问题：各自处理（用户真的停顿了很久）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],  # t=0.1 基线
                [_ask("r2", 200, "第一个问题")],  # t=0.2，deadline=0.6
                [],  # t=0.3
                [],  # t=0.4
                [],  # t=0.5
                [],  # t=0.6 ← 本 tick flush 第一问（无新记录混入）
                [_ask("r3", 900, "第二个问题")],  # t=0.7 新组
                [],  # t=0.8
                [],  # t=0.9 → 耗尽 stop（退出前 flush 第二问）
            ],
            thinking_filler="",
            merge_window=0.4,
        )
        got: list[str] = []

        async def recording_brain(text: str) -> str:
            got.append(text)
            return "回复"

        channel._run_turn = recording_brain
        await channel.run_forever()
        assert got == ["第一个问题", "第二个问题"]

    async def test_merge_order_follows_timestamp_and_dedups_adjacent(self):
        """拼接按时间序；相邻重复（同句两次识别）只留一份。"""
        from deskmate.connectors.xiaomi_voice import _merge_questions

        group = [
            _ask("r3", 300, "什么小说吗"),
            _ask("r2", 200, "你晓得我最近在写"),
            _ask("r2b", 210, "你晓得我最近在写"),  # 相邻重复
        ]
        assert _merge_questions(group) == "你晓得我最近在写什么小说吗"

    async def test_mute_happens_immediately_not_delayed_by_window(self):
        """静音时机不能被聚合窗口推迟——发现即静音（抢答压得越早漏得越少）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
                [],
                [],
            ],
            thinking_filler="",
            merge_window=0.5,
        )
        order: list[str] = []

        async def recording_brain(text: str) -> str:
            order.append("brain")
            return "回复"

        orig_mute = conn.mute_xiaoai

        async def recording_mute() -> bool:
            order.append("mute")
            return await orig_mute()

        conn.mute_xiaoai = recording_mute
        channel._run_turn = recording_brain
        await channel.run_forever()
        assert order[0] == "mute"  # 窗口内 mute 已发生，brain 要等窗口
        assert "brain" in order

    async def test_filler_sent_once_per_group(self):
        """filler 每组只发一次（不是窗口内每条记录一次）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "残句")],
                [],
                [],
                [_ask("r3", 260, "后续")],
                [],
                [],
            ],
            thinking_filler="让我想想",
            merge_window=0.5,
        )
        await channel.run_forever()
        assert conn.spoken.count("让我想想") == 1  # 一组一次

    async def test_stop_flushes_pending_group(self):
        """停止时仍有未到期组：flush 处理，不吞掉用户最后一句话。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "最后一问")],
                [],
                [],
            ],
            thinking_filler="",
            merge_window=30.0,
        )
        got: list[str] = []

        async def recording_brain(text: str) -> str:
            got.append(text)
            return "回复"

        channel._run_turn = recording_brain
        # 脚本耗尽 → on_exhausted → stop；退出前应 flush
        await channel.run_forever()
        assert got == ["最后一问"], f"stop 时应 flush 未到期组，实际: {got}"

    async def test_zero_window_processes_immediately(self):
        """merge_window=0：零延迟立即处理（向后兼容旧行为）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
                [],
            ],
            thinking_filler="",
            merge_window=0.0,
        )
        got: list[str] = []

        async def recording_brain(text: str) -> str:
            got.append(text)
            return "回复"

        channel._run_turn = recording_brain
        await channel.run_forever()
        assert got == ["问题"]


# ---------------------------------------------------------------------- 静音小爱（mute_xiaoai）
class TestMuteXiaoai:
    """小爱抢答消音方案（2026-10-17 萧哥提，xiaogpt --mute_xiaoai 同款）：

    已核实（GitHub 源码 + S12 真机实测）：
    - xiaogpt 用 player_pause（action=pause）不用 player_stop——pause 只暂停
      当前播放流、播放器会话保持；stop 更暴力，部分固件会中断播放器会话。
    - xiaogpt 先 player_get_status 确认 status==1（在播）才 pause——少发无效
      命令；S12 真机实测空闲时 status=3，pause/stop 空闲可达均返 True。
    - xiaogpt 双保险：轮询循环发现新提问即 pause + 主循环处理前再 pause。
    """

    async def test_mute_pauses_only_when_playing(self):
        svc = FakeMinaService()
        svc.play_status_override = {"status": 1, "volume": 40}
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        assert await conn.mute_xiaoai() is True
        assert svc.status_queries == ["d1"]  # 先查状态
        assert svc.pause_calls == ["d1"]  # 在播才 pause
        assert svc.stop_calls == []  # 严禁 player_stop（xiaogpt 社区选择）

    async def test_mute_skips_pause_when_idle(self):
        """status=3（S12 实测空闲态）：不发无效 pause。"""
        svc = FakeMinaService()
        svc.play_status_override = {"status": 3, "volume": 40}
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        assert await conn.mute_xiaoai() is False
        assert svc.pause_calls == []
        assert svc.stop_calls == []

    async def test_mute_skips_pause_when_already_paused(self):
        """status=2（已暂停）：不再 pause。"""
        svc = FakeMinaService()
        svc.play_status_override = {"status": 2, "volume": 40}
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        assert await conn.mute_xiaoai() is False
        assert svc.pause_calls == []

    async def test_mute_propagates_status_query_error(self):
        """查状态失败：异常上抛，由通道 _safe_mute 兜底（fail-open 不杀循环）。"""

        class BrokenStatus(FakeMinaService):
            async def player_get_status(self, deviceId: str):
                raise RuntimeError("network down")

        svc = BrokenStatus()
        conn = XiaomiSpeakerConnector({"did": "d1"}, service=svc)
        try:
            await conn.mute_xiaoai()
        except RuntimeError:
            pass
        else:
            raise AssertionError("状态查询异常应上抛（兜底在通道层）")
        assert svc.pause_calls == []

    async def test_channel_mutes_before_brain_runs(self):
        """发现新提问立即静音——必须早于大脑处理（xiaogpt 轮询循环打断）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],  # 基线轮
                [_ask("r2", 200, "小桔你好")],
            ],
        )

        async def recording_brain(text: str) -> str:
            conn.events.append(("brain", text))
            return "你好呀萧哥"

        channel._run_turn = recording_brain
        await channel.run_forever()
        kinds = [k for k, _ in conn.events]
        assert kinds[0] == "mute", f"首个事件应是静音，实际: {kinds}"
        assert "brain" in kinds and kinds.index("brain") > kinds.index("mute")

    async def test_channel_mutes_each_new_ask(self):
        """一轮轮询返回多条新提问：每条都先静音（连喊场景不漏）。"""
        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题一"), _ask("r3", 300, "问题二")],
            ],
        )
        await channel.run_forever()
        assert len(conn.mutes) >= 2  # 每问「发现即静音」+「检测时刻」各一次（播报前不再静音——会掐断 filler）

    async def test_mute_failure_does_not_kill_loop(self):
        """静音失败（如状态查询网络错）：记日志继续，下一问照常应答。"""

        class BrokenMuteConnector(FakeConnector):
            async def mute_xiaoai(self) -> bool:
                self.mutes.append(True)
                raise RuntimeError("mina 500")

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题一")],
                [_ask("r3", 300, "问题二")],
            ],
            connector_cls=BrokenMuteConnector,
            merge_window=0.0,  # 关聚合，两问独立处理（本测试意图：静音失败不杀循环）
        )
        await channel.run_forever()
        assert conn.spoken == ["默认回复", "默认回复"]  # 两问都正常应答
        assert len(conn.mutes) >= 2


# ------------------------------------------------------- 播放状态探测（提前打断）
class TestPlaybackProbe:
    """2026-10-17 萧哥验收反馈：「小爱的字还是挺多，然后才是让我想想」。

    根因实测：pause 机制本身有效（S12 真机：播放中 status=1 可识别、
    mute_xiaoai pause 后 status=0 真停），链路各段也快（轮询 0.08s、
    mute 0.4s）——晚在「对话记录出现」本身：用户说完 → 小爱开播 →
    记录才出现在 Mina API（估计 1-3s），等记录再 pause 小爱已播大半句。

    对策：播放状态探测——不等记录，每轮查 player_get_status，小爱在播
    （status=1）且非我方播出 → pause。把链路压到 轮询间隔+mute ~0.9s。
    代价：小爱放歌/闹钟/报时也会被 pause（语音出口场景可接受，
    mute_on_playback=false 可关）。守卫：我方生成/播出期（含 filler
    与正式播报的云端播放延迟）绝不探测 pause——会掐小桔自己的声音。
    """

    async def test_probe_mutes_xiaoai_without_any_record(self, caplog):
        """没有任何对话记录也 pause：小爱一开播就掐（不等记录）。"""
        import logging

        channel, conn = _channel([[], [], [], []])
        conn.play_status = {"status": 1}  # 小爱在播，但记录还没出现
        with caplog.at_level(logging.INFO, logger="deskmate.connectors.xiaomi_voice"):
            await channel.run_forever()
        assert conn.mutes, "在播即应 pause（提前打断）"
        assert conn.status_queries >= 1, "应先查播放状态"
        assert any("播放探测" in r.message for r in caplog.records)

    async def test_probe_skips_when_idle(self):
        """空闲（status=0/3）：不发无效 pause。"""
        channel, conn = _channel([[], [], []])
        conn.play_status = {"status": 0}
        await channel.run_forever()
        assert conn.mutes == []
        assert conn.status_queries >= 1  # 但状态查了（探测在工作）

    async def test_probe_respects_own_playback_guard(self):
        """我方生成/播出期：status=1 也绝不 pause（掐小桔自己的声音=事故）。"""

        async def slow_brain(text: str) -> str:
            conn.events.append(("brain", text))
            await asyncio.sleep(0.3)  # 生成期：探测必须让路
            return "回复"

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],  # 基线
                [],  # 探测轮（提问前，合法 pause）
                [_ask("r2", 200, "问题")],  # 触发 slow_brain（guard 起）
                [],  # 生成期
                [],  # 生成期
                [],  # 生成期
                [],  # guard 仍罩（filler/speak 云端延迟）
                [], [], [],
            ],
            run_turn=slow_brain,
        )
        conn.play_status = {"status": 1}  # 全程「在播」
        await channel.run_forever()
        # 关键断言：大脑跑起来之后（guard 已起 + 播报守卫续到播放结束），
        # 一次 mute 都不能再有——那会掐掉小桔自己的 filler/回答。
        kinds = [k for k, _ in conn.events]
        brain_at = kinds.index("brain")
        mutes_after_brain = kinds[brain_at:].count("mute")
        assert mutes_after_brain == 0, (
            f"生成/播出期探测应让路，brain 后还有 {mutes_after_brain} 次 mute"
        )

    async def test_probe_disabled_by_config(self):
        """mute_on_playback=false：关探测（用户接受记录驱动的漏字时）。"""
        channel, conn = _channel([[], [], []])
        conn.play_status = {"status": 1}
        channel._mute_on_playback = False
        await channel.run_forever()
        assert conn.mutes == []
        assert conn.status_queries == 0  # 关了就完全不查（省调用）

    async def test_probe_status_error_does_not_kill_loop(self, caplog):
        """查状态网络错：跳过本轮探测，循环不死（fail-open）。"""
        import logging

        class BrokenStatusConnector(FakeConnector):
            async def get_player_status(self) -> dict:
                raise RuntimeError("mina 500")

        channel, conn = _channel(
            [
                [_ask("r1", 100, "旧")],
                [_ask("r2", 200, "问题")],
            ],
            connector_cls=BrokenStatusConnector,
        )
        with caplog.at_level(logging.DEBUG, logger="deskmate.connectors.xiaomi_voice"):
            await channel.run_forever()
        assert conn.spoken == ["默认回复"]  # 主链路照常

    async def test_probe_mute_failure_does_not_kill_loop(self):
        """探测 pause 失败：不杀循环，下一轮继续探测。"""

        class BrokenProbeMuteConnector(FakeConnector):
            async def mute_xiaoai(self) -> bool:
                self.mutes.append(True)
                raise RuntimeError("mina 500")

        channel, conn = _channel(
            [[], [], [_ask("r1", 100, "旧")], [_ask("r2", 200, "问题")]],
            connector_cls=BrokenProbeMuteConnector,
        )
        conn.play_status = {"status": 1}
        await channel.run_forever()
        assert conn.spoken == ["默认回复"]  # 主链路照常


# ---------------------------------------------------------------------- 幻影模型护栏
class TestPhantomModelGuard:
    """xiaomi:mimo-v2.6 是幻影模型：小米 token-plan 端点 /v1/models 实测没有它
    （2026-10-17 curl 直证；chat 调用返 400 "Unsupported model"，真实存在的同级
    模型是 mimo-v2.6-pro）。

    真实踩坑：prefs.json 的 default_model 被设成幻影模型 → 每轮必 400 → 熔断 →
    降级重试，小爱语音端到端延迟 5-10s。矩阵/别名表再铺它就是给人埋雷。
    """

    def test_matrix_has_no_phantom_xiaomi_model(self):
        from coworker.providers.matrix import MATRIX

        assert "xiaomi:mimo-v2.6" not in MATRIX, "幻影模型必须清出矩阵（永远 400）"
        assert "xiaomi:mimo-v2.6-pro" in MATRIX, "真实模型应保留"

    def test_selection_aliases_never_point_at_phantom(self):
        from deskmate.selection.server import _MODEL_ALIASES

        bad = {s: f for s, f in _MODEL_ALIASES.items() if f == "xiaomi:mimo-v2.6"}
        assert not bad, f"别名仍指向幻影模型: {bad}"
        assert _MODEL_ALIASES["mimo"] == "xiaomi:mimo-v2.6-pro"


# ---------------------------------------------------------------------- 登录 CLI
class TestInteractiveLogin:
    """_interactive_login：MI_PASS 缺失时走 getpass 交互输入（不回显/不进历史）。"""

    def test_missing_mi_pass_prompts_via_getpass(self, monkeypatch):
        import asyncio

        from deskmate.connectors import xiaomi_speaker as xs

        monkeypatch.delenv("MI_PASS", raising=False)
        seen: dict = {}

        class FakeConn:
            def __init__(self, cfg):
                self._cfg = cfg

            async def start(self):
                seen["pass"] = os.environ.get("MI_PASS", "")
                seen["username"] = self._cfg.get("username", "")
                return "did-123"

            async def close(self):
                pass

        monkeypatch.setattr(xs, "XiaomiSpeakerConnector", FakeConn)
        monkeypatch.setattr(xs, "load_speaker_config", lambda: {"username": "138xxxx"})
        monkeypatch.setattr(
            "getpass.getpass", lambda prompt="": "pw-from-getpass"
        )
        rc = asyncio.run(xs._interactive_login())
        assert rc == 0
        assert seen["pass"] == "pw-from-getpass"
        assert seen["username"] == "138xxxx"

    def test_mi_pass_set_skips_getpass(self, monkeypatch):
        import asyncio

        from deskmate.connectors import xiaomi_speaker as xs

        monkeypatch.setenv("MI_PASS", "env-pass")

        def boom(prompt=""):
            raise AssertionError("MI_PASS 已设置时不应弹 getpass")

        class FakeConn:
            def __init__(self, cfg):
                pass

            async def start(self):
                return "did-123"

            async def close(self):
                pass

        monkeypatch.setattr(xs, "XiaomiSpeakerConnector", FakeConn)
        monkeypatch.setattr(xs, "load_speaker_config", lambda: {})
        monkeypatch.setattr("getpass.getpass", boom)
        rc = asyncio.run(xs._interactive_login())
        assert rc == 0


# ---------------------------------------------------------------------- OTP 接线
class TestOtpCallbackWiring:
    """start() 不得用 otp_callback=None 覆盖 miservice 自带的默认终端输入。

    真实踩坑：显式传 None 后，CLI 登录在需要短信验证码时直接抛
    "OTP verification required but no otp_callback provided"，
    终端交互被自己关掉。
    """

    def _fake_miservice(self, monkeypatch, captured: dict):
        import miservice

        class FakeAccount:
            def __init__(self, session, username, password, token_path=".mi.token", **kw):
                captured["kw"] = kw
                self.token = None

            async def login(self, sid):
                return True

        class FakeSvc:
            def __init__(self, account):
                pass

            async def device_list(self):
                return [{"deviceID": "d1", "name": "小爱音箱"}]

        monkeypatch.setattr(miservice, "MiAccount", FakeAccount)
        monkeypatch.setattr(miservice, "MiNAService", FakeSvc)

    def test_no_callback_keeps_library_default(self, monkeypatch):
        import asyncio

        captured: dict = {}
        self._fake_miservice(monkeypatch, captured)
        monkeypatch.setenv("MI_PASS", "pw")
        conn = XiaomiSpeakerConnector({"username": "u", "did": ""})
        try:
            did = asyncio.run(conn.start())
        finally:
            asyncio.run(conn.close())
        assert did == "d1"
        assert "otp_callback" not in captured["kw"]

    def test_explicit_callback_passed_through(self, monkeypatch):
        import asyncio

        captured: dict = {}
        self._fake_miservice(monkeypatch, captured)
        monkeypatch.setenv("MI_PASS", "pw")

        async def cb(otp_method: str) -> str:
            return "123456"

        conn = XiaomiSpeakerConnector({"username": "u", "did": ""})
        try:
            asyncio.run(conn.start(otp_callback=cb))
        finally:
            asyncio.run(conn.close())
        assert captured["kw"].get("otp_callback") is cb


# ---------------------------------------------------------------------- token 复用启动
class TestTokenReuseStart:
    """守护进程常驻路径：~/.mi.token 有效时，无 MI_PASS 也必须能启动。

    真实场景：lifespan 起 VoiceChannel 时环境里没有 MI_PASS，
    若 start() 无条件要求密码，token 明明有效也会被拦，语音通道起不来。
    """

    def _fake_miservice(self, monkeypatch):
        import miservice

        class FakeAccount:
            def __init__(self, session, username, password, token_path=".mi.token", **kw):
                self.token = None
                self.token_store = None

            async def login(self, sid):
                return True

        class FakeSvc:
            def __init__(self, account):
                pass

            async def device_list(self):
                return [{"deviceID": "d1", "name": "小爱音箱"}]

        monkeypatch.setattr(miservice, "MiAccount", FakeAccount)
        monkeypatch.setattr(miservice, "MiNAService", FakeSvc)

    def test_valid_token_starts_without_password(self, tmp_path, monkeypatch):
        import asyncio

        token_file = tmp_path / ".mi.token"
        token_file.write_text(
            json.dumps({"deviceId": "x", "userId": "u", "micoapi": ("s", "t")}),
            encoding="utf-8",
        )
        self._fake_miservice(monkeypatch)
        monkeypatch.delenv("MI_PASS", raising=False)
        conn = XiaomiSpeakerConnector(
            {"username": "u", "did": "", "token_path": str(token_file)}
        )
        try:
            did = asyncio.run(conn.start())
        finally:
            asyncio.run(conn.close())
        assert did == "d1"

    def test_no_token_no_password_still_raises(self, tmp_path, monkeypatch):
        import asyncio

        self._fake_miservice(monkeypatch)
        monkeypatch.delenv("MI_PASS", raising=False)
        conn = XiaomiSpeakerConnector(
            {"username": "u", "did": "", "token_path": str(tmp_path / "none.json")}
        )
        with pytest.raises(RuntimeError, match="未配置小米账号"):
            asyncio.run(conn.start())


class TestVoiceChannelEnvFlag:
    """XIAOMI_SPEAKER_ENABLE 运维开关（.env）：未开时即使 json enabled=true
    也不构建通道。此前 lifespan 漏读该变量——开关是假的，只有 json 生效。"""

    def test_env_off_skips_build(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XIAOMI_SPEAKER_ENABLE", raising=False)
        calls = {"n": 0}

        def fake_build(run_turn, *, config=None, connector=None):
            calls["n"] += 1
            return None

        monkeypatch.setattr(
            "deskmate.connectors.xiaomi_voice.build_voice_channel", fake_build
        )
        from fastapi.testclient import TestClient

        from coworker.server.app import create_app
        from coworker.server.manager import SessionManager

        with TestClient(create_app(SessionManager(data_dir=tmp_path / "data"))):
            pass
        assert calls["n"] == 0

    def test_env_on_builds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XIAOMI_SPEAKER_ENABLE", "1")
        calls = {"n": 0}

        def fake_build(run_turn, *, config=None, connector=None):
            calls["n"] += 1
            return None

        monkeypatch.setattr(
            "deskmate.connectors.xiaomi_voice.build_voice_channel", fake_build
        )
        from fastapi.testclient import TestClient

        from coworker.server.app import create_app
        from coworker.server.manager import SessionManager

        with TestClient(create_app(SessionManager(data_dir=tmp_path / "data"))):
            pass
        assert calls["n"] == 1
