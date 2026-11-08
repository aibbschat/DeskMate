"""小桔既有沉淀的只读取用 —— 群里多放一份她已经读过的文件。

萧哥 11-06 21:25 定调：书桶补集到 155 样本 / 435 万字后，这些积累是**她每天
读过、逐日维护、早已落盘的现成经验**；借它补群聊「不知道作者今天写到哪」的
缺陷，与圆桌「本次现场跑 LLM 抽成有损摘要」性质不同。

这里**什么都不生成**：不抽取、不改写、不概括，只把既有文件原样搬进群共享
材料。零 LLM 调用，全程只读且经过 ``sacred_paths`` 守卫：

- ``<书>/.deskmate`` 下四份圣典、最新一份进度简报、``style_feeds`` 最新一份
  逐字留档，以及独立成文的 ``AUTHOR_RULING.md``（作者裁定，效力最高、
  不随 entities.db 刷新被覆盖）；备份目录 / 快照 / db 一律排除。
- ``style_profile`` 的作者整体文风（155 份书稿样本统计出的既有字段，原样搬运，
  标明是跨书全局而非某本书专属）。
- 最新逐字留档**尾部优先**：停笔点在那里，不在开头。群里问「接下来怎么写」
  时，这一句比第一章有用得多。

预算独立有界（``DESKMATE_BOT_EXPERIENCE_CHARS``，默认 12000 字），按
「作者裁定 → 文风 → 最新简报 → 最新留档尾部 → 圣典」的优先级确定性截断；
装不下的文件进索引，bot 可用 ``read_book(chapter=文件名关键词)`` 按名补读。
截断不是压缩：没进上下文的部分不会被总结成一句话，只会明确告诉 bot
还有哪些文件可点名。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from deskmate.sacred_paths import guarded_assistance_read, guarded_dir_entry

logger = logging.getLogger(__name__)

_BIBLE_FILES = frozenset({"CHARACTERS.MD", "NOTES.MD", "OUTLINE.MD", "TIMELINE.MD"})
# 作者裁定：效力**高于**四圣典，且独立成文、不随 entities.db 刷新被覆盖。
# 圣典只收「已写正文事实」，作者裁定管「往后怎么走」，两者物理分离。
_RULING_FILE = "AUTHOR_RULING.MD"
_BRIEF_MARK = "简报"
_FEED_PREFIX = "新增正文"
_FEED_DIR = "style_feeds"
_SKIP_MARKS = ("_backup_", "_snapshots", "__pycache__")
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text"}
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_DEFAULT_EXPERIENCE_CHARS = 12_000
# 预算再小也要给停笔点留位置：最新留档优先取尾部这么多字。
_FEED_TAIL_CHARS = 2_500


@dataclass(frozen=True)
class ExperienceBlock:
    """一次经验层备料的产物。``fingerprint`` 纳入全部候选文件与文风统计版本。"""

    text: str
    files: tuple[Path, ...]
    fingerprint: str
    index_lines: tuple[str, ...]
    has_any: bool


def load_style_profile() -> dict:
    """读取既有文风画像（BlobStore）。只在这里碰用户库，失败一律返回空字典。"""
    try:
        from deskmate.style_profile_store import DEFAULT_PROFILE_PATH, load_profile

        return load_profile(DEFAULT_PROFILE_PATH) or {}
    except Exception as exc:  # noqa: BLE001 - 备料绝不能因画像读取失败而中断
        logger.warning("文风画像读取失败（经验层跳过文风段）: %s", exc)
        return {}


def _date_of(p: Path) -> str:
    m = _DATE_RE.search(p.name)
    return m.group(1) if m else ""


def _sort_key(p: Path) -> tuple[str, int, str]:
    try:
        mtime = int(p.stat().st_mtime)
    except OSError:
        mtime = 0
    return (_date_of(p), mtime, p.name)


def experience_files(book_dir: Path) -> list[Path]:
    """列出可作经验材料的既有文件（最新在前）。不做内容判断、不读正文。

    只认四类：作者裁定、四份圣典、简报、style_feeds 的逐字留档。目录名含
    ``_backup_`` / ``_snapshots`` 的一律跳过，db / 二进制被扩展名白名单挡掉。
    """
    try:
        bible = Path(book_dir).expanduser().resolve() / ".deskmate"
    except (OSError, RuntimeError, ValueError):
        return []
    if not bible.is_dir() or not guarded_dir_entry(bible, allow_discovery=True):
        return []
    try:
        candidates = sorted(bible.rglob("*"), key=lambda p: p.as_posix())
    except OSError:
        return []
    out: list[Path] = []
    for p in candidates:
        if not p.is_file() or p.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            rel = p.relative_to(bible)
        except ValueError:
            continue
        parts = rel.parts
        if any(part.startswith(".") or any(m in part for m in _SKIP_MARKS) for part in parts):
            continue
        upper = p.name.upper()
        is_bible = upper in _BIBLE_FILES and len(parts) == 1
        is_ruling = upper == _RULING_FILE and len(parts) == 1
        is_brief = _BRIEF_MARK in p.name and upper.endswith(".MD")
        is_feed = p.name.startswith(_FEED_PREFIX) and _FEED_DIR in parts
        if not (is_bible or is_ruling or is_brief or is_feed):
            continue
        if not guarded_dir_entry(p):
            continue
        out.append(p.resolve())
    return sorted(out, key=_sort_key, reverse=True)


def _latest(files: Sequence[Path]) -> Optional[Path]:
    return max(files, key=_sort_key) if files else None


def _fingerprint(files: Sequence[Path], profile: dict) -> str:
    parts = []
    for p in files:
        try:
            st = p.stat()
            parts.append(f"{p.as_posix()}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{p.as_posix()}:?:?")
    bucket = (profile.get("buckets") or {}).get("manuscript") or {}
    style_ver = "|".join(
        str(bucket.get(k) or profile.get(k) or "")
        for k in ("updates", "chars")
    )
    return "|".join(parts) + "|style:" + style_ver


def _format_style(profile: dict) -> str:
    """把既有文风字段原样摆出来，不概括、不改写、不新增判断。"""
    if not profile:
        return ""
    head = profile.get("profile") if isinstance(profile.get("profile"), dict) else {}
    summary = str(profile.get("summary") or head.get("summary") or "").strip()
    feats = profile.get("features") or head.get("features") or {}
    bucket = (profile.get("buckets") or {}).get("manuscript") or {}
    updates = bucket.get("updates") or profile.get("updates") or 0
    chars = bucket.get("chars") or profile.get("chars") or 0
    metrics = bucket.get("metrics") or {}
    if not (summary or feats or metrics):
        return ""
    lines = [
        f"# 作者整体文风（小桔据 {updates} 份书稿样本 / {chars} 字统计；"
        "跨书全局，非某一本书专属，不要当成该书的设定）"
    ]
    if summary:
        lines.append(summary)
    labels = {
        "sentence_style": "句式", "register": "语体", "rhythm": "节奏",
        "word_preference": "用词", "taboos": "忌",
    }
    for key, label in labels.items():
        val = str(feats.get(key) or "").strip()
        if val:
            lines.append(f"- {label}：{val}")
    sent = metrics.get("sentences") or {}
    four = metrics.get("four_char") or {}
    bits = []
    if sent.get("short_pct") is not None:
        bits.append(f"短句 {sent['short_pct']}%")
    if four.get("pct_of_chunks") is not None:
        bits.append(f"四字格 {four['pct_of_chunks']}%")
    if metrics.get("dialogue_pct") is not None:
        bits.append(f"对话 {metrics['dialogue_pct']}%")
    if metrics.get("classical_per_1k") is not None:
        bits.append(f"文言 {metrics['classical_per_1k']}‰")
    if bits:
        lines.append("- 量化：" + " / ".join(bits))
    return "\n".join(lines)


def _take(text: str, remaining: int, *, tail_first: bool = False) -> tuple[str, int, bool]:
    """确定性截断。返回 (正文, 剩余预算, 是否被截)。绝不生成替代文本。"""
    if remaining <= 0 or not text:
        return "", remaining, False
    if len(text) <= remaining:
        return text, remaining - len(text), False
    if tail_first:
        keep = max(1, remaining - 8)
        return "（前面略）\n" + text[-keep:], 0, True
    return text[:remaining] + "\n（后略）", 0, True


def _feed_text(raw: str) -> str:
    """最新留档以停笔点（尾部）为先；有富余预算时补回开头，仍是逐字原文。"""
    if len(raw) <= _FEED_TAIL_CHARS:
        return raw
    return raw[:1000] + "\n……（中段略）……\n" + raw[-_FEED_TAIL_CHARS:]


def collect_experience(
    book_dir: Path,
    *,
    max_chars: int = _DEFAULT_EXPERIENCE_CHARS,
    style_loader: Callable[[], dict] | None = None,
) -> ExperienceBlock:
    """群级一次备料：把小桔既有的沉淀原样摆进材料，零 LLM、纯本地只读 IO。

    没有 ``.deskmate`` 沉淀的书直接返回空块，**不读文风画像**（避免给普通书
    引入对用户库的隐式依赖，也让离线测试完全隔离）。
    """
    files = experience_files(book_dir)
    if not files:
        return ExperienceBlock(text="", files=(), fingerprint="", index_lines=(), has_any=False)

    loader = style_loader or load_style_profile
    profile = loader() or {}
    fingerprint = _fingerprint(files, profile)

    try:
        bible = Path(book_dir).expanduser().resolve() / ".deskmate"
    except (OSError, RuntimeError, ValueError):
        bible = None
    canons = [p for p in files if p.name.upper() in _BIBLE_FILES and p.parent == bible]
    rulings = [p for p in files if p.name.upper() == _RULING_FILE and p.parent == bible]
    briefs = [p for p in files if _BRIEF_MARK in p.name]
    feeds = [p for p in files if p.name.startswith(_FEED_PREFIX) and _FEED_DIR in p.parts]
    brief = _latest(briefs)
    feed = _latest(feeds)
    ruling = _latest(rulings)

    remaining = max(200, int(max_chars))
    sections: list[str] = []
    # 作者裁定最优先：它管「往后怎么走」，效力高于已写正文事实、旧简报与
    # 任何讨论共识；旧大纲／死序／圆桌共识与它冲突时一律以它为准。
    if ruling is not None:
        raw = guarded_assistance_read(ruling) or ""
        if raw:
            body, remaining, _ = _take(raw, remaining)
            if body:
                sections.append(
                    f"## 作者裁定（现行唯一有效口径，优先于下方一切材料） · {ruling.name}\n{body}"
                )
    style = _format_style(profile)
    if style:
        body, remaining, _ = _take(style, remaining)
        sections.append(body)

    for label, path in (
        ("最新进度简报（小桔逐日维护，原样）", brief),
        ("最新写下的正文（逐字留档，末尾即停笔处）", feed),
    ):
        if path is None:
            continue
        raw = guarded_assistance_read(path) or ""
        if not raw:
            continue
        if label.startswith("最新写下"):
            raw = _feed_text(raw)
        body, remaining, _ = _take(
            raw, remaining, tail_first=label.startswith("最新写下")
        )
        if body:
            sections.append(f"## {label} · {path.name}\n{body}")

    for path in canons:
        if remaining <= 0:
            break
        raw = guarded_assistance_read(path) or ""
        if not raw:
            continue
        body, remaining, _ = _take(raw, remaining)
        if body:
            sections.append(f"## 圣典 · {path.name}（每条附正文引文）\n{body}")

    index_lines = []
    if bible is not None:
        for p in files:
            try:
                rel = p.relative_to(bible).as_posix()
                size = p.stat().st_size
            except (ValueError, OSError):
                continue
            index_lines.append(
                f"- .deskmate/{rel}（小桔既有沉淀，{size} 字节，"
                "可用 read_book 的 chapter 填文件名关键词补读）"
            )
    index_lines.append(
        "以上是小桔每天读过并已经落盘的原文 / 事实 / 进度判断，不是本次生成的"
        "纪要或摘要；作者整体文风为跨书全局统计，非某一本书的设定。"
    )
    return ExperienceBlock(
        text="\n\n".join(sections),
        files=tuple(files),
        fingerprint=fingerprint,
        index_lines=tuple(index_lines),
        has_any=True,
    )
