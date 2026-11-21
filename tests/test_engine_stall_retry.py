"""上游 stall 自动重试一钉死（2026-11-20）。

现场：萧哥读一份 25KB 总纲，deepseek 首字 stall 75 秒后整轮报错结束，
界面只剩转圈。修法：本轮一个字都没吐出之前，ProviderStall 自动重试一次，
不把「重试」按钮甩给用户。已经吐过字的轮次绝不重试（会重复输出）。
"""

from __future__ import annotations

import asyncio

import aisuite as ai
from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.providers.errors import FirstTokenTimeout
from coworker.providers import timeouts as _timeouts
from coworker.tools import ToolRegistry


def _text_turn(text):
    return AssistantTurn(text=text, finish_reason="stop")


class StallThenOkProvider(ProviderClient):
    """前 N 次 complete 抛首字超时，之后正常返回。"""

    def __init__(self, turns, stalls=1):
        self._turns = list(turns)
        self._stalls = stalls
        self.calls = 0

    def complete(self, *, model, messages, tools=None, **settings):
        self.calls += 1
        if self._stalls > 0:
            self._stalls -= 1
            raise FirstTokenTimeout(25.0)
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, provider):
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    permissions = PermissionEngine(workspace_root=tmp_path)
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="deepseek-flash",
        max_iterations=8,
    )


def _collect(engine, user_input):
    async def _run():
        return [ev async for ev in engine.run(user_input)]

    return asyncio.run(_run())


def test_first_token_stall_retries_once_and_completes(tmp_path):
    provider = StallThenOkProvider([_text_turn("读完了")], stalls=1)
    engine = _engine(tmp_path, provider)
    events = _collect(engine, "读总纲")

    assert provider.calls == 2, "stall 后必须自动再问一次"
    assert engine._stall_retries == 1
    texts = [ev.data.get("text") for ev in events if ev.type is EventType.ASSISTANT_MESSAGE]
    assert texts[-1] == "读完了"
    assert events[-1].type is EventType.TURN_END
    assert events[-1].data["status"] == "completed"
    # 重试痕迹留在会话里（display-only，不发给模型）
    assert any(
        m.get("role") == "notice" and m.get("kind") == "stall_retry"
        for m in engine.messages
    )


def test_second_stall_is_not_retried_again(tmp_path):
    """只给一次自愈机会：第二次仍 stall 就走既有的 error notice + 收尾，
    绝不无限重试（防死循环）。"""
    provider = StallThenOkProvider([_text_turn("不该到这里")], stalls=2)
    engine = _engine(tmp_path, provider)
    events = _collect(engine, "读总纲")

    assert provider.calls == 2, "只自动重试一次"
    assert engine._stall_retries == 1
    errors = [
        m for m in engine.messages if m.get("role") == "notice" and m.get("kind") == "error"
    ]
    assert errors, "第二次 stall 必须以 error notice 收尾，不能静默"
    assert "timeout" in errors[-1]["text"]
    assert events[-1].type is EventType.ERROR


def test_timeout_defaults_are_tightened():
    from coworker.tools import shell as _shell

    # 2026-11-20 定案：25/45 是排查期的临时值（会把慢而健康的上游误杀成
    # stalled upstream）；默认回到 75/120，与 PR17 文档口径一致。
    assert _timeouts.first_token_timeout() == 75.0
    assert _timeouts.stream_idle_timeout() == 120.0
    # shell 前台命令独立收紧到 30s（marker 失步时不站满两分钟）；真正长命令
    # 由调用方显式传 timeout_seconds，上限仍是 _MAX_TIMEOUT=600s。
    assert _shell._DEFAULT_TIMEOUT == 30.0, "卡死的命令不该再站满两分钟"
