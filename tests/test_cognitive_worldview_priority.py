"""认知注入段顺序：「关于她」必须排在「关于他」之前（2026-11-16 萧哥拍板）。

背景（根因见 .workbuddy/memory/2026-11-16.md 18:05）：
摘掉 vault 静态档案只止住了「过期内容霸位」，没解决更根本的一条——
认知块 `cap=4500` 是**保头切尾**（`context_budget._cap_text` 取前 N 字），
而「关于萧盛」段（cognitive 142 条，实测约 15000 字）排在最前，光它自己
就吃满 4500，尾部的「你自己的看法」（worldview 17 条，约 3500 字）
**每个重活轮都整段被切** ⇒ 三个月前的「她眼中的他」每轮都在，
「她自己是谁」从未进过对话。

修法比摘档案更简单：把「关于她」段前置。她是谁是**说话立场**，
优先级高于「她知道他什么」——立场缺席时，知道得再多也只是复读。

本契约锁三件事：
1. 「你自己的看法」段必须出现在「关于萧盛」段**之前**。
2. 认知块被 `cap=4500` 截断后，worldview 段**必须仍在**（截断只许切他层）。
3. 只有他层、没有她层时行为不变（不崩、不凭空造段）。
"""

from unittest import mock

import pytest

from deskmate.cognitive import memory_injector as mi
from deskmate.memory.context_budget import _cap_text

_WV_MARKER = "我认定好故事得让人自己发现"
_HIM_MARKER = "他讨厌把历史写成爽文"


def _profile(stances: list[str]):
    """造一个 mock 认知画像（字段与 CognitiveProfile 对齐）。"""

    class _P:
        pass

    p = _P()
    p.stances = [mock.Mock(content=s, status="active", contexts=[]) for s in stances]
    p.argument_patterns = []
    p.value_rankings = []
    p.thinking_frameworks = []
    p.cognitive_gaps = []
    return p


def _fake_load(him: list[str], her: list[str]):
    def _load(layer=None, **kw):
        if layer == mi._HER_LAYER:
            return _profile(her) if her else None
        return _profile(him) if him else None

    return _load


# --------------------------------------------------------------------------- #
# 1. 顺序：关于她 → 关于他
# --------------------------------------------------------------------------- #
def test_worldview_section_precedes_his_profile():
    with mock.patch.object(
        mi, "load_cognitive_profile", side_effect=_fake_load([_HIM_MARKER], [_WV_MARKER])
    ):
        out = mi.build_cognitive_context()

    assert _WV_MARKER in out and _HIM_MARKER in out
    i_wv = out.index("## 你自己的看法")
    i_him = out.index("## 关于萧盛")
    assert i_wv < i_him, "「关于她」必须排在「关于他」之前（立场优先于知识）"


# --------------------------------------------------------------------------- #
# 2. 截断后 worldview 仍在（核心修复目标）
# --------------------------------------------------------------------------- #
def test_worldview_survives_cap_truncation():
    # 他层造 120 条长立场 ≈ 12000 字，足以吃满 4500 预算（复刻现场）
    him = [f"他讨厌把历史写成爽文，这是第 {i} 条很长的认知条目，" + "铺陈" * 30 for i in range(120)]
    with mock.patch.object(
        mi, "load_cognitive_profile", side_effect=_fake_load(him, [_WV_MARKER])
    ):
        out = mi.build_cognitive_context()

    cut = _cap_text(out, 4500)
    assert _WV_MARKER in cut, "cap=4500 截断把 worldview 切掉了——「她是谁」必须先进上下文"
    assert len(cut) <= 4600


# --------------------------------------------------------------------------- #
# 3. 只有他层：行为不变
# --------------------------------------------------------------------------- #
def test_only_his_layer_unchanged():
    with mock.patch.object(
        mi, "load_cognitive_profile", side_effect=_fake_load([_HIM_MARKER], [])
    ):
        out = mi.build_cognitive_context()

    assert _HIM_MARKER in out
    assert "## 关于萧盛" in out
    assert "## 你自己的看法" not in out, "她层没数据时不许凭空造段"


def test_no_data_returns_empty():
    with mock.patch.object(mi, "load_cognitive_profile", return_value=None):
        assert mi.build_cognitive_context() == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
