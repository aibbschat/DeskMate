"""P0-7 curator 自控清理测试。

验证：
- auto_archive=False（默认）只报告、不归档（非破坏性）；
- auto_archive=True 仅归档「从未使用 + 陈旧 + 高重叠」的严格低价值技能，
  唯一技能（无重叠）即使陈旧也不归档；
- restore_skill 从 archive/ 一键还原往返；
- auto_archive_enabled() 受 env DESKMATE_CURATOR_AUTO_ARCHIVE 控制。

全部用临时技能库，不碰真实 ~/.deskmate / 神圣稿件。
"""
import os
import time
from pathlib import Path

import pytest

from deskmate.skills import curator
from deskmate.skills.skill_store import SkillStore


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    """把技能库固定到临时目录，并清掉自动归档开关，保证用例隔离。"""
    sd = tmp_path / "skills"
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(sd))
    monkeypatch.delenv("DESKMATE_CURATOR_AUTO_ARCHIVE", raising=False)
    return sd


def _make_skill(skills_dir: Path, category: str, name: str, *, description: str,
                body: str, uses: int = 0, last_used: str = "",
                mtime_days_ago: int | None = None, tags: str = "", trigger: str = ""):
    cat_dir = skills_dir / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    p = cat_dir / (name + ".md")
    fm = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"tags: [{tags}]",
        f"trigger: {trigger}",
        f"uses: {uses}",
        f"last_used: {last_used}",
        f"category: {category}",
        "---",
        "",
        body,
    ]
    p.write_text("\n".join(fm), encoding="utf-8")
    if mtime_days_ago is not None:
        old = time.time() - mtime_days_ago * 86400.0
        os.utime(p, (old, old))
    return p


def _seed(skills_dir: Path):
    """三个技能：A 与 B 高度重叠（B 在用），C 唯一且陈旧。

    A：从未使用 + 陈旧 + 与 B 高重叠 → 严格低价值（应可被自动归档）
    B：与 A 高重叠，但 uses>0 → 不是归档候选，仅作 A 的重叠证据
    C：从未使用 + 陈旧，但内容唯一（无重叠）→ 不应被自动归档
    """
    # A 与 B 用几乎相同的中文描述/正文，保证 Jaccard 高重叠
    shared_desc = "自动生成每日工作日报，汇总当天任务与进度"
    shared_body = "帮助用户快速写日报，提取已完成事项，生成简洁日报模板"
    _make_skill(skills_dir, "general", "daily-report-helper",
                description=shared_desc, body=shared_body,
                uses=0, mtime_days_ago=200, tags="日报", trigger="写日报")
    _make_skill(skills_dir, "general", "daily-report-pro",
                description=shared_desc, body=shared_body,
                uses=5, last_used="2026-08-29", tags="日报", trigger="写日报")
    _make_skill(skills_dir, "writing", "unique-poem-writer",
                description="写古典诗词，讲究押韵对仗",
                body="根据主题创作七言绝句，注重平仄与意境",
                uses=0, mtime_days_ago=200, tags="诗词", trigger="写诗")
    return skills_dir


def test_auto_archive_disabled_does_not_archive(tmp_path, skills_env):
    sd = _seed(skills_env)
    result = curator.curate_skills(tmp_path, auto_archive=False)
    assert result["ok"] is True
    # 默认非破坏性：一件都不归档
    assert result["archived"] == []
    # 但宽候选已识别（A、C 都满足 从未使用+陈旧）
    names = {c["name"] for c in result["archive_candidates"]}
    assert "daily-report-helper" in names
    assert "unique-poem-writer" in names
    # 严格自动归档候选仅含与 B 高重叠的 A
    strict = {c["name"] for c in result["auto_archive_candidates"]}
    assert strict == {"daily-report-helper"}


def test_auto_archive_enabled_archives_only_overlapping(tmp_path, skills_env):
    sd = _seed(skills_env)
    result = curator.curate_skills(tmp_path, auto_archive=True)
    assert result["ok"] is True
    # 仅归档「从未使用 + 陈旧 + 高重叠」的 A；唯一技能 C 不动
    assert result["archived"] == ["daily-report-helper"]
    # 文件确实移入 archive/
    assert (sd / "archive" / "daily-report-helper.md").is_file()
    # 唯一技能 C 仍在原处（未被误归档）
    assert (sd / "writing" / "unique-poem-writer.md").is_file()


def test_restore_skill_roundtrip(tmp_path, skills_env):
    sd = _seed(skills_env)
    # 先自动归档 A
    curator.curate_skills(tmp_path, auto_archive=True)
    assert (sd / "archive" / "daily-report-helper.md").is_file()
    # 一键还原
    store = SkillStore(skills_dir=sd)
    res = store.restore_skill("daily-report-helper")
    assert res.get("status") == "ok"
    # 还原回 general/，archive/ 清空
    assert (sd / "general" / "daily-report-helper.md").is_file()
    assert not (sd / "archive" / "daily-report-helper.md").is_file()
    # 还原后重新出现在技能列表
    names = {s["name"] for s in store.list_skills(limit=50)}
    assert "daily-report-helper" in names


def test_restore_missing_returns_error(tmp_path, skills_env):
    sd = _seed(skills_env)
    store = SkillStore(skills_dir=sd)
    res = store.restore_skill("does-not-exist")
    assert res.get("status") == "error"


def test_auto_archive_enabled_env_var(monkeypatch):
    monkeypatch.delenv("DESKMATE_CURATOR_AUTO_ARCHIVE", raising=False)
    assert curator.auto_archive_enabled() is False
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("DESKMATE_CURATOR_AUTO_ARCHIVE", v)
        assert curator.auto_archive_enabled() is True
    monkeypatch.setenv("DESKMATE_CURATOR_AUTO_ARCHIVE", "false")
    assert curator.auto_archive_enabled() is False
