"""按需语音朗读（豆包 Agent Plan / seed-tts-2.0）的契约测试。

覆盖四层，全部纯逻辑 + 依赖注入，不出网、不写真库、不碰真实音频设备：

1. NDJSON 流式响应解析（data:null 跳过、结束码 20000000、错误码、防静默截断）
2. 屏幕正文 → 可朗读文本的清洗与截断（含「0 是合法值」红线）
3. 意图工具的语义触发与「不带正文」契约
4. VoiceTurnTracker 的单轮语义与「取当轮最终正文、不是工具循环中段」
5. 请求形状锁死（Plan 专属 host、X-Api-* 头、禁走 ark /api/v3）

参考 tests/test_xiaomi_voice.py 的「依赖注入 + fake 驱动全流程」范式。
"""
from __future__ import annotations

import base64
import json

import pytest

from deskmate import tts


def _line(code, data=None, **extra):
    row = {"code": code, "message": "OK" if code == 20000000 else "", "data": data}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False).encode("utf-8")


def _chunk(payload: bytes) -> bytes:
    return _line(0, base64.b64encode(payload).decode("ascii"))


# ---------------------------------------------------------------- 1. NDJSON


def test_parse_ndjson_audio_concatenates_base64_chunks():
    raw = b"\n".join([_chunk(b"AAA"), _chunk(b"BBB"), _line(20000000)])
    assert tts.parse_ndjson_audio(raw) == b"AAABBB"


def test_parse_ndjson_audio_skips_sentence_and_usage_rows():
    """实测响应里夹着 {"code":0,"data":null,"sentence":...} 与结束行的 usage，
    把它们当音频拼进去会污染 mp3。"""
    raw = b"\n".join(
        [
            _chunk(b"ID3"),
            _line(0, None, sentence={"text": "你好", "words": []}),
            _chunk(b"MPEG"),
            _line(20000000, None, usage={"text_words": 2}),
        ]
    )
    assert tts.parse_ndjson_audio(raw) == b"ID3MPEG"


def test_parse_ndjson_audio_raises_on_service_error_code():
    raw = b"\n".join([_line(45000000, None), _line(20000000)])
    with pytest.raises(tts.VoiceReplyError, match="45000000"):
        tts.parse_ndjson_audio(raw)


def test_parse_ndjson_audio_requires_end_marker():
    """没有 code=20000000 就是被截断的流，绝不静默当成一段短音频播出去。"""
    with pytest.raises(tts.VoiceReplyError, match="end"):
        tts.parse_ndjson_audio(_chunk(b"AAA"))


def test_parse_ndjson_audio_rejects_empty_stream():
    with pytest.raises(tts.VoiceReplyError, match="no audio"):
        tts.parse_ndjson_audio(_line(20000000))


# ---------------------------------------------------------------- 2. 正文清洗


def test_clean_for_speech_strips_markdown_furniture():
    src = (
        "# 标题\n\n"
        "这是**重点**和`行内代码`，还有[链接文字](https://example.com)。\n"
        "![图片](https://example.com/a.png)\n"
        "- 列表项一\n- 列表项二\n\n"
        "> 引用一句\n\n"
        "```python\nprint('不该念')\n```\n\n"
        "---\n\n结尾。"
    )
    out = tts.clean_for_speech(src)
    assert "这是重点和行内代码，还有链接文字。" in out
    assert "列表项一" in out and "列表项二" in out
    assert "引用一句" in out
    assert "结尾。" in out
    assert "不该念" not in out
    assert "https://example.com" not in out
    assert "```" not in out and "**" not in out and "---" not in out
    assert "#" not in out and "![" not in out


def test_clean_for_speech_preserves_paragraph_breaks():
    """禁 re.sub(r\"\\s+\") 式全压：段落换行要留住，否则一口气念完。"""
    out = tts.clean_for_speech("第一段。\n\n第二段。")
    assert "第一段。" in out and "第二段。" in out
    assert "\n" in out


def test_truncate_zero_means_no_truncation():
    """数值兜底红线：0 是合法值，不能被 `x or default` 吞成 4000。"""
    text = "甲" * 5000
    out, truncated = tts.truncate_for_speech(text, max_chars=0)
    assert out == text and truncated is False


def test_truncate_prefers_sentence_boundary_and_reports_it():
    text = "第一句。" * 1000
    out, truncated = tts.truncate_for_speech(text, max_chars=50)
    assert truncated is True
    assert len(out) <= 50
    assert out.endswith("。")


