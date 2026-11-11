"""轮内记忆上下文缓存回归（2026-11-10 首字延迟修复）。

同一轮里 engine 会两次取 _outbound_messages → context_provider
（_compaction_due 的 token 估算 + _astream 真正发送），此前每次都完整重跑
build_full_context_for_turn（含天气 HTTP / 语义召回）。这里锁住"同指纹只算一次、
下一轮必须重算、关闭开关时不算"。
"""
import coworker.agent as agent


class _FakeMgr:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def build_full_context_for_turn(self, query: str, *, prev_user: str = "") -> str:
        self.calls.append(query)
        return f"ctx:{query}"


def _setup(monkeypatch):
    fake = _FakeMgr()
    monkeypatch.setenv("DESKMATE_AUTO_INJECT", "1")
    # 2026-11-11 起 manager 按 base 复用（_get_deskmate_manager），不再读全局
    # _deskmate_mgr_singleton；这里按新入口打桩。
    monkeypatch.setattr(agent, "_get_deskmate_manager", lambda *a, **k: fake)
    agent._deskmate_ctx_cache.clear()
    return fake


def test_same_turn_context_built_once(monkeypatch):
    fake = _setup(monkeypatch)
    msgs = [{"role": "user", "content": "林某的师父是谁"}]
    first = agent._deskmate_memory_context(msgs)
    second = agent._deskmate_memory_context(msgs)
    assert first == second == "ctx:林某的师父是谁"
    assert fake.calls == ["林某的师父是谁"]


def test_next_turn_rebuilds_context(monkeypatch):
    fake = _setup(monkeypatch)
    turn1 = [{"role": "user", "content": "第一问"}]
    turn2 = turn1 + [
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "第二问"},
    ]
    agent._deskmate_memory_context(turn1)
    agent._deskmate_memory_context(turn2)
    assert fake.calls == ["第一问", "第二问"]


def test_disabled_switch_skips_build(monkeypatch):
    fake = _setup(monkeypatch)
    monkeypatch.setenv("DESKMATE_AUTO_INJECT", "0")
    assert agent._deskmate_memory_context([{"role": "user", "content": "x"}]) == ""
    assert fake.calls == []
