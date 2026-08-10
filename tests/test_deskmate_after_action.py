"""全局 after-action 记忆钩子测试。

不依赖真实书稿 / LLM。用 tmp_path 模拟 base_dir，monkeypatch 拦截圣经刷新调用。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.memory import after_action as aa
from deskmate.memory import project_memory as pm


def test_project_journal_written_and_injectable(tmp_path: Path) -> None:
    """记一笔项目日志，且能经 format_project_memory 注入下轮对话。"""
    ws = str(tmp_path / "mybook")
    aa.reflect_and_persist(
        ws, "帮我看看这段", "这一段描写很好，建议保留。", base_dir=tmp_path
    )
    proj = aa._project_name_for(ws)
    content = pm.read_project_file(tmp_path, proj, "journal.md")
    assert "帮我看看这段" in content
    assert "建议保留" in content
    # 下一轮对话应能从项目记忆注入
    inj = pm.format_project_memory(tmp_path, proj)
    assert "建议保留" in inj


def test_refresh_bible_only_for_built_books(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总目录下只刷新『已建过圣经』的书，未建的绝不擅自全量建。"""
    root = tmp_path / "已出版"
    root.mkdir()
    book_built = root / "1.某书"
    book_built.mkdir()
    (book_built / ".deskmate").mkdir()  # 已建圣经
    book_unbuilt = root / "2.另一本"
    book_unbuilt.mkdir()
    (book_unbuilt / "a.txt").write_text("正文")  # 未建，但含文本

    calls: list[str] = []

    def fake_inc(folder: str):
        calls.append(folder)
        return {"CHARACTERS.md": "x"}

    monkeypatch.setattr(
        "deskmate.novel_memory.core.incremental_bible_update", fake_inc
    )
    aa.refresh_bible_if_novel(str(root))
    assert str(book_built) in calls
    assert str(book_unbuilt) not in calls  # 未建的被跳过（守红线）


def test_single_book_direct_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单本书（含 .deskmate）直接增量刷新。"""
    book = tmp_path / "book"
    book.mkdir()
    (book / ".deskmate").mkdir()
    calls: list[str] = []
    monkeypatch.setattr(
        "deskmate.novel_memory.core.incremental_bible_update",
        lambda f: (calls.append(f) or {"x": "y"}),
    )
    aa.refresh_bible_if_novel(str(book))
    assert calls == [str(book)]


def test_no_bible_root_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通项目目录（非小说）不触发任何圣经刷新。"""
    plain = tmp_path / "project"
    plain.mkdir()
    (plain / "notes.txt").write_text("hello")
    calls: list[str] = []
    monkeypatch.setattr(
        "deskmate.novel_memory.core.incremental_bible_update",
        lambda f: (calls.append(f) or {"x": "y"}),
    )
    aa.refresh_bible_if_novel(str(plain))
    assert calls == []


def test_prevent_spam_short_message(tmp_path: Path) -> None:
    """防刷屏：极短且无产物 / 习得时跳过项目日志，但仍刷新圣经。"""
    ws = str(tmp_path / "x")
    aa.reflect_and_persist(ws, "hi", "谢谢", base_dir=tmp_path)
    proj = aa._project_name_for(ws)
    content = pm.read_project_file(tmp_path, proj, "journal.md")
    assert content == ""  # 跳过日志


def test_learned_facts_recorded(tmp_path: Path) -> None:
    """有习得要点时即使助手输出短也应记一笔（含习得要点）。"""
    ws = str(tmp_path / "y")
    aa.reflect_and_persist(
        ws,
        "查证靖难之役",
        "已查",
        learned=["靖难之役是朱棣夺位之战"],
        base_dir=tmp_path,
    )
    proj = aa._project_name_for(ws)
    content = pm.read_project_file(tmp_path, proj, "journal.md")
    assert "靖难之役是朱棣夺位之战" in content
