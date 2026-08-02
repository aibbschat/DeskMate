"""GrowthState — 成长状态持久化。

存储位置：~/.deskmate/growth.json
所有数值都有真实行为后果（见 modulate.py），不是展示用空数字。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _default_base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))


@dataclass
class SkillProficiency:
    """单个技能的熟练度状态。"""

    uses: int = 0                 # 使用次数
    success_rate: float = 0.0     # 滑动平均成功率 0-1
    last_used: str = ""           # ISO 日期
    proficiency: float = 0.0      # 熟练度 0-1（由 uses + success_rate + recency 派生）

    def to_dict(self) -> dict:
        return {
            "uses": self.uses,
            "success_rate": round(self.success_rate, 3),
            "last_used": self.last_used,
            "proficiency": round(self.proficiency, 3),
        }


@dataclass
class GrowthState:
    """成长状态。

    关系轨：
    - intimacy: 亲密度 0-100 → 注入 prompt 的语气温度（见 modulate.py）
    - trust:    信任感 0-1   → proactive 置信度阈值偏移（可关闭，见 modulate.py）
    - empathy:  默契度 0-100 → prefetch 记忆预算倍率（见 modulate.py）

    能力轨：
    - level:       等级（由 growth_value XP 派生）
    - growth_value 成长值（技能/记忆/交互行为累积）
    - skill_proficiency: 技能熟练度表（反哺 match_skills 排序 + 索引徽标）

    状态轨（照顾循环）：
    - mood:     心情 0-100（正反馈上升，忽略下降）
    - vitality: 活力 0-100（被使用后消耗，随时间恢复）
    """

    version: int = 1
    intimacy: float = 0.0
    trust: float = 0.5
    empathy: float = 0.0
    mood: float = 80.0
    vitality: float = 100.0

    level: int = 1
    growth_value: int = 0
    skill_proficiency: dict[str, SkillProficiency] = field(default_factory=dict)

    trust_controls_proactive: bool = True   # 信任感是否调制 proactive 阈值（可关闭）
    updated_at: str = ""

    # 最近事件（供前端时间线）
    recent_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "intimacy": round(self.intimacy, 1),
            "trust": round(self.trust, 3),
            "empathy": round(self.empathy, 1),
            "mood": round(self.mood, 1),
            "vitality": round(self.vitality, 1),
            "level": self.level,
            "growth_value": self.growth_value,
            "skill_proficiency": {
                name: p.to_dict() for name, p in self.skill_proficiency.items()
            },
            "trust_controls_proactive": self.trust_controls_proactive,
            "updated_at": self.updated_at,
            "recent_events": self.recent_events[-50:],
        }


def load_state(base_dir: Path | str | None = None) -> GrowthState:
    """加载成长状态；不存在或损坏时返回默认值（不抛异常）。"""
    path = _state_path(base_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            st = GrowthState(
                intimacy=float(data.get("intimacy", 0.0)),
                trust=float(data.get("trust", 0.5)),
                empathy=float(data.get("empathy", 0.0)),
                mood=float(data.get("mood", 80.0)),
                vitality=float(data.get("vitality", 100.0)),
                level=int(data.get("level", 1)),
                growth_value=int(data.get("growth_value", 0)),
                trust_controls_proactive=bool(data.get("trust_controls_proactive", True)),
                updated_at=data.get("updated_at", ""),
                recent_events=data.get("recent_events", []),
            )
            for name, p in (data.get("skill_proficiency") or {}).items():
                st.skill_proficiency[name] = SkillProficiency(
                    uses=int(p.get("uses", 0)),
                    success_rate=float(p.get("success_rate", 0.0)),
                    last_used=str(p.get("last_used", "")),
                    proficiency=float(p.get("proficiency", 0.0)),
                )
            return st
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return GrowthState()


def save_state(state: GrowthState, base_dir: Path | str | None = None) -> None:
    """保存成长状态。"""
    path = _state_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = _now()
        path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _state_path(base_dir: Path | str | None) -> Path:
    base = Path(base_dir) if base_dir else _default_base_dir()
    return base / "growth.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
