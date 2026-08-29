"""P2-5 连续情感关怀——情感时间线 + 主动关怀记忆。

核心：「记得你上周赶稿压力大 → 今天主动问进度」，把亲密度变成连续情感智能。

- 每日情感快照：从 growth state（mood/trust/intimacy + 当日事件）推断情感标签
  （压力大/忙碌/顺利/低落/平静），存入 ``~/.deskmate/growth/emotion_timeline.json``。
- 情感回顾（lookback）：过去 N 天的情感趋势 + 关键事件，输出一段「情感记忆」文本，
  注入 proactive judge context，让 LLM 决定是否主动关怀 + 说什么。
- 关怀去重：同一主题 7 天内不重复发起（care_log 记录）。

独立于 memory 层（不碰 memory manager，WorkBuddy P2-2/P2-3 在向量/长期模式层）。
零 LLM、fail-open。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any


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


# 事件类型 → 情感信号
_POSITIVE_EVENTS = {"creation_approved", "suggestion_accepted", "skill_evolved", "memory_learned"}
_NEGATIVE_EVENTS = {"creation_rework", "suggestion_ignored", "user_corrected"}
_CREATIVE_EVENTS = {"writing", "roundtable", "report", "manuscript", "creation_approved", "creation_rework"}


def _infer_emotion(mood: float, events: dict[str, int]) -> tuple[str, str]:
    """从 mood + 当日事件推断情感标签 + 简短说明。

    Returns: (state, note)
    state: 压力大 / 忙碌 / 顺利 / 低落 / 平静
    """
    neg = sum(events.get(k, 0) for k in _NEGATIVE_EVENTS)
    pos = sum(events.get(k, 0) for k in _POSITIVE_EVENTS)
    creative = sum(events.get(k, 0) for k in _CREATIVE_EVENTS)
    total = sum(events.values())

    # 低落：mood 明显偏低且有负反馈
    if mood < 65 and neg > 0:
        return "低落", f"心情偏低（{mood:.0f}）且有 {neg} 次不顺（打回/纠正/忽略）"
    # 压力大：有打回/纠正，或创作活动多且 mood 不高
    if neg > 0 or (creative >= 2 and mood < 80):
        reason = []
        if neg > 0:
            reason.append(f"{neg} 次不顺")
        if creative >= 2:
            reason.append(f"{creative} 项创作活动")
        return "压力大", "、".join(reason)
    # 顺利：有正反馈且 mood 不低
    if pos > 0 and mood >= 78:
        return "顺利", f"{pos} 次正反馈（定稿/采纳/学到），心情 {mood:.0f}"
    # 忙碌：事件多或创作活动多
    if total >= 3 or creative >= 1:
        return "忙碌", f"{total} 次互动，{creative} 项创作"
    # 平静
    return "平静", f"心情 {mood:.0f}，节奏平稳"


class EmotionTimeline:
    """情感时间线：每日快照 + 回顾 + 关怀去重。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.path = base / "growth" / "emotion_timeline.json"
        self.care_log_path = base / "growth" / "care_log.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._care_log: dict[str, str] = {}  # theme -> last_date
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
        try:
            if self.care_log_path.is_file():
                self._care_log = json.loads(self.care_log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._care_log = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    def _save_care(self) -> None:
        try:
            self.care_log_path.parent.mkdir(parents=True, exist_ok=True)
            self.care_log_path.write_text(
                json.dumps(self._care_log, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    def record_today(self, engine: Any) -> dict[str, Any]:
        """从 growth engine 生成当日情感快照（幂等：同一天更新覆盖）。

        Returns: 当日快照 dict。
        """
        today = _today_iso()
        st = getattr(engine, "state", None)
        if st is None:
            return self._data.get(today, {})

        mood = float(getattr(st, "mood", 80.0) or 80.0)
        trust = float(getattr(st, "trust", 0.5) or 0.5)
        intimacy = float(getattr(st, "intimacy", 0.0) or 0.0)

        # 统计当日事件
        events: dict[str, int] = {}
        for e in getattr(st, "recent_events", []) or []:
            if not isinstance(e, dict):
                continue
            if _evt_date(str(e.get("ts", ""))) == today:
                k = str(e.get("kind", ""))
                if k:
                    events[k] = events.get(k, 0) + 1

        state, note = _infer_emotion(mood, events)
        snapshot = {
            "date": today,
            "mood": round(mood, 1),
            "trust": round(trust, 2),
            "intimacy": round(intimacy, 1),
            "events": events,
            "event_count": sum(events.values()),
            "state": state,
            "note": note,
        }
        self._data[today] = snapshot
        self._save()
        return snapshot

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

        # 最近一天的状态
        latest = snaps[-1]
        lines.append(f"- 最近状态（{latest['date']}）：{latest['state']}——{latest['note']}")

        # 关怀建议（基于趋势）
        if "压力大" in state_counts or "低落" in state_counts:
            lines.append("- 关怀提示：用户近期有压力/低落信号，可主动关心进度和状态（如「最近赶稿辛苦了，今天进度怎么样？」）。")
        elif state_counts.get("顺利", 0) >= len(snaps) / 2:
            lines.append("- 关怀提示：用户近期状态顺利，可在合适时机肯定进展或聊下一步计划。")
        else:
            lines.append("- 关怀提示：用户节奏平稳，可按正常节奏互动，不必过度打扰。")

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
