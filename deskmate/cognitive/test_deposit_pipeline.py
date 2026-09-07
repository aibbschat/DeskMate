"""验证「三条闭环通水」：① 验证答对即沉淀 ② 访谈消化 gap 落四维 ③ 空四维灌候选。

通过 mock deskmate.memory.llm.chat 返回结构化 JSON，验证从用户亲口表达里
抽取的四维度认知正确落库（带 contexts 情境标签、来源可追溯、查重跳过、fail-open）。
"""
import pytest
from unittest.mock import patch

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import SourceRef, CognitiveGap
from deskmate.cognitive.extractor import extract_cognitive_items
from deskmate.cognitive.interview import SocraticInterviewEngine, seed_missing_dimensions
from deskmate.cognitive.validation_engine import get_validation_engine

FAKE_JSON = """{
  "stances": [{"topic":"出版观","content":"书稿出版后就是商品，但灵魂不能卖","contexts":["出版取舍"],"confidence":0.6}],
  "argument_patterns": [{"content":"习惯用正史对比演义来辨伪","pattern_type":"历史类比","evidence_preference":"正史>笔记>演义","contexts":["学术圈"],"confidence":0.6}],
  "value_rankings": [{"domain":"创作","ranking":["真实","好看","创新"],"content":"冲突时保真实","tradeoff_notes":"商业压力下可妥协创新","contexts":["创作圈"],"confidence":0.6}],
  "thinking_frameworks": [{"domain":"历史问题","steps":["制度背景","权力结构","人物选择","后果"],"content":"分析历史先看结构","contexts":["学术圈"],"confidence":0.6}]
}"""


@pytest.fixture
def pm(tmp_path):
    return CognitiveProfileManager(str(tmp_path / "cog.db"))


# ---------------------------------------------------------------------------
# ① 验证答对即沉淀（extract_cognitive_items，mode=new_only）
# ---------------------------------------------------------------------------
def test_extract_new_only_deposits_four_dims(pm):
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", return_value=FAKE_JSON):
        res = extract_cognitive_items(
            "我重视真实，出版也这样", source_type="validation",
            source_title="验证", pm=pm, mode="new_only")
    assert res["deposited"] == 4, res
    assert pm.profile.stances[0].contexts == ["出版取舍"]
    assert pm.profile.stances[0].sources[0].type == "validation"
    assert pm.profile.value_rankings[0].contexts == ["创作圈"]
    assert pm.profile.argument_patterns[0].contexts == ["学术圈"]
    assert pm.profile.thinking_frameworks[0].contexts == ["学术圈"]


def test_extract_new_only_skips_existing(pm):
    pm.add_stance(topic="出版观", content="书稿出版后就是商品",
                  source=SourceRef(type="interview", title="x"))
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", return_value=FAKE_JSON):
        res = extract_cognitive_items("我重视真实", pm=pm, mode="new_only")
    assert res["skipped"] >= 1, res
    assert res["deposited"] == 3, res
    assert len(pm.profile.stances) == 1


def test_extract_fail_open_when_no_llm(pm):
    with patch("deskmate.memory.llm.llm_available", return_value=False):
        res = extract_cognitive_items("任何文字", pm=pm, mode="new_only")
    assert res["error"] == "llm_not_available"
    assert res["deposited"] == 0
    assert len(pm.profile.stances) == 0


def test_apply_feedback_deposits_on_high_similarity(pm):
    """apply_feedback 在像我度>=0.75 时把作答新角度沉淀（①接通验证）。"""
    s, _ = pm.add_stance(topic="出版观", content="书稿出版后就是商品",
                         source=SourceRef(type="interview", title="x"))
    pm.save()
    old_conf = s.confidence
    evaluation = {
        "overall_score": 0.85,
        "related_items": [s.id],
        "scenario": "一个平台约稿要求改结局，你会怎么处理？",
        "dimension_scores": {},
    }
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", return_value=FAKE_JSON):
        get_validation_engine().apply_feedback(
            pm.profile, evaluation, user_response="我重视真实，出版也这样", pm=pm)
    # 原有条目置信度提升
    assert pm.profile.stances[0].confidence > old_conf
    # 新角度沉积（value/thinking/argument 至少落）
    assert len(pm.profile.value_rankings) >= 1
    assert len(pm.profile.thinking_frameworks) >= 1
    assert len(pm.profile.argument_patterns) >= 1


# ---------------------------------------------------------------------------
# ② 访谈消化 gap：_extract_and_resolve_gap 走 extract_cognitive_items 落四维
# ---------------------------------------------------------------------------
def test_interview_digest_gap_deposits_four_dims_and_resolves(pm):
    pm.profile.cognitive_gaps.append(
        CognitiveGap(topic="认知盲区：创作取舍", reason="x", priority=0.7))
    pm.save()
    gap = pm.profile.cognitive_gaps[0]
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", return_value=FAKE_JSON):
        SocraticInterviewEngine._extract_and_resolve_gap(
            pm, gap.id, "我重视真实，出版也这样", layer="cognitive")
    assert len(pm.profile.stances) >= 1
    assert len(pm.profile.value_rankings) >= 1
    assert len(pm.profile.argument_patterns) >= 1
    assert len(pm.profile.thinking_frameworks) >= 1
    assert pm.profile.cognitive_gaps[0].status == "resolved"


def test_interview_absorb_emotional_keeps_legacy(pm):
    """情感层仍走原逻辑（带 emo tag），不被 extractor 覆盖。"""
    pm.profile.cognitive_gaps.append(
        CognitiveGap(topic="关怀盲区：家人", reason="x", priority=0.7))
    pm.save()
    gap = pm.profile.cognitive_gaps[0]
    SocraticInterviewEngine._absorb_response(pm, gap.id, "家人面前我会软下来", layer="emotional")
    s = [x for x in pm.profile.stances if x.topic == "家人"][0]
    # 情感层规则路径带「情感层」tag，且未误走 extractor 落四维
    assert "情感层" in (s.tags or [])
    assert len(pm.profile.value_rankings) == 0


# ---------------------------------------------------------------------------
# ③ 空四维灌候选：generate_questions 扫描全四维 questioned + seed_missing_dimensions
# ---------------------------------------------------------------------------
def test_generate_questions_scans_all_dims_questioned(pm):
    pm.add_value_ranking(domain="创作", ranking=["真实"], content="保真实",
                         source=SourceRef(type="derived", title="推导", confidence=0.55))
    pm.profile.value_rankings[0].mark_questioned(reason="由立场推导候选，待你确认")
    pm.save()
    qs = SocraticInterviewEngine().generate_questions(pm.profile, limit=5)
    assert any("创作" in q["topic"] for q in qs), [q["topic"] for q in qs]


def test_seed_missing_dimensions_seeds_questioned_candidates(pm):
    pm.add_stance(topic="历史真实性", content="正史一元论要不得",
                  source=SourceRef(type="interview", title="x"))
    pm.save()
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.memory.llm.chat", return_value=FAKE_JSON):
        res = seed_missing_dimensions(layer="cognitive", pm=pm)
    assert res["seeded"] >= 1, res
    q_items = [it for it in (pm.profile.value_rankings
                             + pm.profile.thinking_frameworks
                             + pm.profile.argument_patterns)
               if it.status == "questioned"]
    assert len(q_items) >= 1
    # 且带 contexts（容纳矛盾的基础）
    assert all(it.contexts for it in q_items)
