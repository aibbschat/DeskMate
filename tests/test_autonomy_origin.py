"""任务问询/汇报落回「接任务的那个对话」，且不阻塞（先红后绿）。

萧哥 2026-11-05 口径：
「她在会话里建了任务，那她所有的汇报、疑问、要权限都要落在对话里等我回复；
  重要的我暂时没答，她先去执行不需要审批的部分，并定时在对话里报进度。
  不是我去找入口，是她来找我。」

本文件锁六件事：
1. 目标制任务有 origin 会话时，ask_user 落**原对话**（inline 卡 + 对话消息），且不阻塞；
2. 原对话里答过的问题，下一轮 opening 带上答案并清掉 pending；
3. 还没答 → next round 提醒「先做别的、别重复问」；
4. 静默轮按周期往原对话报进度；结果送达时也往原对话发一份；
5. 问了问题的轮次是 needs_you，不当作结果投递；
6. 没有 origin 的（GUI 建的任务）保留原阻塞 Inbox 行为；普通任务不受影响。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from types import SimpleNamespace

from coworker.automation.models import Schedule, ScheduledTask, TaskRun
from coworker.sessions import SessionRecord

ORIGIN = "origin-sess-1"


def _real_task(origin: str = ORIGIN, **kw) -> ScheduledTask:
    base = dict(
        title="汇报回原对话",
        instructions="盯住目标",
        schedule=Schedule(kind="cron", cron="*/10 * * * *"),
        workspace="/tmp/cw-origin",
        agent="cowork",
        goal_mode="watch",
        notify_on_completion=False,
        notify_target=None,
        origin_session_id=origin,
        origin_surface="cowork",
    )
    base.update(kw)
    return ScheduledTask(**base)


def _origin_text(mgr) -> str:
    rec = mgr.session_store.load(ORIGIN)
    return json.dumps(rec.messages if rec else [], ensure_ascii=False)


def _mgr(tmp_path, monkeypatch):
    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action
    from coworker.server.manager import SessionManager

    original = delivery_mod.deliver_run_result
    monkeypatch.setattr(delivery_mod, "deliver_run_result",
        lambda task, run, *, base_dir=None: original(task, run, base_dir=tmp_path))
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    mgr = SessionManager(data_dir=tmp_path / "data")
    mgr._maybe_autotitle = lambda sid: None
    mgr.session_store.save(SessionRecord(
        session_id=ORIGIN, workspace=str(tmp_path), model=mgr.model,
        mode="bypass", agent="cowork",
        messages=[{"role": "user", "content": "帮我建一个盯梢任务"}],
    ))
    return mgr


class ScriptedEngine:
    def __init__(self, steps, openings, *, mgr=None, session_id="", task=None):
        self._steps = steps
        self._openings = openings
        self._mgr = mgr
        self._session_id = session_id
        self._task = task
        self.messages: list[dict] = []

    async def run(self, opening):
        self._openings.append(opening)
        step = self._steps.pop(0) if self._steps else {"text": ""}
        if step.get("ask"):
            asker = self._mgr._task_question_asker(
                self._session_id, "cowork", task=self._task
            )
            await asker({"question": step["ask"], "options": ["A账号", "B账号"]})
        if step.get("raise"):
            raise RuntimeError("boom")
        self.messages.append({"role": "assistant", "content": step.get("text", "")})
        yield SimpleNamespace(
            type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 1}
        )


def _patch_engine(monkeypatch, steps, openings):
    from coworker.server.manager import SessionManager

    monkeypatch.setattr(SessionManager, "_build_task_engine",
        lambda self, task, *, session_id, messages=None: ScriptedEngine(
            steps, openings, mgr=self, session_id=session_id, task=task))


def _run(mgr, task):
    return asyncio.run(mgr._run_scheduled_task(task, "schedule"))


# -- 1. 问询落原对话，且不阻塞 ------------------------------------------------
def test_ask_lands_in_origin_conversation_without_blocking(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    asker = mgr._task_question_asker("__run__x", "cowork", task=task)

    async def go():
        return await asyncio.wait_for(
            asker({"question": "用哪个账号继续？", "options": ["A账号", "B账号"]}),
            timeout=3,
        )

    result = asyncio.run(go())
    assert result.get("answer") == "" and result.get("error"), "有 origin 会话时不能原地阻塞等人"
    assert "continue" in result["error"].lower()
    items = mgr.inbox.pending(session_id=ORIGIN)
    assert items and items[0].kind == "question", "问人卡没落在原会话"
    assert items[0].visibility == "inline", "不该塞进全局 Inbox 让用户到处找"
    text = _origin_text(mgr)
    assert "用哪个账号继续" in text, "原对话里没留下这条问询"


# -- 2/3. 下一轮带上答案或提醒 ------------------------------------------------
def test_answered_origin_question_is_injected_next_round(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    item = mgr.inbox.add_question(
        ORIGIN, "用哪个账号继续？", options=["A账号", "B账号"], visibility="inline"
    )
    mgr.inbox.resolve(item.id, "A账号", by="test")
    prior = TaskRun(
        task_id=task.id, status="needs_you", finished_at=time.time() - 60,
        checkpoint={"goal": {"state": "hash=x", "done": False, "rounds": 1,
                             "pending_question": {"item_id": item.id, "title": "用哪个账号继续？"}}},
    )
    mgr.task_store.add_run(prior)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"text": "收到 A账号，继续执行"}], openings)

    run = _run(mgr, task)
    assert "A账号" in openings[0], "原对话里的答案没带回下一轮"
    assert "pending_question" not in run.checkpoint["goal"], "答过的问题要清掉，别反复用"


def test_unanswered_origin_question_reminds_but_does_not_repeat(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    item = mgr.inbox.add_question(ORIGIN, "用哪个账号继续？", options=["A账号"], visibility="inline")
    prior = TaskRun(
        task_id=task.id, status="needs_you", finished_at=time.time() - 60,
        checkpoint={"goal": {"state": "hash=x", "done": False, "rounds": 1,
                             "pending_question": {"item_id": item.id, "title": "用哪个账号继续？"}}},
    )
    mgr.task_store.add_run(prior)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"text": "[[NO_CHANGE]]\n[[STATE]]hash=x 等回复"}], openings)

    run = _run(mgr, task)
    assert "没答" in openings[0] or "还没" in openings[0], "没提醒「问题还没答」"
    assert "别重复" in openings[0] or "不要重复" in openings[0]
    assert "pending_question" in run.checkpoint["goal"], "没答的问题要留着等下一轮"


# -- 4. 进度/结果回原对话 ------------------------------------------------------
def test_quiet_round_reports_progress_to_origin(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    monkeypatch.setenv("COWORKER_TASK_PROGRESS_EVERY", "1")
    task = _real_task()
    mgr.task_store.save(task)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"text": "[[NO_CHANGE]]\n[[STATE]]hash=x 仍未变"}], openings)

    run = _run(mgr, task)
    assert run.delivered_at is None, "静默轮不该投台账"
    assert "盯梢进度" in _origin_text(mgr), "静默轮没在原对话里报进度"


def test_result_is_reported_to_origin_conversation(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"text": "目标变成 READY 了，报告结果"}], openings)

    run = _run(mgr, task)
    assert run.delivered_at is not None
    text = _origin_text(mgr)
    assert "目标变成 READY 了" in text, "结果没回原对话，用户还得去别处找"


# -- 5. 问了问题的轮次是 needs_you，不是结果 --------------------------------
def test_round_with_question_is_needs_you_not_result(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"ask": "要不要继续等？", "text": "我先做不依赖它的部分"}], openings)

    run = _run(mgr, task)
    assert run.status == "needs_you", "问了问题的轮次不能当完成"
    assert run.delivered_at is None, "问询不是结果，不投台账"
    assert run.checkpoint["goal"].get("pending_question"), "问题没记进检查点，下轮接不上"


# -- 6. 无 origin / 普通任务 --------------------------------------------------
def test_no_origin_falls_back_to_blocking_inbox_asker(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task(origin="")
    asker = mgr._task_question_asker("__run__y", "cowork", task=task)

    async def scenario():
        async def answer_later():
            for _ in range(300):
                pending = mgr.inbox.pending()
                if pending:
                    mgr.inbox.resolve(pending[0].id, "B账号", by="test")
                    return
                await asyncio.sleep(0.01)

        bg = asyncio.create_task(answer_later())
        out = await asyncio.wait_for(asker({"question": "用哪个？"}), timeout=5)
        await bg
        return out

    result = asyncio.run(scenario())
    assert "B账号" in json.dumps(result, ensure_ascii=False)


def test_plain_task_has_no_origin_reports(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task(origin=ORIGIN, goal_mode="")
    mgr.task_store.save(task)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"text": "普通任务结果"}], openings)

    run = _run(mgr, task)
    assert run.delivered_at is not None
    assert "盯梢进度" not in _origin_text(mgr) and "任务结果" not in _origin_text(mgr)

# -- 7/8. 重复提问去重 + 答复确认 ---------------------------------------------
def test_repeated_question_reuses_previous_answer(tmp_path, monkeypatch):
    """同一问题问第二遍：已答就直接把答案当工具结果，不再打扰用户。"""
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    item = mgr.inbox.add_question(ORIGIN, "用哪个账号继续？", options=["A账号", "B账号"], visibility="inline")
    mgr.inbox.resolve(item.id, "A账号", by="test")
    prior = TaskRun(
        task_id=task.id, status="needs_you", finished_at=time.time() - 60,
        checkpoint={"goal": {"state": "hash=x", "done": False, "rounds": 1,
                             "pending_question": {"item_id": item.id, "title": "用哪个账号继续？"}}},
    )
    mgr.task_store.add_run(prior)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"ask": "用哪个账号继续？", "text": "收到 A账号，继续"}], openings)

    run = _run(mgr, task)
    assert mgr.inbox.pending(session_id=ORIGIN) == [], "已答过的问题不该再发一张卡"
    assert run.status != "needs_you", "答案已知就不该再判 needs_you"
    assert run.delivered_at is not None


def test_repeated_pending_question_does_not_duplicate_card(tmp_path, monkeypatch):
    """同一问题还没答时又问：不重复发卡，也不把本轮回成结果。"""
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    mgr.task_store.save(task)
    item = mgr.inbox.add_question(ORIGIN, "用哪个账号继续？", options=["A账号"], visibility="inline")
    prior = TaskRun(
        task_id=task.id, status="needs_you", finished_at=time.time() - 60,
        checkpoint={"goal": {"state": "hash=x", "done": False, "rounds": 1,
                             "pending_question": {"item_id": item.id, "title": "用哪个账号继续？"}}},
    )
    mgr.task_store.add_run(prior)
    openings: list[str] = []
    _patch_engine(monkeypatch, [{"ask": "用哪个账号继续？", "text": "我先做别的"}], openings)

    run = _run(mgr, task)
    pending = mgr.inbox.pending(session_id=ORIGIN)
    assert len(pending) == 1, "同一个问题不该出现两张卡"
    assert run.status == "needs_you"
    assert run.delivered_at is None


def test_resolving_question_acknowledges_in_conversation(tmp_path, monkeypatch):
    """用户在对话里答完卡，立刻有一句「已记录」确认，不是静默消失。"""
    mgr = _mgr(tmp_path, monkeypatch)
    task = _real_task()
    asker = mgr._task_question_asker("__run__z", "cowork", task=task)
    asyncio.run(asker({"question": "用哪个账号继续？", "options": ["A账号", "B账号"]}))
    item = mgr.inbox.pending(session_id=ORIGIN)[0]

    asyncio.run(mgr.resolve_inbox(item.id, "B账号", by="test"))
    text = _origin_text(mgr)
    assert "已记录你的回答：B账号" in text, "答完没有任何确认，用户会以为没点上"
