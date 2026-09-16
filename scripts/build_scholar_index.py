#!/usr/bin/env python3
"""把近现代/当代研究著作灌入本地史料库（FTS5 全文索引）。

为什么要单独建这个库
--------------------
``duan-dai-shi`` / ``shibu-shi`` 两个库**全是古籍原典**。官修正史受政治与权力
因素影响，会讳饰、颠倒、删改，把原典当成唯一标准是有风险的。因此需要把近现代
与当代学者的研究专著一并入库，让检索结果能同时呈现「古人怎么说」与「后人怎么
考」，由使用者交叉比对后自行判断。

本脚本灌入的是**研究著作**层（era=scholar），与原典层在检索时分面交错呈现
（见 ``coworker/history/fts5.py`` 的 ``_facet_merge``）。

元数据约定
----------
* ``category = "研究著作"``  → 被 ``_era_of`` 识别为 scholar 层
* ``source = "scholar"``     → ``_source_order`` 里与 daizhigev20 同级，可直引
* ``dynasty = "当代"``       → **成书年代，不是研究对象朝代**

  这一条是刻意设计的：若填研究对象朝代「明」，模型引用时会写出「明代 萧盛
  《明朝的裂变》」这种把作者当成明代人的错误标注。填「当代」虽不严谨（当代
  不是朝代），但不会误导。研究著作的正确引用格式是「作者 + 书名 + 出版信息」，
  不写朝代。

* ``author`` 必填 → 缺 author 时 ``_era_of`` 无法兜底判定为专著

索引约定（与古籍库完全一致，别改）
----------------------------------
* FTS5 普通模式：``fts5(content, heading, tokenize='unicode61')``
* 正文必须是**字间插空格**的预处理文本（``tokenize_cjk``）
* ``rowid`` 必须等于 ``chunks.id``——检索 SQL 依赖 ``JOIN chunks c ON c.id = f.rowid``

用法
----
    python scripts/build_scholar_index.py                    # 灌入默认清单
    python scripts/build_scholar_index.py 某书.docx 另一本.docx

零外部依赖，只用标准库（.docx 用 zipfile + XML 解析，不装 python-docx）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from docx_extract import extract_docx  # noqa: E402

# 与服务器生产库保持一致的索引定义（普通模式，非 external-content）
FTS_DDL = "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61')"

# CJK 范围逐条对齐 fts5.js：一-鿿(4E00-9FFF) 㐀-䶿(3400-4DBF) 豈-﫿(F900-FAFF)
import re  # noqa: E402

_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
_MULTI_SPACE_RE = re.compile(r"\s+")

# 分块大小。太大则片段里塞满无关内容，太小则割裂论证上下文。
CHUNK_SIZE = 900


def tokenize_cjk(text: str) -> str:
    """在 CJK 字符间插空格（``fts5.js`` 的 ``tokenizeCJK`` 等价实现）。"""
    return _MULTI_SPACE_RE.sub(" ", _CJK_RE.sub(r" \1 ", text)).strip()


# ---------------------------------------------------------------------------
# 待灌书单
#
# 显式列清单而非自动扫描目录：目录里混着合同、选题简介、样张、目录页，
# 自动扫描会把这些无关文件一起灌进去（样张与正文重复还会造成检索重复命中）。
# ---------------------------------------------------------------------------
PUBLISHED = Path.home() / "Documents" / "已出版"

DEFAULT_BOOKS: tuple[tuple[str, str], ...] = (
    # (相对路径, 正式书名)
    (
        "17.明朝的裂变：大明王朝的两张面孔/明朝的裂变.docx",
        "明朝的裂变：大明王朝的两张面孔",
    ),
    ("18.明朝简史/明朝简史.docx", "明朝简史"),
    ("6.大明梦华：明朝生活实录/正文/大明梦华：明朝生活实录.docx", "大明梦华：明朝生活实录"),
    ("10.明朝烟火味儿/正文/明朝烟火味儿.docx", "明朝烟火味儿"),
    ("14.大宋梦华：宋朝的自由与风雅/大宋梦华.docx", "大宋梦华：宋朝的自由与风雅"),
    ("15.朕本凡人，奈何天子/朕本凡人，奈何天子.docx", "朕本凡人，奈何天子"),
    ("17.明朝的裂变：大明王朝的两张面孔/附录/崇祯时期的五十阁臣.docx", "附录：崇祯时期的五十阁臣"),
    ("17.明朝的裂变：大明王朝的两张面孔/附录/明朝内阁成员名录.docx", "附录：明朝内阁成员名录"),
)

# ⚠ 故意不收的两本，别再加回来：
#
# * ``10.明朝烟火味儿/正文/明朝那些闲事儿.docx``（26.6 万字）——与已收的
#   《明朝烟火味儿》是**同一本书的两个稿次**，"闲事儿"是旧稿。实测两书 8-40 字
#   短句交集 180 条，重复率分别为 32.5% / 36.6%，二级标题几乎一一对应：
#     旧稿「一、混堂沐浴那些事儿 / 二、肥皂、香水… / 三、浅谈如厕」
#     定稿「一、捉虱的那些事儿 / 二、混堂沐浴 / 三、关于洗漱的那些奇思妙想」
#   两本都灌会让同一段话在检索里命中两次，白占配额还误导。只收定稿。
#
# * ``11.中国妖怪录/妖怪传说.docx``（20 万字）——神话传说整理，不是史料。
#   灌进 scholar 层有被当成史实引用的风险。萧哥若需要查妖怪典故，应另建库。
#
# * ``20.国子监秘闻录``——神圣稿件，任何情况下不碰。

# 萧哥本人的著作（当代）。dynasty 填"当代"是**成书年代**，不是研究对象朝代——
# 若填"明"，模型引用时会写出"明代 萧盛《明朝的裂变》"这种把作者当成明代人的
# 错误标注。研究著作的正确引用格式是「作者 + 书名」，不写朝代。
AUTHOR_XIAO = "萧盛"
SOURCE = "scholar"
CATEGORY = "研究著作"

# 从维基文库等渠道下载的公有领域著作：文件名（不含扩展名）→ (作者, 成书年代)。
# 同样是成书年代而非研究对象朝代，理由同上。
# 只收录作者去世满 50 年的公有领域作品，详见 scripts/fetch_wikisource.py。
DOWNLOADED_BOOKS: dict[str, tuple[str, str]] = {
    "清朝前纪": ("孟森", "近代"),
    "唐代政治史述论稿": ("陈寅恪", "近代"),
    "殷周制度论": ("王国维", "近代"),
    "中国史纲": ("张荫麟", "近代"),
    "两宋史纲": ("张荫麟", "近代"),
}

# 已核实维基文库**没有**全文的近现代名著（留档，避免以后重复找）：
#   孟森《明史讲义》《清史讲义》、吕思勉《白话本国史》《中国通史》、
#   张荫麟《中国史纲》、梁启超《清代学术概论》、柳诒徵《中国文化史》、
#   邓之诚《中华二千年史》、吴晗《朱元璋传》、陈寅恪《隋唐制度渊源略论稿》。
# 另有钱穆《国史大纲》（2040 解禁）、黄仁宇《万历十五年》（2050 解禁）
# 仍在版权期，任何渠道都不得入库。

SOURCES_DIR = Path.home() / ".deskmate" / "history" / "sources"


def collect_books() -> list[tuple[Path, str, str, str]]:
    """汇总待灌书目：``(路径, 书名, 作者, 成书年代)``。

    全量重建而非增量——库只有几十 MB，重建不到一秒，增量反而要处理去重。
    """
    books: list[tuple[Path, str, str, str]] = []
    for rel, title in DEFAULT_BOOKS:
        books.append((PUBLISHED / rel, title, AUTHOR_XIAO, "当代"))

    if SOURCES_DIR.is_dir():
        # 只灌入 DOWNLOADED_BOOKS 明确列出的研究著作，避免把 sources/ 里的
        # 古籍原典（shiliao-shi.db 已收录）重复灌进 scholar 库。
        for stem, (author, era) in DOWNLOADED_BOOKS.items():
            p = SOURCES_DIR / f"{stem}.txt"
            if p.is_file():
                books.append((p, stem, author, era))
            else:
                print(f"[警告] DOWNLOADED_BOOKS 列出但文件缺失：{p.name}")
    return books


def split_chunks(text: str, size: int = CHUNK_SIZE) -> list[tuple[str, str]]:
    """把正文切成 (heading, content) 列表。

    以 ``## `` 开头的行视为章节标题，作为其后各块的 heading 保留；
    标题文字本身也留在正文里，避免内容丢失。
    """
    chunks: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal buf, length
        if buf:
            body = "\n".join(buf).strip()
            if body:
                chunks.append((heading, body))
            buf = []
            length = 0

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            # 换章时先收尾上一章，避免两块内容拼接跨越章节边界
            if length >= size * 0.6:
                flush()
            heading = stripped[3:].strip()
            buf.append(heading)
            length += len(heading)
            continue
        buf.append(stripped)
        length += len(stripped)
        if length >= size:
            flush()

    flush()
    return chunks


def init_db(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    con.execute(
        """CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            source TEXT,
            source_file TEXT,
            title TEXT,
            category TEXT,
            dynasty TEXT,
            author TEXT,
            content TEXT,
            content_hash TEXT,
            file_path TEXT,
            created_at TEXT
        )"""
    )
    con.execute(
        """CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL,
            chunk_index INTEGER,
            heading TEXT,
            content TEXT,
            fts_content TEXT
        )"""
    )
    con.execute("CREATE INDEX idx_chunks_article ON chunks(article_id)")
    con.execute(FTS_DDL)
    con.commit()
    return con


def _read_book(book_path: Path) -> tuple[str, int]:
    """按扩展名读取正文，返回 (文本, 段落数)。.txt 与 .docx 都支持。"""
    if book_path.suffix.lower() == ".txt":
        text = book_path.read_text(encoding="utf-8", errors="replace")
        return text, text.count("\n") + 1
    return extract_docx(book_path)


def ingest(
    con: sqlite3.Connection,
    book_path: Path,
    title: str,
    author: str,
    dynasty: str,
) -> int:
    text, n_paras = _read_book(book_path)
    if not text or not text.strip():
        print(f"  [跳过] 无正文：{book_path.name}")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = con.execute(
        "INSERT INTO articles (source, source_file, title, category, dynasty, "
        "author, content, content_hash, file_path, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOURCE,
            str(book_path),
            title,
            CATEGORY,
            dynasty,
            author,
            text,
            hashlib.md5(text.encode("utf-8")).hexdigest(),
            str(book_path),
            now,
        ),
    )
    article_id = cur.lastrowid

    chunks = split_chunks(text)
    rows = []
    for idx, (heading, body) in enumerate(chunks):
        rows.append((article_id, idx, heading, body, tokenize_cjk(body)))

    con.executemany(
        "INSERT INTO chunks (article_id, chunk_index, heading, content, fts_content) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    # 注意：这里不写 chunks_fts。FTS 内容表统一由 build_fts() 一次性灌入，
    # 因为它必须严格按 chunks.id 顺序插入才能保证 rowid == chunks.id。
    con.commit()
    era_mark = f"{author}/{dynasty}"
    print(f"  ✓ {title:<28} {era_mark:<14} {len(text):>8,} 字 → {len(chunks):>4} 块")
    return len(chunks)


def build_fts(con: sqlite3.Connection) -> None:
    """重建 FTS 内容表，保证 rowid == chunks.id。"""
    con.execute("DELETE FROM chunks_fts")
    con.execute(
        "INSERT INTO chunks_fts (rowid, content, heading) "
        "SELECT id, fts_content, heading FROM chunks ORDER BY id"
    )
    con.commit()
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
    con.commit()


def verify(con: sqlite3.Connection) -> bool:
    """抽样校验 rowid ↔ chunks.id 映射，错位会导致检索结果张冠李戴。"""
    rows = con.execute(
        "SELECT f.rowid, c.id, substr(c.content,1,40) "
        "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "ORDER BY f.rowid LIMIT 5"
    ).fetchall()
    ok = bool(rows) and all(r[0] == r[1] for r in rows)
    for r in rows:
        mark = "ok " if r[0] == r[1] else "BAD"
        print(f"  [{mark}] rowid={r[0]} chunk.id={r[1]}  {r[2]}…")
    return ok


def main(argv: list[str]) -> int:
    db_path = Path.home() / ".deskmate" / "history" / "scholar-shi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if argv:
        # 临时灌单本：作者/年代从映射表查，查不到则标注为未标注
        books = []
        for a in argv:
            p = Path(a).expanduser()
            author, era = DOWNLOADED_BOOKS.get(p.stem, (AUTHOR_XIAO, "当代"))
            books.append((p, p.stem, author, era))
    else:
        books = collect_books()

    print(f"目标库：{db_path}")
    print(f"待灌入 {len(books)} 部著作\n")

    con = init_db(db_path)
    t0 = time.time()
    total = 0
    try:
        for path, title, author, era in books:
            p = Path(path).expanduser()
            if not p.is_file():
                print(f"  [缺失] {p}")
                continue
            total += ingest(con, p, title, author, era)
        print("\n构建 FTS5 索引…")
        build_fts(con)
    finally:
        n_articles = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"\n灌库完成：{n_articles} 部著作 / {total} 个块 / {time.time() - t0:.1f}s")
        print("抽样校验 rowid ↔ chunks.id：")
        ok = verify(con)
        con.close()

    if db_path.exists():
        print(f"库体积：{db_path.stat().st_size / 1e6:.1f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
