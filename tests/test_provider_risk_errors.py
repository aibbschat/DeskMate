"""provider 错误的友好文案 + 风控载荷取证（2026-10-24）。

背景：小米 MiMo / 火山 ark 的网关会以 "The request was rejected because it was
considered high risk" 瞬时拒绝请求（实测为分钟级自愈的账号级风控窗口，同窗口内
聊天 + 后台 tick 多条独立链路齐拒，同载荷稍后重放全过）。旧行为两个问题：
1. friendly_model_error 不认识它 → 英文原文直接当 assistant 消息显示给萧哥；
2. 载荷没留存 → 无法取证「到底是不是内容触发」。
"""
from __future__ import annotations

import json

from coworker.providers.errors import friendly_model_error

HIGH_RISK = "The request was rejected because it was considered high risk"


def test_friendly_message_for_high_risk_rejection():
    """high risk → 中文友好提示：说明瞬时自愈、非内容定罪、可重发。"""
    msg = friendly_model_error("xiaomi:mimo-v2.6-pro", RuntimeError(HIGH_RISK))
    assert msg, "旧行为返回 None 导致英文原文透传，必须给友好提示"
    assert "风控" in msg
    assert "重" in msg, "应告诉用户可以重发/会自动恢复"


def test_friendly_message_leaves_other_errors_untouched():
    """无关错误不抢翻译（模块原有契约：认不出返回 None）。"""
    assert friendly_model_error("m", RuntimeError("some totally different failure")) is None


def test_risk_payload_capture_writes_file(tmp_path, monkeypatch):
    """被 high risk 拒时把完整请求载荷落盘，供取证「内容 vs 网关级」。"""
    from coworker.providers import openai_provider as op

    monkeypatch.setenv("DESKMATE_LLM_PAYLOAD_CAPTURE_DIR", str(tmp_path))
    kwargs = {
        "model": "mimo-v2.6-pro",
        "messages": [{"role": "user", "content": "晚上好啊"}],
        "tools": [{"type": "function", "function": {"name": "t"}}],
    }
    op._capture_risk_payload(kwargs, RuntimeError(HIGH_RISK))

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, "风控拒绝必须留一份载荷"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["model"] == "mimo-v2.6-pro"
    assert data["messages"][-1]["content"] == "晚上好啊"
    assert "error" in data


def test_risk_payload_capture_ignores_other_errors(tmp_path, monkeypatch):
    """非风控错误不落盘（配额/网络错误有各自的记账与审计，别在这掺和）。"""
    from coworker.providers import openai_provider as op

    monkeypatch.setenv("DESKMATE_LLM_PAYLOAD_CAPTURE_DIR", str(tmp_path))
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    op._capture_risk_payload(kwargs, RuntimeError("quota exhausted"))
    op._capture_risk_payload(kwargs, RuntimeError("connection reset by peer"))
    assert list(tmp_path.glob("*.json")) == []


def test_risk_payload_capture_prunes_old_files(tmp_path, monkeypatch):
    """目录最多留 20 份，防止长年累积（载荷含完整上下文，单份可能几百 KB）。"""
    from coworker.providers import openai_provider as op

    monkeypatch.setenv("DESKMATE_LLM_PAYLOAD_CAPTURE_DIR", str(tmp_path))
    for i in range(25):
        (tmp_path / f"risk-{i:03d}.json").write_text("{}", encoding="utf-8")
    op._capture_risk_payload(
        {"model": "m", "messages": []}, RuntimeError(HIGH_RISK)
    )
    remaining = sorted(tmp_path.glob("risk-*.json"))
    assert len(remaining) <= 20
    assert "risk-000.json" not in [f.name for f in remaining], "应从最旧的开始剪"
