"""专家笔记本 —— 每位专家对每本书一本私有笔记（2026-11-09 萧哥拍板）。

背景：市场调研后萧哥定调——bot 群的形态不变，但上桌的专家要「带着自己的笔记本」：
- 只记这本书、只给这个专家自己看；别的专家读不到，作者能看能删；
- **主动记**：不需要作者点「记一下」，专家自己判断什么值得跨会话复用；
- **自己维护**：可以重写整本，合并去重、删过时、更新被推翻的判断。

与「共享记忆」严格分开：这里不是 deskmate-memory，不进群材料，不喂小桔，不跨书。

安全边界（红线）：
- 只写 `<书目录>/.deskmate/experts/<专家 id>/notes.md`，绝不碰书稿/大纲/别的专家；
- 专家 id 只来自常量集合，模型不能传路径或别人的 id；
- resolve 后必须仍在书目录下，拒软链；原子写；总字数/单条字数有上限。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .reading import BookShelf

#: 只有这三位带笔记本（2026-11-09 萧哥：史料专家用得少，先不做）。
NOTEBOOK_BOTS: frozenset[str] = frozenset({"brainstormer", "lit-editor", "adaptation"})

_EXPERT_ROOT = "experts"
_NOTES_NAME = "notes.md"
MAX_TOTAL_CHARS = 24_000
MAX_ENTRY_CHARS = 1_200
DIGEST_CHARS = 6_000


@dataclass
class NotebookSession:
    """一个专家在当前这本书上的笔记本句柄（engine 级缓存，按轮更新 book）。

    线程安全：同一个 bot 的 engine 有 turn 锁，但整理/掉落可能在流式 abort 后
    才写，所以这里再包一层 RLock。
    """

    expert_id: str
    shelf: BookShelf
    _book_title: str = ""
    _book_dir: Optional[Path] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------------ 定位
    @property
    def book_title(self) -> str:
        return self._book_title

    def set_book(self, title: str | None) -> None:
        """切换到当前这本书（空 / 无法解析 = 本轮没有笔记本，工具会如实说）。"""
        with self._lock:
            text = str(title or "").strip()
            if not text:
                self._book_title, self._book_dir = "", None
                return
            book_dir = self.shelf.resolve(text)
            if book_dir is None:
                self._book_title, self._book_dir = "", None
                return
            self._book_title, self._book_dir = text, book_dir

    def ensure_book(self, book: str | None = None) -> bool:
        """1v1 场景：工具显式带书名；不填就沿用当前书。返回是否能定位到书。"""
        with self._lock:
            text = str(book or "").strip()
            if text:
                self.set_book(text)
            return self._book_dir is not None

    def _path(self, *, create: bool = False) -> Optional[Path]:
        if self._book_dir is None:
            return None
        book_dir = self._book_dir
        target = book_dir / ".deskmate" / _EXPERT_ROOT / self.expert_id / _NOTES_NAME
        try:
            if create:
                target.parent.mkdir(parents=True, exist_ok=True)
            parent = target.parent.resolve()
            parent.relative_to(book_dir.resolve())  # 防逃逸
        except (OSError, ValueError, RuntimeError):
            return None
        if target.exists() and target.is_symlink():
            return None
        return target

    # ------------------------------------------------------------------ 读
    def read(self) -> str:
        with self._lock:
            path = self._path()
            if path is None or not path.is_file():
                return ""
            try:
                return path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                return ""

    def digest(self, limit: int = DIGEST_CHARS) -> str:
        """给 prompt 的摘要：短就整本，长就保留头部 + 最近条目。"""
        text = self.read()
        if len(text) <= limit:
            return text
        head_len = max(300, int(limit * 0.25))
        head, tail = text[:head_len], text[-(limit - head_len):]
        return head + "\n\n……（中间较早的条目已省略，需要看全文用 notebook_read）……\n\n" + tail

    # ------------------------------------------------------------------ 写
    def remember(self, entry: str) -> str:
        """追加一条，带日期；重复条目不重复记；超上限时要求先整理。"""
        with self._lock:
            path = self._path(create=True)
            if path is None:
                return "现在没有挂上具体哪本书，笔记本不可用；先让作者说明书名或路径。"
            clean = " ".join(str(entry or "").split()).strip()
            if not clean:
                return "没收到要记的内容。"
            if len(clean) > MAX_ENTRY_CHARS:
                return f"这条太长了（{len(clean)} 字，上限 {MAX_ENTRY_CHARS}）——只记结论和依据，细节不用抄。"
            current = self.read()
            if clean in current:
                return "这条笔记已经记过了，没有重复写入。"
            if len(current) + len(clean) + 32 > MAX_TOTAL_CHARS:
                return (
                    f"笔记本快满了（当前 {len(current)} 字，上限 {MAX_TOTAL_CHARS}）。"
                    "先用 notebook_rewrite 整理：合并同类、删被推翻的、把过时的压成一句。"
                )
            stamp = datetime.now().strftime("%Y-%m-%d")
            header = ""
            if not current:
                header = (
                    f"# {self.expert_id} 的笔记本 · 《{self._book_title}》\n"
                    "> 只给本人看；作者可看可删。每条带日期；过时/被推翻的自己删。\n\n"
                )
            body = current + "\n" if current else ""
            text = header + body + f"- [{stamp}] {clean}\n"
            self._write_atomic(path, text)
            return f"已记入《{self._book_title}》的笔记本（现有 {len(text)} 字）。"

    def rewrite(self, content: str) -> str:
        """整本重写（专家自己做合并/去重/删过时用）。"""
        with self._lock:
            path = self._path(create=True)
            if path is None:
                return "现在没有挂上具体哪本书，笔记本不可用。"
            text = str(content or "").strip()
            if not text:
                return "重写内容为空；要清空请明确写成一句「清空」，不要用空内容。"
            if len(text) > MAX_TOTAL_CHARS:
                return f"重写内容太长了（{len(text)} 字，上限 {MAX_TOTAL_CHARS}）——先压到上限内。"
            self._write_atomic(path, text + "\n")
            return f"笔记本已重写（现 {len(text)} 字）。"

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def make_notebook_tools(session: NotebookSession) -> list:
    """交给真 Agent 的笔记本工具（只作用于该专家自己的那一本）。"""

    def notebook_read(book: str = "") -> str:
        """读你自己这本笔记本的全文。

        book 不填 = 当前这本书（群聊里会自动挂上）；1v1 里请填书名或路径
        （如 book="夜不收"），否则不知道翻哪个项目。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》）或路径，我才能翻自己的笔记本。"
        text = session.read()
        return text if text else "（这本笔记本还是空的）"

    def notebook_remember(entry: str, book: str = "") -> str:
        """往你自己的笔记本里记一条。

        只在真值得跨会话复用时用：作者明确采纳/否决了什么（连同原因）、查实的
        设定/史实（带章节出处）、你自己认准的判断。不要记闲聊、临时情绪、
        没验证的猜测，也不要抄正文原文。**主动记**，不用等作者说「记一下」。
        book 不填 = 当前这本书；1v1 里带上书名。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》）或路径，再记。"
        return session.remember(entry)

    def notebook_rewrite(content: str, book: str = "") -> str:
        """整本重写你自己的笔记本。

        用来做维护：合并重复、删掉被推翻/过时的条目、把旧判断压成一句、
        补上作者最新裁定。不要用它把笔记越写越长。
        book 不填 = 当前这本书；1v1 里带上书名。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》）或路径，再整理。"
        return session.rewrite(content)

    return [notebook_read, notebook_remember, notebook_rewrite]

