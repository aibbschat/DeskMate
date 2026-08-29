"""P2-1 后台自动提炼（对标 ChatGPT Dreaming）。

空闲时对「你的记忆」（USER.md + MEMORY.md 分节）做静默提炼：
- ① 同主题近重复 → 去重（保留较新 / 较完整的一条，其余删冗余）
- ② 同主题异值（矛盾 / 过时更新）→ 保留最新；旧值若超龄则迁 vault（可恢复），
     未超龄则标注「已由 X 的更新覆盖」，绝不静默丢失
- ③ 超龄且被新条目覆盖的主题 → 迁 vault（复用 decay_memory 的归档通道）

设计原则（与 P0-5/6/7 一脉相承）：
- 零 LLM、确定性、fail-open：任何异常都吞掉，绝不阻塞主流程。
- 幂等：对已干净的记忆再跑 → 0 改动。
- 只动「你的记忆」，**绝不碰书稿记忆**（P0-6 刚分干净，保持边界）。
- 可恢复：改动前写 `<file>.bak`；破坏性动作只走 vault（可回查 traces/dream.jsonl）。
"""

from __future__ import annotations

import datetime
import re
import time
from pathlib import Path

from .manager import DeskMateMemoryManager as MemoryManager
from .user_profile import load_user, save_user

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
DREAM_INTERVAL_DAYS = 1          # 两次提炼最小间隔
_STALE_SUPERSEDE_DAYS = 120      # 超此龄且被新条目覆盖 → 迁 vault
_MERGE_JACCARD = 0.9             # 近重复判定阈值（近乎完全相同才删，零风险）
_SUPERSEDED_MARK = "（已被更新覆盖，旧值保留备查）"  # 矛盾非超龄时的标注
# 值翻转字：任一方含而另一方不含 → 视为矛盾而非重复（防「喜欢」vs「不喜欢」被误删）
_FLIP_CHARS = set("不没否非别勿未无有是")
_MARKER = ".deskmate_dream_last_run"
_REPORT_FILE = "dream_report.md"
DREAM_TRACE_FILE = "dream.jsonl"

# 不被提炼动到的分节（偏好 / 纠正永保真，等价于 decay 的永不衰减规则）
_PROTECTED_SECTIONS = {"preference", "correction", "偏好", "纠正"}


# ---------------------------------------------------------------------------
# 间隔守卫（镜像 curator._should_run）
# ---------------------------------------------------------------------------
def _should_run(base_dir: Path | str) -> bool:
    """距上次提炼超过 DREAM_INTERVAL_DAYS 天才运行。"""
    base = Path(base_dir)
    marker = base / _MARKER
    try:
        if not marker.is_file():
            return True
        last = float(marker.read_text(encoding="utf-8").strip() or "0")
        return (time.time() - last) >= DREAM_INTERVAL_DAYS * 86400
    except Exception:
        return True


def _mark_run(base_dir: Path | str) -> None:
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        (Path(base_dir) / _MARKER).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 文本工具（确定性，零 LLM）
# ---------------------------------------------------------------------------
_CN = re.compile(r"[一-鿿]")
_EN = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    """中文 2–3 字 n-gram + 英文词，作为条目指纹。"""
    text = text.strip()
    toks: set[str] = set()
    for w in _EN.findall(text):
        if len(w) >= 2:
            toks.add(w.lower())
    # 中文窗口 n-gram（2/3 字）
    cn = "".join(_CN.findall(text))
    for n in (3, 2):
        for i in range(len(cn) - n + 1):
            toks.add(cn[i:i + n])
    return toks


def _topic_key(text: str) -> str | None:
    """取条目最强主题锚（出现最频繁、最长的中文 n-gram）。无则 None。"""
    toks = [t for t in _tokens(text) if _CN.search(t)]
    if not toks:
        return None
    # 频率统计（按字符数降序、再按计数）选最具区分度的锚
    from collections import Counter
    c = Counter(toks)
    best = sorted(c.items(), key=lambda kv: (-len(kv[0]), -kv[1]))[0][0]
    return best


def _date_of(entry: str) -> str | None:
    m = re.match(r"(\d{4}-\d{1,2}-\d{1,2})[:：]", entry.strip())
    return m.group(1) if m else None


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _is_value_flip(a: str, b: str) -> bool:
    """任一方含翻转字而另一方不含（如「喜欢」vs「不喜欢」）→ 视为矛盾而非重复。"""
    for ch in _FLIP_CHARS:
        if (ch in a) != (ch in b):
            return True
    return False


