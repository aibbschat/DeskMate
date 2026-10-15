"""办公技能 seed 测试（P1：PilotDeck 办公能力借鉴）。

验证：
1. 仓库 seed 源存在且含 3 个办公技能（office-word/excel/pptx）；
2. ensure_office_skills 幂等装入（首次安装、二次跳过）；
3. 装入后 SkillStore 能识别这 3 个 office 技能；
4. 每个技能都嵌入了「神圣稿件红线」护栏（sacred_paths 引用）。

测试在临时 DESKMATE_SKILLS_DIR 下进行，不污染真实技能库。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from deskmate.skills.seed_office import ensure_office_skills

SEED_DIR = (
    Path(__file__).resolve().parents[1]
    / "deskmate" / "skills" / "seeds" / "office"
)
SKILL_NAMES = ["office-word", "office-excel", "office-pptx"]


@pytest.fixture
def tmp_skills_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="pt_office_skills_")
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", d)
    return d


def test_seed_source_exists():
    assert SEED_DIR.is_dir()
    for name in SKILL_NAMES:
        assert (SEED_DIR / f"{name}.md").is_file()


def test_ensure_office_skills_installs_and_is_idempotent(tmp_skills_dir):
    # 首次：应安装 3 个
    r1 = ensure_office_skills()
    assert set(r1["installed"]) == set(f"{n}.md" for n in SKILL_NAMES)
    assert r1["skipped"] == []

    # 二次：应全部跳过（幂等、非破坏）
    r2 = ensure_office_skills()
    assert r2["installed"] == []
    assert set(r2["skipped"]) == set(f"{n}.md" for n in SKILL_NAMES)


def test_seeded_skills_visible_in_store(tmp_skills_dir):
    from deskmate.skills.skill_store import SkillStore

    ensure_office_skills()
    store = SkillStore(skills_dir=Path(tmp_skills_dir))
    present = {s["name"] for s in store.list_skills(category="office")}
    assert set(SKILL_NAMES) <= present


def test_seeded_skills_embed_sacred_manuscript_guard(tmp_skills_dir):
    """每个办公技能都必须嵌入「神圣稿件红线」护栏（sacred_paths 引用）。"""
    ensure_office_skills()
    from deskmate.skills.skill_store import SkillStore

    store = SkillStore(skills_dir=Path(tmp_skills_dir))
    for name in SKILL_NAMES:
        full = store.read_skill(name)
        assert full is not None, f"{name} 未装入"
        body = full.get("body", "")
        assert "sacred_paths" in body, f"{name} 未嵌入神圣稿件护栏（sacred_paths）"
        assert "is_author_manuscript" in body, f"{name} 未引用 is_author_manuscript() 守卫"


def test_seeded_skills_embed_four_stage_framework(tmp_skills_dir):
    """每个办公技能都应体现「理解→构建→核验→交付」四阶段 + 交付门禁。"""
    ensure_office_skills()
    from deskmate.skills.skill_store import SkillStore

    store = SkillStore(skills_dir=Path(tmp_skills_dir))
    for name in SKILL_NAMES:
        full = store.read_skill(name)
        body = full.get("body", "")
        for token in ("理解", "构建", "核验", "交付", "REJECT"):
            assert token in body, f"{name} 缺少四阶段/交付门禁要素：{token}"
