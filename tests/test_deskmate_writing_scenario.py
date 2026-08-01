"""Tests for DeskMate writing scenario — end-to-end integration across modules.

Covers:
- Novel scanning + bible parsing + relationship extraction
- Memory write/read via project_memory
- File change detection via diff_changes
- Cross-chapter recall via novel_recall
- Consistency checking via check_consistency
- Confidence evaluation via evaluate_action
- Anti-disturb behavior in writing scenarios
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deskmate.novel_memory.core import (
    scan_novel,
    _parse_bible_docs,
    inject_bible,
    read_bible,
    novel_recall,
    novel_relationships,
    check_consistency,
    _extract_relationship_pairs,
)
from deskmate.memory.project_memory import (
    write_project_file,
    read_project_file,
    load_project_memory,
    format_project_memory,
    list_projects,
)
from deskmate.perception.diff_changes import (
    diff_files,
    diff_text,
    summarize_changes,
)
from deskmate.proactivity.confidence import (
    ActionType,
    evaluate_action,
    THRESHOLD_SUGGEST,
    THRESHOLD_PREPARE,
)
from deskmate.proactivity.disturb import DisturbManager
from deskmate.proactivity.judge import _JUDGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def novel_folder(tmp_path: Path) -> Path:
    """Create a temporary novel folder with several chapter files."""
    novel = tmp_path / "my_novel"
    novel.mkdir()

    # Chapter 1 — character introductions
    (novel / "ch01.txt").write_text(
        "第一章 相遇\n\n"
        "张三是一个年轻的剑客，性格刚毅。\n"
        "李四是一个商人，性格圆滑。\n"
        "两人在客栈中相遇，张三救了李四一命。\n"
        "从此两人成为好友，结伴同行。\n",
        encoding="utf-8",
    )

    # Chapter 2 — conflict development
    (novel / "ch02.txt").write_text(
        "第二章 结盟\n\n"
        "张三和李四来到了青云山。\n"
        "王五是青云山的掌门，与张三的师父有旧怨。\n"
        "张三向王五行礼，王五冷眼相对。\n"
        "李四从中斡旋，暂时化解了紧张。\n",
        encoding="utf-8",
    )

    # Chapter 3 — turning point
    (novel / "ch03.txt").write_text(
        "第三章 对峙\n\n"
        "王五终于说出了真相：张三的师父曾背叛了青云山。\n"
        "张三不愿相信，但证据确凿。\n"
        "李四劝张三冷静，但张三拔剑指向王五。\n"
        "三人关系陷入僵局。\n",
        encoding="utf-8",
    )

    return novel


@pytest.fixture
def novel_folder_with_bible(novel_folder: Path, tmp_path: Path) -> Path:
    """Novel folder with pre-built bible (no LLM needed)."""
    bible_dir = novel_folder / ".deskmate"
    bible_dir.mkdir()

    (bible_dir / "CHARACTERS.md").write_text(
        "## CHARACTERS\n"
        "- 张三：主角，年轻剑客，性格刚毅。与李四友好（好友，结伴同行）。\n"
        "- 李四：商人，性格圆滑。与张三友好。从中斡旋调解。\n"
        "- 王五：青云山掌门。与张三的师父有旧怨，与张三对立。\n",
        encoding="utf-8",
    )
    (bible_dir / "OUTLINE.md").write_text(
        "## OUTLINE\n"
        "- 第一章：张三与李四在客栈相遇，成为好友。\n"
        "- 第二章：三人来到青云山，紧张关系初现。\n"
        "- 第三章：真相揭露，三人关系僵局。\n",
        encoding="utf-8",
    )
    (bible_dir / "TIMELINE.md").write_text(
        "## TIMELINE\n"
        "- 第一天：张三与李四客栈相遇。\n"
        "- 第二天：到达青云山。\n"
        "- 第三天：王五揭露真相。\n",
        encoding="utf-8",
    )
    (bible_dir / "NOTES.md").write_text(
        "## NOTES\n"
        "- 青云山：江湖门派，掌门王五。\n"
        "- 伏笔：张三师父的背叛尚未详述，后续需展开。\n",
        encoding="utf-8",
    )
    return novel_folder


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Create a memory base directory with projects structure."""
    (tmp_path / "projects").mkdir()
    return tmp_path


