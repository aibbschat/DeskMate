"""群级共享材料面 —— 群里放一份，各 bot 自取。

萧哥 11-06 的「群共享文件」形态：群主在群里发一个文件，各人自己去读，
不需要一个一个发。这里把「找材料」从**每个 bot 的在线自主决策**上移为
**群级一次性本地 IO 准备**：一次备料，N 位 bot 共用同一份原文出处。

为什么这一层能同时保质、省时、省钱（全部为 11-06 本机实测，见 docs §16）：
- 旧 ``BookShelf.read`` 按文件名排序，《夜不收》前 12000 字是授权协议／粗纲／
  大纲，230KB 正文排第 9 ⇒ bot 第一次「读《夜不收》」读到的是合同，
  只能反复试探 ``chapter`` 撞正文。**既是质量事故，也是 R 轮工具往返的源头**。
- 每次工具往返都要重付一轮长思考（ reasoning 模型默认 60~90s 首 token，
  同题一次直出 329.4s / 20208 completion token，其中 thinking 27194 字）。
  砍掉「找文件」往返不动最后那一轮产生质量的深度思考。

硬红线（保质优先，违反即回退）：
- **共享的是原文分块 + 文件索引，绝不是 LLM 抽取／摘要／圣经／纪要**；
  零 LLM、纯本地 IO、只读走 ``sacred_paths``（复用 ``BookShelf`` 的守卫）。
- **不削真 Agent**：``read_book`` 仍在，bot 可随时按文件名关键词补读别的材料。
- 自然语言触发，零表单：书名从群里那句人话里抽，抽不到就不备料。
- 不改思考强度、不压正文字数、不减唤醒人数。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from deskmate.sacred_paths import guarded_assistance_read

from .reading import BookShelf, parse_book_title

# 非创作文本：默认不喂给 bot（列进索引，bot 主动要才读）。
_NON_FICTION_MARKS = (
    "授权协议", "授权书", "授权合同", "改编协议", "合同", "协议",
    "简介", "目录", "版权", "发票", "收据", "授权",
)
# 创作参考 material：排在正文之后，仍是有用材料（大纲/人物/脑洞汇总）。
_REFERENCE_MARKS = (
    "大纲", "粗纲", "人物", "脑洞", "设定", "梗概", "分集", "讲戏", "小传",
)

# 备料预算。与 BookShelf 默认一致；不放大（单轮已 329s，放大只会更慢更贵）。
_DEFAULT_MAX_CHARS = 12_000
_RELOAD_MARKS = ("重读", "重新读", "再读一遍", "刷新材料", "重新加载")


def classify(name: str) -> int:
    """文件名 → 优先级。0=正文/小说文本，1=创作参考，2=非创作文本。

    只按名字里的关键词，不做内容判断、不调 LLM、不猜语义。
    """
    s = str(name or "")
    if any(m in s for m in _NON_FICTION_MARKS):
        return 2
    if any(m in s for m in _REFERENCE_MARKS):
        return 1
    return 0


def _fingerprint(files: Sequence[Path]) -> str:
    """文件集合的 mtime+size 指纹；改稿后自动失效，不必人工清缓存。"""
    parts = []
    for p in files:
        try:
            st = p.stat()
            parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{p.name}:?:?")
    return "|".join(parts)


@dataclass(frozen=True)
class MaterialPack:
    """一次备料的产物。同一 room 的所有 bot 拿到完全相同的一份（字节一致）。"""

    book: str
    fingerprint: str
    body: str
    index: str

    def prompt_block(self) -> str:
        """注入 prompt 的固定前缀。原文在前，索引在后，不含任何摘要。"""
        return (
            "# 群里已经共享的书稿材料（只读，可直接引用）\n\n"
            + self.body
            + "\n\n"
            + self.index
        )


class RoomMaterials:
    """按 room 缓存的共享材料。纯内存：群是随建随撤的临时容器，服务重启重新备料
    只需 0.04s 本地 IO，落库反而引入持久化与原子写风险（守项目红线）。

    ``prepare()`` 每次都重算 fingerprint：改稿后下一次发言自动换新，
    不需要用户记着说「重读」；显式说「重读」只是强制刷新同一份。
    """

    def __init__(
        self,
        shelf: BookShelf,
        *,
        max_chars: int | None = None,
    ) -> None:
        self.shelf = shelf
        raw = os.environ.get("DESKMATE_BOT_MATERIAL_CHARS")
        self.max_chars = int(raw) if (raw and raw.isdigit() and 200 <= int(raw) <= 80_000) else (
            max_chars if max_chars is not None else _DEFAULT_MAX_CHARS
        )
        self._cache: dict[str, MaterialPack] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 读
    def get(self, room_id: str) -> Optional[MaterialPack]:
        with self._lock:
            return self._cache.get(str(room_id))

    def drop(self, room_id: str) -> None:
        """撤群时清掉材料；消息历史不受影响（RoomStore 只打墓碑）。"""
        with self._lock:
            self._cache.pop(str(room_id), None)

    # ---------------------------------------------------------------- 备料
    def prepare(
        self,
        room_id: str,
        text: str,
        *,
        force: bool = False,
    ) -> Optional[MaterialPack]:
        """从群里一句人话备料；抽不到书名时沿用该群已有材料（不猜、不回退路径）。"""
        rid = str(room_id)
        force = force or self._is_reload(text)
        known = self.shelf.list_books()
        title = parse_book_title(text, known=known)
        with self._lock:
            cached = self._cache.get(rid)
        if not title:
            # 没提书名就沿用群里已放的那份；**但仍要验新鲜度**——作者改稿后
            # 下一句哪怕只是「接着聊」，也不能继续拿旧原文给 bot 用。
            if cached is None:
                return None
            title = cached.book

        book_dir = self.shelf.resolve(title)
        if book_dir is None:
            return cached
        files = self.shelf._chapter_files(book_dir)
        if not files:
            return cached

        fingerprint = _fingerprint(files)
        if (
            not force
            and cached is not None
            and cached.book == title
            and cached.fingerprint == fingerprint
        ):
            return cached

        pack = self._build(title, files, fingerprint)
        with self._lock:
            self._cache[rid] = pack
        return pack

    @staticmethod
    def _is_reload(text: str) -> bool:
        return any(m in str(text or "") for m in _RELOAD_MARKS)

    def _build(
        self, title: str, files: Sequence[Path], fingerprint: str
    ) -> MaterialPack:
        # 正文优先；同类内仍按文件名稳定排序，保证跨 bot 字节一致。
        ranked = sorted(files, key=lambda p: (classify(p.name), p.name))
        parts: list[str] = []
        used = 0
        for p in ranked:
            if classify(p.name) == 2:
                continue  # 非创作文本不进正文，只进索引
            if used >= self.max_chars:
                break
            text = guarded_assistance_read(p, max_chars=self.max_chars)
            if not text:
                continue
            room = self.max_chars - used
            body = text[:room]
            used += len(body)
            parts.append(f"《{title}》· {p.name}\n{body}")

        if not parts:
            body = "（这本书里没有可加载的正文材料。）"
        else:
            body = "\n\n".join(parts)

        lines = [f"《{title}》书稿目录（共 {len(files)} 份，按需补读）："]
        for p in ranked:
            label = {0: "正文/小说", 1: "大纲/人物/脑洞", 2: "协议/简介/目录"}[classify(p.name)]
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            lines.append(f"- {p.name}（{label}，{size} 字节）")
        lines.append(
            "需要上面某一份而本次没读到的内容时，用 read_book 的 chapter 参数填"
            "**文件名关键词**（例如 chapter=\"绝命救援\"）单独补读；"
            "chapter 不是路径，填绝对路径不会命中。没读到的部分不要凭空编情节或段号。"
        )
        return MaterialPack(
            book=title,
            fingerprint=fingerprint,
            body=body,
            index="\n".join(lines),
        )


def default_materials(shelf: BookShelf) -> RoomMaterials:
    return RoomMaterials(shelf)
