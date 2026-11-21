"""创作建议（creative_suggest）+ 认知库（cognition_store）测试。

全部 mock 掉 embedding / LLM，不烧 token、不联网，验证：
- 认知库写入 + 检索（带 fact_keys 溯源）+ 去重
- 创作建议生成（检索认知库 + 锚定真实观察的 refs）
- 降级：认知库空 / LLM 不可用 → 返回 None
- 接线：observation_diary 写完理解后写入认知库
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _fake_embed(text):
    """确定性常量向量，使 cosine=1.0，search 必返回已写入条目（不联网）。"""
    return [0.1] * 8


def test_cognition_store_add_and_search(tmp_path):
    from deskmate.agency.cognition_store import add_understanding, search

    with patch("deskmate.memory.semantic_search.embed_text", _fake_embed):
        add_understanding(str(tmp_path), date="2026-09-06", fact_keys=["k1"],
                          understanding="中东能源紧张影响丝路贸易。", facts=["（外部·财经）中东能源紧张"])
        add_understanding(str(tmp_path), date="2026-09-06", fact_keys=["k2"],
                          understanding="体育突破让国民信心上升。", facts=["（外部·other）郑钦文夺冠"])
        hits = search(str(tmp_path), "丝路 商队 能源", top_k=5)

    assert len(hits) == 2
    assert any("丝路" in h["understanding"] for h in hits)
    # 溯源 fact_keys 保留
    assert all(h["fact_keys"] for h in hits)


def test_cognition_store_dedup(tmp_path):
    from deskmate.agency.cognition_store import add_understanding

    with patch("deskmate.memory.semantic_search.embed_text", _fake_embed):
        assert add_understanding(str(tmp_path), date="d", fact_keys=["k"],
                                 understanding="X", facts=["f"]) is True
        # 同一条理解不重复写
        assert add_understanding(str(tmp_path), date="d", fact_keys=["k"],
                                 understanding="X", facts=["f"]) is False


def test_generate_returns_suggestion_with_refs(tmp_path):
    from deskmate.agency.behaviors.creative_suggest import generate
    from deskmate.agency.cognition_store import add_understanding

    # 2026-11-15：embedding patch 必须覆盖**检索**调用，而不只是写入。
    # 此前只包住 add_understanding，检索时真实后端不可用 → 降级关键词，靠旧的
    # 「一个二字重叠即入选」侥幸命中（测试假绿）。收紧阈值后须走真向量路。
    with patch("deskmate.memory.semantic_search.embed_text", _fake_embed), \
         patch("deskmate.memory.llm.chat_memory",
               return_value="这让我想到那天看到的中东能源紧张——真实的能源与地缘张力，也许能映到你笔下的丝路商队暗线，让权谋更有重量。"), \
         patch("deskmate.memory.llm.memory_llm_available", return_value=True):
        add_understanding(str(tmp_path), date="d", fact_keys=["k1"],
                          understanding="中东能源紧张影响丝路贸易。", facts=["（外部·财经）中东能源紧张"])
        out = generate(str(tmp_path), "大明丝路商队", user_text="角色卡在贸易线")

    assert out is not None
    assert "丝路" in out["suggestion"]
    assert out["refs"]  # 带真实世界溯源


def test_generate_none_when_empty(tmp_path):
    from deskmate.agency.behaviors.creative_suggest import generate

    out = generate(str(tmp_path), "某本还没灵感可照进的书")
    assert out is None


def test_generate_none_when_llm_unavailable(tmp_path):
    from deskmate.agency.behaviors.creative_suggest import generate
    from deskmate.agency.cognition_store import add_understanding

    with patch("deskmate.memory.semantic_search.embed_text", _fake_embed):
        add_understanding(str(tmp_path), date="d", fact_keys=["k1"],
                          understanding="中东能源紧张影响丝路贸易。", facts=["（外部·财经）中东能源紧张"])

    with patch("deskmate.memory.llm.memory_llm_available", return_value=False):
        out = generate(str(tmp_path), "大明丝路商队")
    assert out is None


def test_observation_diary_writes_cognition(tmp_path):
    """understanding 层写完后，理解进入认知库（供创作建议消费）。"""
    from deskmate.agency.behaviors.observation_diary import run_observation_diary
    from deskmate.agency.cognition_store import search as search_cognition

    class FakeLearnedStore:
        def __init__(self, *a, **k):
            pass

        def active_recent(self, limit=200):
            return [{"fact": "（外部·财经）中东能源紧张", "credibility": 0.8}]

    with patch("deskmate.memory.learned_store.LearnedStore", FakeLearnedStore), \
         patch("deskmate.agency.behaviors.understand_observations.decide_and_digest",
               return_value=[{"body": "小桔理解：中东能源紧张影响丝路贸易。",
                              "fact_keys": []}]), \
         patch("deskmate.memory.semantic_search.embed_text", _fake_embed):
        res = run_observation_diary(str(tmp_path),
                                    {"cooldown_hours": 0, "max_observations": 6, "understand": True})

    assert res["written"] == 1
    assert res["understood"] is True
    hits = search_cognition(str(tmp_path), "丝路")
    assert len(hits) == 1
    assert "丝路" in hits[0]["understanding"]


def test_try_inspire_skips_non_creative_context(tmp_path):
    """端点编排：非创作语境不触发（不烧 LLM）。"""
    from deskmate.agency.behaviors.creative_suggest import try_inspire

    out = try_inspire(str(tmp_path), "今天天气真不错")
    assert out["triggered"] is False
    assert out["reason"] == "not_creative_context"


def test_try_inspire_runs_when_due(tmp_path):
    """端点编排：创作语境 + 冷却到点 + 有认知 → 触发并返回带溯源的灵感。"""
    from deskmate.agency.behaviors.creative_suggest import try_inspire
    from deskmate.agency.cognition_store import add_understanding

    # 2026-11-15：① embedding patch 覆盖到检索调用（旧版只包住写入，靠降级误命中假绿）；
    # ② patch 目标改为 creative_suggest 里的绑定名，而不是 chat_flow 的源属性
    #    （``from ... import`` 已绑定，patch 源模块属性根本不生效）；
    # ③ 出稿后新增的落冷却动作在测试里 stub 掉，不写临时目录之外的状态。
    with patch("deskmate.memory.semantic_search.embed_text", _fake_embed), \
         patch("deskmate.agency.behaviors.creative_suggest._inspire_due", return_value=True), \
         patch("deskmate.agency.behaviors.creative_suggest._mark_inspire_delivered"), \
         patch("deskmate.memory.llm.chat_memory",
               return_value="这让我想到那天看到的中东能源紧张——真实的能源与地缘张力，"
                            "也许能映到你笔下的丝路商队暗线，让权谋更有重量。"), \
         patch("deskmate.memory.llm.memory_llm_available", return_value=True):
        add_understanding(str(tmp_path), date="d", fact_keys=["k1"],
                          understanding="中东能源紧张影响丝路贸易。", facts=["（外部·财经）中东能源紧张"])
        out = try_inspire(str(tmp_path), "我这本书里的世界观还没捋清", "大明丝路商队")

    assert out["triggered"] is True
    assert "丝路" in out["suggestion"]
    assert out["refs"]  # 锚定真实世界观察
