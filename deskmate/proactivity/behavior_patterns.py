"""P2-3 长期模式学习——从「行为」推断偏好（越用越懂你 最高阶）。

核心理念：不只记住「用户说过的话」（显式记忆），更要**从用户的行为轨迹推断偏好**。
例如：
- 用户反复在「周X」做某类事 → 学到「他通常在周X做这个」
- 用户连续多次把某任务「从周A推到周B」→ 学到「他不喜欢在周A排这个，自动避开」
- 某行为「大约每 N 天一次」→ 学到稳定节奏

实现原则（与 P0-7/P2-1 一致）：
- **零 LLM**：纯统计/时间模式检测，成本可忽略、可复现、可单测。
- **fail-open**：任一环节异常不阻断主流程；事件日志写入失败静默。
- **可观测可恢复**：事件落地 ``~/.deskmate/behavior_log.jsonl``（追加写，不删不改历史），
  模式可经后端端点 ``GET /v1/deskmate/behavior-patterns`` 查看（8.0 红线：可感可看）。
- **不污染显式记忆**：推断出的行为模式只注入 judge 上下文（与 P1-2 preferences 同通道），
  不写进 USER.md/MEMORY.md（那是「用户说过的」），边界清晰。

事件来源（接线点）：
- ``proactivity/preferences.record_accepted`` → ``suggestion_accepted``
- ``growth/engine.record_creation_feedback`` → ``creation_approved`` / ``creation_rejected``
- ``growth/engine.record_correction`` → ``correction``
- ``proactivity/tick`` 深专注 → ``focus_deep``（30 分钟去重，避免刷屏）
- 调度/推迟事件（未来日历/任务系统集成时）→ ``reschedule``（检测器已就绪）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 行为事件 → 人类可读标签
_KIND_LABELS = {
    "suggestion_accepted": "接受我的提醒",
    "creation_approved": "通过创作产出",
    "creation_rejected": "打回创作重做",
    "correction": "纠正我",
    "focus_deep": "进入深度工作",
    "reschedule": "推迟排期任务",
}

# 模式检测阈值
MIN_EVENTS = 3          # 某类事件至少出现这么多次才提炼
WEEKDAY_DOMINANCE = 0.4 # 某星期几占该类事件 ≥40% 才算「常在周X」
HOUR_DOMINANCE = 0.3    # 某小时占 ≥30% 才算「常在 HH:00」
RECUR_MIN = 3           # 至少 3 次事件才判节奏
RECUR_TOLERANCE = 0.4   # 间隔方差容忍（±40% 视为稳定）
FOCUS_DEDUP_SEC = 1800  # 深专注事件 30 分钟内只记一次，避免刷屏


def _desk_dir(base_dir: str | Path | None) -> Path:
    return Path(base_dir) if base_dir else Path.home() / ".deskmate"


def _log_path(base_dir: str | Path | None) -> Path:
    return _desk_dir(base_dir) / "behavior_log.jsonl"


def _append_line(path: Path, record: dict) -> None:
    """追加一行事件（直接 append，POSIX 下单次 write 原子，失败静默）。

    注意：不能用「临时文件 + os.replace」——那会整文件替换，同一进程内多次
    调用会互相覆盖只留最后一行。日志必须是追加写。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # 行为日志失败绝不阻断主流程


def log_event(kind: str, payload: dict | None = None, ts: float | None = None,
              base_dir: str | Path | None = None) -> None:
    """记录一条行为事件（追加写，幂等安全）。fail-open。"""
    try:
        ts = ts if ts is not None else time.time()
        _append_line(
            _log_path(base_dir),
            {"ts": ts, "kind": str(kind), "payload": payload or {}},
        )
    except Exception:
        pass


