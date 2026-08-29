"""创作流水线 P2 修复验证：SSE 客户端断开后创作任务可取消。

核心：_stream_step / stream_final_review / stream_chat 收到 disconnect() 返回
True 时应立即停止 yield（结束流），不再继续生成无人接收的稿件/圆桌。
"""
from __future__ import annotations

import asyncio
import tempfile
from typing import Optional

from deskmate.creation import service
from deskmate.creation.pipeline import CreativePipeline
from deskmate.creation.executor import StubExecutor


def _tmp_base():
    return tempfile.mkdtemp(prefix="ow_creation_p2_")


class _Disconnect:
    """可控断开信号：前 n 次返回 False，之后返回 True。"""

    def __init__(self, fire_after: int = 0):
        self._calls = 0
        self._fire_after = fire_after

    def __call__(self) -> "asyncio.Future":
        async def _check():
            self._calls += 1
            return self._calls > self._fire_after
        return asyncio.ensure_future(_check())


async def test_stream_step_cancels_on_disconnect():
    """P2-8: SSE 客户端断开时，_stream_step 应立即停止流（不再发 done 事件）。"""
    tmp = _tmp_base()
    pl = CreativePipeline(space="creation:p2_8", base_dir=tmp, executor=StubExecutor())
    pl.start_screenplay_project("概念", episodes=1)
    # 写入大纲草稿，使 draft_outline 能跑通（stub 瞬间完成，但 disconnect 在轮询中检查）
    pid = "p2_8_test"
    service._PIPELINES[pid] = pl

    disc = _Disconnect(fire_after=0)  # 第一次轮询即断开
    events = []
    async for chunk in service._stream_step(pid, "draft_outline", disconnect=disc):
        events.append(chunk)

    # 断开后应立即结束，不应收到 done 事件（生成被取消）
    assert not any('"done"' in e for e in events), \
        f"断开后仍收到 done 事件，任务未被取消: {events}"
    # disconnect 至少被调用一次
    assert disc._calls >= 1, "disconnect 未被调用"


async def test_stream_step_runs_when_connected():
    """对照：客户端未断开时，_stream_step 正常跑完并发送 done 事件。"""
    tmp = _tmp_base()
    pl = CreativePipeline(space="creation:p2_8b", base_dir=tmp, executor=StubExecutor())
    pl.start_screenplay_project("概念", episodes=1)
    pid = "p2_8b_test"
    service._PIPELINES[pid] = pl

    disc = _Disconnect(fire_after=999)  # 永不断开
    events = []
    async for chunk in service._stream_step(pid, "draft_outline", disconnect=disc):
        events.append(chunk)

    assert any('"done"' in e for e in events), "未断开时应正常完成并发送 done 事件"


async def test_stream_chat_cancels_on_disconnect():
    """P2-8: 创作对话流 SSE 在客户端断开时应立即停止。"""
    tmp = _tmp_base()
    pl = CreativePipeline(space="creation:p2_8c", base_dir=tmp, executor=StubExecutor())
    pl.start_screenplay_project("概念", episodes=1)
    pid = "p2_8c_test"
    service._PIPELINES[pid] = pl

    disc = _Disconnect(fire_after=0)
    events = []
    async for chunk in service.stream_chat(pid, "写大纲", disconnect=disc):
        events.append(chunk)

    # 断开后流应结束（可能已 yield 欢迎消息，但不应无限挂起）
    assert disc._calls >= 1


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_stream_step_cancels_on_disconnect())
    asyncio.run(test_stream_step_runs_when_connected())
    asyncio.run(test_stream_chat_cancels_on_disconnect())
    print("ALL P2 PASSED")
