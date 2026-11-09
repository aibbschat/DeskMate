"""专家笔记本 —— 每位专家对每本书一本私有笔记（2026-11-09 萧哥拍板）。

背景：市场调研后萧哥定调——bot 群的形态不变，但上桌的专家要「带着自己的笔记本」：
- 只记这本书、只给这个专家自己看；别的专家读不到，作者能看能删；
- **主动记**：不需要作者点「记一下」，专家自己判断什么值得跨会话复用；
- **自己维护**：可以重写整本，合并去重、删过时、更新被推翻的判断。

与「共享记忆」严格分开：这里不是 deskmate-memory，不进群材料，不喂小桔，不跨书。

安全边界（红线）：
- 只写 `<书目录>/.deskmate/experts/<专家 id>/notes.md`，绝不碰书稿/大纲/别的专家；
- 专家 id 只来自常量集合，模型不能传路径或别人的 id；
- resolve 后必须仍在书目录下，拒软链（含悬空软链）；原子写；总字数/单条字数有上限；
- **多入口写同一本必须 `fcntl.flock`**：1v1（catalog）、Bot 群（runtime）、作者编辑页
  （save_notebook）、后台脚本各有独立的 NotebookSession，进程内的 RLock 拦不住彼此，
  读-改-写会互相覆盖丢笔记。锁文件独立于被 os.replace 换 inode 的 notes.md。
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

try:  # macOS 桌面 + Linux 云端都有；非 POSIX 降级为「仅进程内锁」
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .reading import BookShelf, parse_book_title

#: 只有这三位带笔记本（2026-11-09 萧哥：史料专家用得少，先不做）。
NOTEBOOK_BOTS: frozenset[str] = frozenset({
    "brainstormer", "lit-editor", "adaptation", "outline", "character",
})

_EXPERT_ROOT = "experts"
_NOTES_NAME = "notes.md"
_FEEDBACK_HEADING = "## 作者反馈"
# 只认「独立成行」的反馈区标题；正文里引用同名字符串不算（2026-11-09 复查）。
_FEEDBACK_HEADING_RE = re.compile(r"^## 作者反馈[ \t]*$", re.M)
_FIRST_ENTRY_RE = re.compile(r"^- \[", re.M)
MAX_TOTAL_CHARS = 24_000
MAX_ENTRY_CHARS = 1_200
DIGEST_CHARS = 6_000


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """跨进程/跨入口排他锁：包住整个「读 → 改 → 原子写」事务，不是只包写文件。

    os.replace 会换掉 notes.md 的 inode，锁不能加在目标文件上，用一个独立的
    ``notes.md.lock``（与笔记同目录、0600）。fcntl.flock 在进程退出时自动释放。
    """
    if fcntl is None:  # pragma: no cover
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    fd: Optional[int] = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _entry_text(line: str) -> str:
    """去掉 ``- [2026-11-09] `` 这类前缀，取条目正文（用于整行精确去重）。"""
    s = line.strip()
    if s.startswith("- [") and "]" in s:
        s = s[s.find("]") + 1:].strip()
    return s


def _has_entry(current: str, clean: str) -> bool:
    """逐行精确比较，避免短条目被长条目包含时误判为「已经记过」。"""
    return any(_entry_text(line) == clean for line in current.splitlines())


def _has_line(current: str, line: str) -> bool:
    return any(raw.strip() == line for raw in current.splitlines())


@dataclass
class NotebookSession:
    """一个专家在当前这本书上的笔记本句柄（engine 级缓存，按轮更新 book）。

    线程安全：同一个 bot 的 engine 有 turn 锁，但整理/掉落可能在流式 abort 后
    才写，所以这里再包一层 RLock；跨 session / 跨进程靠 ``_file_lock``。
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
            inside = book_dir.resolve()
            parent = target.parent
            # 先探测再 mkdir：父目录若是书外软链，mkdir(exist_ok=True) 不能成为
            # 「在书外建目录」的副作用。悬空软链也要挡（is_symlink 不依赖 exists）。
            if parent.exists() or parent.is_symlink():
                parent.resolve().relative_to(inside)
            if create:
                parent.mkdir(parents=True, exist_ok=True)
            parent.resolve().relative_to(inside)  # 防逃逸
        except (OSError, ValueError, RuntimeError):
            return None
        if target.is_symlink():
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
            with _file_lock(path):
                current = self.read()
                if _has_entry(current, clean):
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
            hint = ""
            if len(text) > MAX_TOTAL_CHARS * 0.7:
                hint = (
                    " 笔记本已超过七成，建议整理：先用 ask_user 问作者要不要合并/删过时"
                    "（给出清单），同意后再 notebook_rewrite。"
                )
            return f"已记入《{self._book_title}》的笔记本（现有 {len(text)} 字）。{hint}"

    def feedback(self, adopted: bool, note: str) -> str:
        """记作者反馈（采纳/驳回）到笔记本顶部的「作者反馈」区；下次摘要先读到。"""
        with self._lock:
            path = self._path(create=True)
            if path is None:
                return "现在没有挂上具体哪本书，反馈记不了；先说明书名或路径。"
            clean = " ".join(str(note or "").split()).strip()
            if not clean:
                return "没收到具体是哪条建议；一句话说清再用。"
            if len(clean) > MAX_ENTRY_CHARS:
                return f"这条反馈太长了（{len(clean)} 字，上限 {MAX_ENTRY_CHARS}）。"
            with _file_lock(path):
                current = self.read()
                stamp = datetime.now().strftime("%Y-%m-%d")
                marker = "采纳" if adopted else "驳回"
                line = f"- [{stamp}] {marker}：{clean}"
                if _has_line(current, line):
                    return "这条反馈已经记过了，没有重复写入。"
                if len(current) + len(line) + 32 > MAX_TOTAL_CHARS:
                    return (
                        f"笔记本快满了（当前 {len(current)} 字，上限 {MAX_TOTAL_CHARS}）。"
                        "先用 notebook_rewrite 整理，再记这条反馈。"
                    )
                # 只有「独立成行且在第一个条目之前」的标题才是真反馈区；
                # 正文里引用「## 作者反馈」不算，否则会把一条笔记从中间劈开。
                heading = _FEEDBACK_HEADING_RE.search(current)
                first_entry = _FIRST_ENTRY_RE.search(current)
                has_heading = heading is not None and (
                    first_entry is None or heading.start() < first_entry.start()
                )
                if not current:
                    text = (
                        f"# {self.expert_id} 的笔记本 · 《{self._book_title}》\n"
                        "> 只给本人看；作者可看可删。每条带日期；过时/被推翻的自己删。\n\n"
                        f"{_FEEDBACK_HEADING}\n{line}\n"
                    )
                elif has_heading:
                    idx = heading.end()
                    head = current[:idx].rstrip("\n")
                    rest = current[idx:].lstrip("\n")
                    text = head + "\n" + line + "\n" + (rest + "\n" if rest else "")
                else:
                    # 没有反馈区：插在标题/说明块之后、正文之前，保证摘要先读到。
                    lines = current.splitlines()
                    split = 0
                    while split < len(lines) and (
                        lines[split].startswith("#") or lines[split].startswith(">")
                    ):
                        split += 1
                    head = "\n".join(lines[:split]).rstrip("\n")
                    rest = "\n".join(lines[split:]).strip("\n")
                    text = head + f"\n\n{_FEEDBACK_HEADING}\n{line}\n"
                    if rest:
                        text += "\n" + rest + "\n"
                self._write_atomic(path, text)
            return f"已记下作者反馈（{marker}）。"

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
            with _file_lock(path):
                self._write_atomic(path, text + "\n")
            return f"笔记本已重写（现 {len(text)} 字）。"

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        """唯一临时文件 + os.replace：同进程多个 session 不再抢同一个 .tmp<pid>。"""
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
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

        book 不填 = 宿主已挂好的「当前这本书」（每轮按作者发言自动切换）；
        只在要临时翻另一本时才填书名（如 book="夜不收"）。别填路径。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》），我才能翻自己的笔记本。"
        text = session.read()
        return text if text else "（这本笔记本还是空的）"

    def notebook_remember(entry: str, book: str = "") -> str:
        """往你自己的笔记本里记一条。

        只在真值得跨会话复用时用：作者明确采纳/否决了什么（连同原因）、查实的
        设定/史实（带章节出处）、你自己认准的判断。不要记闲聊、临时情绪、
        没验证的猜测，也不要抄正文原文。**主动记**，不用等作者说「记一下」。
        book 不填 = 宿主已挂好的「当前这本书」；要临时记到另一本才填书名。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》），再记。"
        return session.remember(entry)

    def notebook_feedback(adopted: bool, note: str, book: str = "") -> str:
        """记录作者对一条建议的采纳/驳回（写进笔记本顶部的「作者反馈」区）。

        用在作者回答「用这条 / 不用」之后，或作者自己说「这条不错/这条不行」时。
        note 写清是哪条建议、为什么（作者原话优先）。下次开聊先读反馈，别重复推荐
        已被驳回的东西。
        book 不填 = 宿主已挂好的「当前这本书」；要临时记到另一本才填书名。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》），再记反馈。"
        return session.feedback(bool(adopted), note)

    def notebook_rewrite(content: str, book: str = "") -> str:
        """整本重写你自己的笔记本。

        用来做维护：合并重复、删掉被推翻/过时的条目、把旧判断压成一句、
        补上作者最新裁定。不要用它把笔记越写越长。
        book 不填 = 宿主已挂好的「当前这本书」；要临时整理另一本才填书名。
        """
        if not session.ensure_book(book):
            return "未识别到具体哪本书：先告诉我书名（如《夜不收》），再整理。"
        return session.rewrite(content)

    return [notebook_read, notebook_remember, notebook_feedback, notebook_rewrite]


