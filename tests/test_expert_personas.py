"""专家人设补强回归（2026-11-09 萧哥「把各个专家按推荐顺序补上缺口」）。

7 位圆桌专家 + 编辑专家，统一补三样：**读稿纪律 / 引用纪律 / 场合分档**；
每位再补一节可执行的「作业法」（编辑专家是审阅纪律 + 评析检查项）。
本组只锁结构，不锁具体措辞——允许以后改内容，但不许把这些节删掉。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_BUILTIN = Path("coworker/personas/builtin")

# 圆桌 7 位：作业法 + 读稿与引用纪律 + 场合
_ROUNDTABLE = ("historian", "lit-editor", "proofreader", "reader", "publisher", "adaptation", "genre")


@pytest.mark.parametrize("pid", _ROUNDTABLE)
def test_roundtable_expert_has_work_method_and_discipline(pid: str) -> None:
    text = (_BUILTIN / f"{pid}.md").read_text(encoding="utf-8")
    assert "## 你的作业法" in text, f"{pid} 缺少可执行的作业法"
    assert "## 读稿与引用纪律" in text, f"{pid} 缺少读稿/引用纪律"
    assert "## 场合" in text, f"{pid} 缺少群聊 vs 1v1 的场合分档"


def test_editor_expert_has_review_discipline() -> None:
    """编辑专家走 1v1 工具流，补的是审阅纪律 + 评析检查项，不是「作业法」。"""
    text = (_BUILTIN / "editor.md").read_text(encoding="utf-8")
    assert "## 审阅纪律" in text
    assert "读到停笔点" in text
    assert "审阅不代写、不改稿" in text
    assert "## 评析维度" in text
    assert "具体位置" in text


def test_brainstormer_keeps_two_gears() -> None:
    """脑洞专家是模板：开脑洞 / 往下盘两档不许退化成一档。"""
    text = (_BUILTIN / "brainstormer.md").read_text(encoding="utf-8")
    assert "开脑洞（默认）" in text
    assert "往下盘" in text
    assert "不排序、不验收、不替作者挑" in text

def test_experts_do_not_hang_shared_memory() -> None:
    """2026-11-09 萧哥「都拔了」：8 位创作/分析专家 + 脑洞专家不再挂 deskmate-memory。

    §24 实证：共享记忆会带偏（顺着记忆说、记忆可能过时）。只读素材库 novel-memory 保留；
    小桔本人例外——deskmate-memory 就是她自己的记忆本体。
    """
    for pid in (*_ROUNDTABLE, "editor", "brainstormer"):
        text = (_BUILTIN / f"{pid}.md").read_text(encoding="utf-8")
        assert "deskmate-memory" not in text, f"{pid} 还挂着共享记忆 deskmate-memory"
    assert "deskmate-memory" in (_BUILTIN / "deskmate.md").read_text(encoding="utf-8"), (
        "小桔本人的记忆组不能拔"
    )
