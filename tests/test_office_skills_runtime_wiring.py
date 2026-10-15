"""P2 接进验证：office 套件（docx/pptx/xlsx）能被小桔自主运行时的 SkillManager 索引命中。

小桔自主运行时（coworker agent）经 `SkillManager.build_skill_index()` 把
`~/.deskmate/skills` 下的技能注入 <available_skills> 系统 prompt，再由
`match_skills` 按用户消息匹配。本测试用临时技能目录复刻该路径，断言 office 三技能
确实出现在索引中、且能被「做个 PPT / 表格 / Word」类请求匹配——即「办公套件已接进
自主运行时」的回归护栏。
"""
from __future__ import annotations

from pathlib import Path

from deskmate.skills.manager import SkillManager
from deskmate.skills.seed_office import ensure_office_skills


def _seed_into(tmp_path: Path, monkeypatch) -> Path:
    """ensure_office_skills 按设计走 DESKMATE_SKILLS_DIR（不读 base_dir），故用 env 隔离。"""
    skills_dir = tmp_path / "skills"
    monkeypatch.setenv("DESKMATE_SKILLS_DIR", str(skills_dir))
    ensure_office_skills(force=True)
    return skills_dir


def test_office_skills_present_in_runtime_skill_index(tmp_path: Path, monkeypatch):
    skills_dir = _seed_into(tmp_path, monkeypatch)
    idx = SkillManager(skills_dir=skills_dir).build_skill_index()
    assert "office-word" in idx
    assert "office-excel" in idx
    assert "office-pptx" in idx


def test_office_skills_match_office_requests(tmp_path: Path, monkeypatch):
    skills_dir = _seed_into(tmp_path, monkeypatch)
    mgr = SkillManager(skills_dir=skills_dir)

    cases = {
        "帮我做个 PPT 汇报一下季度数据": "office-pptx",
        "把这组数整理成 Excel 表格": "office-excel",
        "新建一份 Word 报告": "office-word",
    }
    for msg, expected in cases.items():
        matched = mgr.match_skills(msg, limit=3)
        names = {m["name"] for m in matched}
        assert expected in names, f"『{msg}』未命中 {expected}，命中: {names}"
