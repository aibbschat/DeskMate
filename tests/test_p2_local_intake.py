"""P2-3 本地摄入管线 hermetic 测试。

不依赖真实库 / 真实文件系统外的任何状态：base_dir 全部指向 tmp_path。
覆盖：神圣稿件排除、系统目录跳过、摘要格式、沉淀幂等 / 更新语义、来源标记。
"""

from __future__ import annotations

import json
from pathlib import Path

from deskmate.ingest import local_intake as li
from deskmate.memory.learned_store import LearnedStore


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_scan_excludes_sacred_and_system_dirs(tmp_path: Path):
    # 顶层笔记（应被扫到）
    _write(tmp_path / "notes" / "idea.md", "# 明朝内阁\n这是关于张居正的想法。")
    # 神圣稿件目录（国子监秘闻录）里的笔记 —— 必须排除
    _write(tmp_path / "国子监秘闻录" / "draft.md", "# 国子监\n绝密草稿。")
    # 系统 / 依赖目录（必须跳过）
    _write(tmp_path / "repo" / ".git" / "log.md", "git stuff")
    _write(tmp_path / "repo" / "node_modules" / "x.md", "dep note")
    _write(tmp_path / "repo" / "src" / "main.rs", "fn main() {}")  # 非文本扩展名

    cands = li.scan_directories([str(tmp_path)])
    paths = {c["path"] for c in cands}
    assert any("idea.md" in p for p in paths), f"应扫到顶层笔记，实际：{paths}"
    assert all("国子监秘闻录" not in p for p in paths), "神圣稿件被扫到"
    assert all(".git" not in p and "node_modules" not in p for p in paths), "系统目录被扫到"
    assert all(p.endswith((".md", ".markdown", ".txt", ".text", ".org", ".rst")) for p in paths)


def test_build_facts_format(tmp_path: Path):
    _path = tmp_path / "n.md"
    _write(_path, "第一条想法\n第二条补充")
    facts = li.build_facts(str(_path))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.startswith("[本地笔记] n：")
    # 不得用《》括文件名 —— 避免被 LearnedStore 当成「书」实体
    assert "《" not in fact and "》" not in fact


def test_ingest_writes_with_source_and_is_idempotent(tmp_path: Path):
    note = tmp_path / "a.md"
    _write(note, "关于写作的方法论。")
    base = tmp_path / "desk"
    r1 = li.ingest_files([str(note)], base_dir=base)
    assert r1["ingested"] == 1 and r1["skipped"] == 0

    store = LearnedStore(base_dir=base)
    rows = store.list_for_ui()
    assert len(rows) == 1
    assert rows[0]["source"] == f"local_intake:{note}"
    assert "本地摄入" not in rows[0]["fact"]  # 来源标记在 source 列，不在 fact 正文

    # 同内容再沉淀 —— 幂等跳过
    r2 = li.ingest_files([str(note)], base_dir=base)
    assert r2["skipped"] == 1 and r2["ingested"] == 0
    assert len(store.list_for_ui()) == 1  # 仍是 1 条活跃


def test_ingest_content_change_retires_old(tmp_path: Path):
    note = tmp_path / "b.md"
    _write(note, "初版内容。")
    base = tmp_path / "desk"
    li.ingest_files([str(note)], base_dir=base)
    store = LearnedStore(base_dir=base)

    # 改内容后重新沉淀
    _write(note, "修订后的内容，加入了新观点。")
    r = li.ingest_files([str(note)], base_dir=base)
    assert r["updated"] == 1 and r["ingested"] == 1

    rows = store.list_for_ui()
    # 旧条目标 superseded，新条目 active —— 活跃只 1 条
    active = [x for x in rows if x["status"] == "active"]
    superseded = [x for x in rows if x["status"] == "superseded"]
    assert len(active) == 1
    assert len(superseded) == 1
    assert "修订后" in active[0]["fact"]


def test_save_and_load_dirs(tmp_path: Path, monkeypatch):
    # 让配置落在 tmp，避免污染真实 ~/.deskmate
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path / "desk"))
    expected = str(Path("/tmp/x").expanduser().resolve())  # /tmp → /private/tmp 规范路径
    saved = li.save_dirs(["/tmp/x", "/tmp/x", "  "])  # 去重 + 去空
    assert saved == [expected]
    assert li.load_dirs() == [expected]
