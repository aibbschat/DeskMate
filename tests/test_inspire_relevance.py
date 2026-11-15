"""灵感卡「割裂」的复现与护栏（2026-11-15）。

病因（11-14 巡查）：
- ``try_inspire`` 的 ``concept or text`` 把用户随口一句同时当成「他正在写的书」
  「检索词」「他刚说」；
- 认知库降级检索（本机 embedding 不可用时真正跑的那条）是「一个二字重叠即入选」；
- prompt 又强制每条启发锚定历史新闻观察，且冷却「触发即刷、出稿失败也吃额度」。
⇒ 卡片与当前话题割裂是结构性的，不是关键词偶尔误触。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _entry(text: str) -> dict:
    return {
        "understanding": text,
        "facts": [],
        "fact_keys": [],
        "date": "d",
        "theme": "",
    }


def test_try_inspire_does_not_promote_text_to_concept(tmp_path: Path):
    """用户随口一句不能被塞进「他正在写的书」。"""
    from deskmate.agency.behaviors import creative_suggest as cs

    captured: dict = {}

    def fake_generate(base_dir, concept, user_text="", **kwargs):
        captured["concept"] = concept
        captured["user_text"] = user_text
        return {"suggestion": "一条贴合话题的启发" * 8, "refs": [["（外部·news）某观察"]]}

    with patch.object(cs, "generate", fake_generate), \
         patch.object(cs, "_inspire_due", return_value=True), \
         patch.object(cs, "_mark_inspire_delivered", create=True):
        out = cs.try_inspire(str(tmp_path), "这段人物动机我捋不清")

    assert out["triggered"] is True
    assert captured["concept"] == "", "用户原话不该被当成书名 / 概念"
    assert captured["user_text"] == "这段人物动机我捋不清"


def test_keyword_search_rejects_incidental_bigram():
    """长句里碰巧一个二字重叠，不该把无关新闻捞进素材。"""
    from deskmate.agency.cognition_store import _keyword_search

    entries = [
        _entry("两艘油轮在霍尔木兹海峡触雷起火，革命卫队说是违规油轮。"),
        _entry("十吨级电动商用飞机有人驾驶首飞成功。"),
    ]
    query = "我在写人物动机想捋清情节顺便看到油轮这两个字但跟书没关系"

    hits = _keyword_search(entries, query, 5)
    assert hits == [], "仅靠一个二字重叠召回 = 灵感卡割裂的直接来源"


def test_keyword_search_keeps_short_precise_query():
    """收紧阈值不能误伤短而准的检索词（既有测试也依赖它）。"""
    from deskmate.agency.cognition_store import _keyword_search

    entries = [_entry("中东能源紧张影响丝路贸易。")]
    hits = _keyword_search(entries, "丝路", 5)
    assert len(hits) == 1


def test_inspire_cooldown_only_after_real_delivery(tmp_path: Path):
    """弃权 / 出稿失败不能白吃 6 小时额度（送达凭证语义）。"""
    from deskmate.agency.behaviors import creative_suggest as cs
    from deskmate.agency.cognition_store import add_understanding

    with patch("deskmate.memory.semantic_search.embed_text", lambda t: [0.1] * 8):
        add_understanding(str(tmp_path), date="d", fact_keys=["k"],
                          understanding="中东能源紧张影响丝路贸易。", facts=["（外部·财经）中东能源紧张"])

    with patch("deskmate.memory.llm.chat_memory", return_value="无"), \
         patch("deskmate.memory.llm.memory_llm_available", return_value=True):
        out = cs.try_inspire(str(tmp_path), "这段人物动机我捋不清")

    assert out["triggered"] is False
    assert not (tmp_path / "agency" / "last_inspire.json").exists(), \
        "弃权却刷了冷却 = 6 小时内不再有机会递真正贴合的灵感"


def test_recent_turns_reach_prompt_without_polluting_query(tmp_path: Path):
    """最近几轮对话进 prompt 作话题锚，但不塞进检索 query（长句全是虚词噪声）。"""
    from deskmate.agency.behaviors import creative_suggest as cs

    captured: dict = {}
    hit = {
        "understanding": "中东能源紧张影响丝路贸易。",
        "facts": ["（外部·财经）中东能源紧张"],
        "fact_keys": ["k"], "date": "d", "theme": "",
    }

    def fake_chat(_system, prompt, **kwargs):
        captured["prompt"] = prompt
        return "这让我想到那天看到的中东能源紧张，也许能映到你笔下的丝路商队暗线，让权谋更有重量。" * 2

    with patch.object(cs, "search_cognition", return_value=[hit]) as search, \
         patch("deskmate.memory.llm.chat_memory", fake_chat), \
         patch("deskmate.memory.llm.memory_llm_available", return_value=True):
        out = cs.generate(
            str(tmp_path), "", user_text="这段人物动机我捋不清",
            recent_turns=["萧盛：上一章在写丝路商队", "小桔：那条暗线还没收"],
        )

    assert out is not None
    assert "你们刚才在聊的" in captured["prompt"]
    assert "丝路商队" in captured["prompt"]
    assert "先判断相不相干" in captured["prompt"]
    assert "（未提供具体书名/概念）" in captured["prompt"], "空概念不该被用户原话顶替"
    query = search.call_args[0][1]
    assert "上一章在写丝路商队" not in query


def test_abstain_marker_prevents_delivery_and_cooldown(tmp_path: Path):
    """素材真存在、但 LLM 判不相干 → 不递卡，也不刷冷却。"""
    from deskmate.agency.behaviors import creative_suggest as cs

    hit = {
        "understanding": "十吨级电动商用飞机有人驾驶首飞成功。",
        "facts": ["（外部·tech）电动飞机首飞"],
        "fact_keys": ["k"], "date": "d", "theme": "",
    }
    with patch.object(cs, "search_cognition", return_value=[hit]), \
         patch("deskmate.memory.llm.chat_memory", return_value="无"), \
         patch("deskmate.memory.llm.memory_llm_available", return_value=True):
        out = cs.try_inspire(str(tmp_path), "这段人物动机我捋不清")

    assert out["triggered"] is False
    assert not (tmp_path / "agency" / "last_inspire.json").exists()
