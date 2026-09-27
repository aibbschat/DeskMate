"""Tests for deskmate.memory module."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from deskmate.memory.soul import (
    load_soul,
    save_soul,
    reset_soul,
    evolve_soul,
    _DEFAULT_SOUL,
)
from deskmate.memory.user_profile import (
    load_user,
    save_user,
    reset_user,
    _DEFAULT_USER,
)
from deskmate.memory.project_memory import (
    list_projects,
    load_project_memory,
    read_project_file,
    write_project_file,
    delete_project_file,
    format_project_memory,
)
from deskmate.memory.manager import DeskMateMemoryManager, MEMORY_CONTEXT_MAX_CHARS
import deskmate.memory.manager as mgr_mod
from deskmate.memory import llm as llm_mod


@pytest.fixture
def tmp_dir(tmp_path: Path):
    """Create a temp deskmate directory with expected structure."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "projects").mkdir()
    return tmp_path


# ============================================================
# SOUL
# ============================================================


class TestSoul:
    def test_load_soul_creates_default_if_missing(self, tmp_dir: Path):
        content = load_soul(tmp_dir)
        assert "DeskMate" in content
        assert "人格运行时状态" in content

    def test_load_soul_preserves_existing(self, tmp_dir: Path):
        (tmp_dir / "SOUL.md").write_text("custom soul", encoding="utf-8")
        content = load_soul(tmp_dir)
        assert content == "custom soul"

    def test_save_and_load_roundtrip(self, tmp_dir: Path):
        save_soul(tmp_dir, "custom soul")
        assert load_soul(tmp_dir) == "custom soul"

    def test_reset_soul_restores_default(self, tmp_dir: Path):
        save_soul(tmp_dir, "modified")
        reset_soul(tmp_dir)
        content = load_soul(tmp_dir)
        assert "DeskMate" in content

    def test_evolve_soul_appends_evolution(self, tmp_dir: Path):
        load_soul(tmp_dir)  # create default
        result = evolve_soul(tmp_dir, "用户喜欢简洁回答")
        assert "简洁回答" in result

    def test_default_soul_contains_key_sections(self):
        assert "人格运行时状态" in _DEFAULT_SOUL
        assert "本层职责" in _DEFAULT_SOUL
        assert "演化护栏" in _DEFAULT_SOUL


# ============================================================
# USER PROFILE
# ============================================================


class TestUserProfile:
    def test_load_user_creates_default_if_missing(self, tmp_dir: Path):
        content = load_user(tmp_dir)
        assert "基本信息" in content
        assert "工作习惯" in content

    def test_load_user_preserves_existing(self, tmp_dir: Path):
        (tmp_dir / "USER.md").write_text("I am a developer", encoding="utf-8")
        content = load_user(tmp_dir)
        assert content == "I am a developer"

    def test_save_and_load_roundtrip(self, tmp_dir: Path):
        save_user(tmp_dir, "test user")
        assert load_user(tmp_dir) == "test user"

    def test_reset_user_restores_default(self, tmp_dir: Path):
        save_user(tmp_dir, "modified")
        reset_user(tmp_dir)
        content = load_user(tmp_dir)
        assert "基本信息" in content


# ============================================================
# PROJECT MEMORY
# ============================================================


