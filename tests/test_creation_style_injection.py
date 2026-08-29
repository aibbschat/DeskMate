"""C 任务回归测试：P1-5 量化风格信号接入写作/审稿流水线。

验证：
- 写作侧：_requirement_block 在存在 style_profile.json 时注入「文风指纹」强约束段；
  无画像时不注入（零配置不污染 prompt）。
- 审稿侧（executor.review）：prompt 注入「文风对齐评判」段。
- 圆桌终审（build_roundtable_context）：上下文注入「文风对齐」指纹。

所有注入走统一的 format_style_fingerprint（带路径缓存、无画像返回空串）。
"""
from __future__ import annotations

import json

import pytest

from deskmate.creation import executor as EX
from deskmate.creation.final_review import FinalReviewProposal, build_roundtable_context
from deskmate.style_profile_store import (
    clear_style_fingerprint_cache,
    format_style_fingerprint,
)


def _metrics() -> dict:
    return {
        "n": 2,
        "sentences": {
            "avg_len": 46.4,
            "median_len": 40,
            "short_pct": 20.0,
            "long_pct": 10.0,
            "overlong_pct": 2.0,
        },
        "paragraphs": {"avg_len": 120, "short_pct": 5.0, "long_pct": 8.0},
        "four_char": {"count": 30, "pct_of_chunks": 32.9},
        "classical_per_1k": 6.3,
        "modern_per_1k": 3.1,
        "dialogue_pct": 20.9,
        "punct": {
            "period_per_1k": 12.0,
            "comma_per_1k": 20.0,
            "dunhao_per_1k": 5.0,
            "semicolon_per_1k": 1.0,
        },
    }


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    """落地一份含量化指标的 style_profile.json，并把 DEFAULT_PROFILE_PATH 指向它。"""
    import deskmate.style_profile_store as sps

    p = tmp_path / "style_profile.json"
    p.write_text(
        json.dumps({"metrics": _metrics(), "updates": 1}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(sps, "DEFAULT_PROFILE_PATH", p)
    clear_style_fingerprint_cache()
    return p


def test_requirement_block_injects_fingerprint_when_profile_present(profile_path):
    block = EX._requirement_block(style=["悬疑"])
    assert "【制作要求（必须严格遵守，违者返工）】" in block
    assert "文风指纹" in block
    assert "句长：平均 46.4" in block


def test_requirement_block_no_fingerprint_when_no_profile(tmp_path, monkeypatch):
    import deskmate.style_profile_store as sps

    monkeypatch.setattr(sps, "DEFAULT_PROFILE_PATH", tmp_path / "nope.json")
    clear_style_fingerprint_cache()
    block = EX._requirement_block(style=["悬疑"])
    assert "文风指纹" not in block
    assert "风格基调：悬疑" in block


def test_format_style_fingerprint_empty_without_metrics(tmp_path, monkeypatch):
    import deskmate.style_profile_store as sps

    p = tmp_path / "style_profile.json"
    p.write_text(
        json.dumps({"profile": {}, "updates": 0}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(sps, "DEFAULT_PROFILE_PATH", p)
    clear_style_fingerprint_cache()
    assert format_style_fingerprint() == ""


def test_review_prompt_injects_style_alignment(profile_path, monkeypatch):
    import deskmate.memory.llm as llm_mod

    captured: dict = {}

    def fake_chat(*, system, user, max_tokens=0):
        captured["user"] = user
        return "VERDICT: APPROVE\n通过"

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    monkeypatch.setattr(llm_mod, "llm_available", lambda: True)

    res = EX.LLMExecutor().review("某段剧本文本", criteria="结构完整、人物动机一致")
    assert res.approved is True
    u = captured["user"]
    assert "验收标准：结构完整" in u
    assert "文风对齐评判" in u
    assert "句长：平均 46.4" in u


def test_roundtable_context_injects_style_alignment(profile_path):
    prop = FinalReviewProposal(
        item_kind="screenplay",
        space="x",
        item_id=1,
        review_item_id=2,
        topic="t",
        manuscript_text="m",
        criteria="c",
        prior_review="前序意见",
    )
    ctx = build_roundtable_context(prop)
    assert "前序意见" in ctx
    assert "文风对齐" in ctx
    assert "句长：平均 46.4" in ctx
