"""Task2 主动行为环测试：闭环决策解析 + 执行桥安全门槛 + 防打扰 + 降级提案。

不依赖真实 LLM：直接构造 CloseLoopDecision 测试执行桥，并验证决策解析的容错。
"""

import os
import tempfile

# 必须在导入 executor 之前把状态目录指到临时目录，避免污染真实的
# ~/.deskmate/memory/disturb_state.json 与 close_loop_pending.jsonl。
_TMP = tempfile.mkdtemp(prefix="deskmate_close_loop_test_")
os.environ["DESKMATE_DIR"] = _TMP
os.environ.pop("DESKMATE_CLOSE_LOOP_AUTO", None)  # 让默认自动模式生效

import pytest

from deskmate.proactivity import executor
from deskmate.proactivity.close_loop import CloseLoopDecision, _parse_decision


# ---------------------------------------------------------------------------
# 决策解析（容错）
# ---------------------------------------------------------------------------
def test_parse_decision_plain_json():
    resp = (
        '{"should_close_loop": true, "action_summary": "同步网站副本", '
        '"rationale": "公众号已改", "confidence": 0.92, "target_hint": "site", '
        '"reversible": true, "risk_level": "low"}'
    )
    d = _parse_decision(resp)
    assert d.should_close_loop
    assert d.confidence == 0.92
    assert d.reversible
    assert d.risk_level == "low"


def test_parse_decision_codeblock():
    resp = '```json\n{"should_close_loop": false, "confidence": 0}\n```'
    d = _parse_decision(resp)
    assert not d.should_close_loop


def test_parse_decision_bad_risk_normalized():
    d = _parse_decision('{"should_close_loop": true, "risk_level": "CRITICAL!"}')
    assert d.risk_level == "low"  # 非法值归一化，绝不主动执行


def test_parse_decision_no_json():
    d = _parse_decision("这段回复里根本没有 json")
    assert not d.should_close_loop


def test_parse_decision_empty():
    d = _parse_decision("")
    assert not d.should_close_loop


# ---------------------------------------------------------------------------
# 自动执行门槛
# ---------------------------------------------------------------------------
def test_auto_eligible_pass():
    d = CloseLoopDecision(True, "同步网站", "r", 0.9, "t", True, "low")
    assert executor._is_auto_eligible(d)


@pytest.mark.parametrize(
    "d",
    [
        CloseLoopDecision(True, "x", "r", 0.5, "t", True, "low"),   # 低置信度
        CloseLoopDecision(True, "x", "r", 0.9, "t", False, "low"),  # 不可逆
        CloseLoopDecision(True, "x", "r", 0.9, "t", True, "high"),  # 高风险
        CloseLoopDecision(False, "x", "r", 0.9, "t", True, "low"),  # 不建议
    ],
)
def test_auto_eligible_blocked(d):
    assert not executor._is_auto_eligible(d)


# ---------------------------------------------------------------------------
# 执行桥（隔离全局状态）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_executor_state():
    executor._executor_fn = None
    executor._auto_mode = True
    executor._disturb.silent_mode = False
    executor._disturb.records = []
    yield
    executor._disturb.records = []


def _eligible_decision():
    return CloseLoopDecision(
        True, "把公众号的修改同步到网站", "两处内容应一致",
        0.92, "website", True, "low",
    )


def test_try_execute_falls_back_to_proposal_without_executor():
    """达门槛但未注册执行器 → 降级为提案（写入 pending），绝不静默自动执行。"""
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True
    pending = executor._PENDING_PATH.read_text(encoding="utf-8")
    assert "把公众号的修改同步到网站" in pending


def test_try_execute_runs_executor_when_registered():
    """达门槛且已注册执行器 + 自动模式开 → 真正提交执行。"""
    called = {}
    executor.register_close_loop_executor(
        lambda instr, ws: called.setdefault("hit", (instr, ws))
    )
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is True
    assert called["hit"] == ("把公众号的修改同步到网站", "/tmp/ws")


def test_try_execute_blocked_by_silent_mode():
    """防打扰静默模式 → 拦截，不做也不提案。"""
    executor._disturb.set_silent_mode(True)
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.skipped is True
    assert res.executed is False
    assert res.proposed is False


def test_try_execute_below_auto_threshold_falls_to_proposal():
    """决策建议闭环但置信度低于自动门槛 → 不自动执行，降级为提案等待用户确认。"""
    d = CloseLoopDecision(True, "x", "r", 0.5, "t", True, "low")  # 低于 AUTO 门槛
    res = executor.try_execute_close_loop(d, "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True
    assert res.skipped is False


def test_auto_mode_off_falls_back_to_proposal():
    """自动模式关 → 即便达门槛也降级为提案。"""
    executor.set_auto_mode(False)
    res = executor.try_execute_close_loop(_eligible_decision(), "/tmp/ws")
    assert res.executed is False
    assert res.proposed is True
