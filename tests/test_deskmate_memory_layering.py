"""P0-6 记忆分层净化 + 时间感知 回归测试。

验证：
- clean_global_memory 把书稿/项目分节从全局 MEMORY.md 迁到独立项目记忆（带 .bak 可逆）
- 净化后全局仅剩「关于你」的内容，无 #project-* 残留
- memory_stale_rate / memory_age_stats 返回合法比率
- extract_session_facts 把书稿事实路由到项目记忆而非全局（防再污染）
- _is_book_content 判别准确（不误伤「关于你」的事实）
- 8.1 方向盘 runner 含 memory_stale_rate 指标
不依赖真实 LLM，使用 monkeypatch 注入抽取结果。
"""

import importlib
import re

import pytest

from deskmate.memory import manager as mgr_mod
from deskmate.memory import project_memory as pm
from deskmate.memory.manager import DeskMateMemoryManager


def _write_memory(base, text):
    (base / "MEMORY.md").write_text(text, encoding="utf-8")


POLLUTED_MEMORY = """§ 偏好 #preference
用户喜欢用 VS Code 写代码。

§ 纠正 #correction
2026-08-03: 未读完正文不妄下结论。

§ project-structure #project-structure
单元剧+暗线汇聚，每章独立故事，陈云远蛰伏后回归。

§ project-progress #project-progress
2026-08-09: 《衙探》第二章已写到周全被审讯一场。
"""


def test_clean_global_memory_migrates_book_sections(tmp_path):
    _write_memory(tmp_path, POLLUTED_MEMORY)
    m = DeskMateMemoryManager(base_dir=tmp_path)

    before = [s["id"] for s in m._parse_sections(m._load_global_memory())[1]]
    assert any("project" in s.lower() for s in before)

    res = m.clean_global_memory()
    assert res["sections_touched"] == 2
    assert "衙探" in res["project_names"]

    after = [s["id"] for s in m._parse_sections(m._load_global_memory())[1]]
    # 全局只剩「关于你」的偏好/纠正，无书稿分节
    assert "偏好" in after and "纠正" in after
    assert not any("project" in s.lower() for s in after)

    # 项目记忆已建立且内容落地
    proj_files = list(pm.load_project_memory(tmp_path, "衙探").keys())
    assert "project-progress.md" in proj_files
    assert "《衙探》第二章" in pm.load_project_memory(tmp_path, "衙探")["project-progress.md"]

    # 备份可还原
    assert (tmp_path / "MEMORY.md.bak").exists()


def test_clean_global_memory_keeps_about_you_sections(tmp_path):
    """「关于你」的分节（偏好/纠正/读书记录）整体保留，不被条目级误伤。"""
    mem = """§ 偏好 #preference
用户喜欢用 VS Code 写代码。

§ 纠正 #correction
2026-08-03: 未读完正文不妄下结论。

§ 读书记录 #reading
2026-08-25: 读完《兰陵王》。
"""
    _write_memory(tmp_path, mem)
    m = DeskMateMemoryManager(base_dir=tmp_path)
    res = m.clean_global_memory()
    assert res["sections_touched"] == 0  # 无书稿内容
    after = [s["id"] for s in m._parse_sections(m._load_global_memory())[1]]
    assert "偏好" in after and "纠正" in after and "读书记录" in after


def test_clean_global_memory_entry_level_mixed_section(tmp_path):
    """混杂分节（如「工作」）只迁出书稿条目，非书稿条目留在全局。"""
    mem = """§ 工作 #work
2026-08-03: 第二章"财当道"前三节设定确认——上舍试舞弊引爆政治伦理事件。

2026-08-01: 专家模式正式落地，测试全部通过。

2026-08-04: 新增韩笑文人物设定：父亲为殿中侍御史。
"""
    _write_memory(tmp_path, mem)
    m = DeskMateMemoryManager(base_dir=tmp_path)
    res = m.clean_global_memory()
    # 2 条书稿（第二章/韩笑文设定）迁出，1 条非书稿（专家模式）留全局
    assert res["entries_migrated"] == 2
    after = [s["id"] for s in m._parse_sections(m._load_global_memory())[1]]
    assert "工作" in after
    work_body = m._parse_sections(m._load_global_memory())[1][after.index("工作")]["body"]
    assert "专家模式正式落地" in work_body          # 非书稿留存
    assert "第二章" not in work_body                 # 书稿已迁出
    assert "韩笑文人物设定" not in work_body


