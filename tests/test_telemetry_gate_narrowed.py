"""遥测门收窄契约（2026-11-20 萧哥拍板：收窄不撤）。

timeline_error 是现行合法写作建议类型（judge / next_steps / preferences 在用），
却被 deny 名单连同 `_error` 后缀一起挡掉。只放行这一项，安全项与后缀兜底全部保留；
生产库 39 行历史遥测不动。
"""

from deskmate.proactivity.ledger_mailbox import _is_telemetry_kind, is_mailbox_letter


def test_timeline_error_is_a_legit_suggestion():
    assert _is_telemetry_kind("timeline_error") is False
    assert is_mailbox_letter({"kind": "timeline_error"}) is True


def test_safety_kinds_still_blocked():
    for kind in (
        "",
        "none",
        "no_action",
        "memory_write_error",
        "memory_write_failure",
        "api_key_exposure_alert",
        "hard_anchor_error",
        "incomplete_response",
        "writing_setting_storage_error",
        "some_future_error",
        "tool_exception",
    ):
        assert _is_telemetry_kind(kind) is True, kind
        assert is_mailbox_letter({"kind": kind}) is False, kind
