"""GrowthEngine — 成长数值计算。

把「技能使用 / 交互反馈」转化为熟练度、等级、亲密度等数值，
并通过 modulate_* 输出真实行为调制参数（技能排序权重、proactive 阈值、记忆预算）。

等级曲线与前端 LEVEL_THRESHOLDS 保持一致（成长值累积制，非了解条数制）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import badges as badge_registry
from .badges import BadgeContext, BadgeDef
from .state import BACKFILLED_MARK, GrowthState, SkillProficiency, load_state, save_state

# 等级阈值（成长值累积）
LEVEL_THRESHOLDS = [0, 5, 15, 30, 50, 80, 120, 170, 230, 300, 400, 520]

# recent_events 在 GrowthState.to_dict 里被截断到最后 50 条。
# 只有当日志长度 < 该上限时，才能断定它是完整历史、可用于回溯推断解锁时间。
EVENT_LOG_CAP = 50

# 内部计数器键（不对外展示，仅用于跨天统计与一次性迁移标记）
_C_WORDS_TODAY = "_words_today"
_C_WORDS_TODAY_DATE = "_words_today_date"
_C_BADGES_MIGRATED = "_badges_migrated"

# 「破镜重圆」判定：中断超过这么多天后回归
COMEBACK_GAP_DAYS = 30

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


def _now_iso() -> str:
    """当前 UTC 时间（秒精度 ISO 串），用作勋章解锁时间戳。"""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


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

        # 全新状态（从未落过盘）没有任何历史可补，直接把迁移标记置位，
        # 免得它的第一枚「初见」被误判成「早期补录」。
        if not self.state.updated_at and not self.state.badges_unlocked:
            self.state.counters.setdefault(_C_BADGES_MIGRATED, 1)

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
        self.state.memories_learned += count
        self._add_event("memory_learned", count=count)
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 活跃度统计（对话计数 / 连续天数）
    # ------------------------------------------------------------------

    def record_chat(self) -> None:
        """记录一次对话（累计总对话数 + 今日活跃日期，供连续天数统计）。

        由对话入口（run_turn 的 turn_start）调用，是可视化面板
        streakDays / totalChats 的真实数据源。

        同时顺带采集「奇遇」类勋章所需的时间线索：深夜/清晨时段、
        跨年守候、长期中断后归来。这些只在对话发生的当下才能观测到，
        事后无从补算，所以必须在这里记。
        """
        import datetime

        now = datetime.datetime.now()
        today = now.date().isoformat()

        # 长期中断后归来（在把今天加进 active_dates 之前判断）
        if today not in self.state.active_dates and self.state.active_dates:
            try:
                last = max(
                    datetime.date.fromisoformat(d)
                    for d in self.state.active_dates
                    if d
                )
                if (now.date() - last).days >= COMEBACK_GAP_DAYS:
                    self._bump(badge_registry.COUNTER_COMEBACK)
            except ValueError:
                pass

        if today not in self.state.active_dates:
            self.state.active_dates.append(today)
        self.state.total_chats += 1

        # 时段线索（用本地时间，符合作息直觉）
        hour = now.hour
        if 0 <= hour < 5:
            self._bump(badge_registry.COUNTER_LATE_NIGHT)
        elif 5 <= hour < 7:
            self._bump(badge_registry.COUNTER_EARLY_BIRD)

        # 跨年守候：公历 12/31 或 1/1 仍在动笔
        if (now.month, now.day) in ((12, 31), (1, 1)):
            self._bump(badge_registry.COUNTER_NEW_YEAR)

        self.save()

    # ------------------------------------------------------------------
    # 通用计数器（勋章统计量）
    # ------------------------------------------------------------------

    def _bump(self, key: str, amount: int = 1) -> None:
        """累加计数器（不落盘，由调用方统一 save）。"""
        if amount == 0:
            return
        self.state.counters[key] = self.state.counters.get(key, 0) + amount

    def get_counter(self, key: str) -> int:
        try:
            return int(self.state.counters.get(key, 0))
        except (TypeError, ValueError):
            return 0

    def record_writing(self, chapters: int = 0, words: int = 0) -> None:
        """记录一次文稿编校的产出量（章节数 + 字数）。

        除累计值外还维护「单日字数峰值」，供「一日千言」这枚隐藏勋章判定。
        跨天时自动清零当日累计。
        """
        import datetime

        if chapters <= 0 and words <= 0:
            return
        if chapters > 0:
            self._bump(badge_registry.COUNTER_CHAPTERS, chapters)
        if words > 0:
            self._bump(badge_registry.COUNTER_WORDS, words)

            today_key = int(datetime.date.today().strftime("%Y%m%d"))
            if self.get_counter(_C_WORDS_TODAY_DATE) != today_key:
                self.state.counters[_C_WORDS_TODAY_DATE] = today_key
                self.state.counters[_C_WORDS_TODAY] = 0
            self._bump(_C_WORDS_TODAY, words)
            today_total = self.get_counter(_C_WORDS_TODAY)
            if today_total > self.get_counter(badge_registry.COUNTER_MAX_DAILY_WORDS):
                self.state.counters[badge_registry.COUNTER_MAX_DAILY_WORDS] = today_total

        self._add_event("writing_reviewed", chapters=chapters, words=words)
        self.save()

    def record_roundtable(self) -> None:
        """记录一次创作圆桌召集。"""
        self._bump(badge_registry.COUNTER_ROUNDTABLES)
        self._add_event("roundtable_held")
        self.save()

    def record_report(self) -> None:
        """记录一次审阅报告生成。"""
        self._bump(badge_registry.COUNTER_REPORTS)
        self._add_event("report_generated")
        self.save()

    def record_manuscript(self, total: Optional[int] = None) -> None:
        """记录书稿入库。

        Args:
            total: 若调用方已知书稿总数，直接以该值为准（幂等，重复保存同一部
                书稿不会虚增）；否则简单累加。
        """
        if total is not None and total >= 0:
            self.state.counters[badge_registry.COUNTER_MANUSCRIPTS] = int(total)
        else:
            self._bump(badge_registry.COUNTER_MANUSCRIPTS)
        self._add_event("manuscript_saved")
        self.save()

    def _compute_streak_days(self) -> int:
        """从 active_dates 计算当前连续活跃天数（含今天）。

        若今天尚未对话，则从昨天起算，避免「今天还没聊就清零」。
        """
        import datetime

        dates = set(self.state.active_dates)
        if not dates:
            return 0
        today = datetime.date.today()
        if today.isoformat() not in dates:
            today = today - datetime.timedelta(days=1)
        streak = 0
        while today.isoformat() in dates:
            streak += 1
            today = today - datetime.timedelta(days=1)
        return streak

    # ------------------------------------------------------------------
    # 勋章体系
    # ------------------------------------------------------------------

    def _badge_context(self) -> BadgeContext:
        """把判定所需的派生量算好一次，供全部勋章共用。"""
        st = self.state
        return BadgeContext(
            state=st,
            streak_days=self._compute_streak_days(),
            active_days=len(set(st.active_dates)),
            skill_count=len(st.skill_proficiency),
            skill_uses=sum(p.uses for p in st.skill_proficiency.values()),
            max_level=len(LEVEL_THRESHOLDS),
        )

    def _event_log_is_complete(self) -> bool:
        """事件日志是否为完整历史。

        recent_events 每次落盘都会截断到最后 EVENT_LOG_CAP 条。因此只有当
        长度严格小于上限时，才能确定「一条都没丢」，进而据此回溯推断解锁时间。
        一旦到达上限，历史就已经不可考了，此时宁可标补录也不臆测日期。
        """
        return len(self.state.recent_events) < EVENT_LOG_CAP

    def _infer_unlock_times(self) -> dict[str, str]:
        """回溯推断勋章的确切解锁时间（仅在证据充分时给出）。

        做法是按时间顺序重放事件日志，累积记忆条数与技能数，找出各阈值
        被跨过的那一刻。为防止「日志不全导致时间偏晚」，重放结束后会与
        当前真实数值核对，对不上就整条线索作废。

        Returns:
            badge_id -> ISO 时间字符串；查不到确切时间的勋章不会出现在结果里。
        """
        if not self._event_log_is_complete():
            return {}

        events = [
            e for e in self.state.recent_events
            if isinstance(e, dict) and e.get("ts")
        ]
        events.sort(key=lambda e: str(e.get("ts", "")))

        found: dict[str, str] = {}
        mem_cum = 0
        skills: set[str] = set()
        # 显式列出可回溯的勋章及其阈值。
        # 之所以不从 BADGES 自动推导：metric 是闭包，无法内省它读的是哪个字段。
        # 宁可在这里手写一份短名单，也不做不可靠的反射。
        mem_targets = [("first_memory", 1), ("memory_50", 50),
                       ("memory_200", 200), ("memory_500", 500)]
        skill_targets = [("first_skill", 1)]

        for e in events:
            kind = e.get("kind")
            ts = str(e.get("ts"))
            if kind == "memory_learned":
                try:
                    mem_cum += int(e.get("count", 1))
                except (TypeError, ValueError):
                    mem_cum += 1
                for bid, target in mem_targets:
                    if bid not in found and mem_cum >= target:
                        found[bid] = ts
            elif kind in ("skill_created", "skill_used", "skill_evolved"):
                name = e.get("skill_name")
                if name:
                    skills.add(str(name))
                for bid, target in skill_targets:
                    if bid not in found and len(skills) >= target:
                        found[bid] = ts

        # 核对：重放结果必须与当前真实状态吻合，否则日志不足采信
        if mem_cum != self.state.memories_learned:
            for bid, _ in mem_targets:
                found.pop(bid, None)
        if len(skills) != len(self.state.skill_proficiency):
            for bid, _ in skill_targets:
                found.pop(bid, None)
        return found

    def check_badges(self, save: bool = True) -> list[BadgeDef]:
        """判定全部勋章，为新达成的记录解锁时间并落盘。

        首次运行（体系上线后的第一次判定）走迁移分支：把此前就已达成的勋章
        一次性登记入册，能回溯确切时间的用真实时间，其余标「补录」——
        并且**不**作为新解锁上报，免得一上来就弹一屏报喜。

        Args:
            save: 有变化时是否立即落盘。

        Returns:
            本次新解锁的勋章定义列表（迁移时恒为空）。
        """
        ctx = self._badge_context()
        record = self.state.badges_unlocked
        migrating = self.get_counter(_C_BADGES_MIGRATED) != 1
        inferred = self._infer_unlock_times() if migrating else {}

        newly: list[BadgeDef] = []
        changed = False

        for badge in badge_registry.BADGES:
            if badge.id in record:
                continue
            if not badge_registry.is_unlocked(badge, ctx):
                continue
            if migrating:
                record[badge.id] = inferred.get(badge.id, BACKFILLED_MARK)
            else:
                record[badge.id] = _now_iso()
                newly.append(badge)
            changed = True

        if migrating:
            self.state.counters[_C_BADGES_MIGRATED] = 1
            changed = True

        if changed and save:
            self.save()
        return newly

    def badge_items(self) -> list[dict]:
        """全部勋章的展示态列表（已解锁在前，含进度与解锁时间）。"""
        ctx = self._badge_context()
        record = self.state.badges_unlocked
        items = []
        for badge in badge_registry.BADGES:
            raw = record.get(badge.id)
            backfilled = raw == BACKFILLED_MARK
            items.append(
                badge_registry.serialize(
                    badge,
                    ctx,
                    unlocked_at=None if (raw is None or backfilled) else raw,
                    backfilled=backfilled,
                )
            )
        return badge_registry.sort_for_display(items)

    def badge_snapshot(self) -> dict:
        """勋章墙数据包：先判定落盘，再输出全量勋章 + 统计摘要。"""
        newly = self.check_badges()
        items = self.badge_items()
        return {
            "badges": items,
            "summary": badge_registry.summarize(items),
            "newlyUnlocked": [
                {
                    "id": b.id,
                    "name": b.name,
                    "glyph": b.glyph,
                    "icon": b.icon,
                    "rarity": b.rarity,
                    "rarityLabel": badge_registry.RARITY_LABELS.get(b.rarity, b.rarity),
                    "celebration": b.celebration,
                }
                for b in newly
            ],
            "companionDays": len(set(self.state.active_dates)),
            "streakDays": self._compute_streak_days(),
        }

    def _derive_achievements(self) -> list[dict]:
        """成就列表（旧状态面板沿用的字段结构）。

        现已改为读取 badges.py 声明式注册表，与勋章墙同一份数据源，
        不再各算各的。
        """
        return self.badge_items()

    def _derive_personality(self) -> dict:
        """人格指纹四维（0-100），从真实行为状态派生：

        - energy(活力型)     = 活力值 vitality
        - empathy(共情型)    = 默契度 empathy（越用越懂你）
        - logic(逻辑型)      = 技能平均成功率（越可靠越偏逻辑）
        - creativity(创意型) = 技能创建/进化事件越多越偏创意
        """
        st = self.state
        rates = [p.success_rate for p in st.skill_proficiency.values()]
        logic = int(round(sum(rates) / len(rates) * 100)) if rates else 50
        creative_events = sum(
            1 for e in st.recent_events
            if e.get("kind") in ("skill_created", "skill_evolved")
        )
        creativity = min(100, 40 + creative_events * 5 + len(st.skill_proficiency) * 4)
        return {
            "energy": int(st.vitality),
            "logic": logic,
            "creativity": int(creativity),
            "empathy": int(st.empathy),
        }

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
        # 可视化面板补充字段（全部基于真实数据派生，非展示用空数字）
        d["total_chats"] = self.state.total_chats
        d["streak_days"] = self._compute_streak_days()
        # 先判定勋章（有新解锁会落盘），再取展示列表，保证两者一致
        newly = self.check_badges()
        d["achievements"] = self.badge_items()
        d["newly_unlocked"] = [
            {
                "id": b.id,
                "name": b.name,
                "glyph": b.glyph,
                "icon": b.icon,
                "rarity": b.rarity,
                "celebration": b.celebration,
            }
            for b in newly
        ]
        d["personality"] = self._derive_personality()
        return d
