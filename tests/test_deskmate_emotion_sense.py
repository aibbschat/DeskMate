"""Tests for 情感感知 deskmate/growth/emotion_sense.py（E3）。

重点不是「判得准」（那是模型的事），而是**判错时它会闭嘴**：
三道闸门、结果校验、永不抛异常。
"""

from __future__ import annotations

import pytest

from deskmate.growth import emotion_sense


@pytest.fixture
def llm_ready(monkeypatch):
    """把 LLM 通道伪装成可用，并返回可注入的回复钩子。"""
    import deskmate.memory.llm as llm_mod

    monkeypatch.delenv(emotion_sense._ENV_FLAG, raising=False)
    monkeypatch.setattr(llm_mod, "memory_llm_available", lambda: True, raising=False)
    holder: dict[str, object] = {"reply": ""}

    def _chat(system, prompt, **kw):
        return holder["reply"]

    monkeypatch.setattr(llm_mod, "chat_memory", _chat, raising=False)
    return holder


def _ok_json(state="压力大", mood=58, confidence=0.8, evidence="卡住了"):
    import json

    return json.dumps(
        {"state": state, "mood": mood, "confidence": confidence, "evidence": evidence},
        ensure_ascii=False,
    )


# ----------------------------------------------------------------------
# 闸门 1：能力
# ----------------------------------------------------------------------
class TestCapabilityGate:
    def test_kill_switch_disables_llm(self, monkeypatch, llm_ready):
        monkeypatch.setenv(emotion_sense._ENV_FLAG, "0")
        assert emotion_sense._llm_enabled() is False
        assert "kill switch" in emotion_sense.llm_disabled_reason()

    def test_kill_switch_accepts_various_off_values(self, monkeypatch, llm_ready):
        for v in ("0", "off", "false", "no", "NONE"):
            monkeypatch.setenv(emotion_sense._ENV_FLAG, v)
            assert emotion_sense._llm_enabled() is False, v

    def test_unset_env_means_enabled(self, llm_ready):
        assert emotion_sense._llm_enabled() is True
        assert emotion_sense.llm_disabled_reason() == ""

    def test_no_provider_disables_llm(self, monkeypatch):
        import deskmate.memory.llm as llm_mod

        monkeypatch.delenv(emotion_sense._ENV_FLAG, raising=False)
        monkeypatch.setattr(llm_mod, "memory_llm_available", lambda: False, raising=False)
        assert emotion_sense._llm_enabled() is False
        assert emotion_sense.llm_disabled_reason() == "未配置 provider"

    def test_llm_falls_back_to_rule_when_disabled(self, monkeypatch, llm_ready):
        monkeypatch.setenv(emotion_sense._ENV_FLAG, "0")
        got = emotion_sense.sense(["今天写得特别顺，心里踏实。"])
        assert got is not None
        assert got["source"] == "rule"
        assert got["state"] == "顺利"


# ----------------------------------------------------------------------
# 闸门 2：素材
# ----------------------------------------------------------------------
class TestSubstanceGate:
    def test_too_short_is_not_sent(self, llm_ready, monkeypatch):
        called = []
        monkeypatch.setattr(
            "deskmate.memory.llm.chat_memory",
            lambda *a, **k: called.append(1) or _ok_json(),
            raising=False,
        )
        assert emotion_sense.llm_sense(["今天累了"]) is None
        assert not called, "过短的碎片不该拿去问模型"

    def test_empty_texts_returns_none(self, llm_ready):
        assert emotion_sense.sense([]) is None
        assert emotion_sense.sense(["", "   "]) is None