# ===========================================================================
# (a) Novel Analysis — scan + parse bible
# ===========================================================================


class TestNovelAnalysis:
    """Test scanning novel files and parsing bible documents."""

    def test_scan_novel_reads_all_chapters(self, novel_folder: Path):
        text, titles = scan_novel(str(novel_folder))
        assert "张三" in text
        assert "李四" in text
        assert "王五" in text
        assert len(titles) == 3
        assert "ch01.txt" in titles

    def test_scan_novel_preserves_chapter_order(self, novel_folder: Path):
        text, titles = scan_novel(str(novel_folder))
        # Chapters should be in sorted order
        idx_ch01 = text.index("第一章")
        idx_ch02 = text.index("第二章")
        idx_ch03 = text.index("第三章")
        assert idx_ch01 < idx_ch02 < idx_ch03

    def test_parse_bible_docs_from_llm_output(self):
        """Simulate LLM output and parse into docs."""
        llm_output = (
            "===== CHARACTERS =====\n"
            "## CHARACTERS\n"
            "- 张三：主角，与李四友好，与王五对立\n"
            "- 李四：商人，与张三友好\n"
            "- 王五：掌门，与张三对立\n"
            "\n===== OUTLINE =====\n"
            "## OUTLINE\n"
            "三章：相遇-结盟-对峙\n"
            "\n===== TIMELINE =====\n"
            "## TIMELINE\n"
            "三天时间跨度\n"
            "\n===== NOTES =====\n"
            "## NOTES\n"
            "青云山门派设定"
        )
        docs = _parse_bible_docs(llm_output)
        assert len(docs) == 4
        assert "张三" in docs["CHARACTERS"]
        assert "王五" in docs["CHARACTERS"]
        assert "青云山" in docs["NOTES"]

    def test_inject_and_read_bible_roundtrip(self, novel_folder: Path, tmp_path: Path):
        """Inject bible docs and read them back."""
        docs = {
            "CHARACTERS": "## CHARACTERS\n- 张三：主角\n- 李四：好友",
            "OUTLINE": "## OUTLINE\n三章故事",
            "TIMELINE": "## TIMELINE\n三天",
            "NOTES": "## NOTES\n世界观",
        }

        import deskmate.novel_memory.core as mod
        db_path = tmp_path / "test.db"
        original = mod._db_path
        mod._db_path = lambda: db_path
        try:
            written = inject_bible(str(novel_folder), docs)
            assert written == 4

            loaded = read_bible(str(novel_folder))
            assert "CHARACTERS" in loaded
            assert "张三" in loaded["CHARACTERS"]
            assert "OUTLINE" in loaded
        finally:
            mod._db_path = original


# ===========================================================================
# (b) Memory Write — save character relationships to project memory
# ===========================================================================


class TestMemoryWrite:
    """Test writing character relationships and analysis results to project memory."""

    def test_write_character_relationships(self, memory_dir: Path):
        relationships = (
            "## 人物关系\n"
            "- 张三 ↔ 李四：友好（好友，结伴同行）\n"
            "- 张三 ↔ 王五：对立（师门旧怨）\n"
            "- 李四 ↔ 王五：中立（从中斡旋）\n"
        )
        write_project_file(memory_dir, "my_novel", "characters.md", relationships)

        content = read_project_file(memory_dir, "my_novel", "characters.md")
        assert "张三" in content
        assert "友好" in content
        assert "对立" in content

    def test_write_and_read_multiple_files(self, memory_dir: Path):
        write_project_file(memory_dir, "my_novel", "characters.md", "人物关系")
        write_project_file(memory_dir, "my_novel", "outline.md", "大纲")
        write_project_file(memory_dir, "my_novel", "timeline.md", "时间线")

        memory = load_project_memory(memory_dir, "my_novel")
        assert len(memory) == 3
        assert "characters.md" in memory
        assert "outline.md" in memory

    def test_project_appears_in_listing(self, memory_dir: Path):
        write_project_file(memory_dir, "my_novel", "notes.md", "笔记")
        projects = list_projects(memory_dir)
        assert "my_novel" in projects

    def test_format_project_memory_for_injection(self, memory_dir: Path):
        write_project_file(
            memory_dir, "my_novel", "characters.md",
            "- 张三：主角\n- 李四：好友"
        )
        formatted = format_project_memory(memory_dir, "my_novel")
        assert "my_novel" in formatted
        assert "张三" in formatted

    def test_overwrite_existing_memory(self, memory_dir: Path):
        write_project_file(memory_dir, "my_novel", "characters.md", "v1 关系")
        write_project_file(memory_dir, "my_novel", "characters.md", "v2 更新后的关系")
        content = read_project_file(memory_dir, "my_novel", "characters.md")
        assert "v2" in content
        assert "v1" not in content


