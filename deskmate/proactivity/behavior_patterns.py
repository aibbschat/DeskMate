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

import atexit
import json
import os
import threading
import time
from pathlib import Path
from deskmate.paths import deskmate_base_dir

# fsync 节流（PR13）：数据仍逐行 append+flush（读后立刻可见），只把昂贵的 fsync
# 合并到「每 20 条或每 5 秒」一次。
_FLUSH_MAX = 20
_FLUSH_SEC = 5.0
_BUF_LOCK = threading.Lock()
_PENDING: dict[str, int] = {}
_LAST_SYNC: dict[str, float] = {}

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 行为事件 → 人类可读标签
# kind → 人类可读标签。**这张表同时是「能不能进模式提炼」的白名单**：
# 凡是出现在喂给 judge 的上下文里的，一律取这里的文案，绝不允许内部键原样上屏
# （2026-09-22 实测输出过「你常在周一做『roundtable_done』」——同 interview 的
#  topic_key 教训：**内部键 ≠ 呈现文案**）。
# ⚠ 因此**技术诊断类事件刻意不登记**：`proactive_tick_http`（排查「访谈没弹」
#   用的打点）没有人类语义，不登记即自动不参与模式提炼。
_KIND_LABELS = {
    "suggestion_accepted": "接受我的提醒",
    "creation_approved": "通过创作产出",
    "creation_rejected": "打回创作重做",
    "correction": "纠正我",
    "focus_deep": "进入深度工作",
    "reschedule": "推迟排期任务",
    # 2026-09-22 补登记：以下 kind 一直在写、却漏了标签 → 会被 _kind_label 回退成
    # 内部键直接上屏。它们都有明确的人类语义，登记后既能参与提炼、又输出人话。
    "roundtable_done": "开圆桌讨论",
    "chat_turn": "和我聊天",
    "review_done": "让我审稿",
    "mention_collab": "点名我协作",
    "suggestion_pushed": "我提醒了你",
}

# 模式检测阈值
MIN_EVENTS = 10         # 某类事件至少出现这么多次才提炼（避免小样本伪规律）
MIN_DISTINCT_DAYS = 2    # weekday/hour 规律至少跨这么多不同自然日才成立（避免单日噪音当规律）
WEEKDAY_DOMINANCE = 0.4 # 某星期几占该类事件 ≥40% 才算「常在周X」
HOUR_DOMINANCE = 0.3    # 某小时占 ≥30% 才算「常在 HH:00」
RECUR_MIN = 3           # 至少 3 次事件才判节奏
RECUR_TOLERANCE = 0.4   # 间隔方差容忍（±40% 视为稳定）
FOCUS_DEDUP_SEC = 1800  # 深专注事件 30 分钟内只记一次，避免刷屏

# 每 (base_dir, kind) 最近一次记录落在的「小时桶」（YYYY-MM-DDTHH）。
# 仅供 log_event(dedup_per_hour=True) 的高频噪声抽样用，见其 docstring。
_HOURLY_DEDUP: dict[str, str] = {}


def _desk_dir(base_dir: str | Path | None) -> Path:
    return Path(base_dir) if base_dir else deskmate_base_dir()


def _log_path(base_dir: str | Path | None) -> Path:
    return _desk_dir(base_dir) / "behavior_log.jsonl"


