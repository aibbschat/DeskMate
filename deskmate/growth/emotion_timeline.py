"""P2-5 连续情感关怀——情感时间线 + 主动关怀记忆。

核心：「记得你上周赶稿压力大 → 今天主动问进度」，把亲密度变成连续情感智能。

- 每日情感快照：从**当日情绪**生成情感标签（压力大/忙碌/顺利/低落/平静），
  存入统一库 ``deskmate.db``（``json_blobs`` 表，``emotion_timeline`` key；
  旧 ``~/.deskmate/growth/emotion_timeline.json`` 首次访问自动幂等迁移并备份
  ``.bak``，见 ``deskmate/persistence.BlobStore``）。

  ⚠️ 2026-09-05（E1/E2/E3）判定的来源与顺序被整个换掉了：

  改之前：``mood`` + 当日事件 → 规则贴标签。而 mood 是个**恒为 80 的假数值**
  （全仓唯一的写入者是 tick_decay 的 ±0.1 收敛），于是
  「低落」分支（mood<65）从体系建立起**一次都没触发过**，
  而只要当天有一次打回，不管有多少次定稿，一律判「压力大」——一票否决。

  改之后：**他自己说的 > 我们观察的**。
    1. 日记自述（``emotion_sense``，LLM 读他当天写的日记，规则词库兜底）
    2. 行为事件推断（``_infer_emotion``，读持久的 ``daily_events``）
    3. 兜底平静
  并且 mood 反过来由情绪派生（``engine.set_mood_from_emotion``），
  **绝不能再拿 mood 去判情绪**——那是循环论证。

- 情感回顾（lookback）：过去 N 天的情感趋势 + 关键事件，输出一段「情感记忆」文本，
  注入 proactive judge context，让 LLM 决定是否主动关怀 + 说什么。
- 关怀去重：同一主题 7 天内不重复发起（care_log 记录，同为 json_blobs key）。

独立于 memory 层（不碰 memory manager，WorkBuddy P2-2/P2-3 在向量/长期模式层）。
行为推断零 LLM；日记自述用 LLM 但三道闸门 + kill switch（见 emotion_sense）。
fail-open。与 growth 同属「设备本地」数据：收进统一库仅物理统一，
不随跨端同步（sync.py 只导出 style/decision 派生包）。
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from deskmate.coerce import as_float
from deskmate.growth import emotion_sense
from deskmate.persistence import BlobStore

logger = logging.getLogger(__name__)


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _evt_date(ts: str) -> str:
    """UTC ISO 时间 → 本地日期。"""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return ""


def _days_back(day: str) -> int:
    """某天距今几天（决定读日记的回看窗口）。解析失败按 1 天算。"""
    try:
        d = datetime.date.fromisoformat(str(day)[:10])
    except (ValueError, TypeError):
        return 1
    return max(1, (datetime.date.today() - d).days + 1)


# 事件类型 → 情感信号
_POSITIVE_EVENTS = {"creation_approved", "suggestion_accepted", "skill_evolved", "memory_learned"}
_NEGATIVE_EVENTS = {"creation_rework", "suggestion_ignored", "user_corrected"}
_CREATIVE_EVENTS = {"writing", "roundtable", "report", "manuscript", "creation_approved", "creation_rework"}


def _infer_emotion(events: dict[str, int]) -> tuple[str, str]:
    """从当日事件推断情感标签 + 简短说明（**行为推断**，无日记时的兜底路径）。

    ⚠️ 2026-09-05（E2）重写的两条原则：

    1. **不再拿 mood 当输入。** mood 现在是情绪的派生结果（E1），再拿它判情绪
       就是自己证自己。旧签名是 ``_infer_emotion(mood, events)``。
    2. **单次不顺不再一票否决。** 旧逻辑是 ``if neg > 0: 压力大``——
       一天里定稿三次、打回一次，整天被判「压力大」，这不合理。
       现在看的是**正负的对比**和**活跃度**。

    Returns: (state, note)
    state: 压力大 / 忙碌 / 顺利 / 低落 / 平静
    """
    neg = sum(events.get(k, 0) for k in _NEGATIVE_EVENTS)
    pos = sum(events.get(k, 0) for k in _POSITIVE_EVENTS)
    creative = sum(events.get(k, 0) for k in _CREATIVE_EVENTS)
    total = sum(events.values())

    # ① 低落：不顺 + 几乎没动静。
    #    区分「低落」与「压力大」的可观测分界是**活跃度**：不顺但还在忙 = 压力大，
    #    不顺且一整天没什么互动 = 低落。旧实现用 mood<65 判，而 mood 恒为 80，
    #    这个分支有史以来一次都没走到过。
    if neg >= 2 and neg > pos and total <= 3:
        return "低落", f"{neg} 次不顺，且当天几乎没有其它互动"

    # ② 压力大：不顺明显压过了顺，而且人还在忙
    if neg >= 2 and neg > pos:
        return "压力大", f"{neg} 次不顺、{pos} 次顺利，共 {total} 次互动"

    # ③ 只打回一次、且当天创作量不小：创作不顺确实构成压力（创作是他的主业）
    if neg == 1 and pos == 0 and creative >= 2:
        return "压力大", f"1 次不顺，且当天有 {creative} 项创作活动"

    # ④ 顺利：有正反馈，且不比负反馈少（打回一次、定稿两次，仍算顺利）
    if pos > 0 and pos >= neg:
        return "顺利", f"{pos} 次正反馈（定稿/采纳/学到）"

    # ⑤ 忙碌：没有明显不顺，但事情多
    if creative >= 2 or total >= 5:
        return "忙碌", f"{total} 次互动，{creative} 项创作"

    # ⑥ 平静（含「有一两次小不顺但整体平稳」——不再整天被一次打回抹掉）
    if neg:
        return "平静", f"节奏平稳（{neg} 次不顺未影响整体）"
    return "平静", "节奏平稳，没有明显波动"


class EmotionTimeline:
    """情感时间线：每日快照 + 回顾 + 关怀去重。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.base = base
        # legacy 路径：仅用于首次访问时幂等迁移进统一库（见 BlobStore.get legacy_path）
        self.path = base / "growth" / "emotion_timeline.json"
        self.care_log_path = base / "growth" / "care_log.json"
        # 统一库：与认知层/日记/成长画像同一物理文件（备份原子单元）
        self._blob = BlobStore(db_path=base / "deskmate.db")
        self._data: dict[str, dict[str, Any]] = {}
        self._care_log: dict[str, str] = {}  # theme -> last_date
        self._load()

    def _load(self) -> None:
        data = self._blob.get(BlobStore.KEY_EMOTION, legacy_path=self.path)
        self._data = data if isinstance(data, dict) else {}
        care = self._blob.get(BlobStore.KEY_CARE_LOG, legacy_path=self.care_log_path)
        self._care_log = care if isinstance(care, dict) else {}

    def _save(self) -> None:
        self._blob.set(BlobStore.KEY_EMOTION, self._data)

    def _save_care(self) -> None:
        self._blob.set(BlobStore.KEY_CARE_LOG, self._care_log)

    def record_today(self, engine: Any) -> dict[str, Any]:
        """生成当日情感快照（幂等：同一天更新覆盖）。

        判定优先级：**日记自述 > 行为事件推断 > 兜底平静**。
        自述是他亲口说的，推断是我们观察的——说的不许被猜的盖过。

        Returns: 当日快照 dict。
        """
        today = _today_iso()
        st = getattr(engine, "state", None)
        if st is None:
            return self._data.get(today, {})

        # ⚠ 必须用 as_float 而不是 ``float(x or 0.5)``：trust 的合法下限就是 0.0，
        # 被 `or` 吞掉后快照里会写成 0.5 —— 实测让情感时间线凭空"冻结 17 天"
        # （2026-09-21 定位：真实值一直是 0.0）。
        trust = as_float(getattr(st, "trust", None), 0.5)
        intimacy = as_float(getattr(st, "intimacy", None), 0.0)
        events = self._events_for(today, st)
        # 一次性取好今天的日记再传进去，免得 _resolve 里又读一遍库
        diary_texts = self._diary_texts_by_day(days=1).get(today)
        # 已落库的自述（访谈回答/聊天片段）：与日记合并后一起判（F1/F2）
        prev = self._data.get(today)
        extra = self._statements_for(today)
        resolved = self._resolve(
            today, events, prev=prev, diary_texts=diary_texts, extra_texts=extra
        )
        state = str(resolved["state"])
        mood = float(resolved["mood"])
        source = str(resolved["source"])
        confidence = float(resolved["confidence"])
        evidence = str(resolved["evidence"])

        snapshot = {
            "date": today,
            "mood": round(mood, 1),
            "trust": round(trust, 2),
            "intimacy": round(intimacy, 1),
            "events": events,
            "event_count": sum(events.values()),
            "state": state,
            "note": str(resolved["note"]),
            # 来源可追溯：stated=他自己写的日记，inferred=我们从行为推的。
            # 下游（花园定色、关怀话术）据此决定敢不敢拿它当真。
            "source": source,
            "confidence": confidence,
            "evidence": evidence,
            # 自述内容指纹：内容没变就不必重问模型（tick 一天跑很多次）。
            # 含日记 + 访谈 + 聊天片段——任一条变了就重判。
            "statement_digest": str(resolved.get("digest", "") or ""),
            # 兼容旧字段：只算日记部分，老数据仍可读
            "diary_digest": emotion_sense.digest(diary_texts or []),
            # 日记之外的自述（访谈/聊天），记下来供合并重判与追溯
            "statements": extra,
        }
        self._data[today] = snapshot
        self._save()
        # 反写 mood：它是情绪的**结果**，不是输入（E1）
        self._write_back_mood(engine, today, mood, source)
        return snapshot

    # ------------------------------------------------------------------
    # 自述登记（F1 访谈 / F2 聊天片段）
    # ------------------------------------------------------------------
    def _statements_for(self, day: str) -> list[str]:
        snap = self._data.get(day)
        if not isinstance(snap, dict):
            return []
        return [str(t) for t in (snap.get("statements") or []) if str(t or "").strip()]

    def record_statement(
        self,
        engine: Any,
        text: str,
        day: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """把他的一段自述并入某天的情感判定（F1/F2 的落点）。"""
        return self.record_statements(engine, [text], day=day)

    def record_statements(
        self,
        engine: Any,
        texts: list[str],
        day: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """把他某天的多段自述并入情感判定（F1/F2 的落点）。

        访谈回答、聊天里他明确说过的话，都是自述——不该只因为没写进日记
        就被当成不存在。本方法把它们登记到当天，与日记**合并后重判**。

        为什么走批量：F2 一次可能补进好几条长期记忆，逐条登记就会逐条
        问模型，token 烧在同一天的重复劳动上。合并成一次判定。

        ⚠ 读不出情绪就**不表态**：返回 None 且不动快照的 state。
        一句"书稿出版后就是商品"是认知表态，不是心情，硬判成平静或低落
        都是在替他编心情——宁可留着昨天的结果。

        Args:
            engine: GrowthEngine（用于取事件与反写 mood），可为 None。
            texts: 他说的原话，可多段。
            day: 归属哪天，默认今天。

        Returns: 更新后的快照；读不出情绪或无新文本 → None。
        """
        fresh = []
        for t in texts or []:
            s = str(t or "").strip()
            if s:
                fresh.append(s)
        if not fresh:
            return None
        day = day or _today_iso()

        saved = self._statements_for(day)
        # 同一句重复提交（前端重试 / tick 重扫）→ 不再判一次
        new = [t for t in fresh if t not in saved]
        if not new:
            return self._data.get(day)

        saved = saved + new
        # 读当天日记：补录历史某天时也要能读到——否则那天只剩访谈/聊天，
        # 缺了日记这个主来源，模型容易读不出情绪而选择不表态。
        diary_texts = self._diary_texts_by_day(days=_days_back(day)).get(day)
        events = self._events_for(day, getattr(engine, "state", None))
        prev = self._data.get(day)
        resolved = self._resolve(
            day, events, prev=prev, diary_texts=diary_texts, extra_texts=saved
        )

        snap = dict(prev) if isinstance(prev, dict) else {"date": day}
        snap.update({
            "date": day,
            "events": events,
            "event_count": sum(events.values()),
            "statements": saved,
            "statement_digest": str(resolved.get("digest", "") or ""),
        })
        if str(resolved["source"]) == "stated":
            snap.update({
                "state": str(resolved["state"]),
                "note": str(resolved["note"]),
                "mood": round(float(resolved["mood"]), 1),
                "source": "stated",
                "confidence": float(resolved["confidence"]),
                "evidence": str(resolved["evidence"]),
            })
            self._data[day] = snap
            self._save()
            if engine is not None:
                self._write_back_mood(engine, day, snap["mood"], "stated")
            return snap

        # 读不出情绪：只登记自述，不改判定（下次又有一句时再一起判）
        self._data[day] = snap
        self._save()
        return None

    # ------------------------------------------------------------------
    # 判定的三个来源
    # ------------------------------------------------------------------
    @staticmethod
    def _events_for(day: str, st: Any) -> dict[str, int]:
        """取某一天的事件计数。

        优先读 ``daily_events``（按天的持久账）。它不是 recent_events——
        后者落盘时被截到最后 50 条，是个会滚动的窗口：跨天回看时前几天的
        事件早被挤走了，拿它统计必然漏。老状态的 daily_events 为空时
        才回落到 recent_events 过滤（结果可能不完整，但总比没有强）。
        """
        daily = getattr(st, "daily_events", None)
        if isinstance(daily, dict) and daily.get(day):
            return {str(k): int(v) for k, v in daily[day].items() if str(k)}

        events: dict[str, int] = {}
        for e in getattr(st, "recent_events", []) or []:
            if not isinstance(e, dict):
                continue
            if _evt_date(str(e.get("ts", ""))) == day:
                k = str(e.get("kind", ""))
                if k:
                    events[k] = events.get(k, 0) + 1
        return events

    def _diary_texts_by_day(self, days: int = 1) -> dict[str, list[str]]:
        """读最近 N 天他**自己写的**日记正文，按本地日分组。

        只收 author='me'：小桔写的日记（author='xiaoju'）是她的心情，
        不能拿来当他的情绪证据——那会变成自己给自己定色。

        fail-open：读不到就返回空 dict，调用方回落到行为推断。
        """
        try:
            from deskmate.growth.diary_store import DiaryStore

            store = DiaryStore(base_dir=self.base)
            page = store.get_posts(limit=200)
            items = page.get("items", []) if isinstance(page, dict) else []
        except Exception as e:
            logger.debug("读日记失败，跳过自述判定: %s", e)
            return {}

        cutoff = (datetime.date.today() - datetime.timedelta(days=max(0, days - 1))).isoformat()
        by_day: dict[str, list[str]] = {}
        for post in items:
            if not isinstance(post, dict):
                continue
            if str(post.get("author", "me")) != "me":
                continue
            content = str(post.get("content", "") or "").strip()
            if not content:
                continue
            day = _evt_date(str(post.get("createdAt", "")))
            if not day or day < cutoff:
                continue
            by_day.setdefault(day, []).append(content)
        # 每天内部按时间正序（get_posts 是倒序），保证 digest 稳定
        return {d: sorted(texts) for d, texts in by_day.items()}

    def _sense_stated(self, texts: list[str], prev: Optional[dict]) -> Optional[dict]:
        """从他自己说的话里读当天的心情。

        文本来源不限于日记：访谈回答、聊天里他明确说过的也算（F1/F2）。
        都是他亲口说的，只是载体不同——写在日记本里和回答提问时不该有区别对待。

        Returns:
            - 读出情绪 → {"state", "mood", "confidence", "evidence", "source": "stated", "digest"}
            - 读不出 / 这批话已经问过了 → {"digest": ..., "skip": True}
              （skip 是给调用方的信号：别再问模型了，直接走行为推断）

        为什么要 skip 这一档：tick 一天跑很多次，若某篇日记本就没什么情绪，
        每次 tick 都重新问一遍模型，会把 token 烧在注定拿不到答案的地方。
        内容指纹一变（他又补写了一则、又答了一道访谈题）就重新判。
        """
        dg = emotion_sense.digest(texts)
        if isinstance(prev, dict) and prev.get("statement_digest") == dg:
            if prev.get("source") == "stated" and prev.get("state"):
                return {
                    "state": str(prev.get("state", "")),
                    "mood": float(prev.get("mood", 0) or 0),
                    "confidence": float(prev.get("confidence", 0) or 0),
                    "evidence": str(prev.get("evidence", "") or ""),
                    "source": "stated",
                    "digest": dg,
                }
            return {"digest": dg, "skip": True}

        res = emotion_sense.sense(texts)
        if not res:
            return {"digest": dg, "skip": True}  # 读不出来 → 不表态
        res["digest"] = dg
        return res

    def _resolve(
        self,
        day: str,
        events: dict[str, int],
        prev: Optional[dict] = None,
        diary_texts: Optional[list[str]] = None,
        extra_texts: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """定夺这一天的情绪。

        Args:
            extra_texts: 日记之外的自述（访谈回答、聊天里他明确说过的）。
                与日记**合并后一起判**，不是二选一——不能因为他这天没写日记，
                就把访谈里说的"最近很累"当成不存在。

        Returns: {"state", "note", "mood", "source", "confidence", "evidence", "digest"}
        """
        # ① 他自己说的话：日记 + 其它自述（访谈/聊天）
        if diary_texts is None:
            diary_texts = self._diary_texts_by_day(days=1).get(day)
        stated = [t for t in list(diary_texts or []) if t]
        stated += [t for t in (extra_texts or []) if str(t or "").strip()]

        stale_digest = ""
        if stated:
            got = self._sense_stated(stated, prev)
            if got and got.get("state") and not got.get("skip"):
                ev = str(got.get("evidence", "") or "")
                note = f"他自己说的：{ev[:40]}" if ev else "根据他当天说过的话"
                return {
                    "state": str(got["state"]),
                    "note": note,
                    "mood": float(got.get("mood") or 0),
                    "source": "stated",
                    "confidence": float(got.get("confidence") or 0),
                    "evidence": ev,
                    "digest": str(got.get("digest", "") or ""),
                }
            # 没读出情绪（或这批已问过）：记住指纹，下次不再白问
            stale_digest = str((got or {}).get("digest", "") or "")

        # ② 行为事件推断（没有自述、或自述里读不出情绪）
        state, note = _infer_emotion(events)
        return {
            "state": state,
            "note": note,
            "mood": emotion_sense.STATE_MOOD.get(state, 78.0),
            "source": "inferred",
            # 行为推断的置信度固定偏低：看的是事件计数，不是他的表达
            "confidence": 0.4,
            "evidence": "",
            "digest": stale_digest,
        }

    @staticmethod
    def _write_back_mood(engine: Any, day: str, mood: float, source: str) -> None:
        """把结算出的心情写回 growth state（E1：mood 是派生值）。"""
        setter = getattr(engine, "set_mood_from_emotion", None)
        if not callable(setter):
            return
        try:
            setter(mood, source, day)
        except Exception as e:
            logger.debug("mood 反写失败（不影响快照）: %s", e)

    def backfill_from_diary(self, engine: Any, days: int = 14) -> int:
        """用日记补记最近 N 天里「没判准或没判过」的快照。

        为什么需要：``record_today`` 只管今天。而用户会补写前几天的日记，
        历史快照里那些靠行为推断出来的标签（甚至可能是错的）不会自动修正。

        - 只覆盖 source 不是 "stated" 的日子（自述一旦落地就不再被推断覆盖）
        - 已经补过的（digest 相同）跳过，不重复烧 token

        Returns: 补记的天数。
        """
        if days <= 0:
            return 0
        by_day = self._diary_texts_by_day(days=days)
        if not by_day:
            return 0
        n = 0
        for day, texts in sorted(by_day.items()):
            prev = self._data.get(day)
            if isinstance(prev, dict) and prev.get("source") == "stated":
                continue  # 已经有自述结论，不动
            # 这批日记已经处理过（含「读不出情绪」的情况）→ 不再重试，省 token
            extra = self._statements_for(day)
            if isinstance(prev, dict) and prev.get("statement_digest") == emotion_sense.digest(
                list(texts) + extra
            ):
                continue
            events = self._events_for(day, getattr(engine, "state", None))
            resolved = self._resolve(
                day, events, prev=prev, diary_texts=texts, extra_texts=extra
            )
            if str(resolved["source"]) != "stated":
                continue  # 这篇日记读不出情绪，不硬凑
            snap = dict(prev) if isinstance(prev, dict) else {"date": day}
            snap.update(
                {
                    "date": day,
                    "state": str(resolved["state"]),
                    "note": str(resolved["note"]),
                    "mood": round(float(resolved["mood"]), 1),
                    "source": "stated",
                    "confidence": float(resolved["confidence"]),
                    "evidence": str(resolved["evidence"]),
                    "diary_digest": str(resolved.get("digest", "") or ""),
                    "statement_digest": str(resolved.get("digest", "") or ""),
                    "statements": extra,
                    "events": events,
                    "event_count": sum(events.values()),
                }
            )
            self._data[day] = snap
            n += 1
        if n:
            self._save()
            # 只把**最新那一天**的心情报回去；旧日补记不该覆盖今天的结果
            latest = max(by_day)
            self._write_back_mood(engine, latest, self._data[latest]["mood"], "stated")
        return n

    def snapshots(self, days: int = 7) -> list[dict[str, Any]]:
        """返回最近 N 天的快照（按日期升序）。"""
        today = datetime.date.today()
        out: list[dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            if d in self._data:
                out.append(self._data[d])
        return out

    def lookback(self, days: int = 7) -> str:
        """回顾过去 N 天的情感趋势 + 关键事件，输出「情感记忆」文本。

        无快照时返回空串（调用方应跳过注入）。
        """
        snaps = self.snapshots(days)
        if not snaps:
            return ""

        states = [s["state"] for s in snaps]
        state_counts: dict[str, int] = {}
        for s in states:
            state_counts[s] = state_counts.get(s, 0) + 1

        # 关键事件汇总
        all_events: dict[str, int] = {}
        for s in snaps:
            for k, v in (s.get("events") or {}).items():
                all_events[k] = all_events.get(k, 0) + v

        lines = [f"过去 {len(snaps)} 天情感回顾："]
        # 状态分布
        dist = "、".join(f"{s}{c}天" for s, c in sorted(state_counts.items(), key=lambda x: -x[1]))
        lines.append(f"- 状态分布：{dist}")

        # 关键事件
        event_labels = {
            "creation_approved": "定稿",
            "creation_rework": "打回",
            "suggestion_accepted": "采纳提醒",
            "suggestion_ignored": "忽略提醒",
            "user_corrected": "纠正",
            "writing": "整理文稿",
            "roundtable": "圆桌讨论",
            "memory_learned": "学到新事",
        }
        event_bits = []
        for k, v in sorted(all_events.items(), key=lambda x: -x[1]):
            label = event_labels.get(k, k)
            event_bits.append(f"{label}{v}次")
        if event_bits:
            lines.append(f"- 关键事件：{'、'.join(event_bits[:6])}")

        # 最近一天的状态——**必须标明来源**：他自己说的和我们猜的，
        # 对 judge 的分量完全不同。拿推断当自述去关怀，等于替他编心情。
        latest = snaps[-1]
        src = "他自己写的日记" if latest.get("source") == "stated" else "从行为推断"
        lines.append(
            f"- 最近状态（{latest['date']}）：{latest['state']}（{src}）——{latest['note']}"
        )
        stated = [s for s in snaps if s.get("source") == "stated"]
        if stated:
            lines.append(f"- 其中 {len(stated)} 天出自他本人写的日记，其余为行为推断")

        # 关怀建议（基于趋势）
        if "压力大" in state_counts or "低落" in state_counts:
            lines.append("- 关怀提示：用户近期有压力/低落信号，可主动关心进度和状态（如「最近赶稿辛苦了，今天进度怎么样？」）。")
        elif state_counts.get("顺利", 0) >= len(snaps) / 2:
            lines.append("- 关怀提示：用户近期状态顺利，可在合适时机肯定进展或聊下一步计划。")
        else:
            lines.append("- 关怀提示：用户节奏平稳，可按正常节奏互动，不必过度打扰。")
        if not stated:
            lines.append("- ⚠️ 近期没有他本人写的日记，上述状态全部来自行为推断，关怀时不要说得像他知道你了解他。")

        return "\n".join(lines)

    def should_care(self, theme: str, cooldown_days: int = 7) -> bool:
        """检查某主题的关怀是否在冷却期内（同一主题 N 天内不重复）。"""
        last = self._care_log.get(theme)
        if not last:
            return True
        try:
            last_date = datetime.date.fromisoformat(last)
            return (datetime.date.today() - last_date).days >= cooldown_days
        except ValueError:
            return True

    def mark_cared(self, theme: str) -> None:
        """记录已发起某主题的关怀。"""
        self._care_log[theme] = _today_iso()
        self._save_care()


if __name__ == "__main__":
    import sys

    tl = EmotionTimeline()
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        print(tl.lookback(days) or "（暂无情感快照）")
    else:
        print("用法: python -m deskmate.growth.emotion_timeline show [天数]")