# ===========================================================================
# (c) Perception Simulation — file change detection
# ===========================================================================


class TestPerceptionSimulation:
    """Test detecting file changes when a new chapter is added."""

    def test_diff_detects_new_chapter_content(self, novel_folder: Path):
        old_text = (novel_folder / "ch01.txt").read_text(encoding="utf-8")
        new_text = old_text + "\n\n新的一幕：张三开始练习剑法。\n"
        diff = diff_text(old_text, new_text, label="ch01变更")
        assert "练习剑法" in diff
        assert "ch01变更" in diff

    def test_summarize_changes_counts_lines(self, novel_folder: Path):
        old_text = (novel_folder / "ch01.txt").read_text(encoding="utf-8")
        new_text = old_text + "\n新增一行\n又新增一行\n"
        stats = summarize_changes(old_text, new_text)
        assert stats["added"] >= 2
        assert stats["total_new"] > stats["total_old"]

    def test_diff_files_between_versions(self, tmp_path: Path):
        old_file = tmp_path / "ch01_v1.txt"
        new_file = tmp_path / "ch01_v2.txt"
        old_file.write_text(
            "张三是一个剑客。\n李四是一个商人。\n",
            encoding="utf-8",
        )
        new_file.write_text(
            "张三是一个剑客。\n李四是一个商人。\n王五出现了。\n",
            encoding="utf-8",
        )
        diff = diff_files(str(old_file), str(new_file))
        assert "王五" in diff

    def test_diff_no_changes_returns_same_message(self, novel_folder: Path):
        text = (novel_folder / "ch01.txt").read_text(encoding="utf-8")
        result = diff_text(text, text)
        assert "无变更" in result


# ===========================================================================
# (d) Recall Verification — cross-chapter information retrieval
# ===========================================================================


class TestRecallVerification:
    """Test cross-chapter recall for writing assistance."""

    def test_recall_finds_character_across_chapters(self, novel_folder_with_bible: Path):
        result = novel_recall("张三 剑客", str(novel_folder_with_bible))
        assert "张三" in result
        assert "人物圣经摘要" in result

    def test_recall_returns_bible_context(self, novel_folder_with_bible: Path):
        result = novel_recall("王五 青云山", str(novel_folder_with_bible))
        assert "王五" in result
        # Should include bible summary
        assert "CHARACTERS" in result or "掌门" in result

    def test_recall_multi_keyword_ranking(self, novel_folder_with_bible: Path):
        result = novel_recall("张三 李四 客栈", str(novel_folder_with_bible))
        # Both characters and location should be found
        assert "张三" in result
        assert "李四" in result

    def test_recall_finds_foreshadowing(self, novel_folder_with_bible: Path):
        result = novel_recall("师父 背叛", str(novel_folder_with_bible))
        assert "师父" in result

    def test_novel_relationships_query(self, novel_folder_with_bible: Path):
        result = novel_relationships("张三", str(novel_folder_with_bible))
        assert "张三" in result
        # Should mention relationship with 李四 or 王五
        assert "李四" in result or "王五" in result

    def test_novel_relationships_not_found(self, novel_folder_with_bible: Path):
        result = novel_relationships("赵六", str(novel_folder_with_bible))
        assert "未直接提及" in result


# ===========================================================================
# (e) Consistency Check — contradiction detection
# ===========================================================================


