#!/usr/bin/env python3
"""批量从维基文库下载唐/宋/明/清史料书籍（公有领域古籍），供灌入史料库。

对每部书，先枚举页面，智能选择「主版本 vs 四庫全書本」：
* 若存在主版本卷目录（如「唐會要/卷001」）且内容足够 → 用主版本
* 否则退回四庫全書本（「唐會要 (四庫全書本)/全覽N」）
避免同书两份重复入库。

用法：
    python scripts/fetch_wikisource_batch.py [书名前缀...]   # 默认下载全部 BOOKS

产物：~/.deskmate/history/sources/<书名>.txt
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_wikisource import (
    _CJK_COUNT,
    OUT_DIR,
    clean_page,
    download,
    expand_pages,
    fetch_wikitexts,
    list_pages,
)

# (维基文库书名前缀, 正式书名, 作者, 成书年代, category)
# category 会进入 articles.category，用于 era 分层：
#   会要/实录/编年/政书/正史/笔记/别史 → primary（原典）
#   文总集/文评 → context；研究著作 → scholar
BOOKS: tuple[tuple[str, str, str, str, str], ...] = (
    # ---------------- 唐代 ----------------
    ("唐會要", "唐会要", "王溥", "五代", "会要"),
    ("唐六典", "唐六典", "李林甫等", "唐", "政书"),
    ("唐律疏議", "唐律疏议", "长孙无忌等", "唐", "政书"),
    ("貞觀政要", "贞观政要", "吴兢", "唐", "政书"),
    ("大唐創業起居注", "大唐创业起居注", "温大雅", "唐", "编年"),
    ("唐摭言", "唐摭言", "王定保", "五代", "笔记"),
    ("唐國史補", "唐国史补", "李肇", "唐", "笔记"),
    ("酉陽雜俎", "酉阳杂俎", "段成式", "唐", "笔记"),
    ("大唐新語", "大唐新语", "刘肃", "唐", "笔记"),
    ("隋唐嘉話", "隋唐嘉话", "刘餗", "唐", "笔记"),
    ("因話錄", "因话录", "赵璘", "唐", "笔记"),
    ("封氏聞見記", "封氏闻见记", "封演", "唐", "笔记"),
    ("朝野僉載", "朝野佥载", "张鷟", "唐", "笔记"),
    ("開元天寶遺事", "开元天宝遗事", "王仁裕", "五代", "笔记"),
    ("唐才子傳", "唐才子传", "辛文房", "元", "传记"),
    ("東觀奏記", "东观奏记", "裴庭裕", "唐", "笔记"),
    ("大業雜記", "大业杂记", "杜宝", "隋", "笔记"),
    ("五代會要", "五代会要", "王溥", "五代", "会要"),
    ("唐鑑", "唐鉴", "范祖禹", "宋", "编年"),
    ("南唐書", "南唐书", "马令", "宋", "别史"),
    # ---------------- 宋代 ----------------
    ("宋會要輯稿", "宋会要辑稿", "徐松辑", "清", "会要"),
    ("續資治通鑑長編", "续资治通鉴长编", "李焘", "宋", "编年"),
    ("建炎以來繫年要錄", "建炎以来系年要录", "李心传", "宋", "编年"),
    ("建炎以來朝野雜記", "建炎以来朝野杂记", "李心传", "宋", "笔记"),
    ("三朝北盟會編", "三朝北盟会编", "徐梦莘", "宋", "编年"),
    ("東都事略", "东都事略", "王称", "宋", "别史"),
    ("夢溪筆談", "梦溪笔谈", "沈括", "宋", "笔记"),
    ("涑水記聞", "涑水记闻", "司马光", "宋", "笔记"),
    ("容齋隨筆", "容斋随笔", "洪迈", "宋", "笔记"),
    ("老學庵筆記", "老学庵笔记", "陆游", "宋", "笔记"),
    ("鶴林玉露", "鹤林玉露", "罗大经", "宋", "笔记"),
    ("邵氏聞見錄", "邵氏闻见录", "邵伯温", "宋", "笔记"),
    ("曲洧舊聞", "曲洧旧闻", "朱弁", "宋", "笔记"),
    ("鐵圍山叢談", "铁围山丛谈", "蔡絛", "宋", "笔记"),
    ("揮麈錄", "挥麈录", "王明清", "宋", "笔记"),
    ("雞肋編", "鸡肋编", "庄绰", "宋", "笔记"),
    ("萍洲可談", "萍洲可谈", "朱彧", "宋", "笔记"),
    ("歸田錄", "归田录", "欧阳修", "宋", "笔记"),
    ("澠水燕談錄", "渑水燕谈录", "王辟之", "宋", "笔记"),
    ("隆平集", "隆平集", "曾巩", "宋", "别史"),
    ("宋宰輔編年錄", "宋宰辅编年录", "徐自明", "宋", "编年"),
    ("皇宋通鑑長編紀事本末", "皇宋通鉴长编纪事本末", "杨仲良", "宋", "纪事本末"),
    ("太平廣記", "太平广记", "李昉等", "宋", "类书"),
    # ---------------- 明代 ----------------
    ("明實錄", "明实录", "官修", "明", "实录"),
    ("明史紀事本末", "明史纪事本末", "谷应泰", "清", "纪事本末"),
    ("萬曆野獲編", "万历野获编", "沈德符", "明", "笔记"),
    ("明通鑑", "明通鉴", "夏燮", "清", "编年"),
    ("國榷", "国榷", "谈迁", "明", "编年"),
    ("罪惟錄", "罪惟录", "查继佐", "清", "别史"),
    ("明季北略", "明季北略", "计六奇", "清", "笔记"),
    ("明季南略", "明季南略", "计六奇", "清", "笔记"),
    ("南明野史", "南明野史", "三余氏", "清", "别史"),
    ("三垣筆記", "三垣笔记", "李清", "明", "笔记"),
    ("烈皇小識", "烈皇小识", "文秉", "清", "笔记"),
    ("明夷待訪錄", "明夷待访录", "黄宗羲", "清", "政书"),
    ("玉堂叢語", "玉堂丛语", "焦竑", "明", "笔记"),
    ("雙槐歲鈔", "双槐岁钞", "黄瑜", "明", "笔记"),
    ("水東日記", "水东日记", "叶盛", "明", "笔记"),
    ("菽園雜記", "菽园杂记", "陆容", "明", "笔记"),
    ("名山藏", "名山藏", "何乔远", "明", "别史"),
    ("皇明經世文編", "皇明经世文编", "陈子龙等", "明", "文总集"),
    # ---------------- 清代 ----------------
    ("東華錄", "东华录", "蒋良骐", "清", "编年"),
    ("清實錄", "清实录", "官修", "清", "实录"),
    ("清稗類鈔", "清稗类钞", "徐珂", "民国", "笔记"),
    ("聖武記", "圣武记", "魏源", "清", "纪事本末"),
    ("嘯亭雜錄", "啸亭杂录", "昭梿", "清", "笔记"),
    ("清會典", "清会典", "官修", "清", "政书"),
    ("清朝通典", "清朝通典", "官修", "清", "政书"),
    ("皇朝經世文編", "皇朝经世文编", "贺长龄", "清", "文总集"),
    ("竹葉亭雜記", "竹叶亭杂记", "姚元之", "清", "笔记"),
    ("郎潛紀聞", "郎潜纪闻", "陈康祺", "清", "笔记"),
    ("巢林筆談", "巢林笔谈", "龚炜", "清", "笔记"),
    ("池北偶談", "池北偶谈", "王士禛", "清", "笔记"),
    ("聽雨叢談", "听雨丛谈", "福格", "清", "笔记"),
    ("冷廬雜識", "冷庐杂识", "陆以湉", "清", "笔记"),
    ("揚州畫舫錄", "扬州画舫录", "李斗", "清", "笔记"),
    ("履園叢話", "履园丛话", "钱泳", "清", "笔记"),
    ("海國圖志", "海国图志", "魏源", "清", "史地"),
    ("瀛寰志略", "瀛寰志略", "徐继畬", "清", "史地"),
    ("蒙古游牧記", "蒙古游牧记", "张穆", "清", "史地"),
    ("清史紀事本末", "清史纪事本末", "黄鸿寿", "民国", "纪事本末"),
    # ---------------- 综合/断代补全 ----------------
    ("通志", "通志", "郑樵", "宋", "政书"),
    ("續通典", "续通典", "官修", "清", "政书"),
    ("東觀漢記", "东观汉记", "班固等", "东汉", "正史"),
    ("新元史", "新元史", "柯劭忞", "民国", "正史"),
)

# 排除的四库全书本——仅当主版本内容不足时降级使用
_SKQ_PAT = re.compile(r"\(四庫全書本\)|\(四庫全書[^)]*\)")


def _split_versions(pages: list[str]) -> tuple[list[str], list[str]]:
    """把页面分为主版本与四库全书本。"""
    main_v, skq_v = [], []
    for p in pages:
        if _SKQ_PAT.search(p):
            skq_v.append(p)
        else:
            main_v.append(p)
    return main_v, skq_v


def download_best(prefix: str, title: str, author: str) -> Path | None:
    """智能版本选择下载。优先主版本，主版本内容不足时退回四库全书本。"""
    print(f"\n=== {title}（{author}）===")
    pages = list_pages(prefix)
    if not pages:
        print("  [跳过] 维基文库无此书")
        return None
    main_v, skq_v = _split_versions(pages)
    # 统计主版本中含"卷"的正文页数（真章节页）
    main_vol_pages = [p for p in main_v if re.search(r"/卷\d", p)]
    print(f"  主版本 {len(main_v)} 页（含卷正文 {len(main_vol_pages)}），"
          f"四库本 {len(skq_v)} 页")

    # 主版本有正文卷页 → 用主版本；否则退回四库本
    use_pages = main_v if len(main_vol_pages) >= 2 or len(main_v) >= 3 else skq_v or main_v
    label = "主版本" if use_pages is main_v else ("四库全书本" if use_pages is skq_v else "主版本")
    print(f"  选用：{label}（{len(use_pages)} 页）")

    # 直接调用 download（它内部会再次 list_pages，这里我们改用一个
    # 显式页面列表的下载实现——复用 download 但传 prefix 会被它重新枚举。
    # 因此这里临时把选定的页面作为 EXTRA 处理：直接构造一个轻量下载。
    return _download_with_pages(use_pages, prefix, title, author)


def _download_with_pages(pages: list[str], prefix: str, title: str, author: str) -> Path | None:
    """基于已枚举的页面列表下载（复用 fetch_wikisource 的清洗逻辑）。"""
    chapter_wikitext = fetch_wikitexts(pages)
    plan: list[tuple[str, list[str]]] = []
    inline: list[tuple[str, str]] = []
    for pg in pages:
        raw = chapter_wikitext.get(pg, "")
        if not raw:
            continue
        if "/" in pg:
            heading = pg.split("/", 1)[1]
        elif pg in (prefix, title):
            heading = title
        else:
            heading = pg
        heading = heading.replace("_", " ").strip() or title
        refs = expand_pages(raw)
        if refs:
            plan.append((heading, refs))
        else:
            body = clean_page(raw)
            if len(body) >= 40:
                inline.append((heading, body))

    total_pages = sum(len(r) for _, r in plan)
    print(f"  需抓 {total_pages} 个 Page: 页面（另有 {len(inline)} 章内联正文）")

    need: list[str] = [t for _, refs in plan for t in refs]
    page_text = {}
    if need:
        page_text = fetch_wikitexts(need)

    parts: list[str] = []
    for heading, refs in plan:
        chunks = []
        for t in refs:
            body = clean_page(page_text.get(t, ""))
            if len(_CJK_COUNT.findall(body)) >= 20:
                chunks.append(body)
        if chunks:
            parts.append(f"## {heading}\n\n" + "\n\n".join(chunks))
    for heading, body in inline:
        parts.append(f"## {heading}\n\n{body}")

    if not parts:
        print("  [跳过] 无有效内容")
        return None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{title}.txt"
    out.write_text("\n\n".join(parts), encoding="utf-8")
    total = sum(len(p) for p in parts)
    print(f"  → {out.name}  {total:,} 字")
    return out


def main(argv: list[str]) -> int:
    if argv:
        # 按前缀过滤 BOOKS（支持用书名或前缀子串匹配）
        books = [b for b in BOOKS if any(a in b[0] or a in b[1] for a in argv)]
        if not books:
            # 当作全新书名前缀处理
            for a in argv:
                books.append((a, a, "未标注", "?", "史书"))
    else:
        books = list(BOOKS)

    print(f"输出目录：{OUT_DIR}")
    print(f"待下载：{len(books)} 部\n")
    saved, failed, skipped = [], [], []
    for i, (prefix, title, author, _era, _cat) in enumerate(books, 1):
        try:
            p = download_best(prefix, title, author)
            (saved if p else skipped).append(title)
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {title}: {type(exc).__name__} {exc}")
            failed.append(title)
        print(f"  进度 {i}/{len(books)}")
    print(f"\n完成：成功 {len(saved)} / 跳过 {len(skipped)} / 失败 {len(failed)}")
    for t in saved:
        print(f"  ✓ {t}")
    for t in skipped:
        print(f"  - {t}")
    for t in failed:
        print(f"  ✗ {t}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
