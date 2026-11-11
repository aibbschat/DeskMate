"""开脑洞形态回归（2026-11-08 晚）：默认发散，作者挑中才收敛。

萧哥原话：「主要是用来开脑洞的，打开我的写作思路」；11-06 也说过
「不需要验审，不需要写大纲，光提供各种脑洞思路」。而 16:57 专家那轮的
实际产出是「三条野路子 → 逐条过审 → 我认哪条 → 落笔建议」的 2158 字方案，
方向正好相反。本组锁住：

- 人设默认档 = 开脑洞：4~6 条真不同、允许半成品、不排序/不替作者挑/不写
  分层方案或大纲；
- 往下盘只在作者挑中之后：盘局势、查伏笔、算代价，动作过三问（谁有权 /
  对手怎么破 / 里面的人凭什么）；
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


# --------------------------------------------------------------------- 人设

def test_persona_defaults_to_divergent_brainstorm() -> None:
    text = _persona_text()
    assert "开脑洞（默认）" in text
    assert "往下盘" in text
    # 发散档的四条硬约束
    assert "4~6 条真正不同的" in text
    assert "允许半成品" in text
    assert "不排序、不验收、不替作者挑" in text
    assert "不写成分层方案" in text


def test_persona_converges_only_after_author_picks() -> None:
    text = _persona_text()
    assert "就这条" in text and "才进这个档" in text
    # 往下盘的三问：谁有权 / 对手怎么破 / 里面的人凭什么
    assert "谁有权让它发生" in text
    assert "对手会怎么破" in text
    assert "凭什么照你说的做" in text


def test_persona_still_keeps_reading_and_honesty_red_lines() -> None:
    text = _persona_text()
    assert "默认只读正文" in text
    assert "先找停笔点" in text
    assert "本次未加载" in text
    assert "钉死出处，绝不编造" in text
    assert "半空的冒充成正文" in text


# --------------------------------------------------------------------- 群聊形态

def test_group_guidance_does_not_converge_brainstorm_rounds() -> None:
    guidance = runtime._GROUP_GUIDANCE
    assert "先分清他要的是脑洞还是方案" in guidance
    assert "不排序、不替他挑" in guidance
    assert "谁有权让它发生" in guidance
    assert "别把大家的话题收敛成一个方案" not in guidance  # 这句在第 2 轮 lead


def test_round_two_lead_allows_brainstorm_branching() -> None:
    p = runtime.build_prompt("给几个方向", None, round_index=2)
    assert "别把大家的话题收敛成一个方案" in p
    assert "也可以抛一条完全不同的" in p


def test_solo_guidance_keeps_brainstorm_short() -> None:
    solo = runtime._SOLO_GUIDANCE
    assert "开脑洞时" in solo
    assert "往下盘时" in solo


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

def test_brainstormer_is_expert_only_not_in_group_roster() -> None:
    """2026-11-08 决策 A：开脑洞固定在独立专家，不再进 Bot 群名册。

    persona 已按 §24 去掉 roundtable 标记 ⇒ roster.list_bots() 不含它；
    但 registry 仍要能解析到它（专家会话靠这个）。
    """
    from coworker.personas.registry import get_registry
    from deskmate.bots.roster import list_bots

    ids = {b.id for b in list_bots()}
    assert "brainstormer" not in ids, "脑洞专家不应出现在 Bot 群名册"
    assert get_registry().get("brainstormer") is not None, "专家入口仍须能解析到它"


def test_bot_group_page_points_brainstorm_to_expert() -> None:
    """Bot 群页面要主动引导：开脑洞去「专家」找创意脑洞专家，而不是在群里找。"""
    from deskmate.bots import web as bots_web

    page = bots_web.PAGE
    assert "创意脑洞专家" in page
    assert "开脑洞请走" in page
    assert "Bot 群适合碰撞、审稿、调度" in page or "这里适合碰撞、审稿、调度" in page
