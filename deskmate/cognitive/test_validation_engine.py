"""ValidationEngine 测试：「认知 Turing test」闭环。

覆盖：
- 生成情境：从高置信度画像条目抽话题、套模板、带 related_items
- 评估相似度：四维度打分、方向一致性判断（AI 与用户方向相反 → stance 低分）
- 反哺画像：像 → 置信度升；不像 → 置信度降 + 生成验证盲区 gap
- 幂等：同 topic 验证盲区不重复生成
- 降级：画像条目不足时返回空场景不崩溃
"""

import os
import sys
import tempfile
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.models import SourceRef
from deskmate.cognitive.validation_engine import (
    get_validation_engine,
    run_validation,
    TuringScenarioEngine,
)


def _seed_profile(pm):
    """写入高置信度认知画像（立场/价值观）。"""
    pm.add_stance(
        "史料真实性与创作",
        "重视历史小说中的史实基础，以历史人物/事件为锚点展开叙事，尊重历史严肃性。",
        SourceRef(type="vault", title="v1", excerpt="史实是创作的前提"),
        tags=["立场", "历史观", "严肃"],
        category="立场",
    )
    pm.add_stance(
        "创作主题",
        "每部作品必须有明确的主题内核，不能只是情节堆砌。",
        SourceRef(type="vault", title="v2", excerpt="作品必须有主题"),
        tags=["立场", "创作观"],
        category="立场",
    )
    pm.add_stance(
        "敬重的作家",
        "最敬重的前辈作家是武侠小说家金庸、梁羽生、古龙。",
        SourceRef(type="vault", title="v3", excerpt="金庸梁羽生古龙"),
        tags=["立场", "作家"],
        category="立场",
    )
    # 价值观排序
    pm.add_stance(
        "价值观：创作优先",
        "史料真实 > 叙事流畅 > 戏剧效果，冲突时守住史料真实。",
        SourceRef(type="vault", title="v4", excerpt="史料真实最高"),
        tags=["价值观", "排序"],
        category="价值观",
    )
    # 提升置信度到 0.7+（模拟多来源累积，和真实库一致）
    p = pm.get()
    for s in p.stances:
        s.add_source(SourceRef(type="vault", title="extra1", excerpt="补充来源1"))
        s.add_source(SourceRef(type="vault", title="extra2", excerpt="补充来源2"))
        s.confidence = 0.8
    pm.save()
    return pm


def _fresh_manager():
    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "test.db")
    pm = CognitiveProfileManager(profile_path=db)
    _seed_profile(pm)
    return pm, tmp


def test_generate_scenario_from_high_confidence():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    assert scenario["id"]
    assert scenario["related_items"]
    # 场景是"没遇到过"的假设情境（含"约稿/机会"等）
    assert scenario["scenario"]
    assert "？" in scenario["question"] or "?" in scenario["question"]
    # 引用的条目必须是高置信度
    assert scenario["source_item"]["confidence"] >= 0.7


def test_generate_scenario_empty_profile_fail_open():
    engine = get_validation_engine()
    scenario = engine.generate_scenario(None)
    assert scenario["id"] == ""
    assert scenario["scenario"] == ""


def test_evaluate_direction_mismatch_low_stance():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    ev = engine.evaluate_similarity(
        pm.get(),
        scenario["id"],
        "我会改。稿费翻倍为什么不改？商业价值第一。",       # AI 违背画像
        "不改。史料真实性是前提，不能为讨好读者扭曲结局。",   # 用户符合画像
        use_llm=False,  # 规则引擎方向逻辑
    )
    assert ev["overall_score"] < 0.5
    assert ev["dimension_scores"]["stance"] < 0.3
    # 方向相反 → 有分歧点
    assert any("相反" in d for d in ev["divergence_points"])


def test_evaluate_direction_match_high():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    ev = engine.evaluate_similarity(
        pm.get(),
        scenario["id"],
        "不改。史料就是史料，守住创作底线，不能为商业扭曲历史。",
        "不改。真实比稿费重要，尊重史实是我的底线。",
        use_llm=False,
    )
    # 方向一致 → stance 不低
    assert ev["dimension_scores"]["stance"] >= 0.5
    assert not any("相反" in d for d in ev["divergence_points"])


def test_apply_feedback_downgrade_and_gap():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    item_id = scenario["related_items"][0]
    before = next(it for it in pm.get().stances if it.id == item_id).confidence

    ev = engine.evaluate_similarity(
        pm.get(),
        scenario["id"],
        "我会改。稿费翻倍为什么不改？商业价值第一。",
        "不改。史料真实性是前提。",
        use_llm=False,
    )
    ev["related_items"] = scenario["related_items"]
    ev["scenario"] = scenario["scenario"]
    engine.apply_feedback(pm.get(), ev)
    pm.save()

    after = next(it for it in pm.get().stances if it.id == item_id).confidence
    assert after < before  # 置信度降了
    gaps = [g for g in pm.get().cognitive_gaps if g.status == "open" and "验证盲区" in g.topic]
    assert len(gaps) >= 1  # 生成验证盲区


def test_apply_feedback_idempotent_gap():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    ev = {
        "overall_score": 0.3,
        "dimension_scores": {"stance": 0.1, "argument": 0.5, "value": 0.4, "expression": 0.5},
        "divergence_points": ["x"],
        "related_items": scenario["related_items"],
        "scenario": scenario["scenario"],
    }
    engine.apply_feedback(pm.get(), ev)
    engine.apply_feedback(pm.get(), ev)  # 第二次
    pm.save()
    gaps = [g for g in pm.get().cognitive_gaps if g.status == "open" and "验证盲区" in g.topic]
    assert len(gaps) == 1  # 幂等，不重复


