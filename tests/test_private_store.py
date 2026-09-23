"""私人资料库（private_store）单测 —— hermetic，零外部依赖。

验证核心可行性：灌库后能被 fts5.search_across_dbs 检索到（与史料通道共用引擎）。
"""

import sqlite3
from pathlib import Path

from coworker.history.fts5 import search_across_dbs
from coworker.history.private_store import (
    clean_missing,
    ingest_files,
    list_categories,
    scan_sources,
    _db_path,
)


def _mk_file(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_ingest_and_search_md(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "明史笔记.md", "张居正推行一条鞭法，整顿财政。\n\n万历初年清丈田亩，国库充盈。")
    r = ingest_files([f], "写作教程", history_dir=hist)
    assert r["ingested"] == 1, r
    dbp = _db_path("写作教程", history_dir=hist)
    results = search_across_dbs([str(dbp)], "一条鞭法", limit=5)
    assert len(results) >= 1, results
    assert any("一条鞭法" in (x.get("snippet") or "") for x in results)
    assert results[0]["category"] == "写作教程"
    assert results[0]["source"] == "private"


def test_idempotent_and_update(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "a.md", "初版内容：王安石变法。")
    assert ingest_files([f], "随笔", history_dir=hist)["ingested"] == 1
    assert ingest_files([f], "随笔", history_dir=hist)["skipped"] == 1
    # 改内容 → 更新（旧 chunks_fts 必须被清掉，否则会重复/残留）
    _mk_file(src, "a.md", "修订版：王安石变法与熙宁新政。")
    r3 = ingest_files([f], "随笔", history_dir=hist)
    assert r3["updated"] == 1, r3
    dbp = _db_path("随笔", history_dir=hist)
    con = sqlite3.connect(str(dbp))
    # 更新后该 article 只应有 1 条，且 chunks_fts 行数与 chunks 一致
    n_articles = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_fts = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    con.close()
    assert n_articles == 1, n_articles
    assert n_chunks == n_fts, (n_chunks, n_fts)


def test_sacred_excluded(tmp_path: Path):
    hist = tmp_path / "history"
    sacred = tmp_path / "国子监秘闻录"
    sacred.mkdir()
    f = _mk_file(sacred, "secret.md", "绝密内容不许摄入。")
    r = ingest_files([str(f)], "史料", history_dir=hist)
    assert r["skipped"] == 1
    assert not _db_path("史料", history_dir=hist).is_file()


def test_supported_exts_only(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "a.png", "binary")
    assert ingest_files([f], "随笔", history_dir=hist)["skipped"] == 1


def test_scan_is_light_no_content_read(tmp_path: Path):
    src = tmp_path / "notes"
    src.mkdir()
    _mk_file(src, "x.md", "内容" * 1000)
    cands = scan_sources([str(src)], history_dir=tmp_path / "history")
    assert len(cands) == 1
    assert cands[0]["ext"] == ".md"
    assert "size" in cands[0] and "mtime" in cands[0]
    # 不读内容：候选里没有 excerpt / snippet 字段
    assert "excerpt" not in cands[0] and "snippet" not in cands[0]


def test_list_categories_and_clean_missing(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "b.md", "某资料内容。")
    ingest_files([str(f)], "分类甲", history_dir=hist)
    assert "分类甲" in list_categories(history_dir=hist)
    Path(f).unlink()
    n = clean_missing("分类甲", history_dir=hist)
    assert n == 1
    con = sqlite3.connect(str(_db_path("分类甲", history_dir=hist)))
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    con.close()


def test_chinese_category_filename(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "c.md", "明清笔记一则。")
    ingest_files([str(f)], "明清笔记", history_dir=hist)
    dbp = _db_path("明清笔记", history_dir=hist)
    assert dbp.name == "private-明清笔记.db"
    assert dbp.is_file()


def test_search_private_tool(tmp_path: Path):
    from coworker.history.private_tool import make_private_search_tool

    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    f = _mk_file(src, "明史笔记.md", "张居正推行一条鞭法，整顿财政。")
    ingest_files([f], "写作教程", history_dir=hist)
    tool = make_private_search_tool(data_dir=hist)
    out = tool("一条鞭法")
    assert out["count"] >= 1, out
    # 逻辑隔离：私人资料不当史料分层
    assert out["results"][0]["era"] == "private"
    assert out["results"][0]["eraLabel"] == "私人资料"
    # 限定不存在的分类 → 空结果
    out2 = tool("一条鞭法", categories=["不存在的分类"])
    assert out2["count"] == 0


def test_private_scan_skips_non_supported_and_sacred(tmp_path: Path):
    src = tmp_path / "notes"
    src.mkdir()
    _mk_file(src, "a.md", "资料一")
    _mk_file(src, "b.png", "图片")
    sacred = tmp_path / "国子监秘闻录"
    sacred.mkdir()
    _mk_file(sacred, "s.md", "神圣")
    cands = scan_sources([str(src), str(sacred)], history_dir=tmp_path / "history")
    assert {c["ext"] for c in cands} == {".md"}  # 只收 md，跳过 png 与神圣目录


def test_pdf_docx_graceful_skip(tmp_path: Path):
    hist = tmp_path / "history"
    src = tmp_path / "notes"
    src.mkdir()
    # 伪 pdf/docx（非真实格式）→ 解析失败 → fail-open 跳过，不崩溃
    pf = _mk_file(src, "fake.pdf", "not a real pdf")
    df = _mk_file(src, "fake.docx", "not a real docx")
    r = ingest_files([pf, df], "测试", history_dir=hist)
    assert r["skipped"] == 2
    assert r["ingested"] == 0
