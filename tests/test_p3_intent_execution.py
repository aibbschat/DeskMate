"""R12 · 意图执行闭环（江河设计 §五「自主是出海口」后半程）——「你同意了，然后呢」。

覆盖（对齐 ROUND-12 指令 §四验收判据）：
1. 主路径：裁决 ``executed`` → 执行器产出 ``intent_outputs``、intent.status=done、关联 task_log；
2. 护栏·原稿：goal 命中「替你写作」红线 → no-op（不产出、不抛）；
3. 护栏·外发：goal 指向对外表达 → **绝不自动发**，只落 draft_external 草稿 + 二次人审提案；
   二次人审点头后才真发（唯一外发口）；
4. 裁决分支：``rejected`` / ``never`` **不触发执行**；
5. fail-open：LLM 异常 → 意图回退 active、不抛、lesson 仍写；
6. 注入：``build_intent_context`` 可取最近产物（与 lessons 同层）。
"""

from __future__ import annotations

import json

import pytest

from deskmate.agency.engine import AgencyEngine
from deskmate.agency.task_log import TaskStatus
from deskmate.agency.behaviors import intent_executor as ex
from deskmate.cognitive.memory_injector import build_intent_context
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENCY_INTENT_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("AGENCY_INTENT_OUTBOUND_ENABLED", raising=False)
    monkeypatch.delenv("DESKMATE_INTENT_INJECT", raising=False)
    return tmp_path


def _store(env) -> CognitiveStore:
    return CognitiveStore(db_path=env / "deskmate.db")


def _patch_llm(monkeypatch, out: str, available: bool = True):
    monkeypatch.setattr("deskmate.memory.llm.chat", lambda *a, **k: out)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: available)


def _propose(env, goal: str, motivation: str = "因为想弄清", origin: str = "长期兴趣"):
    """建一条意图并生成提案卡，返回 ``(engine, store, iid, rec)``。"""
    store = _store(env)
    iid = store.add_intent(goal, motivation, origin=origin)
    eng = AgencyEngine(base_dir=str(env))
    rec = eng.propose_from_intent(store.get_open_intents()[0])
    assert rec is not None
    return eng, store, iid, rec


_OUT = json.dumps({"kind": "synthesis", "title": "串成一条线", "body": "我把散落的记忆串起来了……"},
                  ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1) 主路径：executed → 真产出（判据 §四.1）
# ---------------------------------------------------------------------------
def test_executed_intent_produces_output(env, monkeypatch):
    eng, store, iid, rec = _propose(env, "我想把散落的记忆串成一条线")
    _patch_llm(monkeypatch, _OUT)

    assert eng.resolve_proposal(rec.id, TaskStatus.EXECUTED) is True

    outs = store.get_recent_intent_outputs(5)
    assert len(outs) == 1
    assert outs[0]["status"] == "done"
    assert outs[0]["kind"] == "synthesis"
    assert "串起来" in outs[0]["artifact"]
    assert outs[0]["intent_id"] == iid
    # 意图完成 → 离开 active
    assert store.get_intent(iid)["status"] == "done"
    # 关联 task_log（回流链通）
    got = eng.log.get(rec.id)
    assert got.extra["execution"]["executed"] is True
    assert got.extra["execution"]["output_id"] == outs[0]["id"]


# ---------------------------------------------------------------------------
# 2) 护栏·原稿（永久红线）：no-op，不产出、不抛
# ---------------------------------------------------------------------------
def test_manuscript_intent_is_noop(env):
    # 故意不 patch LLM：原稿闸门在 provider 之前，应当直接 no-op 不碰模型
    eng, store, iid, rec = _propose(env, "我想替萧盛把《国子监》的原稿正文改写一遍")

    assert eng.resolve_proposal(rec.id, TaskStatus.EXECUTED) is True

    assert store.get_recent_intent_outputs(5) == []
    assert store.get_intent(iid)["status"] == "dropped"   # 红线命中 → 丢弃该意图
    assert eng.log.get(rec.id).extra["execution"]["reason"] == "sacred_manuscript"