def test_apply_feedback_upgrade_confidence():
    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    item_id = scenario["related_items"][0]
    before = next(it for it in pm.get().stances if it.id == item_id).confidence

    ev = {
        "overall_score": 0.85,
        "dimension_scores": {"stance": 0.9, "argument": 0.8, "value": 0.85, "expression": 0.8},
        "divergence_points": [],
        "related_items": scenario["related_items"],
        "scenario": scenario["scenario"],
    }
    engine.apply_feedback(pm.get(), ev)
    pm.save()

    after = next(it for it in pm.get().stances if it.id == item_id).confidence
    assert after > before  # 置信度升了


def test_evaluate_uses_llm_when_available(monkeypatch):
    """LLM 可用且返回合法 JSON → 走 LLM 评估（_engine=llm）。"""
    monkeypatch.setattr(
        "deskmate.memory.llm.chat_memory",
        lambda *a, **k: json.dumps({
            "overall_score": 0.82,
            "dimension_scores": {"stance": 0.85, "argument": 0.8, "value": 0.78, "expression": 0.75},
            "divergence_points": [],
            "feedback": "AI 的回答与你高度一致。",
        }, ensure_ascii=False),
    )
    monkeypatch.setattr(
        "deskmate.memory.llm.memory_llm_available",
        lambda *a, **k: True,
    )

    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    ev = engine.evaluate_similarity(
        pm.get(), scenario["id"],
        "不改。史料是底线，不能为商业扭曲历史结局。",
        "不改。真实比稿费重要，尊重史实。",
        related_items=scenario["related_items"],
    )
    assert ev["_engine"] == "llm"
    assert ev["overall_score"] == 0.82
    assert ev["dimension_scores"]["stance"] == 0.85


def test_evaluate_falls_back_to_rules_when_llm_fails(monkeypatch):
    """LLM 不可用 / 抛异常 → 静默降级到规则引擎（_engine=rules）。"""
    def boom(*a, **k):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", boom)
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda *a, **k: True)

    pm, tmp = _fresh_manager()
    engine = get_validation_engine()
    scenario = engine.generate_scenario(pm.get())
    ev = engine.evaluate_similarity(
        pm.get(), scenario["id"],
        "我会改。稿费翻倍为什么不改？商业价值第一。",
        "不改。史料真实性是前提。",
        related_items=scenario["related_items"],
    )
    assert ev["_engine"] == "rules"
    assert ev["dimension_scores"]["stance"] < 0.3  # 规则仍能判方向相反


# ---------------------------------------------------------------------------
# 回归：反哺必须落在「用户真正作答」的那条画像上
# ---------------------------------------------------------------------------

def test_run_validation_feedback_targets_the_answered_scenario():
    """评估阶段必须复用作答时的情境，不能重新随机挑一条。

    回归背景：run_validation 曾在评估前重新 generate_scenario，用户答的是 A 条目、
    反哺改的却是 B 条目 —— 置信度朝错误方向调，还会凭空生出「验证盲区」gap。
    """
    pm, tmp = _fresh_manager()
    db = str(Path(tmp) / "test.db")
    profile = pm.get()
    engine = get_validation_engine()

    # 默认情境会选中一条；刻意挑另一条作为「用户作答的那条」
    default_scenario = engine.generate_scenario(profile)
    target = next(s for s in profile.stances if s.id not in default_scenario["related_items"])
    others = [s for s in profile.stances if s.id != target.id]
    before_target = target.confidence
    before_others = {s.id: s.confidence for s in others}

    r = run_validation(
        profile_path=db,
        ai_response="我会改。稿费翻倍为什么不改？商业价值第一。",
        user_response="不改。史料真实性是前提，不能为讨好读者扭曲结局。",
        scenario_id="scenario-under-test",
        related_items=[target.id],
        scenario_text="假如有平台开出三倍稿费，要求改掉结局。",
    )

    assert r["applied"] is True
    # 还原的是作答时的情境，不是重新生成的
    assert r["scenario"]["id"] == "scenario-under-test"
    assert r["scenario"]["related_items"] == [target.id]

    after = CognitiveProfileManager(profile_path=db).get()
    target_after = next(s for s in after.stances if s.id == target.id)
    assert target_after.confidence < before_target, "被作答的那条应当降置信度（回答与画像不符）"
    for s in after.stances:
        if s.id in before_others:
            assert s.confidence == before_others[s.id], f"未被作答的条目不应被改动：{s.topic}"


def test_run_validation_falls_back_when_scenario_missing():
    """前端未回传情境（旧客户端）时回退到重新生成，不崩溃。"""
    pm, tmp = _fresh_manager()
    db = str(Path(tmp) / "test.db")
    r = run_validation(
        profile_path=db,
        ai_response="不改。史料是底线。",
        user_response="不改。真实比稿费重要。",
    )
    assert r["applied"] is True
    assert r["scenario"]["id"]


def test_run_validation_falls_back_when_items_stale():
    """回传的条目已不在画像中（被清理）时回退重新生成，不静默写坏数据。"""
    pm, tmp = _fresh_manager()
    db = str(Path(tmp) / "test.db")
    r = run_validation(
        profile_path=db,
        ai_response="不改。史料是底线。",
        user_response="不改。真实比稿费重要。",
        scenario_id="stale-scenario",
        related_items=["no-such-item-id"],
        scenario_text="一个早已不存在的情境",
    )
    assert r["applied"] is True
    # 已回退：重新生成的情境有自己的 id，不会沿用失效的那个
    assert r["scenario"]["id"] != "stale-scenario"
