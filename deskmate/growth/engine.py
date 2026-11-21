"""GrowthEngine — 成长数值计算。

把「技能使用 / 交互反馈」转化为熟练度、等级、亲密度等数值，
并通过 modulate_* 输出真实行为调制参数（技能排序权重、proactive 阈值、记忆预算）。

等级曲线与前端 LEVEL_THRESHOLDS 保持一致（成长值累积制，非了解条数制）。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

from . import badges as badge_registry
from .badges import BadgeContext, BadgeDef
from .state import BACKFILLED_MARK, GrowthState, SkillProficiency, load_state, save_state

logger = logging.getLogger(__name__)

# 等级阈值（成长值累积）
LEVEL_THRESHOLDS = [0, 5, 15, 30, 50, 80, 120, 170, 230, 300, 400, 520]

# 关系阶段（8.2 长跑设计）：由 bond_depth 派生，是「数值封顶后」的成长第二曲线。
# 亲密度/默契度满格后，溢出的增量转入 bond_depth，深度跨过阈值即进入下一阶段。
# 阈值节奏：满格后每次 +3（如接受一条建议）全量转深度，
#   熟络 ≈ 满格后 10 次 · 默契 ≈ 再 20 次 · 知己 ≈ 再 37 次 · 灵魂伴侣 ≈ 再 67 次。
BOND_STAGES = [
    (0, "初识"),
    (30, "熟络"),
    (90, "默契"),
    (200, "知己"),
    (400, "灵魂伴侣"),
]

# 默契细分子域（8.2 深化）：整体默契满格后，子域继续深化，「懂你」更细、更可感。
# 各子域 0-100，封顶溢出同样转 bond_depth。
DOMAIN_CREATION = "creation"      # 创作：编校 / 审稿 / 圆桌 / 书稿入库
DOMAIN_MEMORY = "memory"          # 记忆：新事实被记住
DOMAIN_PROACTIVE = "proactive"    # 主动：主动建议被接受 / 忽略
DOMAIN_COMPANION = "companion"    # 陪伴：日常对话时长

# --- P0 修正（2026-08-30 三维审计）：深度轨道对老用户「归零」的补偿 ---
# 背景：8.2 新增 bond_depth / empathy_domains 时**只对增量生效**，老用户
# （如 155 轮、intimacy 97.5）的历史积累在新轨道上显示为 0 → 前端 BondPanel
# 显示「🌱 初识 · 深度 0」，与实际使用深度严重倒错。以下常量用于一次性折算
# 与「未封顶也让轨道动起来」的每轮微量沉淀。
MIGRATION_VERSION = 2       # 已完成存量折算的状态版本（幂等守卫，>= 则跳过）
DEPTH_PER_INTIMACY = 0.5    # 折算：每 1 点亲密度 → 关系深度
DEPTH_PER_EMPATHY = 0.3     # 折算：每 1 点默契度 → 关系深度

# --- 事件详情（日记叙事素材）的体积上限 ---
# recent_events 常驻内存且落盘，详情只供日记「讲得出具体事」，不是仓库。
# 超了就截——宁可少一句细节，也不能让 state 文件无限膨胀。
_MAX_DETAIL_CHARS = 120     # 单条详情最长字符
_MAX_DETAIL_ITEMS = 4       # 单事件最多带几条


def _clip(text: Optional[str]) -> str:
    """详情截断：去空白、限长、空值归零。存进事件前一律过一遍。"""
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s[:_MAX_DETAIL_CHARS]


def _clip_list(items: Optional[list]) -> list[str]:
    """详情列表截断：逐条 _clip，去重，限量。"""
    if not items:
        return []
    out: list[str] = []
    for it in items:
        c = _clip(it)
        if c and c not in out:
            out.append(c)
        if len(out) >= _MAX_DETAIL_ITEMS:
            break
    return out
DEPTH_PER_CHAT = 0.1        # 折算：每 1 轮对话 → 关系深度（对话数按 500 封顶）
DEPTH_PER_ROUND = 0.05      # 每轮微量沉淀：未封顶时深度轨道也要「看得见地动」
COMPANION_PER_CHAT = 0.3    # 陪伴子域每轮增量（与 record_chat 原逻辑一致）
EMPATHY_DOMAIN_LABELS = {
    DOMAIN_CREATION: "创作",
    DOMAIN_MEMORY: "记忆",
    DOMAIN_PROACTIVE: "主动",
    DOMAIN_COMPANION: "陪伴",
}

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
TRUST_APPROVED = 0.02     # 创作定稿/通过：认可 AI 产出
TRUST_REWORK = -0.03      # 创作打回重做：AI 没一次做对
TRUST_CORRECTED = -0.02   # 用户明确纠正：AI 理解有偏差
TRUST_CLAMP = (0.0, 1.0)

EMPATHY_ACCEPTED = 2.0
EMPATHY_IGNORED = -1.0
EMPATHY_APPROVED = 2.0
EMPATHY_REWORK = -2.0
EMPATHY_CORRECTED = -1.5

# 2026-09-05（E1）：MOOD_ACCEPTED / MOOD_IGNORED 已随 mood 语义变更一并删除。
# 它们曾在这里定义、却只被「忽略/打回/纠正」三处直接增减 mood——而 mood 本身
# 没有任何可信来源（真实库里恒为 80.0），等于用行为猜情绪、还猜了个常量出来。
# 现在 mood 由当日情绪结算派生（见 set_mood_from_emotion），业务代码不再碰它。

XP_CREATION_APPROVED = 4

# 记忆预算调制：默契度 0-100 → 倍率 [0.9, 1.3]
EMPATHY_BUDGET_MIN = 0.9
EMPATHY_BUDGET_MAX = 1.3

# 活力（照顾循环）：被使用消耗、随时间缓慢恢复。
# 旧参数（消耗 1.0 / 每 tick 恢复 0.2）下，后台心跳每 ~5 分钟恢复一次、远快于
# 偶发的消耗，导致活力几乎恒定顶在 100，看起来像「写死的常量」。新参数让消耗更
# 明显、恢复更克制，活力才会随真实使用上下浮动。
VITALITY_DECAY_PER_USE = 2.0       # 每次被使用（技能/建议/记忆）消耗
VITALITY_RECOVER_PER_TICK = 0.1    # 每次 proactive tick（约 5 分钟）恢复

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

        # P0-2：一次性把历史存量折算进 8.2 的深度轨道（幂等，version 守卫）。
        # fail-open：折算失败绝不影响成长引擎本身的可用性。
        try:
            self.migrate_depth_once()
        except Exception:
            logger.exception("bond_depth 存量折算失败（已跳过，不影响成长引擎）")

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self) -> None:
        save_state(self.state, self.base_dir)

    def migrate_depth_once(self) -> bool:
        """P0-2：一次性把「存量」折算进 8.2 的深度轨道（**幂等**，version 守卫）。

        背景：8.2 新增 ``bond_depth`` / ``empathy_domains`` 时**只对增量生效**。
        老用户（如 155 轮、intimacy 97.5）的历史积累在新轨道上恒为 0，前端
        BondPanel 因此显示「🌱 初识 · 深度 0」，与实际使用深度严重倒错。

        折算口径（**补录，不是虚构**）：严格沿用各 ``record_*`` 既定的增量规则
        反推，只用真实累计量，绝不凭空造数——
        - ``companion`` ← ``total_chats × COMPANION_PER_CHAT``（record_chat 既定每轮 +0.3）
        - ``memory``    ← ``memories_learned × INTIMACY_MEMORY_LEARNED``（既定每条 +1.0）
        - ``creation``  ← 圆桌 / 书稿等 counters 累计 × 1.0（既定每次 +1.0）
        - ``bond_depth``← 亲密度 / 默契度 / 对话轮数加权（深度 = 长期关系的沉淀）

        Returns:
            是否执行了折算（False = 已是新版本，或纯新用户无存量可补）。
        """
        if self.state.version >= MIGRATION_VERSION:
            return False

        # 纯新用户：无任何存量可补，直接置位，避免无谓折算
        has_history = (
            self.state.intimacy > 0
            or self.state.empathy > 0
            or self.state.total_chats > 0
            or self.state.memories_learned > 0
        )
        if not has_history:
            self.state.version = MIGRATION_VERSION
            self.save()
            return False

        # 1) 子域补录（按既定规则反推，clamp 到 0-100）
        # 只在**确有存量**时写入——避免给零存量场景造出 0.0 的空键
        #（空键会让「在哪方面更懂你」出现无意义的零值条目）。
        if self.state.total_chats > 0 and not self.state.empathy_domains.get(DOMAIN_COMPANION):
            self.state.empathy_domains[DOMAIN_COMPANION] = min(
                100.0, self.state.total_chats * COMPANION_PER_CHAT
            )
        if self.state.memories_learned > 0 and not self.state.empathy_domains.get(DOMAIN_MEMORY):
            self.state.empathy_domains[DOMAIN_MEMORY] = min(
                100.0, self.state.memories_learned * INTIMACY_MEMORY_LEARNED
            )
        creation_events = self.state.counters.get(
            badge_registry.COUNTER_ROUNDTABLES, 0
        ) + self.state.counters.get(badge_registry.COUNTER_MANUSCRIPTS, 0)
        if creation_events and not self.state.empathy_domains.get(DOMAIN_CREATION):
            self.state.empathy_domains[DOMAIN_CREATION] = min(
                100.0, float(creation_events)
            )

        # 2) 深度折算：亲密度 / 默契度 / 对话轮数加权（对话数封顶 500，防刷）
        if self.state.bond_depth <= 0:
            self.state.bond_depth = round(
                self.state.intimacy * DEPTH_PER_INTIMACY
                + self.state.empathy * DEPTH_PER_EMPATHY
                + min(self.state.total_chats, 500) * DEPTH_PER_CHAT,
                2,
            )
            self._add_event(
                "depth_backfilled",
                depth=self.state.bond_depth,
                note="历史积累折算进关系深度（一次性补录，非新增互动）",
            )

        self.state.version = MIGRATION_VERSION
        self.save()
        return True

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

        trust_controls_proactive=False 时**只关掉 trust 这一项偏移**，
        意志层（认知缺口 / 高置信立场）的调制仍然生效。

        信任感 >0.5 → 阈值下调（更主动）；<0.5 → 阈值上调（更克制）。

        意志层增量（W2b-1）：认知把握度叠加。
        - 存在 open gap（盲区）→ 阈值上调，知道自己不知道就先别瞎主动
        - 高置信立场（≥0.7 active）≥3 条 → 阈值下调，更敢主动
        阈值来自 will_settings，可在设置区调。空画像 → 只看 trust（现状）。
        """
        # ⚠ 2026-09-22：门控只该关掉 **trust 那一项**偏移，不能短路整个函数。
        # 原写法是 `if not ...: return base_threshold` —— 会把下面的意志层调制
        # 一起跳过，于是「关掉信任调制」实际变成「关掉全部阈值调制」，
        # 与该开关的字面语义（信任感是否调制阈值）不符。
        if self.state.trust_controls_proactive:
            offset = (self.state.trust - 0.5) * (2 * TRUST_THRESHOLD_SPAN)
        else:
            offset = 0.0
        result = base_threshold - offset
        try:
            from deskmate.will.digest import get_digest
            from deskmate.will.settings import load_will_settings

            bd = str(self.base_dir) if self.base_dir else None
            settings = load_will_settings(base_dir=bd)
            if settings.enabled:
                d = get_digest(base_dir=bd)
                if d.available:
                    if d.open_gap_count > 0:
                        result += settings.thresholds["cognitive_threshold_delta_neg"]
                    if d.stance_count >= 3:
                        result -= settings.thresholds["cognitive_threshold_delta_pos"]
        except Exception:
            pass  # fail-open
        return clamp(result, 0.0, 1.0)

    def modulate_memory_budget(self, base_budget: int) -> int:
        """记忆注入预算调制：默契度越高 → 注入越多（记忆匹配更准，AI 更懂你）。

        意志层增量（W2b-2）：认知有价值观权衡信号 → 预算 ×ratio，
        给模型多一点"决断材料"。总数仍受 MEMORY_CONTEXT_MAX_CHARS 限制，
        不挤占其它层。阈值来自 will_settings。空画像 → 只看 empathy（现状）。
        """
        ratio = EMPATHY_BUDGET_MIN + (self.state.empathy / 100.0) * (EMPATHY_BUDGET_MAX - EMPATHY_BUDGET_MIN)
        result = int(base_budget * ratio)
        try:
            from deskmate.will.digest import get_digest
            from deskmate.will.settings import load_will_settings

            bd = str(self.base_dir) if self.base_dir else None
            settings = load_will_settings(base_dir=bd)
            if settings.enabled:
                d = get_digest(base_dir=bd)
                if d.available and d.has_value_signal:
                    result = int(result * settings.thresholds["memory_budget_ratio"])
        except Exception:
            pass  # fail-open
        return result

    def modulate_proactive_frequency(self, base_interval_sec: float) -> float:
        """proactive 心跳间隔调制：活力低 → 间隔拉长（累了不打扰）。

        意志层增量（W2a）：今日情绪为压力/低落/忙碌 → 进一步拉长间隔
        （"我赶稿时她不烦我"）。阈值来自 will_settings，可在设置区调。
        空画像 / kill switch / 异常 → 完全保持现状。
        """
        vitality = self.state.vitality
        # 活力 100 → 间隔 *0.85；活力 0 → 间隔 *1.6
        ratio = 1.6 - (vitality / 100.0) * 0.75
        result = base_interval_sec * ratio
        try:
            from deskmate.will.digest import get_digest
            from deskmate.will.settings import load_will_settings

            bd = str(self.base_dir) if self.base_dir else None
            settings = load_will_settings(base_dir=bd)
            if settings.enabled:
                d = get_digest(base_dir=bd)
                if d.available and d.recent_emotion in ("压力大", "低落", "忙碌"):
                    result *= settings.thresholds["emotional_proactive_frequency_max"]
        except Exception:
            pass  # fail-open：不影响原逻辑
        return result

    def modulate_tone_prompt(self) -> str:
        """关系真实状态（注入主聊天上下文；只给事实，不给表达方式）。

        2026-11-20 按萧哥拍板「只留真实状态」重写：亲密度数值与关系阶段名
        是事实，will 的「他表达过：…」是用户亲口立场的转述；**不出现任何
        "语气应该怎样"的演出指令**——表达类模板/映射表是项目红线
        （真实优先 v2：系统只给真实状态，不给表达方式）。

        亲密度 < 10 → 空串；空画像 / will kill switch / 异常 → 不追加，fail-open。
        """
        intimacy = self.state.intimacy
        if intimacy < 10:
            return ""
        _, stage_name = self.bond_stage_from_depth(self.state.bond_depth)
        base = f"关系状态：亲密度 {int(intimacy)}（{stage_name}）。"

        # 意志层增量：高置信立场 → 追加用户表达过的偏好
        try:
            from deskmate.will.digest import get_digest
            from deskmate.will.settings import load_will_settings

            bd = str(self.base_dir) if self.base_dir else None
            settings = load_will_settings(base_dir=bd)
            if settings.enabled:
                d = get_digest(base_dir=bd)
                if d.available and len(d.top_stances) >= 2:
                    snippet = "；".join(c for _, c, _ in d.top_stances[:2])
                    max_len = int(settings.thresholds["tone_prompt_max_len"])
                    if len(snippet) > max_len:
                        snippet = snippet[:max_len] + "…"
                    if snippet:
                        base = base + f" 他表达过：{snippet}。"
        except Exception:
            pass  # fail-open
        return base

    # ------------------------------------------------------------------
    # 关系深度 / 阶段（8.2 长跑设计：数值封顶后仍可进化）
    # ------------------------------------------------------------------

    @staticmethod
    def bond_stage_from_depth(depth: float) -> tuple[int, str]:
        """由关系深度派生 (阶段序号, 阶段名)。depth<0 视为 0。"""
        d = max(0.0, depth)
        idx, name = 0, BOND_STAGES[0][1]
        for i, (th, nm) in enumerate(BOND_STAGES):
            if d >= th:
                idx, name = i, nm
            else:
                break
        return idx, name

    def _gain_relation(self, key: str, amount: float, lo: float = 0.0, hi: float = 100.0) -> float:
        """增长关系数值；到上限后的溢出转入 bond_depth（封顶不浪费）。

        返回值 = 实际生效的增长量（可为负）。
        """
        before = float(getattr(self.state, key))
        new = clamp(before + amount, lo, hi)
        setattr(self.state, key, new)
        actual = new - before
        if amount > 0 and actual < amount:
            self._deposit_depth(amount - actual)
        return actual

    def _gain_domain(self, domain: str, amount: float) -> float:
        """增长默契细分子域；到上限后的溢出同样转入 bond_depth。"""
        before = float(self.state.empathy_domains.get(domain, 0.0))
        new = clamp(before + amount, 0.0, 100.0)
        self.state.empathy_domains[domain] = new
        actual = new - before
        if amount > 0 and actual < amount:
            self._deposit_depth(amount - actual)
        return actual

    def _deposit_depth(self, overflow: float) -> None:
        """把关系数值的封顶溢出存入 bond_depth，并检查阶段是否升级。"""
        if overflow <= 0:
            return
        before_depth = self.state.bond_depth
        self.state.bond_depth = round(self.state.bond_depth + overflow, 2)
        if self.bond_stage_from_depth(self.state.bond_depth)[0] > self.bond_stage_from_depth(before_depth)[0]:
            _, name = self.bond_stage_from_depth(self.state.bond_depth)
            self._add_event(
                "bond_stage_up",
                stage=name,
                depth=round(self.state.bond_depth, 1),
            )

    def current_empathy_domains(self) -> dict:
        """默契细分子域概览（供前端展示「在哪方面更懂你」）。"""
        domains = dict(self.state.empathy_domains)
        top = max(domains.items(), key=lambda kv: kv[1], default=("", 0.0))
        return {
            "domains": {k: round(v, 1) for k, v in domains.items()},
            "top": EMPATHY_DOMAIN_LABELS.get(top[0], "") if top[0] else "",
            "top_value": round(top[1], 1) if top[0] else 0.0,
        }

    def current_bond(self) -> dict:
        """当前关系阶段与进度（供前端展示 / snapshot）。"""
        idx, name = self.bond_stage_from_depth(self.state.bond_depth)
        next_th = BOND_STAGES[idx + 1][0] if idx + 1 < len(BOND_STAGES) else None
        if next_th is None:
            progress = 1.0
        else:
            cur_th = BOND_STAGES[idx][0]
            progress = clamp((self.state.bond_depth - cur_th) / max(1, next_th - cur_th), 0, 1)
        return {
            "depth": round(self.state.bond_depth, 1),
            "stage": name,
            "stage_index": idx,
            "next_threshold": next_th,
            "progress": round(progress, 3),
            "maxed": next_th is None,
        }

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
        self._gain_relation("intimacy", INTIMACY_SKILL_USED)
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
        self._gain_relation("intimacy", INTIMACY_SKILL_CREATED)
        self._bump(badge_registry.COUNTER_SKILL_CREATED)
        self._add_event("skill_created", skill_name=skill_name)
        self._recompute_level()
        self.save()

    def record_skill_evolved(
        self,
        skill_name: str,
        success: bool = True,
        skill_store_uses: Optional[int] = None,
        skill_store_rate: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """技能进化（成功执行 / 用户纠正后优化）：熟练度额外增长。

        Args:
            reason: 这次进化**因为什么**（一句话，来自调用方手里的真实上下文）。
                只记技能名的话，日记只能写「XX 又精进了」这种台账——技能日志里
                已有完整记录，日记重复它毫无价值。带上原因，日记才讲得出
                「因为你三次纠正洪武年份，这条考据技能长进了」。
                超长会自动截断（见 ``_clip``），绝不撑爆 recent_events。
        """
        prof = self.state.skill_proficiency.get(skill_name, SkillProficiency())
        prof.uses = (skill_store_uses if skill_store_uses is not None else prof.uses + 1)
        if skill_store_rate is not None:
            prof.success_rate = skill_store_rate
        prof.last_used = self._now_date()
        prof.proficiency = compute_proficiency(prof.uses, prof.success_rate, prof.last_used)
        self.state.skill_proficiency[skill_name] = prof

        self.state.growth_value += XP_SKILL_EVOLVED
        self._gain_relation("intimacy", INTIMACY_SKILL_USED)
        self._bump(badge_registry.COUNTER_SKILL_EVOLVED)
        self._add_event("skill_evolved", skill_name=skill_name, reason=_clip(reason))
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
            self._gain_relation("trust", TRUST_ACCEPTED, 0.0, 1.0)
            self._gain_relation("empathy", EMPATHY_ACCEPTED)
            self._gain_relation("intimacy", INTIMACY_SUGGESTION_ACCEPTED)
            self._gain_domain(DOMAIN_PROACTIVE, EMPATHY_ACCEPTED)
            self.state.growth_value += XP_SUGGESTION_ACCEPTED
            self._add_event("suggestion_accepted")
        else:
            self._gain_relation("trust", TRUST_IGNORED, 0.0, 1.0)
            self._gain_relation("empathy", EMPATHY_IGNORED)
            self._gain_relation("intimacy", INTIMACY_SUGGESTION_IGNORED)
            self._gain_domain(DOMAIN_PROACTIVE, EMPATHY_IGNORED)
            self._add_event("suggestion_ignored")
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 创作反馈（P0-3：定稿/打回 → 信任/默契）
    # ------------------------------------------------------------------

    def record_creation_feedback(self, approved: bool) -> None:
        """用户对创作产出的拍板反馈：定稿(True) → 信任↑ 默契↑；打回(False) → 信任↓ 默契↓。

        打回重做 = 「AI 没一次做对」的负反馈（trust 降幅大于忽略建议）；
        定稿/通过 = 认可产出（正反馈）。同时计入「创作」默契子域（8.2）。
        """
        if approved:
            self._gain_relation("trust", TRUST_APPROVED, 0.0, 1.0)
            self._gain_relation("empathy", EMPATHY_APPROVED)
            self._gain_domain(DOMAIN_CREATION, EMPATHY_APPROVED)
            self.state.growth_value += XP_CREATION_APPROVED
            self._add_event("creation_approved", note="创作定稿/通过")
            self._log_behavior("creation_approved")
        else:
            self._gain_relation("trust", TRUST_REWORK, 0.0, 1.0)
            self._gain_relation("empathy", EMPATHY_REWORK)
            self._gain_domain(DOMAIN_CREATION, EMPATHY_REWORK)
            self._add_event("creation_rework", note="创作被打回重做")
            self._log_behavior("creation_rejected")
        self._apply_decay()
        self._recompute_level()
        self.save()

    def record_correction(self, note: Optional[str] = None) -> None:
        """用户明确纠正（「不对/别这样/记错了」）：信任微降 + 默契微调。

        区别于「忽略建议」——这是主动纠错信号，AI 应校准理解而非只是降低好感。

        Args:
            note: 用户纠正的**原话**（调用方手里有 last_user）。不传则沿用
                硬编码的「用户明确纠正」，日记就只能写出一句没有内容的话。
        """
        # ⚠ 同日去抖（2026-09-22）：纠正检测是 after_action 每轮跑的启发式，
        # 一次长会话里同一件事可能连续多轮命中信号词 → 信任感被反复扣减
        # （每次 -0.02，25 次即归零，而正向回升要用户显式点「接受/定稿」）。
        # 信任受损是「今天发生了一件事」级别的事件，一天记一次就够；
        # 默契/子域/事件计数**不**去抖——每次纠正都是一条独立的校准信息。
        day = self._now_date()
        already_corrected_today = int(
            (self.state.daily_events.get(day) or {}).get("user_corrected", 0)
        )
        if already_corrected_today == 0:
            self._gain_relation("trust", TRUST_CORRECTED, 0.0, 1.0)
        self._gain_relation("empathy", EMPATHY_CORRECTED)
        # P0-4：纠正 = 理解校准信号，必须计入「记忆」默契子域。
        # 此前只在总数上惩罚（trust 降），用户感受不到「它因我的纠正而
        # 改进」——只有惩罚、没有成长。取绝对值：纠正让 AI 多拿到一条校准信息，
        # 对「懂你」是增量；trust 的下降已表达「这次没做对」。
        self._gain_domain(DOMAIN_MEMORY, abs(EMPATHY_CORRECTED))
        # 纠正原话照实带出去——日记要写的不是「他纠正了我一次」，
        # 而是「他说：不是洪武三年，是洪武十五年」。没原话就只能记账。
        self._add_event(
            "user_corrected",
            note=_clip(note) or "用户明确纠正",
        )
        self._log_behavior("correction")
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 记忆学习（了解度 → 默契度微增）
    # ------------------------------------------------------------------

    def record_memory_learned(
        self, count: int = 1, texts: Optional[list] = None
    ) -> None:
        """每次记住新事实，默契度微增（记忆匹配变准）。

        Args:
            texts: 这次**记住了什么**（事实原文列表）。此前只传 count，
                日记便只能写「又记住了你 4 件小事」——数字谁都会记，
                得把内容带上，日记才写得出「记住了你在写《国子监》第三章」。
                最多留 _MAX_DETAIL_ITEMS 条、每条截断，防止撑爆 recent_events。
        """
        if count <= 0:
            return
        detail = _clip_list(texts)
        self._gain_relation("empathy", INTIMACY_MEMORY_LEARNED * count)
        self._gain_relation("intimacy", INTIMACY_MEMORY_LEARNED * count)
        self.state.growth_value += XP_MEMORY_LEARNED * count
        self.state.memories_learned += count
        self._gain_domain(DOMAIN_MEMORY, INTIMACY_MEMORY_LEARNED * count)
        self._add_event("memory_learned", count=count, texts=detail)
        self._apply_decay()
        self._recompute_level()
        self.save()

    # ------------------------------------------------------------------
    # 活跃度统计（对话计数 / 连续天数）
    # ------------------------------------------------------------------

    def record_chat(self, domain: str = DOMAIN_COMPANION) -> None:
        """记录一次对话（累计总对话数 + 今日活跃日期，供连续天数统计）。

        由对话入口（run_turn 的 turn_start）调用，是可视化面板
        streakDays / totalChats 的真实数据源。

        Args:
            domain: 本轮归属的默契子域。创作流水线传 ``DOMAIN_CREATION``，
                桌搭 / 通用助手传 ``DOMAIN_COMPANION``（默认）。
                P0-1：此前只有 deskmate 模式会计数，导致创作场景（萧哥主场景）
                的「被懂得」完全不进成长数值，故放开调用方并支持子域归属。

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

        # 陪伴 / 创作子域：日常对话与创作行为分别沉淀到对应默契子域（每轮 +0.3，克制）
        self._gain_domain(domain, COMPANION_PER_CHAT)

        # P0-3：每轮微量沉淀进关系深度。
        # 原设计只有「关系数值被上限截断的溢出」才转 bond_depth，导致封顶前的
        # 漫长区间里深度轨道恒定不动（用户长期看到「深度 0」）。这里让每轮对话
        # 也沉淀极小一点，保证轨道在未封顶时同样「看得见地动」。
        self._deposit_depth(DEPTH_PER_ROUND)

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
        self._gain_domain(DOMAIN_CREATION, 1.0)
        self.save()

    def record_roundtable(self) -> None:
        """记录一次创作圆桌召集。"""
        self._bump(badge_registry.COUNTER_ROUNDTABLES)
        self._gain_domain(DOMAIN_CREATION, 1.0)
        self._add_event("roundtable_held")
        self.save()

    def record_report(self) -> None:
        """记录一次审阅报告生成。"""
        self._bump(badge_registry.COUNTER_REPORTS)
        self._gain_domain(DOMAIN_CREATION, 1.0)
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
        self._gain_domain(DOMAIN_CREATION, 1.0)
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
        # 创造性读持久计数器，不读 recent_events —— 后者是 50 条易失窗口，
        # 老用户的早期技能事件会被挤出窗口，人格维度会莫名其妙倒退。
        # 计数器尚未累积到的存量用户退回窗口统计，避免升级瞬间归零。
        creative_events = int(st.counters.get(badge_registry.COUNTER_SKILL_CREATED, 0)) + int(
            st.counters.get(badge_registry.COUNTER_SKILL_EVOLVED, 0)
        )
        if creative_events <= 0:
            creative_events = sum(
                1 for e in st.recent_events
                if e.get("kind") in ("skill_created", "skill_evolved")
            )
        # 累计量用平方根压缩：前期涨得动、后期收敛，不会早早封顶失去区分度
        creativity = min(
            100,
            40 + int(round(4 * math.sqrt(creative_events))) + len(st.skill_proficiency) * 4,
        )
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
        """活力消耗：每次被使用（技能/建议），活力明显下降。"""
        self.state.vitality = clamp(self.state.vitality - VITALITY_DECAY_PER_USE, 0, 100)

    def tick_decay(self) -> None:
        """时间衰减（proactive tick 可调用）：活力缓慢恢复。

        2026-09-05（E1）：这里原本还有「心情向 80 收敛」的 ±0.1 微调——它是
        mood 唯一的写入者，也就是那个假数值的全部来源：不管今天顺不顺，
        mood 都只会一格一格爬回 80。删除后 mood 只由当日情绪结算写入。
        """
        self.state.vitality = clamp(self.state.vitality + VITALITY_RECOVER_PER_TICK, 0, 100)
        self.save()

    def set_mood_from_emotion(self, value: float, source: str, day: str) -> bool:
        """由当日情绪结算写入 mood——**mood 唯一的写入口**（E1）。

        业务代码（采纳/忽略/打回/纠正）一律不许再直接增减 mood：那是用行为
        猜情绪，而猜出来的东西没有任何可信来源。mood 现在是情绪的**结果**。

        Args:
            value: 派生出的心情值 0-100。
            source: "stated"（他自己写的日记）/ "inferred"（行为事件推断）。
            day: 结算日 YYYY-MM-DD。早于已结算日的回填会被拒绝，
                免得补记上周日记时把今天刚算出来的结果盖掉。

        Returns:
            是否真的写入。
        """
        if not day or not source:
            return False
        if self.state.mood_date and day < self.state.mood_date:
            return False
        self.state.mood = clamp(round(float(value), 1), 0, 100)
        self.state.mood_source = str(source)
        self.state.mood_date = str(day)
        self.save()
        return True

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
        # 同步记一份按天的持久账。recent_events 落盘时只留最后 50 条，会滚走；
        # 情感时间线要按天回看那天发生了什么，必须靠这份账而不是那个窗口。
        day = datetime.datetime.now().astimezone().date().isoformat()
        counts = self.state.daily_events.setdefault(day, {})
        counts[kind] = int(counts.get(kind, 0)) + 1

    def _log_behavior(self, kind: str) -> None:
        """P2-3：把成长/创作反馈当作行为事件记录，供长期模式学习推断偏好。

        fail-open：任何异常静默，绝不阻断成长数值主流程。
        """
        try:
            from deskmate.proactivity.behavior_patterns import log_event

            log_event(kind, base_dir=self.base_dir)
        except Exception:
            pass

    def snapshot(self) -> dict:
        """完整状态快照（供前端渲染）。"""
        d = self.state.to_dict()
        d["level_progress"] = self.current_level_progress()
        d["bond"] = self.current_bond()
        d["empathy_domains"] = self.current_empathy_domains()
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
