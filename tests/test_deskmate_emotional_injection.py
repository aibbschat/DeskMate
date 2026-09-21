"""情感画像不受「分身模式」门控（2026-09-21）。

病根
----
``manager.build_full_context_for_turn`` 里，认知画像与情感画像**共用同一个开关**：

    if alter_ego_enabled() and has_cognitive_data():
        parts.append(build_cognitive_context())   # 里面含 cognitive + emotional

而 ``alter_ego``（分身模式）**默认关闭** → ``emotional`` 层（她记着的他的情绪规律）
**从来没进过对话**。可"记得他什么时候低落、靠什么缓过来"是「懂你」的地基，
不是"用他的腔调写作"的附属品。

放行范围**只有 emotional 层**：人格底座 / 认知层 / 世界观仍按原样归分身与世界管道管。
分身开着时依旧走 ``build_cognitive_context()``（已含本层，不重复注入）。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from deskmate.cognitive import memory_injector as mi
from deskmate.memory.manager import DeskMateMemoryManager

_EMO = "## 关于萧盛的情绪——你记着的\n- 他写顺的时候情绪明显变好，卡住时靠重读史料缓过来。"
_FULL = "## 关于萧盛——你认识的这个人\n- 认知层全文。"


class _Item:
    def __init__(self, content):
        self.content = content
        self.domain = ""
        self.ranking = []
        self.steps = []
        self.contexts = []
        self.status = "active"


class _Profile:
    stances = [_Item("写作顺利时情绪变好。")]
    value_rankings = []
    argument_patterns = []
    thinking_frameworks = []
    cognitive_gaps = []


class TestBuildEmotionalContext:
    def test_returns_formatted_when_profile_present(self):
        with mock.patch.object(mi, "load_cognitive_profile", return_value=_Profile()) as lp, \
             mock.patch.object(mi, "format_profile", return_value="- 情绪条目"):
            out = mi.build_emotional_context()
        assert "情绪条目" in out
        assert "情绪" in out.split("\n", 1)[0]
        lp.assert_called_once_with("emotional")

    def test_empty_when_no_profile(self):
        with mock.patch.object(mi, "load_cognitive_profile", return_value=None):
            assert mi.build_emotional_context() == ""

    def test_empty_when_format_yields_nothing(self):
        with mock.patch.object(mi, "load_cognitive_profile", return_value=_Profile()), \
             mock.patch.object(mi, "format_profile", return_value=""):
            assert mi.build_emotional_context() == ""

    def test_fail_open_on_exception(self):
        with mock.patch.object(mi, "load_cognitive_profile", side_effect=RuntimeError("boom")):
            assert mi.build_emotional_context() == ""

    def test_only_emotional_layer_is_read(self):
        """不许顺手把认知层 / 世界观也放行——那是本次刻意圈定的边界。"""
        seen: list[str] = []

        def _record(layer="cognitive"):
            seen.append(layer)
            return None

        with mock.patch.object(mi, "load_cognitive_profile", side_effect=_record):
            mi.build_emotional_context()
        assert seen == ["emotional"]


class TestManagerWiring:
    def _mgr(self, tmp_path: Path) -> DeskMateMemoryManager:
        return DeskMateMemoryManager(tmp_path)

    def test_emotional_injected_when_alter_ego_off(self, tmp_path: Path):
        mgr = self._mgr(tmp_path)
        with mock.patch.object(mgr, "prefetch", return_value=""), \
             mock.patch("deskmate.alter_ego.config.is_enabled", return_value=False), \
             mock.patch.object(mi, "build_emotional_context", return_value=_EMO) as be:
            ctx = mgr.build_full_context_for_turn("今天怎么样")
        assert "重读史料缓过来" in ctx, "分身关着时情感画像必须仍然注入"
        be.assert_called_once()

    def test_full_cognitive_used_when_alter_ego_on(self, tmp_path: Path):
        mgr = self._mgr(tmp_path)
        with mock.patch.object(mgr, "prefetch", return_value=""), \
             mock.patch("deskmate.alter_ego.config.is_enabled", return_value=True), \
             mock.patch.object(mi, "has_cognitive_data", return_value=True), \
             mock.patch.object(mi, "build_cognitive_context", return_value=_FULL), \
             mock.patch.object(mi, "build_emotional_context", return_value=_EMO) as be:
            ctx = mgr.build_full_context_for_turn("今天怎么样")
        assert "认知层全文" in ctx
        assert "重读史料缓过来" not in ctx, "分身开着时不该再单独注入一遍情绪层"
        be.assert_not_called()

    def test_fail_open_when_emotional_lookup_raises(self, tmp_path: Path):
        mgr = self._mgr(tmp_path)
        with mock.patch.object(mgr, "prefetch", return_value=""), \
             mock.patch("deskmate.alter_ego.config.is_enabled", return_value=False), \
             mock.patch.object(mi, "build_emotional_context", side_effect=RuntimeError("x")):
            # fail-open：情绪画像取不到时不许抛，其余块照常
            ctx = mgr.build_full_context_for_turn("hi")
        assert "重读史料缓过来" not in ctx