class TestProjectMemory:
    def test_list_projects_empty(self, tmp_dir: Path):
        projects = list_projects(tmp_dir)
        assert projects == []

    def test_list_projects_with_projects(self, tmp_dir: Path):
        (tmp_dir / "projects" / "my-project").mkdir(parents=True)
        (tmp_dir / "projects" / "my-project" / "notes.md").write_text("notes", encoding="utf-8")
        projects = list_projects(tmp_dir)
        assert "my-project" in projects

    def test_write_and_read_project_file(self, tmp_dir: Path):
        write_project_file(tmp_dir, "test-proj", "notes.md", "hello")
        content = read_project_file(tmp_dir, "test-proj", "notes.md")
        assert content == "hello"

    def test_read_nonexistent_project_file(self, tmp_dir: Path):
        result = read_project_file(tmp_dir, "no-proj", "no-file.md")
        assert result == ""

    def test_delete_project_file(self, tmp_dir: Path):
        write_project_file(tmp_dir, "test-proj", "notes.md", "hello")
        delete_project_file(tmp_dir, "test-proj", "notes.md")
        result = read_project_file(tmp_dir, "test-proj", "notes.md")
        assert result == ""

    def test_load_project_memory(self, tmp_dir: Path):
        write_project_file(tmp_dir, "my-proj", "context.md", "context data")
        write_project_file(tmp_dir, "my-proj", "notes.md", "notes data")
        memory = load_project_memory(tmp_dir, "my-proj")
        assert "context.md" in memory
        assert "notes.md" in memory
        assert memory["context.md"] == "context data"

    def test_format_project_memory(self, tmp_dir: Path):
        write_project_file(tmp_dir, "my-proj", "context.md", "project context")
        result = format_project_memory(tmp_dir, "my-proj")
        assert "my-proj" in result
        assert "project context" in result

    def test_format_empty_project_memory(self, tmp_dir: Path):
        result = format_project_memory(tmp_dir, "no-proj")
        assert result == ""


# ============================================================
# MEMORY MANAGER
# ============================================================


