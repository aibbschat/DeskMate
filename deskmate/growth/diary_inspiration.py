"""小桔日记的「灵感」机制——素材攒够了才动笔。

设计原则（萧哥 2026-09-02 定调）
--------------------------------
日记**不是**定时任务。定时触发会在没素材的日子产出流水账废话——
「今天没有太多动静」写一百遍，不如不写。

正确逻辑是：当天发生的事累积到一定程度，自然「产生灵感」，
与主动建议按置信度阈值弹窗、深度访谈按 open gap 数触发同一范式——
**条件满足才发生**，没攒够就安静等着。

三个要点
--------
1. **素材账本**（``<base>/diary/materials/YYYY-MM-DD.jsonl``）

   为什么不能直接读 ``GrowthState.recent_events``？因为它在落盘时会被
   截断到最后 ``EVENT_LOG_CAP``(50) 条，是**易失窗口**。实测 09-01 一天
   就产生 28 条事件，两天就能把前一天的素材整体挤出去——等到晚上再写，
   素材早没了。账本在每次 sync 时增量落盘，持久保存，不受窗口挤压。

2. **加权评分**：里程碑/反思类权重高，日常类权重低。
   ``memory_learned`` 只有 0.5 分/条——学会一件小事不构成一篇日记。

3. **单类封顶**（``_KIND_CAP``）：防批量刷分。
   一次导入 15 条记忆、或 11 个技能同时进化，那是**一次批处理**的副产品，
   不是 11 件值得写的事。不封顶的话，任何批量操作都会瞬间刷满灵感分。

4. **一天可以写多则**（萧哥 2026-09-02：不要限死每天一条）。
   素材被写进某一则后就打上 ``entry`` 标记「消费掉」，只有**没写过的新素材**
   才继续计分。攒够门槛就再追加一则（``DiaryWriter.append_entry``），
   绝不覆盖当天已写的内容——覆盖会让「今天」永远停在第一则落笔的那一刻。
   第二则只讲后来的事，不会把第一则复述一遍。

   门槛：第一则 ``_INSPIRE_THRESHOLD``(5.0)，后续各则 ``_NEXT_ENTRY_DELTA``(4.0)。
   后续略低，是因为追加一则的"成本"比开篇低——它只是接着往下记。
   另设 ``_MIN_GAP_MINUTES`` 冷却，防止素材集中涌入时连着刷出好几则。

零 LLM、fail-open：任何异常都静默，绝不影响主动推理主流程。
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Optional

from deskmate.growth._atomic import unique_tmp
from deskmate.growth.diary import _fingerprint, diary_write_lock
from deskmate.growth.engine import GrowthEngine

# ----------------------------------------------------------------------
# 灵感权重表
# ----------------------------------------------------------------------
# 分档依据：
#   4.0 里程碑 —— 值得单独记一笔的「第一次 / 拍板 / 新建」
#   3.0 反思类 —— 有情感含量，值得写进心里（纠正、打回、圆桌）
#   2.0 协作类 —— 一起做成了一件事
#   1.0 日常类 —— 有互动，但分量轻
#   0.5 痕迹类 —— 只是留下痕迹，不构成叙事
#   0.0 不算素材 —— 系统补录，不是当天真实发生的事
_EVENT_WEIGHTS: dict[str, float] = {
    # 里程碑
    "skill_created": 4.0,
    "creation_approved": 4.0,
    "badge_unlocked": 3.5,
    # 反思
    "user_corrected": 3.0,
    "creation_rework": 3.0,
    # 协作
    "roundtable": 3.0,
    "suggestion_accepted": 2.0,
    "skill_evolved": 2.0,
    "writing": 2.0,
    "report": 2.0,
    # 日常
    "skill_used": 1.5,
    "suggestion_ignored": 1.0,
    # 痕迹
    "chat": 0.5,
    "memory_learned": 0.5,
    # 非素材
    "depth_backfilled": 0.0,
}

_DEFAULT_WEIGHT = 0.5          # 未知事件按「痕迹」计，不因不认识而漏记
_KIND_CAP = 6.0                # 单一类型当天的灵感分封顶（防批量刷分）
_INSPIRE_THRESHOLD = 5.0       # 第一则的动笔阈值
_NEXT_ENTRY_DELTA = 4.0        # 后续各则的门槛（已写过之后，新素材攒够这么多就再记一笔）
_REFRESH_DELTA = _NEXT_ENTRY_DELTA  # 旧名保留，语义同上
_MIN_GAP_MINUTES = 60          # 两则之间的最小间隔：防素材集中涌入时连着刷好几则

# 阈值 5.0 的标定依据（实测 ~/.deskmate 真实事件分布）：
#   08-24 1 条 memory_learned        = 0.5  → 不写（确实没东西可写）
#   08-26 8 条 memory_learned        = 4.0  → 不写（只是学了些小事）
#   08-29 1 skill_used + 1 memory    = 2.0  → 不写
#   08-30 3 次 user_corrected        = 6.0  → 写（"你纠正了我三次"值得一篇）
#   08-31 5 次 user_corrected        = 6.0  → 写
#   09-01 15 memory + 2 纠正 + 11 进化 = 18.0 → 写（素材爆棚）


def _today_local() -> str:
    return datetime.date.today().isoformat()


def _evt_date(ts: str) -> str:
    """ISO 时间 → 本地日期 YYYY-MM-DD。"""
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return ""


def _minutes_since(iso: Optional[str]) -> Optional[float]:
    """距某个 ISO 时刻过了多少分钟；无法解析返回 None（视为无需冷却）。"""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return None
    return (datetime.datetime.now() - dt).total_seconds() / 60.0


# ----------------------------------------------------------------------
# 存量素材回补
# ----------------------------------------------------------------------
# 2026-09-03：L1（事件带原文）上线**之前**产生的 memory_learned 事件只有
# count 没有 texts。但那些原文一直躺在 learned_history 表里——一次「记住」
# 会同时留下两条痕迹（成长事件 + 记忆库记录），时间戳秒级对齐。
# 不把它们搬回来，日记就还是「我又记住了你 4 件小事」，L1/L3 全白做。
#
# 这是**搬运**不是创作：不产生任何新内容，库里没有的就是没有。
_LEARNED_MATCH_WINDOW = 8.0        # 秒：同一次动作两条痕迹的合理时间差
_LEARNED_FACT_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[:：]\s*")


def _local_naive(ts: object) -> Optional[datetime.datetime]:
    """任意 ISO 时间戳 → 本地 naive datetime（可互相比较）。解析不了给 None。

    事件 ts 带 ``+00:00``，learned_history 的 ts 是本地时间无时区；
    统一成本地 naive 才能比对。
    """
    try:
        dt = datetime.datetime.fromisoformat(str(ts or ""))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(tzinfo=None)


def backfill_learned_texts(items: list[dict], base_dir: Path | str | None = None) -> int:
    """给缺原文的 ``memory_learned`` 事件补上事实原文。返回补上的事件条数。

    幂等：已有 ``texts`` 的跳过；查不到库就原样返回，绝不改坏素材。
    """
    need = [
        x for x in items
        if isinstance(x, dict)
        and str(x.get("kind")) == "memory_learned"
        and not x.get("texts")
    ]
    if not need:
        return 0

    try:
        from deskmate.memory.learned_store import LearnedStore

        rows = LearnedStore(base_dir=base_dir).recent(limit=500)
    except Exception:
        return 0

    stamped: list[tuple[datetime.datetime, str]] = []
    for r in rows or []:
        t = _local_naive(r.get("ts", ""))
        # fact 形如「2026-09-03: 太学三舍制设定确认…」，日期前缀是冗余的
        fact = _LEARNED_FACT_PREFIX.sub("", str(r.get("fact", "") or "")).strip()
        if t is not None and fact:
            stamped.append((t, fact))
    if not stamped:
        return 0

    filled = 0
    used: set[str] = set()
    for x in need:
        t = _local_naive(x.get("ts", ""))
        if t is None:
            continue
        # 一条事实只发一次：相邻两个事件的时间窗会重叠，
        # 不排掉已被领走的，同一件事会在素材里出现两遍。
        hits = [
            f for (ft, f) in stamped
            if f not in used
            and abs((ft - t).total_seconds()) <= _LEARNED_MATCH_WINDOW
        ]
        if hits:
            x["texts"] = hits
            used.update(hits)
            filled += 1
    return filled


class MaterialLedger:
    """日记素材账本：按天持久保存的事件流水。

    存在的唯一理由：``GrowthState.recent_events`` 只有 50 条窗口，会丢素材。
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.dir = base / "diary" / "materials"

    # ------------------------------------------------------------------
    def _path(self, date: str) -> Path:
        return self.dir / f"{date}.jsonl"

    def load(self, date: str) -> list[dict]:
        p = self._path(date)
        if not p.is_file():
            return []
        out: list[dict] = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
        except OSError:
            return []
        return out

    def pending_items(self, date: str) -> list[dict]:
        """**还没写进日记**的素材（``entry`` 为空）。

        素材一旦被某一则采用就打上 ``entry`` 序号，之后不再重复计分——
        否则第二则会把第一则写过的事复述一遍，且分数永远降不下来。
        """
        return [x for x in self.load(date) if not x.get("entry")]

    def consume(self, date: str, items: list[dict], seq: int) -> int:
        """把一批素材标记为「已写进第 seq 则」，返回实际标记条数。"""
        fps = {_fingerprint(x) for x in items if isinstance(x, dict)}
        if not fps:
            return 0
        all_items = self.load(date)
        n = 0
        for it in all_items:
            if not it.get("entry") and _fingerprint(it) in fps:
                it["entry"] = int(seq)
                n += 1
        if n:
            self._atomic_write(date, all_items)
        return n

    def sync(self, events: list[dict]) -> int:
        """把最近的事件增量并入账本（按天分文件，指纹去重）。

        Args:
            events: ``GrowthState.recent_events``（或任何事件 dict 列表）

        Returns:
            本次新增的条数。
        """
        by_date: dict[str, list[dict]] = {}
        for e in events:
            if not isinstance(e, dict):
                continue
            d = _evt_date(str(e.get("ts", "")))
            if not d:
                continue
            by_date.setdefault(d, []).append(e)

        added = 0
        for date, items in by_date.items():
            existing = self.load(date)
            seen = {_fingerprint(x) for x in existing}
            fresh = [e for e in items if _fingerprint(e) not in seen]
            if not fresh:
                continue
            # 同一指纹只留一条，再按时间排序，保证账本有序
            merged = existing + fresh
            merged.sort(key=lambda x: str(x.get("ts", "")))
            self._atomic_write(date, merged)
            added += len(fresh)
        return added

    def _atomic_write(self, date: str, items: list[dict]) -> None:
        """临时文件 + os.replace 原子落盘（与项目其它持久化一致的约定）。

        临时文件名必须**唯一**（pid + 线程号）：账本会被 tick 线程、GUI 请求
        线程和独立 MCP 进程同时写，固定名互踩会让一方 ``os.replace`` 报
        ``FileNotFoundError``、整次写入静默丢失（见 ``diary.unique_tmp``）。
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(date)
        tmp = unique_tmp(p)
        try:
            tmp.write_text(
                "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 落笔记录：今天写了几则、写掉了多少分、上一则什么时候落的笔
    # ------------------------------------------------------------------
    def _meta_path(self, date: str) -> Path:
        return self.dir / f"{date}.meta.json"

    def meta(self, date: str) -> dict:
        p = self._meta_path(date)
        if not p.is_file():
            return {}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write_meta(self, date: str, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = unique_tmp(self._meta_path(date))
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._meta_path(date))
        except OSError:
            pass

    def mark_entry(self, date: str, seq: int, score: float) -> None:
        """记下「第 seq 则已落笔」：则数、累计已写分、落笔时刻（供冷却判断）。"""
        cur = self.meta(date)
        self._write_meta(date, {
            "entries": max(int(cur.get("entries", 0) or 0), int(seq)),
            "written_score": round(
                float(cur.get("written_score", 0) or 0) + float(score), 2
            ),
            "last_entry_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })

    def mark_written(self, date: str, score: float) -> None:
        """兼容旧调用：按「又记下一则」处理。"""
        self.mark_entry(date, self.entries(date) + 1, score)

    def entries(self, date: str) -> int:
        """当天已写了几则。"""
        return int(self.meta(date).get("entries", 0) or 0)

    def last_entry_at(self, date: str) -> Optional[str]:
        v = self.meta(date).get("last_entry_at")
        return str(v) if v else None

    def written_score(self, date: str) -> Optional[float]:
        """当天累计已写掉的灵感分；没写过返回 None。"""
        meta = self.meta(date)
        if not meta:
            return None
        try:
            return float(meta.get("written_score", 0))
        except (ValueError, TypeError):
            return None


def score_items(items: list[dict]) -> tuple[float, dict[str, float]]:
    """给一组事件打灵感分。

    Returns:
        ``(总分, 各类型封顶后得分)``
    """
    per_kind: dict[str, float] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind", ""))
        w = _EVENT_WEIGHTS.get(kind, _DEFAULT_WEIGHT)
        if w <= 0:
            continue
        per_kind[kind] = per_kind.get(kind, 0.0) + w
    capped = {k: min(v, _KIND_CAP) for k, v in per_kind.items()}
    return sum(capped.values()), capped


class DiaryInspiration:
    """判断「今天值不值得写一篇日记」。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.base = base
        self.ledger = MaterialLedger(base)
        self.engine = GrowthEngine(base_dir=None if base == Path.home() / ".deskmate" else base)

    # ------------------------------------------------------------------
    def sync_events(self) -> int:
        """从成长事件增量同步到素材账本。返回新增条数。

        同步后对最近几天的账本做一次**回补**：给缺原文的 ``memory_learned``
        事件补上事实原文，补到了就持久化。这样即使 LLM 不可用，规则版
        也能写出具体内容，而不是「我又记住了你 N 件小事」。

        回补**不受 added 是否为 0 影响**——存量素材早就同步过了，
        恰恰是它们缺内容。函数内部先判断有没有缺的，没有就立刻返回。
        """
        try:
            added = self.ledger.sync(list(self.engine.state.recent_events))
        except Exception:
            return 0
        self._backfill_recent()
        return added

    def _backfill_recent(self, days: int = 3) -> None:
        """回补最近 ``days`` 天账本里缺原文的素材。幂等，失败静默。"""
        try:
            d0 = datetime.date.fromisoformat(_today_local())
        except (ValueError, TypeError):
            return
        for back in range(days):
            d = (d0 - datetime.timedelta(days=back)).isoformat()
            try:
                items = self.ledger.load(d)
                if not items:
                    continue
                if backfill_learned_texts(items, base_dir=self.base):
                    self.ledger._atomic_write(d, items)
            except Exception:
                continue

    def score(self, date: str | None = None) -> tuple[float, dict[str, float]]:
        """当天灵感分（先同步，再评分）。"""
        date = date or _today_local()
        self.sync_events()
        return score_items(self.ledger.load(date))

    def materials(self, date: str | None = None) -> list[dict]:
        date = date or _today_local()
        return self.ledger.load(date)

    def pending(self, date: str | None = None) -> list[dict]:
        """当天还没写进日记的素材。"""
        date = date or _today_local()
        self.sync_events()
        return self.ledger.pending_items(date)

    # ------------------------------------------------------------------
    def should_write(self, date: str | None = None) -> dict:
        """判断今天**是否该再记一笔**（一天可多则，不是写完就锁死）。

        计分只看**没写过**的新素材：写进某一则的素材会被打上 ``entry`` 消费掉。

        Returns:
            ``{date, score(=pending_score), threshold, detail, should_write,
                is_refresh, entries_today, pending_count, materials_count,
                total_score, reason}``
        """
        date = date or _today_local()
        self.sync_events()
        items = self.ledger.load(date)
        pending = [x for x in items if not x.get("entry")]
        score, detail = score_items(pending)
        total, _ = score_items(items)

        entries = self.ledger.entries(date)
        # 第一则门槛高一点；后续各则只是"接着记"，门槛略低
        threshold = _INSPIRE_THRESHOLD if entries == 0 else _NEXT_ENTRY_DELTA
        gap_min = _minutes_since(self.ledger.last_entry_at(date))
        cooling = gap_min is not None and gap_min < _MIN_GAP_MINUTES

        ok = bool(score >= threshold and not cooling)
        if entries == 0:
            reason = (
                f"攒够了素材（{score:.1f} ≥ {threshold}）"
                if score >= threshold
                else f"素材还不够（{score:.1f} < {threshold}），今天先不写"
            )
        elif cooling:
            reason = f"刚记下一笔（{gap_min:.0f} 分钟前），让它先沉淀一会儿"
        elif ok:
            reason = f"又攒了 {score:.1f} 分新素材，值得再记一笔"
        else:
            reason = (
                f"今天已记 {entries} 笔，新素材 {score:.1f} 分还没攒够 {threshold}"
            )

        return {
            "date": date,
            "score": round(score, 2),          # 下一则的进度（未消费素材分）
            "threshold": threshold,
            "detail": {k: round(v, 2) for k, v in detail.items()},
            "should_write": ok,
            "is_refresh": entries > 0,         # 兼容旧字段：今天是否已经动过笔
            "entries_today": entries,
            "pending_score": round(score, 2),
            "pending_count": len(pending),
            "total_score": round(total, 2),
            "materials_count": len(items),
            "reason": reason,
        }


