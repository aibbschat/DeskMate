"""开脑洞形态回归（2026-11-08 晚）：默认发散，作者挑中才收敛。

萧哥原话：「主要是用来开脑洞的，打开我的写作思路」；11-06 也说过
「不需要验审，不需要写大纲，光提供各种脑洞思路」。而 16:57 专家那轮的
实际产出是「三条野路子 → 逐条过审 → 我认哪条 → 落笔建议」的 2158 字方案，
方向正好相反。本组锁住：

- 人设默认档 = 开脑洞：条数不设死、允许半成品、火花阶段不审/不排序/
  不替作者挑/不写分层方案；
- 往下盘只在作者挑中或点名要方案之后：盘局势、查伏笔、算代价，动作过三问
  （谁有权 / 对手怎么破 / 里面的人凭什么）；
- 「下一步怎么办」不等于要方案——先给可能性，不交一二三步；
- 「脑洞大开 / 先读原稿」不再被误判成深挖（该走默认开脑洞档）；
- 群聊 guidance、solo lead、第 2 轮接话 lead 都不得把开脑洞收敛成方案。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.bots import discussion, runtime

_PERSONA = Path("coworker/personas/builtin/brainstormer.md")
# 2026-11-11 B 方案：脑洞专家转 OPE-58 bundle，人设正文原样搬进 manifest.md。
_PERSONA_BUNDLE = Path("coworker/personas/builtin/brainstormer/manifest.md")


def _persona_text() -> str:
    p = _PERSONA if _PERSONA.is_file() else _PERSONA_BUNDLE
    return p.read_text(encoding="utf-8")


def _skill_text() -> str:
    return Path(
        "coworker/personas/builtin/brainstormer/skills/brainstormer-method/SKILL.md"
    ).read_text(encoding="utf-8")


# --------------------------------------------------------------------- 人设

def test_persona_defaults_to_divergent_brainstorm() -> None:
    text = _persona_text()
    assert "开脑洞（默认）" in text
    assert "往下盘" in text
    # 发散档的硬约束（2026-11-22：不设条数，避免把火花写成配额）
    assert "条数不设死" in text
    assert "允许半成品" in text
    assert "不排序、不验收、不替作者挑" in text
    assert "不写成分层方案" in text


def test_persona_converges_only_after_author_picks() -> None:
    text = _persona_text()
    assert "就这条" in text and "才进这个档" in text
    # 往下盘的三问：谁有权 / 对手怎么破 / 里面的人凭什么
    assert "谁有权让它发生" in text
    assert "对手会怎么破" in text
    assert "人物凭什么这么做" in text


def test_persona_still_keeps_reading_and_honesty_red_lines() -> None:
    text = _persona_text()
    assert "默认只读正文" in text
    assert "先找停笔点" in text
    assert "本次未加载" in text
    assert "钉死出处，绝不编造" in text
    assert "半空的冒充成正文" in text


def test_spark_phase_has_no_quota_or_self_audit() -> None:
    """火花档不设条数、不在生成时自审——把「正确的废话」挡在门外。"""
    text = _persona_text()
    skill = _skill_text()
    assert "条数不设死" in text and "条数不设死" in skill
    assert "通常 2-4" not in text
    assert "哪里可能塌" not in skill
    assert "火花阶段不审" in skill
    assert "判据只有一条" in skill


def test_next_step_question_does_not_trigger_plan() -> None:
    """作者问「下一步怎么办」不是要方案；先给可能性，不交一二三步。"""
    text = _persona_text()
    skill = _skill_text()
    assert "下一步怎么办" in text
    assert "不等于要方案" in text and "不等于要方案" in skill
    assert "不交一二三步" in text


def test_down_gear_needs_explicit_plan_request() -> None:
    text = _persona_text()
    assert "给方案" in text and "写大纲" in text
    assert "才进这个档" in text


def test_memory_does_not_recycle_old_sparks() -> None:
    text = _persona_text()
    assert "旧判断、旧火花只当参考" in text


# --------------------------------------------------------------------- 群聊形态

def test_group_guidance_does_not_converge_brainstorm_rounds() -> None:
    guidance = runtime._GROUP_GUIDANCE
    assert "要脑洞/方向/灵感，就给不同可能" in guidance
    assert "别替作者拍板" in guidance
    assert "作者明确说" in guidance and "给方案/写大纲" in guidance
    assert "别把大家的话题收敛成一个方案" not in guidance  # 这句在第 2 轮 lead


def test_round_two_lead_allows_brainstorm_branching() -> None:
    p = runtime.build_prompt("给几个方向", None, round_index=2)
    assert "别急着把话题收成一个方案" in p
    assert "可以抛一条完全不同的" in p

def test_solo_guidance_keeps_brainstorm_short() -> None:
    solo = runtime._SOLO_GUIDANCE
    assert "要火花/方向/碰撞" in solo
    assert "他明确要你盘深" in solo


def test_deep_lead_is_still_the_convergent_gear() -> None:
    p = runtime.build_prompt("深挖一下烧仓的完整方案", None, deep=True)
    assert "深挖任务" in p
    assert "盘局势" in p or "盘清局势" in p
    assert "自己裂开" in p


@pytest.mark.parametrize(
    "text",
    [
        "我需要脑洞大开的安排和设计",
        "给我几个方向，打开一下思路",
    ],
)
def test_brainstorm_requests_are_not_deep_gear(text: str) -> None:
    assert discussion.is_deep_dig(text) is False


# --------------------------------------------------------------------- 决策 A：入口收口

def test_brainstormer_in_group_roster_and_expert() -> None:
    """半山书院自由拉群：brainstormer 标了 group_chat ⇒ 可进群名册；
    registry 仍解析得到它，独立专家入口（开脑洞 1v1 引导）保留。"""
    from coworker.personas.registry import get_registry
    from deskmate.bots.roster import list_bots

    ids = {b.id for b in list_bots()}
    assert "brainstormer" in ids, "半山书院自由拉群后，脑洞搭档应可入群"
    assert get_registry().get("brainstormer") is not None, "专家入口仍须能解析到它"


def test_bot_group_page_points_brainstorm_to_expert() -> None:
    """Bot 群页面要主动引导：开脑洞去「专家」找脑洞搭档，而不是在群里找。"""
    from deskmate.bots import web as bots_web

    page = bots_web.PAGE
    assert "脑洞搭档" in page
    assert "开脑洞请走" in page
    assert "Bot 群适合碰撞、审稿、调度" in page or "这里适合碰撞、审稿、调度" in page