class TestMemoryManager:
    def test_build_system_prompt_includes_soul_and_user(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        prompt = mgr.build_system_prompt()
        assert "DeskMate" in prompt
        assert "基本信息" in prompt

    def test_prefetch_returns_empty_when_no_memory(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        result = mgr.prefetch("hello")
        assert result == ""

    def test_prefetch_returns_relevant_content(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好\n用户喜欢Python编程\n\n§ 工作\n用户在做AI项目",
            encoding="utf-8",
        )
        result = mgr.prefetch("Python")
        assert "Python" in result

    def test_prefetch_truncates_long_content(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr(
            "deskmate.memory.manager.MEMORY_CONTEXT_MAX_CHARS", 200
        )
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text("§ 大段记忆\n" + "x" * 1000, encoding="utf-8")
        result = mgr.prefetch("记忆")
        # With limit 200, result should be <memory-context> + ~200 chars + </memory-context>
        assert len(result) < 400
        assert "…" in result

    def test_write_and_read_memory(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.write_memory("soul", "test soul content")
        content = mgr.read_memory("soul")
        assert "test soul content" in content

    def test_write_memory_replaces_existing(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.write_memory("soul", "first version")
        mgr.write_memory("soul", "second version")
        content = mgr.read_memory("soul")
        assert "second version" in content
        assert "first version" not in content

    def test_reset_memory_restores_default(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.write_memory("soul", "modified soul")
        mgr.reset_memory("soul")
        content = mgr.read_memory("soul")
        assert "DeskMate" in content

    def test_set_active_project(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        write_project_file(tmp_dir, "my-proj", "notes.md", "project notes")
        mgr.set_active_project("my-proj")
        assert mgr.active_project == "my-proj"

    def test_build_system_prompt_includes_project_when_active(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        write_project_file(tmp_dir, "my-proj", "notes.md", "important context")
        mgr.set_active_project("my-proj")
        prompt = mgr.build_system_prompt()
        assert "important context" in prompt

    def test_search_memory_finds_keywords(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好\n用户喜欢Python编程\n\n§ 工作\n用户在做AI项目",
            encoding="utf-8",
        )
        results = mgr._search_global_memory("Python")
        assert "Python" in results

    def test_search_memory_returns_empty_for_no_match(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好\n用户喜欢Python编程",
            encoding="utf-8",
        )
        results = mgr._search_global_memory("JavaScript")
        assert results == ""


# ============================================================
# § 分节 + #标签
# ============================================================


class TestSectionedMemory:
    def test_tag_filter_returns_matching_section(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好 #preference\n用户喜欢Python编程\n\n"
            "§ 纠正 #correction\n用户纠正我张三李四关系",
            encoding="utf-8",
        )
        pref = mgr._search_global_memory("tag:preference")
        assert "Python" in pref
        assert "张三" not in pref

    def test_tag_filter_correction(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好 #preference\n用户喜欢Python\n\n"
            "§ 纠正 #correction\n用户纠正我张三李四关系",
            encoding="utf-8",
        )
        pref = mgr._search_global_memory("tag:correction")
        assert "张三" in pref
        assert "Python" not in pref

    def test_tag_filter_no_match_returns_empty(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好 #preference\n用户喜欢Python",
            encoding="utf-8",
        )
        assert mgr._search_global_memory("tag:work") == ""

    def test_keyword_search_works_without_tags(self, tmp_dir: Path):
        """向后兼容：无标签分节仍可按关键词命中。"""
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好\n用户喜欢Python编程\n\n§ 工作\n用户在做AI项目",
            encoding="utf-8",
        )
        assert "Python" in mgr._search_global_memory("Python")
        assert mgr._search_global_memory("JavaScript") == ""


# ============================================================
# 智能 memory_append（§ 路由）
# ============================================================


class TestSmartAppend:
    def test_append_to_existing_section_by_target(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好 #preference\n用户喜欢Python",
            encoding="utf-8",
        )
        mgr.append_to_section("memory", "用户喜欢简洁回复", target_section="喜好")
        mem = (tmp_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "§ 喜好" in mem
        assert "用户喜欢简洁回复" in mem
        assert "用户喜欢Python" in mem  # 原内容保留

    def test_append_with_leading_tag_routes_and_strips(self, tmp_dir: Path):
        """entry 首行带 #correction → 归入 § 纠正，且前缀被去掉避免重复。"""
        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.append_to_section("memory", "#correction 用户纠正我张三李四关系")
        mem = (tmp_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "§ 纠正 #correction" in mem
        assert "用户纠正我张三李四关系" in mem
        # 前缀 #correction 不应作为正文残留
        assert mem.count("#correction") == 1

    def test_append_creates_new_section_when_unknown(self, tmp_dir: Path):
        """无标签、无 LLM、无匹配标题 → 落到**兜底分节**（不报错、内容不丢）。

        ⚠ 断言在 2026-09-26 改过：旧值锁的是「默认 § 笔记」。但 `#note` 的 TTL
        是 7 天**直接删除且不落 vault**——而这条兜底恰恰只在 LLM 不可用 / 超时 /
        返回跑偏时生效，等于「最不确定的环境做最不可逆的动作」。
        现在锁新价值：兜底必须落**不衰减**的分节（宁留不误删）。
        """
        import deskmate.memory.manager as M

        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.append_to_section("memory", "一条零散笔记")
        mem = (tmp_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "§ 长期事实 #stable" in mem, "兜底分节必须是不衰减的（旧的 § 笔记 7 天即删）"
        assert "一条零散笔记" in mem
        assert M.MEMORY_DECAY_TTL.get("stable") is None, "兜底所在分节绝不能被登记进 TTL"

    def test_append_to_user_profile(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        mgr.append_to_section("user", "用户喜欢用VS Code")
        user = (tmp_dir / "USER.md").read_text(encoding="utf-8")
        assert "用户喜欢用VS Code" in user

    def test_append_never_writes_soul(self, tmp_dir: Path):
        """🛡️ 护栏：任何 memory append 都不得改动 SOUL.md。"""
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "SOUL.md").write_text("SENTINEL-SOUL", encoding="utf-8")
        mgr.append_to_section("memory", "#correction 用户纠正我", target_section="纠正")
        mgr.append_to_section("user", "用户偏好X")
        assert (tmp_dir / "SOUL.md").read_text(encoding="utf-8") == "SENTINEL-SOUL"


# ============================================================
# 会话结束抽取（post_session）
# ============================================================


class TestPostSession:
    def test_post_session_skips_without_llm(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr(mgr_mod, "llm_available", lambda: False)
        mgr = DeskMateMemoryManager(tmp_dir)
        data = mgr.post_session_summary("用户说了一句话", "助手回复")
        assert data["written"] == 0
        assert "跳过" in data["notes"]

    def test_post_session_extracts_and_routes(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr(mgr_mod, "memory_llm_available", lambda: True)
        monkeypatch.setattr(
            mgr_mod,
            "chat_memory",
            lambda system, user, **kw: json.dumps(
                [
                    {"category": "preference", "text": "用户喜欢喝咖啡"},
                    {"category": "correction", "text": "张三和李四是敌人不是朋友"},
                ],
                ensure_ascii=False,
            ),
        )
        mgr = DeskMateMemoryManager(tmp_dir)
        # 需要一个会话记录，否则 early-return
        sessions = tmp_dir / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / "100.md").write_text(
            "## User\n张三李四到底是啥关系\n## Assistant\n我再确认下", encoding="utf-8"
        )
        # SOUL 守卫先验
        (tmp_dir / "SOUL.md").write_text("SENTINEL-SOUL", encoding="utf-8")

        result = mgr.post_session_summary("张三李四到底是啥关系", "我再确认下")
        data = result
        assert data["written"] == 2
        user = (tmp_dir / "USER.md").read_text(encoding="utf-8")
        mem = (tmp_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "咖啡" in user
        assert "§ 纠正 #correction" in mem
        assert "敌人不是朋友" in mem
        # 护栏：SOUL 未被写入
        assert (tmp_dir / "SOUL.md").read_text(encoding="utf-8") == "SENTINEL-SOUL"

    def test_post_session_fail_safe_on_bad_llm(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr(mgr_mod, "memory_llm_available", lambda: True)
        monkeypatch.setattr(mgr_mod, "chat_memory", lambda system, user, **kw: "不是合法JSON")
        mgr = DeskMateMemoryManager(tmp_dir)
        sessions = tmp_dir / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / "1.md").write_text("## User\nhi\n## Assistant\nhello", encoding="utf-8")
        result = mgr.post_session_summary("hi", "hello")
        data = result
        assert data["written"] == 0
        # 失败安全：异常 JSON 不应崩溃，应安全结束（且未写入任何事实）
        assert "归档" in data["notes"]


# ============================================================
# 每轮自动注入（P3 opt-in）
# ============================================================


class TestPerTurnInjection:
    def test_build_full_context_combines_memory_and_user(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text(
            "§ 喜好 #preference\n用户喜欢Python", encoding="utf-8"
        )
        (tmp_dir / "USER.md").write_text("# 用户画像\n- 历史小说家", encoding="utf-8")
        ctx = mgr.build_full_context_for_turn("Python")
        assert "<memory-context>" in ctx
        assert "用户画像" in ctx
        assert "Python" in ctx

    def test_auto_inject_on_by_default(self, tmp_dir: Path):
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text("§ 喜好\n用户喜欢Python", encoding="utf-8")
        out = mgr.deskmate_auto_inject_context("Python")
        assert "<memory-context>" in out
        assert "Python" in out

    def test_auto_inject_opt_out_via_env(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setenv("DESKMATE_AUTO_INJECT", "0")
        mgr = DeskMateMemoryManager(tmp_dir)
        (tmp_dir / "MEMORY.md").write_text("§ 喜好\n用户喜欢Python", encoding="utf-8")
        assert mgr.deskmate_auto_inject_context("Python") == ""


def test_extract_keywords_handles_book_title_marks_and_bigram():
    """分词补丁回归：书名号《》应被切分、长中文词补 CJK bigram，使『《兰陵王传》』能命中记忆里的『兰陵王』。"""
    kws = DeskMateMemoryManager._extract_keywords("《大秦宣太后》《兰陵王传》等")
    joined = "".join(kws)
    assert "兰陵" in kws, f"应补出 bigram 兰陵，实际: {kws}"
    assert "兰陵王传" in kws or "兰陵王" in kws, f"长词应保留/拆解，实际: {kws}"
    assert "《" not in joined and "》" not in joined, f"书名号不应残留成 token，实际: {kws}"
    # 普通自然语言查询仍能产出关键词
    kws2 = DeskMateMemoryManager._extract_keywords("我早期作品和现在作品的写作风格有什么区别")
    assert any("早期" in k or "风格" in k for k in kws2), f"应保留语义词，实际: {kws2}"