# ----------------------------------------------------------------------
# 主动触发钩子（供 proactivity tick 调用）
# ----------------------------------------------------------------------
def write_entry_once(
    date: str,
    fresh: list[dict],
    badges: list[dict] | None = None,
    score: float = 0.0,
    base_dir: Path | str | None = None,
    insp: DiaryInspiration | None = None,
) -> Optional[dict]:
    """幂等写入一则日记；这批素材已被写过则返回 None。

    ## 为什么需要「进锁后再校验一次」

    三个调用方（后台 tick / GUI 按钮 / 独立 MCP 进程）都是在**锁外**先取一次
    pending 才进来的。第一个进锁的写完并 ``consume`` 之后，第二个手里那批素材
    其实已经进了日记——不做二次校验，就会写出**第二条重复的日记**。这正是
    萧哥 2026-09-14 报的「一条日记写成前后两条，得拼起来读」：一天里
    10:04 / 10:48 / 18:31 各撞了一对，文件 10 则而账本只记 9 则。

    所以本函数把「算 seq → LLM 渲染 → 落盘 → 消费素材」整段放进跨进程锁，
    并在锁内重新取 pending、与传入的 ``fresh`` 取交集：交集为空即说明已被别的
    触发源写掉，直接返回 None（调用方据此知道自己这一笔被"抢"了）。

    Returns:
        写了返回 ``{date, path, seq, at}``；没写（已被写好 / 写盘失败）返回 None。
    """
    try:
        from deskmate.growth.diary import DiaryWriter
    except Exception:  # pragma: no cover - 理论不可达
        return None
    try:
        insp = insp or DiaryInspiration(base_dir=base_dir)
        with diary_write_lock(insp.base):
            still = {_fingerprint(x) for x in insp.ledger.pending_items(date)}
            todo = [x for x in fresh if _fingerprint(x) in still]
            if not todo:
                return None  # 已被别的触发源写进日记

            writer = DiaryWriter(base_dir=insp.base)
            res = writer.append_entry(date, events=todo, badges=badges or [])

            # 消费掉这批素材 + 记录这一则，让「下一则」从零开始重新攒
            insp.ledger.consume(date, todo, res["seq"])
            insp.ledger.mark_entry(date, res["seq"], score)
            return res
    except Exception:
        return None  # 日记失败绝不阻断主动推理主流程


