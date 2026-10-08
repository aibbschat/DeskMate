"""R13 意图讨论流（江河设计 §五「自主是出海口」续）——「你同意了，然后呢」。

R12 让意图能被放行执行，但执行后**静默落库**——用户看不到产出，没法点评，
没法来回讨论。R13 把执行改成**跨会话回合的讨论过程**：start → run → 用户反馈
→ 再改（run round+1）/ 定稿（finalize）/ 不要（cancel）。

覆盖：
1. ``start_discussion`` 主路径：设 discuss_state、写开场白 ledger、intent.status=active
2. ``start_discussion`` 多并发护栏：同 session 已有 discussing → ``session_busy``
3. ``start_discussion`` 红线检查：替你写原稿 → ``sacred_manuscript``
4. ``start_discussion`` intent 不存在 → ``intent_not_found``
5. ``run_discussion_round`` 主路径：跑 executor 出 draft、写 ledger、更新 discuss_state
6. ``finalize_discussion`` 主路径：落 intent_outputs、写总结 lesson、清 discuss_state
7. ``cancel_discussion`` 主路径：intent.status=dropped、清 discuss_state
8. ``classify_user_message`` 三分类（mock LLM）
9. ``classify_user_message`` fail-open → chitchat（LLM 不可用）
10. ``extract_lesson_from_feedback`` ``is_meaningful=true`` 写库
11. ``extract_lesson_from_feedback`` ``is_meaningful=false`` 不写库
"""

from __future__ import annotations

import json

import pytest

from deskmate.agency.behaviors import intent_discuss as d
from deskmate.cognitive.store import CognitiveStore