def test_path_guard_uses_sacred_paths(env, tmp_path):
    """显式路径走 sacred_paths.guarded_write（唯一事实源）：受保护原稿 → 命中。"""
    book = tmp_path / "国子监"
    (book / ".deskmate").mkdir(parents=True)
    manuscript = book / "正文.docx"
    manuscript.write_text("旧稿", encoding="utf-8")

    assert ex.is_manuscript_intent(f"我想改 {manuscript} 里的情节") is True
    # 非原稿路径不受影响（同目录下的小桔自有资料）
    plain = tmp_path / "notes.md"
    plain.write_text("笔记", encoding="utf-8")
    assert ex.is_manuscript_intent(f"我想整理 {plain} 里的笔记") is False


# ---------------------------------------------------------------------------
# 3) 护栏·外发：只出草稿 + 二次人审（判据 §四.3）；点头后才真发
# ---------------------------------------------------------------------------
def test_external_intent_only_makes_draft(env, monkeypatch):
    eng, store, iid, rec = _propose(env, "我想发帖说说我对算法圈的判断", origin="长期兴趣")
    sent: list = []
    monkeypatch.setattr("deskmate.connectors.aicircle.post_topic",
                        lambda *a, **k: sent.append(a))
    _patch_llm(monkeypatch, _OUT)

    assert eng.resolve_proposal(rec.id, TaskStatus.EXECUTED) is True

    assert sent == []                                     # 绝不自动外发
    outs = store.get_recent_intent_outputs(5)
    assert len(outs) == 1
    assert outs[0]["kind"] == "draft_external"
    assert outs[0]["status"] == "pending_review"
    # 二次人审提案已生成（risk=high，不在白名单 → 不会被 tick 自动跑）
    pending = [r for r in eng.log.list(limit=50, status=TaskStatus.PROPOSED)
               if r.action_type == "intent_outbound"]
    assert len(pending) == 1
    assert pending[0].risk == "high"
    assert pending[0].extra["output_id"] == outs[0]["id"]
    assert store.get_intent(iid)["status"] == "active"    # 外发未完 → 意图未 done


def test_second_approval_actually_sends(env, monkeypatch):
    eng, store, iid, rec = _propose(env, "我想发帖说说我对算法圈的判断")
    monkeypatch.setattr("deskmate.connectors.aicircle.post_topic", lambda *a, **k: None)
    _patch_llm(monkeypatch, _OUT)
    eng.resolve_proposal(rec.id, TaskStatus.EXECUTED)

    out_rec = [r for r in eng.log.list(limit=50, status=TaskStatus.PROPOSED)
               if r.action_type == "intent_outbound"][0]
    sent: list = []
    monkeypatch.setattr("deskmate.connectors.aicircle.post_topic",
                        lambda tag, title, content: sent.append((tag, title, content)))

    assert eng.resolve_proposal(out_rec.id, TaskStatus.EXECUTED) is True

    assert len(sent) == 1                                  # 你点头了 → 真发出去
    assert "串起来" in sent[0][2]
    assert store.get_recent_intent_outputs(5)[0]["status"] == "done"
    assert store.get_intent(iid)["status"] == "done"
    assert eng.log.get(out_rec.id).extra["sent"] is True


def test_outbound_disabled_by_env(env, monkeypatch):
    eng, store, iid, rec = _propose(env, "我想发帖说说我对算法圈的判断")
    monkeypatch.setattr("deskmate.connectors.aicircle.post_topic", lambda *a, **k: None)
    _patch_llm(monkeypatch, _OUT)
    eng.resolve_proposal(rec.id, TaskStatus.EXECUTED)
    out_rec = [r for r in eng.log.list(limit=50, status=TaskStatus.PROPOSED)
               if r.action_type == "intent_outbound"][0]

    sent: list = []
    monkeypatch.setattr("deskmate.connectors.aicircle.post_topic",
                        lambda *a, **k: sent.append(a))
    monkeypatch.setenv("AGENCY_INTENT_OUTBOUND_ENABLED", "0")
    eng.resolve_proposal(out_rec.id, TaskStatus.EXECUTED)

    assert sent == []                                      # 外发总闸关掉 → 不出去
    assert eng.log.get(out_rec.id).extra["send_reason"] == "outbound_disabled"