def _append_line(path: Path, record: dict) -> None:
    """追加一行事件；``flush`` 每条都做（读后立刻可见），``fsync`` 合并节流。

    PR13：实测 ``behavior_log.jsonl`` 27k 行 / 7.3MB，旧实现每条都
    ``open→write→flush→fsync``，fsync 是每轮对话的固定磁盘开销。行为日志是
    **可丢的观测数据**：数据仍逐行落页缓存，只把 fsync 合并成 20 条 / 5 秒一次，
    异常断电最多丢最后一批（语义可接受）。
    ``DESKMATE_BEHAVIOR_LOG_SYNC=1`` 回到逐条 fsync（排查用）。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
    except (OSError, TypeError, ValueError):
        return
    key = str(path)
    force_sync = os.environ.get("DESKMATE_BEHAVIOR_LOG_SYNC", "0") == "1"
    sync_now = force_sync
    with _BUF_LOCK:
        n = _PENDING.get(key, 0) + 1
        _PENDING[key] = n
        _LAST_SYNC.setdefault(key, time.time())
        if n >= _FLUSH_MAX or (time.time() - _LAST_SYNC[key]) >= _FLUSH_SEC:
            sync_now = True
            _PENDING[key] = 0
            _LAST_SYNC[key] = time.time()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            if sync_now:
                os.fsync(f.fileno())
    except OSError:
        pass  # 行为日志失败绝不阻断主流程


def flush_behavior_log(base_dir: str | Path | None = None) -> None:
    """把本轮事件强制 fsync 落盘（切进程/测试收尾/运维脚本用）。"""
    path = _log_path(base_dir)
    key = str(path)
    with _BUF_LOCK:
        _PENDING[key] = 0
        _LAST_SYNC[key] = time.time()
    try:
        if path.is_file():
            with open(path, "a", encoding="utf-8") as f:
                os.fsync(f.fileno())
    except OSError:
        pass


def _flush_all() -> None:
    for key in list(_PENDING.keys()):
        flush_behavior_log(Path(key).parent)


atexit.register(_flush_all)


def _scrub(obj):
    """递归对行为事件 payload 做脱敏（8.4：敏感原文不落行为日志）。"""
    try:
        from deskmate.memory.sensitivity import redact

        if isinstance(obj, str):
            return redact(obj)
        if isinstance(obj, dict):
            return {k: _scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_scrub(v) for v in obj]
    except Exception:
        pass
    return obj


def log_event(kind: str, payload: dict | None = None, ts: float | None = None,
              base_dir: str | Path | None = None, *, dedup_per_hour: bool = False) -> None:
    """记录一条行为事件（追加写，幂等安全）。fail-open。

    8.4：payload 中的字符串会先经 redact 脱敏，避免敏感原文进入行为日志。

    dedup_per_hour=True：同一 (base_dir, kind) 在同一自然小时内最多落一行。
    供**高频噪声事件**使用——`suggestion_pushed` 曾占行为日志 94%
    （13197 / 14069 条），而实测它因推送时段分散**提炼不出任何模式**
    （`mine_patterns()` 对它零输出）⇒ 纯粹是存储噪声。
    抽样只保留分布形状：`_detect_weekday/hour_cadence` 判的是"某桶占比是否超阈值"，
    同小时内多条对占比没有额外信息，所以降噪后仍能提炼「常在何时/周几」。
    内存态去重，进程重启后同一小时可能多记 1-2 条（可接受，不会失控）。
    """
    try:
        ts = ts if ts is not None else time.time()
        if dedup_per_hour:
            key = f"{_desk_dir(base_dir)}|{kind}"
            bucket = time.strftime("%Y-%m-%dT%H", time.localtime(ts))
            if _HOURLY_DEDUP.get(key) == bucket:
                return
            _HOURLY_DEDUP[key] = bucket
        _append_line(
            _log_path(base_dir),
            {"ts": ts, "kind": str(kind), "payload": _scrub(payload or {})},
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


def _iter_lines_reverse(path: Path, block: int = 16384):
    """按字节块从文件尾反向逐行产出（新→旧），只读一遍，不整文件进内存。"""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        pos = size
        buf = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + buf
            parts = data.split(b"\n")
            buf = parts[0]  # 头部可能是半行，留到下一块
            for raw in reversed(parts[1:]):
                if raw.strip():
                    yield raw
        if buf.strip():
            yield buf


def _tail_lines(path: Path, max_bytes: int = 65536) -> tuple[list[str], bool]:
    """从文件尾部反向取一段行。返回 ``(lines 旧→新, whole_file)``。"""
    out: list[str] = []
    read = 0
    whole = True
    for raw in _iter_lines_reverse(path):
        read += len(raw) + 1
        if read > max_bytes:
            whole = False
            break
        out.append(raw.decode("utf-8", "replace"))
    out.reverse()
    return out, whole


def _recent_logged(kind: str, within_sec: int, base_dir=None) -> bool:
    """判断最近 within_sec 秒内是否已记过同类事件（去重用）。

    PR13：只读文件尾部（旧实现每次读全文件 7.3MB），事件按时间追加，
    碰到早于窗口的记录即可停止。
    """
    try:
        p = _log_path(base_dir)
        if not p.is_file():
            return False
        flush_behavior_log(base_dir)  # 缓冲里最新的也要算进来
        now = time.time()
        cutoff = now - within_sec
        window = 65536
        while True:
            lines, whole = _tail_lines(p, max_bytes=window)
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = float(rec.get("ts", 0) or 0)
                if ts < cutoff:
                    return False
                if rec.get("kind") == kind:
                    return True
            if whole:
                return False
            window *= 8
    except (OSError, ValueError):
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
    """读取 since_ts 之后的事件（PR13：从文件尾部反向读，越过窗口即停）。

    旧实现 ``read_text().splitlines()`` 把 7.3MB 全量进内存再逐行 json；
    现在按需扩大尾部窗口（256KB 起、每次 4×），读到早于窗口的记录或文件头即停。
    """
    p = _log_path(base_dir)
    if not p.is_file():
        return []
    flush_behavior_log(base_dir)
    out: list[dict] = []
    try:
        for raw in _iter_lines_reverse(p):
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            ts = float(rec.get("ts", 0) or 0)
            if ts < since_ts:
                break  # 事件按时间追加：越过窗口即可停，不再往回读
            out.append(rec)
    except OSError:
        return []
    out.reverse()
    return out


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def _detect_weekday_cadence(events: list[dict]) -> list[str]:
    """每类事件按星期几分桶，提炼「常在周X做Y」。"""
    by_kind: dict[str, list[int]] = {}
    by_kind_ts: dict[str, list[float]] = {}
    for e in events:
        wd = _weekday_of(e)
        if wd is None:
            continue
        by_kind.setdefault(e["kind"], []).append(wd)
        by_kind_ts.setdefault(e["kind"], []).append(float(e.get("ts", 0) or 0.0))
    out: list[str] = []
    for kind, wds in by_kind.items():
        if len(wds) < MIN_EVENTS:
            continue
        # 跨度保护：至少跨 MIN_DISTINCT_DAYS 个自然日，避免「同一天刷 N 次」被当成长期习惯
        days = {int(time.localtime(t).tm_yday) for t in by_kind_ts[kind] if t > 0}
        if len(days) < MIN_DISTINCT_DAYS:
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
    by_kind_ts: dict[str, list[float]] = {}
    for e in events:
        h = _hour_of(e)
        if h is None:
            continue
        by_kind.setdefault(e["kind"], []).append(h)
        by_kind_ts.setdefault(e["kind"], []).append(float(e.get("ts", 0) or 0.0))
    out: list[str] = []
    for kind, hours in by_kind.items():
        if len(hours) < MIN_EVENTS * 2:  # 小时粒度更密，要求更多样本
            continue
        # 跨度保护：至少跨 MIN_DISTINCT_DAYS 个自然日，避免单日噪音当规律
        days = {int(time.localtime(t).tm_yday) for t in by_kind_ts[kind] if t > 0}
        if len(days) < MIN_DISTINCT_DAYS:
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
    """提炼近期行为模式。返回结构化 dict，供端点/上下文注入消费。

    ⚠ 只有 ``_KIND_LABELS`` 登记过的 kind 才参与提炼（**人话白名单**，见该表注释）：
    未登记者不会把内部键拼进 judge 上下文。``event_count`` / ``kinds`` 仍按
    **全量**统计（它们是「日志里有什么」的事实，不是分析结论），另给
    ``patterned_kinds`` 说明实际参与了哪些。
    """
    since = time.time() - days * 86400
    all_events = _load_events(base_dir, since)
    if not all_events:
        return {
            "window_days": days,
            "event_count": 0,
            "patterns": [],
            "context": "",
        }
    events = [e for e in all_events if str(e.get("kind")) in _KIND_LABELS]
    patterns = (
        _detect_weekday_cadence(events)
        + _detect_hour_cadence(events)
        + _detect_recurrence(events)
        + _detect_deferral(events)
    )
    return {
        "window_days": days,
        "event_count": len(all_events),
        "kinds": sorted({e["kind"] for e in all_events}),
        "patterned_kinds": sorted({str(e.get("kind")) for e in events}),
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


def archive_behavior_log(
    base_dir: str | Path | None = None, days: int = 90, *, dry_run: bool = True
) -> dict:
    """把超过 ``days`` 的行为事件滚到 ``behavior_log.YYYYQn.jsonl.gz``。

    默认 **dry-run**（只报表数）；``dry_run=False`` 才真正：
      1. 旧事件按季度分桶追加进 gzip（多成员 gzip 合法，可续写）；
      2. 热文件用「同目录临时文件 + os.replace」原子替换，只留窗口内事件。

    ``_load_events`` 不读归档 ⇒ 归档后 ``mine_patterns(days=120)`` 实际窗口上限
    就是 ``days``（本方案默认 90，需 Owner 确认，见文档 §9.7）。
    """
    p = _log_path(base_dir)
    if not p.is_file():
        return {"archived": 0, "kept": 0, "files": [], "dry_run": dry_run}
    flush_behavior_log(base_dir)
    cutoff = time.time() - float(days) * 86400
    old: dict[str, list[str]] = {}
    kept: list[str] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(json.loads(line).get("ts", 0) or 0)
                except (ValueError, TypeError):
                    kept.append(line)
                    continue
                if ts >= cutoff:
                    kept.append(line)
                    continue
                t = time.localtime(ts)
                q = (t.tm_mon - 1) // 3 + 1
                name = f"behavior_log.{t.tm_year}Q{q}.jsonl.gz"
                old.setdefault(name, []).append(line)
    except OSError:
        return {"archived": 0, "kept": 0, "files": [], "dry_run": dry_run}

    total_old = sum(len(v) for v in old.values())
    if dry_run or total_old == 0:
        return {
            "archived": total_old,
            "kept": len(kept),
            "files": sorted(old),
            "dry_run": dry_run,
        }

    dir_ = p.parent
    written: list[str] = []
    try:
        import gzip

        for name, lines in old.items():
            target = dir_ / name
            with open(target, "ab") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                    gz.write(("\n".join(lines) + "\n").encode("utf-8"))
            written.append(name)
        tmp = p.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError:
        return {"archived": 0, "kept": len(kept), "files": written, "dry_run": dry_run}
    return {"archived": total_old, "kept": len(kept), "files": written, "dry_run": False}


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
