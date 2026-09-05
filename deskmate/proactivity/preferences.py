"""P1-2 主动建议的事后反馈闭环——被接受的建议沉淀为「行为偏好」。

让主动本身「越用越懂」：用户反复接受某类提醒 → 记为行为偏好 →
judge 决策时注入「用户喜欢什么提醒」，同类建议更敢提、更贴需求。

- 独立文件 ``~/.deskmate/proactivity/preferences.json``（不碰 memory manager）。
- 零 LLM、fail-open；沉淀阈值 2 次，自动衰减（>30 天未再接受则不视为活跃偏好）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# 沉淀阈值：同一类型建议被接受达到该次数即视为「行为偏好」
ACCEPT_THRESHOLD = 2
# 活跃衰减天数：超过未再接受则不注入 judge
ACTIVE_DAYS = 30
# 偏好类型描述（建议类型 → 用户可读短语）
_TYPE_LABELS = {
    "plot_contradiction": "情节矛盾提醒",
    "character_inconsistency": "角色不一致提醒",
    "missing_foreshadow": "伏笔遗漏提醒",
    "timeline_error": "时间线错误提醒",
}


def _type_label(t: str) -> str:
    return _TYPE_LABELS.get(t, t)


@dataclass
class PreferenceEntry:
    type: str                    # 建议类型（如 plot_contradiction）
    accepted_count: int = 0      # 累计接受次数
    last_content: str = ""       # 最近一次被接受的建议内容
    first_ts: float = 0.0        # 首次接受时间
    last_ts: float = 0.0         # 最近接受时间

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "accepted_count": self.accepted_count,
            "last_content": self.last_content,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class PreferenceStore:
    """被接受建议 → 行为偏好的沉淀与读取。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        base = Path(base_dir) if base_dir else Path.home() / ".deskmate"
        self.path = base / "proactivity" / "preferences.json"
        self._prefs: dict[str, PreferenceEntry] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for t, raw in (data.get("preferences") or {}).items():
                    if isinstance(raw, dict):
                        self._prefs[t] = PreferenceEntry(
                            type=t,
                            accepted_count=int(raw.get("accepted_count", 0)),
                            last_content=str(raw.get("last_content", "")),
                            first_ts=float(raw.get("first_ts", 0.0)),
                            last_ts=float(raw.get("last_ts", 0.0)),
                        )
        except Exception:
            self._prefs = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "preferences": {
                            t: e.to_dict() for t, e in self._prefs.items()
                        },
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 偏好沉淀失败不阻断主流程

    def record_accepted(self, suggestion_type: str, content: str = "") -> None:
        """记录一次被接受的建议（同类累计，形成偏好）。"""
        t = suggestion_type.strip() or "general"
        now = time.time()
        e = self._prefs.get(t)
        if e is None:
            e = PreferenceEntry(type=t, first_ts=now, last_ts=now)
        e.accepted_count += 1
        e.last_ts = now
        if content:
            e.last_content = content[:200]
        self._prefs[t] = e
        self._save()
        # P2-3：把「接受建议」当作行为事件记录，供长期模式学习推断偏好
        try:
            from deskmate.proactivity.behavior_patterns import log_event

            log_event("suggestion_accepted", {"type": t, "content": content[:200]},
                      base_dir=self.base_dir)
        except Exception:
            pass

    def active_preferences(self, now: float | None = None) -> list[dict]:
        """活跃行为偏好（接受≥阈值 且 30 天内仍活跃）。"""
        now = now or time.time()
        out: list[dict] = []
        for e in self._prefs.values():
            if e.accepted_count < ACCEPT_THRESHOLD:
                continue
            if now - e.last_ts > ACTIVE_DAYS * 86400:
                continue  # 太久没再接受，弱化退出
            out.append(
                {
                    "type": e.type,
                    "label": _type_label(e.type),
                    "accepted_count": e.accepted_count,
                    "last_content": e.last_content,
                }
            )
        out.sort(key=lambda d: -d["accepted_count"])
        return out

    def to_context(self, now: float | None = None) -> str:
        """格式化供 judge 注入（无活跃偏好时返回空串）。"""
        prefs = self.active_preferences(now)
        if not prefs:
            return ""
        lines = [
            f"- 你常接受「{p['label']}」类提醒（已 {p['accepted_count']} 次），"
            f"这类提醒对他有价值，可更主动；最近一次：{p['last_content'][:60]}"
            for p in prefs
        ]
        return "\n".join(lines)




if __name__ == "__main__":
    import sys

    store = PreferenceStore()
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        for p in store.active_preferences():
            print(f"- {p['label']} ×{p['accepted_count']}: {p['last_content'][:40]}")
    else:
        print("用法: python -m deskmate.proactivity.preferences list")
