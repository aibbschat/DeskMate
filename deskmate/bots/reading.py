"""自然语言读稿 —— bot 说「先读《夜不收》」，它自己去找、自己去读。

萧哥 11-06 决策 1：不做「阅读全书」工具按钮。群里是真 Agent，书名写在人话里，
由 bot 自主决定调用只读工具，工具负责把自然语言书名解析成本地书稿并**只读**加载。

红线（务必保留）：
- 每一个读用户文件的入口都走 ``deskmate/sacred_paths.py``：目录遍历用
  ``guarded_dir_entry``，正文读取用 ``guarded_assistance_read``（读侧全放行，
  白名单扩展名、resolve 真实路径、失败返回 None）。
- **绝不写**、绝不删、绝不改写原稿；本模块没有任何写文件路径。
- 解析后的路径必须仍在 books_root 之下，挡住 ``..`` / 软链穿越。
- 输出有硬上限（max_chars），防止整本书灌进上下文把一次群发言烧成天价。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from deskmate.sacred_paths import guarded_assistance_read, guarded_dir_entry

# 与 sacred_paths._ASSISTANCE_EXTS / novel_memory 对齐：纯文本 + docx。
_BOOK_EXTS = {".txt", ".md", ".markdown", ".text", ".novel", ".docx"}
_DEFAULT_BOOKS_ROOT = Path("~/Documents/已出版")
_BOOK_MARKS = re.compile(r"[《<]([^》>]{1,40})[》>]")
# 目录名带编号前缀（"16.夜不收"）是萧哥书库的实际形态，只剥前缀不改名。
_NUM_PREFIX = re.compile(r"^\d+[.\-_、\s]+")


def parse_book_title(text: str, *, known: Sequence[str] | None = None) -> Optional[str]:
    """从一句人话里抽书名，抽不到返回 None（**不猜、不回退到任意路径**）。

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


@dataclass(frozen=True)
class BookShelf:
    """只读书架。books_root 可由环境变量 ``DESKMATE_BOT_BOOKS_ROOT`` 覆盖（测试隔离）。"""

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
            # 允许《16.夜不收》这类带编号的写法，仍只接受精确剥前缀后的匹配
            d = dirs.get(_clean_dir_name(name))
        if d is None:
            return None
        try:
            d.resolve().relative_to(self.books_root)
        except (ValueError, OSError, RuntimeError):
            return None
        return d

    # ------------------------------------------------------------ 正文（只读）
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

    def read(self, title: str, chapter: str = "") -> str:
        """按书名（+ 可选章节关键词）只读加载正文；找不到/读失败返回空串，绝不抛。

        返回文本带文件名出处，方便 bot 引用「哪一章」而不是凭空编。
        """
        book_dir = self.resolve(title)
        if book_dir is None:
            return ""
        files = self._chapter_files(book_dir)
        needle = str(chapter or "").strip()
        if needle:
            matched = [p for p in files if needle in p.name]
            if matched:
                files = matched
        parts: list[str] = []
        used = 0
        partial: list[str] = []   # 读到了但被预算截断的章节
        unread: list[str] = []    # 完全没进上下文的章节
        book_name = _clean_dir_name(book_dir.name)
        for p in files:
            if used >= self.max_chars:
                unread.append(p.name)
                continue
            text = guarded_assistance_read(p, max_chars=self.max_chars)
            if not text:
                unread.append(p.name)
                continue
            room = self.max_chars - used
            body = text[:room]
            used += len(body)
            parts.append(f"《{book_name}》· {p.name}\n{body}")
            if len(body) < len(text):
                partial.append(p.name)
        if used >= self.max_chars or partial or unread:
            # 真 Agent 得知道「后面还有稿子」以及怎么自己续读，否则只能就着
            # 开头硬编，具体建议从根上就没材料。文件名即续读线索，零表单。
            todo = list(dict.fromkeys(partial + unread))
            listed = "、".join(todo[:8])
            more = f"，另有 {len(todo) - 8} 个" if len(todo) > 8 else ""
            parts.append(
                f"（本次已到读取上限 {self.max_chars} 字；尚未完整加载：{listed}{more}。"
                '需要后续内容时用 chapter 参数填文件名关键词（如 chapter="附录"）'
                "单独续读；没读到的部分不要凭空推断情节或段号。）"
            )
        return "\n\n".join(parts)


def default_shelf() -> BookShelf:
    """默认只读书架。``DESKMATE_BOT_READ_CHARS`` 可调一次读稿的字数预算。

    默认 12000 字是成本护栏，不是「全书」：真 Agent 会看到未加载章节名并可用
    ``chapter`` 自己续读。想让它一次读更多，把这个环境变量调大即可（上限 80000），
    代价是输入 token 和单轮耗时同步上升。
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


def make_reading_tools(shelf: BookShelf) -> list[Callable[..., str]]:
    """交给真 Agent 的**只读**工具集（只有两个）。函数名即模型看到的工具名。"""

    def list_books() -> str:
        """列出书架上当前可以读的书名。用户提到某本书但不确定全名时先查这里。"""
        names = shelf.list_books()
        return "、".join(names) if names else "书架为空"

    def read_book(title: str, chapter: str = "") -> str:
        """只读加载一本书的正文，绝不修改文件。

        title 用自然语言里的书名即可（如「《夜不收》」）；chapter 可选，
        填章节名或关键词（如「烧仓」）只挑匹配的文件，留空则读全书前若干章。
        一次读不全时返回内容里会列出未加载的章节文件名，用 chapter 填其中
        的关键词再调用一次即可续读 —— 想引用后段情节就自己多读几次。
        """
        return shelf.read(title, chapter=chapter)

    return [list_books, read_book]
