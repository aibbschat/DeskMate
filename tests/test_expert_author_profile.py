"""专家基础记忆（作者档案）回归（2026-11-09）。

萧哥：让专家知道「我是谁、干什么的、写过哪些书」；如果已知就忽略。
实现不新建记忆层：从既有 ~/.deskmate/USER.md + 书架动态拼，只注入给创作/分析专家，
小桔等有自己记忆的 persona 不重复注入。
"""
from __future__ import annotations

from deskmate.bots.author import author_profile, author_profile_for


def test_author_profile_has_identity_and_books() -> None:
    block = author_profile()
    assert "作者" in block
    assert "萧" in block
    assert "写作者" in block or "历史小说" in block
    assert "AI 不代笔" in block


def test_author_profile_only_injected_for_experts() -> None:
    for pid in ("brainstormer", "lit-editor", "adaptation", "historian", "editor", "outline", "character"):
        assert author_profile_for(pid), f"{pid} 应该拿到作者档案"
    for pid in ("cowork", "deskmate", "ops", ""):
        assert author_profile_for(pid) == "", f"{pid} 不该重复注入"
