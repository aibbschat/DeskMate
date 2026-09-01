#!/usr/bin/env python3
"""为史料库重建 FTS5 全文索引（本地 Mac 侧执行）。

背景
----
百家学院原始史料库体积巨大（``duan-dai-shi`` 1.7G、``dong-shi-gong`` 6.4G），
其中约 80% 是 FTS5 倒排索引（单字切分导致 postings 极度膨胀）。生产服务器只有
约 1G 可用内存，重建索引会 OOM；而本机到服务器的实测带宽只有约 0.55 MB/s。

因此采用「服务器导出裸表 → 本地重建索引」的两段式：

* 服务器端只导出 ``articles`` + ``chunks`` 两张表，且剥掉两处冗余列
  （``articles.content`` 与 chunks 重复、``chunks.fts_content`` 可由 content 推导），
  传输量从 1.7G 降到约 300M。
* 索引在本机重建——Mac 的 CPU/内存远强于服务器，几分钟即可完成。

用法
----
    python scripts/build_history_index.py ~/.deskmate/history/duan-dai-shi.db
    python scripts/build_history_index.py ~/.deskmate/history/*.db

索引约定
--------
FTS5 用 ``tokenize='unicode61'`` 按空格切分，因此喂进去的文本必须是**字间插
空格**的预处理文本。灌库侧用 JS 的 ``tokenizeCJK`` 做这一步，本脚本用等价的
Python 实现（字符范围与替换规则逐条对齐），保证行为一致
（参见 ``ai-experts-platform/backend/shared/fts5.js``）。

``rowid`` 必须等于 ``chunks.id``——``coworker/history/fts5.py`` 的检索 SQL 依赖
``JOIN chunks c ON c.id = f.rowid``，错位会导致结果张冠李戴。脚本末尾会抽样
校验这一映射。

零外部依赖，只用标准库。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

# 与服务器生产库保持一致的索引定义（普通模式，非 external-content）
FTS_DDL = "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61')"

# CJK 范围逐条对齐 fts5.js：一-鿿(4E00-9FFF) 㐀-䶿(3400-4DBF) 豈-﫿(F900-FAFF)
_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
_MULTI_SPACE_RE = re.compile(r"\s+")


def tokenize_cjk(text: str) -> str:
    """在 CJK 字符间插空格（``fts5.js`` 的 ``tokenizeCJK`` 等价实现）。"""
    return _MULTI_SPACE_RE.sub(" ", _CJK_RE.sub(r" \1 ", text)).strip()


def build_index(db_path: Path) -> bool:
    if not db_path.is_file():
        print(f"  [跳过] 文件不存在：{db_path}")
        return False

    print(f"\n=== {db_path.name} ===")
    con = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chunks" not in tables or "articles" not in tables:
            print("  [跳过] 缺少 articles/chunks 表，不是有效的史料裸库")
            return False

        n_articles = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        print(f"  articles={n_articles}  chunks={n_chunks}")
        if n_chunks == 0:
            print("  [跳过] chunks 为空")
            return False

        if "chunks_fts" in tables:
            print("  已存在 chunks_fts，先删除（幂等重建）")
            con.execute("DROP TABLE chunks_fts")
            con.commit()

        con.execute(FTS_DDL)
        con.commit()
        print("  建表完成")

        # 流式读取 + 分批写入，避免一次性把 14 万行全装进内存
        t0 = time.time()
        src = con.execute("SELECT id, heading, content FROM chunks")
        batch: list[tuple[int, str, str]] = []
        inserted = 0
        for cid, heading, content in src:
            # fts_content 若已存在则沿用，否则本地生成（与灌库侧 tokenizeCJK 等价）
            batch.append((cid, tokenize_cjk(content or ""), heading or ""))
            if len(batch) >= 5000:
                con.executemany(
                    "INSERT INTO chunks_fts(rowid, content, heading) VALUES (?,?,?)", batch
                )
                inserted += len(batch)
                batch.clear()
                print(f"    已索引 {inserted}/{n_chunks}", end="\r", flush=True)
        if batch:
            con.executemany("INSERT INTO chunks_fts(rowid, content, heading) VALUES (?,?,?)", batch)
            inserted += len(batch)
        con.commit()
        print(f"\n  灌索引完成：{inserted} 行 ({time.time() - t0:.1f}s)")

        t0 = time.time()
        con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        con.commit()
        print(f"  optimize 完成 ({time.time() - t0:.1f}s)")

        n_idx = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        print(f"  索引行数 = {n_idx}（应等于 chunks={n_chunks}）")
        if n_idx != n_chunks:
            print("  [错误] 索引行数与 chunks 不一致！")
            return False

        # 抽样校验：确认 rowid↔chunks.id 映射成立，且真能检索到内容
        for word in ("项 羽", "明 太 祖"):
            rows = con.execute(
                "SELECT a.title, substr(c.content, 1, 40) FROM chunks_fts f "
                "JOIN chunks c ON c.id = f.rowid JOIN articles a ON a.id = c.article_id "
                "WHERE chunks_fts MATCH ? LIMIT 1",
                (f'"{word}"',),
            ).fetchall()
            if rows:
                title, snippet = rows[0]
                print(f'  [校验] "{word}" → 《{title}》 {snippet[:36]}…')
            else:
                print(f'  [校验] "{word}" 无命中')

        size_mb = db_path.stat().st_size / 1048576
        print(f"  最终文件大小 = {size_mb:.0f} MB")
        return True
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    ok = True
    for arg in argv[1:]:
        if not build_index(Path(arg).expanduser()):
            ok = False
    print("\n全部完成" if ok else "\n有库未成功，见上方日志")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
