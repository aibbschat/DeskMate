"""刀1+刀2 回归：稿子位置从「碰运气的检索」变成「随身常识」（2026-11-10 萧哥拍板）。

现场（2026-11-10 晚实测）：正稿路径的唯一持久条目在 learned_history 里 active，
但——
1. 每轮注入按最后一条用户消息的 2-gram 召回，「你去看一下」这类零路径词消息
   永远召回不到 → 她每轮两手空空，只能全盘乱翻（还翻到过桌面同名旧快照）；
2. memory_search 只搜 USER/MEMORY/项目三套**文件**记忆，learned_history 库
   永远搜不到——知识放在库里等于没有。

锁两个治本行为：
- 刀1：无论消息带不带关键词，build_full_context_for_turn 都注入「书稿根目录 +
  当前在写正稿绝对路径」（数据源 writing_context，只读 mtime，fail-open）；
- 刀2：memory_search 增加库侧检索（learned scope），用她当晚的原 query 能找到
  路径条目；无关 query 不命中（不整库乱灌）。

全部 tmp_path + DESKMATE_DIR / DESKMATE_WRITING_WORKSPACE 隔离，不碰真实库、
不碰真实已出版目录。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from deskmate.memory.learned_store import LearnedStore
from deskmate.proactivity import writing_context as wc


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("manuscript", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _make_workspace(tmp_path: Path, *, now: float | None = None) -> Path:
    ws = tmp_path / "已出版"
    ref = now if now is not None else time.time()
    # 当前在写：3 小时前动笔
    _touch(ws / "16.夜不收" / "夜不收：绝命救援.docx", ref - 3 * 3600)
    # 其他书：12 天没动
    _touch(ws / "20.国子监秘闻录" / "国子监秘闻录.docx", ref - 12 * 86400)
    # Word 锁文件与隐藏文件不算活动（口径与 scan_writing_books 一致）
    _touch(ws / "16.夜不收" / "~$夜不收：绝命救援.docx", ref)
    _touch(ws / "16.夜不收" / ".hidden.md", ref)
    return ws


# ---------------------------------------------------------------------------
# 刀1：writing_context.manuscript_location_context
# ---------------------------------------------------------------------------
def test_manuscript_context_gives_abs_path_and_current_book(tmp_path):
    ws = _make_workspace(tmp_path)
    ctx = wc.manuscript_location_context(ws)
    assert ctx, "扫得到书却返回空"
    assert str(ws / "16.夜不收" / "夜不收：绝命救援.docx") in ctx, "必须给正稿绝对路径"
    assert "当前在写" in ctx
    assert "书稿根目录" in ctx and str(ws) in ctx, "根目录=日常写作存放位置，常识行"
    assert "国子监" in ctx and "12 天" in ctx, "其他书要带搁置天数"
    assert "~$" not in ctx and ".hidden" not in ctx, "锁文件/隐藏文件混进了活动口径"


def test_manuscript_context_fail_open_on_missing_workspace(tmp_path):
    assert wc.manuscript_location_context(tmp_path / "不存在") == ""
    assert wc.manuscript_location_context(None) == ""


# ---------------------------------------------------------------------------
# 刀1 集成：零路径词消息也能拿到正稿位置（当晚现场复现）
# ---------------------------------------------------------------------------
def _build_ctx(tmp_path, monkeypatch, message: str) -> str:
    ws = _make_workspace(tmp_path)
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    monkeypatch.setenv("DESKMATE_WRITING_WORKSPACE", str(ws))
    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=base)
    return mgr.build_full_context_for_turn(message)


def test_full_context_carries_manuscript_location_without_keywords(tmp_path, monkeypatch):
    # 2026-11-10 21:15 现场消息原样复现：一个路径词都没有
    ctx = _build_ctx(
        tmp_path, monkeypatch,
        "下午四点去接尹遥，今天的任务没完成，晚上写了一些，你去看一下",
    )
    assert "夜不收：绝命救援.docx" in ctx, "刀1失效：零路径词消息仍拿不到正稿路径"


def test_full_context_carries_manuscript_location_for_generic_ask(tmp_path, monkeypatch):
    ctx = _build_ctx(tmp_path, monkeypatch, "帮我看看今天的稿子")
    assert "夜不收：绝命救援.docx" in ctx


def test_manuscript_inject_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_WRITING_INJECT", "0")
    ctx = _build_ctx(tmp_path, monkeypatch, "帮我看看今天的稿子")
    assert "夜不收：绝命救援.docx" not in ctx, "DESKMATE_WRITING_INJECT=0 杀开关失效"


# ---------------------------------------------------------------------------
# 刀2：库侧检索（learned scope）
# ---------------------------------------------------------------------------
def _seed_learned(base: Path) -> None:
    LearnedStore(base_dir=base).append(
        [
            "萧盛书稿固定位置：《夜不收》正稿=/Users/xiaocheng/Documents/已出版/16.夜不收/夜不收：绝命救援.docx",
            "萧哥周六周日不写作，陪孩子",
        ],
        source="live",
    )


def test_search_user_facts_finds_path_fact_with_her_queries(tmp_path):
    _seed_learned(tmp_path)
    from deskmate.memory.user_fact_context import search_user_facts

    # 她当晚的原 query（两条都要能找到）
    for query in ("夜不收 稿件 路径 docx", "夜不收 文件夹 目录 桌面 在哪 读稿"):
        hits = search_user_facts(tmp_path, query)
        assert any("绝命救援" in fact for _, fact, _ in hits), f"库侧检索没找到路径条目：{query}"
    # 无关 query 不命中（不整库乱灌）
    assert not search_user_facts(tmp_path, "天气怎么样")


def test_memory_search_tool_learned_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    _seed_learned(tmp_path)
    from deskmate.memory import server as mem_server

    monkeypatch.setattr(mem_server, "_mgr", None)
    out = mem_server.memory_search("夜不收 稿件 路径 docx", scope="learned")
    assert "绝命救援" in out, "memory_search 的 learned scope 没接上库"
    out_all = mem_server.memory_search("夜不收 文件夹 目录 在哪 读稿", scope="all")
    assert "绝命救援" in out_all, "scope=all 未包含库侧结果"