# ---------------------------------------------------------------------------
# 单文件提炼（USER.md 或某个 MEMORY.md 分节）
# ---------------------------------------------------------------------------
def _synthesize_entries(
    entries: list[str],
    today: datetime.date,
    report: dict,
) -> list[str]:
    """对一组条目做去重 + 矛盾消解，返回精简后的条目。

    - 近重复（高 Jaccard 重叠）→ 删冗余，留较新/较完整。
    - 同主题异值（矛盾）→ 留新值；旧值未超龄只标注保留（不丢），
      超龄则迁 vault（可恢复）。
    不动未被命中的条目原位；结果幂等（再跑 0 改动）。
    """
    if len(entries) <= 1:
        return entries

    n = len(entries)
    tok = [_tokens(e) for e in entries]
    dt = [_date_of(e) for e in entries]
    consumed: set[int] = set()

    # ① 近重复去重（删冗余）
    for a in range(n):
        if a in consumed:
            continue
        for b in range(a + 1, n):
            if b in consumed:
                continue
            if _jaccard(tok[a], tok[b]) >= _MERGE_JACCARD and not _is_value_flip(entries[a], entries[b]):
                winner, loser = _newer_or_longer(a, b, dt, entries)
                consumed.add(loser)
                report["merged"] += 1

    # ② 同主题异值（矛盾 / 过时更新）
    for a in range(n):
        if a in consumed:
            continue
        if _SUPERSEDED_MARK in entries[a]:
            continue  # 已标注为旧值的条目不再发起新矛盾判定（防重复计数）
        ka = _topic_key(entries[a])
        if not ka:
            continue
        for b in range(a + 1, n):
            if b in consumed or a in consumed:
                continue
            if _SUPERSEDED_MARK in entries[b]:
                continue  # 已标注旧值不再参与矛盾判定
            if _topic_key(entries[b]) != ka:
                continue
            if entries[a].strip() == entries[b].strip():
                continue
            newer, older = _newer_or_longer(a, b, dt, entries)
            report["contradictions_resolved"] += 1
            older_date = dt[older]
            age = None
            if older_date:
                try:
                    age = (today - datetime.date.fromisoformat(older_date)).days
                except ValueError:
                    age = None
            if age is not None and age > _STALE_SUPERSEDE_DAYS:
                # 超龄被覆盖 → 迁 vault（可恢复），从记忆移除
                consumed.add(older)
                report["stale_superseded"] += 1
                report.setdefault("_to_vault", []).append(entries[older])
            else:
                # 未超龄 → 标注旧值保留（幂等：已标注不再加），绝不静默丢失
                report.setdefault("_annotated", []).append(entries[older])
                if _SUPERSEDED_MARK not in entries[older]:
                    entries[older] = entries[older] + _SUPERSEDED_MARK

    kept = [entries[i] for i in range(n) if i not in consumed]
    return kept


