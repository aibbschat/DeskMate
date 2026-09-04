#!/usr/bin/env python3
"""把唐/宋/明/清史料书籍灌入本地史料库，建为独立的 ``shiliao-shi.db``。

这批书是**原典史料**（会要/实录/编年/政书/正史/别史/笔记/传记/类书/纪事本末/
史地），category 含对应关键字，经 ``fts5._ERA_BY_KEYWORD`` 归为 primary（原典），
与正史/编年/政书一起参与「原典↔研究著作」交叉比对。

为什么不并入现有库
------------------
* ``duan-dai-shi`` / ``shibu-shi`` 是服务器导出固化的古籍库，不动它们更安全。
* 独立建库，``_discover_experts`` 自动扫描目录即纳入检索；重跑幂等。
* 与 scholar（研究著作）/ collected（萧哥本地电子书）保持分工清晰。

元数据约定
----------
* category 用「会要/实录/编年/政书/正史/别史/笔记/传记/类书/纪事本末/史地」，
  全部经 _ERA_BY_KEYWORD 归 primary；「文总集」归 context（旁证，不作史实依据）。
* dynasty = 成书年代（作者所处时代）。
* source = "shiliao"，与古籍库同级可直引。

来源
----
* 维基文库下载的 .txt（scripts/fetch_wikisource_batch.py）
* 本地《宋会要辑稿.doc》提取（scripts/doc_extract_binary.py）

用法
----
    python scripts/build_shiliao_index.py
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

SOURCES_DIR = Path.home() / ".deskmate" / "history" / "sources"
DB_PATH = Path.home() / ".deskmate" / "history" / "shiliao-shi.db"
SOURCE = "shiliao"

# (txt文件名, 正式书名, 作者, 成书年代, category)
# category → era：会要/实录/编年/政书/正史/别史/笔记/传记/类书/纪事本末/史地 → primary
#             文总集 → context
MANIFEST: tuple[tuple[str, str, str, str, str], ...] = (
    # ---------------- 唐代 ----------------
    ("唐会要.txt", "唐会要", "王溥", "五代", "会要"),
    ("唐六典.txt", "唐六典", "李林甫等", "唐", "政书"),
    ("唐律疏议.txt", "唐律疏议", "长孙无忌等", "唐", "政书"),
    ("贞观政要.txt", "贞观政要", "吴兢", "唐", "政书"),
    ("大唐创业起居注.txt", "大唐创业起居注", "温大雅", "唐", "编年"),
    ("唐摭言.txt", "唐摭言", "王定保", "五代", "笔记"),
    ("唐国史补.txt", "唐国史补", "李肇", "唐", "笔记"),
    ("酉阳杂俎.txt", "酉阳杂俎", "段成式", "唐", "笔记"),
    ("大唐新语.txt", "大唐新语", "刘肃", "唐", "笔记"),
    ("隋唐嘉话.txt", "隋唐嘉话", "刘餗", "唐", "笔记"),
    ("因话录.txt", "因话录", "赵璘", "唐", "笔记"),
    ("封氏闻见记.txt", "封氏闻见记", "封演", "唐", "笔记"),
    ("朝野佥载.txt", "朝野佥载", "张鷟", "唐", "笔记"),
    ("开元天宝遗事.txt", "开元天宝遗事", "王仁裕", "五代", "笔记"),
    ("唐才子传.txt", "唐才子传", "辛文房", "元", "传记"),
    ("东观奏记.txt", "东观奏记", "裴庭裕", "唐", "笔记"),
    ("大业杂记.txt", "大业杂记", "杜宝", "隋", "笔记"),
    ("五代会要.txt", "五代会要", "王溥", "五代", "会要"),
    ("唐鉴.txt", "唐鉴", "范祖禹", "宋", "编年"),
    ("南唐书.txt", "南唐书", "马令", "宋", "别史"),
    # ---------------- 宋代 ----------------
    ("宋会要辑稿.txt", "宋会要辑稿", "徐松辑", "清", "会要"),
    ("宋刑统.txt", "宋刑统", "窦仪", "宋", "政书"),
    ("宋大诏令集.txt", "宋大诏令集", "宋绶子孙辑", "宋", "政书"),
    ("宋史全文.txt", "宋史全文", "不著撰人", "元", "编年"),
    ("续资治通鉴长编.txt", "续资治通鉴长编", "李焘", "宋", "编年"),
    ("建炎以来系年要录.txt", "建炎以来系年要录", "李心传", "宋", "编年"),
    ("建炎以来朝野杂记.txt", "建炎以来朝野杂记", "李心传", "宋", "笔记"),
    ("三朝北盟会编.txt", "三朝北盟会编", "徐梦莘", "宋", "编年"),
    ("东都事略.txt", "东都事略", "王称", "宋", "别史"),
    ("梦溪笔谈.txt", "梦溪笔谈", "沈括", "宋", "笔记"),
    ("涑水记闻.txt", "涑水记闻", "司马光", "宋", "笔记"),
    ("容斋随笔.txt", "容斋随笔", "洪迈", "宋", "笔记"),
    ("老学庵笔记.txt", "老学庵笔记", "陆游", "宋", "笔记"),
    ("鹤林玉露.txt", "鹤林玉露", "罗大经", "宋", "笔记"),
    ("邵氏闻见录.txt", "邵氏闻见录", "邵伯温", "宋", "笔记"),
    ("曲洧旧闻.txt", "曲洧旧闻", "朱弁", "宋", "笔记"),
    ("铁围山丛谈.txt", "铁围山丛谈", "蔡絛", "宋", "笔记"),
    ("挥麈录.txt", "挥麈录", "王明清", "宋", "笔记"),
    ("鸡肋编.txt", "鸡肋编", "庄绰", "宋", "笔记"),
    ("萍洲可谈.txt", "萍洲可谈", "朱彧", "宋", "笔记"),
    ("归田录.txt", "归田录", "欧阳修", "宋", "笔记"),
    ("渑水燕谈录.txt", "渑水燕谈录", "王辟之", "宋", "笔记"),
    ("隆平集.txt", "隆平集", "曾巩", "宋", "别史"),
    ("宋宰辅编年录.txt", "宋宰辅编年录", "徐自明", "宋", "编年"),
    ("皇宋通鉴长编纪事本末.txt", "皇宋通鉴长编纪事本末", "杨仲良", "宋", "纪事本末"),
    ("太平广记.txt", "太平广记", "李昉等", "宋", "类书"),
    # ---------------- 明代 ----------------
    ("明实录.txt", "明实录", "官修", "明", "实录"),
    ("明史纪事本末.txt", "明史纪事本末", "谷应泰", "清", "纪事本末"),
    ("万历野获编.txt", "万历野获编", "沈德符", "明", "笔记"),
    ("明通鉴.txt", "明通鉴", "夏燮", "清", "编年"),
    ("国榷.txt", "国榷", "谈迁", "明", "编年"),
    ("罪惟录.txt", "罪惟录", "查继佐", "清", "别史"),
    ("甲申传信录.txt", "甲申传信录", "钱士馨", "明", "笔记"),
    ("明季北略.txt", "明季北略", "计六奇", "清", "笔记"),
    ("明季南略.txt", "明季南略", "计六奇", "清", "笔记"),
    ("南明野史.txt", "南明野史", "三余氏", "清", "别史"),
    ("三垣笔记.txt", "三垣笔记", "李清", "明", "笔记"),
    ("烈皇小识.txt", "烈皇小识", "文秉", "清", "笔记"),
    ("明夷待访录.txt", "明夷待访录", "黄宗羲", "清", "政书"),
    ("玉堂丛语.txt", "玉堂丛语", "焦竑", "明", "笔记"),
    ("双槐岁钞.txt", "双槐岁钞", "黄瑜", "明", "笔记"),
    ("水东日记.txt", "水东日记", "叶盛", "明", "笔记"),
    ("菽园杂记.txt", "菽园杂记", "陆容", "明", "笔记"),
    ("名山藏.txt", "名山藏", "何乔远", "明", "别史"),
    ("皇明经世文编.txt", "皇明经世文编", "陈子龙等", "明", "文总集"),
    # ---------------- 清代 ----------------
    ("东华录.txt", "东华录", "蒋良骐", "清", "编年"),
    ("清实录.txt", "清实录", "官修", "清", "实录"),
    ("清稗类钞.txt", "清稗类钞", "徐珂", "民国", "笔记"),
    ("圣武记.txt", "圣武记", "魏源", "清", "纪事本末"),
    ("啸亭杂录.txt", "啸亭杂录", "昭梿", "清", "笔记"),
    ("清会典.txt", "清会典", "官修", "清", "政书"),
    ("清朝通典.txt", "清朝通典", "官修", "清", "政书"),
    ("皇朝经世文编.txt", "皇朝经世文编", "贺长龄", "清", "文总集"),
    ("竹叶亭杂记.txt", "竹叶亭杂记", "姚元之", "清", "笔记"),
    ("郎潜纪闻.txt", "郎潜纪闻", "陈康祺", "清", "笔记"),
    ("巢林笔谈.txt", "巢林笔谈", "龚炜", "清", "笔记"),
    ("池北偶谈.txt", "池北偶谈", "王士禛", "清", "笔记"),
    ("听雨丛谈.txt", "听雨丛谈", "福格", "清", "笔记"),
    ("冷庐杂识.txt", "冷庐杂识", "陆以湉", "清", "笔记"),
    ("扬州画舫录.txt", "扬州画舫录", "李斗", "清", "笔记"),
    ("履园丛话.txt", "履园丛话", "钱泳", "清", "笔记"),
    ("海国图志.txt", "海国图志", "魏源", "清", "史地"),
    ("瀛寰志略.txt", "瀛寰志略", "徐继畬", "清", "史地"),
    ("蒙古游牧记.txt", "蒙古游牧记", "张穆", "清", "史地"),
    ("清史纪事本末.txt", "清史纪事本末", "黄鸿寿", "民国", "纪事本末"),
    # ---------------- 综合/断代补全 ----------------
    ("通志.txt", "通志", "郑樵", "宋", "政书"),
    ("续通典.txt", "续通典", "官修", "清", "政书"),
    ("东观汉记.txt", "东观汉记", "班固等", "东汉", "正史"),
    ("新元史.txt", "新元史", "柯劭忞", "民国", "正史"),
)


def _read_text(path: Path) -> str:
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


def ingest(con, book_path: Path, title: str, author: str, dynasty: str, category: str) -> int:
    text = _read_text(book_path)
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
    print(f"  ✓ {title:<16} {author}/{dynasty:<3} {len(text):>9,} 字 / {cjk:>8,} 中文 → {len(chunks):>4} 块 [{category}]")
    return len(chunks)


def main() -> int:
    if not SOURCES_DIR.is_dir():
        print(f"[错误] 找不到源目录：{SOURCES_DIR}")
        return 1
    print(f"源目录：{SOURCES_DIR}")
    print(f"目标库：{DB_PATH}")
    print(f"清单 {len(MANIFEST)} 部（其余文件一律不入库）\n")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = init_db(DB_PATH)
    t0 = time.time()
    total = 0
    present, missing = [], []
    try:
        for fname, title, author, dynasty, category in MANIFEST:
            p = SOURCES_DIR / fname
            if not p.is_file():
                missing.append((fname, title))
                print(f"  [缺失] {fname}")
                continue
            present.append(title)
            total += ingest(con, p, title, author, dynasty, category)
        print("\n构建 FTS5 索引…")
        build_fts(con)
    finally:
        n = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"\n灌库完成：{n} 部 / {total} 块 / {time.time() - t0:.1f}s")
        print("抽样校验 rowid ↔ chunks.id：")
        ok = verify(con)
        con.close()
    if missing:
        print(f"\n⚠ 缺失 {len(missing)} 部（尚未下载到 sources/）：")
        for fname, title in missing:
            print(f"    {title}（{fname}）")
    if DB_PATH.exists():
        print(f"库体积：{DB_PATH.stat().st_size / 1e6:.1f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
