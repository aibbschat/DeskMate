"""零依赖 .docx 文本提取：.docx 本质是 zip + word/document.xml。

只依赖标准库，避免沙箱内 pip 装包失败的问题。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARA = f"{_W_NS}p"
_TEXT = f"{_W_NS}t"
_TAB = f"{_W_NS}tab"
_BR = f"{_W_NS}br"
_PSTYLE = f"{_W_NS}pStyle"
_PPR = f"{_W_NS}pPr"
_VAL = f"{_W_NS}val"


def _style_of(para: ET.Element) -> str:
    ppr = para.find(_PPR)
    if ppr is None:
        return ""
    ps = ppr.find(_PSTYLE)
    if ps is None:
        return ""
    return ps.get(_VAL) or ""


def _para_text(para: ET.Element) -> str:
    parts: list[str] = []
    for node in para.iter():
        if node.tag == _TEXT:
            parts.append(node.text or "")
        elif node.tag == _TAB:
            parts.append("\t")
        elif node.tag == _BR:
            parts.append("\n")
    return "".join(parts)


def extract_docx(path: Path) -> tuple[str, int]:
    """返回 (纯文本, 段落数)。正文按段落换行，标题行保留。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        target = "word/document.xml"
        if target not in names:
            return "", 0
        xml = z.read(target)
    root = ET.fromstring(xml)
    body = root.find(f"{_W_NS}body")
    if body is None:
        return "", 0

    lines: list[str] = []
    for para in body.iter(_PARA):
        text = _para_text(para).strip()
        if not text:
            continue
        style = _style_of(para)
        # Heading1..9 / 标题1..9 → 加 ## 前缀，灌库时当章节标题
        if re.match(r"^(Heading|heading|标题|Title)\s*\d*$", style) or "Heading" in style or "标题" in style:
            lines.append("## " + text)
        else:
            lines.append(text)
    return "\n".join(lines), len(lines)


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        fp = Path(p)
        txt, n = extract_docx(fp)
        print(f"=== {fp.name} === 段落 {n} · 字符 {len(txt)}")
        print(txt[:400].replace("\n", " / "))
        print()