class TestConsistencyCheck:
    """Test check_consistency for detecting contradictions."""

    def test_consistent_text_passes(self, novel_folder_with_bible: Path):
        bible = read_bible(str(novel_folder_with_bible))
        # New text consistent with bible: 张三 and 李四 are friends
        new_text = "张三和李四一起喝酒，庆祝结盟成功。"
        result = check_consistency(bible, new_text)
        assert result["consistent"] is True
        assert len(result["issues"]) == 0

    def test_contradictory_text_detected(self, novel_folder_with_bible: Path):
        bible = read_bible(str(novel_folder_with_bible))
        # Bible says 张三 and 李四 are friendly; new text says they are enemies
        new_text = "张三和李四在客栈对峙，两人已成敌人，张三恨李四入骨。"
        result = check_consistency(bible, new_text)
        assert result["consistent"] is False
        assert len(result["issues"]) > 0
        assert any("矛盾" in issue for issue in result["issues"])

    def test_consistency_with_empty_bible(self):
        result = check_consistency({}, "任何文本")
        assert result["consistent"] is True
        assert result["checked_pairs"] == 0

    def test_consistency_only_checks_shared_sentences(self, novel_folder_with_bible: Path):
        bible = read_bible(str(novel_folder_with_bible))
        # Only mentions one character — no pair to check
        new_text = "张三独自练剑，心事重重。"
        result = check_consistency(bible, new_text)
        # No pairs both mentioned in same sentence
        assert result["consistent"] is True

    def test_extract_relationship_pairs(self):
        text = (
            "## CHARACTERS\n"
            "- 张三：主角，与李四友好\n"
            "- 王五：掌门，与张三对立\n"
        )
        pairs = _extract_relationship_pairs(text)
        # Should find at least 张三-李四 and 张三-王五
        key_zl = tuple(sorted(["张三", "李四"]))
        key_zw = tuple(sorted(["张三", "王五"]))
        assert key_zl in pairs
        assert key_zw in pairs


# ===========================================================================
# (f) Confidence Evaluation — writing scenario
# ===========================================================================


class TestConfidenceEvaluation:
    """Test confidence evaluation for writing scenario suggestions."""

    def test_high_confidence_contradiction_suggests(self):
        """Clear contradiction should trigger SUGGEST."""
        decision = evaluate_action(0.9, reason="发现人物关系明显矛盾")
        assert decision.action == ActionType.SUGGEST
        assert decision.confidence == 0.9
        assert "矛盾" in decision.reason

    def test_medium_confidence_possible_issue_prepares(self):
        """Possible issue should trigger PREPARE (quiet notification)."""
        decision = evaluate_action(0.6, reason="疑似时间线不一致")
        assert decision.action == ActionType.PREPARE

    def test_low_confidence_no_action(self):
        """Uncertain observation should not disturb user."""
        decision = evaluate_action(0.3, reason="信息不足，无法判断")
        assert decision.action == ActionType.NO_ACTION

    def test_writing_contradiction_high_score(self):
        """Simulate: strong contradiction found → high confidence → SUGGEST."""
        # 3 issues found → should be high confidence
        num_issues = 3
        confidence = min(1.0, 0.5 + num_issues * 0.15)
        decision = evaluate_action(confidence, reason=f"发现{num_issues}处人物关系矛盾")
        assert decision.action == ActionType.SUGGEST

    def test_writing_consistent_low_score(self):
        """Simulate: no issues found → should not suggest."""
        decision = evaluate_action(0.1, reason="文本一致，无矛盾")
        assert decision.action == ActionType.NO_ACTION


# ===========================================================================
# (g) Anti-Disturb Mechanism — writing scenario
# ===========================================================================