# ----------------------------------------------------------------------
# 闸门 3：结果
# ----------------------------------------------------------------------
class TestResultGate:
    def test_valid_output_accepted(self, llm_ready):
        llm_ready["reply"] = _ok_json()
        got = emotion_sense.sense(["今天第三章卡住了，写不下去，很烦躁。"])
        assert got["state"] == "压力大"
        assert got["source"] == "llm"
        assert got["confidence"] == 0.8

    def test_unknown_state_discarded(self, llm_ready):
        llm_ready["reply"] = _ok_json(state="暴躁")   # 不在五态白名单
        got = emotion_sense.sense(["今天心情一般般吧，说不上来。"])
        # 被丢弃后落到规则层 → 这段没有情绪词 → 不表态
        assert got is None

    def test_empty_state_means_model_abstained(self, llm_ready):
        llm_ready["reply"] = '{"state":"","mood":0,"confidence":0,"evidence":""}'
        assert emotion_sense.llm_sense(["今天把书稿目录整理了一下。"]) is None

    def test_out_of_range_mood_discarded(self, llm_ready):
        llm_ready["reply"] = _ok_json(mood=180)
        assert emotion_sense.llm_sense(["今天卡住了，很烦躁，写不下去。"]) is None

    def test_low_confidence_discarded(self, llm_ready):
        """拿不准就不表态 —— 这是本模块最重要的行为。"""
        llm_ready["reply"] = _ok_json(confidence=0.2)
        assert emotion_sense.llm_sense(["今天卡住了，很烦躁，写不下去。"]) is None

    def test_fabricated_evidence_is_stripped(self, llm_ready):
        """模型编的证据必须清零 —— 留着假证据比没有证据更危险。"""
        llm_ready["reply"] = _ok_json(evidence="他说他想去海边")
        got = emotion_sense.sense(["今天卡住了，很烦躁，写不下去。"])
        assert got["evidence"] == ""
        assert got["state"] == "压力大"   # 判断本身仍可用

    def test_real_evidence_is_kept(self, llm_ready):
        llm_ready["reply"] = _ok_json(evidence="写不下去")
        got = emotion_sense.sense(["今天卡住了，写不下去，很烦躁。"])
        assert got["evidence"] == "写不下去"

    def test_non_json_reply_discarded(self, llm_ready):
        llm_ready["reply"] = "我觉得他今天心情不太好。"
        assert emotion_sense.llm_sense(["今天卡住了，很烦躁，写不下去。"]) is None

    def test_json_is_extracted_from_surrounding_text(self, llm_ready):
        llm_ready["reply"] = f'好的，我的判断是：{_ok_json()}。希望有帮助。'
        got = emotion_sense.llm_sense(["今天卡住了，很烦躁，写不下去。"])
        assert got is not None and got["state"] == "压力大"

    def test_hypothetical_evidence_voids_the_judgement(self, llm_ready):
        """「如果没有他，心情会跌到极点」——那是**没发生的事**。

        真实案例（2026-09-05 的日记）：他写「我一直在庆幸一件事——幸好有尹遥……
        我感觉到很幸福，会想：如果没有尹遥，我一定是郁闷的，心情就会跌落至极点」。
        模型摘录后半句当证据，把「庆幸」判成了「低落」——他明明在庆幸，
        小桔却要去安慰他。这种错比不判断糟糕得多，所以整次判定作废。
        """
        text = (
            "我一直在庆幸一件事——幸好有尹遥。他一直跟爸爸亲，看着他要爸爸时，"
            "我感觉到很幸福，会想：如果没有尹遥，我一定是郁闷的，心情就会跌落至极点。"
        )
        llm_ready["reply"] = _ok_json(state="低落", mood=30, evidence="心情就会跌落至极点")
        assert emotion_sense.llm_sense([text]) is None

    def test_hypothetical_mark_inside_evidence_is_caught(self, llm_ready):
        """模型把整句引全时，引导词在证据自己身上。"""
        text = "看着他很活泼，我感觉到很幸福。会想：如果没有尹遥，我一定是郁闷的。"
        llm_ready["reply"] = _ok_json(
            state="低落", mood=30, evidence="如果没有尹遥，我一定是郁闷的"
        )
        assert emotion_sense.llm_sense([text]) is None

    def test_real_statement_not_mistaken_as_hypothetical(self, llm_ready):
        """真实陈述不能被误伤 —— 闸门拦的是假设，不是所有负面句。"""
        text = "今天卡住了，写不下去，很烦躁，这种状态让人沮丧。"
        llm_ready["reply"] = _ok_json(
            state="压力大", mood=55, evidence="今天卡住了，写不下去"
        )
        got = emotion_sense.llm_sense([text])
        assert got is not None and got["state"] == "压力大"

    def test_mood_clamped_away_from_extremes(self, llm_ready):
        """模型偶尔给极端值（多半又是读错了），不许一路飘到 0 或 100。"""
        text = "今天卡住了，写不下去，很烦躁。"
        llm_ready["reply"] = _ok_json(state="低落", mood=5, evidence="今天卡住了")
        got = emotion_sense.llm_sense([text])
        assert got is not None
        assert got["mood"] >= 25.0

    def test_mood_falls_back_to_state_baseline(self, llm_ready):
        llm_ready["reply"] = _ok_json(state="顺利", mood=0)
        got = emotion_sense.sense(["今天写得特别顺，真好。"])
        assert got["mood"] == emotion_sense.STATE_MOOD["顺利"]


