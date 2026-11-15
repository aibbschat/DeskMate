"""「他这几天亲口说起的」——用户近况事件的记住、闭合与读回。

2026-11-15 萧哥提问：小桔会弹「半小时/1 小时了休息一下」的卡，但那是
``companion.py`` 的分钟阈值 + 模板，**不是主动关心**。真正的主动关心是
「她真的记着，并在聊天中带出来」——他上周五说头痛、进度没完成，周末说补回来了，
隔一天再聊天时，那些事应该在场，她自己决定要不要接、怎么接。

这条模块只做三件事，刻意不做第四件：

1. **记住**：从每轮抽取器拿到 ``episode`` 条目，逐字校验证据后落盘。
2. **闭合**：同主题的新说法能把旧的未了结事件关掉（补回进度 ≠ 头不痛了，
   所以按 ``subject`` 精确匹配，绝不连带闭合）。
3. **读回**：把「还没了结的」和「已经有了结果的」同时作为**真实状态**注入
   每轮上下文（含闲聊轮）。

**不做的**：不生成关心话术、不规定她的表达方式、不做时间/关键词触发、不弹卡。
按「真实优先」红线，系统只给真实状态；她要不要提、用什么口气提，是她的事。

边界（红线）：
- 只记萧盛本人亲口说的近况，**evidence 必须是原话逐字片段**，否则拒写。
- 绝不写 ``USER.md`` / ``MEMORY.md`` / ``learned_history`` / 正文记忆
  （暂时状态不是持久事实，且 USER.md 有归属闸）。独立 json 文件。
- 持久化：``file_write_lock`` + ``atomic_write_text``（多入口写同一文件）。
- 全程 fail-open：读失败返回空串；写失败静默丢弃，绝不影响主对话。
- 开关：``DESKMATE_EPISODES_ENABLED=0`` 整条链关闭。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

from deskmate.growth._atomic import atomic_write_text
from deskmate.memory._fs_lock import file_write_lock

_FILENAME = "agency/user_episodes.json"
_VERSION = 1
_MAX_ITEMS = 40
_MAX_EVIDENCE = 3
_OPEN_TTL_DAYS = 10          # 未了结的事最多挂 10 天，别变成无限期骚扰
_RESOLVED_TTL_DAYS = 4       # 已有结果的再留 4 天，够「隔一天接上」
_DEFAULT_LIMIT = 3


def _enabled() -> bool:
    return os.environ.get("DESKMATE_EPISODES_ENABLED", "1") != "0"


def _path(base_dir: str | Path | None) -> Path:
    if base_dir is None:
        base_dir = Path(os.path.expanduser("~/.deskmate"))
    return Path(base_dir) / _FILENAME


def _norm(text: Any) -> str:
    """逐字校验用的归一：只去空白，不改字、不换同义词。"""
    return re.sub(r"\s+", "", str(text or ""))


def _today() -> str:
    return date.today().isoformat()


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": _VERSION, "items": []}


def read_items(base_dir: str | Path | None = None) -> list[dict]:
    """只读返回全部事件（含已过期，供测试/排障）。生产注入请用 ``build_context``。"""
    try:
        return [i for i in _load(_path(base_dir))["items"] if isinstance(i, dict)]
    except Exception:
        return []


def _write_unlocked(path: Path, data: dict) -> None:
    """只在 ``file_write_lock`` 临界区内调用：同进程重复 flock 会自锁死。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _user_only_text(text: str) -> str:
    """从 session 文本里只取 ``## User`` 段——证据不许引自小桔自己的回复。"""
    parts = re.split(r"(?im)^\s*##\s*(user|assistant|system)\s*$", str(text or ""))
    out: list[str] = []
    for i in range(1, len(parts) - 1, 2):
        if parts[i].strip().lower() == "user":
            out.append(parts[i + 1])
    return "\n".join(out)


def _clean_subject(value: Any) -> str:
    s = str(value or "").strip().strip("「」《》\"'")
    # 主题是给机器做闭合匹配的短锚点，太长说明模型在写句子而不是抽对象。
    return s if 1 < len(s) <= 16 else ""


def _expired(item: dict, today: date) -> bool:
    try:
        last = date.fromisoformat(str(item.get("last_day") or item.get("opened_day") or ""))
    except (ValueError, TypeError):
        return True
    ttl = _RESOLVED_TTL_DAYS if item.get("status") == "resolved" else _OPEN_TTL_DAYS
    return (today - last).days > ttl


