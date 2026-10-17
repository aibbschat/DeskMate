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
        self.played: list[str] = []
        self.stops = 0
        self.polls = 0

    async def latest_asks(self) -> list[dict]:
        self.polls += 1
        if self._script:
            return self._script.pop(0)
        if self.on_exhausted is not None:
            self.on_exhausted()
        return []

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        return True

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


def _channel(script: list[list[dict]], run_turn=None, connector_cls=FakeConnector):
    """装配 通道+假连接器（脚本耗尽自动停），返回 (channel, connector)。"""
    channel = VoiceChannel(
        connector=None,
        run_turn=run_turn or _brain(),
        poll_interval=0.1,
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
        assert conn.stops >= 1  # 播报前打断了小爱原生应答
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

    async def device_list(self, master: int = 0):
        return self._devices

    async def get_conversations(self, deviceId: str, hardware: str, limit: int = 10):
        """userprofile conversation 接口的 fake（生产走 account 直连 HTTP）。"""
        self.last_hardware = hardware
        return self._records

    async def get_latest_ask(self, deviceId: str):
        return []  # nlp_result_get 路线：S12 老固件实测返 1001 空数据，已弃用

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