# ------------------------------------------------------------------ 作者侧界面用
def _notes_path_for(shelf: BookShelf, book: str, expert: str) -> Optional[Path]:
    """定位某本书某位专家的 notes.md；非法专家/书/越界/软链一律 None。"""
    if str(expert or "") not in NOTEBOOK_BOTS:
        return None
    try:
        book_dir = shelf.resolve(str(book or ""))
    except Exception:  # noqa: BLE001
        return None
    if book_dir is None:
        return None
    path = book_dir / ".deskmate" / _EXPERT_ROOT / str(expert) / _NOTES_NAME
    try:
        path.parent.resolve().relative_to(book_dir.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    if path.exists() and path.is_symlink():
        return None
    return path


def list_notebooks(shelf: BookShelf) -> list[dict]:
    """作者侧：列出所有有笔记的「书 × 专家」，新的在前。"""
    out: list[dict] = []
    for title in shelf.list_books():
        for expert in sorted(NOTEBOOK_BOTS):
            path = _notes_path_for(shelf, title, expert)
            if path is None or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                updated = int(path.stat().st_mtime)
            except OSError:
                continue
            out.append({
                "book": title,
                "expert": expert,
                "chars": len(text),
                "updated": updated,
            })
    out.sort(key=lambda item: item["updated"], reverse=True)
    return out


def read_notebook(shelf: BookShelf, book: str, expert: str) -> Optional[str]:
    path = _notes_path_for(shelf, book, expert)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def save_notebook(shelf: BookShelf, book: str, expert: str, content: str) -> tuple[bool, str]:
    """作者侧：整本保存编辑后的笔记（走与专家同一套上限/原子写/守卫）。"""
    if str(expert or "") not in NOTEBOOK_BOTS:
        return False, "只有脑洞 / 文学编辑 / 影视改编三位有笔记本"
    session = NotebookSession(str(expert), shelf)
    if not session.ensure_book(book):
        return False, "找不到这本书"
    message = session.rewrite(content)
    if message.startswith("重写内容为空") or message.startswith("重写内容太长"):
        return False, message
    return True, message


def delete_notebook(shelf: BookShelf, book: str, expert: str) -> bool:
    """作者侧：删掉一本笔记（只删 notes.md，不碰书稿）。"""
    path = _notes_path_for(shelf, book, expert)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
