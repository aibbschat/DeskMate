#!/usr/bin/env python3
"""从维基文库下载**公有领域**的近现代史学著作全文（纯文本）。

版权红线
--------
只下载作者去世满 50 年、已进入公有领域的作品：

* 孟森（1938 年去世）—— 公有领域
* 陈寅恪（1969 年去世）—— 公有领域
* 张荫麟（1942 年去世）—— 公有领域
* 吕思勉（1957 年去世）—— 公有领域

**仍在版权期、本脚本不会碰**：顾颉刚（1980 去世，2030 解禁）、
钱穆（1990 去世，2040 解禁）、黄仁宇（2000 去世，2050 解禁）。
维基文库本身也只收录公有领域文本，这是选它作为来源的主要原因。

用法
----
    python scripts/fetch_wikisource.py              # 下载默认清单
    python scripts/fetch_wikisource.py 清朝前紀       # 下载指定书目

产物：``~/.deskmate/history/sources/<书名>.txt``，供 build_scholar_index.py 灌库。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://zh.wikisource.org/w/api.php"
OUT_DIR = Path.home() / ".deskmate" / "history" / "sources"
UA = "OpenWorkerHistoryBot/1.0 (local personal research use)"

# 书名前缀 → (正式书名, 作者, 研究对象朝代)
DEFAULT_BOOKS: tuple[tuple[str, str, str, str], ...] = (
    ("清朝前紀", "清朝前纪", "孟森", "清"),
    ("唐代政治史述論稿", "唐代政治史述论稿", "陈寅恪", "唐"),
    ("殷周制度論", "殷周制度论", "王国维", "先秦"),
)

# 有些书在维基文库上不是「父页 + 子页」结构，而是几个**平级独立页面**。
# 陈寅恪《唐代政治史述论稿》就是如此：主页面只有一篇自序，三篇正文各自成页，
# 用 allpages 按书名前缀枚举只能捞到自序，必须显式补上。
EXTRA_PAGES: dict[str, tuple[str, ...]] = {
    "唐代政治史述论稿": (
        "統治階級之氏族及其升降",
        "政治革命及黨派分野",
        "外族盛衰之連環性及外患與内政之關係",
    ),
}

_WIKI_NOISE = re.compile(
    r"\{\{[^{}]*\}\}|<!--.*?-->|\[\[(?:文件|File|Image):[^\]]*\]\]|__[A-Z]+__",
    re.DOTALL,
)

# ProofreadPage 机制：正文页面往往只有一句
#   <pages index="XXX.pdf" from="8" to="12" />
# 真正的正文在 ``Page:XXX.pdf/8`` 这类页面里（扫描本逐页校对后的文本）。
# 直接抓主页面只会拿到目录，必须解析这个标签再去 Page: 命名空间取。
_PAGES_TAG = re.compile(r"<pages\b([^>]*?)/?>", re.DOTALL)

# 判断一页是不是真有内容：扫描本里常有空白页、纯页码页、插图页，
# 用「汉字个数」而不是「字符总数」衡量，避免把只有标点的页面当正文。
_CJK_COUNT = re.compile(r"[㐀-鿿豈-﫿]")


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def _get(params: dict[str, str], retries: int = 3) -> dict:
    """带重试的 API 请求——跨境访问维基文库 SSL 握手超时相当频繁。"""
    url = f"{API}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def fetch_wikitexts(titles: list[str]) -> dict[str, str]:
    """批量取 wikitext。逐页请求太慢（跨境约 6s/次），一次取 20 个。"""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 20):
        batch = titles[i:i + 20]
        data = _get({
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": "|".join(batch),
            "format": "json",
            "formatversion": "2",
        })
        for pg in data.get("query", {}).get("pages", []):
            if pg.get("missing"):
                continue
            revs = pg.get("revisions") or []
            if not revs:
                continue
            slot = revs[0].get("slots", {}).get("main", {})
            out[pg["title"]] = slot.get("content", "") or ""
        time.sleep(0.3)
    return out


def clean_page(wikitext: str) -> str:
    """清理 Page: 命名空间的 wikitext，只留正文。"""
    # <noinclude> 里是页眉页脚（如「清朝前紀　上編」），要去掉但别误删正文
    text = re.sub(r"<noinclude>.*?</noinclude>", "", wikitext, flags=re.DOTALL)
    text = re.sub(r"<pagequality[^>]*/>", "", text)
    text = re.sub(r"</?[^>]+>", "", text)  # 其余 HTML 标签
    text = _WIKI_NOISE.sub("", text)
    text = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", text)  # [[链接|文字]] → 文字
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def list_pages(prefix: str) -> list[str]:
    """枚举以 prefix 开头的所有页面（主页面 + 子页面）。"""
    titles: list[str] = []
    cont = None
    while True:
        p = {
            "action": "query",
            "list": "allpages",
            "apprefix": prefix,
            "aplimit": "200",
            "format": "json",
        }
        if cont:
            p["apcontinue"] = cont
        data = _get(p)
        titles.extend(pg["title"] for pg in data["query"]["allpages"])
        if "continue" in data:
            cont = data["continue"]["apcontinue"]
            time.sleep(0.3)
        else:
            break
    # 主页面排最前，子页面按名称排（「第一篇」等自然序靠零填充，维基通常已处理）
    return sorted(set(titles), key=lambda t: ("/" in t, t))


def expand_pages(wikitext: str) -> list[str]:
    """把 ``<pages index="X.pdf" from="8" to="12" />`` 展开成 Page: 标题列表。

    有些章节页面是纯手写正文（没有 <pages> 标签），此时返回空列表，
    由调用方直接清洗 wikitext 本身。
    """
    titles: list[str] = []
    for attrs in _PAGES_TAG.findall(wikitext):
        index = _attr(attrs, "index")
        if not index:
            continue
        start = _to_int(_attr(attrs, "from"))
        end = _to_int(_attr(attrs, "to")) or start
        if start is None:
            continue
        for n in range(start, end + 1):
            titles.append(f"Page:{index}/{n}")
    return titles


def _to_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def download(prefix: str, title: str, author: str) -> Path | None:
    print(f"\n=== {title}（{author}）===")
    pages = list_pages(prefix)
    for extra in EXTRA_PAGES.get(title, ()):
        if extra not in pages:
            pages.append(extra)
    if not pages:
        print("  [跳过] 维基文库无此书")
        return None
    print(f"  发现 {len(pages)} 个章节页面")

    # 1) 批量取所有章节页面的 wikitext，解析出真正承载正文的 Page: 标题
    chapter_wikitext = fetch_wikitexts(pages)
    plan: list[tuple[str, list[str]]] = []   # (章节名, Page: 标题列表)
    inline: list[tuple[str, str]] = []       # (章节名, 直接可用的正文)
    for pg in pages:
        raw = chapter_wikitext.get(pg, "")
        if not raw:
            print(f"  [空] {pg}")
            continue
        # 子页面取「书名/篇章名」的后半段；平级独立页面直接用自己的页面名；
        # 只有主页面（就是书名本身）才用书名做章节名。
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
    print(f"  需抓 {total_pages} 个 Page: 页面（另有 {len(inline)} 章为内联正文）")

    # 2) 批量抓 Page: 命名空间的正文——这是唯一能拿到扫描本校对文本的地方
    need: list[str] = [t for _, refs in plan for t in refs]
    page_text = fetch_wikitexts(need)
    missing = [t for t in need if t not in page_text]
    if missing:
        print(f"  [缺失] {len(missing)} 个 Page: 页面（如 {missing[0]}）")

    # 3) 按章节顺序拼接，清掉只有标点或页眉的废页
    parts: list[str] = []
    for heading, refs in plan:
        chunks: list[str] = []
        for t in refs:
            body = clean_page(page_text.get(t, ""))
            if len(_CJK_COUNT.findall(body)) >= 20:
                chunks.append(body)
        if chunks:
            parts.append(f"## {heading}\n\n" + "\n\n".join(chunks))
            print(f"  {heading:<30} {len(chunks):>3}/{len(refs):<3} 页  "
                  f"{sum(len(c) for c in chunks):>7} 字")
    for heading, body in inline:
        parts.append(f"## {heading}\n\n{body}")
        print(f"  {heading:<30} {'内联':>7}  {len(body):>7} 字")

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
        # 传书名时按 prefix 或 title 反查清单，这样才能带上 EXTRA_PAGES 等配置；
        # 查不到才当作全新 prefix 处理。
        books = []
        for a in argv:
            hit = next((b for b in DEFAULT_BOOKS if a in (b[0], b[1])), None)
            books.append(hit if hit else (a, a, "未标注", ""))
    else:
        books = list(DEFAULT_BOOKS)

    print(f"输出目录：{OUT_DIR}")
    print("版权提示：仅下载作者去世满 50 年的公有领域作品")
    saved = []
    for prefix, title, author, _dyn in books:
        p = download(prefix, title, author)
        if p:
            saved.append(p)
    print(f"\n完成：{len(saved)}/{len(books)} 部")
    for p in saved:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