# ---------------------------------------------------------------- 3. 意图工具


def test_voice_tool_has_no_text_parameter():
    """模型只判意图，不另写一段朗读稿 —— 正文由宿主从最终回复取。"""
    tool = tts.make_voice_reply_tool()
    schema = tool.__coworker_schema__
    assert tool.__name__ == tts.VOICE_TOOL_NAME
    assert schema["function"]["name"] == tts.VOICE_TOOL_NAME
    assert schema["function"]["parameters"]["properties"] == {}
    assert schema["function"]["parameters"].get("required", []) == []


def test_voice_tool_description_uses_semantic_triggers_not_a_keyword_list():
    desc = tts.make_voice_reply_tool().__coworker_schema__["function"]["description"]
    for phrase in ("语音回复", "念给我听", "读出来", "说给我听", "文字就行"):
        assert phrase in desc
    assert "speak" in desc.lower() or "read aloud" in desc.lower()


def test_voice_tool_is_low_risk_and_no_approval():
    tool = tts.make_voice_reply_tool()
    assert tool.__aisuite_tool_metadata__.requires_approval is False
    result = tool()
    assert result.get("voice_reply_requested") is True
    assert "audio" not in result and "text" not in result


# ---------------------------------------------------------------- 4. 单轮追踪


def test_tracker_picks_last_final_reply_not_tool_loop_intermediate():
    tracker = tts.VoiceTurnTracker()
    tracker.observe("assistant_message", {"text": "我先查一下", "tool_calls": ["request_voice_reply"]})
    tracker.observe("tool_finished", {"name": tts.VOICE_TOOL_NAME, "status": "ok"})
    tracker.observe("assistant_message", {"text": "这是屏幕上那条最终回复。", "tool_calls": []})
    assert tracker.should_speak is True
    assert tracker.text == "这是屏幕上那条最终回复。"


def test_tracker_stays_silent_without_tool_call():
    """平时不自动朗读：没调工具就绝不发声。"""
    tracker = tts.VoiceTurnTracker()
    tracker.observe("assistant_message", {"text": "普通回复", "tool_calls": []})
    assert tracker.should_speak is False


def test_tracker_ignores_failed_tool_and_other_tools():
    tracker = tts.VoiceTurnTracker()
    tracker.observe("tool_finished", {"name": tts.VOICE_TOOL_NAME, "status": "error"})
    tracker.observe("tool_finished", {"name": "web_search", "status": "ok"})
    tracker.observe("assistant_message", {"text": "正文", "tool_calls": []})
    assert tracker.should_speak is False


def test_tracker_is_single_turn():
    """说完即静默：新的一轮不继承上一轮的语音标记。"""
    first = tts.VoiceTurnTracker()
    first.observe("tool_finished", {"name": tts.VOICE_TOOL_NAME, "status": "ok"})
    first.observe("assistant_message", {"text": "上一轮", "tool_calls": []})
    assert first.should_speak is True
    second = tts.VoiceTurnTracker()
    second.observe("assistant_message", {"text": "这一轮", "tool_calls": []})
    assert second.should_speak is False


def test_tracker_disabled_gate():
    tracker = tts.VoiceTurnTracker(enabled=False)
    tracker.observe("tool_finished", {"name": tts.VOICE_TOOL_NAME, "status": "ok"})
    tracker.observe("assistant_message", {"text": "正文", "tool_calls": []})
    assert tracker.should_speak is False


# ---------------------------------------------------------------- 5. 请求形状


def test_request_targets_plan_host_not_ark():
    """萧哥红线：ark.cn-beijing.volces.com/api/v3 接入会产生额外费用。"""
    assert tts.TTS_URL == "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
    assert "ark.cn-beijing.volces.com" not in tts.TTS_URL
    assert "/api/v3/plan/" in tts.TTS_URL


def test_request_headers_carry_resource_id_and_uuid():
    headers = tts.build_tts_headers("fake-key", request_id="fixed-uuid")
    assert headers["X-Api-Key"] == "fake-key"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert headers["X-Api-Request-Id"] == "fixed-uuid"
    assert "Authorization" not in headers