def maybe_write_diary(base_dir: Path | str | None = None) -> Optional[dict]:
    """灵感驱动的日记写入钩子。

    与 ``maybe_trigger_interview`` 同一范式：纯规则、零 LLM、满足条件才发生。
    不同之处：日记是「她自己动笔」，不打断用户，所以**不需要用户在场**，
    也不占用 SUGGEST 通道——攒够了就安静写下来。

    真正落笔交给 ``write_entry_once``（跨进程锁 + 幂等）——因为除本入口外，
    GUI 的「生成今日日记」按钮与独立的 proactivity MCP 进程也会写日记。

    Returns:
        写了返回 payload（含 path/score/reason/is_refresh）；没写返回 None。
    """
    try:
        from deskmate.growth.diary import DiaryWriter

        insp = DiaryInspiration(base_dir=base_dir)
        verdict = insp.should_write()
        if not verdict.get("should_write"):
            return None

        date = verdict["date"]
        # 只写「还没写进日记」的素材——第二则若带上第一则用过的素材，
        # 就会把早上那篇复述一遍（重复叙事比不写更伤）
        fresh = insp.ledger.pending_items(date)
        if not fresh:
            return None

        writer = DiaryWriter(base_dir=insp.base)
        # 徽章只在第一则记：后续补记再列一遍「今天解锁徽章」是冗余
        badges = writer._badges_on(date) if writer.entry_count(date) == 0 else []
        res = write_entry_once(
            date, fresh, badges, score=verdict["score"], base_dir=insp.base, insp=insp,
        )
        if not res:
            return None  # 已被别的触发源写掉（锁内校验拦下）

        return {
            "date": date,
            "path": res["path"],
            "entry": res["seq"],
            "entries_today": res["seq"],
            "at": res["at"],
            "score": verdict["score"],
            "detail": verdict["detail"],
            "is_refresh": verdict["is_refresh"],
            "reason": verdict["reason"],
        }
    except Exception:
        return None  # 日记失败绝不阻断主动推理主流程
