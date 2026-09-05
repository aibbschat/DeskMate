"""回归：人格指纹「创意型」不得随事件窗口滚动而倒退。

成长引擎的 ``recent_events`` 是 50 条易失窗口（``EVENT_LOG_CAP``）。截断发生在
**落盘**时（``state.py`` 的 ``recent_events[-EVENT_LOG_CAP:]``），所以进程重启后
才看得到效果 —— 测试必须重新加载引擎才能复现真实场景。

项目自己在 ``_event_log_is_complete`` 里就明确规避过这个窗口坑。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deskmate.growth.engine import GrowthEngine
from deskmate.growth.state import SkillProficiency


def _fresh() -> GrowthEngine:
    return GrowthEngine(base_dir=Path(tempfile.mkdtemp()))


def _scroll_window(eng: GrowthEngine, n: int = 60) -> None:
    """灌入一批无关事件并落盘（截断只发生在落盘时）。"""
    for _ in range(n):
        eng._add_event("chat")
    eng.save()


def _reload(eng: GrowthEngine) -> GrowthEngine:
    """重新加载引擎，等价于进程重启后读到被截断的事件窗口。"""
    fresh = GrowthEngine(base_dir=eng.base_dir)
    assert len(fresh.state.recent_events) <= 50, "重新加载后窗口应已被截断"
    return fresh


def test_creativity_survives_event_window_scrolling():
    """滚出窗口的技能事件不能让创意维度倒退。

    回归背景：creative_events 曾直接统计 recent_events，老用户早期技能事件被
    挤出窗口后归零，creativity 从 100 掉回 40+ —— 人格指纹随时间倒退。
    """
    eng = _fresh()
    eng.record_skill_created("skill-a")
    for _ in range(30):
        eng.record_skill_evolved("skill-a")
    high = eng.snapshot()["personality"]["creativity"]

    _scroll_window(eng)
    after = _reload(eng).snapshot()["personality"]["creativity"]

    assert after >= high, f"窗口滚动后创意维度不应倒退：{high} -> {after}"


def test_creativity_monotonic_as_skills_accumulate():
    """技能持续累积时创意维度单调不减，不因窗口抖动忽高忽低。"""
    eng = _fresh()
    seen: list[int] = []
    for i in range(5):
        eng.record_skill_created(f"skill-{i}")
        for _ in range(10):
            eng.record_skill_evolved(f"skill-{i}")
        _scroll_window(eng, 20)  # 每轮之间让窗口滚动一次
        eng = _reload(eng)
        seen.append(eng.snapshot()["personality"]["creativity"])

    assert seen == sorted(seen), f"创意维度应单调不减：{seen}"
    assert seen[-1] > seen[0], "技能持续累积时创意维度应当有增长"


def test_creativity_falls_back_to_window_for_legacy_state():
    """计数器尚未累积的存量用户退回窗口统计，升级瞬间不会归零。"""
    eng = _fresh()
    # 直接构造「有技能事件但没有计数器」的存量状态
    eng.state.skill_proficiency["legacy-skill"] = SkillProficiency()
    eng._add_event("skill_created", skill_name="legacy-skill")
    eng._add_event("skill_evolved", skill_name="legacy-skill")

    assert eng.state.counters.get("skills_created", 0) == 0
    personality = eng._derive_personality()
    assert personality["creativity"] > 40, "存量用户的技能事件仍应计入，不应归零"
