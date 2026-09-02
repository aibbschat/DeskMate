#!/usr/bin/env python3
"""把萧哥本地收集的电子书（/Users/xiaocheng/Documents/文字/电子书）里**可用**的
历史类著作灌入本地史料库（FTS5 全文索引），建为独立的 ``collected-shi.db``。

为什么要独立建库
----------------
* 不动现有 ``duan-dai-shi`` / ``shibu-shi``（古籍原典）与 ``scholar-shi``（研究著作），
  降低误伤风险；新库由 ``_discover_experts`` 自动扫描目录纳入检索。
* 全量重建（几十 MB，秒级），重复运行幂等——以后萧哥往文件夹里加了新书，
  只要在下方 ``MANIFEST`` 里加一行，重跑本脚本即可。

显式白名单，绝不自动扫描整个文件夹
----------------------------------
文件夹里混着《水浒传》（演义小说）、地图集、图录等，自动扫描会把这些无关甚至
有害的内容一起灌进去。因此这里用**白名单**：只有列在 ``MANIFEST`` 里的文件才入库，
未列出的文件（含《水浒传》）一律不动。新增书必须显式加进清单。

era 分层（与全库一致，见 ``coworker/history/fts5.py`` 的 ``_era_of``）
---------------------------------------------------------------
* 宋人笔记/会要（东京梦华录、夷坚志、梦粱录、山家清供、西湖老人繁胜录）是**原典**
  （pre-modern primary），``category="笔记"`` 经 ``_ERA_BY_KEYWORD`` 归为 ``primary``，
  与原典层（正史/编年/政书/史部）一起参与「原典↔研究著作」分面交错，供交叉比对。
* 以后若加入近现代研究专著，``category="研究著作"`` 即归为 ``scholar``。

文本处理
--------
* ``.txt`` 优先按 UTF-8 读，失败或中文比例过低时回退 GB18030（梦粱录/山家清供是
  GBK 编码，直读会变乱码）。
* 部分 .txt 是从盗版/文库站扒下来的，开头带站点水印（"TXT小说天堂"、"
  首页史藏志存记录" 之类），``_cleanup`` 把这些水印行删掉，避免污染检索。

零外部依赖（复用 build_scholar_index 的建库逻辑）。
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_scholar_index import (  # noqa: E402
    init_db,
    split_chunks,
    build_fts,
    verify,
    tokenize_cjk,
)

COLLECTED_DIR = Path.home() / "Documents" / "文字" / "电子书"
DB_PATH = Path.home() / ".deskmate" / "history" / "collected-shi.db"
SOURCE = "collected"

# 白名单：(文件名, 正式书名, 作者, 成书年代, category)
# category="笔记" 经 fts5._ERA_BY_KEYWORD 归为 primary（原典）。
MANIFEST: tuple[tuple[str, str, str, str, str], ...] = (
    ("东京梦华录_UTF8.txt", "东京梦华录", "孟元老", "宋", "笔记"),
    ("夷坚志.txt", "夷坚志", "洪迈", "宋", "笔记"),
    ("梦粱录.txt", "梦粱录", "吴自牧", "宋", "笔记"),
    ("山家清供.txt", "山家清供", "林洪", "宋", "笔记"),
    ("西湖老人繁胜录.txt", "西湖老人繁胜录", "西湖老人", "宋", "笔记"),
)

# 扒站水印特征行：含这些串的行直接删掉（不会出现在真实史料正文里）。
_BANNER_PATTERNS = ("http", "小说天堂", "史藏志存记录", "最有文艺气息", "文学网站")


def _read_text(path: Path) -> str:
    """读 .txt：UTF-8 优先，中文比例过低回退 GB18030（梦粱录/山家清供系 GBK）。"""
    raw = path.read_bytes()
    best_text, best_ratio = "", -1.0
    for enc in ("utf-8", "gb18030"):
        try:
            t = raw.decode(enc)
        except UnicodeError:
            continue
        cjk = len(re.findall(r"[㐀-鿿豈-﫿]", t))
        ratio = cjk / max(1, len(t))
        if ratio > best_ratio:
            best_ratio, best_text = ratio, t
    return best_text


def _cleanup(text: str) -> str:
    """删掉扒站水印行，避免污染检索。"""
    lines = [
        ln for ln in text.split("\n")
        if not any(p in ln for p in _BANNER_PATTERNS)
    ]
    return "\n".join(lines)


def ingest(con, book_path: Path, title: str, author: str, dynasty: str, category: str) -> int:
    text = _cleanup(_read_text(book_path))
    if not text or not text.strip():
        print(f"  [跳过] 无正文/解码失败：{book_path.name}")
        return 0
    cjk = len(re.findall(r"[㐀-鿿豈-﫿]", text))
    if cjk < 500:
        print(f"  [跳过] 中文过少（{cjk} 字），疑似乱码：{book_path.name}")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = con.execute(
        "INSERT INTO articles (source, source_file, title, category, dynasty, "
        "author, content, content_hash, file_path, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOURCE, str(book_path), title, category, dynasty, author,
            text, hashlib.md5(text.encode("utf-8")).hexdigest(), str(book_path), now,
        ),
    )
    article_id = cur.lastrowid
    chunks = split_chunks(text)
    con.executemany(
        "INSERT INTO chunks (article_id, chunk_index, heading, content, fts_content) "
        "VALUES (?,?,?,?,?)",
        [(article_id, i, h, b, tokenize_cjk(b)) for i, (h, b) in enumerate(chunks)],
    )
    con.commit()
    print(f"  ✓ {title:<22} {author}/{dynasty:<3} {len(text):>9,} 字 / {cjk:>8,} 中文 → {len(chunks):>4} 块")
    return len(chunks)


def main() -> int:
    if not COLLECTED_DIR.is_dir():
        print(f"[错误] 找不到收集目录：{COLLECTED_DIR}")
        return 1
    print(f"源目录：{COLLECTED_DIR}")
    print(f"目标库：{DB_PATH}")
    print(f"白名单 {len(MANIFEST)} 部（其余文件一律不入库）\n")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = init_db(DB_PATH)
    t0 = time.time()
    total = 0
    try:
        for fname, title, author, dynasty, category in MANIFEST:
            p = COLLECTED_DIR / fname
            if not p.is_file():
                print(f"  [缺失] {fname}")
                continue
            total += ingest(con, p, title, author, dynasty, category)
        print("\n构建 FTS5 索引…")
        build_fts(con)
    finally:
        n = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"\n灌库完成：{n} 部 / {total} 块 / {time.time() - t0:.1f}s")
        print("抽样校验 rowid ↔ chunks.id：")
        ok = verify(con)
        con.close()
    if DB_PATH.exists():
        print(f"库体积：{DB_PATH.stat().st_size / 1e6:.1f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
