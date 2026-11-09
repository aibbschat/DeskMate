"""专家笔记本回归（2026-11-09）：书 × 专家的私有笔记，主动记 + 自己维护。

萧哥拍板：① 不是「作者觉得有用才记」，是专家**主动记**；② 可以自己改、优化；
③ 先给脑洞（brainstormer）/ 文学编辑（lit-editor）/ 影视改编（adaptation）三位。

锁住的红线：
- 只写 <书目录>/.deskmate/experts/<专家 id>/notes.md；
- 书稿只读，笔记本之外一个字不写；别的专家读不到；
- 软链不跟、超上限先整理、重复不重复记。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deskmate.bots import notebooks, runtime
from deskmate.bots.notebooks import NOTEBOOK_BOTS, NotebookSession, make_notebook_tools
from deskmate.bots.reading import BookShelf


def _shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。正文正文正文。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


def _session(tmp_path: Path, expert: str = "brainstormer") -> NotebookSession:
    s = NotebookSession(expert, _shelf(tmp_path))
    s.set_book("测试书")
    return s


def _notes_path(tmp_path: Path, expert: str = "brainstormer") -> Path:
    return tmp_path / "books" / "16.测试书" / ".deskmate" / "experts" / expert / "notes.md"


# ------------------------------------------------------------------ 写入/读取

def test_remember_writes_private_notebook(tmp_path: Path) -> None:
    s = _session(tmp_path)
    msg = s.remember("作者裁定：三人一个也不能死（他亲口说的）")
    assert "已记入" in msg
    path = _notes_path(tmp_path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "三人一个也不能死" in text
    assert re.search(r"\[\d{4}-\d{2}-\d{2}\]", text)
    # 书稿一字未动
    assert (tmp_path / "books" / "16.测试书" / "正文.txt").read_text(encoding="utf-8") == (
        "第一章：开头。正文正文正文。"
    )


def test_duplicate_is_not_appended_twice(tmp_path: Path) -> None:
    s = _session(tmp_path)
    s.remember("作者不喜欢把苦难摆出来展览")
    msg = s.remember("作者不喜欢把苦难摆出来展览")
    assert "已经记过" in msg
    assert _notes_path(tmp_path).read_text(encoding="utf-8").count("苦难") == 1


def test_no_book_means_no_notebook(tmp_path: Path) -> None:
    s = NotebookSession("brainstormer", _shelf(tmp_path))
    s.set_book("不存在的书")
    assert s.read() == ""
    msg = s.remember("随便记一条")
    assert "没有挂上" in msg


def test_tools_expose_expected_names(tmp_path: Path) -> None:
    tools = make_notebook_tools(_session(tmp_path))
    assert {t.__name__ for t in tools} == {
        "notebook_read", "notebook_remember", "notebook_feedback", "notebook_rewrite",
    }


# ------------------------------------------------------------------ 自我维护

def test_rewrite_replaces_whole_notebook(tmp_path: Path) -> None:
    s = _session(tmp_path)
    s.remember("旧判断 A")
    s.remember("旧判断 B")
    msg = s.rewrite("# 重整\n- 旧判断 A 与 B 合并成一条")
    assert "已重写" in msg
    text = _notes_path(tmp_path).read_text(encoding="utf-8")
    assert "旧判断 A" in text and "旧判断 B" not in text


def test_entry_and_total_caps(tmp_path: Path) -> None:
    s = _session(tmp_path)
    too_long = "长" * (notebooks.MAX_ENTRY_CHARS + 10)
    assert "太长" in s.remember(too_long)
    # 充满笔记本后要求先整理
    assert "已重写" in s.rewrite("占" * (notebooks.MAX_TOTAL_CHARS - 10))
    assert "快满" in s.remember("再记一条")


def test_symlink_not_followed(tmp_path: Path) -> None:
    s = _session(tmp_path)
    target = _notes_path(tmp_path)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("外部文件", encoding="utf-8")
    target.symlink_to(outside)
    assert s.read() == ""
    assert "没有挂上" in s.remember("不该写进去")
    assert outside.read_text(encoding="utf-8") == "外部文件"


def test_digest_keeps_head_and_tail(tmp_path: Path) -> None:
    s = _session(tmp_path)
    s.rewrite("头" * 800 + "中" * 8000 + "尾" * 800)
    d = s.digest(limit=2000)
    assert "头" in d and "尾" in d
    assert "省略" in d
    assert len(d) <= 2000 + 120


# ------------------------------------------------------------------ 接进 runtime

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


def _runtime(tmp_path: Path) -> runtime.BotRuntime:
    return runtime.BotRuntime(
        books_root=tmp_path / "books",
        workspace_root=tmp_path / "ws",
        provider=_SilentProvider(),
        model="test-model",
    )


def test_only_three_experts_get_notebook_tools(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    try:
        for bot_id in ("brainstormer", "lit-editor", "adaptation"):
            eng = rt.engine(bot_id)
            names = set(eng.registry.names())
            assert {"notebook_read", "notebook_remember", "notebook_rewrite"} <= names
        for bot_id in ("historian", "reader", "publisher", "genre", "proofreader"):
            eng = rt.engine(bot_id)
            names = set(eng.registry.names())
            assert not (names & {"notebook_read", "notebook_remember", "notebook_rewrite"})
    finally:
        rt.close()


def test_engine_notebook_roundtrip(tmp_path: Path) -> None:
    _shelf(tmp_path)  # 先造出「16.测试书」，runtime 才解析得到
    rt = _runtime(tmp_path)
    try:
        eng = rt.engine("brainstormer")
        session = eng._notebook_session
        assert isinstance(session, NotebookSession)
        session.set_book("测试书")
        msg = eng.registry.execute("notebook_remember", {"entry": "作者说：别再把苦难摆出来展览"})
        assert "已记入" in str(msg)
        assert "苦难摆出来" in _notes_path(tmp_path).read_text(encoding="utf-8")
        assert "苦难摆出来" in eng.registry.execute("notebook_read", {})
    finally:
        rt.close()


def test_digest_enters_prompt_and_book_is_synced(tmp_path: Path) -> None:
    _shelf(tmp_path)
    rt = _runtime(tmp_path)
    try:
        eng = rt.engine("brainstormer")
        materials = SimpleNamespace(book="测试书")
        assert rt._notebook_digest(eng, materials) == ""
        eng._notebook_session.set_book("测试书")
        eng.registry.execute("notebook_remember", {"entry": "作者裁定：三人不死"})
        digest = rt._notebook_digest(eng, materials)
        assert "三人不死" in digest
        prompt = runtime.build_prompt("接着聊", None, materials=materials, notebook=digest)
        assert "你的笔记本（私人，只有你看得到）" in prompt
        assert "三人不死" in prompt
        # 没书/没笔记时 prompt 不出现笔记本块
        assert "你的笔记本" not in runtime.build_prompt("接着聊", None)
    finally:
        rt.close()

class _NoteProvider:
    """离线 provider：模型自己决定调 notebook_remember，然后给最终发言。"""

    def __init__(self) -> None:
        self.step = 0
        self.calls: list[dict] = []

    def capabilities(self, model: str):
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn, ToolCall

        self.calls.append({"messages": messages, "tools": tools})
        self.step += 1
        if self.step == 1:
            return AssistantTurn(
                text=None,
                tool_calls=[ToolCall(
                    id="n1", name="notebook_remember",
                    arguments={"entry": "作者裁定：三人一个也不能死"},
                )],
            )
        return AssistantTurn(text="记下了。")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def test_model_can_actively_write_notebook_through_engine(tmp_path: Path) -> None:
    """端到端：模型自主发起 notebook_remember → 落进私有笔记 → 再给最终发言。"""
    from deskmate.bots.materials import MaterialPack
    from deskmate.bots.runtime import BotRuntime

    _shelf(tmp_path)
    prov = _NoteProvider()
    rt = BotRuntime(
        books_root=tmp_path / "books",
        workspace_root=tmp_path / "ws",
        provider=prov,
        model="test-model",
    )
    try:
        pack = MaterialPack(book="测试书", fingerprint="x", body="正文", index="索引")
        reply = rt.speak(
            "brainstormer",
            "先读《测试书》，把我刚说的裁定记下来",
            history=[],
            materials=pack,
        )
        assert "记下了" in reply
        assert prov.step == 2, "应当发生一次笔记本工具调用 + 一次最终发言"
        text = _notes_path(tmp_path).read_text(encoding="utf-8")
        assert "三人一个也不能死" in text
        # 工具 schema 要真的交给模型（否则它没法主动记）
        tool_names = {
            (t.get("function") or {}).get("name")
            for t in (prov.calls[0]["tools"] or [])
        }
        assert "notebook_remember" in tool_names
        # 书稿只读：正文没被动
        assert (tmp_path / "books" / "16.测试书" / "正文.txt").read_text(encoding="utf-8") == (
            "第一章：开头。正文正文正文。"
        )
    finally:
        rt.close()

# ------------------------------------------------------------------ 1v1（catalog）

def test_catalog_exposes_notebook_only_for_three_experts(tmp_path: Path) -> None:
    import coworker.agent  # noqa: F401 — 走真实 import 顺序，避开 catalog 历史循环
    from coworker.agents.base import AgentContext
    from coworker.catalog import expand

    for pid in ("brainstormer", "lit-editor", "adaptation"):
        names = {t.__name__ for t in expand(["notebook"], AgentContext(persona_id=pid))}
        assert {"notebook_read", "notebook_remember", "notebook_rewrite"} <= names
    for pid in ("historian", "editor", "cowork", ""):
        assert expand(["notebook"], AgentContext(persona_id=pid)) == []


def test_persona_tool_list_includes_notebook() -> None:
    from coworker.personas.registry import get_registry

    for pid in ("brainstormer", "lit-editor", "adaptation"):
        entry = get_registry().get(pid)
        assert "notebook" in entry.manifest.tools


def test_tools_accept_explicit_book_for_1v1(tmp_path: Path) -> None:
    """1v1 没有「当前书」，工具必须能靠 book 参数定位。"""
    shelf = _shelf(tmp_path)
    s = NotebookSession("lit-editor", shelf)  # 不 set_book
    tools = {t.__name__: t for t in make_notebook_tools(s)}
    assert "未识别" in tools["notebook_read"]()
    msg = tools["notebook_remember"]("作者说：下一章先别写景", book="测试书")
    assert "已记入" in msg
    assert "先别写景" in tools["notebook_read"](book="测试书")

# ------------------------------------------------------------------ 作者侧（看/删）

def test_author_side_list_read_delete(tmp_path: Path) -> None:
    s = _session(tmp_path, "adaptation")
    s.remember("作者说：下一章先别写景")
    entries = notebooks.list_notebooks(s.shelf)
    assert len(entries) == 1
    assert entries[0]["book"] == "测试书" and entries[0]["expert"] == "adaptation"
    assert entries[0]["chars"] > 0
    text = notebooks.read_notebook(s.shelf, "测试书", "adaptation")
    assert text and "先别写景" in text
    assert notebooks.delete_notebook(s.shelf, "测试书", "adaptation") is True
    assert notebooks.list_notebooks(s.shelf) == []
    assert (tmp_path / "books" / "16.测试书" / "正文.txt").read_text(encoding="utf-8") == (
        "第一章：开头。正文正文正文。"
    )


def test_author_side_rejects_non_notebook_experts(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    assert notebooks.read_notebook(shelf, "测试书", "historian") is None
    assert notebooks.delete_notebook(shelf, "测试书", "historian") is False

# ------------------------------------------------------------------ 反馈（采纳/驳回）

def test_feedback_lands_in_top_section(tmp_path: Path) -> None:
    s = _session(tmp_path)
    s.remember("一条普通判断")
    assert "采纳" in s.feedback(True, "第三条方向采用，伏笔能接上")
    assert "驳回" in s.feedback(False, "第五条太理想化")
    text = _notes_path(tmp_path).read_text(encoding="utf-8")
    assert "## 作者反馈" in text
    assert "采纳：" in text and "驳回：" in text
    # 反馈区在顶部，普通判断还在
    assert text.index("## 作者反馈") < text.index("一条普通判断")
    assert "采纳" in s.digest()


def test_feedback_requires_note_and_caps(tmp_path: Path) -> None:
    s = _session(tmp_path)
    assert "没收到" in s.feedback(True, "")
    assert "太长" in s.feedback(True, "长" * (notebooks.MAX_ENTRY_CHARS + 10))


def test_feedback_tool_with_book_for_1v1(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    s = NotebookSession("adaptation", shelf)  # 不 set_book
    tools = {t.__name__: t for t in make_notebook_tools(s)}
    assert "未识别" in tools["notebook_feedback"](True, "x")
    assert "采纳" in tools["notebook_feedback"](True, "用这条", book="测试书")
    assert "采纳" in tools["notebook_read"](book="测试书")


def test_remember_hints_consolidation_when_large(tmp_path: Path) -> None:
    s = _session(tmp_path)
    assert "已重写" in s.rewrite("占" * int(notebooks.MAX_TOTAL_CHARS * 0.72))
    assert "建议整理" in s.remember("再记一条")
