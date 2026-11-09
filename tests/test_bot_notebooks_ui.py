"""专家笔记本界面回归（2026-11-09）：/notebooks 页 + 查看/删除 API。

页面与 API 都在 sidecar 的 /bots 前缀下（auth 前缀放行已有测试覆盖）。
锁住：能被主 GUI 打开、能看、能删、删了不影响书稿、非法专家/越界拒绝。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deskmate.bots.notebooks import NotebookSession
from deskmate.bots.reading import BookShelf
from deskmate.bots.web import create_app


def _make_book(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


def _client(tmp_path: Path, monkeypatch, prefix: str = "") -> TestClient:
    monkeypatch.setenv("DESKMATE_BOT_BOOKS_ROOT", str(tmp_path / "books"))
    (tmp_path / "store").mkdir(exist_ok=True)
    return TestClient(create_app(base_dir=str(tmp_path / "store"), base_prefix=prefix))


def test_notebooks_page_renders(tmp_path: Path, monkeypatch) -> None:
    shelf = _make_book(tmp_path)
    client = _client(tmp_path, monkeypatch)
    page = client.get("/notebooks")
    assert page.status_code == 200
    assert "专家笔记本" in page.text
    assert "const BASE" in page.text
    assert client.get("/api/notebooks").json()["items"] == []
    assert shelf.list_books() == ["测试书"]


def test_notebooks_content_and_delete(tmp_path: Path, monkeypatch) -> None:
    shelf = _make_book(tmp_path)
    s = NotebookSession("brainstormer", shelf)
    s.set_book("测试书")
    s.remember("作者裁定：三人一个也不能死")
    client = _client(tmp_path, monkeypatch)

    items = client.get("/api/notebooks").json()["items"]
    assert items and items[0]["expert"] == "brainstormer"

    got = client.get(
        "/api/notebooks/content", params={"book": "测试书", "expert": "brainstormer"}
    )
    assert got.status_code == 200
    assert "三人一个也不能死" in got.json()["text"]

    deleted = client.delete(
        "/api/notebooks", params={"book": "测试书", "expert": "brainstormer"}
    )
    assert deleted.status_code == 200 and deleted.json()["ok"] is True
    assert client.get("/api/notebooks").json()["items"] == []
    # 书稿不受影响
    assert (tmp_path / "books" / "16.测试书" / "正文.txt").read_text(encoding="utf-8") == "第一章：开头。"


def test_notebooks_rejects_bad_expert_and_book(tmp_path: Path, monkeypatch) -> None:
    _make_book(tmp_path)
    client = _client(tmp_path, monkeypatch)
    assert client.get(
        "/api/notebooks/content", params={"book": "测试书", "expert": "historian"}
    ).status_code == 404
    assert client.get(
        "/api/notebooks/content", params={"book": "../etc", "expert": "brainstormer"}
    ).status_code == 404
    assert client.delete(
        "/api/notebooks", params={"book": "不存在", "expert": "brainstormer"}
    ).status_code == 404


def test_bot_page_links_to_notebooks_with_prefix(tmp_path: Path, monkeypatch) -> None:
    _make_book(tmp_path)
    client = _client(tmp_path, monkeypatch, prefix="/bots")
    bot_page = client.get("/")
    assert bot_page.status_code == 200
    assert 'id="head-notebooks"' in bot_page.text
    assert "url('/notebooks')" in bot_page.text
    nb_page = client.get("/notebooks")
    assert nb_page.status_code == 200
    assert '"/bots"' in nb_page.text

def test_notebooks_save_edit(tmp_path: Path, monkeypatch) -> None:
    """作者可以编辑保存；上限/空内容/非法专家都拒绝；书稿不动。"""
    shelf = _make_book(tmp_path)
    s = NotebookSession("lit-editor", shelf)
    s.set_book("测试书")
    s.remember("旧内容")
    client = _client(tmp_path, monkeypatch)

    saved = client.post(
        "/api/notebooks/content",
        json={"book": "测试书", "expert": "lit-editor", "text": "# 整理后\n- 只剩一条有用的"},
    )
    assert saved.status_code == 200 and saved.json()["ok"] is True
    text = (tmp_path / "books" / "16.测试书" / ".deskmate" / "experts" / "lit-editor" / "notes.md").read_text(encoding="utf-8")
    assert "只剩一条有用的" in text and "旧内容" not in text

    too_long = client.post(
        "/api/notebooks/content",
        json={"book": "测试书", "expert": "lit-editor", "text": "长" * 24_010},
    )
    assert too_long.status_code == 400
    assert client.post(
        "/api/notebooks/content",
        json={"book": "测试书", "expert": "lit-editor", "text": "   "},
    ).status_code == 400
    assert client.post(
        "/api/notebooks/content",
        json={"book": "测试书", "expert": "historian", "text": "x"},
    ).status_code == 400
    assert (tmp_path / "books" / "16.测试书" / "正文.txt").read_text(encoding="utf-8") == "第一章：开头。"


def test_notebooks_page_has_edit_controls(tmp_path: Path, monkeypatch) -> None:
    _make_book(tmp_path)
    page = _client(tmp_path, monkeypatch).get("/notebooks").text
    assert 'id="edit"' in page and 'id="save"' in page and 'id="editor"' in page
    assert "notebooks/content" in page
