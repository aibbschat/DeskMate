"""Tests for P1-7 无密钥记忆兜底（deskmate/memory/local_extractor.py）。

零 LLM 本地规则抽取：未配 provider 时从对话中提取 preference/correction/work 三类事实，
避免「越用越懂你」静默失效。
"""

from __future__ import annotations

import pytest

from deskmate.memory.local_extractor import (
    _extract_user_messages,
    extract_facts_from_sessions,
    extract_facts_local,
)


def _session(user: str, assistant: str = "好的。") -> str:
    return f"## User\n{user}\n\n## Assistant\n{assistant}\n"


class TestSessionParsing:
    def test_extract_user_section(self):
        text = _session("我喜欢喝咖啡")
        msgs = _extract_user_messages(text)
        assert len(msgs) == 1
        assert "我喜欢喝咖啡" in msgs[0]

    def test_multiple_user_sections(self):
        text = "## User\n第一句\n\n## Assistant\n嗯\n\n## User\n第二句\n"
        msgs = _extract_user_messages(text)
        assert len(msgs) == 2
        assert "第一句" in msgs[0]
        assert "第二句" in msgs[1]

    def test_no_user_section(self):
        text = "## Assistant\n只有助手\n"
        assert _extract_user_messages(text) == []

    def test_empty_text(self):
        assert _extract_user_messages("") == []
        assert extract_facts_local("") == []


class TestPreferenceExtraction:
    def test_like(self):
        facts = extract_facts_local(_session("我喜欢用 VS Code 写代码"))
        assert any("VS Code" in f["text"] for f in facts)
        assert any(f["category"] == "preference" for f in facts)

    def test_habit(self):
        facts = extract_facts_local(_session("我习惯早上写稿"))
        assert any("早上写稿" in f["text"] for f in facts)

    def test_future_habit(self):
        facts = extract_facts_local(_session("以后都用宋体排版"))
        assert any("宋体排版" in f["text"] for f in facts)

    def test_negative_preference(self):
        facts = extract_facts_local(_session("不要用 emoji 表情"))
        assert any("emoji" in f["text"] for f in facts)

    def test_personal_info(self):
        facts = extract_facts_local(_session("我叫张三，来自北京"))
        assert any("张三" in f["text"] for f in facts)
        assert any("北京" in f["text"] for f in facts)

    def test_date_prefix(self):
        facts = extract_facts_local(_session("我喜欢喝茶"), date="2026-08-30")
        assert facts[0]["text"].startswith("2026-08-30: ")

    def test_no_preference_in_plain_text(self):
        facts = extract_facts_local(_session("今天天气不错"))
        assert not any(f["category"] == "preference" for f in facts)


class TestCorrectionExtraction:
    def test_not_correct(self):
        facts = extract_facts_local(_session("不对，应该是宋体不是黑体"))
        assert any(f["category"] == "correction" for f in facts)
        assert any("宋体" in f["text"] for f in facts)

    def test_is_not(self):
        facts = extract_facts_local(_session("不是第三章，是第五章"))
        assert any(f["category"] == "correction" for f in facts)

    def test_should_be(self):
        facts = extract_facts_local(_session("应该是 2026 年不是 2025 年"))
        assert any(f["category"] == "correction" for f in facts)


class TestWorkExtraction:
    def test_book_with_action(self):
        facts = extract_facts_local(_session("《衙探》第二章定稿了"))
        assert any(f["category"] == "work" for f in facts)
        assert any("衙探" in f["text"] for f in facts)

    def test_chapter_completion(self):
        facts = extract_facts_local(_session("第三章写完了"))
        assert any(f["category"] == "work" for f in facts)
        assert any("第三章" in f["text"] for f in facts)

    def test_book_only_weak_signal(self):
        facts = extract_facts_local(_session("我在写《星辰大海》"))
        # 只有书名没有动作 → 弱信号"正在创作"
        assert any("星辰大海" in f["text"] for f in facts)

    def test_no_work(self):
        facts = extract_facts_local(_session("随便聊聊"))
        assert not any(f["category"] == "work" for f in facts)


class TestDedupAndMerge:
    def test_dedup_within_session(self):
        text = "## User\n我喜欢喝咖啡\n\n## Assistant\n嗯\n\n## User\n我喜欢喝咖啡\n"
        facts = extract_facts_local(text)
        coffee_facts = [f for f in facts if "咖啡" in f["text"]]
        assert len(coffee_facts) == 1  # 去重

    def test_multi_session_merge(self):
        s1 = _session("我喜欢喝咖啡")
        s2 = _session("我习惯早起")
        facts = extract_facts_from_sessions([s1, s2])
        assert any("咖啡" in f["text"] for f in facts)
        assert any("早起" in f["text"] for f in facts)

    def test_multi_session_dedup(self):
        s1 = _session("我喜欢喝咖啡")
        s2 = _session("我喜欢喝咖啡")
        facts = extract_facts_from_sessions([s1, s2])
        assert len([f for f in facts if "咖啡" in f["text"]]) == 1


class TestFailOpen:
    def test_malformed_text_no_crash(self):
        # 各种异常输入都不应崩溃
        assert extract_facts_local(None) == []  # type: ignore
        assert extract_facts_local("   ") == []
        assert extract_facts_local("## User\n\n## Assistant\n") == []


class TestManagerIntegration:
    """集成测试：MemoryManager.extract_session_facts 无 provider 时走本地抽取。"""

    def test_extract_without_provider(self, tmp_path, monkeypatch):
        from deskmate.memory.manager import DeskMateMemoryManager as MemoryManager

        # 模拟无 provider
        import deskmate.memory.manager as mgr
        monkeypatch.setattr(mgr, "memory_llm_available", lambda: False)

        mm = MemoryManager(base_dir=tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "s1.md").write_text(
            "## User\n我喜欢用 VS Code\n\n## Assistant\n好的\n",
            encoding="utf-8",
        )
        result = mm.extract_session_facts()
        assert result["written"] >= 1
        # 验证写入了 USER.md
        user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
        assert "VS Code" in user_md

    def test_post_session_summary_without_provider(self, tmp_path, monkeypatch):
        from deskmate.memory.manager import DeskMateMemoryManager as MemoryManager

        import deskmate.memory.manager as mgr
        monkeypatch.setattr(mgr, "memory_llm_available", lambda: False)

        mm = MemoryManager(base_dir=tmp_path)
        result = mm.post_session_summary(
            user_msg="以后都用黑体排版",
            assistant_msg="好的，记住了",
        )
        assert result["written"] >= 1
        user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
        assert "黑体" in user_md