# ---------------------------------------------------------------------------
# 公共夹具备份
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp_path 作 DESKMATE_DIR；关掉联网嵌入模型（防卡死）。"""
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    return tmp_path


def _store(env) -> CognitiveStore:
    return CognitiveStore(db_path=env / "deskmate.db")


def _patch_llm(monkeypatch, out: str, available: bool = True):
    """打桩 LLM：chat 返回 out；llm_available 返回 available。"""
    monkeypatch.setattr("deskmate.memory.llm.chat", lambda *a, **k: out)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: available)


def _seed_intent(env, goal: str = "我想把算法圈的争论整理一下",
                 motivation: str = "因为老看到却不明白",
                 origin: str = "长期兴趣") -> str:
    """建一条 open 意图，返回 intent_id。"""
    store = _store(env)
    iid = store.add_intent(goal, motivation, origin=origin)
    return iid


# ---------------------------------------------------------------------------
# 1) start_discussion 主路径
# ---------------------------------------------------------------------------
def test_start_discussion_sets_state_and_writes_opening_ledger(env, monkeypatch):
    iid = _seed_intent(env)
    _patch_llm(monkeypatch, "好，我去把算法圈几条消息捋一捋，一会儿给你看。")

    res = d.start_discussion(str(env), iid, "sess_A")

    assert res["ok"] is True
    assert res["ledger_id"]  # 写了开场白 ledger
    assert "算法圈" in res["text"] or res["text"]  # LLM 生成的开场白
    # discuss_state 已设
    intent = _store(env).get_intent(iid)
    state = json.loads(intent["discuss_state"])
    assert state["phase"] == "opening"
    assert state["session_id"] == "sess_A"
    assert state["round"] == 1
    # intent.status=active
    assert intent["status"] == "active"


# ---------------------------------------------------------------------------
# 2) start_discussion 多并发护栏：同 session 已有 discussing → session_busy
# ---------------------------------------------------------------------------
def test_start_discussion_blocks_second_concurrent_intent(env, monkeypatch):
    iid1 = _seed_intent(env, goal="我想整理算法圈争论")
    iid2 = _seed_intent(env, goal="我想写一篇关于霍尔木兹的笔记")
    _patch_llm(monkeypatch, "好，我去做了。")

    r1 = d.start_discussion(str(env), iid1, "sess_A")
    assert r1["ok"] is True

    r2 = d.start_discussion(str(env), iid2, "sess_A")
    assert r2["ok"] is False
    assert r2["reason"] == "session_busy"
    assert r2["blocking_intent_id"] == iid1


# ---------------------------------------------------------------------------
# 3) start_discussion 红线检查：替你写原稿 → sacred_manuscript
# ---------------------------------------------------------------------------
def test_start_discussion_rejects_manuscript_intent(env):
    iid = _seed_intent(env, goal="我想替萧盛把《国子监》的原稿正文改写一遍")

    res = d.start_discussion(str(env), iid, "sess_A")

    assert res["ok"] is False
    assert res["reason"] == "sacred_manuscript"


# ---------------------------------------------------------------------------
# 4) start_discussion intent 不存在 → intent_not_found
# ---------------------------------------------------------------------------
def test_start_discussion_unknown_intent(env):
    res = d.start_discussion(str(env), "ghost_id", "sess_A")
    assert res["ok"] is False
    assert res["reason"] == "intent_not_found"


# ---------------------------------------------------------------------------
# 5) run_discussion_round 主路径：跑 executor 出 draft、写 ledger、更新 discuss_state
# ---------------------------------------------------------------------------
_OUT = json.dumps({
    "kind": "synthesis",
    "title": "算法圈争论的脉络",
    "body": "我把算法圈这几天的争论串成了三条主线……",
}, ensure_ascii=False)


def test_run_round_produces_draft_and_updates_state(env, monkeypatch):
    iid = _seed_intent(env)
    _patch_llm(monkeypatch, "好，我去做了。")
    d.start_discussion(str(env), iid, "sess_A")

    # run 第 1 轮：patch executor 用的 LLM
    _patch_llm(monkeypatch, _OUT)
    res = d.run_discussion_round(str(env), iid, round_num=1)

    assert res["ok"] is True
    assert res["round"] == 1
    assert res["artifact"]  # 出了 draft
    assert "算法圈" in res["artifact"]
    assert res["ledger_id"]  # 写了 draft ledger
    # discuss_state 推进到 working
    intent = _store(env).get_intent(iid)
    state = json.loads(intent["discuss_state"])
    assert state["phase"] == "working"
    assert state["round"] == 1
    assert state["current_draft_ledger_id"] == res["ledger_id"]


# ---------------------------------------------------------------------------
# 6) finalize_discussion 主路径
# ---------------------------------------------------------------------------
def test_finalize_writes_output_and_summary_lesson(env, monkeypatch):
    iid = _seed_intent(env)
    _patch_llm(monkeypatch, "好，我去做了。")
    d.start_discussion(str(env), iid, "sess_A")
    _patch_llm(monkeypatch, _OUT)
    d.run_discussion_round(str(env), iid, round_num=1)

    # finalize：patch LLM 返回总结 lesson
    _patch_llm(monkeypatch, json.dumps(
        {"lesson_text": "做综述时，萧盛希望开头先点明结论再展开。"},
        ensure_ascii=False))
    res = d.finalize_discussion(str(env), iid)

    assert res["ok"] is True
    assert res["output_id"]  # 落了 intent_outputs
    assert res["lesson_written"] is True  # 写了总结 lesson
    # intent.status=done
    assert _store(env).get_intent(iid)["status"] == "done"
    # discuss_state 清空
    assert _store(env).get_intent(iid)["discuss_state"] == ""
    # intent_outputs 里能查到
    outs = _store(env).get_recent_intent_outputs(5)
    assert any(o["intent_id"] == iid for o in outs)


# ---------------------------------------------------------------------------
# 7) cancel_discussion 主路径
# ---------------------------------------------------------------------------
def test_cancel_drops_intent_and_clears_state(env, monkeypatch):
    iid = _seed_intent(env)
    _patch_llm(monkeypatch, "好，我去做了。")
    d.start_discussion(str(env), iid, "sess_A")

    res = d.cancel_discussion(str(env), iid)

    assert res["ok"] is True
    assert _store(env).get_intent(iid)["status"] == "dropped"
    assert _store(env).get_intent(iid)["discuss_state"] == ""


# ---------------------------------------------------------------------------
# 8) classify_user_message 三分类
# ---------------------------------------------------------------------------
def test_classify_user_message_finalize(env, monkeypatch):
    _patch_llm(monkeypatch, json.dumps(
        {"verdict": "finalize", "reason": "萧盛说定稿"}, ensure_ascii=False))
    r = d.classify_user_message("就这样，挺好", "整理算法圈争论")
    assert r["verdict"] == "finalize"


def test_classify_user_message_feedback(env, monkeypatch):
    _patch_llm(monkeypatch, json.dumps(
        {"verdict": "feedback", "reason": "萧盛在给具体反馈"}, ensure_ascii=False))
    r = d.classify_user_message("这里太书面了，改口语化点", "整理算法圈争论")
    assert r["verdict"] == "feedback"


def test_classify_user_message_chitchat_when_llm_unavailable(env, monkeypatch):
    _patch_llm(monkeypatch, "", available=False)
    r = d.classify_user_message("今天天气不错", "整理算法圈争论")
    assert r["verdict"] == "chitchat"
    assert r["reason"] == "no_llm"


def test_classify_user_message_fail_open_on_exception(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("LLM 挂了")
    monkeypatch.setattr("deskmate.memory.llm.chat", boom)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    r = d.classify_user_message("任意话", "整理算法圈争论")
    assert r["verdict"] == "chitchat"
    assert r["reason"] == "classify_failed"


# ---------------------------------------------------------------------------
# 9) extract_lesson_from_feedback：is_meaningful 守卫
# ---------------------------------------------------------------------------
def test_extract_lesson_meaningful(env, monkeypatch):
    _patch_llm(monkeypatch, json.dumps(
        {"is_meaningful": True, "lesson_text": "萧盛偏好口语化表达，不要书面腔。"},
        ensure_ascii=False))
    r = d.extract_lesson_from_feedback("这里太书面了", "上一版 draft", "整理算法圈争论")
    assert r["is_meaningful"] is True
    assert "口语化" in r["lesson_text"]


def test_extract_lesson_not_meaningful(env, monkeypatch):
    _patch_llm(monkeypatch, json.dumps(
        {"is_meaningful": False, "lesson_text": ""}, ensure_ascii=False))
    r = d.extract_lesson_from_feedback("嗯，看了", "上一版 draft", "整理算法圈争论")
    assert r["is_meaningful"] is False
    assert r["lesson_text"] == ""


def test_extract_lesson_empty_feedback_short_circuits(env, monkeypatch):
    # 空反馈直接返回 is_meaningful=false，不调 LLM
    called = {"n": 0}
    def fake_chat(*a, **k):
        called["n"] += 1
        return json.dumps({"is_meaningful": True, "lesson_text": "不该被调到"})
    monkeypatch.setattr("deskmate.memory.llm.chat", fake_chat)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    r = d.extract_lesson_from_feedback("", "上一版 draft", "整理算法圈争论")
    assert r["is_meaningful"] is False
    assert called["n"] == 0  # 短路了，没调 LLM


# ---------------------------------------------------------------------------
# 12) 端点级：/discuss/start 必须自带开场白投递 + 兜底建会话记录
# （2026-10-08 萧哥报「提议卡点执行，落不到聊天，直接消失」第三处根因）
#
# 场景锁死：用户看到提议卡的唯一位置是 DeskHome——**空会话**（一条消息未发）。
# 服务端此时**没有会话记录**（记录只在首轮消息后才建）：
#   - 旧实现靠前端回调通用 speak 端点投递开场白 → append_session_messages
#     因记录不存在返 False → 404 → 开场白落不进聊天（卡消失、聊天空白）。
#   - 且通用 speak 的 append/broadcast 都不带 meta → 即使记录在，也渲染成
#     普通气泡而非 intent_discuss 讨论卡。
# 修复后契约：start 端点自己 (a) 兜底建 deskmate 会话记录 (b) append+broadcast
# 都带 meta{origin=intent_handoff, intent_id, phase=opening}。前端不再调 speak。
# ---------------------------------------------------------------------------
def _endpoint_client(env, monkeypatch, opening_text: str = "好，我去做了。"):
    """起一个最小 SessionManager + TestClient（无 LLM、无网络），打桩开场白。"""
    from fastapi.testclient import TestClient

    from coworker.providers import ModelCapabilities, ProviderClient
    from coworker.server import SessionManager, create_app

    class _NoLLM(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            raise RuntimeError("本测试不应触发对话补全")

        def capabilities(self, model):
            return ModelCapabilities()

    manager = SessionManager(workspace=env, provider=_NoLLM())
    _patch_llm(monkeypatch, opening_text)
    return TestClient(create_app(manager)), manager


def test_discuss_start_delivers_opening_with_meta_and_bootstraps_session(env, monkeypatch):
    iid = _seed_intent(env)
    client, manager = _endpoint_client(env, monkeypatch, "好，我把算法圈捋一捋，一会儿给你看。")

    r = client.post(
        "/v1/deskmate/agency/intent/discuss/start",
        json={"intent_id": iid, "session_id": "brand_new_empty_session"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ledger_id"]
    assert "算法圈" in body["text"]

    # ① 空会话（服务端原本无记录）已兜底创建，agent=deskmate
    rec = manager.session_store.load("brand_new_empty_session")
    assert rec is not None, "start 必须兜底建会话记录，否则开场白落不进聊天"
    assert rec.agent == "deskmate"

    # ② 开场白已带 meta 落库（前端靠 meta.origin=intent_handoff 渲染讨论卡；reload 重建也靠它）
    msgs = manager.session_messages("brand_new_empty_session")
    assert msgs, "开场白必须落进会话消息"
    meta = msgs[-1].get("meta") or {}
    assert meta.get("origin") == "intent_handoff"
    assert meta.get("intent_id") == iid
    assert meta.get("phase") == "opening"
    assert msgs[-1].get("content") == body["text"]

    # ③ 开场白 ledger 已补账（防 DeskHome 待办列表重复递）
    from deskmate.proactivity.ledger import lookup_items
    hits = lookup_items(base_dir=env, handle=body["ledger_id"], limit=5)
    assert hits and hits[0]["id"] == body["ledger_id"]
    assert hits[0]["delivered_at"], "开场白 ledger 应标 delivered"


def test_discuss_start_existing_session_keeps_record_untouched(env, monkeypatch):
    """已有会话记录时不重建、不覆盖（只补投递）。"""
    from coworker.sessions import SessionRecord

    iid = _seed_intent(env)
    client, manager = _endpoint_client(env, monkeypatch, "好，我去做了。")
    manager.session_store.save(
        SessionRecord(
            session_id="existing_session",
            workspace="",
            model="test-model",
            mode="default",
            messages=[{"role": "user", "content": "早", "ts": 1}],
            agent="deskmate",
        )
    )

    r = client.post(
        "/v1/deskmate/agency/intent/discuss/start",
        json={"intent_id": iid, "session_id": "existing_session"},
    )
    assert r.status_code == 200
    rec = manager.session_store.load("existing_session")
    assert rec is not None
    assert rec.model == "test-model", "不得覆盖已有会话记录字段"
    # 开场白追加在既有消息之后
    msgs = manager.session_messages("existing_session")
    assert len(msgs) == 2
    assert (msgs[-1].get("meta") or {}).get("origin") == "intent_handoff"