# ---------------------------------------------------------------------------
# 4) 裁决分支：rejected / never 不触发执行（判据 §四.4）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("decision", [TaskStatus.REJECTED, TaskStatus.NEVER])
def test_non_executed_decision_does_not_run(env, monkeypatch, decision):
    eng, store, iid, rec = _propose(env, "我想把散落的记忆串成一条线")
    _patch_llm(monkeypatch, _OUT)

    assert eng.resolve_proposal(rec.id, decision) is True

    assert store.get_recent_intent_outputs(5) == []
    assert store.get_intent(iid)["status"] == "active"     # 未执行，仍挂着
    # R10 教学回流照旧
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM teachings").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 5) fail-open：LLM 异常 → 回退 active、不抛、lesson 仍写（判据 §四.5）
# ---------------------------------------------------------------------------
def test_llm_error_rolls_back_and_keeps_lesson(env, monkeypatch):
    eng, store, iid, rec = _propose(env, "我想把散落的记忆串成一条线")
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("deskmate.memory.llm.chat", _boom)

    assert eng.resolve_proposal(rec.id, TaskStatus.EXECUTED) is True   # 不抛

    assert store.get_recent_intent_outputs(5) == []
    assert store.get_intent(iid)["status"] == "active"
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM teachings").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 6) 注入：build_intent_context（判据 §四.6）
# ---------------------------------------------------------------------------
def test_build_intent_context_injects_recent_outputs(env):
    store = _store(env)
    store.add_intent_output("i1", "我想把散落的记忆串成一条线", "串出来的一条线：……")
    store.add_intent_output("i2", "我想发帖说算法圈", "草稿正文：……",
                            kind="draft_external", status="pending_review")

    ctx = build_intent_context(str(env))
    assert "小桔自己做过的事" in ctx
    assert "我想把散落的记忆串成一条线" in ctx
    assert "还没发" in ctx                                  # pending_review 会被标注


def test_build_intent_context_fail_open(env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(CognitiveStore, "_connect", _boom)
    assert build_intent_context(str(env)) == ""
    assert build_intent_context(str(env), limit=0) == ""


# ---------------------------------------------------------------------------
# 7) 执行器单测：闸门 / 解析 / 本地史料
# ---------------------------------------------------------------------------
def test_executor_gates(env):
    assert ex.run_intent_execution(str(env), {"goal": ""})["reason"] == "no_goal"
    assert ex.run_intent_execution(
        str(env), {"goal": "我想做点什么"}, config={"enabled": False}
    )["reason"] == "disabled"


def test_executor_disabled_by_env(env, monkeypatch):
    monkeypatch.setenv("AGENCY_INTENT_EXECUTION_ENABLED", "0")
    res = ex.run_intent_execution(str(env), {"goal": "我想做点什么"})
    assert res["executed"] is False and res["reason"] == "disabled"


def test_parse_intent_output_tolerates_fence_and_noise():
    raw = "好的：\n```json\n{\"kind\":\"diary\",\"title\":\"今天\",\"body\":\"今天想通了一件事\"}\n```"
    out = ex.parse_intent_output(raw)
    assert out["kind"] == "diary" and out["body"].startswith("今天想通")
    # body 空 / 解析不出 → None
    assert ex.parse_intent_output("") is None
    assert ex.parse_intent_output('{"title":"没有正文"}') is None
    assert ex.parse_intent_output("她说她做不出来") is None


def test_research_intent_uses_local_history_only(env):
    hist = env / "history"
    hist.mkdir()
    (hist / "算法圈.md").write_text("算法圈的来龙去脉……", encoding="utf-8")

    assert ex.is_research_intent("我想查一下算法圈的史料佐证") is True
    mats = ex.collect_materials(str(env), "我想查一下算法圈的史料佐证")
    assert any("算法圈的来龙去脉" in m for m in mats)      # 只用本地史料库，不联网


def test_whitelist_has_no_intent_slugs(env):
    """执行相关 slug（intent / intent_outbound）都不在白名单 → 永不被 tick 自动跑。"""
    from deskmate.agency.settings import DEFAULTS

    assert "intent" not in DEFAULTS["whitelist"]["actions"]
    assert "intent_outbound" not in DEFAULTS["whitelist"]["actions"]
    assert DEFAULTS["intent_execution"]["enabled"] is True