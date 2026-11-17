"""引文核验 —— 引号里的话必须是正文原话（2026-11-17 编造台词事故后立）。

事故形态：人物专家读完 13.1 万字全书、交付 3983 字小传，判断大体站得住，
却编了一批**正文根本不存在**的台词，还当「正文锚点」标了章号：

- "日落之后，就是月升。月落之前，国王必须死"（标第四章）——「月落」全书零命中
- "韩国师不是要帮寡人复位，是要寡人的命"（标第十五章）——「韩国师」零命中
- "故意让几万百姓出城，当了倭军的肉盾"——正文是「利用他们作人肉护盾」，改写后当引文
- "被架在火上的鸭子""汤武革命"——零命中

这比整段胡说更难被发现：读起来像原文，还带引号和章号，读者会当成已写事实来用。
所以红线是 **正文未见就是未见**，不能当已写事实。

这里只做机械比对，不做语义判断（不调 LLM、不问模型「这句像不像原文」）：
- 引号里的每一段（按省略号切开）必须能在正文里逐字找到；比对只忽略标点与空白。
  改写、转述、拼接都会判「未见」——这正是要的效果：**转述不该加引号**。
- 找不到就在右引号后插一个短标（默认「（正文未见原句）」），**不改写、不删句、
  不动专家的话**；短引（4-5 字）只在文末清单里列，避免正文被标记淹掉。
- 找到的顺带算真实章号；旁边标的章号与真实章号不符的单独列出。

宁可疑标（把转述标成未见），不可放过（把编造当原文）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: 引号内文字最短几位才当「引文」核。再短的（「明君」「借刀」）是概念词，
#: 不是引用，硬核只会制造噪音。
MIN_QUOTE_CHARS = 4

#: 多短的未见引文进文末清单、而不在正文里就地标。正文里标记太多会把可读性淹掉，
#: 但清单一条都不少——「汤武革命」这种四字编造也必须在案。
INLINE_MIN_CHARS = 6

#: 就地标记文案。说「未见原句」而不是「编造」：我们只知道逐字没找到，
#: 不知道作者是转述还是编的——这个边界不能糊。
UNVERIFIED_MARK = "（正文未见原句）"

#: 文末清单的标题。`scan` 会忽略这一行之后的所有引号：清单里列的就是「未见」的
#: 句子，把已交付的文本（正文 + 清单）再核一次时，它们不该被当成新引文。
#: 没有这条，同一份文本每核一遍就长一批——实测 53 处 → 72 处、39 条 → 55 条。
SUMMARY_HEADING = "## 引文核验（机械比对，非模型判断）"

#: 右引号之后最多看几个字符算「章号标注」（"……"（第四章））。
#: 窗口别放太宽：章号常是**下一句**在说别的事（"（第十五章重提此案）"），
#: 一律算成这句的出处就会造出一堆假警报。
_CHAPTER_CLAIM_WINDOW = 24
_CHAPTER_CLAIM = re.compile(r"第[零一二三四五六七八九十百千两\d]{1,8}[章回]")

#: 「……」|「...」都表示引文拼了两段，一段一段核。
_ELLIPSIS = re.compile(r"…+|\.{3,}|⋯+")

#: 双引号家族：直角/龟甲/弯引号 + 直引号。直引号单独一条，避免和中文引号抢。
_QUOTE_PAT = re.compile(
    r"[「『“](?P<cjk>[^「」『』“”]{2,300})[」』”]"
    r"|(?P<plain>[\"](?P<straight>[^\"\n]{2,300})[\"])"
)


def normalize(text: str) -> str:
    """只留字母数字与汉字，抹掉标点空白并转小写。

    为何抹这么狠：正文与模型输出常在标点上分家（全角/半角、逗号有无、破折号长短），
    逐字比对标点会满屏假阳性。抹掉标点后仍要求**字序完全一致**——改写一个字就过不了，
    这条底线还在。
    """
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())


def _flatten(text: str) -> tuple[str, list[int]]:
    """归一化文本 + 每个字在原文里的下标（用来回查章号）。"""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(str(text or "")):
        if ch.isalnum():
            chars.append(ch.lower())
            index.append(i)
    return "".join(chars), index


def _segments(inner: str) -> list[str]:
    """按省略号切段，丢掉过短的碎片（"原来……" 这种半句）。"""
    out: list[str] = []
    for part in _ELLIPSIS.split(str(inner or "")):
        norm = normalize(part)
        if len(norm) >= MIN_QUOTE_CHARS:
            out.append(norm)
    return out


@dataclass
class Citation:
    """一处引号。`inner` 原样保留（报告里要能读出来是专家自己写的哪句）。"""

    inner: str
    start: int  # 左引号在原文的下标
    end: int  # 右引号之后的下标（标记插这里）
    found: bool = False
    chapter: str = ""  # 真实章号（仅 found 时有值）
    claimed: str = ""  # 旁边标的章号（若有）
    mismatch: bool = False


@dataclass
class CitationReport:
    total: int = 0
    found: int = 0
    unverified: list[Citation] = field(default_factory=list)
    mismatched: list[Citation] = field(default_factory=list)
    annotated: str = ""

    @property
    def flagged(self) -> bool:
        return bool(self.unverified or self.mismatched)


def _chapter_at(manuscript: Any, pos: int) -> Optional[Any]:
    for chapter in getattr(manuscript, "chapters", ()) or ():
        start = int(getattr(chapter, "start", 0))
        end = int(getattr(chapter, "end", 0))
        if start <= pos < end:
            return chapter
    return None


def _locate(flat: str, inner: str) -> Optional[int]:
    """整句逐字命中才算命中；拼了省略号的，每一段都要命中。

    返回第一段在归一化正文里的位置；任一段缺就 None。
    """
    segments = _segments(inner)
    if not segments:
        return None
    first: Optional[int] = None
    for seg in segments:
        pos = flat.find(seg)
        if pos < 0:
            return None
        if first is None:
            first = pos
    return first


def _chapters_flat(manuscript: Any) -> dict[str, str]:
    """每章的归一化正文：用来判断「这句在该章里到底有没有」。

    不这么做的话，一句在第一章和第十章都出现过的话，只要 `find` 先撞上第一章，
    就会被判成「标第十章是错的」——假警报比不报更伤信任。
    """
    source = str(getattr(manuscript, "text", "") or "")
    out: dict[str, str] = {}
    for chapter in getattr(manuscript, "chapters", ()) or ():
        label = str(getattr(chapter, "label", "") or "")
        if not label:
            continue
        start = int(getattr(chapter, "start", 0))
        end = int(getattr(chapter, "end", 0))
        out[label] = normalize(source[start:end])
    return out


def scan(text: str, manuscript: Any) -> list[Citation]:
    """扫出所有引号并逐条核对。不修改 `text`。

    幂等：已经标过「正文未见原句」的引文、以及文末清单里的引号一律跳过。
    这两个来源都是**我们自己写上去的**，不是专家原话——不跳过的话，
    「核一遍再核一遍」会让同一份交付物每过一道就多出一批「未见」。
    """
    raw = str(text or "")
    cut = raw.find(SUMMARY_HEADING)
    if cut != -1:
        raw = raw[:cut]
    flat, index = _flatten(getattr(manuscript, "text", "") or "")
    per_chapter = _chapters_flat(manuscript)
    found_list: list[Citation] = []
    mark_span = len(UNVERIFIED_MARK) + 8  # annotate 会把标记挪到右括号之后
    for match in _QUOTE_PAT.finditer(raw):
        if UNVERIFIED_MARK in raw[match.end() : match.end() + mark_span]:
            continue
        inner = match.group("cjk") if match.group("cjk") is not None else match.group("straight")
        inner = str(inner or "")
        if len(normalize(inner)) < MIN_QUOTE_CHARS:
            continue
        hit = _locate(flat, inner)
        cite = Citation(inner=inner, start=match.start(), end=match.end(), found=hit is not None)
        if hit is not None:
            orig = index[hit] if hit < len(index) else 0
            chapter = _chapter_at(manuscript, orig)
            cite.chapter = str(getattr(chapter, "label", "") or "")
            claimed = _claimed_chapter(raw, cite)
            if claimed:
                cite.claimed = claimed
                # 只有「这句话在该章里确实找不到」才算章号不符。
                in_claimed = per_chapter.get(claimed, "")
                appears = bool(in_claimed) and all(
                    seg in in_claimed for seg in _segments(inner)
                )
                cite.mismatch = bool(cite.chapter) and claimed != cite.chapter and not appears
        found_list.append(cite)
    return found_list


def _claimed_chapter(text: str, cite: Citation) -> str:
    """引文旁边标的章号：先看右引号后一小段（"……"（第四章）），再看前面一小段。"""
    tail = text[cite.end : cite.end + _CHAPTER_CLAIM_WINDOW]
    m = _CHAPTER_CLAIM.search(tail)
    if m:
        return m.group(0)
    head = text[max(0, cite.start - _CHAPTER_CLAIM_WINDOW) : cite.start]
    m = _CHAPTER_CLAIM.search(head)
    return m.group(0) if m else ""


def annotate(text: str, citations: list[Citation]) -> str:
    """在「未见」的长引文右引号后插标记。从后往前插，下标不串位。"""
    marks = [
        (c.end, UNVERIFIED_MARK)
        for c in citations
        if not c.found and len(normalize(c.inner)) >= INLINE_MIN_CHARS
    ]
    if not marks:
        return text
    # 引号外紧跟着右括号时（"……"（第六章点破））把标记挪到括号后面，
    # 别插出「（正文未见原句））」这种双层括号。
    closers = set("）)】]」』")
    fixed: list[tuple[int, str]] = []
    for pos, marker in marks:
        while pos < len(text) and text[pos] in closers:
            pos += 1
        fixed.append((pos, marker))
    out = text
    for pos, marker in sorted(fixed, key=lambda x: x[0], reverse=True):
        out = out[:pos] + marker + out[pos:]
    return out


def _quote_list(items: list[Citation], limit: int = 8) -> str:
    seen: list[str] = []
    for c in items:
        text = c.inner.strip()
        if text and text not in seen:
            seen.append(text)
    shown = [f"「{t}」" for t in seen[:limit]]
    rest = len(seen) - len(shown)
    return "、".join(shown) + (f" 等 {len(seen)} 处" if rest > 0 else "")


def summary(report: CitationReport) -> str:
    """文末核验清单。给模型看，也给萧哥看——**不加渲染**，就是事实。"""
    lines = [
        "---",
        "",
        "## 引文核验（机械比对，非模型判断）",
        "",
        f"核对 {report.total} 处引号，{report.found} 处逐字命中，"
        f"{len(report.unverified)} 处正文未见原句。",
    ]
    short = [c for c in report.unverified if len(normalize(c.inner)) < INLINE_MIN_CHARS]
    long_ones = [c for c in report.unverified if len(normalize(c.inner)) >= INLINE_MIN_CHARS]
    if long_ones:
        lines.append(f"- 正文未见（已在原文就地标注）：{_quote_list(long_ones)}")
    if short:
        lines.append(f"- 正文未见（短引）：{_quote_list(short)}")
    if report.mismatched:
        rows = [
            f"「{c.inner.strip()[:24]}」标 {c.claimed}，实际在 {c.chapter}"
            for c in report.mismatched[:6]
        ]
        lines.append("- 章号不符：" + "；".join(rows))
    lines += [
        "",
        "> 比对只忽略标点与空白：**转述、改写也会被判未见**——引号里只能放原话，"
        "转述请去掉引号。被标注的句子不得当作已写事实引用。",
    ]
    return "\n".join(lines)


def check(text: str, manuscript: Any) -> CitationReport:
    """一次核完：返回报告（含加注后的正文）。"""
    citations = scan(text, manuscript)
    report = CitationReport(
        total=len(citations),
        found=sum(1 for c in citations if c.found),
        unverified=[c for c in citations if not c.found],
        mismatched=[c for c in citations if c.mismatch],
    )
    report.annotated = annotate(text, citations)
    return report


# --------------------------------------------------------------------------- #
# 给专家自己用的工具：交付前先核一遍，而不是等出门被标
# --------------------------------------------------------------------------- #
#: 一次最多核多少句（模型容易把整篇塞进来）。
_MAX_QUOTES = 40


def make_quote_check_tool(shelf: Any, book: str = ""):
    """造 `quote_check` 工具：逐字核对「这句话到底在不在正文里」。

    为什么给工具而不只是事后标注：模型被标记之后**这一轮已经交付完了**，
    下一轮重派才知道。让它自己先核，能当场把编的句子改成转述或删掉——
    这比事后标一遍有用得多。
    """

    def quote_check(quotes: list, book_name: str = "") -> dict:
        """核对引文是否逐字出现在正文里（忽略标点与空白）。

        quotes: 要核对的句子列表。传**不带引号的原话**，一句一条。
        book_name: 书名，一般不用填（默认就是这次派给你的那本书）。
        返回每句的 found / chapter；found=false 表示正文没有这句原话——
        那就别加引号，改成转述，或标「正文未见」。
        """
        title = str(book_name or "").strip() or str(book or "").strip()
        manuscript = None
        try:
            if title and shelf is not None:
                manuscript = shelf.manuscript(title)
        except Exception:  # noqa: BLE001 —— 工具绝不因书架问题炸掉整轮咨询
            manuscript = None
        if manuscript is None:
            return {
                "error": (
                    f"解析不到书稿「{title or '未指定'}」，没法核对。先 list_books 确认书名，"
                    "或直接读稿；核不了就别把句子放进引号。"
                )
            }
        flat, index = _flatten(getattr(manuscript, "text", "") or "")
        rows: list[dict[str, Any]] = []
        for raw in list(quotes or [])[:_MAX_QUOTES]:
            inner = str(raw or "").strip().strip("「」『』“”\"'")
            if len(normalize(inner)) < MIN_QUOTE_CHARS:
                continue
            hit = _locate(flat, inner)
            if hit is None:
                rows.append({"quote": inner, "found": False, "chapter": ""})
                continue
            chapter = _chapter_at(manuscript, index[hit])
            rows.append(
                {
                    "quote": inner,
                    "found": True,
                    "chapter": str(getattr(chapter, "label", "") or ""),
                }
            )
        missing = [r["quote"] for r in rows if not r["found"]]
        return {
            "book": title,
            "checked": len(rows),
            "missing": len(missing),
            "results": rows,
            "hint": (
                "核不到的句子不要加引号：要么去掉引号改成转述，要么标「正文未见」。"
                if missing
                else "全部逐字命中，可以引用。"
            ),
        }

    return quote_check
