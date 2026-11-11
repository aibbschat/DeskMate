"""对话复盘流水（独立 journal 文件）。

2026-11-11 一段止血：复盘流水从 USER.md 迁出。

原因：写进 USER.md 后，注入时又被 ``strip_auto_review`` 整段剥掉——写了不读，
纯占体积（实测 USER.md 30.7KB、上限 15KB，其中复盘流水约 9.7KB）。现在改成
「单独一份 journal + 注入时真的读最近几天」。

存储：``~/.deskmate/journal/YYYY-MM.md``（按月分文件，避免单文件无限膨胀）
格式：

    ## 2026-11-11

    - 今天定下了烧仓戏的改法
    - [归档] 2026-10-31: 从 USER.md 迁入的旧流水（带标记，摘要跳过）

注入：``recent_digest(days=7)`` 取最近 N 天的日期标题 + 条目（跳过 ``[归档]``），
由 ``DeskMateMemoryManager.build_full_context_for_turn`` 作为「近期复盘」块
放进上下文。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from deskmate.growth._atomic import unique_tmp

from ._fs_lock import file_write_lock

logger = logging.getLogger(__name__)

_HEAD_RE = re.compile(r"^##\s*(\d{4}-\d{2}-\d{2})\s*$")
_DEFAULT_DIGEST_DAYS = 7
_DEFAULT_MAX_CHARS = 1200
_DEFAULT_MAX_ITEMS_PER_DAY = 4

# 迁移历史标记：从 USER.md 搬来的旧流水统一加这个前缀，``recent_digest`` 跳过。
# 新写的复盘正文不带该前缀——这样「迁移历史」与「新写正文」永远分得开，
# 以后再有历史搬进来，沿用同一个前缀即可，不会混进「最近几天」的摘要。
ARCHIVE_PREFIX = "[归档]"


def is_archived(entry: str) -> bool:
    """条目是否属于迁移历史（带 ``[归档]`` 前缀）。"""
    return (entry or "").lstrip().startswith(ARCHIVE_PREFIX)


def _journal_dir(base_dir: str | Path) -> Path:
    return Path(base_dir) / "journal"


def _month_path(base_dir: str | Path, day: date) -> Path:
    return _journal_dir(base_dir) / f"{day:%Y-%m}.md"


def _norm(text: str) -> str:
    """去列表前缀、空白与标点，用于同日去重。"""
    t = re.sub(r"^[-*]\s+", "", (text or "").strip())
    t = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", t)
    return re.sub(r"[\s，。！？；：、,.!?;:]+", "", t).lower()


def _read_entries(path: Path) -> dict[str, list[str]]:
    """解析 journal：{日期: [条目...]}。"""
    result: dict[str, list[str]] = {}
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    current: str | None = None
    for line in lines:
        m = _HEAD_RE.match(line.strip())
        if m:
            current = m.group(1)
            result.setdefault(current, [])
            continue
        if current and line.strip().startswith(("-", "*")):
            entry = re.sub(r"^[-*]\s+", "", line.strip()).strip()
            if entry:
                result[current].append(entry)
    return result


def _render(entries: dict[str, list[str]]) -> str:
    """按日期倒序渲染（最新在最上面）。"""
    blocks = []
    for day in sorted(entries, reverse=True):
        items = entries[day]
        if not items:
            continue
        blocks.append("## %s\n\n%s\n" % (day, "\n".join("- %s" % i for i in items)))
    return "\n".join(blocks).rstrip() + ("\n" if blocks else "")


def append_review(
    base_dir: str | Path,
    lines: list[str],
    day: date | None = None,
) -> int:
    """把复盘条目追加到当月 journal，按日期归并、按内容去重。

    返回实际新增条数。fail-open：任何异常都不影响主流程。
    """
    items = [re.sub(r"^[-*]\s+", "", (t or "").strip()) for t in lines]
    items = [t for t in items if t]
    if not items:
        return 0
    day = day or date.today()
    path = _month_path(base_dir, day)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-11-11 复检修：原来固定 ``.tmp`` 名且无锁，24 线程实测丢 17~18/24，
        # 却仍返回 added>0（上层记「journal +1」成功）。锁必须覆盖整个
        # 读-改-写临界区，否则两个线程都基于旧快照写，后写者覆盖先写者。
        with file_write_lock(path):
            entries = _read_entries(path)
            key = day.isoformat()
            existing = entries.setdefault(key, [])
            seen = {_norm(t) for t in existing}
            added = 0
            for t in items:
                n = _norm(t)
                if not n or n in seen:
                    continue
                seen.add(n)
                existing.append(t)
                added += 1
            if not added:
                return 0
            tmp = unique_tmp(path)
            tmp.write_text(_render(entries), encoding="utf-8")
            os.replace(tmp, path)
            return added
    except Exception:
        logger.warning("journal: 写复盘流水失败", exc_info=True)
        return 0


def recent_digest(
    base_dir: str | Path,
    days: int = _DEFAULT_DIGEST_DAYS,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_items_per_day: int = _DEFAULT_MAX_ITEMS_PER_DAY,
) -> str:
    """生成「近期复盘」注入块；没有数据时返回空串。

    只取最近 ``days`` 天，每天最多 ``max_items_per_day`` 条，整体不超过
    ``max_chars``，避免把 journal 变成新的上下文负担。
    """
    try:
        root = _journal_dir(base_dir)
        if not root.is_dir():
            return ""
        today = date.today()
        earliest = today - timedelta(days=max(0, days - 1))
        wanted = {(earliest + timedelta(days=i)).isoformat() for i in range(days)}
        merged: dict[str, list[str]] = {}
        # 2026-11-11 复检修：原来用「当月 1 号 − 31 天」近似上个月，3 月 1 日
        # 会落到 1 月 29 日，整月漏读 2 月（每年 3/5/7/10/12 月上旬必现）。
        # 改为按 wanted 覆盖的真实月份逐个读文件。
        month = earliest.replace(day=1)
        last_month = today.replace(day=1)
        seen_months: set[date] = set()
        while month <= last_month and month not in seen_months:
            seen_months.add(month)
            path = _month_path(base_dir, month)
            for day, items in _read_entries(path).items():
                if day in wanted and items:
                    merged[day] = items
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        if not merged:
            return ""
        lines = ["## 近期复盘（她记得的最近几天）", ""]
        for day in sorted(merged, reverse=True):
            # 跳过迁移历史（[归档]），让摘要回到「最近几天真实复盘」的用途。
            items = [i for i in merged[day] if not is_archived(i)][:max_items_per_day]
            if not items:
                continue
            body = "；".join(items)
            lines.append("- %s：%s" % (day[5:], body))
        if len(lines) <= 2:
            return ""
        text = "\n".join(lines).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text
    except Exception:
        logger.debug("journal: 读取复盘摘要失败", exc_info=True)
        return ""
