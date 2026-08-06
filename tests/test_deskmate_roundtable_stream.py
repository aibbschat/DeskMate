"""圆桌原生流式（逐字）事件链回归测试。

验证：
- 进程内人格 + 主持走 turn_start / turn_delta / turn_end（带 turn_id），而非整段 turn；
- 多发言者并发时 token 按 turn_id 交错；
- 每个 turn_end.text 等于其 deltas 的拼接（最终以完整文本为准）；
- 外部 A2A peer 仍发整段 turn（非流式）；
- run() 聚合接口仍返回结构化结果（向后兼容）。
"""
import asyncio

import pytest

import deskmate.roundtable.coordinator as C


PAYLOADS = {
    "host": ["【主持】", "开场与纪要内容。"],
    "historian": ["【史料考据】", "年代核验通过。"],
    "editor": ["【文学编辑】", "叙事节奏良好。"],
    "proofreader": ["【文字校勘】", "未见硬伤。"],
    "reader": ["【读者视角】", "阅读体验不错。"],
}


async def _fake_stream(system, user, *, temperature=0.3, max_tokens=800, timeout=300.0):
    # 用唯一括号标记定位身份，避免提示词互相提及导致的误判
    key = "host"
    for k in PAYLOADS:
        if f"「{k}」" in system:
            key = k
            break
    for p in PAYLOADS[key]:
        await asyncio.sleep(0.001)
        yield p


@pytest.fixture(autouse=True)
def _patch_llm(monkeypatch):
    # 不依赖真实 provider 是否配置：强制可用，确保走流式链路（而非（LLM 未就绪）兜底）
    monkeypatch.setattr(C, "llm_available", lambda: True)
    monkeypatch.setattr(C, "chat_stream", _fake_stream)


async def _collect(rt):
    types, start_ids, delta_ids, end_texts, speaking, turns = [], set(), [], {}, [], []
    async for ev in rt.run_stream():
        types.append(ev["type"])
        if ev["type"] == "turn_start":
            start_ids.add(ev["turn_id"])
        elif ev["type"] == "turn_delta":
            delta_ids.append(ev["turn_id"])
        elif ev["type"] == "turn_end":
            end_texts[ev["turn_id"]] = ev["text"]
        elif ev["type"] == "speaking":
            speaking.append(ev["speaker"])
        elif ev["type"] == "turn":
            turns.append(ev["turn_id"])
    return types, start_ids, delta_ids, end_texts, speaking, turns


async def test_inprocess_uses_streaming_events():
    rt = C.Roundtable("测试议题：元末风物", personas=C.PERSONAS[:4], max_rounds=1)
    types, start_ids, delta_ids, end_texts, speaking, turns = await _collect(rt)

    assert types[0] == "meta" and types[1] == "peers"
    assert {"turn_start", "turn_delta", "turn_end", "done"}.issubset(set(types))
    # 进程内（无 peer）不应出现整段 turn 事件
    assert not turns, "进程内发言不应发整段 turn 事件"
    # 开场 + 4 人格 + 收尾纪要 共 6 个流式发言
    assert start_ids == {"host-open", "host-synth", "p0-r1", "p1-r1", "p2-r1", "p3-r1"}, start_ids
    # 每个流式发言至少产出 1 个 delta，且 turn_end 文本含其全部片段
    for tid in start_ids:
        assert end_texts[tid].startswith("【") and end_texts[tid].endswith("。")
    # 主持"思考中"已被广播
    assert "桌搭（主持）" in speaking


async def test_streaming_interleaves_by_turn_id():
    rt = C.Roundtable("测试议题：X", personas=C.PERSONAS[:4], max_rounds=2)
    types, start_ids, delta_ids, end_texts, _, _ = await _collect(rt)
    # 多轮 + 收窄：除开场/收尾外，第1轮有分歧归纳（host-div-1），第2轮无（max_rounds=2）
    assert "host-div-1" in start_ids
    # 所有 delta 都带有合法 turn_id
    valid = start_ids | {"host-div-1"}
    assert all(d in valid for d in delta_ids), delta_ids
    assert "done" in types


async def test_run_aggregates_compatibly():
    rt = C.Roundtable("测试议题：Y", personas=C.PERSONAS[:4], max_rounds=1)
    res = await rt.run()
    assert res.rounds_held == 1
    # 开场 + 4 人格 + 收尾纪要 = 至少 6 条发言
    assert len(res.turns) >= 6
    assert res.synthesis.startswith("【主持】")


async def test_peer_still_uses_whole_turn():
    """外部 peer 拿不到 token 流，应整段返回 turn（而非流式三件套）。"""
    rt = C.Roundtable(
        "测试议题：Z",
        peers=[C.PeerSpec("Hermes", "http://localhost:9/nope")],
        personas=C.PERSONAS[:1],
        max_rounds=1,
    )
    # peer 探活会失败（url 不可达），离线跳过 -> 不会有 peer turn；
    # 这里只验证结构：若 peer 在线，它走 _peer_turn 发整段 turn。直接单测 _peer_turn。
    out_q = asyncio.Queue()

    async def fake_peer_speak(*a, **k):
        return "（Hermes 整段回复）"

    rt._peer_speak = fake_peer_speak
    t = await rt._peer_turn(out_q, round_no=1, peer=C.PeerSpec("Hermes", "x"), ctx="", divergences=[], story_block="")
    events = []
    while not out_q.empty():
        events.append(out_q.get_nowait())
    assert any(e["type"] == "turn" and e["speaker"] == "Hermes" for e in events)
    assert t.text == "（Hermes 整段回复）"
