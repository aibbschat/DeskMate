"""「她真的记着」端到端回归（2026-11-15 萧哥提问）。

萧哥的例子：上周五头痛、进度没完成，跟小桔说了；周末又把进度补回来了；
只隔一天再聊天时，如果她真记着，应该能自然接上「今天还头痛吗」。

这不是「半小时了休息一下」那种时间卡（``companion.py`` 的 30/60/90/120
分钟阈值 + 模板），而是两件不同的事：前者是系统按时推一条内容，后者是
**他亲口说过的、还没了结的事，在她下一轮回复里在场**。

所以要守的不是「有没有弹卡」，是这条链的四段：
    ① 用户原话 → 逐字引证地抽成「近况事件」（不许编）
    ② 新信息能把旧的未了结事件**闭合**（周末补回 → 关闭周五欠账）
    ③ 闲聊轮也不整块丢「她的理解/立场/作品硬锚」与「他这几天说起的」
    ④ event 只作为**真实状态**注入，不含任何规定表达方式的话术

先红后绿：本文件在修复前应当全红（模块缺失 / 闲聊轮丢块）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

CHITCHAT = "早"  # is_light_turn 的 _GREETING_ONLY 白名单，纯寒暄轮


def _episodes():
    try:
        from deskmate.memory import user_episodes
    except ImportError as exc:  # 修复前：整条链根本不存在
        pytest.fail(f"连续记忆模块缺失（「记着」链路没有承载）：{exc}")
    return user_episodes


def _seed_beliefs(base_dir: Path) -> None:
    """让 world 块至少产出「理解」段 + 作品硬锚，不依赖真实库。"""
    (base_dir / "agency").mkdir(parents=True, exist_ok=True)
    (base_dir / "agency" / "observation_understanding.jsonl").write_text(
        json.dumps(
            {"ts": datetime.now().timestamp(), "body": "她对这件事的理解：结构比事件更值得看。"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _manager(base_dir: Path, monkeypatch):
    from deskmate.memory.manager import DeskMateMemoryManager

    monkeypatch.setenv("DESKMATE_WEATHER", "0")
    monkeypatch.setenv("DESKMATE_EMOTION_ENGINE", "0")
    monkeypatch.setenv("DESKMATE_CONTEXT_TOKEN_BUDGET", "100000")
    return DeskMateMemoryManager(base_dir=base_dir)


# ---------------------------------------------------------------------------
# 一、闲聊轮：不能再整块丢掉「她是谁」
# ---------------------------------------------------------------------------
def test_chitchat_turn_keeps_her_beliefs_and_works_anchor(tmp_path: Path, monkeypatch):
    """「早」这种纯寒暄轮，她的理解 / 立场 / 防虚构硬锚也得在场。"""
    _seed_beliefs(tmp_path)
    ctx = _manager(tmp_path, monkeypatch).build_full_context_for_turn(CHITCHAT)

    assert "她对世界的理解" in ctx, "闲聊轮把她的理解整块丢了"
    assert "萧盛真实作品事实" in ctx, "防虚构闸门不能被闲聊轮裁掉"


def test_chitchat_turn_world_block_is_compact(tmp_path: Path, monkeypatch):
    """点亮 ≠ 把 4500 字剪报全灌回来：闲聊轮必须走精简配额。"""
    _seed_beliefs(tmp_path)
    ctx = _manager(tmp_path, monkeypatch).build_full_context_for_turn(CHITCHAT)
    anchor_at = ctx.find("萧盛真实作品事实")
    assert anchor_at >= 0
    assert anchor_at < 1400, f"硬锚被推到 {anchor_at} 字符，闲聊轮 cap_light 会切掉它"


# ---------------------------------------------------------------------------
# 二、「他这几天亲口说起的」：记住、闭合、闲聊轮带出
# ---------------------------------------------------------------------------
def _day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


def test_unfinished_episode_survives_chitchat_turn(tmp_path: Path, monkeypatch):
    """隔一天纯寒暄，他上周说的头痛仍应作为真实状态在场。"""
    ep = _episodes()
    said = "今天头有点痛，这周的进度就先放着吧"
    ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛，进度先放下", "status": "open", "evidence": said},
    ], source_text=said, day=_day(1))

    ctx = _manager(tmp_path, monkeypatch).build_full_context_for_turn(CHITCHAT)
    assert "头有点痛" in ctx, "他说过的事没有抵达闲聊轮的表达层"
    assert "还没了结" in ctx or "没再提" in ctx, "只给状态，别写成待办或话术"


def test_resolution_closes_matching_open_episode(tmp_path: Path):
    """周末补回进度 → 关闭周五欠账；头痛没说好，仍是 open。"""
    ep = _episodes()
    ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛", "status": "open", "evidence": "今天头有点痛"},
        {"subject": "本周码字进度", "state": "周五进度没完成", "status": "open",
         "evidence": "周五的进度没完成"},
    ], source_text="今天头有点痛，周五的进度没完成", day=_day(3))

    later = "周末把落下的进度补回来了"
    out = ep.record(tmp_path, [
        {"subject": "本周码字进度", "state": "进度已补回", "status": "resolved",
         "evidence": later},
    ], source_text=later, day=_day(1))

    items = {i["subject"]: i for i in ep.read_items(tmp_path)}
    assert out["closed"] == 1
    assert items["本周码字进度"]["status"] == "resolved"
    assert "补回来了" in items["本周码字进度"]["resolution"]
    assert items["头痛"]["status"] == "open", "补进度不等于头不痛了，不许连带闭合"


def test_subject_drift_still_closes_via_open_subject_anchor(tmp_path: Path):
    """真实 LLM 实测回归：周五抽「周五进度」，周末说成「码字进度」。

    跨轮 subject 用词不稳定，只靠 subject 精确匹配会闭不上——旧欠账永远挂着，
    还多出一条 resolved。抽取器把当前 open 清单回喂给 LLM，由它给 closes
    指回清单里的原 subject；这是语义锚定，不是关键词表。
    """
    ep = _episodes()
    ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛", "status": "open", "evidence": "今天头有点痛"},
        {"subject": "周五进度", "state": "周五没完成", "status": "open",
         "evidence": "周五的进度没完成，周末再补吧"},
    ], source_text="今天头有点痛，周五的进度没完成，周末再补吧", day=_day(3))

    later = "周末把落下的进度补回来了"
    out = ep.record(tmp_path, [
        {"subject": "码字进度", "state": "已补回", "status": "resolved",
         "evidence": later, "closes": "周五进度"},
    ], source_text=later, day=_day(1))

    items = {i["subject"]: i for i in ep.read_items(tmp_path)}
    assert out["closed"] == 1
    assert items["周五进度"]["status"] == "resolved"
    assert "码字进度" not in items, "闭合了旧事就不该再新建一条同义事件"
    assert items["头痛"]["status"] == "open", "closes 只准指名闭合它点的那件"


def test_both_open_and_resolved_reach_context(tmp_path: Path, monkeypatch):
    """她既看得到「没再提的头痛」，也看得到「已经补回的进度」。"""
    ep = _episodes()
    ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛", "status": "open", "evidence": "今天头有点痛"},
        {"subject": "本周码字进度", "state": "周五没完成", "status": "open",
         "evidence": "周五的进度没完成"},
    ], source_text="今天头有点痛，周五的进度没完成", day=_day(3))
    ep.record(tmp_path, [
        {"subject": "本周码字进度", "state": "已补回", "status": "resolved",
         "evidence": "周末把进度补回来了"},
    ], source_text="周末把进度补回来了", day=_day(1))

    ctx = _manager(tmp_path, monkeypatch).build_full_context_for_turn(CHITCHAT)
    assert "头有点痛" in ctx
    assert "补回来了" in ctx, "闭合证据也要在场，否则她会拿旧状态追问"


def test_verbatim_evidence_required(tmp_path: Path):
    """evidence 必须是他原话的逐字片段——不许替他编一句「近况」。"""
    ep = _episodes()
    out = ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛", "status": "open",
         "evidence": "他昨天头痛得厉害"},  # 原话里没有这句
    ], source_text="今天头有点痛", day=_day(1))

    assert out["written"] == 0
    assert ep.read_items(tmp_path) == []


def test_no_episode_means_no_scripted_care(tmp_path: Path):
    """没有真实近况事件时注入为空：绝不退化成又一张模板关心卡。"""
    ep = _episodes()
    assert ep.build_context(tmp_path) == ""
    assert not any(k in ep.build_context(tmp_path) for k in ("休息一下", "要不要喝口水"))


def test_stale_episodes_expire(tmp_path: Path):
    """十天前的旧事不再每轮挂在上下文里；不是无限期骚扰。"""
    ep = _episodes()
    said = "那天有点头痛"
    ep.record(tmp_path, [
        {"subject": "头痛", "state": "他说头痛", "status": "open", "evidence": said},
    ], source_text=said, day=_day(20))
    assert ep.build_context(tmp_path) == ""


# ---------------------------------------------------------------------------
# 三、抽取分流：episode 绝不落 USER.md / MEMORY.md（归属闸红线）
# ---------------------------------------------------------------------------
def test_extractor_routes_episode_away_from_user_md(tmp_path: Path, monkeypatch):
    """近况是暂时状态，不是持久事实——不能混进画像与长期记忆。"""
    import deskmate.memory.manager as manager_mod

    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    session_file = sessions / "1.md"
    session_file.write_text(
        "## User\n今天头有点痛，周五的进度没完成，周末补吧\n\n## Assistant\n头疼就别纠结进度\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager_mod, "memory_llm_available", lambda: True)
    monkeypatch.setattr(
        manager_mod, "chat_memory",
        lambda *a, **k: json.dumps([
            {"category": "episode", "text": "他说头痛、周五进度没完成",
             "subject": "头痛", "state": "他说头痛", "status": "open",
             "evidence": "今天头有点痛"},
        ], ensure_ascii=False),
    )
    mgr = manager_mod.DeskMateMemoryManager(base_dir=tmp_path)
    out = mgr._extract_single_session(session_file)

    assert out.get("episodes") == 1
    for name in ("USER.md", "MEMORY.md"):
        p = tmp_path / name
        assert not (p.exists() and "episode" in p.read_text(encoding="utf-8")), \
            f"近况事件写进了 {name}，踩 USER.md 归属闸"
    ep = _episodes()
    assert [i["subject"] for i in ep.read_items(tmp_path)] == ["头痛"]
