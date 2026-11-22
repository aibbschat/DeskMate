"""will 关系状态接通契约（2026-11-20 萧哥拍板）。

红线：情绪/表达类禁硬约束/模板/映射表——系统只给真实状态，不给表达方式。
因此 modulate_tone_prompt 只允许陈述亲密度/关系阶段/他亲口说过的立场，
不得出现任何"语气应该怎样"的演出指令；且必须挂在真实主聊天链路上。
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def growth(tmp_path):
    from deskmate.growth.engine import GrowthEngine

    return GrowthEngine(base_dir=str(tmp_path))


def test_relation_state_is_fact_not_expression_directive(growth):
    """高亲密度 + 高关系阶段：只出事实，不出语气演出指令。"""
    growth.state.intimacy = 80.0
    growth.state.bond_depth = 200.0
    out = growth.modulate_tone_prompt()
    assert "亲密度 80" in out
    assert "知己" in out  # 关系阶段是事实，不是表达方式
    for banned in ("老朋友", "语气", "拘谨", "礼貌友善", "自来熟", "客套", "自然地用"):
        assert banned not in out, f"出现演出指令：{banned} / {out}"


def test_low_intimacy_stays_silent(growth):
    growth.state.intimacy = 0
    assert growth.modulate_tone_prompt() == ""


def test_will_stance_survives_as_fact(tmp_path, monkeypatch, growth):
    """will 的「他表达过」是用户真实立场，保留；截断规则不变。"""
    from deskmate.will import digest as dm
    from deskmate.will.digest import WillDigest
    from deskmate.will.settings import WillSettings

    d = WillDigest(
        available=True,
        top_stances=[("t1", "我要的是短句", 0.9), ("t2", "别写长段落", 0.8)],
    )
    monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
    monkeypatch.setattr(
        "deskmate.will.settings.load_will_settings",
        lambda base_dir=None: WillSettings(enabled=True),
    )
    growth.state.intimacy = 60
    out = growth.modulate_tone_prompt()
    assert "他表达过" in out
    assert "我要的是短句" in out


def test_main_chat_injects_relation_state(tmp_path, monkeypatch):
    """主聊天每轮走 build_full_context_for_turn，必须带上关系状态块。"""
    from deskmate.memory.manager import DeskMateMemoryManager

    mm = DeskMateMemoryManager(base_dir=str(tmp_path))
    marker = "关系状态：亲密度 80"
    monkeypatch.setattr(
        "deskmate.growth.engine.GrowthEngine.modulate_tone_prompt",
        lambda self: marker + "（知己）。",
    )
    out = mm.build_full_context_for_turn("hi")
    assert marker in out


def test_old_system_prompt_mount_removed():
    """旧挂载点（build_system_prompt）不得再注入关系状态，避免 MCP read(all) 重复。"""
    src = (_REPO / "deskmate" / "memory" / "manager.py").read_text(encoding="utf-8")
    start = src.index("def build_system_prompt(")
    end = src.index("\n    def ", start + 10)
    block = src[start:end]
    assert "modulate_tone_prompt" not in block
    assert "与用户的关系" not in block