# ----------------------------------------------------------------------
# 规则降级
# ----------------------------------------------------------------------
class TestRuleFallback:
    def test_positive_words(self):
        got = emotion_sense.rule_sense(["下午第三章写顺了，真好，心里踏实。"])
        assert got["state"] == "顺利"
        assert got["source"] == "rule"

    def test_negative_words(self):
        got = emotion_sense.rule_sense(["今天卡住了，写不下去，很烦躁。"])
        assert got["state"] == "压力大"

    def test_low_spirit_words(self):
        got = emotion_sense.rule_sense(["提不起精神，什么也不想做，很沮丧。"])
        assert got["state"] == "低落"

    def test_no_emotion_words_returns_none(self):
        assert emotion_sense.rule_sense(["今天把书稿目录整理了一下。"]) is None

    def test_tie_returns_none(self):
        """正负打平 → 不表态，交给行为推断。"""
        assert emotion_sense.rule_sense(["写得顺利，但也很累。"]) is None

    def test_rule_confidence_is_low(self):
        """规则的置信度必须低于 LLM —— 它只数了词，没读句子。"""
        got = emotion_sense.rule_sense(["下午第三章写顺了，真好。"])
        assert got["confidence"] < 0.6


# ----------------------------------------------------------------------
# 健壮性
# ----------------------------------------------------------------------
class TestRobustness:
    def test_llm_exception_falls_back_to_rule(self, llm_ready, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("provider 炸了")

        monkeypatch.setattr("deskmate.memory.llm.chat_memory", _boom, raising=False)
        got = emotion_sense.sense(["下午第三章写顺了，真好，心里踏实。"])
        assert got["source"] == "rule"

    def test_sense_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            emotion_sense, "llm_sense", lambda t: (_ for _ in ()).throw(ValueError("x"))
        )
        # 异常被外层吞掉？llm_sense 自己有 try，这里验证 sense 组合后的兜底
        try:
            emotion_sense.sense(["下午第三章写顺了，真好。"])
        except Exception as e:  # pragma: no cover
            pytest.fail(f"sense 不该抛异常: {e}")

    def test_digest_stable_and_sensitive(self):
        a = emotion_sense.digest(["上午改了一版。"])
        b = emotion_sense.digest(["上午改了一版。"])
        c = emotion_sense.digest(["上午改了一版。", "下午又改了一版。"])
        assert a == b
        assert a != c

    def test_all_states_have_a_mood_baseline(self):
        for s in emotion_sense.EMOTION_STATES:
            assert 0 < emotion_sense.STATE_MOOD[s] <= 100, s
