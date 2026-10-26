"""方案乙回归：小桔生成的技能必须落进 Coworker 规范技能池，被 skill_list 可见。

红→绿：
- 小桔 skill_create / 自动蒸馏落盘后，应在 DESKMATE_COWORKER_SKILLS_DIR（即
  coworker 规范全局技能目录 ~/.config/coworker/skills）下产出 <slug>/SKILL.md；
- coworker 的 SkillLoader（effective_skill_names 同款扫描）能读到它的 name/description；
- 小桔自己的 list_skills(include_coworker=True) 也能列出该技能（同池、不分裂）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coworker.skills.base import _parse_skill

import deskmate.skills.skill_store as ds
from deskmate.skills.skill_store import SkillStore


@pytest.fixture()
def cw_dir(tmp_path, monkeypatch):
    """重定向发布目标到临时目录，模拟生产 ~/.config/coworker/skills。"""
    d = tmp_path / "coworker-skills"
    monkeypatch.setenv("DESKMATE_COWORKER_SKILLS_DIR", str(d))
    return d


@pytest.fixture()
def flat_store(tmp_path, monkeypatch):
    """小桔扁平技能库（受 DESKMATE_SKILLS_DIR 影响）。"""
    d = tmp_path / "deskmate-skills"
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(d))
    return SkillStore()


def _cw_skills(cw_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not cw_dir.is_dir():
        return out
    for sub in sorted(cw_dir.iterdir()):
        md = sub / "SKILL.md"
        if md.is_file():
            out[sub.name] = md
    return out


def test_create_publishes_to_coworker_pool(flat_store, cw_dir):
    """skill_create 必须在 coworker 规范目录产出 SKILL.md。"""
    flat_store.create_skill(
        name="写作营发布",
        description="把成稿发布到写作营网站",
        body="## 步骤\n1. 整理成稿\n2. 调发布接口",
        tags=["写作", "发布"],
        trigger="用户让你发到写作营时",
        category="writing",
    )
    skills = _cw_skills(cw_dir)
    assert skills, "coworker 规范目录下没有生成任何 SKILL.md"
    # 中文名 slug 退化为 hash，直接取唯一一个解析
    assert len(skills) == 1
    skill = _parse_skill(next(iter(skills.values())))
    assert skill.name == "写作营发布"
    assert skill.description == "把成稿发布到写作营网站"
    assert "整理成稿" in skill.instructions


def test_coworker_loader_sees_published_skill(flat_store, cw_dir):
    """模拟 effective_skill_names 的 SkillLoader 扫描能识别小桔技能。"""
    from coworker.skills.base import SkillLoader

    flat_store.create_skill(
        name="pdf-review",
        description="审校 PDF 稿件",
        body="步骤...",
        category="office",
    )
    loader = SkillLoader([cw_dir])
    assert "pdf-review" in loader.names()
    assert loader.get("pdf-review").description == "审校 PDF 稿件"


def test_list_includes_coworker_only_skill(flat_store, cw_dir):
    """coworker 端独有技能（如用户用 save_skill 创建）也应被小桔列表看到。"""
    # 模拟一个只存在于 coworker 池、不在小桔扁平库的技能
    only_md = cw_dir / "only-cw" / "SKILL.md"
    only_md.parent.mkdir(parents=True, exist_ok=True)
    only_md.write_text(
        "---\nname: 只属于coworker池\ndescription: 由 save_skill 创建\n---\n\n正文\n",
        encoding="utf-8",
    )
    # 默认（扁平库）不应包含它
    default = flat_store.list_skills()
    assert not any(s["name"] == "只属于coworker池" for s in default)
    # 合并视图应包含
    merged = flat_store.list_skills(include_coworker=True)
    assert any(s["name"] == "只属于coworker池" for s in merged)


def test_patch_republishes_to_coworker(flat_store, cw_dir):
    """修补技能后，coworker 池里的副本也应同步更新。"""
    flat_store.create_skill(
        name="test-patch",
        description="原始描述",
        body="原始正文",
        category="general",
    )
    flat_store.patch_skill("test-patch", "原始正文", "更新后的正文")
    skills = _cw_skills(cw_dir)
    assert skills
    skill = _parse_skill(next(iter(skills.values())))
    assert "更新后的正文" in skill.instructions
    assert "原始正文" not in skill.instructions


def test_migrate_legacy_flat_skills(flat_store, cw_dir):
    """存量小桔扁平技能应可一键发布到 coworker 池（幂等）。"""
    flat_store.create_skill(
        name="legacy-skill",
        description="历史技能",
        body="历史正文",
        category="coding",
    )
    # 清空发布目录，模拟「从未发布过」的存量场景
    import shutil

    if cw_dir.is_dir():
        shutil.rmtree(cw_dir)
    cw_dir.mkdir(parents=True, exist_ok=True)

    ds.migrate_legacy_flat_skills(flat_store)
    skills = _cw_skills(cw_dir)
    assert skills
    skill = _parse_skill(next(iter(skills.values())))
    assert skill.name == "legacy-skill"
    assert "历史正文" in skill.instructions