def test_request_body_uses_req_params_shape():
    body = tts.build_tts_payload("你好", voice="zh_female_vv_uranus_bigtts")
    assert body["req_params"]["text"] == "你好"
    assert body["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"
    assert body["req_params"]["audio_params"]["format"] == "mp3"
    assert "model" not in body and "response_format" not in body


def test_voice_can_be_switched_by_env(monkeypatch):
    """豆包音色 ID 各家不同：允许不改代码换音色。"""
    monkeypatch.setenv("DESKMATE_TTS_VOICE", "zh_male_wennuanahuazhe_mars_bigtts")
    assert tts.build_tts_payload("你好")["req_params"]["speaker"] == (
        "zh_male_wennuanahuazhe_mars_bigtts"
    )
    # 显式传参优先于 env
    assert tts.build_tts_payload("你好", voice="explicit")["req_params"]["speaker"] == "explicit"


def test_synthesize_uses_plan_key_from_secret_store(tmp_path):
    captured = {}

    def fake_poster(url, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return b"\n".join([_chunk(b"FAKEMP3"), _line(20000000)])

    class FakeSecrets:
        def get(self, profile):
            assert profile == "provider:ark-agent-plan-cn"
            return {"api_key": "ark-fake", "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3"}

    audio = tts.synthesize("你好", secrets=FakeSecrets(), poster=fake_poster)
    assert audio == b"FAKEMP3"
    assert captured["url"] == tts.TTS_URL
    assert captured["headers"]["X-Api-Key"] == "ark-fake"


def test_speak_reply_plays_cleaned_final_text_and_fails_open(tmp_path):
    played = {}

    def fake_poster(url, headers, body, timeout):
        played["payload"] = json.loads(body)
        return b"\n".join([_chunk(b"MP3"), _line(20000000)])

    def fake_player(data, suffix):
        played["audio"] = data
        played["suffix"] = suffix
        return True

    class FakeSecrets:
        def get(self, profile):
            return {"api_key": "ark-fake"}

    result = tts.speak_reply(
        "**重点**：这是正文。",
        secrets=FakeSecrets(),
        poster=fake_poster,
        player=fake_player,
    )
    assert result["ok"] is True
    assert played["audio"] == b"MP3"
    assert played["suffix"] == ".mp3"
    assert played["payload"]["req_params"]["text"] == "重点：这是正文。"
    assert result["text"] == "重点：这是正文。"


def test_speak_reply_missing_key_returns_disabled_without_raising():
    class EmptySecrets:
        def get(self, profile):
            return None

    result = tts.speak_reply("正文", secrets=EmptySecrets(), poster=lambda *a: pytest.fail("should not post"))
    assert result["ok"] is False
    assert "key" in result["reason"].lower()


def test_speak_reply_network_failure_is_fail_open():
    def bad_poster(*args):
        raise OSError("network down")

    class FakeSecrets:
        def get(self, profile):
            return {"api_key": "ark-fake"}

    result = tts.speak_reply("正文", secrets=FakeSecrets(), poster=bad_poster)
    assert result["ok"] is False
    assert "network down" in result["reason"]


# ---------------------------------------------------------------- 6. 音色切换


def test_every_preset_is_a_uranus_voice():
    """Plan + seed-tts-2.0 实测只有 2.0（uranus）音色能出音频：
    1.0 的 mars/moon/conversation 音色一律 `resource ID is mismatched`。
    这条锁住「别把没授权的音色塞进预设」。"""
    for preset in tts.VOICE_PRESETS.values():
        assert preset["id"].endswith("_uranus_bigtts"), preset


def test_resolve_voice_accepts_alias_chinese_name_and_full_id():
    assert tts.resolve_voice("vivi") == "zh_female_vv_uranus_bigtts"
    assert tts.resolve_voice("VIVI") == "zh_female_vv_uranus_bigtts"      # 大小写不敏感
    assert tts.resolve_voice(" 高冷御姐 ") == "zh_female_gaolengyujie_uranus_bigtts"
    assert tts.resolve_voice("zh_male_aojiaobazong_uranus_bigtts") == (
        "zh_male_aojiaobazong_uranus_bigtts"
    )
    assert tts.resolve_voice("") is None
    assert tts.resolve_voice("没这个音色") is None


def test_voice_catalog_is_human_readable_and_covers_both_genders():
    rows = tts.voice_catalog()
    assert len(rows) >= 6
    assert {r["gender"] for r in rows} == {"female", "male"}
    for row in rows:
        assert row["alias"] and row["label"] and row["id"]


def test_current_voice_precedence_explicit_env_saved_default(monkeypatch, tmp_path):
    """explicit 参数 > env > 写盘值 > 默认。四层都要能单测，不许隐式耦合。"""
    tts.save_voice("xiaohe", prefs_path=tmp_path / "prefs.json")
    assert tts.current_voice(explicit="explicit", prefs_path=tmp_path / "prefs.json") == "explicit"

    monkeypatch.setenv("DESKMATE_TTS_VOICE", "from-env")
    assert tts.current_voice(prefs_path=tmp_path / "prefs.json") == "from-env"

    monkeypatch.delenv("DESKMATE_TTS_VOICE", raising=False)
    assert tts.current_voice(prefs_path=tmp_path / "prefs.json") == (
        "zh_female_xiaohe_uranus_bigtts"
    )

    assert tts.current_voice(prefs_path=tmp_path / "nothing.json") == tts.DEFAULT_VOICE


def test_save_voice_persists_id_and_keeps_other_prefs_keys(tmp_path):
    """写 prefs.json 只动自己的键，绝不覆盖 memory_llm_group 等既有配置。"""
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"memory_llm_group": "paid", "default_model": "x:y"}), encoding="utf-8")
    tts.save_voice("御姐", prefs_path=prefs)
    data = json.loads(prefs.read_text(encoding="utf-8"))
    assert data["voice_reply_voice"] == "zh_female_gaolengyujie_uranus_bigtts"
    assert data["memory_llm_group"] == "paid"
    assert data["default_model"] == "x:y"
    assert tts.load_saved_voice(prefs_path=prefs) == "zh_female_gaolengyujie_uranus_bigtts"


