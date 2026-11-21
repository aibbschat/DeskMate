"""DeepSeek thinking 模式的 reasoning_content 回传契约（2026-11-20）。

现场：萧哥把会话切到 DeepSeek 官方，整单 400 ——“The reasoning_content in the
thinking mode must be passed back to the API”。根因是 assistant 消息的 reasoning
只作展示、从不回传，而 DeepSeek 的 thinking 模式要求逐条回传。
"""

from __future__ import annotations

from coworker.providers.openai_provider import _prepare_messages

_DEEPSEEK = "https://api.deepseek.com"
_ARK = "https://ark.cn-beijing.volces.com/api/plan/v3"


def _msgs():
    return [
        {"role": "user", "content": "读总纲"},
        {"role": "assistant", "content": "在读", "reasoning": "先看目录", "usage": {"input": 1}},
        {"role": "tool", "tool_call_id": "c1", "content": "正文"},
    ]


def test_deepseek_gets_reasoning_content_back():
    out = _prepare_messages(_msgs(), base_url=_DEEPSEEK)
    assistant = out[1]
    assert assistant["reasoning_content"] == "先看目录"
    assert "reasoning" not in assistant, "原字段会被 DeepSeek 当成未知键"
    assert assistant["content"] == "在读"


def test_other_gateways_drop_reasoning_entirely():
    out = _prepare_messages(_msgs(), base_url=_ARK)
    assistant = out[1]
    assert "reasoning_content" not in assistant
    assert "reasoning" not in assistant


def test_plain_messages_are_untouched():
    msgs = [{"role": "user", "content": "hi"}]
    out = _prepare_messages(msgs, base_url=_DEEPSEEK)
    assert out == msgs


def test_user_with_reasoning_is_not_promoted():
    """只有 assistant 消息回传；user/tool 上若混进 reasoning 直接丢掉。"""
    msgs = [{"role": "user", "content": "x", "reasoning": "不该在这"}]
    out = _prepare_messages(msgs, base_url=_DEEPSEEK)
    assert "reasoning_content" not in out[0]
    assert out[0]["content"] == "x"