def log_reschedule(task_type: str, from_weekday: int, to_weekday: int,
                   ts: float | None = None, base_dir: str | Path | None = None) -> None:
    """记录一次排期推迟（文档 P2-3 招牌例子：周四审稿推到周五）。

    from_weekday/to_weekday 用 0=周一..6=周日（与 time.struct_time.tm_wday 一致）。
    调度/日历系统集成时可调用本函数，检测器会自动提炼「周四别排 X」。
    """
    log_event(
        "reschedule",
        {"task_type": str(task_type), "from_weekday": int(from_weekday),
         "to_weekday": int(to_weekday)},
        ts=ts, base_dir=base_dir,
    )


def _recent_logged(kind: str, within_sec: int, base_dir=None) -> bool:
    """判断最近 within_sec 秒内是否已记过同类事件（去重用）。"""
    try:
        p = _log_path(base_dir)
        if not p.is_file():
            return False
        lines = p.read_text(encoding="utf-8").splitlines()
        now = time.time()
        for line in reversed(lines[-200:]):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != kind:
                continue
            return (now - float(rec.get("ts", 0))) < within_sec
    except OSError:
        return False
    return False


def log_focus_deep(app: str = "", title: str = "", base_dir=None) -> None:
    """记录一次深专注（30 分钟去重），供「常在周X/某时段深度工作」模式提炼。"""
    if _recent_logged("focus_deep", FOCUS_DEDUP_SEC, base_dir=base_dir):
        return
    log_event("focus_deep", {"app": app, "title": title}, base_dir=base_dir)


# ---------------------------------------------------------------------------
# 模式提炼（零 LLM）
# ---------------------------------------------------------------------------

def _load_events(base_dir, since_ts: float) -> list[dict]:
    p = _log_path(base_dir)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if float(rec.get("ts", 0)) >= since_ts:
                out.append(rec)
    except OSError:
        return []
    return out


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def _detect_weekday_cadence(events: list[dict]) -> list[str]:
    """每类事件按星期几分桶，提炼「常在周X做Y」。"""
    by_kind: dict[str, list[int]] = {}
    for e in events:
        wd = _weekday_of(e)
        if wd is None:
            continue
        by_kind.setdefault(e["kind"], []).append(wd)
    out: list[str] = []
    for kind, wds in by_kind.items():
        if len(wds) < MIN_EVENTS:
            continue
        counts = [0] * 7
        for w in wds:
            counts[w] += 1
        modal = max(range(7), key=lambda i: counts[i])
        if counts[modal] >= WEEKDAY_DOMINANCE * len(wds):
            out.append(
                f"你常在{WEEKDAYS[modal]}做「{_kind_label(kind)}」"
                f"（近 {len(wds)} 次中 {counts[modal]} 次）"
            )
    return out


def _detect_hour_cadence(events: list[dict]) -> list[str]:
    """按小时分桶，提炼活跃时段。"""
    by_kind: dict[str, list[int]] = {}
    for e in events:
        h = _hour_of(e)
        if h is None:
            continue
        by_kind.setdefault(e["kind"], []).append(h)
    out: list[str] = []
    for kind, hours in by_kind.items():
        if len(hours) < MIN_EVENTS * 2:  # 小时粒度更密，要求更多样本
            continue
        counts = [0] * 24
        for h in hours:
            counts[h] += 1
        modal = max(range(24), key=lambda i: counts[i])
        if counts[modal] >= HOUR_DOMINANCE * len(hours):
            out.append(
                f"你通常在 {modal:02d}:00 前后「{_kind_label(kind)}」"
                f"（约 {counts[modal]}/{len(hours)} 次落在该时段）"
            )
    return out