def test_save_voice_rejects_unknown_voice_without_writing(tmp_path):
    prefs = tmp_path / "prefs.json"
    with pytest.raises(tts.VoiceReplyError, match="unknown"):
        tts.save_voice("查无此声", prefs_path=prefs)
    assert not prefs.exists()


def test_corrupt_prefs_is_tolerated(tmp_path):
    """手改坏了 prefs.json 也不能让语音链路炸 —— 读失败就当没配过。"""
    prefs = tmp_path / "prefs.json"
    prefs.write_text("{not json", encoding="utf-8")
    assert tts.load_saved_voice(prefs_path=prefs) is None
    assert tts.current_voice(prefs_path=prefs, env={}) == tts.DEFAULT_VOICE


def test_set_voice_tool_switches_and_reports(tmp_path):
    tool = tts.make_set_voice_tool(prefs_path=tmp_path / "prefs.json")
    assert tool.__name__ == tts.SET_VOICE_TOOL_NAME
    schema = tool.__coworker_schema__
    assert "voice" in schema["function"]["parameters"]["properties"]
    # 刻意不是必填：不带参调用＝列出可选音色（用户问「都有什么声音」时用）
    assert schema["function"]["parameters"]["required"] == []

    ok = tool("阳光青年")
    assert ok["ok"] is True
    assert ok["voice"] == "zh_male_yangguangqingnian_uranus_bigtts"
    assert tts.load_saved_voice(prefs_path=tmp_path / "prefs.json") == ok["voice"]

    bad = tool("不存在的音色")
    assert bad["ok"] is False
    assert "unknown" in bad["reason"].lower()
    # 认不出时把可选清单带回去，模型下一句才能给用户具体选项
    assert bad["voices"]


def test_set_voice_tool_lists_choices_when_asked_without_argument(tmp_path):
    """用户问「都有什么声音」时，模型可以不带参调用拿到清单。"""
    tool = tts.make_set_voice_tool(prefs_path=tmp_path / "prefs.json")
    out = tool()
    assert out["voices"] and len(out["voices"]) >= 6


def test_build_payload_uses_saved_voice(monkeypatch, tmp_path):
    monkeypatch.delenv("DESKMATE_TTS_VOICE", raising=False)
    tts.save_voice("儒雅逸辰", prefs_path=tmp_path / "prefs.json")
    body = tts.build_tts_payload("你好", prefs_path=tmp_path / "prefs.json")
    assert body["req_params"]["speaker"] == "zh_male_ruyayichen_uranus_bigtts"


# ---------------------------------------------------------------- 7. 语音气泡


