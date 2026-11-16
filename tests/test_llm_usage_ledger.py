"""LLM token 用量账本（2026-11-16 B1 基线工程）测试。

立项背景：本地此前没有任何 token 用量记录（deskmate.db 无 usage 表、
behavior_log 仅 ts/kind/payload、llm_circuit_breaker 只记熔断事件）⇒
所有降本改造（读稿预算、重复注入削减等）缺验收基线。红线：先补基线再谈优化。

契约：
- 账本落统一库 deskmate.db 新表 llm_usage（DESKMATE_DIR 可覆写隔离）；
- 🔴 llm_usage **不进** SYNC_TABLES：用量是端本地统计，跨端同步无意义；
- fail-open 铁律：记账任何异常都不许向上抛，绝不影响对话本身；
- 记录点=OpenAIProvider 出口（stream/complete 包装层），成功带 usage、
  失败带 status=error。
"""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

import coworker.llm_usage as ledger


def _rows(tmp_path):
    con = sqlite3.connect(tmp_path / "deskmate.db")
    try:
        return con.execute(
            "SELECT model, provider, status, input_tokens, cache_read, cache_write,"
            " completion_tokens, prompt_chars, completion_chars, error, scene"
            " FROM llm_usage ORDER BY id"
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 账本本体
# ---------------------------------------------------------------------------
def test_record_writes_row_with_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    usage = SimpleNamespace(input=31, output=7, cache_read=10, cache_write=2)
    ledger.record(
        model="ark:deepseek-v4-flash",
        provider="https://example.com/v1",
        usage=usage,
        prompt_chars=1234,
        completion_chars=89,
    )
    rows = _rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "ark:deepseek-v4-flash"
    assert r[1] == "https://example.com/v1"
    assert r[2] == "ok"
    assert r[3:7] == (31, 10, 2, 7)
    assert r[7:9] == (1234, 89)
    assert r[9] is None
    assert r[10] is None


def test_record_without_usage_leaves_tokens_null(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    ledger.record(model="xiaomi:mimo-v2.6-pro", prompt_chars=100, completion_chars=0,
                  status="error", error="RuntimeError: boom")
    rows = _rows(tmp_path)
    assert rows[0][3:7] == (None, None, None, None)
    assert rows[0][2] == "error"
    assert "boom" in rows[0][9]


def test_record_scene_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    with ledger.scene_scope("bot_group"):
        ledger.record(model="m", prompt_chars=1)
    ledger.record(model="m", prompt_chars=1)
    rows = _rows(tmp_path)
    assert rows[0][10] == "bot_group"
    assert rows[1][10] is None


def test_record_ts_monotonic(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    ledger.record(model="m", prompt_chars=1)
    time.sleep(0.01)
    ledger.record(model="m", prompt_chars=1)
    con = sqlite3.connect(tmp_path / "deskmate.db")
    try:
        ts = [r[0] for r in con.execute("SELECT ts FROM llm_usage ORDER BY id")]
    finally:
        con.close()
    assert len(ts) == 2 and ts[1] > ts[0]


def test_record_fail_open_on_broken_db(tmp_path, monkeypatch):
    # 库文件损坏 / 目录只读：record 必须静默吞掉，绝不抛
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    (tmp_path / "deskmate.db").write_bytes(b"this is not a sqlite db")
    ledger.record(model="m", prompt_chars=1)  # 不得抛
    ledger.record(model="m", prompt_chars=1)  # 第二次也必须吞掉
    readonly = tmp_path / "ro"
    (readonly / "sub").mkdir(parents=True)
    try:
        monkeypatch.setenv("DESKMATE_DIR", str(readonly / "sub"))
        (readonly / "sub").chmod(0o500)
        ledger.record(model="m", prompt_chars=1)  # 建不了库也必须吞掉
    finally:
        (readonly / "sub").chmod(0o755)  # 恢复权限，免得 pytest 清理 tmp 时卡死


# ---------------------------------------------------------------------------
# Provider 出口挂钩
# ---------------------------------------------------------------------------
class _FakeCompletions:
    def __init__(self, chunks=None, exc=None, response=None):
        self._chunks = chunks
        self._exc = exc
        self._response = response

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        if self._response is not None:
            return self._response
        return iter(self._chunks)


class _FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _stream_chunks(text: str, usage_obj=None):
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason="stop")
    out = [SimpleNamespace(choices=[choice], usage=None)]
    if usage_obj is not None:
        out.append(SimpleNamespace(choices=[], usage=usage_obj))
    return out


def test_provider_stream_records_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    from coworker.providers.openai_provider import OpenAIProvider

    usage_obj = SimpleNamespace(
        prompt_tokens=31, completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=10),
    )
    client = _FakeClient(_FakeCompletions(chunks=_stream_chunks("你好呀", usage_obj)))
    prov = OpenAIProvider(client=client, default_model="m", base_url="https://gw/v1")
    chunks = list(prov.stream(model="m", messages=[{"role": "user", "content": "hi"}]))
    assert chunks[-1].turn.text == "你好呀"
    rows = _rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "m" and r[1] == "https://gw/v1" and r[2] == "ok"
    assert r[3:7] == (21, 10, 0, 7)   # input=31-10 fresh, cache_read=10
    assert r[8] == len("你好呀")


def test_provider_stream_records_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    from coworker.providers.openai_provider import OpenAIProvider

    client = _FakeClient(_FakeCompletions(exc=RuntimeError("gateway boom")))
    prov = OpenAIProvider(client=client, default_model="m")
    with pytest.raises(RuntimeError):
        list(prov.stream(model="m", messages=[{"role": "user", "content": "hi"}]))
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][2] == "error"
    assert "gateway boom" in rows[0][9]


def test_provider_complete_records_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    from coworker.providers.openai_provider import OpenAIProvider

    usage_obj = SimpleNamespace(
        prompt_tokens=31, completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    msg = SimpleNamespace(content="answer", tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=usage_obj)
    client = _FakeClient(_FakeCompletions(response=resp))
    prov = OpenAIProvider(client=client, default_model="m")
    turn = prov.complete(model="m", messages=[{"role": "user", "content": "hi"}])
    assert turn.text == "answer"
    rows = _rows(tmp_path)
    assert rows[0][2] == "ok"
    assert rows[0][3:7] == (31, 0, 0, 7)
    assert rows[0][8] == len("answer")


def test_provider_ledger_never_breaks_chat(tmp_path, monkeypatch):
    # 账本彻底坏掉（目录只读建不了库）时，provider 必须照常工作
    ro = tmp_path / "ro"
    (ro / "sub").mkdir(parents=True)
    try:
        monkeypatch.setenv("DESKMATE_DIR", str(ro / "sub"))
        (ro / "sub").chmod(0o500)
        from coworker.providers.openai_provider import OpenAIProvider

        client = _FakeClient(_FakeCompletions(chunks=_stream_chunks("ok")))
        prov = OpenAIProvider(client=client, default_model="m")
        chunks = list(prov.stream(model="m", messages=[{"role": "user", "content": "hi"}]))
        assert chunks[-1].turn.text == "ok"
    finally:
        (ro / "sub").chmod(0o755)  # 恢复权限，免得 pytest 清理 tmp 时卡死
