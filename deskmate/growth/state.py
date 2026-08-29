"""GrowthState — 成长状态持久化。

存储位置：~/.deskmate/growth.json
所有数值都有真实行为后果（见 modulate.py），不是展示用空数字。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# 勋章解锁时间的「不可考」标记：体系建立之前就已达成、无从查证确切日期的勋章，
# 一律用它占位而不编造日期。前端据此显示「早期补录」。
BACKFILLED_MARK = "backfilled"


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
    # 关系深度（8.2 长跑设计）：关系数值封顶后，溢出转入这里累积，
    # 由深度派生「关系阶段」（初识→熟络→默契→知己→灵魂伴侣）。
    # 让「数值满格」之后仍有可感知的进化，而不是死在数值上限。
    bond_depth: float = 0.0

    level: int = 1
    growth_value: int = 0
    skill_proficiency: dict[str, SkillProficiency] = field(default_factory=dict)

    # 活跃度统计（供可视化面板的连续天数 / 总对话数）
    total_chats: int = 0                      # 累计对话轮数
    active_dates: list[str] = field(default_factory=list)  # 活跃日期 ISO (YYYY-MM-DD)
    memories_learned: int = 0                # 累计已学事实条数（供成就）

    # 通用计数器（勋章体系的统计量：编校章节/字数、圆桌次数、深夜对话等）
    # 键名统一由 badges.COUNTER_* 常量定义，禁止在业务代码里硬编码字符串。
    # 用 dict 而非散字段，是为了新增指标时不必改动 dataclass 与读写逻辑。
    counters: dict[str, int] = field(default_factory=dict)

    # 勋章解锁记录：badge_id -> ISO 时间字符串，或 BACKFILLED_MARK（时间不可考）
    badges_unlocked: dict[str, str] = field(default_factory=dict)

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
            "bond_depth": round(self.bond_depth, 1),
            "level": self.level,
            "growth_value": self.growth_value,
            "skill_proficiency": {
                name: p.to_dict() for name, p in self.skill_proficiency.items()
            },
            "total_chats": self.total_chats,
            "active_dates": self.active_dates[-365:],  # 最多保留近一年
            "memories_learned": self.memories_learned,
            "counters": dict(self.counters),
            # 勋章解锁记录必须完整保留，绝不截断——这是「长久化」的根本
            "badges_unlocked": dict(self.badges_unlocked),
            "trust_controls_proactive": self.trust_controls_proactive,
            "updated_at": self.updated_at,
            "recent_events": self.recent_events[-50:],
        }


def _num(data: dict, key: str, default, cast):
    """逐字段安全取值：单个字段坏掉只影响它自己，不牵连整份状态。

    这一点对勋章体系至关重要——旧实现里任何一个字段解析失败都会整体回退
    默认值，等于把用户攒了很久的勋章记录一次性抹掉。
    """
    try:
        v = data.get(key, default)
        return cast(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def load_state(base_dir: Path | str | None = None) -> GrowthState:
    """加载成长状态；不存在或损坏时返回默认值（不抛异常）。"""
    path = _state_path(base_dir)
    if not path.is_file():
        return GrowthState()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return GrowthState()
    if not isinstance(data, dict):
        return GrowthState()

    st = GrowthState(
        intimacy=_num(data, "intimacy", 0.0, float),
        trust=_num(data, "trust", 0.5, float),
        empathy=_num(data, "empathy", 0.0, float),
        mood=_num(data, "mood", 80.0, float),
        vitality=_num(data, "vitality", 100.0, float),
        bond_depth=_num(data, "bond_depth", 0.0, float),
        level=_num(data, "level", 1, int),
        growth_value=_num(data, "growth_value", 0, int),
        trust_controls_proactive=bool(data.get("trust_controls_proactive", True)),
        updated_at=str(data.get("updated_at", "") or ""),
        total_chats=_num(data, "total_chats", 0, int),
        memories_learned=_num(data, "memories_learned", 0, int),
    )

    raw_dates = data.get("active_dates")
    if isinstance(raw_dates, list):
        st.active_dates = [str(d) for d in raw_dates if d]

    raw_events = data.get("recent_events")
    if isinstance(raw_events, list):
        st.recent_events = [e for e in raw_events if isinstance(e, dict)]

    # 计数器：逐键容错，坏值跳过而非整表丢弃
    raw_counters = data.get("counters")
    if isinstance(raw_counters, dict):
        for k, v in raw_counters.items():
            try:
                st.counters[str(k)] = int(v)
            except (TypeError, ValueError):
                continue

    # 勋章解锁记录：同上，逐条容错，尽最大努力保住每一枚
    raw_badges = data.get("badges_unlocked")
    if isinstance(raw_badges, dict):
        for k, v in raw_badges.items():
            if k and isinstance(v, str):
                st.badges_unlocked[str(k)] = v

    raw_skills = data.get("skill_proficiency")
    if isinstance(raw_skills, dict):
        for name, p in raw_skills.items():
            if not isinstance(p, dict):
                continue
            st.skill_proficiency[str(name)] = SkillProficiency(
                uses=_num(p, "uses", 0, int),
                success_rate=_num(p, "success_rate", 0.0, float),
                last_used=str(p.get("last_used", "") or ""),
                proficiency=_num(p, "proficiency", 0.0, float),
            )
    return st


def save_state(state: GrowthState, base_dir: Path | str | None = None) -> None:
    """保存成长状态。

    写盘失败不抛异常（成长统计不该让主流程崩），但会记一条 warning——
    勋章解锁记录靠这一步落盘，静默失败等于丢勋章，必须留痕。
    """
    path = _state_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = _now()
        data = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        # 临时文件 + 原子 rename：先写 *.tmp 再 os.replace 覆盖，避免并发读写时
        # 另一个实例读到写了一半（被截断）的文件，从而误判为损坏、回退到全新默认
        # 态、再把真实进度整体覆盖掉——这正是「桌宠莫名重置」的根因。
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)  # POSIX/NT 均保证不会让读取方看到半截文件
    except OSError as e:
        logging.warning("growth state 写盘失败（勋章解锁可能未保存）: %s", e)


def _state_path(base_dir: Path | str | None) -> Path:
    base = Path(base_dir) if base_dir else _default_base_dir()
    return base / "growth.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