class TestAntiDisturbWritingScenario:
    """Test that anti-disturb mechanism works correctly in writing scenarios."""

    def test_first_contradiction_suggestion_allowed(self):
        mgr = DisturbManager()
        assert mgr.should_allow("plot_contradiction", "张三李四矛盾")

    def test_same_contradiction_blocked_in_cooldown(self):
        mgr = DisturbManager()
        mgr.record_suggestion("plot_contradiction", "张三李四矛盾")
        # Same key blocked within 30min cooldown
        assert not mgr.should_allow("plot_contradiction", "张三李四矛盾")
        # Different contradiction allowed
        assert mgr.should_allow("plot_contradiction", "王五时间线矛盾")

    def test_three_consecutive_ignores_blocks_type(self):
        mgr = DisturbManager()
        # User ignores 3 contradiction suggestions
        mgr.record_suggestion("plot_contradiction", "issue1", accepted=False)
        mgr.record_suggestion("plot_contradiction", "issue2", accepted=False)
        mgr.record_suggestion("plot_contradiction", "issue3", accepted=False)
        # 4th contradiction of same type should be blocked
        assert not mgr.should_allow("plot_contradiction", "issue4")
        # But a different type (e.g., missing_foreshadow) should still be allowed
        assert mgr.should_allow("missing_foreshadow", "伏笔1")

    def test_accept_resets_ignore_counter(self):
        mgr = DisturbManager()
        mgr.record_suggestion("plot_contradiction", "issue1", accepted=False)
        mgr.record_suggestion("plot_contradiction", "issue2", accepted=False)
        mgr.record_suggestion("plot_contradiction", "issue3", accepted=True)  # accepted → reset
        mgr.record_suggestion("plot_contradiction", "issue4", accepted=False)
        # Only 1 consecutive ignore after accept, should allow
        assert mgr.should_allow("plot_contradiction", "issue5")

    def test_silent_mode_during_writing(self):
        """User says 'don't disturb me while writing' → silent mode."""
        mgr = DisturbManager()
        mgr.set_silent_mode(True)
        # Nothing should be allowed
        assert not mgr.should_allow("plot_contradiction", "any_issue")
        assert not mgr.should_allow("missing_foreshadow", "any_foreshadow")
        assert not mgr.should_allow("character_inconsistency", "any_char")
        # Turn off silent mode
        mgr.set_silent_mode(False)
        assert mgr.should_allow("plot_contradiction", "new_issue")

    def test_different_suggestion_types_independent(self):
        """Ignore count for one type should not affect another."""
        mgr = DisturbManager()
        # 3 ignores for contradiction type
        mgr.record_suggestion("plot_contradiction", "c1", accepted=False)
        mgr.record_suggestion("plot_contradiction", "c2", accepted=False)
        mgr.record_suggestion("plot_contradiction", "c3", accepted=False)
        assert not mgr.should_allow("plot_contradiction", "c4")
        # But foreshadow type should be unaffected
        assert mgr.should_allow("missing_foreshadow", "f1")

    def test_persist_writing_suggestions(self, tmp_path: Path):
        """Writing suggestion records should survive persistence."""
        path = tmp_path / "disturb.json"
        mgr1 = DisturbManager()
        mgr1.persist(path)
        mgr1.record_suggestion("character_inconsistency", "张三性格矛盾", accepted=False)
        mgr1.record_suggestion("plot_contradiction", "时间线错乱", accepted=True)

        mgr2 = DisturbManager()
        mgr2.persist(path)
        assert len(mgr2.records) == 2
        types = {r.suggestion_type for r in mgr2.records}
        assert "character_inconsistency" in types
        assert "plot_contradiction" in types


# ===========================================================================
# (h) Judge Prompt — writing scenario dimensions
# ===========================================================================


class TestJudgePromptWritingDimensions:
    """Test that the judge prompt contains writing scenario criteria."""

    def test_prompt_contains_plot_contradiction(self):
        assert "情节矛盾" in _JUDGE_SYSTEM_PROMPT

    def test_prompt_contains_character_inconsistency(self):
        assert "角色不一致" in _JUDGE_SYSTEM_PROMPT or "角色" in _JUDGE_SYSTEM_PROMPT

    def test_prompt_contains_foreshadowing(self):
        assert "伏笔" in _JUDGE_SYSTEM_PROMPT

    def test_prompt_contains_writing_scenario_section(self):
        assert "写作场景" in _JUDGE_SYSTEM_PROMPT

    def test_prompt_contains_writing_suggestion_types(self):
        assert "character_inconsistency" in _JUDGE_SYSTEM_PROMPT
        assert "timeline_error" in _JUDGE_SYSTEM_PROMPT


# ===========================================================================
# (i) End-to-End Writing Scenario
# ===========================================================================


