#!/usr/bin/env python3
"""从中华典藏网（diancang.xyz）抓取公有领域古籍全文。

专为维基文库缺失的唐/宋/明/清史料补档。古籍均为公有领域文本。

用法：
    python scripts/fetch_diancang.py <目录页URL> <书名> [作者]
"""
from __future__ import annotations

import re
import sys
import time
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OUT_DIR = Path.home() / ".deskmate" / "history" / "sources"

# 内置书单：(书名, 目录页URL, 作者)。中华典藏网公有领域古籍。
BOOKS: dict[str, tuple[str, str]] = {
    "宋刑统": ("https://www.diancang.xyz/lishizhuanji/12622/", "窦仪"),
    "甲申传信录": ("https://www.diancang.xyz/lishizhuanji/8036/", "钱士馨"),
    "明史纪事本末": ("https://www.diancang.xyz/lishizhuanji/mingshijishibenmo/", "谷应泰"),
    "梦溪笔谈": ("https://www.diancang.xyz/xueshuzaji/mengxibitan/", "沈括"),
    "建炎以来系年要录": ("https://www.diancang.xyz/lishizhuanji/jianyanyilaixinianyaolu/", "李心传"),
    "容斋随笔": ("https://www.diancang.xyz/xueshuzaji/rongzhaisuibi/", "洪迈"),
    "挥麈录": ("https://www.diancang.xyz/lishizhuanji/huizhulu/", "王明清"),
    "三朝北盟会编": ("https://www.diancang.xyz/lishizhuanji/sanchaobeimenghuibian/", "徐梦莘"),
    "东华录": ("https://www.diancang.xyz/lishizhuanji/donghualu/", "蒋良骐"),
    "新元史": ("https://www.diancang.xyz/lishizhuanji/xinyuanshi/", "柯劭忞"),
    "中国史纲": ("https://www.diancang.xyz/lishizhuanji/14100/", "张荫麟"),
    "两宋史纲": ("https://www.diancang.xyz/lishizhuanji/18058/", "张荫麟"),
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


def extract_chapters(toc_url: str, book_id: str) -> list[tuple[str, str]]:
    """从目录页提取 (章节标题, 章节URL)。book_id 支持数字或字母路径段。"""
    html = fetch(toc_url)
    chapters = []
    for m in re.finditer(r'href="([^"]*?/%s/(\d+)\.html)"[^>]*>([^<]{1,40})<' % re.escape(book_id), html):
        url, _id, title = m.group(1), m.group(2), m.group(3).strip()
        if not url.startswith("http"):
            url = "https://www.diancang.xyz" + url
        if url == toc_url:
            continue
        chapters.append((title, url))
    # 去重保序
    seen = set()
    out = []
    for t, u in chapters:
        if u not in seen:
            seen.add(u)
            out.append((t, u))
    return out


def clean_body(html: str, title: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)
    # 提取 p 标签段落（古籍正文，宋刑统/甲申传信录等）
    paras = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.DOTALL):
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if t and len(t) >= 2:
            paras.append(t)
    text = "\n".join(paras)
    # p 标签过少 → 回退全文文本提取（明史纪事本末等正文不在 p 标签内）
    if len(text) < 500:
        raw = re.sub(r"<[^>]+>", "\n", html)
        raw = raw.replace("&nbsp;", "").replace("&ldquo;", "“").replace("&rdquo;", "”")
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        # 跳过导航：从"作家"行或包含全角空格缩进的行开始
        start = 0
        for i, ln in enumerate(lines):
            if ln.endswith("作品集"):
                start = i + 1
                break
        text = "\n".join(lines[start:])
    # 过滤导航噪音
    noise = ("作品集", "字体设置", "首页", "分类", "学术杂记", "文学艺术", "佛学宝典",
             "天文地理", "玄学五术", "儒理哲学", "历史传记", "诗词戏曲", "类书文集",
             "国学知识", "外国名著", "专题", "人物", "书单", "风云榜", "留言")
    lines = [ln for ln in text.split("\n") if ln.strip() not in noise and len(ln.strip()) > 1]
    return "\n".join(lines).strip()


def main(argv: list[str]) -> int:
    if argv:
        targets = []
        for a in argv:
            if a in BOOKS:
                targets.append((a, BOOKS[a][0], BOOKS[a][1]))
            else:
                targets.append((a, a, ""))
    else:
        targets = [(t, u, a) for t, (u, a) in BOOKS.items()]

    for title, toc_url, author in targets:
        _grab_one(title, toc_url, author)
    return 0


def _grab_one(title: str, toc_url: str, author: str) -> int:
    # 从 URL 提取 book_id（最后一个路径段，支持数字或字母）
    m = re.search(r"/([^/]+)/?$", toc_url.rstrip("/"))
    if not m or m.group(1).startswith("lishizhuanji"):
        print("[错误] 无法从 URL 提取书籍 ID")
        return 1
    book_id = m.group(1)

    print(f"=== {title}（{author}）===")
    print(f"目录页：{toc_url}")
    chapters = extract_chapters(toc_url, book_id)
    print(f"发现 {len(chapters)} 章")

    parts = []
    for i, (ctitle, curl) in enumerate(chapters, 1):
        try:
            html = fetch(curl)
            body = clean_body(html, title)
            if body:
                parts.append(f"## {ctitle}\n\n{body}")
                print(f"  [{i}/{len(chapters)}] {ctitle}: {len(body):,} 字")
            else:
                print(f"  [{i}/{len(chapters)}] {ctitle}: 空")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(chapters)}] {ctitle}: 失败 {exc}")
        time.sleep(0.3)

    if not parts:
        print("[跳过] 无有效内容")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{title}.txt"
    out.write_text("\n\n".join(parts), encoding="utf-8")
    total = sum(len(p) for p in parts)
    print(f"→ {out.name}  {total:,} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
