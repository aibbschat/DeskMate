"""写作状态上下文 + 多书并行门禁 + 按书名的负反馈冷却。

背景（2026-10-24）：后台 tick 创建 ProactiveEngine 时不传 workspace，memory /
project 上下文也是空，judge 看不到手稿与最近改动，只能凭情绪判断。本模块只读
文件名与 mtime，把「哪本书最近动过笔」喂给 judge；解析失败一律 fail-open。

2026-11-05 事故（P0/P1）：萧哥当天 17:53 刚存盘《夜不收》，右上角却反复弹
《国子监秘闻录》7 天没动笔。数字没错（国子监确 7.18 天），错在口径——
旧版把 6 本书按 mtime 平铺，系统里没有「当前在写哪本」这个概念，于是
「换了本」被翻译成「这本搁置了，温柔地推一下」。同一天台账里 4 条、
disturb_state 里 15+ 条同主题卡片，忽略反馈又按「正文前 100 字」去重，
措辞一变冷却就失效。

本模块现在是这条链路的**单一事实源**：
- ``scan_writing_books`` / ``current_writing_book``：结构化算出当前在写项目；
- ``build_writing_status``：给 judge 的文本显式标「当前在写」/「其他项目·搁置」；
- ``guard_writing_suggestion``：硬门禁，存在今天/昨天动笔的书时，禁止产出
  针对其他书的停笔/提醒/关怀卡（不靠模型自觉）；
- ``record_book_feedback`` / ``is_book_cooled``：负反馈按**书名对象**冷却 7 天，
  措辞怎么变都命中。冷却存在 ``deskmate.db`` 的 ``proactivity_meta``（SQL
  upsert 原子、单键互不覆盖），不写易被多进程全量覆盖的 disturb_state.json。

读物一律只读；唯一的写入是运行时数据目录里的冷却簿记，绝不碰稿件正文。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_MANUSCRIPT_EXTS = (".docx", ".md")
_BOOK_DIR_PREFIX = re.compile(r"^\d+\s*[.、]\s*")
_DEFAULT_ROOT = "~/Documents/已出版"

# 今天/昨天动过笔的书 = 当前在写。超过这个窗口就没有「当前项目」，
# 此时才允许按停笔判断（避免把真停笔也一并拦掉）。
CURRENT_BOOK_WINDOW_DAYS = 2.0
# 被忽略的书冷却 7 天：这段时间里任何针对它的催写/关怀都不再弹。
BOOK_COOLDOWN_SECONDS = 7 * 86400
_COOLDOWN_PREFIX = "writing_cooldown:"

# 书名（《书名》）抽取；不带书名号时由 known_names / 冷却表里的对象名做子串匹配。
_ANGLE_TITLE_RE = re.compile(r"《([^》]{1,40})》")

# 「催写/停笔」语义线索：必须同时命中「提到非当前在写的书」+ 这类线索才拦，
# 免得误伤针对别的书的真实情节矛盾/伏笔发现。
# 另有类型兜底：emotional_care / writing_stall 这类「拿旧书搭话」的卡片，
# 即使一句「没动笔」都没写（如「那边我也在，随时陪你捋情节」），仍属 P0 要挡的
# 关怀式提醒，见 ``_STALL_KINDS``。
_STALL_KINDS = frozenset(
    {"writing_stall", "emotional_care", "task_reminder", "writing_review", "generic"}
)
_STALL_CUE_RE = re.compile(
    r"没动笔|未动笔|没有动笔|没碰|没见你|没动静|没动它|没见它|"
    r"停笔|停了|停[一二三四五六七八九十两0-9]+\s*天|歇|搁置|"
    r"回去写|接着写|回去翻|翻翻旧稿|翻旧稿|找回感觉|找找手感|"
    r"卡在哪|卡在|惦记|催你|不催|慢慢来|等你哪天|"
    r"一周没|几天没|有段日子|[0-9一二三四五六七八九十两]+\s*天(?:没|未|不|没有)|"
    r"催|进度",
)


def _book_name(dir_name: str) -> str:
    return _BOOK_DIR_PREFIX.sub("", dir_name).strip() or dir_name


def resolve_writing_workspace(explicit=None, base_dir=None):
    """解析写作工作区目录，优先级：显式参数 > 环境变量 > ingest_dirs.json 首项 > 默认根。

    返回 Path 或 None（都不存在时）。调用方不得假设返回值非空。
    """
    try:
        candidates: list[str] = []
        if explicit:
            candidates.append(str(explicit))
        env = (os.environ.get("DESKMATE_WRITING_WORKSPACE") or "").strip()
        if env:
            candidates.append(env)
        if base_dir is not None:
            try:
                ingest = Path(base_dir).expanduser() / "ingest_dirs.json"
                if ingest.is_file():
                    data = json.loads(ingest.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        candidates.extend(str(x) for x in data if x)
            except Exception:
                pass
        candidates.append(_DEFAULT_ROOT)
        for raw in candidates:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p
    except Exception:
        pass
    return None


def _newest_activity(book_dir: Path):
    """书名目录下最新一个手稿类文件的 (mtime, 文件名)；无则 None。

    ~$ 锁文件（Word 打开时的临时文件）与隐藏文件不算活动——开着 ≠ 在写。
    """
    newest = None
    try:
        for p in book_dir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.startswith("~$") or name.startswith("."):
                continue
            if p.suffix.lower() not in _MANUSCRIPT_EXTS:
                continue
            m = p.stat().st_mtime
            if newest is None or m > newest[0]:
                newest = (m, name)
    except OSError:
        return None
    return newest


@dataclass(frozen=True)
class WritingBook:
    """一本书的只读观测。"""
    name: str
    mtime: float
    filename: str


def scan_writing_books(workspace, *, now: float | None = None) -> list[WritingBook]:
    """扫工作区，返回按最近动笔倒序的书列表；任何异常返回空列表（fail-open）。

    与 health_producers._scan_books 的口径一致（一级子目录、.docx/.md、
    排除 ~$ 与隐藏文件），但返回结构化对象，供门禁与状态文本共用同一份事实。
    """
    try:
        if workspace is None:
            return []
        root = Path(workspace).expanduser()
        if not root.is_dir():
            return []
        books: list[WritingBook] = []
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            act = _newest_activity(d)
            if act is None:
                continue
            books.append(WritingBook(_book_name(d.name), act[0], act[1]))
        books.sort(key=lambda b: b.mtime, reverse=True)
        return books
    except Exception:
        return []


def current_writing_book(
    books: list[WritingBook], *, now: float | None = None
) -> WritingBook | None:
    """最近动笔且仍在 2 天窗口内的那本 = 当前在写；否则 None（没有当前项目）。"""
    if not books:
        return None
    cur = max(books, key=lambda b: b.mtime)
    ref = time.time() if now is None else now
    return cur if (ref - cur.mtime) / 86400.0 < CURRENT_BOOK_WINDOW_DAYS else None


def format_writing_status(
    books: list[WritingBook], *, limit: int = 6, now: float | None = None
) -> str:
    """把结构化书列表格式化成 judge 能用的短状态（总长截断 800 字）。"""
    if not books:
        return ""
    ref = time.time() if now is None else now
    cur = current_writing_book(books, now=ref)
    lines = ["写作状态（手稿文件修改时间，只读统计）："]
    for b in books[:limit]:
        days = (ref - b.mtime) / 86400.0
        if days < 1:
            when = "今天动过笔"
        elif days < 2:
            when = "昨天动过笔"
        else:
            when = f"{int(days)} 天没动笔"
        if cur is not None and b.name == cur.name:
            lines.append(f"- 当前在写：《{b.name}》：{when}（{b.filename}）")
        elif cur is not None:
            # 保留旧口径「N 天没动笔」字面，但明确它只是切换后的其他项目
            lines.append(f"- 其他项目·搁置：《{b.name}》：{when}（{b.filename}）")
        else:
            lines.append(f"- 《{b.name}》：{when}（{b.filename}）")
    if cur is not None:
        lines.append(
            "- 提示：多书并行时「其他项目·搁置 N 天」只表示换了本，不是中断或卡住；"
            "有当前在写项目时不要催其他书，也不要把停笔当作关怀理由。"
        )
    else:
        lines.append("- 提示：最近两天没有书动过笔，若确有停笔可正常判断。")
    return "\n".join(lines)[:800]


def build_writing_status(workspace, *, limit: int = 6, now: float | None = None) -> str:
    """兼容旧入口：扫工作区并格式化；扫不到返回空串（fail-open）。"""
    return format_writing_status(
        scan_writing_books(workspace), limit=limit, now=now
    )


def manuscript_location_context(
    workspace, *, limit: int = 4, now: float | None = None
) -> str:
    """「萧哥的稿子在哪」固定注入块（刀1，2026-11-10 萧哥拍板）。

    病灶（2026-11-10 晚实测）：正稿路径知识只在 learned_history 里靠 query
    2-gram 召回，而萧哥日常消息（「你去看一下」）带不上路径词 → 每轮注入两手
    空空，小桔只能全盘乱翻（还翻到过桌面同名旧快照）。本块**不依赖对话关键词**：
    只读扫描写作工作区（与 ``scan_writing_books`` 同口径：一级子目录、
    .docx/.md、排除 ~$ 与隐藏文件），给出书稿根目录 + 当前在写正稿的**绝对路径**
    + 最近动笔时间。绝不写稿件（只 stat）。

    fail-open：workspace 为空/异常一律返回空串，不阻断注入。
    """
    try:
        if workspace is None:
            return ""
        root = Path(workspace).expanduser()
        if not root.is_dir():
            return ""
        ref = time.time() if now is None else now
        found: list[tuple[float, str, str]] = []  # (mtime, 书名, 正稿绝对路径)
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            act = _newest_activity(d)
            if act is None:
                continue
            found.append((act[0], _book_name(d.name), str(d / act[1])))
        if not found:
            return ""
        found.sort(key=lambda x: x[0], reverse=True)
        cur = (
            found[0]
            if (ref - found[0][0]) / 86400.0 < CURRENT_BOOK_WINDOW_DAYS
            else None
        )

        def _when(m: float) -> str:
            days = (ref - m) / 86400.0
            if days < 1:
                return "今天动过笔"
            if days < 2:
                return "昨天动过笔"
            return f"{int(days)} 天没动笔"

        lines = [
            "## 萧哥的稿子在哪（系统实时探测，非记忆；读稿/报进度直接用）",
            f"- 书稿根目录：{root}（每本书一个子目录，稿件只读；"
            "读 docx 用 officecli view 并带 TMPDIR=<可写目录>）",
        ]
        if cur is not None:
            lines.append(
                f"- 当前在写：《{cur[1]}》正稿={cur[2]}（{_when(cur[0])}）"
                "——读稿、对进度一律以此路径为准，别处的同名文件不是正稿"
            )
            for m, name, path in found[1:limit]:
                lines.append(
                    f"- 其他书：《{name}》{path}（{_when(m)}；换书在写 ≠ 停笔/卡住）"
                )
        else:
            m, name, path = found[0]
            lines.append(f"- 最近动笔的书：《{name}》{path}（{_when(m)}）")
            for m, name, path in found[1:limit]:
                lines.append(f"- 其他书：《{name}》{path}（{_when(m)}）")
        return "\n".join(lines)[:800]
    except Exception:
        return ""


def _name_aliases(name: str) -> list[str]:
    """书名别名：去掉副标题 + 中文长书名的前三字简称。

    11-05 实测有卡片写「国子监那边不急，慢慢来」，用简称绕过了只认全名的
    匹配；门禁与冷却因此都要认简称。三字门槛避免「明朝」「大明」这类两字
    通用词把不相干的书一起算上。
    """
    aliases = [name]
    for sep in ("：", ":", "·", "—"):
        head = name.split(sep)[0].strip()
        if len(head) >= 2 and head != name:
            aliases.append(head)
    if len(name) >= 5:
        head = name[:3]
        if head not in aliases:
            aliases.append(head)
    return aliases


def extract_book_titles(text: str, known_names=()) -> set[str]:
    """从文本里认出书名：《...》优先，再用已知书名做子串兜底。"""
    found = {m.strip() for m in _ANGLE_TITLE_RE.findall(text or "") if m.strip()}
    for name in known_names:
        if not name:
            continue
        if any(alias and alias in (text or "") for alias in _name_aliases(name)):
            found.add(name)
    return found


def guard_writing_suggestion(
    *,
    content: str,
    reasoning: str,
    suggestion_type: str,
    books: list[WritingBook],
    now: float | None = None,
    forced_self_expression: bool = False,
) -> str:
    """P0 硬门禁：返回拦截原因（非空=应拦）；空串=放行。

    语义：存在今天/昨天动过笔的书时，「另一本 N 天没动笔」是**切换项目**，
    不是中断。此时任何点名了其他书、且带停笔/催写/关怀语义的建议一律拦下，
    不交给模型自觉。反过来，不点名书名的日常关怀、针对别的书的真实情节
    矛盾发现、以及她自己的心情表达，都不受影响。
    """
    try:
        if forced_self_expression:
            return ""  # 真实优先红线：她自己的表达不受打扰节奏约束
        cur = current_writing_book(books, now=now)
        if cur is None:
            return ""  # 没有当前项目 → 可能是真停笔，不拦
        text = f"{content or ''}\n{reasoning or ''}"
        # 语义线索命中，或本身就是「拿某本书搭话」的催写/关怀类型 → 才继续判对象。
        # 真实情节矛盾（plot_contradiction 等）不在此列，不会被误拦。
        if not (
            _STALL_CUE_RE.search(text)
            or str(suggestion_type or "") in _STALL_KINDS
        ):
            return ""
        others = [
            b.name
            for b in books
            if b.name != cur.name
            and any(alias and alias in text for alias in _name_aliases(b.name))
        ]
        if not others:
            return ""
        return (
            f"当前在写《{cur.name}》（今天/昨天动过笔），"
            f"《{others[0]}》只是切换后的其他项目，不是中断；"
            "禁止针对它发起停笔提醒/回去写/翻旧稿/关怀式催写"
        )
    except Exception:
        return ""  # fail-open：门禁自身绝不阻断主动推理


# ---------------------------------------------------------------------------
# P1：按书名对象的负反馈冷却（写 deskmate.db 的 proactivity_meta）
# ---------------------------------------------------------------------------


def _meta_connect(base_dir):
    """打开运行时库并自举 proactivity_meta（与 health_producers 共用同一张表）。"""
    base = Path(base_dir or "~/.deskmate").expanduser()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS proactivity_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    return conn


def _known_book_names() -> list[str]:
    """反馈文本没带书名号时的兜底：从写作工作区读已知书名（只读、fail-open）。"""
    try:
        ws = resolve_writing_workspace()
        return [b.name for b in scan_writing_books(ws)]
    except Exception:
        return []


def record_book_feedback(
    *,
    text: str,
    accepted: bool,
    suggestion_type: str,
    base_dir,
    now: float | None = None,
) -> list[str]:
    """记录一次反馈到「书名」这个对象上，返回受影响书名（排序）。

    - 忽略：该书冷却 7 天；下次措辞再变也认得出是同一本书。
    - 接受：清掉冷却（他愿意聊这本书）。
    - 认不出书名的通用关怀：不记（noop），避免把无关对象一起冷却。

    每条记录的 value 里带 ``object`` / ``suggestion_type`` / ``accepted`` /
    ``expires_at``，即「feedback 带类型与对象」的凭证；SQL 单键 upsert 天然
    原子，不会被别的 tick 的全文件写回覆盖。
    """
    try:
        # 《书名》是强信号，优先用；只有完全没写书名号时才退回已知书名做子串兜底，
        # 免得把工作区里恰好是别的书名子串的对象一起冷却。
        names = {m.strip() for m in _ANGLE_TITLE_RE.findall(text or "") if m.strip()}
        if not names:
            names = extract_book_titles(text, _known_book_names())
        if not names:
            return []
        ref = time.time() if now is None else now
        conn = _meta_connect(base_dir)
        try:
            for name in sorted(names):
                key = f"{_COOLDOWN_PREFIX}{name}"
                if accepted:
                    conn.execute("DELETE FROM proactivity_meta WHERE key=?", (key,))
                else:
                    value = json.dumps(
                        {
                            "object": name,
                            "suggestion_type": str(suggestion_type or ""),
                            "accepted": False,
                            "recorded_at": ref,
                            "expires_at": ref + BOOK_COOLDOWN_SECONDS,
                            "source_text": (text or "")[:200],
                        },
                        ensure_ascii=False,
                    )
                    conn.execute(
                        "INSERT INTO proactivity_meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
            conn.commit()
        finally:
            conn.close()
        return sorted(names)
    except Exception:
        return []  # fail-open：反馈记录失败绝不阻断 tick / HTTP 端点


def is_book_cooled(text: str, base_dir, *, now: float | None = None) -> bool:
    """文本点到的书名是否仍在冷却期（7 天）；任何异常返回 False（不误拦）。"""
    try:
        ref = time.time() if now is None else now
        conn = _meta_connect(base_dir)
        try:
            rows = conn.execute(
                "SELECT key, value FROM proactivity_meta WHERE key LIKE ?",
                (_COOLDOWN_PREFIX + "%",),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return False
        for key, value in rows:
            name = key[len(_COOLDOWN_PREFIX):]
            try:
                meta = json.loads(value)
            except Exception:
                continue
            if float(meta.get("expires_at") or 0) <= ref:
                continue
            if any(alias and alias in (text or "") for alias in _name_aliases(name)):
                return True
        return False
    except Exception:
        return False
