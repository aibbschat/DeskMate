#!/usr/bin/env python3
"""从本地《宋会要辑稿.doc》（OLE2 老 Word）提取并清理正文。

该 .doc 是 Word 97-2003 二进制格式，FIB 布局异常（无法用标准 piece table
解析），但正文以 UTF-16LE 连续存储在 WordDocument 流中。本脚本：
1. 按 UTF-16LE 解码整个 WordDocument 流
2. 用特征定位正文起点（首个「宋會要輯稿」）
3. 用 CJK+标点密度滑窗找正文结束边界（跳过尾部二进制噪音）
4. 清理格式噪音：页码标记、`檕` 分段符、导航行、残留控制符

用法：
    python scripts/doc_extract_binary.py   # 提取宋会要辑稿
"""
from __future__ import annotations

import re
from pathlib import Path

import olefile

SRC = Path.home() / "Documents" / "文字" / "电子书" / "宋会要辑稿.doc"
OUT = Path.home() / ".deskmate" / "history" / "sources" / "宋会要辑稿.txt"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_PUNCT = re.compile(r"[。，、；：「」『』（）《》〈〉？！…—·]")
_PAGE = re.compile(r"\s*-\s*\d+\s*-\s*")
_NAV = re.compile(r"^/.*$", re.MULTILINE)


def find_end(body: str, mark: int) -> int:
    """从最后卷首标记向后找正文结束（CJK>100 且标点>=2 视为正文）。"""
    end = mark
    i = mark
    while i < len(body):
        seg = body[i:i + 200]
        if not seg:
            break
        if len(_CJK.findall(seg)) > 100 and len(_PUNCT.findall(seg)) >= 2:
            end = i + 200
            i += 200
        else:
            break
    return end


_CJK2 = re.compile(r"[\u4e00-\u9fff]")


def _is_garbage_char(c: str) -> bool:
    """乱码特征字符：私有区 / 补充私有区 / 替换符 / 东南亚文字 / 通用符号区。
    真实中文古籍正文不会成片出现这些码位。"""
    cp = ord(c)
    if 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or cp == 0xFFFD:
        return True
    if 0x0780 <= cp <= 0x1FFF:
        return True
    if 0x2000 <= cp <= 0x2BFF:
        return True
    return False


def _remove_garbage(text: str) -> str:
    """剔除 WordDocument 流中混入的格式二进制数据（UTF-16 误解码为乱码）。
    这类数据成片出现私有区/东南亚字符/「汉字+拉丁字母」交替模式。
    只剔除长度>5000 的乱码大段，保留正文中的生僻字段（宋会要辑稿含反切注音）。"""
    WIN = 600
    n = len(text)
    bad = []
    i = 0
    while i < n:
        seg = text[i:i + WIN]
        g = sum(1 for c in seg if _is_garbage_char(c))
        alt = len(re.findall(r"[\u4e00-\u9fff][A-Za-z0-9]", seg))
        if g > 40 or alt > 50:
            bad.append((i, i + WIN))
        i += WIN
    merged = []
    for s, e in bad:
        if merged and s <= merged[-1][1] + WIN:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append([s, e])
    parts = []
    prev = 0
    for s, e in merged:
        if e - s > 5000:  # 只剔除乱码大段
            parts.append(text[prev:s])
            prev = e
    parts.append(text[prev:])
    return "".join(parts)


def clean(raw: str) -> str:
    # 分段符：檕 及其重复 → 换行
    text = re.sub(r"檕+", "\n", raw)
    # 页码标记
    text = _PAGE.sub("", text)
    # 导航行（/xxx/）
    text = _NAV.sub("", text)
    # 残留控制符与字节对齐符
    text = text.replace("\x00", "").replace("·", "")
    # 剔除格式二进制乱码大段
    text = _remove_garbage(text)
    # 压缩空行/空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> int:
    if not SRC.is_file():
        print(f"[错误] 找不到 {SRC}")
        return 1
    ole = olefile.OleFileIO(str(SRC))
    word = ole.openstream("WordDocument").read()
    ole.close()

    text = word.decode("utf-16-le", errors="replace")
    start = text.find("宋會要輯稿")
    if start < 0:
        print("[错误] 未找到正文起点")
        return 1
    body = text[start - 20:]
    mark = body.rfind("宋會要輯稿‧")
    end = find_end(body, mark)
    raw = body[:end]

    cleaned = clean(raw)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(cleaned, encoding="utf-8")
    print(f"提取完成：原始 {len(raw):,} 字符 → 清理后 {len(cleaned):,} 字符")
    print(f"输出：{OUT}")
    print("--- 开头 ---")
    print(cleaned[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
