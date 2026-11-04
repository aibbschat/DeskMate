"""B 阶段先红测试：出勤三档与「无人时绝不挂起」。

萧哥 11-04 定稿：D1/D2 不设任务类别／站点清单，D3 **保持 BYPASS**。
BYPASS 下 ``permissions.evaluate()`` 早返回 ``allowed=True, needs_user=False``，
运行期审批卡本就不产生 —— 所以 B1 不必改 ``engine.py:_authorize()``，
真正要解决的只剩提问类工具在无人时的无限等待
（``engine.py:2294-2338 _handle_ask_user()`` 里 ``await self.question_asker(...)``）。

实现落 ``deskmate/autonomy/attendance.py``（纯函数 + async asker，最小可回退）。
本文件先锁契约与接线；实现后须补一条真跑 ``_handle_ask_user`` 不挂的集成用例，
静态断言不算绿灯证据。
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest


def _attendance():
    return importlib.import_module("deskmate.autonomy.attendance")


# -- 契约：三档与旧布尔兼容 --------------------------------------------------


def test_attendance_module_exists():
    assert _attendance() is not None


def test_three_levels_are_declared():
    m = _attendance()
    assert tuple(m.LEVELS) == ("attended", "inbox", "auto")


def test_normalize_attendance_accepts_legacy_booleans():
    m = _attendance()
    assert m.normalize_attendance(False) == "attended"
    assert m.normalize_attendance(None) == "attended"
    assert m.normalize_attendance("off") == "attended"
    assert m.normalize_attendance(True) == "inbox"
    assert m.normalize_attendance("on") == "inbox"
    assert m.normalize_attendance("inbox") == "inbox"
    assert m.normalize_attendance("auto") == "auto"


def test_legacy_unattended_json_still_loads(tmp_path):
    """回归护栏：旧 ``unattended.json`` 是 bool dict，新语义不能让它炸。"""
    import json

    from coworker.unattended import UnattendedRegistry

    cfg = tmp_path / "unattended.json"
    cfg.write_text(json.dumps({"s1": True, "s2": False}), encoding="utf-8")
    reg = UnattendedRegistry(cfg)
    assert reg.is_unattended("s1") is True
    assert reg.is_unattended("s2") is False
    assert set(reg.sessions()) == {"s1"}


def test_only_auto_answers_automatically():
    m = _attendance()
    assert m.should_auto_answer("auto") is True
    assert m.should_auto_answer("inbox") is False, "inbox 档必须真等人，这是设计不是 hang"
    assert m.should_auto_answer("attended") is False


# -- 契约：代答内容与「绝不挂起」 --------------------------------------------


def test_auto_answer_is_fixed_and_least_destructive():
    m = _attendance()
    text = m.AUTO_ANSWER
    assert isinstance(text, str) and text.strip()
    assert "最小破坏" in text
    assert m.auto_answer("问题甲") == m.auto_answer("问题乙") == text


def test_auto_question_asker_never_hangs_and_audits():
    """最关键的一条：asker 不许 await 任何人，1 秒内必须返回。"""
    m = _attendance()
    audit: list[dict] = []
    asker = m.build_auto_question_asker(audit.append)

    async def _call():
        return await asyncio.wait_for(
            asker({"question": "要不要继续？"}, "call-auto-1"), timeout=1.0
        )

    result = asyncio.run(_call())
    assert result.get("answer") == m.AUTO_ANSWER
    assert not result.get("error"), "auto 档不该返回失败态，否则会诱导模型重试"
    assert audit, "代答必须落审计，否则「她为什么这么选」不可回放"


# -- 回归护栏：D3 定稿的前提 --------------------------------------------------


def test_bypass_produces_no_needs_user_cards(tmp_path):
    """锁住「B1 不用改 _authorize()」这个前提；谁改了 BYPASS 语义，这条先红。"""
    import aisuite as ai

    from coworker.permissions import Mode, PermissionEngine
    from coworker.tools import ToolRegistry

    reg = ToolRegistry()
    reg.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    eng = PermissionEngine(workspace_root=tmp_path, mode=Mode.BYPASS_APPROVALS)
    d = eng.evaluate(
        "write_file", {"path": "probe.txt", "content": "x"},
        reg.get("write_file").metadata,
    )
    assert d.allowed is True and d.needs_user is False


def test_manager_wires_attendance_asker():
    """接线检查：函数写好却没挂进调用链＝云端静默失效，历史上踩过。"""
    repo = Path(__file__).resolve().parents[1]
    src = (repo / "coworker/server/manager.py").read_text(encoding="utf-8")
    assert "build_auto_question_asker" in src, (
        "manager._build_task_engine 没有把 auto 档 asker 接进 question_asker"
    )
