#!/usr/bin/env python3
"""从国学典籍网（ab.newdu.com）抓取公有领域古籍全文。

支持两种结构：
* 目录模式：目录页列出所有分卷链接（如《宋史全文》b839.html）
* 链表模式：从起始页沿「下一篇」遍历（如《宋大诏令集》s108733.html）

古籍均为公有领域文本。
用法：
    python scripts/fetch_newdu.py 宋史全文 宋大诏令集
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OUT_DIR = Path.home() / ".deskmate" / "history" / "sources"
BASE = "https://ab.newdu.com"

# 书名 → (目录页URL, 作者, 模式)
BOOKS: dict[str, tuple[str, str, str]] = {
    "宋史全文": ("https://ab.newdu.com/book/b839.html", "不著撰人", "catalog"),
    "宋大诏令集": ("https://ab.newdu.com/book/s108733.html", "宋绶子孙辑", "chain"),
    "续资治通鉴长编": ("https://ab.newdu.com/book/book.php?bid=842", "李焘", "catalog"),
    "明通鉴": ("https://ab.newdu.com/book/ms121611.html", "夏燮", "chain"),
    "太平广记": ("https://ab.newdu.com/book/mb248.html", "李昉等", "catalog"),
}


def fetch(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def clean_body(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)
    # 找正文起始：书名或典型正文标记
    idx = html.find("正文")
    body_html = html
    if idx > 0:
        # 找正文区（通常正文在目录导航之后）
        m = re.search(r'正文</[^>]+>(.*)', html, re.DOTALL)
        if m:
            body_html = m.group(1)
    text = re.sub(r"<[^>]+>", "\n", body_html)
    text = text.replace("&nbsp;", "").replace("&ldquo;", "“").replace("&rdquo;", "”")
    text = text.replace("&middot;", "·").replace("&amp;", "&")
    lines = [ln.strip() for ln in text.split("\n")]
    # 过滤导航噪音
    noise = ("首页", "书库", "编年类", "正文", "上一篇", "下一篇", "返回目录",
             "经部", "史部", "子部", "集部", "道藏", "佛藏", "小说", "简体", "繁体")
    out = []
    for ln in lines:
        if not ln:
            continue
        if any(ln == n for n in noise):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def grab_catalog(toc_url: str, title: str, author: str) -> int:
    """目录模式：目录页列出全部分卷链接。"""
    html = fetch(toc_url)
    links = re.findall(r'href="(/book/(?:s|ms)\d+\.html)"[^>]*>([^<]{2,30})<', html)
    chapters = [(t.strip(), BASE + u) for u, t in links]
    # 去重：保留含书名的卷页
    chapters = [c for c in chapters if any(k in c[0] for k in ("卷", "提要", "序"))]
    seen, uniq = set(), []
    for t, u in chapters:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    print(f"发现 {len(uniq)} 章")
    parts = []
    for i, (ct, cu) in enumerate(uniq, 1):
        try:
            h = fetch(cu)
            body = clean_body(h)
            if body:
                parts.append(f"## {ct}\n\n{body}")
                print(f"  [{i}/{len(uniq)}] {ct}: {len(body):,} 字", flush=True)
            else:
                print(f"  [{i}/{len(uniq)}] {ct}: 空", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(uniq)}] {ct}: 失败 {exc}", flush=True)
        time.sleep(0.3)
    return _save(parts, title, author)


def grab_chain(start_url: str, title: str, author: str, max_pages: int = 400) -> int:
    """链表模式：沿「下一篇」遍历。"""
    parts = []
    url = start_url
    seen_urls = set()
    count = 0
    while url and count < max_pages:
        if url in seen_urls:
            break
        seen_urls.add(url)
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {url}: {exc}")
            break
        # 标题
        mt = re.search(r"<title>([^<]+)</title>", html)
        ct = mt.group(1).strip() if mt else f"第{count+1}页"
        body = clean_body(html)
        if body:
            parts.append(f"## {ct}\n\n{body}")
        count += 1
        print(f"  [{count}] {ct}: {len(body):,} 字", flush=True)
        # 找下一篇链接
        m = re.search(r'href="(/book/(?:s|ms)\d+\.html)"[^>]*>下一篇', html)
        if not m:
            m = re.search(r'下一篇[^<]*<[^>]*href="([^"]+)"', html)
        if m:
            nxt = m.group(1)
            url = nxt if nxt.startswith("http") else BASE + nxt
        else:
            break
        time.sleep(0.3)
    return _save(parts, title, author)


def _save(parts: list[str], title: str, author: str) -> int:
    if not parts:
        print("[跳过] 无有效内容")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{title}.txt"
    out.write_text("\n\n".join(parts), encoding="utf-8")
    total = sum(len(p) for p in parts)
    print(f"→ {out.name}  {total:,} 字")
    return 0


def main(argv: list[str]) -> int:
    targets = argv or list(BOOKS.keys())
    for t in targets:
        if t not in BOOKS:
            print(f"[跳过] 未知书名：{t}")
            continue
        toc, author, mode = BOOKS[t]
        print(f"=== {t}（{author}）===")
        if mode == "catalog":
            grab_catalog(toc, t, author)
        else:
            grab_chain(toc, t, author)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
