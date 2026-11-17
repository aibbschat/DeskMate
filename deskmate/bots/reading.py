"""自然语言读稿 —— bot 说「先读《夜不收》」，它自己去找、自己去读。

萧哥 11-06 决策 1：不做「阅读全书」工具按钮。群里是真 Agent，书名写在人话里，
由 bot 自主决定调用只读工具，工具负责把自然语言书名解析成本地书稿并只读加载。

红线（务必保留）：
- 每一个读用户文件的入口都走 deskmate/sacred_paths.py：目录遍历用
  guarded_dir_entry，正文读取用 guarded_assistance_read（读侧全放行，
  白名单扩展名、resolve 真实路径、失败返回 None）。
- 绝不写、绝不删、绝不改写原稿；本模块没有任何写文件路径。
- 解析后的路径必须仍在 books_root 之下，挡住 .. / 软链穿越。
- 输出有硬上限（max_chars），防止整本书灌进上下文把一次群发言烧成天价。

2026-11-08 读稿层重做（萧哥拍板「以正文为准，读到停笔点」）：
- 按章切分：正文里的「第X章」行首标题即章节边界；没标题的正文整本当一章。
- 停笔点优先：书比预算长时不再从第一页硬砍到 80% —— 而是「开头一段 +
  结尾一段（最新写到的位置）」，中间显式标出来并用章节名告诉 bot 怎么补读。
  作者在末尾写作，脑洞必须站在停笔点上，而不是只见过开头。
- 覆盖率诚实：索引里写清「正文共 N 字 / M 章，本次加载 X 字，哪些章没加载」，
  不再出现「正文已加载」这种让模型误以为读全了的假话。
- 点名读章：read_book(chapter="第十一章") / 章节标题关键词 / 文件名关键词
  都能定位；一次读不完时返回未加载清单，真 Agent 主权不削。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from deskmate.sacred_paths import guarded_assistance_read, guarded_dir_entry

# 与 sacred_paths._ASSISTANCE_EXTS / novel_memory 对齐：纯文本 + docx。
_BOOK_EXTS = {".txt", ".md", ".markdown", ".text", ".novel", ".docx"}
_DEFAULT_BOOKS_ROOT = Path("~/Documents/已出版")
_BOOK_MARKS = re.compile(r"[《<]([^》>]{1,40})[》>]")
# 目录名带编号前缀（"16.夜不收"）是萧哥书库的实际形态，只剥前缀不改名。
_NUM_PREFIX = re.compile(r"^\d+[.\-_、\s]+")

# --------------------------------------------------------------------------- #
# 文件名分类（单一事实源，被 materials.RoomMaterials 与 BookShelf.read 共用）
#
# §18（11-08 萧哥定调）：大纲／粗纲／脑洞／设定这类既有规划只是参考，
# 不是已写正文。默认喂给 bot 会把新脑洞锚死在旧章纲／旧结局上，因此
# classify!=0 的一律「只进索引、不默认加载」；bot 点名补读仍然放行。
# --------------------------------------------------------------------------- #
_NON_FICTION_MARKS = (
    "授权协议", "授权书", "授权合同", "改编协议", "合同", "协议",
    "简介", "目录", "版权", "发票", "收据", "授权",
)
_REFERENCE_MARKS = (
    "大纲", "粗纲", "人物", "脑洞", "设定", "梗概", "分集", "讲戏", "小传",
)


def classify(name: str) -> int:
    """文件名 → 优先级。0=正文/小说文本，1=创作参考，2=非创作文本。

    只按名字里的关键词，不做内容判断、不调 LLM、不猜语义。
    只有 0 会进默认材料 body；1／2 只进索引供点名补读。
    """
    s = str(name or "")
    if any(m in s for m in _NON_FICTION_MARKS):
        return 2
    if any(m in s for m in _REFERENCE_MARKS):
        return 1
    return 0


def parse_book_title(text: str, *, known: Sequence[str] | None = None) -> Optional[str]:
    """从一句人话里抽书名，抽不到返回 None（不猜、不回退到任意路径）。

    两条路：① 《书名》/ <书名> 书名号，最自然也最可靠；
    ② 对照书架已知书名做包含匹配（"大家先把夜不收读一遍"）。
    """
    s = str(text or "")
    m = _BOOK_MARKS.search(s)
    if m:
        return m.group(1).strip()
    for name in known or ():
        n = str(name).strip()
        if n and n in s:
            return n
    return None


def _clean_dir_name(raw: str) -> str:
    return _NUM_PREFIX.sub("", raw).strip()


# --------------------------------------------------------------------------- #
# 章节切分
# --------------------------------------------------------------------------- #
_CHAPTER_LINE = re.compile(
    r"^[ \t]*(第[零一二三四五六七八九十百千两\d]{1,8}[章回])"
    r"(?:[ \t]*[：:、.．\-—·][ \t]*|[ \t]+)?(.*?)[ \t]*$",
    re.MULTILINE,
)
# 从一句话里抽章节标签（"读一下第五到第七章" / "第十章、第十一章"）。
_CH_LABEL_IN_TEXT = re.compile(r"第[零一二三四五六七八九十百千两\d]{1,8}[章回]")

# 🔴 2026-11-17：区间 / 并列写法的解析（"第七到第九章" / "第7-9章" / "第七、八、九章"）。
# 旧实现只有上面那条 `第X章` 正则，而区间写法里**只有最后一个序号跟着「章」**，
# 于是 "第七到第九章" 只匹配到第九章 —— 文档明写支持 "第五到第七章"，实际只返回一章，
# 模型以为读了三章、拿到的是一章，只能反复重读整本。这是专家读不完书的直接原因之一。
_CH_NUM_CHARS = "零一二三四五六七八九十百千两"
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
#: 序号后必须紧跟「章/回」、并列符、区间符或结尾 —— 否则 "第一人称" 这类会误命中。
_CH_NUM_LOOSE = re.compile(
    rf"第?([{_CH_NUM_CHARS}\d]{{1,8}})(?=[章回、，,~～至到\-—–]|$)"
)
_RANGE_SEPS = ("~", "～", "至", "到", "-", "—", "–")
_LIST_SEPS = ("、", "，", ",")


def _cn_to_int(raw: str) -> Optional[int]:
    """「九」→9、「十一」→11、「二十一」→21、「7」→7；解析不了返回 None。"""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    section = 0
    num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        else:
            return None
    return section + num


def _label_to_int(chapter: "Chapter") -> Optional[int]:
    """Chapter.label("第九章") → 9；"楔子"/"全文" → None。"""
    m = re.match(rf"第([{_CH_NUM_CHARS}\d]{{1,8}})[章回]", str(chapter.label or ""))
    return _cn_to_int(m.group(1)) if m else None


@dataclass(frozen=True)
class Chapter:
    """正文里的一章。偏移量相对整本拼接后的正文文本（含文件头）。"""

    index: int
    label: str          # "第十一章"
    title: str          # "烧粮仓"（可能为空）
    start: int
    end: int

    @property
    def chars(self) -> int:
        return max(0, self.end - self.start)

    @property
    def heading(self) -> str:
        return f"{self.label}：{self.title}" if self.title else self.label


def split_chapters(text: str) -> list[Chapter]:
    """按「第X章」行首标题切章；没有标题就整本当一章。

    只认独立成行且很短的标题行（行长 <=40、标题 <=30 字），正文里
    「第三章那句话」这类行内提及不会误切成章。
    """
    s = str(text or "")
    if not s:
        return []
    hits: list[tuple[int, str, str]] = []
    for m in _CHAPTER_LINE.finditer(s):
        line = m.group(0)
        title = (m.group(2) or "").strip()
        if len(line) > 40 or len(title) > 30:
            continue
        hits.append((m.start(), m.group(1), title))
    if not hits:
        return [Chapter(0, "全文", "", 0, len(s))]
    chapters: list[Chapter] = []
    if hits[0][0] > 200:
        chapters.append(Chapter(0, "楔子", "章前", 0, hits[0][0]))
    for i, (start, label, title) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(s)
        chapters.append(Chapter(len(chapters), label, title, start, end))
    return chapters


def _span_desc(chapters: Sequence[Chapter]) -> str:
    labels = [c.label for c in chapters]
    if not labels:
        return "（无章节标记）"
    if len(labels) == 1:
        return labels[0]
    if len(labels) <= 3:
        return "、".join(labels)
    return f"{labels[0]}～{labels[-1]}"


# --------------------------------------------------------------------------- #
# 整本正文 + 覆盖率
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Coverage:
    """一次「按预算装正文」的结果：正文体 + 诚实的覆盖率说明。"""

    body: str
    note: str
    total: int
    budget: int
    head_chars: int
    tail_chars: int
    gap_chars: int
    full: bool
    loaded_indexes: tuple[int, ...]      # 有内容进上下文的所有章
    partial_indexes: tuple[int, ...]     # 只装进一部分的章（预算从中间截断）
    head_desc: str
    tail_desc: str
    gap_desc: str


@dataclass(frozen=True)
class Manuscript:
    """整本正文（只读）。render() 负责按预算装开头 + 停笔点。"""

    title: str
    text: str
    chapters: tuple[Chapter, ...]
    sources: tuple[str, ...]
    total: int
    #: 每份正文文件在合并文本里的 (文件名, 起点, 终点) —— 用来告诉 bot
    #: 「哪一份文件没读全」，也是它用 chapter 填文件名续读的线索。
    file_spans: tuple[tuple[str, int, int], ...] = ()

    # -------------------------------------------------------------- 选章
    def find_chapters(self, needle: str) -> list[Chapter]:
        """按章节标签 / 章节标题 / 区间（"第五到第七章"）定位章节。

        区间与并列写法先走序号解析（2026-11-17 修）：模型实际会写
        "第七到第九章"、"第7-9章"、"第七、八、九章"，这几种里只有最后一个
        序号跟着「章」字，旧的 `第X章` 正则只认出一个，于是"点三章只给一章"。
        """
        s = str(needle or "").strip()
        if not s:
            return []

        # ① 区间 / 并列：至少两个序号才判定，单个序号交给下面的原逻辑（含标题匹配）。
        nums = [n for n in (_cn_to_int(m) for m in _CH_NUM_LOOSE.findall(s)) if n is not None]
        if len(nums) >= 2:
            if any(sep in s for sep in _RANGE_SEPS):
                lo, hi = min(nums), max(nums)
                picked = [
                    c for c in self.chapters
                    if (n := _label_to_int(c)) is not None and lo <= n <= hi
                ]
            elif any(sep in s for sep in _LIST_SEPS):
                want = set(nums)
                picked = [c for c in self.chapters if _label_to_int(c) in want]
            else:
                picked = []
            if picked:
                return picked

        labels = _CH_LABEL_IN_TEXT.findall(s)
        if labels:
            want: list[Chapter] = []
            for label in labels:
                for c in self.chapters:
                    if c.label == label and c not in want:
                        want.append(c)
            if len(want) >= 2 and any(sep in s for sep in ("～", "~", "至", "到", "-", "—")):
                order = {id(c): i for i, c in enumerate(self.chapters)}
                i, j = sorted(order[id(c)] for c in want)
                return list(self.chapters[i : j + 1])
            if want:
                return want
            return [c for c in self.chapters if any(lab in c.label for lab in labels)]
        hits = [c for c in self.chapters if s in c.heading]
        if hits:
            return hits
        return [c for c in self.chapters if s in c.label]

    # -------------------------------------------------------------- 渲染
    def render(self, budget: int, *, head_ratio: float = 0.4) -> Coverage:
        """按预算装正文：短书整本；长书「开头 + 结尾（停笔点）」，中间显式标出。"""
        budget = max(200, int(budget))
        total = self.total
        chapters = list(self.chapters)
        if total <= budget:
            note = (
                f"正文共 {total:,} 字 / {len(chapters)} 章，本次全部加载"
                "（结尾即最新写到的位置）。"
            )
            return Coverage(
                body=self.text,
                note=note,
                total=total,
                budget=budget,
                head_chars=total,
                tail_chars=0,
                gap_chars=0,
                full=True,
                loaded_indexes=tuple(c.index for c in chapters),
                partial_indexes=(),
                head_desc=_span_desc(chapters),
                tail_desc="",
                gap_desc="",
            )
        head_budget = min(total, max(1, int(budget * head_ratio)))
        tail_budget = min(total - head_budget, max(1, budget - head_budget))
        # 切在段落边界上，别把一句话劈成两半（最多让 400 字）。
        head_len = self.text.rfind("\n", max(0, head_budget - 400), head_budget)
        if head_len < 200:
            head_len = head_budget
        tail_start = self.text.find(
            "\n", total - tail_budget, min(total, total - tail_budget + 400)
        )
        if tail_start < 0 or tail_start <= head_len:
            tail_start = total - tail_budget
        tail_len = total - tail_start
        gap_from, gap_to = head_len, tail_start
        gap = max(0, gap_to - gap_from)

        def touched(lo: int, hi: int) -> list[Chapter]:
            return [c for c in chapters if c.start < hi and c.end > lo]

        head_chs = touched(0, head_len)
        tail_chs = touched(gap_to, total)
        gap_chs = [c for c in chapters if c not in head_chs and c not in tail_chs]
        if not gap_chs:
            # 缺口落在开头/结尾那两章的中段时，用「被截断的章」来描述。
            gap_chs = [c for c in chapters if c.start < gap_to and c.end > gap_from]
        head_desc = _span_desc(head_chs)
        tail_desc = _span_desc(tail_chs)
        gap_desc = _span_desc(gap_chs)

        marker = (
            f"\n\n〔……中间 {gap:,} 字（{gap_desc}）本次未加载——只是省上下文，"
            "不是正文缺失；要写到其中哪一段，用 read_book chapter=\"第X章\" 补读，"
            "别略过。下面接的是正文结尾、最新写到的地方。〕\n\n"
        )
        body = self.text[:head_len] + marker + self.text[gap_to:]
        uncovered_files = [
            name
            for name, start, end in self.file_spans
            if not (end <= head_len or start >= gap_to)
        ]
        file_note = (
            "本次未完整加载的正文文件：" + "、".join(uncovered_files)
            + "（可用 read_book 的 chapter 填文件名关键词单独补读）。"
            if uncovered_files
            else ""
        )
        note = (
            f"正文共 {total:,} 字 / {len(chapters)} 章；本次读取上限 {budget} 字，"
            f"实际加载 {head_len + tail_len:,} 字：开头 {head_len:,} 字（{head_desc}）"
            f"+ 结尾 {tail_len:,} 字（{tail_desc}，停笔点在内）；"
            f"中间 {gap:,} 字（{gap_desc}）未加载，可用 read_book chapter=\"第X章\" 点名补读。"
            + file_note
        )
        def _fully_inside(c: Chapter) -> bool:
            return c.end <= head_len or c.start >= gap_to

        loaded = tuple(sorted({c.index for c in head_chs + tail_chs}))
        partial = tuple(sorted({c.index for c in head_chs + tail_chs if not _fully_inside(c)}))
        return Coverage(
            body=body,
            note=note,
            total=total,
            budget=budget,
            head_chars=head_len,
            tail_chars=tail_len,
            gap_chars=gap,
            full=False,
            loaded_indexes=loaded,
            partial_indexes=partial,
            head_desc=head_desc,
            tail_desc=tail_desc,
            gap_desc=gap_desc,
        )

    def render_chapter(self, chapter: Chapter, budget: int) -> str:
        budget = max(200, int(budget))
        raw = self.text[chapter.start : chapter.end]
        head = f"《{self.title}》正文 · {chapter.heading}（本章 {chapter.chars:,} 字）"
        if len(raw) <= budget:
            return f"{head}\n{raw}"
        return (
            f"{head}\n{raw[:budget]}"
            f"\n（本章共 {chapter.chars:,} 字，本次读取上限 {budget} 字；"
            "后段未加载，用更具体的标题 / 文件名关键词再点名。）"
        )


# --------------------------------------------------------------------------- #
# 书架
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BookShelf:
    """只读书架。books_root 可由环境变量 DESKMATE_BOT_BOOKS_ROOT 覆盖（测试隔离）。"""

    books_root: Path
    max_chars: int = 12_000
    _cache: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "books_root", Path(self.books_root).expanduser().resolve())
        if self.max_chars < 200:
            raise ValueError("max_chars 太小，读不到任何正文")

    # ------------------------------------------------------------ 目录（只读）
    def _book_dirs(self) -> dict[str, Path]:
        cached = self._cache.get("dirs")
        if cached is not None:
            return cached
        out: dict[str, Path] = {}
        root = self.books_root
        if not root.is_dir() or not guarded_dir_entry(root, allow_discovery=True):
            self._cache["dirs"] = out
            return out
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            self._cache["dirs"] = out
            return out
        for p in entries:
            if not p.is_dir() or not guarded_dir_entry(p, allow_discovery=True):
                continue
            name = _clean_dir_name(p.name)
            if name and not name.startswith("."):
                out.setdefault(name, p.resolve())
        self._cache["dirs"] = out
        return out

    def list_books(self) -> list[str]:
        return sorted(self._book_dirs())

    def resolve(self, title: str) -> Optional[Path]:
        """书名 → 书稿目录；解析后必须仍在 books_root 之下，否则拒绝。"""
        name = parse_book_title(str(title or ""), known=self.list_books()) or str(title or "").strip()
        if not name:
            return None
        dirs = self._book_dirs()
        d = dirs.get(name)
        if d is None:
            d = dirs.get(_clean_dir_name(name))
        if d is None:
            return None
        try:
            d.resolve().relative_to(self.books_root)
        except (ValueError, OSError, RuntimeError):
            return None
        return d

    # ------------------------------------------------------------ 正文（只读）
    def _experience_files(self, book_dir: Path) -> list[Path]:
        """小桔既有的 .deskmate 沉淀。只在 chapter 点名时才进补读候选。"""
        from .experience import experience_files

        return experience_files(book_dir)

    def _chapter_files(self, book_dir: Path) -> list[Path]:
        try:
            entries = sorted(book_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        return [
            p.resolve()
            for p in entries
            if p.is_file()
            and p.suffix.lower() in _BOOK_EXTS
            and not p.name.startswith(".")
            and guarded_dir_entry(p)
        ]

    def _file_text(self, p: Path) -> str:
        """单文件全文（带 mtime/size 缓存）。读侧守卫仍走 sacred_paths。"""
        try:
            st = p.stat()
            key = ("text", str(p), int(st.st_mtime), int(st.st_size))
        except OSError:
            key = ("text", str(p), 0, 0)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        text = guarded_assistance_read(p, max_chars=10_000_000) or ""
        self._cache[key] = text
        return text

    def _manuscript_from(self, title: str, files: Sequence[Path]) -> Optional[Manuscript]:
        parts: list[str] = []
        sources: list[str] = []
        spans: list[tuple[str, int, int]] = []
        offset = 0
        for p in files:
            text = self._file_text(p)
            if not text:
                continue
            sources.append(p.name)
            chunk = f"《{title}》· {p.name}\n{text}"
            parts.append(chunk)
            spans.append((p.name, offset, offset + len(chunk)))
            offset += len(chunk) + 2  # 段落分隔符
        if not parts:
            return None
        merged = "\n\n".join(parts)
        return Manuscript(
            title=title,
            text=merged,
            chapters=tuple(split_chapters(merged)),
            sources=tuple(sources),
            total=len(merged),
            file_spans=tuple(spans),
        )

    def manuscript(self, title: str) -> Optional[Manuscript]:
        """整本已写正文（只含 classify==0 的文件，章节已切好）。找不到返回 None。"""
        book_dir = self.resolve(title)
        if book_dir is None:
            return None
        files = [p for p in self._chapter_files(book_dir) if classify(p.name) == 0]
        if not files:
            return None
        return self._manuscript_from(_clean_dir_name(book_dir.name), files)

    def read(self, title: str, chapter: str = "", *, max_chars: int | None = None) -> str:
        """按书名（+ 可选章节 / 文件名关键词）只读加载正文；找不到返回空串，绝不抛。

        chapter 留空 = 读整本（预算不够时自动装「开头 + 停笔点」，中间显式标出）。
        chapter 非空先按章节匹配（"第十一章" / "烧粮仓" / "第五到第七章"），
        再退回文件名关键词（含大纲、小桔沉淀）。max_chars 是本次调用预算。
        """
        book_dir = self.resolve(title)
        if book_dir is None:
            return ""
        budget = self.max_chars if max_chars is None else int(max_chars)
        if budget < 200:
            budget = 200
        book_name = _clean_dir_name(book_dir.name)
        manuscript = self.manuscript(title)
        needle = str(chapter or "").strip()
        if needle:
            if manuscript is not None:
                chapters = manuscript.find_chapters(needle)
                if chapters:
                    # 2026-11-16 靶子①修复（萧哥拍板）：多章点名的预算按章数均分，
                    # 总量受单次预算封顶。旧行为每章各吃满 budget ⇒「第1到第3章」
                    # = 3×budget 无上限（默认 80k 下可达 24 万字）。保底
                    # _MULTI_CHAPTER_MIN 字防碎片化；单章点名均分即 budget，行为不变。
                    per = budget if len(chapters) == 1 else max(
                        _MULTI_CHAPTER_MIN, budget // len(chapters)
                    )
                    return "\n\n".join(
                        manuscript.render_chapter(c, per) for c in chapters
                    )
            matched = [p for p in self._chapter_files(book_dir) if needle in p.name]
            if not matched:
                matched = [p for p in self._experience_files(book_dir) if needle in p.name]
            if matched:
                alternative = self._manuscript_from(book_name, matched)
                if alternative is not None:
                    cov = alternative.render(budget)
                    return f"{cov.body}\n\n{cov.note}"
            if manuscript is not None:
                return (
                    "（没有匹配到这一章 / 文件。可用："
                    + "、".join(c.heading for c in manuscript.chapters)
                    + "）"
                )
            return ""
        if manuscript is None:
            return (
                "（这本书顶层没有正文／小说文本；大纲、粗纲、设定等参考文档不默认加载。"
                "确需查看请用 chapter 参数填文件名关键词点名补读。）"
            )
        cov = manuscript.render(budget)
        return f"{cov.body}\n\n{cov.note}"


def default_shelf() -> BookShelf:
    """默认只读书架。DESKMATE_BOT_READ_CHARS 可调一次读稿的字数预算。

    默认 12000 字是成本护栏，不是「全书」：书比预算长时按「开头 + 停笔点」
    装，并在正文与索引里都如实标出哪些章没加载、怎么点名补读。
    """
    root = os.environ.get("DESKMATE_BOT_BOOKS_ROOT") or _DEFAULT_BOOKS_ROOT
    raw = os.environ.get("DESKMATE_BOT_READ_CHARS")
    max_chars = 12_000
    if raw:
        try:
            parsed = int(raw)
            if 200 <= parsed <= 80_000:
                max_chars = parsed
        except ValueError:
            pass
    return BookShelf(Path(root), max_chars=max_chars)


# --------------------------------------------------------------------------- #
# 读稿预算（2026-11-08 能力常备化）
# --------------------------------------------------------------------------- #
DEEP_READ_CHARS_DEFAULT = 80_000

# 多章点名（区间/多标签命中）时每章保底字数（2026-11-16 靶子①）：区间均分预算
# 后，防止章数一多每章只剩几百字的碎片化。单章点名不受它影响（吃满预算）。
_MULTI_CHAPTER_MIN = 1_000


def deep_read_chars() -> int:
    """读稿预算（DESKMATE_BOT_DEEP_READ_CHARS 可调，合法 12k~100k）。"""
    raw = os.environ.get("DESKMATE_BOT_DEEP_READ_CHARS")
    if raw:
        try:
            parsed = int(raw)
            if 12_000 <= parsed <= 100_000:
                return parsed
        except ValueError:
            pass
    return DEEP_READ_CHARS_DEFAULT


def make_reading_tools(shelf: BookShelf, *, max_chars: int | None = None) -> list:
    """交给真 Agent 的只读工具集（只有两个）。函数名即模型看到的工具名。

    2026-11-08（萧哥拍板「以正文为准，读到停笔点」）：max_chars 缺省即
    常备预算（deep_read_chars()，默认 80000）；读多少由模型按题判断。
    但读不全时工具会如实报覆盖率与未加载章节，模型不能拿开头硬推全局。
    """
    budget = deep_read_chars() if max_chars is None else int(max_chars)

    def list_books() -> str:
        """列出书架上当前可以读的书名。用户提到某本书但不确定全名时先查这里。"""
        names = shelf.list_books()
        return "、".join(names) if names else "书架为空"

    def read_book(title: str, chapter: str = "") -> str:
        """只读补读书稿，绝不修改文件。留空读整本：开头 + 停笔点（结尾）一起装，
        中间没装的章节会明确列出，别拿开头硬推全局。

        读多少由题决定：要系统级方案（盘局势／完整设计／「怎么才能完成
        任务」这类硬题）时，先读全文再动笔；小问题（这段好不好、这个词准
        不准）按需点名。chapter 可以填章节（"第十一章" / "烧粮仓" /
        "第五到第七章"），也可以填文件名关键词（如「绝命救援」「进度简报」）；
        它不是路径，填绝对路径不会命中。群里已经共享了一份正文材料，先看那份再决定补什么。
        大纲／粗纲／设定属于既有规划参考，不是已写
        正文、不是作者终裁，讨论新走向时不要拿它当既定框架。
        """
        return shelf.read(title, chapter=chapter, max_chars=budget)

    return [list_books, read_book]
