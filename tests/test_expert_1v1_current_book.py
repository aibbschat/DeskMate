"""1v1「当前书」由宿主挂载，不交给模型自觉（2026-11-09 萧哥拍板 A 案）。

事故复现：catalog._notebook 建的是空 NotebookSession，人设又只写「第一次用带
书名」。实测带过一次 book="夜不收" 后，换聊《国子监秘闻录》不带参就**静默写回
夜不收**（返回值还写着《夜不收》）。根因是状态残留 + 提示词与实现语义打架。

本组锁住：
- 宿主每轮从最后一条 user 消息认书名 → set_book，模型不带 book 也能落到对的书；
- 换书时自动切过去，不再串本；
- 从没认出过书 → fail-closed 要求显式书名，绝不猜；
- 模型显式带 book 仍优先；
- prompt 只给「书名」不给绝对路径（路径是宿主内部实现，外泄会诱导绕守卫读稿）；
- 没有笔记本的 persona 不受影响。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from coworker.agent import build_engine
from coworker.personas.registry import get_registry


class _SilentProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="ok")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn, StreamChunk

        yield StreamChunk(turn=AssistantTurn(text="ok"))


@pytest.fixture()
def shelf_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "books"
    for name in ("16.夜不收", "17.国子监秘闻录"):
        d = root / name
        d.mkdir(parents=True)
        (d / "正文.txt").write_text("第一章。", encoding="utf-8")
    monkeypatch.setenv("DESKMATE_BOT_BOOKS_ROOT", str(root))
    return root


def _engine(persona: str):
    agent = get_registry().get(persona).agent()
    return build_engine(agent=agent, provider=_SilentProvider(), model="m", workspace=None)


def _turn(eng, text: str) -> str:
    """模拟一轮：context_provider 在 LLM 调用前执行。"""
    return eng.context_provider([{"role": "user", "content": text}])


def _notes(root: Path, dirname: str, expert: str = "brainstormer") -> list[str]:
    p = root / dirname / ".deskmate" / "experts" / expert / "notes.md"
    if not p.is_file():
        return []
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- [")]


def test_mount_current_book_from_user_message(shelf_root: Path) -> None:
    """宿主挂载：不带 book 也能落到本轮提到的那本。"""
    eng = _engine("brainstormer")
    _turn(eng, "来聊聊夜不收这一章")
    assert "notebook_remember" in set(eng.registry.names())
    msg = eng.registry.execute("notebook_remember", {"entry": "作者定：龙山仓伏笔"})
    assert "未识别" not in msg
    assert any("龙山仓伏笔" in l for l in _notes(shelf_root, "16.夜不收"))


def test_switch_book_no_longer_writes_to_previous(shelf_root: Path) -> None:
    """换书保护：先带 book 记 A，再聊 B 不带参 → 必须落 B，A 不再新增。"""
    eng = _engine("brainstormer")
    _turn(eng, "接着聊")
    eng.registry.execute(
        "notebook_remember", {"entry": "作者定：水门铁栅", "book": "夜不收"}
    )
    _turn(eng, "《国子监秘闻录》的伏笔帮我捋一下")
    msg = eng.registry.execute("notebook_remember", {"entry": "作者定：元祐党争"})
    assert "未识别" not in msg
    assert any("元祐党争" in l for l in _notes(shelf_root, "17.国子监秘闻录"))
    assert not any("元祐党争" in l for l in _notes(shelf_root, "16.夜不收"))


def test_never_guesses_book_without_any_signal(shelf_root: Path) -> None:
    """从没认出过书 → fail-closed；显式带书名才放行。"""
    eng = _engine("brainstormer")
    _turn(eng, "你好")
    msg = eng.registry.execute("notebook_remember", {"entry": "作者定：某条笔记"})
    assert "未识别" in msg
    assert _notes(shelf_root, "16.夜不收") == []
    ok = eng.registry.execute(
        "notebook_remember", {"entry": "作者定：某条笔记", "book": "夜不收"}
    )
    assert "未识别" not in ok
    assert any("某条笔记" in l for l in _notes(shelf_root, "16.夜不收"))


def test_explicit_book_still_overrides_mount(shelf_root: Path) -> None:
    """宿主挂了 A，模型显式带 B 仍以显式为准。"""
    eng = _engine("brainstormer")
    _turn(eng, "来聊聊夜不收")
    msg = eng.registry.execute(
        "notebook_remember", {"entry": "作者定：元祐", "book": "国子监秘闻录"}
    )
    assert "未识别" not in msg
    assert any("元祐" in l for l in _notes(shelf_root, "17.国子监秘闻录"))
    assert not any("元祐" in l for l in _notes(shelf_root, "16.夜不收"))


def test_prompt_reports_book_name_but_never_leaks_path(shelf_root: Path) -> None:
    """prompt 只回显书名，绝不给绝对路径（避免诱导绕守卫读稿/写到书外）。"""
    eng = _engine("brainstormer")
    ctx = _turn(eng, "来聊聊夜不收这一章")
    assert "夜不收" in ctx
    assert str(shelf_root) not in ctx
    assert "/16.夜不收" not in ctx


def test_unrecognized_turn_keeps_previous_book_visible(shelf_root: Path) -> None:
    """"接着聊"沿用上次那本，但必须如实回显是哪本，不能闷头沿用。"""
    eng = _engine("brainstormer")
    _turn(eng, "来聊聊夜不收")
    ctx = _turn(eng, "接着聊")
    assert "夜不收" in ctx
    msg = eng.registry.execute("notebook_remember", {"entry": "作者定：续章伏笔"})
    assert "夜不收" in msg
    assert any("续章伏笔" in l for l in _notes(shelf_root, "16.夜不收"))


def test_non_notebook_persona_gets_no_book_context(shelf_root: Path) -> None:
    """没有笔记本的专家不受影响，也不注入书稿上下文。"""
    eng = _engine("historian")
    ctx = _turn(eng, "来聊聊夜不收这一章")
    assert "当前在聊" not in ctx
    assert "notebook_remember" not in set(eng.registry.names())
