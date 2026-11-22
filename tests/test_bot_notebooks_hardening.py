"""专家笔记本补强回归（2026-11-09 复查）：并发/守卫/重复判定/反馈区/群聊工具面。

复查实测抓到的 6 个缺陷，先红后绿锁住：
1. 1v1 与群聊各有 NotebookSession，读-改-写没有跨入口锁 ⇒ 丢笔记 / FileNotFoundError；
2. save_notebook 在路径守卫拒绝时仍返回成功（UI 报「已保存」，实际没写）；
3. remember 用子串判重复 ⇒ 短条目被已有长条目包含就被吞掉；
4. 悬空软链绕过「拒软链」；
5. feedback 用 index() 找标题 ⇒ 正文里引用同名标题会把笔记劈开；
6. 群聊人设承诺 ask_user / notebook_feedback，工具面却没注册；guidance 又无条件说
   「只读，不写文件」，与「主动记」冲突。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from deskmate.bots import notebooks as nb
from deskmate.bots.notebooks import (
    NotebookSession,
    _notes_path_for,
    save_notebook,
)
from deskmate.bots.reading import BookShelf


def _shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


def _notes_path(tmp_path: Path, expert: str = "brainstormer") -> Path:
    return (
        tmp_path / "books" / "16.测试书" / ".deskmate" / "experts" / expert / "notes.md"
    )


def _session(tmp_path: Path, expert: str = "brainstormer") -> NotebookSession:
    s = NotebookSession(expert, _shelf(tmp_path))
    s.set_book("测试书")
    return s


# ------------------------------------------------------------------ 1. 跨入口并发
def test_concurrent_sessions_do_not_lose_notes(tmp_path: Path, monkeypatch) -> None:
    """同一个 notes.md 被 8 个独立 session（模拟 1v1 / 群聊 / 作者编辑）同时追加。"""
    shelf = _shelf(tmp_path)
    sessions = [NotebookSession("brainstormer", shelf) for _ in range(8)]
    for s in sessions:
        s.set_book("测试书")

    original = nb.NotebookSession._write_atomic

    def slow_write(path, text):  # noqa: ANN001
        time.sleep(0.012)  # 放大读-改-写的交错窗口
        return original(path, text)

    monkeypatch.setattr(nb.NotebookSession, "_write_atomic", staticmethod(slow_write))

    errors: list[str] = []

    def go(i: int, s: NotebookSession) -> None:
        try:
            s.remember(f"并发条目-{i}：作者采纳了第{i}条伏笔")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=go, args=(i, s)) for i, s in enumerate(sessions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = _notes_path(tmp_path).read_text(encoding="utf-8")
    assert not errors, f"并发写入抛异常：{errors}"
    for i in range(8):
        assert f"并发条目-{i}" in text, f"第 {i} 条笔记丢失"


def test_cross_process_writes_are_serialized(tmp_path: Path) -> None:
    """跨进程（桌面后端 / 云端 / 后台脚本）写同一本，必须靠文件锁串行化。"""
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章。", encoding="utf-8")

    code = """
import sys
from pathlib import Path
from deskmate.bots.reading import BookShelf
from deskmate.bots.notebooks import NotebookSession
s = NotebookSession('brainstormer', BookShelf(Path(sys.argv[1])))
s.set_book('测试书')
tag, base = sys.argv[2], int(sys.argv[3])
for i in range(8):
    s.remember('进程%s-条目-%d：作者采纳' % (tag, base + i))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("NODE_OPTIONS", None)
    root = Path(__file__).resolve().parents[1]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path / "books"), tag, str(100 * i)],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i, tag in enumerate(("A", "B", "C"))
    ]
    failures = []
    for p in procs:
        _, err = p.communicate(timeout=60)
        if p.returncode != 0:
            failures.append(err[-400:])
    assert not failures, f"子进程失败：{failures}"

    text = _notes_path(tmp_path).read_text(encoding="utf-8")
    missing = [
        f"进程{tag}-条目-{100 * i + j}"
        for i, tag in enumerate(("A", "B", "C"))
        for j in range(8)
        if f"进程{tag}-条目-{100 * i + j}" not in text
    ]
    assert not missing, f"跨进程并发丢了 {len(missing)} 条：{missing[:5]}"


