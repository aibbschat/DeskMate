"""作者基础档案 —— 从既有 USER.md + 书架动态拼，不新建记忆层（2026-11-09）。

萧哥要求：专家「知道我是谁、是干什么的、写了哪些书」。我们不再给专家挂共享记忆
（§29），所以这里只做**最小事实注入**：
- 名字/身份从 `~/.deskmate/USER.md` 的画像里取（已有就不重复存）；
- 出版作品从书架动态列（`~/Documents/已出版`，不写死书单）；
- 只注入给创作/分析专家（有自己记忆的小桔/主 Coworker 不重复注入）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .reading import BookShelf, default_shelf

#: 需要「知道作者是谁」的专家 persona（与 Bot 群名册 / 专家中心同源）。
EXPERT_PERSONA_IDS: frozenset[str] = frozenset({
    "brainstormer", "lit-editor", "adaptation", "historian",
    "proofreader", "reader", "publisher", "genre", "editor",
    "outline", "plot", "character",
})

_USER_MD = Path("~/.deskmate/USER.md")
_MAX_BOOKS = 24


def _user_md_text() -> str:
    try:
        return _USER_MD.expanduser().read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _name_and_line(text: str) -> tuple[str, str]:
    """从 USER.md「基本信息」的第一条 bullet 里取名字与一句身份。

    带日期的 bullet 是硬规矩（如「2026-10-31: 萧盛要求——…」），不是身份行；
    USER.md 硬规矩排在前面时，先前实现会把规矩当身份拼进档案（实测：专家拿到的
    「作者身份」是代笔规矩那一句），跳过带日期的行再认。
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        if not body or "萧" not in body:
            continue
        if re.match(r"\d{4}-\d{2}-\d{2}", body):
            continue
        name = "萧盛"
        head = re.split(r"[，,；;：:]", body, maxsplit=1)
        if head and 1 <= len(head[0].strip()) <= 6:
            name = head[0].strip()
        return name, body
    return "萧盛", "写作者（历史小说作家）"


def author_profile(shelf: Optional[BookShelf] = None) -> str:
    """给专家看的一页作者档案；读不到来源时给最小可用版本。"""
    text = _user_md_text()
    name, first = _name_and_line(text)
    identity = first
    if len(identity) > 180:
        identity = identity[:180] + "……"
    try:
        books = (shelf or default_shelf()).list_books()
    except Exception:  # noqa: BLE001 —— 书架读不到不影响档案主体
        books = []
    book_line = ""
    if books:
        shown = "、".join(books[:_MAX_BOOKS])
        more = f" 等 {len(books)} 本" if len(books) > _MAX_BOOKS else f"（共 {len(books)} 本）"
        book_line = f"- 书架上能看到的作品：{shown}{more}。"
    return "\n".join(
        part for part in (
            "# 作者（你服务的人，事实性背景，不是小说设定）",
            f"- {identity}",
            book_line,
            "- 他的小说正文亲手写，AI 不代笔；你负责查资料、理思路、给建议。",
            "- 不确定的事实别编；以正文和作者最新裁定为准。",
        ) if part
    )


def author_profile_for(persona_id: str) -> str:
    """只给创作/分析专家注入；小桔等有自己记忆的 persona 返回空。"""
    pid = str(persona_id or "").strip()
    if pid not in EXPERT_PERSONA_IDS:
        return ""
    return author_profile()
