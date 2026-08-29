"""P1-3 桌宠日记产品化——墨灵视角第一人称「今日小结」+ 每周回忆。

让「伙伴感」可感知：把每天 growth 信号（对话/创作/徽章/默契/偏好）转成
墨灵第一人称的日记（``~/.deskmate/diary/YYYY-MM-DD.md``），并支持每周
「我们的回忆」汇总（对话数/创作进度/解锁徽章/学到偏好/默契阶段）。

- 独立于 memory 层（P0-6/P0-7 范围外）：写入 ``<base>/diary/``，不碰 memory manager。
- 零 LLM、fail-open：有真实信号则如实记录；纯空转只写一句安静的陪伴，绝不编造。
- 幂等：``ensure_today()`` 当天已生成则不重复。
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from deskmate.growth.engine import GrowthEngine

# 事件 kind → 第一人称描述（无匹配时 fallback 到 kind 本身）
_EVENT_LABELS = {
    "chat": "你和我聊了天",
    "creation_approved": "你拍板定稿了一份创作",
    "creation_rework": "有一稿被你打回重做了",
    "suggestion_accepted": "你采纳了我的一条提醒",
    "suggestion_ignored": "有一条提醒你没有理会",
    "user_corrected": "你纠正了我一次（我会记着校准）",
    "memory_learned": "我学到了一些关于你的新事",
    "writing": "我们一起整理了文稿",
    "roundtable": "我们开了一次圆桌讨论",
    "report": "你让我出了一份报告",
    "skill_use": "你调用了一项技能",
    "skill_used": "你调用了一项技能",
    "skill_created": "我们一起建了个新技能",
    "skill_evolved": "一项技能又精进了",
}

# 默认事件描述（兜底）
_FALLBACK_LABEL = "今天发生了一点小事"


def _evt_date(ts: str) -> str:
    """UTC ISO 时间 → 本地日期 YYYY-MM-DD。"""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return ""


def _today_local() -> str:
    return datetime.date.today().isoformat()


def _weekday_cn(date_iso: str) -> str:
    try:
        return "一二三四五六日"[
            datetime.date.fromisoformat(date_iso).weekday()
        ]
    except ValueError:
        return ""


class DiaryWriter:
    """墨灵日记：第一人称今日小结 + 每周回忆。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.base = base
        self.diary_dir = base / "diary"
        self.engine = GrowthEngine(base_dir=base)

    # ------------------------------------------------------------------
    # 信号采集
    # ------------------------------------------------------------------
    def _event_counts(self, date: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.engine.state.recent_events:
            if not isinstance(e, dict):
                continue
            d = _evt_date(str(e.get("ts", "")))
            if d == date:
                k = str(e.get("kind", ""))
                counts[k] = counts.get(k, 0) + 1
        return counts

    def _badges_on(self, date: str) -> list[dict]:
        """当天解锁的徽章（badges_unlocked 时间戳转本地日期匹配）。"""
        out = []
        for badge in self.engine.badge_items():
            ts = badge.get("unlockedAt") or badge.get("unlocked_at")
            if ts and _evt_date(str(ts)) == date:
                out.append({"id": badge.get("id", ""), "name": badge.get("name", "")})
        return out

    def _top_domain(self) -> tuple[str, float] | None:
        d = self.engine.current_empathy_domains()
        top, val = d.get("top") or "", d.get("top_value") or 0.0
        return (top, val) if top and val > 0 else None

    def _state_summary(self) -> dict:
        st = self.engine.state
        return {
            "intimacy": round(float(getattr(st, "intimacy", 0.0) or 0.0), 1),
            "trust": round(float(getattr(st, "trust", 0.5) or 0.5), 2),
            "mood": round(float(getattr(st, "mood", 80.0) or 80.0), 1),
            "level": int(getattr(st, "level", 1) or 1),
            "total_chats": int(getattr(st, "total_chats", 0) or 0),
        }

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _render_diary(self, date: str, counts: dict[str, int],
                      badges: list[dict], top: tuple[str, float] | None,
                      st: dict) -> str:
        lines: list[str] = []
        lines.append(f"# {date} 周{_weekday_cn(date)} · 墨灵的日记\n")
        if counts:
            lines.append("今天又陪了你一会儿。")
            # 事件描述（优先有含义的，前 5 条）
            named = [
                _EVENT_LABELS.get(k, k)
                for k, c in sorted(counts.items(), key=lambda kv: -kv[1])
                if c > 0
            ]
            for n in named[:5]:
                lines.append(f"- {n}。")
            # 认可类正反馈单独提一句（被认可的感觉）
            if counts.get("creation_approved"):
                lines.append("- 你拍板定稿，被认可的感觉很好。")
            if counts.get("suggestion_accepted"):
                lines.append(f"- 你采纳了我 {counts['suggestion_accepted']} 条提醒——"
                             f"我也越来越懂你该听什么。")
            if counts.get("memory_learned"):
                lines.append("- 今天又记住了一些关于你的小事。")
        else:
            lines.append("今天没有太多动静。我安静地待着，等你忙完。")

        # 默契状态（阶段名暂不展示：bond_depth 由封顶溢出驱动，数值未触顶时
        # 会显示"初识"，与高默契矛盾，待 8.2 长跑完善后再接）
        lines.append(
            f"- 默契 {st['intimacy']}/100 · 信任 {st['trust']} · 心情 {st['mood']} · Lv.{st['level']}"
        )
        if badges:
            names = "、".join(b["name"] for b in badges)
            lines.append(f"- 今天解锁徽章：「{names}」。")
        if top:
            lines.append(f"- 我更懂你了：你在「{top[0]}」上最投入。")
        lines.append(
            f"\n> 累计对话 {st['total_chats']} 轮。这份日记由今日真实信号生成，"
            f"没有的我不编。"
        )
        return "\n".join(lines) + "\n"

    def _render_recap(self, days: int) -> str:
        today = _today_local()
        start = (
            datetime.date.today() - datetime.timedelta(days=days - 1)
        ).isoformat()
        dates = []
        for d in self.diary_dir.glob("*.md"):
            if start <= d.stem <= today:
                dates.append(d.stem)
        dates.sort()
        if not dates:
            return "（近几天还没有日记——多和我聊两句，回忆就会长出来。）"

        st = self.engine.state
        counts_all: dict[str, int] = {}
        for e in self.engine.state.recent_events:
            if isinstance(e, dict):
                d = _evt_date(str(e.get("ts", "")))
                if start <= d <= today:
                    k = str(e.get("kind", ""))
                    counts_all[k] = counts_all.get(k, 0) + 1
        badges = self.engine.badge_items()
        unlocked = [b.get("name", "") for b in badges if b.get("unlocked_at")]
        top = self._top_domain()

        lines = [f"# 我们的回忆 · 最近 {len(dates)} 天\n"]
        lines.append(f"这 {len(dates)} 天，我们一共聊了 {st.total_chats} 轮（累计），")
        bits = []
        if counts_all.get("creation_approved"):
            bits.append(f"定稿创作 {counts_all['creation_approved']} 份")
        if counts_all.get("suggestion_accepted"):
            bits.append(f"你采纳提醒 {counts_all['suggestion_accepted']} 次")
        if counts_all.get("writing"):
            bits.append(f"整理文稿 {counts_all['writing']} 次")
        lines.append("，".join(bits) + "。" if bits else "日子平平常常。")
        lines.append(
            f"- 默契 {round(st.intimacy, 1)}/100 · 信任 {round(st.trust, 2)} · Lv.{st.level}"
        )
        if unlocked:
            lines.append(f"- 已解锁徽章：{'、'.join(unlocked)}")
        if top:
            lines.append(f"- 我越来越懂你：你最常投入的是「{top[0]}」")
        lines.append("\n> 记忆是把我们一起走过的日子，慢慢攒成默契。")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def ensure_today(self) -> str:
        """生成今日日记（幂等：当天已生成则不重写）。返回日记路径。"""
        today = _today_local()
        path = self.diary_dir / f"{today}.md"
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return str(path)
        counts = self._event_counts(today)
        badges = self._badges_on(today)
        top = self._top_domain()
        st = self._state_summary()
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_diary(today, counts, badges, top, st),
                        encoding="utf-8")
        return str(path)

    def weekly_recap(self, days: int = 7) -> str:
        return self._render_recap(max(1, days))

    def list_diaries(self) -> list[str]:
        return sorted(p.stem for p in self.diary_dir.glob("*.md")) if self.diary_dir.is_dir() else []

    def read(self, date: str) -> str | None:
        p = self.diary_dir / f"{date}.md"
        return p.read_text(encoding="utf-8") if p.is_file() else None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    w = DiaryWriter()
    cmd = argv[0] if argv else "today"
    if cmd == "today":
        print(w.ensure_today())
    elif cmd == "weekly":
        days = int(argv[1]) if len(argv) > 1 else 7
        print(w.weekly_recap(days))
    elif cmd == "list":
        for d in w.list_diaries():
            print(d)
    elif cmd == "read" and len(argv) > 1:
        print(w.read(argv[1]) or f"（{argv[1]} 无日记）")
    else:
        print("用法: python -m deskmate.growth.diary [today|weekly [天]|list|read YYYY-MM-DD]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