# ------------------------------------------------------------------ 2. 守卫误报
def test_save_notebook_reports_guard_rejection_as_failure(tmp_path: Path) -> None:
    """父目录是书外软链 ⇒ 路径守卫拒绝，不能让 UI 显示「已保存」。"""
    shelf = _shelf(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    d = tmp_path / "books" / "16.测试书" / ".deskmate" / "experts" / "lit-editor"
    d.mkdir(parents=True)
    d.rmdir()
    d.symlink_to(outside, target_is_directory=True)

    ok, message = save_notebook(shelf, "测试书", "lit-editor", "作者编辑后的内容")
    assert ok is False, f"守卫拒绝却报成功：{message}"
    assert not (outside / "notes.md").exists(), "绝不能写到书目录之外"


# ------------------------------------------------------------------ 3. 重复判定
def test_remember_short_entry_is_not_swallowed_by_longer_one(tmp_path: Path) -> None:
    s = _session(tmp_path, "adaptation")
    s.remember("作者明确采纳影视改编优先，表达的观点胜过史料真实")
    msg = s.remember("影视改编优先")
    text = _notes_path(tmp_path, "adaptation").read_text(encoding="utf-8")
    assert "已经记过" not in msg, "短条目被长条目的子串误判为重复"
    assert "影视改编优先" in text
    assert text.count("- [") == 2, "应当有两条独立笔记"


def test_feedback_duplicate_check_is_line_exact(tmp_path: Path) -> None:
    s = _session(tmp_path, "lit-editor")
    s.feedback(True, "把三人活下来扩成主线")
    msg = s.feedback(True, "把三人活下来")
    assert "已经记过" not in msg


# ------------------------------------------------------------------ 4. 软链
def test_dangling_symlink_is_rejected(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    s = NotebookSession("adaptation", shelf)
    s.set_book("测试书")
    p = _notes_path(tmp_path, "adaptation")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(tmp_path / "不存在的目标.md")
    assert s._path() is None, "悬空软链也必须拒绝"
    assert s.read() == ""
    assert _notes_path_for(shelf, "测试书", "adaptation") is None


# ------------------------------------------------------------------ 5. 反馈区插入
def test_feedback_does_not_split_note_when_body_quotes_heading(tmp_path: Path) -> None:
    s = _session(tmp_path, "lit-editor")
    original = "第一条普通笔记：正文里引用了「## 作者反馈」这个标题做例子"
    s.remember(original)
    s.feedback(True, "这条建议采纳")
    text = _notes_path(tmp_path, "lit-editor").read_text(encoding="utf-8")
    assert original in text, "原笔记被从中间劈开了"
    assert text.count("## 作者反馈") >= 1
    # 反馈条目要在真正的反馈区标题之后（正文引用不算标题）
    heading_pos = [i for i in range(len(text)) if text.startswith("\n## 作者反馈\n", i)]
    assert heading_pos, "没有独立的反馈区标题行"
    assert text.find("- [", heading_pos[0]) > heading_pos[0]


# ------------------------------------------------------------------ 6. 群聊工具面
def test_group_guidance_matches_actual_tool_surface(tmp_path: Path) -> None:
    """人设承诺的工具必须真的存在；群里不做的两条要显式说明。"""
    from deskmate.bots import runtime

    class P:
        def capabilities(self, model):  # noqa: ANN001
            from coworker.providers.base import ModelCapabilities

            return ModelCapabilities()

        def complete(self, **kw):  # noqa: ANN001
            from coworker.providers.base import AssistantTurn

            return AssistantTurn(text="ok")

        def stream(self, **kw):  # noqa: ANN001
            from coworker.providers.base import AssistantTurn, StreamChunk

            yield StreamChunk(turn=AssistantTurn(text="ok"))

    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    rt = runtime.BotRuntime(
        books_root=tmp_path / "books",
        workspace_root=tmp_path / "ws",
        provider=P(),
        model="test-model",
    )
    eng = rt.engine("brainstormer")
    names = set(eng.registry.names())
    assert "notebook_feedback" not in names, "群聊按拍板不做反馈闭环"
    assert "ask_user" not in names, "群聊没有问答卡"
    prompt = str(eng.messages[0].get("content") or "")
    assert "没有 ask_user" in prompt, "人设必须显式说明当前没有问答卡"
    assert "没有 notebook_feedback" in prompt, "人设必须显式说明群里没有反馈工具"
    assert "书稿只读" in prompt, "「只读」要限定为书稿，不能压掉主动记笔记"