class TestEndToEndWritingScenario:
    """Full end-to-end scenario: a writer using DeskMate for novel writing."""

    def test_full_writing_workflow(
        self, novel_folder: Path, memory_dir: Path, tmp_path: Path
    ):
        """Simulate the complete writing-assistance workflow."""

        # --- Step 1: Scan the novel ---
        text, titles = scan_novel(str(novel_folder))
        assert len(titles) == 3
        assert "张三" in text

        # --- Step 2: Parse bible (simulated LLM output) ---
        llm_output = (
            "===== CHARACTERS =====\n## CHARACTERS\n"
            "- 张三：主角，剑客。与李四友好，与王五对立\n"
            "- 李四：商人。与张三友好\n"
            "- 王五：掌门。与张三对立\n"
            "===== OUTLINE =====\n## OUTLINE\n三章故事线\n"
            "===== TIMELINE =====\n## TIMELINE\n三天\n"
            "===== NOTES =====\n## NOTES\n青云山设定"
        )
        docs = _parse_bible_docs(llm_output)
        assert "CHARACTERS" in docs

        # --- Step 3: Save to project memory ---
        write_project_file(memory_dir, "my_novel", "characters.md", docs["CHARACTERS"])
        write_project_file(memory_dir, "my_novel", "outline.md", docs["OUTLINE"])

        saved_chars = read_project_file(memory_dir, "my_novel", "characters.md")
        assert "张三" in saved_chars
        assert "友好" in saved_chars

        # --- Step 4: Inject bible to novel folder (simulated) ---
        import deskmate.novel_memory.core as mod
        db_path = tmp_path / "test.db"
        original = mod._db_path
        mod._db_path = lambda: db_path
        try:
            inject_bible(str(novel_folder), docs)
            loaded = read_bible(str(novel_folder))
            assert "CHARACTERS" in loaded
        finally:
            mod._db_path = original

        # --- Step 5: Simulate new chapter being added ---
        old_ch01 = (novel_folder / "ch01.txt").read_text(encoding="utf-8")
        new_ch01 = old_ch01 + "\n张三和李四闹翻了，两人成为敌人。\n"

        # --- Step 6: Detect changes via diff ---
        diff = diff_text(old_ch01, new_ch01, label="ch01")
        assert "闹翻" in diff
        stats = summarize_changes(old_ch01, new_ch01)
        assert stats["added"] >= 1

        # --- Step 7: Check consistency against bible ---
        consistency = check_consistency(loaded, new_ch01)
        # This should detect that 张三 and 李四 are now enemies, contradicting "友好"
        assert consistency["consistent"] is False
        assert len(consistency["issues"]) > 0

        # --- Step 8: Evaluate confidence ---
        confidence = min(1.0, 0.5 + len(consistency["issues"]) * 0.15)
        decision = evaluate_action(confidence, reason=f"发现{len(consistency['issues'])}处矛盾")
        assert decision.action in (ActionType.SUGGEST, ActionType.PREPARE)

        # --- Step 9: Check disturb manager allows the suggestion ---
        mgr = DisturbManager()
        assert mgr.should_allow("plot_contradiction", "张三李四关系矛盾")

    def test_recall_after_bible_injection(self, novel_folder_with_bible: Path):
        """After bible is injected, recall should return rich context."""
        result = novel_recall("张三 李四 关系", str(novel_folder_with_bible))
        # Should have bible summary
        assert "人物圣经摘要" in result
        # Should have character info
        assert "张三" in result
        # Should have text hits
        assert "正文命中片段" in result or "客栈" in result or "结伴" in result

    def test_consistency_clean_chapter_passes(self, novel_folder_with_bible: Path):
        """A new chapter that doesn't contradict should pass consistency check."""
        bible = read_bible(str(novel_folder_with_bible))
        new_chapter = (
            "第四章 出发\n\n"
            "张三和李四告别了青云山，继续上路。\n"
            "两人一路上有说有笑，关系更近了一步。\n"
        )
        result = check_consistency(bible, new_chapter)
        assert result["consistent"] is True

    def test_consistency_contradictory_chapter_fails(self, novel_folder_with_bible: Path):
        """A new chapter that contradicts bible relationships should fail."""
        bible = read_bible(str(novel_folder_with_bible))
        # 张三 and 李四 are friends in bible, but this chapter makes them enemies
        new_chapter = (
            "第四章 反目\n\n"
            "张三拔剑指向李四，两人成为敌人。\n"
            "李四冷笑着说：从今以后，我们势不两立。\n"
        )
        result = check_consistency(bible, new_chapter)
        assert result["consistent"] is False
        assert len(result["issues"]) > 0