def test_message_tool_is_zero_argument_like_the_speak_tool():
    """气泡走「发消息」而不是「立刻念」：同一个哲学——模型只判意图，不产正文。"""
    tool = tts.make_send_voice_message_tool()
    assert tool.__name__ == tts.MESSAGE_TOOL_NAME
    schema = tool.__coworker_schema__
    assert schema["function"]["parameters"]["properties"] == {}
    assert schema["function"]["parameters"].get("required", []) == []
    assert tool() == {"voice_message_requested": True, "scope": "this_turn_only"}


def test_two_voice_tools_are_distinguishable_by_description():
    """「念给我听」和「发条语音」是两个不同出口，描述里必须各自点明，否则模型会选错。"""
    speak = tts.make_voice_reply_tool().__coworker_schema__["function"]["description"].lower()
    message = tts.make_send_voice_message_tool().__coworker_schema__["function"]["description"].lower()
    assert "speaker" in speak or "aloud" in speak
    assert "message" in message or "chat" in message


def test_tracker_marks_message_mode_and_does_not_speak():
    tracker = tts.VoiceTurnTracker()
    tracker.observe("tool_finished", {"name": tts.MESSAGE_TOOL_NAME, "status": "ok"})
    tracker.observe("assistant_message", {"text": "正文。", "tool_calls": []})
    assert tracker.should_send_message is True
    assert tracker.should_speak is False
    assert tracker.mode == "message"


def test_tracker_last_voice_tool_wins():
    """同一轮两个工具都调了（模型犹豫）：以最后一次为准，绝不同时出声又发气泡。"""
    tracker = tts.VoiceTurnTracker()
    tracker.observe("tool_finished", {"name": tts.VOICE_TOOL_NAME, "status": "ok"})
    tracker.observe("tool_finished", {"name": tts.MESSAGE_TOOL_NAME, "status": "ok"})
    tracker.observe("assistant_message", {"text": "正文。", "tool_calls": []})
    assert tracker.mode == "message"
    assert tracker.should_speak is False


def test_build_voice_message_synthesizes_without_playing():
    """气泡要拿到音频字节自己发，绝不能在服务端先 afplay 一遍。"""

    def fake_poster(url, headers, body, timeout):
        return b"\n".join([_chunk(b"A" * 8000), _line(20000000)])

    class FakeSecrets:
        def get(self, profile):
            return {"api_key": "ark-fake"}

    result = tts.build_voice_message("**正文**。", secrets=FakeSecrets(), poster=fake_poster)
    assert result["ok"] is True
    assert result["audio"] == b"A" * 8000
    assert result["text"] == "正文。"
    # 契约：只合成，不播。函数根本不接受 player 参数。
    assert "played" not in result


def test_build_voice_message_is_fail_open():
    class FakeSecrets:
        def get(self, profile):
            return {"api_key": "ark-fake"}

    def bad_poster(*a):
        raise OSError("network down")

    result = tts.build_voice_message("正文", secrets=FakeSecrets(), poster=bad_poster)
    assert result["ok"] is False
    assert "network down" in result["reason"]
    assert result["audio"] == b""


def test_message_length_is_capped_shorter_than_plain_speech():
    """气泡是聊天里的一条消息，不是有声书：上限要比「朗读」短，避免几 MB 塞进 WS。"""
    assert tts.DEFAULT_MESSAGE_MAX_CHARS < tts.DEFAULT_MAX_CHARS
    long_text = "甲" * 3000
    result = tts.build_voice_message(
        long_text,
        secrets=type("S", (), {"get": lambda self, p: {"api_key": "k"}})(),
        poster=lambda *a: b"\n".join([_chunk(b"MP3"), _line(20000000)]),
    )
    assert result["truncated"] is True
    assert len(result["text"]) <= tts.DEFAULT_MESSAGE_MAX_CHARS


def test_estimate_audio_seconds_matches_observed_bitrate():
    """实测 64kbps：34221B ↔ 4.27s。前端会用真实 duration 校正，这里只要不偏一个量级。"""
    assert 4.0 < tts.estimate_audio_seconds(b"\0" * 34221) < 4.6
    assert tts.estimate_audio_seconds(b"") == 0.0


def test_voice_reply_available_gate(monkeypatch):
    monkeypatch.setenv("DESKMATE_VOICE_REPLY_ENABLED", "1")
    assert tts.voice_reply_available(platform="darwin") is True
    assert tts.voice_reply_available(platform="linux") is False
    monkeypatch.setenv("DESKMATE_VOICE_REPLY_ENABLED", "0")
    assert tts.voice_reply_available(platform="darwin") is False
