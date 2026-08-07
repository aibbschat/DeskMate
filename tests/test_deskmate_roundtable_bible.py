"""圆桌圣经注入的回归测试：典故人物（allusion）不得被当本作角色检索 / 讨论。

背景：国子监全量重建后，圣经『人物』段混入了嬴政、王安石等仅角色对白中提及的
历史人物。圆桌有两个环节会误用它们：
1. `_bible_summary` 的 guard 声明（已补充「典故段非本作角色」约束）；
2. `_bible_character_names` 把圣经人名当全书 FTS5 检索兜底词——若把嬴政当检索词，
   会召回对白里"某人说当年嬴政如何"的片段，强化"这是角色"的错觉。

本文件锁定第 2 点：提取主角名时必须只取「本作角色」小节，排除「典故提及」小节。
"""
from __future__ import annotations

from deskmate.roundtable.coordinator import Roundtable


def _rt_with_bible(characters_md: str) -> Roundtable:
    rt = Roundtable(topic="评审", manuscript_path="/tmp/does-not-exist")
    # 绕过懒加载：直接灌入缓存
    rt._bible_loaded = True
    rt._bible_cache = {"CHARACTERS": characters_md}
    return rt


def test_character_names_excludes_allusion():
    """圆桌提取『主角名』做全书检索兜底时，必须排除典故人物（如嬴政）。"""
    md = (
        "## CHARACTERS\n\n"
        "### 人物（本作角色）\n"
        "- **韩笑文**：主角\n"
        "- **董文正**：配角\n\n"
        "### 对话／典故中提及的历史人物（非本作角色）\n"
        "- **嬴政**：对白举例\n"
        "- **王安石**：对白举例\n"
    )
    rt = _rt_with_bible(md)
    names = rt._bible_character_names(20)
    assert "韩笑文" in names
    assert "董文正" in names
    assert "嬴政" not in names
    assert "王安石" not in names


def test_character_names_backward_compat_no_group_marker():
    """旧格式圣经（无分组标记）应退回整段提取，不报错也不漏掉本作角色。"""
    md = (
        "## CHARACTERS\n\n"
        "### 人物\n"
        "- **韩笑文**：主角\n"
        "- **董文正**：配角\n"
    )
    rt = _rt_with_bible(md)
    names = rt._bible_character_names(20)
    assert "韩笑文" in names
    assert "董文正" in names