def _detect_recurrence(events: list[dict]) -> list[str]:
    """同类事件间隔稳定 → 提炼节奏（大约每 N 天一次）。"""
    by_kind: dict[str, list[float]] = {}
    for e in events:
        t = float(e.get("ts", 0))
        by_kind.setdefault(e["kind"], []).append(t)
    out: list[str] = []
    for kind, ts_list in by_kind.items():
        ts_list.sort()
        if len(ts_list) < RECUR_MIN:
            continue
        gaps = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
        gaps = [g for g in gaps if g > 0]
        if len(gaps) < RECUR_MIN - 1:
            continue
        gaps.sort()
        median = gaps[len(gaps) // 2]
        if median <= 0:
            continue
        stable = all(abs(g - median) <= RECUR_TOLERANCE * median for g in gaps)
        if stable and median >= 3600:  # 至少 1 小时以上才有意义
            days = max(1, round(median / 86400))
            out.append(
                f"「{_kind_label(kind)}」大约每 {days} 天一次"
                f"（{len(ts_list)} 次观测，间隔较稳定）"
            )
    return out


def _detect_deferral(events: list[dict]) -> list[str]:
    """reschedule 事件：从周A推到周B 一致 → 学到「周四别排X」。"""
    by_task: dict[str, list[tuple[int, int]]] = {}
    for e in events:
        if e["kind"] != "reschedule":
            continue
        p = e.get("payload", {})
        task = p.get("task_type")
        fw = p.get("from_weekday")
        tw = p.get("to_weekday")
        if task is None or fw is None or tw is None:
            continue
        by_task.setdefault(str(task), []).append((int(fw), int(tw)))
    out: list[str] = []
    for task, pairs in by_task.items():
        if len(pairs) < 2:
            continue
        # 多数一致的 (from→to) 方向
        from collections import Counter
        direction = Counter(pairs)
        (f, t), n = direction.most_common(1)[0]
        if n >= 2 and n >= 0.5 * len(pairs):
            out.append(
                f"你常把「{task}」从{WEEKDAYS[f]}推到{WEEKDAYS[t]}"
                f"（{n} 次），建议避开{WEEKDAYS[f]}排「{task}」"
            )
    return out


def _weekday_of(e: dict) -> int | None:
    try:
        return int(time.localtime(float(e["ts"])).tm_wday)
    except (ValueError, KeyError, OverflowError):
        return None


def _hour_of(e: dict) -> int | None:
    try:
        return int(time.localtime(float(e["ts"])).tm_hour)
    except (ValueError, KeyError, OverflowError):
        return None


def mine_patterns(base_dir: str | Path | None = None, days: int = 120) -> dict:
    """提炼近期行为模式。返回结构化 dict，供端点/上下文注入消费。"""
    since = time.time() - days * 86400
    events = _load_events(base_dir, since)
    if not events:
        return {
            "window_days": days,
            "event_count": 0,
            "patterns": [],
            "context": "",
        }
    patterns = (
        _detect_weekday_cadence(events)
        + _detect_hour_cadence(events)
        + _detect_recurrence(events)
        + _detect_deferral(events)
    )
    return {
        "window_days": days,
        "event_count": len(events),
        "kinds": sorted({e["kind"] for e in events}),
        "patterns": patterns,
        "context": _format_context(patterns),
    }


def _format_context(patterns: list[str]) -> str:
    if not patterns:
        return ""
    lines = ["以下是**从你长期行为推断**出的规律（非你明说的偏好，仅作参考）："]
    for p in patterns:
        lines.append(f"- {p}")
    return "\n".join(lines)


def to_context(base_dir: str | Path | None = None, days: int = 120) -> str:
    """格式化供 judge 注入（无模式时返回空串）。"""
    try:
        return mine_patterns(base_dir, days=days).get("context", "")
    except Exception:
        return ""


if __name__ == "__main__":
    import sys

    _d = sys.argv[1] if len(sys.argv) > 1 else None
    res = mine_patterns(_d)
    print(f"窗口 {res['window_days']} 天 · 事件 {res['event_count']} 条")
    if res["patterns"]:
        print("推断出的行为模式：")
        for p in res["patterns"]:
            print(f"  • {p}")
    else:
        print("（暂无足够行为数据，多互动一段时间后会自动长出模式）")