def _newer_or_longer(i: int, j: int, dt: list[str | None], entries: list[str]) -> tuple[int, int]:
    di = dt[i]
    dj = dt[j]
    # 都有日期 → 新的赢
    if di and dj:
        try:
            if datetime.date.fromisoformat(di) >= datetime.date.fromisoformat(dj):
                return i, j
            return j, i
        except ValueError:
            pass
    # 一个有日期 → 有日期的赢
    if di and not dj:
        return i, j
    if dj and not di:
        return j, i
    # 都没日期 → 较长（信息更完整）的赢
    return (i, j) if len(entries[i]) >= len(entries[j]) else (j, i)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def run_dream(base_dir: Path | str, aggressive: bool = False) -> dict:
    """执行一次记忆提炼。返回报告 dict。

    aggressive=True 时放宽阈值（更积极去重）；默认保守。
    """
    base = Path(base_dir)
    mgr = MemoryManager(base_dir=base)
    today = datetime.date.today()
    report: dict = {
        "ok": True,
        "merged": 0,
        "contradictions_resolved": 0,
        "stale_superseded": 0,
        "entries_touched": 0,
        "files_touched": [],
        "notes": "",
    }

    try:
        # ---- USER.md ----
        user_text = load_user(base)
        if user_text.strip():
            orig_user = user_text.strip()
            entries = [e.strip() for e in re.split(r"\n\s*\n", user_text) if e.strip()]
            new_entries = _synthesize_entries(entries, today, report)
            new_user_str = "\n\n".join(new_entries)
            # 注意：_synthesize_entries 会原地给旧值加标注，故用渲染字符串与原串比对
            if new_user_str != orig_user:
                _backup_and_write(base / "USER.md", user_text,
                                  new_user_str + "\n")
                report["files_touched"].append("USER.md")

        # ---- MEMORY.md 分节 ----
        memory = mgr._load_global_memory()
        if memory.strip():
            preamble, sections = mgr._parse_sections(memory)
            modified = False
            for sec in sections:
                if sec["tags"] & _PROTECTED_SECTIONS:
                    continue  # 偏好/纠正永保真
                orig_body = sec["body"].strip()
                body_entries = [e.strip() for e in re.split(r"\n\s*\n", sec["body"]) if e.strip()]
                if len(body_entries) <= 1:
                    continue
                new_body = _synthesize_entries(body_entries, today, report)
                new_body_str = "\n\n".join(new_body)
                if new_body_str != orig_body:
                    sec["body"] = new_body_str
                    modified = True
            if modified:
                mgr._save_global_memory(mgr._render_sections(preamble, sections))
                report["files_touched"].append("MEMORY.md")

        # ---- 迁 vault（超龄被覆盖的旧值，可恢复）----
        for old in report.pop("_to_vault", []):
            try:
                mgr.vault.write_entry(
                    content=old, category="note", tags=["dream-superseded"], source="memory-dream"
                )
                report["entries_touched"] += 1
            except Exception as e:
                logger.debug("dream: vault 归档失败（跳过）: %s", e)

        # ---- 标注的旧值（未超龄，保留备查）已随分节回写；此处仅计入统计 ----
        annotated = report.pop("_annotated", [])
        report["entries_touched"] += len(annotated)

        # ---- 报告落盘 + trace（8.3 可见）----
        report["entries_touched"] += report["merged"] + report["contradictions_resolved"]
        _write_report(base, report)
        try:
            mgr._append_trace(DREAM_TRACE_FILE, {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "merged": report["merged"],
                "contradictions_resolved": report["contradictions_resolved"],
                "stale_superseded": report["stale_superseded"],
                "files": report["files_touched"],
            })
        except Exception:
            pass
        report["notes"] = (
            f"去重 {report['merged']} 条，矛盾消解 {report['contradictions_resolved']} 条，"
            f"超龄迁 vault {report['stale_superseded']} 条"
        )
    except Exception:
        import logging
        logging.exception("dream: 提炼异常（已跳过，不阻塞主流程）")
        report["ok"] = False
        report["notes"] = "提炼异常，已安全跳过"

    _mark_run(base)
    return report


def _backup_and_write(path: Path, old_text: str, new_text: str) -> None:
    try:
        if path.is_file():
            (path.parent / f"{path.name}.bak").write_text(old_text, encoding="utf-8")
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        pass


def _write_report(base: Path, report: dict) -> None:
    try:
        lines = ["# 记忆自动提炼报告（Dreaming · P2-1）", ""]
        lines.append(f"- 时间：{datetime.datetime.now().isoformat(timespec='seconds')}")
        lines.append(f"- 去重：{report['merged']} 条")
        lines.append(f"- 矛盾消解：{report['contradictions_resolved']} 条")
        lines.append(f"- 超龄迁 vault（可恢复）：{report['stale_superseded']} 条")
        lines.append(f"- 触及文件：{', '.join(report['files_touched']) or '无'}")
        lines.append("")
        lines.append("> 只动「你的记忆」（USER.md / MEMORY.md），绝不碰书稿记忆。"
                     "破坏性动作均进 vault，可在 traces/dream.jsonl 查证。")
        (base / _REPORT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def last_run(base_dir: Path | str) -> float | None:
    try:
        p = Path(base_dir) / _MARKER
        return float(p.read_text(encoding="utf-8").strip()) if p.is_file() else None
    except Exception:
        return None


import logging  # noqa: E402  (放在文件尾，避免干扰上方常量可读性)
logger = logging.getLogger("deskmate.memory.dream")
