"""圣典自动写入退役契约（2026-11-20 萧哥拍板）。

背景：写稿从不读圣典，错字/疑笔在回复里直报后作者直接改稿。自动写圣典
= 空转 + 污染（对话产出覆盖正文人物）+ 耗 LLM，故停用一切**自动**写入口。

保留：显式工具（novel_memory/server.py 的 build_bible / refresh_bible），
只有作者主动点名时才跑；底层函数本身不删，便于按 git 历史复活。

本文件锁的是「自动入口不得再触发写圣典」，不是「圣典功能不存在」。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.memory import after_action as aa

_REPO = Path(__file__).resolve().parents[1]


def test_reflect_and_persist_never_refreshes_bible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对话后钩子（文件侧）不得再触发 incremental_bible_update。"""
    monkeypatch.setattr(aa, "_submit_after_action", lambda base_dir, fn: fn())
    calls: list[str] = []

    def fake_refresh(workspace: str | None) -> None:
        calls.append(str(workspace))

    monkeypatch.setattr(aa, "refresh_bible_if_novel", fake_refresh)
    book = tmp_path / "1.某书"
    book.mkdir()
    (book / ".deskmate").mkdir()
    aa.reflect_and_persist(str(book), "查证一段", "已查证给出结论", base_dir=tmp_path)
    assert calls == [], "after_action 不得自动刷新圣典"


def test_reflect_and_persist_never_reflux_dialogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对话后钩子（内容侧）不得再把对话产出回流进圣典。"""
    monkeypatch.setattr(aa, "_submit_after_action", lambda base_dir, fn: fn())
    calls: list[str] = []

    def fake_reflux(workspace, last_asst, artifacts) -> None:
        calls.append(str(workspace))

    monkeypatch.setattr(aa, "_upsert_content_loop", fake_reflux)
    book = tmp_path / "1.某书"
    book.mkdir()
    (book / ".deskmate").mkdir()
    aa.reflect_and_persist(
        str(book),
        "查证一段",
        "这是一段足够长的助手回复，超过三百字才会触发内容回流门槛，绝对不该再写圣典。",
        base_dir=tmp_path,
    )
    assert calls == [], "after_action 不得自动回流对话产出到圣典"


def test_report_endpoint_has_no_auto_bible_refresh() -> None:
    """报告端点读稿前不得再自动刷新圣典（原 P2-10 线程池调用已退役）。"""
    src = (_REPO / "coworker" / "server" / "app.py").read_text(encoding="utf-8")
    assert "refresh_bible_if_novel" not in src, "报告端点不得自动刷新圣典"


def test_roundtable_has_no_auto_report_reflux() -> None:
    """圆桌收尾不得再把《综合研判报告》自动回流圣典。"""
    src = (_REPO / "deskmate" / "roundtable" / "web.py").read_text(encoding="utf-8")
    # 函数本体保留可复活，但不得再有自动拉起的线程调用点
    assert "target=_upsert_report_to_bible" not in src, "圆桌报告不得自动回流圣典"