# ------------------------------------------------------------------ 1v1 当前书挂载
def _last_user_text(messages) -> str:
    """取最后一条 user 消息的文本；与 DeskMate 记忆注入的多模态取法一致。"""
    if not messages:
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return str(part.get("text", ""))
        return ""
    return ""


def book_from_messages(messages, shelf: BookShelf) -> Optional[str]:
    """从当轮发言认书名 —— 与群聊 materials.prepare 同一口径（不猜、不回退路径）。

    认不出返回 None，由调用方决定沿用还是要求显式书名；绝不用 mtime 推断
    「当前在写」（那是 proactivity 关怀判断的口径，多书并行下会静默记错书）。
    """
    text = _last_user_text(messages)
    if not text:
        return None
    try:
        known = shelf.list_books()
    except Exception:
        known = []
    try:
        return parse_book_title(text, known=known) or None
    except Exception:
        return None


def mount_current_book(session: NotebookSession, messages) -> str:
    """宿主每轮挂书：状态由宿主持有，不交给模型自觉（群聊 runtime:589 的 1v1 对等物）。

    - 当轮认出书名且书架里存在 → set_book 切过去，换书不再串到上一本；
    - 认不出 → 沿用上一轮那本，但**如实回显书名**；
    - 从没认出过 → 明说「还没确定」，写工具保持 fail-closed，绝不猜。

    返回值是注入本轮 prompt 的一行状态；只给书名，绝不外泄绝对路径
    （1v1 没有受守卫的 read_book，给路径只会诱导模型拿通用文件工具绕开
    章节索引 / §18 分类规则去读正文）。
    """
    try:
        title = book_from_messages(messages, session.shelf)
        if title:
            if session.shelf.resolve(title) is not None:
                session.set_book(title)
                return (
                    f"当前在聊：《{session.book_title}》。你的笔记本已挂到这本；"
                    "笔记本工具不带 book 就是指它。换书时说书名即可。"
                )
            if session.book_title:
                return (
                    f"你提到《{title}》，但书架里没有这本；笔记本仍挂《{session.book_title}》。"
                    "要换书请说书架里有的书名。"
                )
            return (
                f"你提到《{title}》，但书架里没有这本，笔记本还没挂上。"
                "请说书架里有的书名，或第一次用工具时带上 book。"
            )
        if session.book_title:
            return (
                f"当前在聊：《{session.book_title}》（沿用上一轮）。你的笔记本挂在这本；"
                "换书时说书名即可。"
            )
        return (
            "当前在聊：还没确定是哪本书。第一次用笔记本工具请带书名"
            "（book=\"夜不收\"），或先告诉我书名。"
        )
    except Exception:
        return ""  # fail-open：挂书失败不能拖垮本轮对话


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
    if path.is_symlink():
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
        return False, "这位专家没有笔记本（只有脑洞 / 文学编辑 / 影视改编 / 大纲 / 人物）"
    # 先用作者侧的同一套守卫探一次：软链/越界在 rewrite 里会返回「没有挂上具体
    # 哪本书」，若只看消息前缀就会误报成功，UI 会 toast「已保存」但文件没动。
    if _notes_path_for(shelf, book, expert) is None:
        return False, "路径守卫拒绝：这本书或这位专家的笔记本不可用"
    session = NotebookSession(str(expert), shelf)
    if not session.ensure_book(book):
        return False, "找不到这本书"
    message = session.rewrite(content)
    return message.startswith("笔记本已重写"), message


def delete_notebook(shelf: BookShelf, book: str, expert: str) -> bool:
    """作者侧：删掉一本笔记（只删 notes.md，不碰书稿）。"""
    path = _notes_path_for(shelf, book, expert)
    if path is None:
        return False
    try:
        with _file_lock(path):
            if not path.is_file():
                return False
            path.unlink()
        return True
    except OSError:
        return False