def record(
    base_dir: str | Path | None,
    episodes: list[dict] | None,
    *,
    source_text: str = "",
    day: str | None = None,
) -> dict:
    """登记本轮抽到的近况事件；返回 ``{written, closed, skipped}``。

    ``episodes`` 每项形如
    ``{subject, state, status(open|resolved), evidence}``；``evidence`` 必须是
    ``source_text`` 里用户原话的逐字片段（去空白后比对），否则整条拒写。
    """
    result = {"written": 0, "closed": 0, "skipped": 0}
    if not _enabled() or not episodes:
        return result
    stamp = day or _today()
    # 抽取器传进来的可能是 session 全文（含她的回复），只认 User 段。
    user_text = _user_only_text(source_text) or str(source_text or "")
    haystack = _norm(user_text)
    if not haystack:
        return result

    try:
        path = _path(base_dir)
        with file_write_lock(path):
            data = _load(path)
            items: list[dict] = [i for i in data["items"] if isinstance(i, dict)]
            today = date.fromisoformat(stamp) if day else date.today()
            items = [i for i in items if not _expired(i, today)]

            for raw in episodes:
                if not isinstance(raw, dict):
                    result["skipped"] += 1
                    continue
                subject = _clean_subject(raw.get("subject"))
                evidence = str(raw.get("evidence") or "").strip()
                status = str(raw.get("status") or "").strip().lower()
                if status not in ("open", "resolved"):
                    result["skipped"] += 1
                    continue
                if not subject or not evidence:
                    result["skipped"] += 1
                    continue
                if _norm(evidence) not in haystack:
                    # 防编闸门：模型改写/脑补出来的「近况」一条都不收。
                    result["skipped"] += 1
                    continue

                # closes 是抽取器回看「当前还没了结的事」清单后给出的语义锚点：
                # 跨轮 subject 用词会漂移（真实 LLM 实测「周五进度」→「码字进度」），
                # 只按本轮 subject 精确匹配会闭不上，旧欠账永远挂着。命中则只闭
                # 它点名的那一条，绝不连带别的 open 事件。
                closes = _clean_subject(raw.get("closes"))
                exact = [
                    i for i in items
                    if i.get("status") == "open" and i.get("subject") == subject
                ]
                if status == "resolved" and closes:
                    named = [
                        i for i in items
                        if i.get("status") == "open" and i.get("subject") == closes
                    ]
                    open_hits = named or exact
                else:
                    open_hits = exact
                if status == "open" and open_hits:
                    target = open_hits[-1]
                    target["last_day"] = stamp
                    if raw.get("state"):
                        target["state"] = str(raw["state"])[:120]
                    ev = list(target.get("evidence") or [])
                    if evidence not in ev:
                        ev.append(evidence)
                    target["evidence"] = ev[-_MAX_EVIDENCE:]
                    result["written"] += 1
                    continue

                if status == "resolved":
                    for target in open_hits:
                        target["status"] = "resolved"
                        target["resolved_day"] = stamp
                        target["last_day"] = stamp
                        target["resolution"] = evidence
                        result["closed"] += 1
                    if open_hits:
                        result["written"] += 1
                        continue

                items.append({
                    "id": f"{subject}|{stamp}",
                    "subject": subject,
                    "state": str(raw.get("state") or "")[:120],
                    "status": status,
                    "opened_day": stamp,
                    "last_day": stamp,
                    "resolved_day": stamp if status == "resolved" else None,
                    "evidence": [evidence],
                    "resolution": evidence if status == "resolved" else None,
                    "recorded_at": time.time(),
                })
                result["written"] += 1

            # 同主题只留最新一条 open，避免每轮重复堆叠。
            seen: set[str] = set()
            deduped: list[dict] = []
            for item in reversed(items):
                key = (str(item.get("subject")), str(item.get("status")))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            items = list(reversed(deduped))[-_MAX_ITEMS:]
            _write_unlocked(path, {"version": _VERSION, "items": items})
    except Exception:
        return {"written": 0, "closed": 0, "skipped": result["skipped"]}
    return result


def build_context(
    base_dir: str | Path | None = None,
    *,
    limit: int = _DEFAULT_LIMIT,
    now: date | None = None,
) -> str:
    """把近几天他说过的、还没了结 / 刚有结果的事，作为**真实状态**拼成注入文本。

    只陈述「谁、哪天、原话怎么说、后来有没有下文」。不写「你该关心他」、
    不写「请主动问一句」，也不给任何语气/话术——那是她的表达，不是系统的。
    """
    if not _enabled():
        return ""
    try:
        today = now or date.today()
        items = [i for i in read_items(base_dir) if not _expired(i, today)]
        if not items:
            return ""
        # 稳定排序：先按最近提及降序，再按「未了结优先」分组。
        items.sort(key=lambda i: str(i.get("last_day") or ""), reverse=True)
        items.sort(key=lambda i: 0 if i.get("status") == "open" else 1)
        items = items[: max(1, int(limit))]

        lines: list[str] = []
        for item in items:
            ev = [str(x) for x in (item.get("evidence") or []) if str(x).strip()]
            if not ev:
                continue
            day = str(item.get("opened_day") or "")
            head = f"{day} 他说「{ev[-1]}」"
            if item.get("status") == "resolved":
                resolution = str(item.get("resolution") or "")
                rday = str(item.get("resolved_day") or "")
                tail = f"；{rday} 他又说「{resolution}」——这件已经有了结果" if resolution \
                    else "——这件已经有了结果"
            else:
                tail = "——这件事后来没再提"
            lines.append(head + tail)
        if not lines:
            return ""
        header = (
            "## 他这几天亲口说起的（原话；是他知道的事，"
            "不是要你汇报，也不规定你怎么说）"
        )
        return header + "\n" + "\n".join(lines)
    except Exception:
        return ""