def test_clean_global_memory_idempotent_when_no_book(tmp_path):
    _write_memory(tmp_path, "§ 偏好 #preference\n用户喜欢喝咖啡。\n")
    m = DeskMateMemoryManager(base_dir=tmp_path)
    res = m.clean_global_memory()
    assert res["sections_touched"] == 0
    assert res["notes"]  # 有说明文案


def test_memory_stale_rate_valid_range(tmp_path):
    _write_memory(tmp_path, POLLUTED_MEMORY)
    m = DeskMateMemoryManager(base_dir=tmp_path)
    rate = m.memory_stale_rate()
    assert 0.0 <= rate <= 1.0
    stats = m.memory_age_stats()
    assert stats["total"] >= 1
    assert stats["stale_days"] == 180


def test_is_book_content_distinguishes(tmp_path):
    m = DeskMateMemoryManager(base_dir=tmp_path)
    assert m._is_book_content("第二章官称已定稿") is True
    assert m._is_book_content("《衙探》人物设定") is True
    assert m._is_book_content("用户喜欢喝咖啡") is False
    assert m._is_book_content("用户纠正我别瞎编") is False


def test_resolve_project_name_from_title(tmp_path):
    m = DeskMateMemoryManager(base_dir=tmp_path)
    assert m._resolve_project_name("《衙探》第二章进度") == "衙探"
    # 无书名、无 active_project → None
    assert m._resolve_project_name("用户喜欢咖啡") is None


def test_extract_session_facts_routes_book_to_project(tmp_path, monkeypatch):
    """书稿事实应从创作第一刻就进项目记忆，而非污染全局 MEMORY.md。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.md").write_text(
        "## User\n《衙探》第二章大纲定了吗\n\n## Assistant\n已定稿。\n", encoding="utf-8"
    )

    monkeypatch.setattr(mgr_mod, "memory_llm_available", lambda: True)
    # 抽取器返回一条书稿 work 事实（含书名，便于解析项目名）
    monkeypatch.setattr(
        mgr_mod,
        "chat_memory",
        lambda *a, **k: '[{"category":"work","text":"2026-08-29: 《衙探》第二章大纲已定稿"}]',
    )
    # 去重检查直接放行
    monkeypatch.setattr(DeskMateMemoryManager, "_is_duplicate", lambda self, t: False)

    m = DeskMateMemoryManager(base_dir=tmp_path)
    result = m.extract_session_facts(limit=1)
    assert result["written"] == 1

    # 全局 MEMORY.md 不应出现该书稿分节
    global_ids = [s["id"] for s in m._parse_sections(m._load_global_memory())[1]]
    assert not any(s.lower().startswith("project") for s in global_ids)
    # 项目记忆里应有 work.md
    assert "work.md" in pm.load_project_memory(tmp_path, "衙探")
    assert "第二章大纲已定稿" in pm.load_project_memory(tmp_path, "衙探")["work.md"]


def test_extract_session_facts_non_book_stays_global(tmp_path, monkeypatch):
    """非书稿事实仍走全局，行为不回归。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.md").write_text(
        "## User\n以后都用 VS Code\n\n## Assistant\n好的。\n", encoding="utf-8"
    )
    monkeypatch.setattr(mgr_mod, "memory_llm_available", lambda: True)
    monkeypatch.setattr(
        mgr_mod,
        "chat_memory",
        lambda *a, **k: '[{"category":"preference","text":"2026-08-29: 用户偏好用 VS Code"}]',
    )
    monkeypatch.setattr(DeskMateMemoryManager, "_is_duplicate", lambda self, t: False)

    m = DeskMateMemoryManager(base_dir=tmp_path)
    m.extract_session_facts(limit=1)
    # 偏好写入 USER.md（非全局 MEMORY.md 的 project 分节）
    assert "VS Code" in (tmp_path / "USER.md").read_text(encoding="utf-8")


def test_runner_has_memory_stale_rate_metric():
    """8.1 方向盘包含 P0-6 陈旧率指标。"""
    runner = importlib.import_module("tests.memory_bench.runner")
    import tempfile

    from tests.memory_bench.fixture_xiaosheng import build_xiaosheng_fixture

    d = tempfile.mkdtemp(prefix="membench_p06_")
    build_xiaosheng_fixture(__import__("pathlib").Path(d))
    rep = runner.evaluate(__import__("pathlib").Path(d))
    assert "memory_stale_rate" in rep
    assert rep["memory_stale_rate"] is not None
    assert 0.0 <= rep["memory_stale_rate"] <= 1.0
