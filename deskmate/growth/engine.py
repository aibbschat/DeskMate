"""GrowthEngine — 成长数值计算。

把「技能使用 / 交互反馈」转化为熟练度、等级、亲密度等数值，
并通过 modulate_* 输出真实行为调制参数（技能排序权重、proactive 阈值、记忆预算）。

等级曲线与前端 LEVEL_THRESHOLDS 保持一致（成长值累积制，非了解条数制）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .state import GrowthState, SkillProficiency, load_state, save_state

# 等级阈值（成长值累积）
LEVEL_THRESHOLDS = [0, 5, 15, 30, 50, 80, 120, 170, 230, 300, 400, 520]

# 熟练度参数
PROFICIENCY_USES_TARGET = 20        # 使用 20 次接近熟练上限
PROFICIENCY_RECENCY_DAYS = 30       # 超过 30 天未用开始生疏
PROFICIENCY_RECENCY_DECAY = 0.85    # 生疏衰减系数

# 熟练度对匹配排序的权重（±20%）
MATCH_PROFICIENCY_WEIGHT = 0.4      # (proficiency - 0.5) * WEIGHT → [-0.2, +0.2]

# 事件 XP（成长值）
XP_SKILL_CREATED = 10
XP_SKILL_USED = 2
XP_SKILL_EVOLVED = 6
XP_MEMORY_LEARNED = 1
XP_SUGGESTION_ACCEPTED = 4
XP_SUGGESTION_IGNORED = 0

# 亲密度 / 信任感 / 默契度 / 心情 增量
INTIMACY_SKILL_CREATED = 2.0
INTIMACY_SKILL_USED = 0.5
INTIMACY_MEMORY_LEARNED = 1.0
INTIMACY_SUGGESTION_ACCEPTED = 3.0
INTIMACY_SUGGESTION_IGNORED = -1.0

TRUST_ACCEPTED = 0.03
TRUST_IGNORED = -0.02
TRUST_CLAMP = (0.0, 1.0)

EMPATHY_ACCEPTED = 2.0
EMPATHY_IGNORED = -1.0

MOOD_ACCEPTED = 4.0
MOOD_IGNORED = -3.0

# 记忆预算调制：默契度 0-100 → 倍率 [0.9, 1.3]
EMPATHY_BUDGET_MIN = 0.9
EMPATHY_BUDGET_MAX = 1.3

# proactive 阈值调制：信任感 0-1 → 偏移 [-0.08, +0.08]
TRUST_THRESHOLD_SPAN = 0.08


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_proficiency(uses: int, success_rate: float, last_used: str = "") -> float:
    """技能熟练度：使用量 + 成功率 + 时效性。

    0-1 区间：
    - uses 使用 20 次贡献 50%
    - success_rate 贡献 30%
    - 时效性贡献 20%（30 天未用则打 0.85 折）
    """
    import datetime

    use_part = clamp(uses / PROFICIENCY_USES_TARGET, 0.0, 1.0) * 0.5
    rate_part = clamp(success_rate, 0.0, 1.0) * 0.3
    recency_part = 0.2
    if last_used:
        try:
            last_dt = datetime.date.fromisoformat(last_used)
            days = (datetime.date.today() - last_dt).days
            if days > PROFICIENCY_RECENCY_DAYS:
                recency_part *= PROFICIENCY_RECENCY_DECAY
        except ValueError:
            pass
    return clamp(use_part + rate_part + recency_part, 0.0, 1.0)


def level_from_value(value: int) -> int:
    level = 1
    for i in range(1, len(LEVEL_THRESHOLDS)):
        if value >= LEVEL_THRESHOLDS[i]:
            level = i + 1
        else:
            break
    return level


class GrowthEngine:
    """成长引擎：加载状态 → 事件处理 → 行为调制参数输出。"""

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir else None
        self.state = load_state(self.base_dir)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self) -> None:
        save_state(self.state, self.base_dir)

    def _now_date(self) -> str:
        import datetime

        return datetime.date.today().isoformat()

    # ------------------------------------------------------------------
    # 行为调制参数（真实后果）
    # ------------------------------------------------------------------

    def modulate_skill_ranking(self, match_score: float, skill_name: str) -> float:
        """技能匹配排序调制：熟练度对分数加权（±20%）。

        熟练度高的技能更容易被选中 → 越用越顺手 → 效果更好 → 熟练度再升。
        """
        prof = self.get_proficiency(skill_name)
        factor = 1.0 + (prof - 0.5) * MATCH_PROFICIENCY_WEIGHT
        return match_score * factor

    def modulate_proactive_threshold(self, base_threshold: float) -> float:
        """proactive 置信度阈值调制：信任感越高 → 阈值越低 → 更敢主动。

        trust_controls_proactive=False 时返回原值（用户可关闭）。
        信任感 >0.5 → 阈值下调（更主动）；<0.5 → 阈值上调（更克制）。
        """
        if not self.state.trust_controls_proactive:
            return base_threshold
        offset = (self.state.trust - 0.5) * (2 * TRUST_THRESHOLD_SPAN)
        return base_threshold - offset

    def modulate_memory_budget(self, base_budget: int) -> int:
        """记忆注入预算调制：默契度越高 → 注入越多（记忆匹配更准，AI 更懂你）。"""
        ratio = EMPATHY_BUDGET_MIN + (self.state.empathy / 100.0) * (EMPATHY_BUDGET_MAX - EMPATHY_BUDGET_MIN)
        return int(base_budget * ratio)

    def modulate_proactive_frequency(self, base_interval_sec: float) -> float:
        """proactive 心跳间隔调制：活力低 → 间隔拉长（累了不打扰）。"""
        vitality = self.state.vitality
        # 活力 100 → 间隔 *0.85；活力 0 → 间隔 *1.6
        ratio = 1.6 - (vitality / 100.0) * 0.75
        return base_interval_sec * ratio

    def modulate_tone_prompt(self) -> str:
        """亲密度语气提示（注入 system prompt）。

        亲密度高 → 提示 AI 语气更亲切；低 → 保持礼貌克制。空串表示无需注入。
        """
        intimacy = self.state.intimacy
        if intimacy >= 60:
            return "你们已经很熟悉了（亲密度高）。可以自然地用轻松亲切的语气，不必拘谨，像老朋友一样。"
        if intimacy >= 25:
            return "你们的关系正在升温（亲密度中）。语气可以逐渐自然，偶尔带一点亲近感。"
        if intimacy >= 10:
            return "你们刚认识不久（亲密度低）。保持礼貌友善，不要太自来熟。"
        return ""

    # ------------------------------------------------------------------
    # 技能熟练度
    # ------------------------------------------------------------------

    def get_proficiency(self, skill_name: str) -> float:
        prof = self.state.skill_proficiency.get(skill_name)
        return prof.proficiency if prof else 0.0

    def record_skill_use(
        self,
        skill_name: str,
        success: bool = True,
        skill_store_uses: Optional[int] = None,
        skill_store_rate: Optional[float] = None,
    ) -> None:
        """技能使用：更新熟练度 + 成长值 + 亲密度。

        Args:
            skill_name: 技能名
            success: 本次是否成功
            skill_store_uses: 技能库里的累计使用次数（SkillStore frontmatter）
            skill_store_rate: 技能库里的成功率（SkillStore frontmatter）
        """
        now = self._now_date()
        prof = self.state.skill_proficiency.get(skill_name, SkillProficiency())
        prof.uses = (skill_store_uses if skill_store_uses is not None else prof.uses + 1)
        if skill_store_rate is not None:
            prof.success_rate = skill_store_rate
        elif success:
            # 无外部数据时本地滑动平均
            prof.success_rate = (prof.success_rate * (prof.uses - 1) + 1.0) / max(1, prof.uses)
        prof.last_used = now
        prof.proficiency = compute_proficiency(prof.uses, prof.success_rate, now)
        self.state.skill_proficiency[skill_name] = prof

        self.state.growth_value += XP_SKILL_USED
        self.state.intimacy = clamp(self.state.intimacy + INTIMACY_SKILL_USED, 0, 100)
        self._add_event("skill_used", skill_name=skill_name, success=success)
        self._apply_decay()
        self._recompute_level()
        self.save()

    def record_skill_created(self, skill_name: str) -> None:
        """技能创建：初始熟练度 + 成长值。"""
        self.state.skill_proficiency.setdefault(
            skill_name,
            SkillProficiency(uses=0, last_used=self._now_date()),
        )
        self.state.growth_value += XP_SKILL_CREATED
        self.state.intimacy = clamp(self.state.intimacy + INTIMACY_SKILL_CREATED, 0, 100)
        self._add_event("skill_created", skill_name=skill_name)
        self._recompute_level()
        self.save()

    def record_skill_evolved(
        self,
        skill_name: str,
        success: bool = True,
        skill_store_uses: Optional[int] = None,
        skill_store_rate: Optional[float] = None,
    ) -> None:
        """技能进化（成功执行 / 用户纠正后优化）：熟练度额外增长。"""
        prof = self.state.skill_proficiency.get(skill_name, SkillProficiency())
        prof.uses = (skill_store_uses if skill_store_uses is not None else prof.uses + 1)
        if skill_store_rate is not None:
            prof.success_rate = skill_store_rate
        prof.last_used = self._now_date()
        prof.proficiency = compute_proficiency(prof.uses, prof.success_rate, prof.last_used)
        self.state.skill_proficiency[skill_name] = prof

        self.state.growth_value += XP_SKILL_EVOLVED
        self.state.intimacy = clamp(self.state.intimacy + INTIMACY_SKILL_USED, 0, 100)
        self._add_event("skill_evolved", skill_name=skill_name)
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 交互反馈（proactive 建议接受/忽略）
    # ------------------------------------------------------------------

    def record_suggestion_feedback(self, accepted: bool) -> None:
        """用户对主动建议的反馈：信任感 / 默契度 / 心情 / 亲密度。

        接受 → 信任↑ 默契↑ 心情↑（越用越默契）
        忽略 → 信任↓ 默契↓ 心情↓
        """
        if accepted:
            self.state.trust = clamp(self.state.trust + TRUST_ACCEPTED, *TRUST_CLAMP)
            self.state.empathy = clamp(self.state.empathy + EMPATHY_ACCEPTED, 0, 100)
            self.state.mood = clamp(self.state.mood + MOOD_ACCEPTED, 0, 100)
            self.state.intimacy = clamp(self.state.intimacy + INTIMACY_SUGGESTION_ACCEPTED, 0, 100)
            self.state.growth_value += XP_SUGGESTION_ACCEPTED
            self._add_event("suggestion_accepted")
        else:
            self.state.trust = clamp(self.state.trust + TRUST_IGNORED, *TRUST_CLAMP)
            self.state.empathy = clamp(self.state.empathy + EMPATHY_IGNORED, 0, 100)
            self.state.mood = clamp(self.state.mood + MOOD_IGNORED, 0, 100)
            self.state.intimacy = clamp(self.state.intimacy + INTIMACY_SUGGESTION_IGNORED, 0, 100)
            self._add_event("suggestion_ignored")
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 记忆学习（了解度 → 默契度微增）
    # ------------------------------------------------------------------

    def record_memory_learned(self, count: int = 1) -> None:
        """每次记住新事实，默契度微增（记忆匹配变准）。"""
        if count <= 0:
            return
        self.state.empathy = clamp(self.state.empathy + INTIMACY_MEMORY_LEARNED * count, 0, 100)
        self.state.intimacy = clamp(self.state.intimacy + INTIMACY_MEMORY_LEARNED * count, 0, 100)
        self.state.growth_value += XP_MEMORY_LEARNED * count
        self._add_event("memory_learned", count=count)
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 照顾循环：活力 / 心情衰减
    # ------------------------------------------------------------------

    def _apply_decay(self) -> None:
        """活力消耗：每次被使用（技能/建议），活力小幅下降。"""
        self.state.vitality = clamp(self.state.vitality - 1.0, 0, 100)

    def tick_decay(self) -> None:
        """时间衰减（proactive tick 可调用）：活力缓慢恢复，心情向 80 收敛。"""
        # 活力随时间恢复（每次 tick 恢复 0.2）
        self.state.vitality = clamp(self.state.vitality + 0.2, 0, 100)
        # 心情向中性 80 缓慢收敛（避免长期低落或过高）
        if self.state.mood < 80:
            self.state.mood = clamp(self.state.mood + 0.1, 0, 100)
        elif self.state.mood > 80:
            self.state.mood = clamp(self.state.mood - 0.1, 0, 100)
        self.save()

    # ------------------------------------------------------------------
    # 等级
    # ------------------------------------------------------------------

    def _recompute_level(self) -> None:
        self.state.level = level_from_value(self.state.growth_value)

    def current_level_progress(self) -> dict:
        """当前等级进度（供前端进度条）。"""
        lv = self.state.level
        cur_base = LEVEL_THRESHOLDS[lv - 1]
        next_base = LEVEL_THRESHOLDS[lv] if lv < len(LEVEL_THRESHOLDS) else cur_base + 120
        span = max(1, next_base - cur_base)
        cur = self.state.growth_value - cur_base
        return {
            "level": lv,
            "cur": cur,
            "span": span,
            "progress": clamp(cur / span, 0, 1),
            "toNext": max(0, next_base - self.state.growth_value),
            "maxed": lv >= len(LEVEL_THRESHOLDS),
        }

    # ------------------------------------------------------------------
    # 事件日志 & 状态导出
    # ------------------------------------------------------------------

    def _add_event(self, kind: str, **extra) -> None:
        import datetime

        self.state.recent_events.append(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "kind": kind,
                **extra,
            }
        )

    def snapshot(self) -> dict:
        """完整状态快照（供前端渲染）。"""
        d = self.state.to_dict()
        d["level_progress"] = self.current_level_progress()
        d["proficiency_ranking_factor"] = {
            name: round(1.0 + (p.proficiency - 0.5) * MATCH_PROFICIENCY_WEIGHT, 3)
            for name, p in self.state.skill_proficiency.items()
        }
        return d
