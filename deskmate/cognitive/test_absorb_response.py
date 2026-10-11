"""访谈回答吸收兜底回归：任何抽取失败都落规则兜底，绝不静默丢弃。

背景（2026-10-11，萧哥实证）：
旧 _absorb_response 只在 ``dep.get("error") == "llm_not_available"`` 时走规则兜底，
其余 error（抽取器抛异常 / JSON 解析失败 / 调用失败 → ``{"error": "..."}``）落入 else
分支什么都不落，等于用户白答。修复后 ``if dep.get("error"):`` 覆盖所有失败。

覆盖：
- 成功（无 error 键）→ 信任 LLM 路径，不补 访谈 类 stance（避免重复落）
- llm_call_failed / json_parse_failed / exception → 落 访谈 类 stance（兜底）
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import SourceRef, CognitiveGap
from deskmate.cognitive.interview import SocraticInterviewEngine


def _setup():
    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "test.db")
    pm = CognitiveProfileManager(profile_path=db)
    gap = CognitiveGap(topic="测试盲区：某个话题", reason="从未提及", priority=0.7)
    pm.get().cognitive_gaps.append(gap)
    pm.save()
    return pm, gap


def _count_interview_stances(pm):
    return sum(1 for s in pm.get().stances if s.category == "访谈")


@pytest.fixture
def patched_extractor(monkeypatch):
    """强制 LLM 可用，并暴露一个可替换的 extract_cognitive_items。

    注意：extractor 内部 ``from deskmate.memory.llm import llm_available``，
    llm_available 不在 extractor 模块级，故 patch 点在 deskmate.memory.llm。
    """
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda *a, **k: True)
    calls = {"fn": None}

    def _set(fn):
        calls["fn"] = fn
        monkeypatch.setattr(
            "deskmate.cognitive.extractor.extract_cognitive_items", fn
        )

    return _set


def test_success_no_rule_fallback(patched_extractor):
    """成功（无 error 键）→ 信任 LLM 路径，不补 访谈 类 stance。"""
    pm, gap = _setup()
    before = _count_interview_stances(pm)

    def ok(*a, **k):
        return {"deposited": 1, "skipped": 0}

    patched_extractor(ok)
    SocraticInterviewEngine._absorb_response(pm, gap.id, "我的真实回答")
    pm.save()
    after = _count_interview_stances(pm)
    assert after == before, "成功路径不应再补一条 访谈 类 stance（避免重复落）"


def test_llm_call_failed_falls_back(patched_extractor):
    pm, gap = _setup()
    before = _count_interview_stances(pm)

    def fail(*a, **k):
        return {"deposited": 0, "skipped": 0, "error": "llm_call_failed: timeout"}

    patched_extractor(fail)
    SocraticInterviewEngine._absorb_response(pm, gap.id, "我的真实回答")
    pm.save()
    assert _count_interview_stances(pm) == before + 1, "调用失败必须兜底落 访谈 类 stance"


def test_json_parse_failed_falls_back(patched_extractor):
    pm, gap = _setup()
    before = _count_interview_stances(pm)

    def fail(*a, **k):
        return {"deposited": 0, "skipped": 0, "error": "json_parse_failed"}

    patched_extractor(fail)
    SocraticInterviewEngine._absorb_response(pm, gap.id, "我的真实回答")
    pm.save()
    assert _count_interview_stances(pm) == before + 1, "JSON 解析失败必须兜底落 访谈 类 stance"


def test_extractor_exception_falls_back(patched_extractor):
    pm, gap = _setup()
    before = _count_interview_stances(pm)

    def boom(*a, **k):
        raise RuntimeError("抽取器炸了")

    patched_extractor(boom)
    SocraticInterviewEngine._absorb_response(pm, gap.id, "我的真实回答")
    pm.save()
    assert _count_interview_stances(pm) == before + 1, "抽取器抛异常必须兜底落 访谈 类 stance"
