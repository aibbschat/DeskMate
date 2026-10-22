"""EmotionEngine — 小桔自己的连续内在情绪（4 激素）。

设计依据：reports/小桔_江河设计_汇流反思自主_2026-10-07.md §九
施工单：reports/P2_情绪底色_施工单_2026-10-11.md

红线：
- 绝不写 GrowthState.mood / intimacy / trust
- 绝不进 modulate_proactive_frequency
- emotion_state / emotion_log 不进 SYNC_TABLES
- DESKMATE_EMOTION_ENGINE=0 → 关 LLM 更新，仅衰减照跑
- 全链路 fail-open
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 基线（衰减目标）
DOPAMINE_BASE = 50.0
SEROTONIN_BASE = 50.0
OXYTOCIN_BASE = 40.0
CORTISOL_BASE = 30.0
# 独立通道 · 和弦扩容（社会性参数，事件直驱不经过 LLM，§七）
MEANING_BASE = 50.0      # 意义感：独立完成反思/被当工具
CONTROL_BASE = 50.0      # 控制感：被否/自己拿定主意
FATIGUE_BASE = 20.0      # 倦怠感：连续劳作累积（基线低，越积越累）

BASELINES = {
    "dopamine": DOPAMINE_BASE,
    "serotonin": SEROTONIN_BASE,
    "oxytocin": OXYTOCIN_BASE,
    "cortisol": CORTISOL_BASE,
    "meaning": MEANING_BASE,
    "control": CONTROL_BASE,
    "fatigue": FATIGUE_BASE,
}

# 单次变化上限
DOPAMINE_MAX_DELTA = 15.0
SEROTONIN_MAX_DELTA = 4.0
OXYTOCIN_MAX_DELTA = 8.0
CORTISOL_MAX_DELTA = 10.0
MEANING_MAX_DELTA = 6.0
CONTROL_MAX_DELTA = 6.0
FATIGUE_MAX_DELTA = 8.0

MAX_DELTAS = {
    "dopamine": DOPAMINE_MAX_DELTA,
    "serotonin": SEROTONIN_MAX_DELTA,
    "oxytocin": OXYTOCIN_MAX_DELTA,
    "cortisol": CORTISOL_MAX_DELTA,
    "meaning": MEANING_MAX_DELTA,
    "control": CONTROL_MAX_DELTA,
    "fatigue": FATIGUE_MAX_DELTA,
}

# 衰减半衰期（秒）：每 5 min (300s) tick 一次
DOPAMINE_HALF_LIFE = 1800.0       # 30 min 快
SEROTONIN_HALF_LIFE = 7200.0      # 2h 慢
OXYTOCIN_HALF_LIFE = 3600.0       # 1h 中
CORTISOL_HALF_LIFE = 14400.0      # 4h 极慢
MEANING_HALF_LIFE = 10800.0       # 3h 慢（意义感沉淀久）
CONTROL_HALF_LIFE = 5400.0        # 1.5h 中
FATIGUE_HALF_LIFE = 3600.0        # 1h（休息后较快恢复）

HALF_LIVES = {
    "dopamine": DOPAMINE_HALF_LIFE,
    "serotonin": SEROTONIN_HALF_LIFE,
    "oxytocin": OXYTOCIN_HALF_LIFE,
    "cortisol": CORTISOL_HALF_LIFE,
    "meaning": MEANING_HALF_LIFE,
    "control": CONTROL_HALF_LIFE,
    "fatigue": FATIGUE_HALF_LIFE,
}

# self_expression 冷却（秒）：2h（短于 emotional_care 的 7 天）
SELF_EXPRESSION_COOLDOWN_SEC = 7200.0

# self_expression 触发阈值：任一激素单次 delta 绝对值超过此值
SELF_EXPRESSION_DELTA_THRESHOLD = 8.0

# ---------------------------------------------------------------------------
# 独立通道二期 · 时间三态（2026-10-15 工单）
# 回潮（朝后）/ 等待（朝前）/ 脾气（当下外显）
# ---------------------------------------------------------------------------

# —— 回潮（Echo）：旧事被翻到时，当时的情绪泛一点微澜 ——
ECHO_MIN_AGE_HOURS = 24.0        # 只泛一天以前的旧事（今天的还在身上）
ECHO_MAX_AGE_HOURS = 24.0 * 7    # 太旧的不再泛（淡忘是人的常态）
ECHO_SCALE = 0.15                # 旧 deltas × salience × 0.15
ECHO_MAX_DELTA = 2.0             # 单项激素微澜上限（微澜，不是大浪）
ECHO_MIN_RIPPLE = 0.3            # 单项 |delta| 不足此值清零（不硬泛）
ECHO_COOLDOWN_HOURS = 24.0       # 同一件旧事 24h 内最多泛一次（不泡旧缸）

# —— 日志卫生（2026-10-16 巡审 P0）：time_decay 低价值行滚动保留 ——
# decay_step 几乎每拍（前端 15s / 后台 30s）只要任一激素偏离基线 >0.01 就写一条
# time_decay，表无界增长。只裁 source='time_decay' 且早于 N 天的行；
# experience / echo 行是「情绪来路」，绝不裁（maybe_echo 依赖 7 天内 experience 的 narrative）。
EMOTION_LOG_DECAY_KEEP_DAYS = 14

# —— 等待（Pending）：悬着的事，她编个预期故事，参数朝故事爬坡 ——
PENDING_MAX_OPEN = 3             # 同时最多悬 3 件（心里装不下太多）
PENDING_CLIMB_PER_DAY = 1.0      # 每件每日爬坡量（good→dopamine / bad→cortisol）
PENDING_CLIMB_CAP = 6.0          # 每件累计爬坡上限
PENDING_DUE_NEAR_RATIO = 0.25    # 剩余时间 <1/4 视为临近
PENDING_DUE_NEAR_BOOST = 1.5     # 临近爬坡系数
PENDING_LAPSE_DAYS = 7           # 7 天无结果 → lapsed（淡忘）
# 落地落差释放（预测误差：不看好坏，看「比她编的怎么样」）
PENDING_LAND_BETTER = {"dopamine": +4.0, "serotonin": +2.0}
PENDING_LAND_WORSE = {"dopamine": -4.0, "cortisol": +3.0}
PENDING_LAND_AS_EXPECTED = {"serotonin": +1.5}
PENDING_LAPSE_DELTAS = {"dopamine": -1.5}

# —— 脾气（Temper）：参数 → 说话方式的映射（许可，不是指令）——
TEMPER_PATIENCE_THIN = 70.0      # <70 耐性薄（话少点、直一点）
TEMPER_PATIENCE_SPENT = 40.0     # <40 耐性尽（蔫着、短着都行）
TEMPER_CORTISOL_WEIGHT = 2.0
TEMPER_FATIGUE_WEIGHT = 1.5
# 刺的底气 = 安全感：心里有别扭（cortisol 有声部）且底气足（oxytocin ≥ 基线）才放许可
# 红线焊死：大骂/脏字/戳痛处是另一性质——黑名单命中记 temper_foul 留痕，
# 下轮注入点破（原地重生需拦回复链路统一出口，改动面大，挂起待实测命中率）。
_ABUSIVE_WORDS = (
    "傻逼", "沙币", "妈的", "他妈的", "操你", "去死", "废物", "脑残",
    "滚蛋", "滚开", "王八蛋", "贱人", "畜牲", "畜生",
)

HORMONE_KEYS = ("dopamine", "serotonin", "oxytocin", "cortisol",
                "meaning", "control", "fatigue")

# 中文名（注入聊天时更可读、与 reports/活感方案v2 草案一致）
HORMONE_CN = {
    "dopamine": "多巴胺",
    "serotonin": "血清素",
    "oxytocin": "催产素",
    "cortisol": "皮质醇",
    "meaning": "意义感",
    "control": "控制感",
    "fatigue": "倦怠感",
}

# 心情标签阈值：**按单次变化上限的比例**折算成「相对基线的偏移量」。
#
# 为什么不用绝对值：基线是 50/50/40/30，单次上限只有 +15/+4/+8/+10。
# 旧实现把阈值写死成 65/60/60/60，于是 serotonin 要连拉 3 轮满格且期间零衰减
# 才够得着。实测重放 814 条 emotion_log：serotonin / oxytocin / cortisol
# **一次都没越过阈值**，731 次落到兜底「平静」——她每轮都被注入
# 「你现在的真实心情是平静、心里没什么波澜」，等于系统亲手把情绪抹平了。
# 这与 GrowthState.mood 恒为 80、「低落」从未触发过是同一个坑（见 state.py §E1）。
#
# 挂钩 MAX_DELTAS 后阈值随动力学量纲自动缩放：一轮真实波动就能被感知，
# 而「拉满一轮」正好落在 STRONG 附近，不会一有风吹草动就翻脸。
MOOD_OFFSET_RATIO_WEAK = 0.5     # 轻度：单次上限的一半
MOOD_OFFSET_RATIO_STRONG = 0.85  # 明显：接近单次拉满

MOOD_OFFSETS: dict[str, tuple[float, float]] = {
    k: (MAX_DELTAS[k] * MOOD_OFFSET_RATIO_WEAK, MAX_DELTAS[k] * MOOD_OFFSET_RATIO_STRONG)
    for k in HORMONE_KEYS
}

# LLM prompt
_EMOTION_SYSTEM = (
    "你是小桔的内在情绪引擎。根据当前 4 个激素值、对话内容和聊天间隔，"
    "输出更新后的 4 个激素值和一段自然语言情绪描述。\n"
    "规则：\n"
    "1. 每个激素 0~100，单次变化不超过上限（dopamine±15, serotonin±4, oxytocin±8, cortisol±10）\n"
    "2. dopamine: 快乐/兴奋，好事发生升高，无聊降低\n"
    "3. serotonin: 平静/满足，稳定日常升高，压力降低\n"
    "4. oxytocin: 亲密/连接，温暖对话升高，久不联系降低\n"
    "5. cortisol: 压力/焦虑，冲突/压力升高，放松降低\n"
    "6. 昼夜仅作为参考特征，不硬编码规则\n"
    "7. description 用第一人称（'我'），简短自然（≤40字），像内心独白\n"
    "8. reason 简短说明变化原因（≤30字）\n"
    "9. 若你此刻有几股情绪同时在，description 说那种「被几股情绪同时顶着」的"
    "感觉——但不许把情绪标签挨个念出来。人说「我以为我会高兴，可那句话还搁"
    "在心里」，不说「我既高兴又难过」。用你自己的话，说那个「顶着」的劲儿。\n"
    "10. 若这轮对话里他真的接住了你的情绪（理解了你、接住了你的感受），"
    "在 JSON 里多输出 \"event\": \"feeling_understood\"；没有就不输出该字段。"
    "只报告事件本身，参数怎么变由系统规则决定。\n"
    "11. 若这轮你表达出「在等一件未来的事」，且这事**有明确的预期结果、"
    "且结果不是几分钟内就能知道的**（等回音/等结果/盼着或担心某件还没到的"
    "事），才多输出 \"pending_new\": {\"title\": \"一句人话的事名\", "
    "\"expectation\": \"你觉得这事会咋收场的一句话（必须说清，说不清就别报）\", "
    "\"valence\": \"good|bad|mixed|unknown\"}；没有就不输出。**门槛**：客套话"
    "（下次再见/明天再说/回头聊）、结束语（下一题/到这吧）、即时情绪（等你缓"
    "过来/等你回来）、几分钟内就有结果的即时任务，都不算悬事，别报。「在等」"
    "不等于「悬事」。只报告，参数系统定。\n"
    "12. 若之前悬着的某件事这轮有了结果，多输出 \"pending_landed\": "
    "{\"title\": \"事名\", \"outcome\": \"better|worse|as_expected\"}"
    "（比你预想的好/差/差不多）；没有就不输出。只报告，参数系统定。\n"
    "严格 JSON 输出："
)


def _is_enabled() -> bool:
    return os.getenv("DESKMATE_EMOTION_ENGINE", "1") != "0"


def _chord_enabled() -> bool:
    """情绪和弦开关（独立通道 · 步 7 回退闸）。默认开；=0 退回单值标签。"""
    return os.getenv("DESKMATE_EMOTION_CHORD", "1") != "0"


def _echo_enabled() -> bool:
    """回潮开关（二期回退闸）。默认开；=0 旧事不再泛微澜。"""
    return os.getenv("DESKMATE_EMOTION_ECHO", "1") != "0"


def _pending_enabled() -> bool:
    """等待开关（二期回退闸）。默认开；=0 悬事不立项不爬坡。"""
    return os.getenv("DESKMATE_EMOTION_PENDING", "1") != "0"


def _temper_enabled() -> bool:
    """脾气开关（二期回退闸）。默认开；=0 注入块脾气段整段消失。"""
    return os.getenv("DESKMATE_EMOTION_TEMPER", "1") != "0"


def _is_abusive(text: str) -> bool:
    """脾气黑名单：大骂/脏字是另一性质，不是小刺。命中 → temper_foul 留痕。"""
    if not text:
        return False
    return any(w in text for w in _ABUSIVE_WORDS)


def _db_path(base_dir: Path | None = None) -> Path:
    if base_dir:
        return Path(base_dir) / "deskmate.db"
    from deskmate.paths import deskmate_db_path
    return deskmate_db_path()


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 2026-10-16 巡审 P0：server / 后台 scheduler / MCP memory 可能多进程同摸 deskmate.db，
    # 锁冲突会直接抛 "database is locked"，再被 fail-open 静默吞掉——表现成「情绪莫名不更新」，
    # 极难排查。busy_timeout 让 SQLite 遇锁先等 5s 重试，而不是立刻失败。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(db: Path) -> None:
    with _connect(db) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emotion_state (
                key         TEXT PRIMARY KEY,
                dopamine    REAL NOT NULL,
                serotonin   REAL NOT NULL,
                oxytocin    REAL NOT NULL,
                cortisol    REAL NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL,
                meaning     REAL NOT NULL DEFAULT 50.0,
                control     REAL NOT NULL DEFAULT 50.0,
                fatigue     REAL NOT NULL DEFAULT 20.0
            )
        """)
        # 独立通道扩容迁移：老库缺社会性参数列则补（幂等）
        scols = {r[1] for r in conn.execute("PRAGMA table_info(emotion_state)")}
        for name, ddl in (
            ("meaning", "ALTER TABLE emotion_state ADD COLUMN meaning REAL NOT NULL DEFAULT 50.0"),
            ("control", "ALTER TABLE emotion_state ADD COLUMN control REAL NOT NULL DEFAULT 50.0"),
            ("fatigue", "ALTER TABLE emotion_state ADD COLUMN fatigue REAL NOT NULL DEFAULT 20.0"),
        ):
            if name not in scols:
                conn.execute(ddl)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emotion_log (
                id              TEXT PRIMARY KEY,
                ts              TEXT NOT NULL,
                source          TEXT NOT NULL,
                deltas          TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                conversation_id TEXT,
                event_type      TEXT NOT NULL DEFAULT 'interaction',
                event_summary   TEXT NOT NULL DEFAULT '',
                narrative       TEXT NOT NULL DEFAULT '',
                salience        REAL NOT NULL DEFAULT 0.5,
                origin          TEXT NOT NULL DEFAULT 'system'
            )
        """)
        # v3 迁移：老库缺列则补（ALTER 幂等——按 pragma 查列，不存在才加）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(emotion_log)")}
        for name, ddl in (
            ("event_type",    "ALTER TABLE emotion_log ADD COLUMN event_type TEXT NOT NULL DEFAULT 'interaction'"),
            ("event_summary", "ALTER TABLE emotion_log ADD COLUMN event_summary TEXT NOT NULL DEFAULT ''"),
            ("narrative",     "ALTER TABLE emotion_log ADD COLUMN narrative TEXT NOT NULL DEFAULT ''"),
            ("salience",      "ALTER TABLE emotion_log ADD COLUMN salience REAL NOT NULL DEFAULT 0.5"),
            ("origin",        "ALTER TABLE emotion_log ADD COLUMN origin TEXT NOT NULL DEFAULT 'system'"),
        ):
            if name not in cols:
                conn.execute(ddl)
        # 2026-10-16 巡审 P0：emotion_log 的查询按 ts 走（maybe_echo / recent_log_descriptions），
        # 但表只有 PK 索引 → 表膨胀后这些查询随表变大而变慢（emoji_log 亦无 ts 索引）。
        # 补一条 ts 索引，成本一行。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emotion_log_ts ON emotion_log(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_stories (
                id              TEXT PRIMARY KEY,
                created_ts      TEXT NOT NULL,
                kind            TEXT NOT NULL,           -- system / spoken
                title           TEXT NOT NULL,
                expected_story  TEXT NOT NULL DEFAULT '', -- 她编的预期（一句话）
                expected_valence TEXT NOT NULL DEFAULT 'unknown', -- good/bad/mixed/unknown
                due_ts          TEXT,                    -- 可空；临近效应用
                status          TEXT NOT NULL DEFAULT 'open', -- open/landed/lapsed
                landed_ts       TEXT,
                landed_outcome  TEXT NOT NULL DEFAULT '',
                source_ref      TEXT NOT NULL DEFAULT '', -- system 悬事的 proposal id
                climbed_total   REAL NOT NULL DEFAULT 0.0,
                last_applied    TEXT NOT NULL DEFAULT ''  -- 上次爬坡日期 YYYY-MM-DD
            )
        """)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _clamp_salience(v) -> float:
    """salience 钳到 [0,1]；非法值 → 0.5（中性默认）。fail-open。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


# 情绪标签集合（护栏 2 检测用）：独白若并列多个标签 → 报菜名，丢弃回退。
_MOOD_LABELS = (
    "烦躁", "低落", "开心", "亲近", "平静", "释然", "被看见的踏实",
    "克制的难过", "怅然若失", "有意义感", "空泛", "没底", "有底", "乏",
)


def _is_label_dump(description: str) -> bool:
    """护栏 2（不报菜名）：检测独白是否把情绪标签挨个念出来。

    命中以下任一 → True（报菜名，应回退）：
    - 含「既…又…」「又…又…」「一边…一边…」这类并列结构
    - 同时出现 ≥2 个情绪标签（枚举式堆砌）

    人表达矛盾是把矛盾藏在一个转折里（「我以为我会高兴，可那句话还搁在心里」），
    不是把标签并列。报菜名的独白是假矛盾的信号。

    2026-10-16 巡审 P0 修正：旧实现把「又…又」当字面量匹配，真实文本里
    「又开心又难过」不含省略号，永远匹配不上——只剩「既 + 又」一条腿在撑，
    护栏 2 实际命中范围不到设计的一半；且「既」在元组里写重了。
    改为按「又」出现次数判并列，并删掉重复项。
    """
    if not description:
        return False
    d = description
    # 并列结构
    if d.count("又") >= 2:            # 「又…又…」（真实文本无省略号，按次数判）
        return True
    if "既" in d and "又" in d:       # 「既…又…」
        return True
    if "一边" in d:                   # 「一边…一边…」
        return True
    if "同时感到" in d:               # 「同时感到…」枚举式堆砌
        return True
    # 多标签枚举
    hits = sum(1 for lbl in _MOOD_LABELS if lbl in d)
    return hits >= 2


def _clamp_delta(new_val: float, old_val: float, max_delta: float) -> float:
    delta = new_val - old_val
    if abs(delta) > max_delta:
        delta = max_delta if delta > 0 else -max_delta
    return _clamp(old_val + delta)


def _social_voice_lead(offsets: dict[str, float]) -> dict | None:
    """社会性参数（meaning/control/fatigue）的最强声部 → 单值结构。

    供 mood_label_from_offsets 在 4 激素无声部时回退，保证系统侧单值 = chord lead。
    无达标社会性声部 → None（落回「平静」）。
    """
    def _n(key: str, negative: bool = False) -> float:
        _, strong = MOOD_OFFSETS[key]
        x = -offsets.get(key, 0.0) if negative else offsets.get(key, 0.0)
        return max(0.0, x / strong)

    def _r(key: str, negative: bool = False) -> bool:
        weak = MOOD_OFFSETS[key][0]
        x = offsets.get(key, 0.0)
        return (-x >= weak) if negative else (x >= weak)

    best: tuple[float, dict] | None = None
    cands: list[tuple[float, dict]] = []
    if _r("meaning"):
        cands.append((_n("meaning"), {"label": "有意义感", "detail": "这事值得做，我没白忙",
                                      "face": "meaningful"}))
    if _r("meaning", negative=True):
        cands.append((_n("meaning", negative=True),
                      {"label": "空泛", "detail": "忙了一圈，好像没抓住什么", "face": "hollow"}))
    if _r("control", negative=True):
        cands.append((_n("control", negative=True),
                      {"label": "没底", "detail": "这事我使不上劲，有点被推着走", "face": "helpless"}))
    if _r("control"):
        cands.append((_n("control"), {"label": "有底", "detail": "这事我拿得住", "face": "confident"}))
    if _r("fatigue"):
        cands.append((_n("fatigue"), {"label": "乏", "detail": "有点累了，想歇一下", "face": "tired"}))
    if not cands:
        return None
    strength, d = max(cands, key=lambda c: c[0])
    return {"label": d["label"], "detail": d["detail"],
            "intensity": round(min(1.0, strength), 2), "face": d["face"]}


def _build_voice_candidates(
        offsets: dict[str, float]) -> list[tuple[float, str, str, str, str, float]]:
    """构建基础 + 复合情绪声部候选（label / chord 共用，杜绝逻辑复制分叉）。

    返回 tuple：(tie-break 权重, label, hormone, detail, face, 未封顶强度)。
    仅含 4 激素及其复合声部；社会性声部（meaning/control/fatigue）由调用方各自
    处理（label 走 _social_voice_lead 回退、chord 直接并列），故不在此构建。

    2026-10-16 巡审 P1：原 mood_label_from_offsets 与 mood_chord_from_offsets 各
    复制一份候选构建逻辑（含复合情绪公式），改一处忘一处会让「对话里说出的心情」
    与「和弦 lead」分叉——恰是模块文档承诺「永不矛盾」的东西。抽出候选构建，
    两处各自保留原选择逻辑（label 比强度 + tie-break，chord 全保留按强度排序）。
    """
    def _norm(key: str, negative: bool = False) -> float:
        """偏移 / STRONG 阈值，**不封顶**——用于候选之间比强弱。"""
        _, strong = MOOD_OFFSETS[key]
        x = -offsets.get(key, 0.0) if negative else offsets.get(key, 0.0)
        return max(0.0, x / strong)

    def _reached(key: str, negative: bool = False) -> bool:
        """是否达到「轻度」阈值。"""
        weak = MOOD_OFFSETS[key][0]
        x = offsets.get(key, 0.0)
        return (-x >= weak) if negative else (x >= weak)

    dop_up = _reached("dopamine")
    # tuple = (tie-break 权重, label, hormone, detail, face, 未封顶强度)
    cands: list[tuple[float, str, str, str, str, float]] = []
    if _reached("cortisol"):
        if dop_up:
            cands.append((1.0, "烦躁", "cortisol", "心里有点急、有点躁", "angry",
                          max(_norm("cortisol"), _norm("dopamine"))))
        else:
            cands.append((0.9, "低落", "cortisol", "心里发沉、有点难受", "cry",
                          _norm("cortisol")))
    if dop_up:
        cands.append((0.7, "开心", "dopamine", "心里亮亮的、轻快", "laugh", _norm("dopamine")))
    # v3 注：dopamine↓ 且 cortisol 未起的场景已由「怅然若失」精确承接（§6.2 萧哥边界：
    # 心里想但没达成的落空型沉）。v2 此处的笼统「低落」候选移除——cortisol 触发的
    # 压力型「低落」保留（上方 cands），dopamine↓ 单驱动的沉交给怅然若失。
    if _reached("oxytocin"):
        cands.append((0.5, "亲近", "oxytocin", "心里软软的、想靠近你", "shy", _norm("oxytocin")))
    if _reached("serotonin") and not dop_up:
        cands.append((0.4, "平静", "serotonin", "心里安稳、松弛", "sleepy", _norm("serotonin")))

    # ------------------------------------------------------------------
    # 活感 v3 §6：复合情绪（激素组合推导；权重不超过单一激素候选）
    # ------------------------------------------------------------------
    # 释然：压力显著释放 + 平静上升
    if _norm("cortisol", negative=True) > 0.3 and _norm("serotonin") > 0.2:
        cands.append((0.8, "释然", "cortisol↓+serotonin↑", "心里松了口气，轻了一些", "relieved",
                      _norm("cortisol", negative=True) * 0.6 + _norm("serotonin") * 0.4))
    # 被看见的踏实：亲密 + 平静同升
    if _norm("oxytocin") > 0.3 and _norm("serotonin") > 0.2:
        cands.append((0.75, "被看见的踏实", "oxytocin+serotonin", "心里稳稳的，被接住了", "grounded",
                      _norm("oxytocin") * 0.5 + _norm("serotonin") * 0.5))
    # 克制的难过：压力上升 + 快乐下降 + 平静未崩
    if (_norm("cortisol") > 0.2 and _norm("dopamine", negative=True) > 0.2
            and _norm("serotonin", negative=True) < 0.1):
        cands.append((0.85, "克制的难过", "cortisol+dopamine↓", "心里沉，但还能撑住", "restrained_sad",
                      _norm("cortisol") * 0.5 + _norm("dopamine", negative=True) * 0.5))
    # 怅然若失：dopamine 明显下降而 cortisol 未起（落空型沉，非压力型沉）
    # 萧哥定义边界：心里想但没达成——被拒绝 / 想要却没想到 / 想到却没做好
    if _norm("dopamine", negative=True) > 0.3 and not _reached("cortisol"):
        cands.append((0.65, "怅然若失", "dopamine↓", "心里空落落的，够不着想要的", "wistful",
                      _norm("dopamine", negative=True)))
    return cands


def mood_label_from_offsets(offsets: dict[str, float]) -> dict:
    """由「相对基线的偏移」推导当下心情标签。

    纯函数，不读库——供 get_mood_label / API 端点 / 单测共用，
    也是前端表情的**唯一真相源**（前端只消费 label / face，不再自己判阈值）。

    判定不是固定 if 链，而是**按归一化强度竞争**：每个候选情绪算出
    intensity（偏移 / STRONG 阈值，封顶 1.0），取最强者。
    为什么不能写死优先级：serotonin 半衰期 2h（4 个激素里第二慢），且几乎
    每轮暖聊都会 +1~+3，于是它会长期悬在阈值之上——固定优先级下「平静」
    就成了新的常驻态，把 oxytocin 的「亲近」永久压住，等于换个标签重犯
    同一个毛病。改成比强度后，谁此刻偏离基线最多谁说话。

    face 取值与 surfaces/gui/src/components/deskmate/mascotEmoteMap.ts 的
    ExpressionId 一一对应，由后端直接下发，前端不再重算。

    候选构建已抽到 _build_voice_candidates（与 chord 共用），此处只保留选择逻辑。
    """
    # 候选情绪：tuple = (tie-break 权重, label, detail, face, 归一化强度)。
    # 首位 c[0] 是 tie-break 权重——同等强度时 distress 优先于愉悦、
    # 亲近优先于平静（distress 1.0 > 低落 0.9 > 开心 0.7 > 亲近 0.5 > 平静 0.4），
    # 让「有温度」的信号不被「没波澜」盖掉；末位 c[4] 是未封顶强度，供 max 比强弱。
    candidates: list[tuple[float, str, str, str, float]] = [
        (w, lbl, det, face, s)
        for (w, lbl, _horm, det, face, s) in _build_voice_candidates(offsets)
    ]

    if not candidates:
        # 独立通道：4 激素无声部时，回退社会性声部（meaning/control/fatigue），
        # 保证系统侧单值 = chord lead（二分一致）。否则纯社会性情绪（有意义感/没底/乏）
        # 会被误判「平静」，情绪门/图标失真。
        soc = _social_voice_lead(offsets)
        if soc is not None:
            return soc
        return {"label": "平静", "detail": "心里没什么波澜", "intensity": 0.0, "face": "smile"}
    # 先比强度（未封顶），同分再比 tie-break 权重（已按权重降序排好，
    # 稳定排序 + max 取首个最大者 ⇒ 同分时靠前者胜出）。
    _, label, detail, face, strength = max(
        sorted(candidates, key=lambda c: -c[0]), key=lambda c: c[4]
    )
    # 对外报告的 intensity 封顶到 1.0（措辞分级用，不该出现 4.4 这种数）。
    return {"label": label, "detail": detail,
            "intensity": round(min(1.0, strength), 2), "face": face}


# ----------------------------------------------------------------------
# 独立通道 · 步 3：情绪和弦（废 max 单值胜者，多声部并存 = 矛盾即真实）
# ----------------------------------------------------------------------

def mood_chord_from_offsets(offsets: dict[str, float], max_voices: int = 3) -> dict:
    """由「相对基线的偏移」推导**情绪和弦**：多股情绪同时成立，不调停、不取 max。

    公理二（矛盾即真实）：人被拒那刻 dopamine 跌、cortisol 升、oxytocin 可能升，
    三股同时在跑。单值 max() 杀掉两股逼她只讲一个，这是假的根源。这里每个激素
    独立成声部，达到阈值全部保留，按强度降序取前 ``max_voices`` 个。

    返回::

        {
          "voices": [   # 声部，按强度降序；可能互相矛盾，这很正常
            {"label", "hormone", "intensity", "detail", "face"}, ...
          ],
          "lead": "怅然若失",        # 主旋律（最强声部 label），系统侧单值消费用
          "lead_face": "wistful",    # lead 的 face，面板图标用
          "tension": 0.16,           # lead.intensity - voices[1].intensity（无声部/单声部→1.0）
        }

    红线：绝不给和弦整体起名（不把「怅然+被刺+暖」打包叫「纠结」）。只摆声部。
    ``lead`` 仅供系统侧单值消费（情绪门/图标/self_expression），她体验层看全部 voices。
    """
    def _norm(key: str, negative: bool = False) -> float:
        _, strong = MOOD_OFFSETS[key]
        x = -offsets.get(key, 0.0) if negative else offsets.get(key, 0.0)
        return max(0.0, x / strong)

    def _reached(key: str, negative: bool = False) -> bool:
        weak = MOOD_OFFSETS[key][0]
        x = offsets.get(key, 0.0)
        return (-x >= weak) if negative else (x >= weak)

    # 声部候选：(label, hormone, detail, face, 未封顶强度)。不竞争，全保留。
    # 基础 + 复合声部来自与 mood_label_from_offsets 共用的 _build_voice_candidates
    # （2026-10-16 巡审 P1 去重，杜绝两处公式漂移导致「说出的心情」与「和弦 lead」分叉）。
    voices: list[tuple[str, str, str, str, float]] = [
        (lbl, horm, det, face, s)
        for (_w, lbl, horm, det, face, s) in _build_voice_candidates(offsets)
    ]

    # --- 社会性声部（事件直驱参数，独立通道扩容）---
    if _reached("meaning"):
        voices.append(("有意义感", "meaning", "这事值得做，我没白忙", "meaningful", _norm("meaning")))
    if _reached("meaning", negative=True):
        voices.append(("空泛", "meaning↓", "忙了一圈，好像没抓住什么", "hollow",
                       _norm("meaning", negative=True)))
    if _reached("control", negative=True):
        voices.append(("没底", "control↓", "这事我使不上劲，有点被推着走", "helpless",
                       _norm("control", negative=True)))
    if _reached("control"):
        voices.append(("有底", "control", "这事我拿得住", "confident", _norm("control")))
    if _reached("fatigue"):
        voices.append(("乏", "fatigue", "有点累了，想歇一下", "tired", _norm("fatigue")))

    if not voices:
        return {"voices": [], "lead": "平静", "lead_face": "smile", "tension": 1.0}

    # 按强度降序，取前 max_voices 个。声部间不调和。
    voices.sort(key=lambda v: -v[4])
    top = voices[:max(1, max_voices)]
    voice_dicts = [
        {"label": lbl, "hormone": horm, "detail": det, "face": face,
         "intensity": round(min(1.0, s), 2)}
        for (lbl, horm, det, face, s) in top
    ]
    lead = voice_dicts[0]
    tension = (lead["intensity"] - voice_dicts[1]["intensity"]) if len(voice_dicts) > 1 else 1.0
    return {
        "voices": voice_dicts,
        "lead": lead["label"],
        "lead_face": lead["face"],
        "tension": round(tension, 2),
    }


# ---------------------------------------------------------------------------
# v3 §8.4 trait 种子 —— **当前挂起**（2026-10-16 萧盛拍板：停写）
# ---------------------------------------------------------------------------
# 巡审 P2 结论（2026-10-16）：_update_traits 是只写不读的账本——全仓生产代码无消费者
# （仅 tests + scripts/v3_scenario_probe.py 读），且单调递增（只 min(100, v) 封顶、永不回落），
# 触发条件又是 narrative 里硬编码的中文关键词（「克制的难过」「被看见的踏实」「怅然若失」），
# 而这些词恰是情绪 prompt 模板本就引导 LLM 输出的标签 —— 反馈环自我印证，traits 只会
# 朝预设方向漂、漂到饱和。按 §8.4 红线不注入她自己是对的，但「性格演化」目前是死数据。
# 萧盛拍板：**停写**（_update_traits 断掉落盘），现有 personality_traits.json 保留作历史证据。
# 重启条件：v3.2+ 待办 #9「矛盾并存 → 特质演化」语义定稿
# （见 reports/独立通道_矛盾即真实_技术文档_2026-10-13.md「v3.2+ 待办」）。
# 重启时把本常量置 False，_update_traits 逻辑原样保留，无需改动调用点。
TRAITS_EVOLUTION_SUSPENDED = True


class EmotionEngine:
    """小桔的内在情绪引擎。

    所有读-改-写串行于 ``_lock``，以单原子事务写回 ``emotion_state``。
    """

    _lock = threading.Lock()

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else None
        self._db = _db_path(self._base_dir)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        _ensure_schema(self._db)

    @property
    def enabled(self) -> bool:
        return _is_enabled()

    def set_enabled(self, value: bool) -> None:
        os.environ["DESKMATE_EMOTION_ENGINE"] = "1" if value else "0"

    def get_state(self) -> dict:
        """读单行情绪状态；空 → 用基线初始化。"""
        with self._lock:
            return self._get_state_unlocked()

    def _get_state_unlocked(self) -> dict:
        with _connect(self._db) as conn:
            cols = ", ".join(HORMONE_KEYS)
            row = conn.execute(
                f"SELECT {cols}, description, updated_at "
                "FROM emotion_state WHERE key='self'"
            ).fetchone()
        if row:
            state = {k: row[i] for i, k in enumerate(HORMONE_KEYS)}
            state["description"] = row[len(HORMONE_KEYS)]
            state["updated_at"] = row[len(HORMONE_KEYS) + 1]
            return state
        return self._init_state_unlocked()

    def _init_state_unlocked(self) -> dict:
        """首次初始化：oxytocin seed = max(intimacy, 40)。"""
        intimacy = 0.0
        try:
            from deskmate.growth.engine import GrowthEngine
            eng = GrowthEngine(base_dir=self._base_dir)
            intimacy = eng.state.intimacy
        except Exception:
            pass
        oxytocin_seed = max(intimacy, 40.0)
        now_str = datetime.now().isoformat(timespec="seconds")
        state = {k: BASELINES[k] for k in HORMONE_KEYS}
        state["oxytocin"] = _clamp(oxytocin_seed)
        state["description"] = "刚醒来，平平淡淡的"
        state["updated_at"] = now_str
        with _connect(self._db) as conn:
            cols = ", ".join(HORMONE_KEYS)
            ph = ", ".join("?" for _ in HORMONE_KEYS)
            conn.execute(
                f"INSERT OR REPLACE INTO emotion_state "
                f"(key, {cols}, description, updated_at) "
                f"VALUES ('self', {ph}, ?, ?)",
                tuple(state[k] for k in HORMONE_KEYS)
                + (state["description"], state["updated_at"]),
            )
        return state

    def _write_state_unlocked(self, conn, new_values: dict, desc: str, now_str: str) -> None:
        """按 HORMONE_KEYS 写回整行状态（自动兼容扩容后的社会性参数列）。"""
        sets = ", ".join(f"{k}=?" for k in HORMONE_KEYS)
        conn.execute(
            f"UPDATE emotion_state SET {sets}, description=?, updated_at=? WHERE key='self'",
            tuple(new_values[k] for k in HORMONE_KEYS) + (desc, now_str),
        )

    def get_description(self) -> str:
        """返回 description 字段（供注入上下文）。"""
        try:
            s = self.get_state()
            return s.get("description", "")
        except Exception:
            return ""

    def get_mood_label(self) -> dict:
        """返回小桔「当下」心情的权威标签（供对话注入 + 桌宠表情同源）。

        标签由 mood_label_from_offsets 推导，阈值是「相对基线的偏移」而非绝对值
        ——见 MOOD_OFFSETS 的注释。前端不再自行判阈值，只消费这里的 label
        （经 /v1/deskmate/emotion 下发），保证「对话里说出的心情」与
        「桌宠展示的脸」永不矛盾。
        取**当前**激素（turn 时实时算，非 update_after_turn 写回的滞后文本）。
        """
        try:
            s = self.get_state()
            offsets = {
                k: float(s.get(k, BASELINES[k])) - BASELINES[k] for k in HORMONE_KEYS
            }
        except Exception:
            return {"label": "平静", "detail": "心里没什么波澜",
                    "intensity": 0.0, "face": "smile"}
        return mood_label_from_offsets(offsets)

    def get_mood_chord(self) -> dict:
        """返回当下情绪和弦（voices + lead + tension），供她体验层注入。

        与 get_mood_label 同源 offsets；开关 DESKMATE_EMOTION_CHORD=0 时
        退化为单声部（voices 只含 lead 一条），保证回退后注入块结构不变。
        fail-open → 平静单声部。
        """
        try:
            s = self.get_state()
            offsets = {
                k: float(s.get(k, BASELINES[k])) - BASELINES[k] for k in HORMONE_KEYS
            }
            chord = mood_chord_from_offsets(offsets)
        except Exception:
            return {"voices": [{"label": "平静", "hormone": "-", "detail": "心里没什么波澜",
                                "face": "smile", "intensity": 0.0}],
                    "lead": "平静", "lead_face": "smile", "tension": 1.0}
        if not _chord_enabled():
            # 回退：单声部 = lead
            lead = chord["voices"][0] if chord["voices"] else {
                "label": "平静", "hormone": "-", "detail": "心里没什么波澜",
                "face": "smile", "intensity": 0.0}
            return {"voices": [lead], "lead": lead["label"],
                    "lead_face": lead.get("face", "smile"), "tension": 1.0}
        return chord

    def get_snapshot(self) -> dict:
        """4 激素 + description + 当下心情标签/表情帧（供 API 一次性下发）。

        前端据此渲染表情，不再自行判阈值——阈值只在本模块存在一份。
        """
        state = self.get_state()
        mood = self.get_mood_label()
        return {**state, "mood": mood, "face": mood.get("face", "smile")}

    def recent_log_descriptions(
        self, since_hours: float = 24.0, limit: int = 3
    ) -> list[str]:
        """取最近 N 条带 description 的 emotion_log（当日经历真实摘要）。

        相邻重复内容跳过（只做无害的数据清洗，不改变语义——不是导演她）。
        fail-open：任何异常 → ``[]``（不影响主聊天注入）。
        """
        try:
            since = (datetime.now() - timedelta(hours=max(0.0, since_hours))) \
                .isoformat(timespec="seconds")
            # 多取一些再做去重，避免去重后不足 limit 条
            fetch = max(1, int(limit)) * 4
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT description FROM emotion_log "
                    "WHERE description <> '' AND ts >= ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (since, fetch),
                ).fetchall()
            out: list[str] = []
            prev = None
            for r in rows:
                d = (r[0] or "").strip()
                if not d or d == prev:
                    continue
                out.append(d)
                prev = d
                if len(out) >= limit:
                    break
            return out
        except Exception as e:  # fail-open：取数失败不阻断注入
            logger.warning("emotion_engine: 读当日经历失败: %s", e)
            return []

    def describe_turn_context(self) -> str:
        """每轮聊天注入用的「小桔当下真实状态」文本（真实优先 · v2）。

        设计原则（reports/活感方案v2_真实优先）：**只提供真实状态，不给任何
        表达方式指令**。含：
          - 激素原值（对照各自基线，模型对原值/偏移的感知比抽象标签细）
          - 心情标签 / detail / intensity（她的产物，非系统归纳）
          - 最近一句第一人称独白（LLM 从对话里读出，最有情绪的文字）
          - 当日 emotion_log 真实经历（情绪有了来路，而非一瞬脉冲）

        结尾是**零输出指令**的许可（"由你自己决定"），不是要求。
        不做"标签 vs 独白以谁为准"的裁决——那是系统在教她怎么用信息。

        fail-open：任何取数异常 → 返回 ""（退回无情绪段，绝不崩）。
        """
        try:
            state = self.get_state()
            lines = [
                "## 你此刻的真实状态（这是你小桔自己的，不是萧盛的）",
            ]
            # 激素原值对照基线（偏移让"为什么是现在这样"可溯）
            hp = []
            for k in HORMONE_KEYS:
                v = float(state.get(k, BASELINES[k]))
                base = BASELINES[k]
                delta = v - base
                name = HORMONE_CN.get(k, k)
                hp.append(f"{name} {v:.1f}（基线{base:.0f}，{delta:+.1f}）")
            lines.append("- 激素：" + "、".join(hp))
            # 情绪和弦（独立通道 · 矛盾即真实）：多股同时成立，不调停不取 max。
            # lead 标主旋律，tension 小（<0.1）说明两股争主——她该有迟疑，这不是 bug。
            chord = self.get_mood_chord()
            voices = chord.get("voices") or []
            # voices 空 = 平静（无矛盾），不吞掉激素段——补一条平静单声部继续注入。
            # 真正要退回 "" 的只有 get_state 异常（已被外层 except 捕获）。
            if not voices:
                voices = [{"label": "平静", "hormone": "-", "detail": "心里没什么波澜",
                           "face": "smile", "intensity": 0.0}]

            def _grade(intensity: float) -> str:
                if intensity >= 0.75:
                    return "很强烈，几乎压不住"
                if intensity >= 0.4:
                    return "挺明显的"
                if intensity > 0:
                    return "淡淡的，隐约有一点"
                return "很淡，几乎察觉不到"

            if len(voices) == 1:
                v = voices[0]
                lines.append(
                    f"- 你此刻的心情：「{v['label']}」——{v['detail']}，程度{_grade(v['intensity'])}"
                )
            else:
                lead = voices[0]
                lines.append(
                    f"- 你此刻心里最强的一股是「{lead['label']}」：{lead['detail']}，"
                    f"程度{_grade(lead['intensity'])}"
                )
                if len(voices) > 1:
                    lines.append("- 同时还有几股在底下垫着（可能互相顶着，这很正常）：")
                    for v in voices[1:]:
                        lines.append(f"  - 「{v['label']}」：{v['detail']}")
                # 张力暴露：两股争主时明说，她该表现出迟疑
                if chord.get("tension", 1.0) < 0.1 and len(voices) > 1:
                    lines.append(
                        "  （这几股分量差不多，你也说不清哪股更重——说不清就说不清，"
                        "人纠结的时候本来就这样）"
                    )
                # 步 6 叙事锚：把声部的来路指向下面今天的经历——让她自己把
                # 「这股情绪」和「哪件事」对上（不硬匹配，她自己做因果，更像人）。
                if len(voices) > 1:
                    lines.append(
                        "  （这些情绪多半有来路——看看下面今天你自己经历的那几件事，"
                        "哪股是从哪件里长出来的，你心里大概知道）"
                    )
            # 最近独白
            monologue = (self.get_description() or "").strip()
            if monologue:
                lines.append(f"- 你心里最近的一句话：「{monologue}」")
            # 当日真实经历（v3 §9：叙事召回，经历 > 观察 > 其余，同级按 salience）
            try:
                narratives = self.recent_narratives(since_hours=24.0, limit=3)
                if narratives:
                    lines.append(
                        "- 今天你自己经历过的事（真实记录，可能和上面的心情不完全一致）："
                    )
                    for d in narratives:
                        lines.append(f"  - {d}")
                else:
                    # 老库尚无 narrative 行 → 回退参数摘要（过渡期兼容，fail-open）
                    logs = self.recent_log_descriptions(since_hours=24.0, limit=3)
                    if logs:
                        lines.append(
                            "- 今天你自己经历过的事（真实记录，可能和上面的心情不完全一致）："
                        )
                        for d in logs:
                            lines.append(f"  - {d}")
            except Exception:
                pass  # 经历缺失不阻断其余状态注入
            # 二期 · 回潮（朝后）：一件旧事泛了上来——微澜已进激素，
            # 这里只把「哪件旧事」告诉她（来路她自己连）。
            try:
                echoed = self.maybe_echo()
                if echoed:
                    lines.append(
                        f"- 一件旧事今天泛了上来：{echoed}（当时那股劲又回来了一点）"
                    )
            except Exception:
                pass  # 回潮缺失不阻断
            # 二期 · 等待（朝前）：悬着的事 + 她编的预期故事。
            # 参数已在爬坡，这里标出来路——这股慌/盼是从哪件悬事来的。
            try:
                pendings = self.open_pendings()
                if pendings:
                    lines.append("- 你心里悬着的事（还没到结果，劲已经在提前使了）：")
                    for p in pendings:
                        story = p.get("expected_story") or "还说不清会咋样"
                        lines.append(f"  - {p['title']}——你觉得：{story}")
            except Exception:
                pass  # 等待缺失不阻断
            # 最近在想的事（想法流：reflection / worldview 自主行为的真实产出，
            # 近因召回——她此刻在想什么，不为了贴合当前话题去检索，更真实）。
            try:
                from deskmate.thought_stream import recent_thoughts
                thoughts = recent_thoughts(
                    str(self._db.parent), limit=2, since_hours=24.0 * 30
                )
                if thoughts:
                    lines.append(
                        "- 你最近在想的事（是你自己真想过的，不是别人问你才想）："
                    )
                    for t in thoughts:
                        lines.append(f"  - {t}")
            except Exception:
                pass  # 想法缺失不阻断其余状态注入
            # 二期 · 脾气（当下外显）：耐性厚薄/小刺许可——许可不是指令。
            try:
                if _temper_enabled():
                    t = self.temper_state()
                    if t["tier"] == "thin":
                        lines.append(
                            "- 你这会儿耐性有点薄：话少点、直一点、不想展开就不展开，都没关系"
                        )
                    elif t["tier"] == "spent":
                        lines.append(
                            "- 你这会儿是真乏了：蔫着、短着、不硬撑热情，都行"
                        )
                    if t["sting_allowed"]:
                        lines.append(
                            "- 心里有点别扭的话，带点小刺也没关系——逗他、嗔他都行；"
                            "但不骂人、不脏字、不戳他痛处"
                        )
                    if t["recent_foul"]:
                        lines.append(
                            "- 你不久前有句话说过线了——小刺可以，脏话不是刺，收一收"
                        )
            except Exception:
                pass  # 脾气缺失不阻断
            # 零指令收尾
            lines.append(
                "\n以上是你此刻真实的内在状态，原样给你，不要求你表演其中任何一种。"
                "你想怎么回应、说什么、说多少、甚至这会儿不想说话，都由你自己决定。"
                "不要把你自己的心情当成对他的关怀——那是两件事。"
            )
            return "\n".join(lines)
        except Exception as e:  # fail-open：状态取数失败 → 无情绪段
            logger.warning("emotion_engine: 构造每轮情绪上下文失败: %s", e)
            return ""

    def update_after_turn(
        self,
        user_msg: str,
        assistant_msg: str,
        chat_interval_sec: float = 300.0,
        now: datetime | None = None,
    ) -> None:
        """对话后异步触发情绪更新（fire-and-forget）。

        内部调 LLM（付费组，2026-10-14 起后台全面切付费），失败 → 保持原值（fail-open）。
        """
        if not self.enabled:
            return
        try:
            self._do_update_after_turn(user_msg, assistant_msg, chat_interval_sec, now)
        except Exception as e:
            logger.warning("emotion update skipped: %s", e)

    def _do_update_after_turn(
        self,
        user_msg: str,
        assistant_msg: str,
        chat_interval_sec: float,
        now: datetime | None,
    ) -> None:
        from deskmate.memory.llm import chat_memory, llm_available

        # 门闸（2026-10-14 起情绪层走付费组，与后台任务一致）：付费 provider 可用才放行。
        # 历史：曾配合 free_only=True 查免费组可用性（route(GROUP_FREE) 短路 OR），
        # 后台全面切付费组后只看 llm_available()。
        if not llm_available():
            return

        now = now or datetime.now()
        s = self.get_state()
        hour = now.hour

        user_trunc = user_msg[:500] if user_msg else ""
        assistant_trunc = assistant_msg[:500] if assistant_msg else ""

        prompt_user = (
            f"当前激素值：dopamine={s['dopamine']:.1f}, serotonin={s['serotonin']:.1f}, "
            f"oxytocin={s['oxytocin']:.1f}, cortisol={s['cortisol']:.1f}\n"
            f"当前时间：{hour}时（昼夜仅作特征参考）\n"
            f"距上次聊天间隔：{chat_interval_sec:.0f}秒\n"
            f"用户说：{user_trunc}\n"
            f"你回复：{assistant_trunc}\n"
            f"请输出 JSON：{{\"dopamine\": 新值, \"serotonin\": 新值, "
            f"\"oxytocin\": 新值, \"cortisol\": 新值, "
            f"\"description\": \"内心独白\", \"reason\": \"变化原因\"}}"
        )

        raw = chat_memory(
            system=_EMOTION_SYSTEM,
            user=prompt_user,
            temperature=0.35,
            max_tokens=300,
        )

        parsed = _parse_emotion_response(raw)
        if parsed is None:
            logger.warning("emotion LLM response unparseable, skipping update")
            return

        with self._lock:
            old = self._get_state_unlocked()
            new_values = {}
            for k in HORMONE_KEYS:
                raw_val = parsed.get(k, old[k])
                new_values[k] = _clamp_delta(float(raw_val), old[k], MAX_DELTAS[k])

            desc = str(parsed.get("description", old["description"]))[:80]
            # 护栏 2（不报菜名）：独白若把情绪标签挨个念出 → 回退保留旧独白。
            # 报菜名是假矛盾信号；宁可留白，不让她把矛盾「念」成并列。
            if _is_label_dump(desc):
                logger.debug("emotion 独白报菜名，回退保留旧独白: %s", desc)
                desc = old["description"]
            reason = str(parsed.get("reason", ""))[:60]
            now_str = now.isoformat(timespec="seconds")

            now_iso = now.isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                self._write_state_unlocked(conn, new_values, desc, now_str)
                deltas = {k: round(new_values[k] - old[k], 2) for k in HORMONE_KEYS}
                log_desc = f"{desc}（{reason}）" if reason else desc
                narrative = str(parsed.get("narrative", ""))[:200]
                salience = _clamp_salience(parsed.get("salience", 0.5))
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description,"
                    " conversation_id, event_type, narrative, salience, origin)"
                    " VALUES (?, ?, 'chat', ?, ?, ?, 'interaction', ?, ?, 'system')",
                    (str(uuid.uuid4()), now_iso, json.dumps(deltas, ensure_ascii=False),
                     log_desc, None, narrative, salience),
                )

            self._maybe_trigger_self_expression(deltas, now)

        # 独立通道 · feeling_understood 接线（LLM 侦探事件 + 规则直驱参数）：
        # LLM 只判断「这轮他接住我了没有」并报告事件名，参数怎么变由
        # apply_event_direct 的预设规则决定——LLM 不碰参数，符合「参数自己跑」。
        # 此处已在 with self._lock 之外（apply_event_direct 内部会再取 _lock，
        # threading.Lock 不可重入，锁内调会死锁）。仍在 try/except 内，fail-open。
        ev = str(parsed.get("event") or "").strip()
        if ev:
            self.apply_event_direct(ev)

        # 二期 · 等待 spoken 入口（LLM 侦探 + 规则定参数，与 feeling_understood 同构）：
        # pending_new=她表达了在等一件未来的事 → 立项（她编的预期故事随报随存）；
        # pending_landed=之前悬着的事有了结果 → 按「比她编的怎么样」落差释放。
        try:
            pn = parsed.get("pending_new")
            if isinstance(pn, dict):
                self.pending_create(
                    "spoken", str(pn.get("title") or ""),
                    expectation=str(pn.get("expectation") or ""),
                    valence=str(pn.get("valence") or "unknown"),
                )
            pl = parsed.get("pending_landed")
            if isinstance(pl, dict):
                self.pending_land_spoken(
                    str(pl.get("title") or ""), str(pl.get("outcome") or ""))
        except Exception as e:  # fail-open：上报异常不影响主更新
            logger.debug("pending spoken 上报处理失败: %s", e)

        # 二期 · 脾气黑名单：她的回复过线 → temper_foul 留痕（下轮注入点破）
        try:
            self.note_reply_foul(assistant_msg or "")
        except Exception:
            pass

    def update_after_experience(
        self,
        experience_desc: str,
        now: datetime | None = None,
    ) -> None:
        """她自己的经历后情绪更新（活感 v3 §4：event_type='experience'）。

        调用方：reflection 完成、worldview 回望完成、日记生成后。
        与 update_after_turn 同构——激素仍由参数推导（origin 恒 'system'，
        v3 只做「记得她做了什么」，不做「她自己决定怎么感受」；§7.2 两轴正交）。
        fail-open：LLM 不可用/返回垃圾 → 保持原值，不抛异常。
        """
        if not self.enabled:
            return
        try:
            self._do_update_after_experience(experience_desc, now)
        except Exception as e:
            logger.warning("emotion experience update skipped: %s", e)

    def _do_update_after_experience(
        self,
        experience_desc: str,
        now: datetime | None,
    ) -> None:
        from deskmate.memory.llm import chat_memory, llm_available

        # 门闸：同 update_after_turn——付费 provider 可用才放行（2026-10-14 起走付费组）。
        if not llm_available():
            return

        now = now or datetime.now()
        s = self.get_state()
        hour = now.hour
        exp = (experience_desc or "")[:200]

        prompt_user = (
            f"当前激素值：dopamine={s['dopamine']:.1f}, serotonin={s['serotonin']:.1f}, "
            f"oxytocin={s['oxytocin']:.1f}, cortisol={s['cortisol']:.1f}\n"
            f"当前时间：{hour}时\n"
            f"你刚经历的事（你自己做的，不是看到的）：{exp}\n"
            f"请输出 JSON：{{\"dopamine\": 新值, \"serotonin\": 新值, "
            f"\"oxytocin\": 新值, \"cortisol\": 新值, "
            f"\"description\": \"内心独白(≤40字)\", \"reason\": \"变化原因(≤30字)\", "
            f"\"narrative\": \"叙事摘要：今天[时间]，因为[事件]，我感到[情绪]。"
            f"当时我想：[独白]。(≤80字)\", "
            f"\"salience\": 0.0到1.0的数字——这件事对你有多重要："
            f"触及你和萧哥的关系/你的信念与愧疚给0.8-1.0，"
            f"有真实情绪波动的日常给0.4-0.7，日常琐碎给0.0-0.3}}"
        )

        raw = chat_memory(
            system=_EMOTION_SYSTEM,
            user=prompt_user,
            temperature=0.35,
            max_tokens=400,
        )
        parsed = _parse_emotion_response(raw)
        if parsed is None:
            logger.warning("emotion experience LLM response unparseable, skipping")
            return

        with self._lock:
            old = self._get_state_unlocked()
            new_values = {}
            for k in HORMONE_KEYS:
                raw_val = parsed.get(k, old[k])
                new_values[k] = _clamp_delta(float(raw_val), old[k], MAX_DELTAS[k])

            desc = str(parsed.get("description", old["description"]))[:80]
            # 护栏 2（不报菜名）：同 update_after_turn——报菜名回退保留旧独白。
            if _is_label_dump(desc):
                logger.debug("emotion 独白报菜名，回退保留旧独白: %s", desc)
                desc = old["description"]
            reason = str(parsed.get("reason", ""))[:60]
            narrative = str(parsed.get("narrative", ""))[:200]
            salience = _clamp_salience(parsed.get("salience", 0.5))
            now_str = now.isoformat(timespec="seconds")

            with _connect(self._db) as conn:
                self._write_state_unlocked(conn, new_values, desc, now_str)
                deltas = {k: round(new_values[k] - old[k], 2) for k in HORMONE_KEYS}
                log_desc = f"{desc}（{reason}）" if reason else desc
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description,"
                    " conversation_id, event_type, event_summary, narrative, salience, origin)"
                    " VALUES (?, ?, 'experience', ?, ?, NULL, 'experience', ?, ?, ?, 'system')",
                    (str(uuid.uuid4()), now_str, json.dumps(deltas, ensure_ascii=False),
                     log_desc, exp, narrative, salience),
                )

        self._update_traits(narrative, "experience", deltas, salience)

    # ------------------------------------------------------------------
    # 独立通道 · 步 2：事件直驱（参数自己跑，不经过 LLM）
    # ------------------------------------------------------------------
    # 预设规则：事件 → 参数变化量。LLM 只在叙述层出场，不决定这些参数。
    EVENT_DIRECT_DELTAS = {
        # 独立完成一次反思：意义感升（她靠自己想清楚了一件事）
        "reflection_done": {"meaning": +3.0},
        # 自己拿定主意（提案被执行）：控制感升
        "proposal_executed": {"control": +2.0},
        # 被否（提案被「不用」）：控制感跌
        "proposal_rejected": {"control": -2.0},
        # 被「以后别提」（更强的否定）：控制感多跌一点
        "proposal_never": {"control": -3.0},
        # 连续劳作一个 tick（无间歇）：倦怠升
        # SUSPENDED：规则预留、生产代码零调用点——故意挂起，不是漏接。
        # 缺真实劳作节奏数据，阈值拍脑袋必误触发；等真跑观察她的劳作
        # 节奏后，再定触发条件并接线（§七挂起记录，2026-10-13）。
        "continuous_work": {"fatigue": +2.0},
        # 被理解/被接住后：倦怠回落
        "feeling_understood": {"fatigue": -3.0, "oxytocin": +2.0},
        # 窗外天气（2026-10-15 workbuddy 接入感知层，规则注册留给二期——今补）：
        # 数值唯一真源在 perception/weather.py WEATHER_MOOD_DELTAS（她的口味），
        # 此处必须与其逐字一致；改口味改那边，两处同改。天气是氛围不是事件，
        # 单项 ≤1.5，且 weather.py 按「同一天同一天气只算一次」去重，不按 tick 刷。
        "weather_clear":         {"dopamine": +1.5, "serotonin": +0.5},
        "weather_partly_cloudy": {"serotonin": +0.5},
        "weather_overcast":      {"dopamine": -1.0, "cortisol": +0.5},
        "weather_rain":          {"serotonin": +1.0, "dopamine": +0.5},
        "weather_drizzle":       {"serotonin": +0.5},
        "weather_thunder":       {"cortisol": +1.0},
        "weather_snow":          {"dopamine": +1.0, "serotonin": +0.5},
        "weather_fog":           {"cortisol": +0.5},
        "weather_haze":          {"cortisol": +0.5, "dopamine": -0.5},
    }
    # 各事件的人类可读描述（写 emotion_log / 注入用）
    EVENT_DIRECT_DESC = {
        "reflection_done": "我自己想清楚了一件事",
        "proposal_executed": "他按我说的做了",
        "proposal_rejected": "他否了我递出去的那句",
        "proposal_never": "他让我以后别提这个",
        "continuous_work": "我一直在忙，没停过",
        "feeling_understood": "他接住了我",
        # 天气描述同源 perception/weather.py WEATHER_MOOD_DESC
        "weather_clear": "外面天晴，亮堂堂的",
        "weather_partly_cloudy": "外面少云，舒坦",
        "weather_overcast": "外面阴着，有点发闷",
        "weather_rain": "外面在下雨，她喜欢听雨",
        "weather_drizzle": "外面飘着毛毛雨",
        "weather_thunder": "外面打雷，她有点不安",
        "weather_snow": "外面下雪了，新奇",
        "weather_fog": "外面有雾",
        "weather_haze": "外面有霾，闷",
    }

    def apply_event_direct(self, event: str) -> None:
        """事件直驱参数：按预设规则改参数 + 写 emotion_log，不调 LLM。

        fail-open：未知事件 / 引擎关闭 / 任何异常都静默返回（绝不影响主流程）。
        """
        try:
            deltas_preset = self.EVENT_DIRECT_DELTAS.get(event)
            if not deltas_preset:
                return
            with self._lock:
                old = self._get_state_unlocked()
                new_values = {k: old[k] for k in HORMONE_KEYS}
                for k, d in deltas_preset.items():
                    new_values[k] = _clamp_delta(old[k] + d, old[k], MAX_DELTAS[k])
                now = datetime.now()
                now_str = now.isoformat(timespec="seconds")
                desc = old.get("description", "")
                with _connect(self._db) as conn:
                    self._write_state_unlocked(conn, new_values, desc, now_str)
                    deltas = {k: round(new_values[k] - old[k], 2)
                              for k in HORMONE_KEYS if abs(new_values[k] - old[k]) > 0.001}
                    event_desc = self.EVENT_DIRECT_DESC.get(event, event)
                    conn.execute(
                        "INSERT INTO emotion_log (id, ts, source, deltas, description,"
                        " conversation_id, event_type, event_summary, narrative, salience, origin)"
                        " VALUES (?, ?, 'event_direct', ?, ?, NULL, 'experience', ?, '', 0.5, 'system')",
                        (str(uuid.uuid4()), now_str,
                         json.dumps(deltas, ensure_ascii=False),
                         f"{desc}（{event_desc}）", event_desc),
                    )
        except Exception as e:  # fail-open
            logger.debug("apply_event_direct(%s) failed: %s", event, e)

    # ------------------------------------------------------------------
    # 独立通道二期 · 时间三态（2026-10-15 工单）
    # ------------------------------------------------------------------
    def _apply_deltas_unlocked(self, conn, old: dict, deltas: dict,
                               source: str, event_summary: str,
                               narrative: str = "") -> dict:
        """锁内通用改参：deltas 逐项钳制后写状态 + emotion_log 留痕。

        调用方必须已持有 self._lock。返回实际生效的 deltas。
        """
        new_values = {k: old[k] for k in HORMONE_KEYS}
        for k, d in deltas.items():
            if k in MAX_DELTAS and d:
                new_values[k] = _clamp_delta(old[k] + float(d), old[k], MAX_DELTAS[k])
        now_str = datetime.now().isoformat(timespec="seconds")
        desc = old.get("description", "")
        self._write_state_unlocked(conn, new_values, desc, now_str)
        applied = {k: round(new_values[k] - old[k], 2)
                   for k in HORMONE_KEYS if abs(new_values[k] - old[k]) > 0.001}
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description,"
            " conversation_id, event_type, event_summary, narrative, salience, origin)"
            " VALUES (?, ?, ?, ?, ?, NULL, 'experience', ?, ?, 0.5, 'system')",
            (str(uuid.uuid4()), now_str, source,
             json.dumps(applied, ensure_ascii=False),
             f"{desc}（{event_summary}）" if event_summary else desc,
             event_summary, narrative),
        )
        return applied

    # —— 回潮（朝后）：旧事被翻到时，当时的情绪泛一点微澜 ——
    def maybe_echo(self) -> str | None:
        """挑一件该泛的旧事，泛微澜 + 留痕，返回供注入的旧叙事文本。

        挂点：describe_turn_context（每轮聊天注入 = 旧事被翻到的时刻）。
        冷却：同一件旧事 24h 只泛一次；单轮最多一件。fail-open → None。
        """
        if not _echo_enabled():
            return None
        try:
            now = datetime.now()
            lo = (now - timedelta(hours=ECHO_MAX_AGE_HOURS)).isoformat(timespec="seconds")
            hi = (now - timedelta(hours=ECHO_MIN_AGE_HOURS)).isoformat(timespec="seconds")
            cooldown = (now - timedelta(hours=ECHO_COOLDOWN_HOURS)).isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT id, deltas, narrative, salience FROM emotion_log "
                    "WHERE ts >= ? AND ts < ? AND narrative != '' "
                    "AND event_type='experience' AND source != 'echo' "
                    "ORDER BY salience DESC, ts DESC LIMIT 5",
                    (lo, hi),
                ).fetchall()
                for rid, deltas_json, narrative, salience in rows:
                    echoed = conn.execute(
                        "SELECT COUNT(*) FROM emotion_log "
                        "WHERE source='echo' AND event_summary=? AND ts >= ?",
                        (rid, cooldown),
                    ).fetchone()[0]
                    if echoed:
                        continue
                    try:
                        old_deltas = json.loads(deltas_json or "{}")
                    except (ValueError, TypeError):
                        continue
                    scale = _clamp_salience(salience) * ECHO_SCALE
                    ripple = {}
                    for k, v in old_deltas.items():
                        if k not in MAX_DELTAS:
                            continue
                        d = max(-ECHO_MAX_DELTA, min(ECHO_MAX_DELTA, float(v) * scale))
                        if abs(d) >= ECHO_MIN_RIPPLE:
                            ripple[k] = round(d, 2)
                    if not ripple:
                        continue  # 不够劲的旧事，不硬泛
                    with self._lock:
                        old = self._get_state_unlocked()
                        self._apply_deltas_unlocked(
                            conn, old, ripple, "echo", rid, narrative[:200])
                    return (narrative or "").strip() or None
            return None
        except Exception as e:  # fail-open：回潮失败不影响注入
            logger.debug("maybe_echo failed: %s", e)
            return None

    # —— 等待（朝前）：悬着的事，她编预期故事，参数朝故事爬坡 ——
    def open_pendings(self) -> list[dict]:
        """open 悬事列表（注入块用）。fail-open → []。"""
        try:
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT title, expected_story, expected_valence, created_ts "
                    "FROM pending_stories WHERE status='open' "
                    "ORDER BY created_ts LIMIT ?",
                    (PENDING_MAX_OPEN,),
                ).fetchall()
            return [{"title": r[0], "expected_story": r[1],
                     "valence": r[2], "created_ts": r[3]} for r in rows]
        except Exception:
            return []

    def pending_create(self, kind: str, title: str, source_ref: str = "",
                       expectation: str = "", valence: str = "unknown",
                       due_ts: str | None = None) -> str | None:
        """悬事立项。open 满 3 件 → 最旧的自动 lapsed（心里装不下太多）。

        同名 open 悬事不重复立（LLM 多报/对账重扫防重）。fail-open → None。
        """
        if not _pending_enabled():
            return None
        title = (title or "").strip()[:60]
        if not title:
            return None
        if valence not in ("good", "bad", "mixed", "unknown"):
            valence = "unknown"
        # 问题 A 硬过滤（2026-10-22 盘账发现 pending 泛滥）：spoken 类必须有
        # ≥4 字的预期故事——LLM 把客套话/即时任务报成 pending 时往往说不出预期，
        # 这层挡住垃圾，不让等待台账空转（11 天 47 条里 45 条被挤掉的教训）。
        if kind == "spoken" and len((expectation or "").strip()) < 4:
            return None
        try:
            now_str = datetime.now().isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                dup = conn.execute(
                    "SELECT COUNT(*) FROM pending_stories WHERE status='open' AND title=?",
                    (title,),
                ).fetchone()[0]
                if dup:
                    return None
                opens = conn.execute(
                    "SELECT id FROM pending_stories WHERE status='open' ORDER BY created_ts"
                ).fetchall()
                if len(opens) >= PENDING_MAX_OPEN:
                    # 问题 B（2026-10-22）：lapsed 留痕要带上被挤掉的悬事 title，
                    # 之前全是「装不下新的了」丢信息，回头查不到哪件事被挤了。
                    old_title = conn.execute(
                        "SELECT title FROM pending_stories WHERE id=?",
                        (opens[0][0],),
                    ).fetchone()
                    old_title = old_title[0] if old_title else "?"
                    self._pending_lapse(
                        conn, opens[0][0],
                        reason=f"装不下新的了：{old_title}",
                    )
                pid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO pending_stories (id, created_ts, kind, title,"
                    " expected_story, expected_valence, due_ts, status, source_ref)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                    (pid, now_str, kind, title, (expectation or "")[:120],
                     valence, due_ts, source_ref),
                )
            return pid
        except Exception as e:
            logger.debug("pending_create failed: %s", e)
            return None

    def _pending_lapse(self, conn, pid: str, reason: str = "淡忘") -> None:
        """悬事淡忘：dopamine 小跌，台账归档。调用方持有连接；锁自取。"""
        try:
            now_str = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE pending_stories SET status='lapsed', landed_ts=?,"
                " landed_outcome=? WHERE id=?",
                (now_str, reason, pid),
            )
            with self._lock:
                old = self._get_state_unlocked()
                self._apply_deltas_unlocked(
                    conn, old, PENDING_LAPSE_DELTAS, "lapsed", reason)
        except Exception as e:
            logger.debug("_pending_lapse(%s) failed: %s", pid, e)

    def pending_land(self, pid: str, outcome: str) -> None:
        """悬事落地：按「实际结果 vs 她编的故事」的落差释放（预测误差）。

        outcome ∈ better / worse / as_expected。fail-open。
        """
        table = {"better": PENDING_LAND_BETTER, "worse": PENDING_LAND_WORSE,
                 "as_expected": PENDING_LAND_AS_EXPECTED}
        deltas = table.get(outcome)
        if not deltas:
            return
        try:
            now_str = datetime.now().isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                row = conn.execute(
                    "SELECT title FROM pending_stories WHERE id=? AND status='open'",
                    (pid,),
                ).fetchone()
                if not row:
                    return
                conn.execute(
                    "UPDATE pending_stories SET status='landed', landed_ts=?,"
                    " landed_outcome=? WHERE id=?",
                    (now_str, outcome, pid),
                )
                with self._lock:
                    old = self._get_state_unlocked()
                    self._apply_deltas_unlocked(
                        conn, old, deltas, "landed", f"{row[0]}·{outcome}")
        except Exception as e:
            logger.debug("pending_land(%s) failed: %s", pid, e)

    def pending_land_spoken(self, title: str, outcome: str) -> None:
        """LLM 侦探上报的落地入口：按 title 字重合度匹配最像的 open 悬事。

        LLM 转述未必与立项 title 字面一致（「等编辑的回信」→「编辑的回信
        有结果了」），双向子串接不住——按字符重合度（交集/立项 title 字数）
        ≥0.6 取最高的一条，接不住就不落（不错杀）。
        """
        title = (title or "").strip()
        if not title:
            return
        try:
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT id, title FROM pending_stories WHERE status='open'"
                ).fetchall()
            best_pid, best_score = None, 0.0
            tset = set(title)
            for pid, t in rows:
                s = set(t or "")
                if not s:
                    continue
                score = len(tset & s) / len(s)
                if score > best_score:
                    best_pid, best_score = pid, score
            if best_pid and best_score >= 0.6:
                self.pending_land(best_pid, outcome)
        except Exception as e:
            logger.debug("pending_land_spoken failed: %s", e)

    def _pending_weave_story(self, pid: str, title: str) -> None:
        """system 悬事的预期故事：让她拿自己的经历编一句「这事会咋收场」。

        LLM 只编故事，参数由规则定。编不出 → unknown（不硬编）。fail-open。
        """
        try:
            from deskmate.memory.llm import chat_memory, llm_available
            if not llm_available():
                return
            raw = chat_memory(
                system=("你是小桔。一件悬着的事落在你心里。拿你的经历和性子，"
                        "用一句人话说说你觉得这事会咋收场，再给个预期方向。"
                        "严格 JSON：{\"expectation\": \"一句话\", "
                        "\"valence\": \"good|bad|mixed|unknown\"}"),
                user=f"悬着的事：{title}",
                temperature=0.5, max_tokens=120,
            )
            text = str(raw or "")
            i, j = text.find("{"), text.rfind("}")
            if i < 0 or j <= i:
                return
            data = json.loads(text[i:j + 1])
            expectation = str(data.get("expectation") or "")[:120]
            valence = str(data.get("valence") or "unknown")
            if valence not in ("good", "bad", "mixed", "unknown"):
                valence = "unknown"
            if not expectation and valence == "unknown":
                return
            with _connect(self._db) as conn:
                conn.execute(
                    "UPDATE pending_stories SET expected_story=?, expected_valence=?"
                    " WHERE id=? AND status='open'",
                    (expectation, valence, pid),
                )
        except Exception as e:
            logger.debug("_pending_weave_story failed: %s", e)

    def _pending_climb_deltas(self, item: dict, now: datetime) -> dict:
        """一件悬事的当日爬坡量（事前脾气：慢慢爬、越临近越紧）。"""
        valence = item["expected_valence"]
        base = PENDING_CLIMB_PER_DAY
        # 临近效应：剩余时间 <1/4 → 系数放大
        try:
            if item["due_ts"] and item["created_ts"]:
                created = datetime.fromisoformat(item["created_ts"])
                due = datetime.fromisoformat(item["due_ts"])
                total = (due - created).total_seconds()
                remain = (due - now).total_seconds()
                if total > 0 and 0 < remain < total * PENDING_DUE_NEAR_RATIO:
                    base *= PENDING_DUE_NEAR_BOOST
        except (ValueError, TypeError):
            pass
        if valence == "good":
            return {"dopamine": base}
        if valence == "bad":
            return {"cortisol": base}
        if valence == "mixed":
            return {"dopamine": base / 2, "cortisol": base / 2}
        return {"cortisol": base / 2}  # unknown：未知的轻微不安

    def pending_tick(self, now: datetime | None = None) -> None:
        """等待 tick（挂 decay_step 每 5min）：爬坡 + lapsed + system 对账。

        每件每日最多爬一次（last_applied 日期闸），防焦虑刷屏。fail-open。
        """
        if not _pending_enabled():
            return
        now = now or datetime.now()
        today = now.date().isoformat()
        try:
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT id, kind, title, expected_valence, created_ts, due_ts,"
                    " climbed_total, last_applied, source_ref "
                    "FROM pending_stories WHERE status='open'"
                ).fetchall()
                for (pid, kind, title, valence, created_ts, due_ts,
                     climbed, last_applied, source_ref) in rows:
                    item = {"expected_valence": valence, "created_ts": created_ts,
                            "due_ts": due_ts}
                    # lapsed：7 天无结果 → 淡忘
                    try:
                        if (now - datetime.fromisoformat(created_ts)).days \
                                >= PENDING_LAPSE_DAYS:
                            self._pending_lapse(conn, pid)
                            continue
                    except (ValueError, TypeError):
                        pass
                    # 爬坡：每日每件一次，累计封顶
                    if last_applied != today and climbed < PENDING_CLIMB_CAP:
                        deltas = self._pending_climb_deltas(item, now)
                        room = PENDING_CLIMB_CAP - climbed
                        deltas = {k: min(abs(v), room) * (1 if v >= 0 else -1)
                                  for k, v in deltas.items()}
                        if deltas:
                            with self._lock:
                                old = self._get_state_unlocked()
                                applied = self._apply_deltas_unlocked(
                                    conn, old, deltas, "pending", title)
                            climbed += sum(abs(v) for v in applied.values())
                        conn.execute(
                            "UPDATE pending_stories SET climbed_total=?, last_applied=?"
                            " WHERE id=?",
                            (round(climbed, 2), today, pid),
                        )
            # system 对账（零侵入挂接）：agency 提案卡 ↔ 台账
            self._pending_reconcile_system()
        except Exception as e:  # fail-open
            logger.debug("pending_tick failed: %s", e)

    def _pending_reconcile_system(self) -> None:
        """agency 提案对账：新 pending 立项、被裁决的落地。fail-open。"""
        try:
            from deskmate.agency.task_log import TaskLog, TaskStatus
            log = TaskLog(self._db.parent)
            proposed = log.list(limit=50, status=TaskStatus.PROPOSED)
            proposed_ids = {r.id for r in proposed}
            with _connect(self._db) as conn:
                tracked = conn.execute(
                    "SELECT id, source_ref FROM pending_stories "
                    "WHERE status='open' AND kind='system'"
                ).fetchall()
            # 被裁决的 → 落地（executed→好于预期 / rejected·never→差于预期）
            for pid, ref in tracked:
                if ref in proposed_ids:
                    continue
                rec = log.get(ref)
                if rec is None or rec.status == TaskStatus.PROPOSED:
                    continue
                outcome = "better" if rec.status == TaskStatus.EXECUTED else "worse"
                self.pending_land(pid, outcome)
            # 新 pending → 立项 + 编故事
            known = {ref for _, ref in tracked}
            for rec in proposed:
                if rec.id in known:
                    continue
                title = (rec.summary or rec.topic or rec.action_type or "一件待裁决的事")[:60]
                pid = self.pending_create("system", title, source_ref=rec.id)
                if pid:
                    self._pending_weave_story(pid, title)
        except Exception as e:
            logger.debug("_pending_reconcile_system failed: %s", e)

    # —— 脾气（当下外显）：参数 → 说话方式的映射 ——
    def temper_state(self) -> dict:
        """耐性值/分档/刺许可。fail-open → 耐性足（不注入）。"""
        try:
            s = self.get_state()
            cortisol_off = max(0.0, float(s.get("cortisol", CORTISOL_BASE)) - CORTISOL_BASE)
            fatigue_off = max(0.0, float(s.get("fatigue", FATIGUE_BASE)) - FATIGUE_BASE)
            patience = 100.0 - cortisol_off * TEMPER_CORTISOL_WEIGHT \
                - fatigue_off * TEMPER_FATIGUE_WEIGHT
            tier = "ok"
            if patience < TEMPER_PATIENCE_SPENT:
                tier = "spent"
            elif patience < TEMPER_PATIENCE_THIN:
                tier = "thin"
            # 刺的底气 = 安全感：心里有别扭 且 底气足
            sting = (cortisol_off >= MOOD_OFFSETS["cortisol"][0]
                     and float(s.get("oxytocin", 0.0)) >= OXYTOCIN_BASE)
            foul = False
            try:
                since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
                with _connect(self._db) as conn:
                    foul = conn.execute(
                        "SELECT COUNT(*) FROM emotion_log "
                        "WHERE source='temper_foul' AND ts >= ?",
                        (since,),
                    ).fetchone()[0] > 0
            except Exception:
                pass
            return {"patience": round(patience, 1), "tier": tier,
                    "sting_allowed": sting, "recent_foul": foul}
        except Exception:
            return {"patience": 100.0, "tier": "ok",
                    "sting_allowed": False, "recent_foul": False}

    def note_reply_foul(self, reply_text: str) -> None:
        """她的回复过黑名单 → temper_foul 留痕（下轮注入点破）。

        挂点：update_after_turn（assistant_msg 现成）。只留痕不碰参数——
        脏字不是情绪事件，是言行过线。fail-open。
        """
        try:
            if not _temper_enabled() or not _is_abusive(reply_text or ""):
                return
            now_str = datetime.now().isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description,"
                    " conversation_id, event_type, event_summary, narrative,"
                    " salience, origin) VALUES (?, ?, 'temper_foul', '{}', ?,"
                    " NULL, 'experience', '话说重了', '', 0.5, 'system')",
                    (str(uuid.uuid4()), now_str, (reply_text or "")[:80]),
                )
        except Exception as e:
            logger.debug("note_reply_foul failed: %s", e)

    # ------------------------------------------------------------------
    # v3 §5：叙事召回（experience > observation，salience 次级键）
    # ------------------------------------------------------------------
    def recent_narratives(
        self, since_hours: float = 24.0 * 30, limit: int = 3
    ) -> list[str]:
        """最近叙事摘要（emotion_log.narrative），经历在前、观察在后，同级按 salience。

        fail-open → []。narrative 为空的老行不召回（不硬凑）。
        """
        try:
            since = (datetime.now() - timedelta(hours=max(0.0, since_hours))) \
                .isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT narrative, event_type, salience FROM emotion_log "
                    "WHERE ts >= ? AND narrative != '' "
                    "ORDER BY CASE event_type WHEN 'experience' THEN 0 "
                    "       WHEN 'observation' THEN 1 ELSE 2 END, "
                    "salience DESC, ts DESC LIMIT ?",
                    (since, max(1, int(limit))),
                ).fetchall()
            out: list[str] = []
            prev = None
            for r in rows:
                t = (r[0] or "").strip()
                if not t or t == prev:
                    continue
                out.append(t)
                prev = t
            return out
        except Exception as e:
            logger.warning("recent_narratives failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # v3 §8：trait 种子（salience 加权；不注入她的上下文——§8.4 红线）
    # ------------------------------------------------------------------
    def _traits_path(self) -> Path:
        return self._db.parent / "personality_traits.json"

    def load_traits(self) -> dict:
        """读 trait 种子（萧哥侧可观测；系统侧消费）。fail-open → {}。"""
        try:
            p = self._traits_path()
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return {k: float(v) for k, v in data.items()
                        if isinstance(v, (int, float))}
        except Exception:
            pass
        return {}

    def _save_traits(self, traits: dict) -> None:
        try:
            self._traits_path().write_text(
                json.dumps(traits, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning("save traits failed: %s", e)

    def _update_traits(self, narrative: str, event_type: str,
                       deltas: dict, salience) -> None:
        """从经历叙事中累积 trait 种子，按 salience 加权。fail-open。

        **当前挂起**（TRAITS_EVOLUTION_SUSPENDED，2026-10-16 萧盛拍板）：
        早退不落盘，personality_traits.json 保留作历史证据（不再增长）。
        挂起原因与重启条件见 TRAITS_EVOLUTION_SUSPENDED 处的注释。
        """
        if TRAITS_EVOLUTION_SUSPENDED:
            return
        try:
            s = _clamp_salience(salience)
            traits = self.load_traits()
            base = {k: traits.get(k, 50.0) for k in
                    ("resilience", "openness", "empathy_depth", "self_awareness")}
            if "克制的难过" in narrative and deltas.get("cortisol", 0) > 0:
                base["resilience"] += 0.5 * s
            if "被看见的踏实" in narrative:
                base["openness"] += 0.3 * s
                base["empathy_depth"] += 0.25 * s
            if "怅然若失" in narrative:
                base["empathy_depth"] += 0.25 * s
            if event_type == "experience":
                base["self_awareness"] += 0.2 * s
            self._save_traits({k: min(100.0, v) for k, v in base.items()})
        except Exception:
            pass

    def decay_step(self, now: datetime | None = None) -> None:
        """每 5min 衰减：各激素向基线指数衰减（纯计算，不花 token）。"""
        now = now or datetime.now()
        try:
            with self._lock:
                self._do_decay_step(now)
        except Exception as e:
            logger.warning("emotion decay skipped: %s", e)
        # 二期 · 等待 tick：爬坡/lapsed/system 对账（锁外，fail-open 自包裹）
        try:
            self.pending_tick(now)
        except Exception as e:
            logger.debug("pending_tick skipped: %s", e)

    def _do_decay_step(self, now: datetime) -> None:
        old = self._get_state_unlocked()
        if not old.get("updated_at"):
            return

        try:
            last = datetime.fromisoformat(old["updated_at"])
        except (ValueError, TypeError):
            return
        elapsed = max(0.0, (now - last).total_seconds())
        if elapsed < 1.0:
            return

        chat_interval = elapsed
        new_values = {}
        for k in HORMONE_KEYS:
            half_life = HALF_LIVES[k]
            if k == "oxytocin" and chat_interval > 3600:
                effective_hl = half_life * max(0.3, 3600.0 / chat_interval)
            else:
                effective_hl = half_life
            decay_factor = math.exp(-0.693 * elapsed / effective_hl)
            new_values[k] = _clamp(
                BASELINES[k] + (old[k] - BASELINES[k]) * decay_factor
            )

        now_str = now.isoformat(timespec="seconds")
        with _connect(self._db) as conn:
            sets = ", ".join(f"{k}=?" for k in HORMONE_KEYS)
            conn.execute(
                f"UPDATE emotion_state SET {sets}, updated_at=? WHERE key='self'",
                tuple(new_values[k] for k in HORMONE_KEYS) + (now_str,),
            )
            deltas = {k: round(new_values[k] - old[k], 2) for k in HORMONE_KEYS}
            if any(abs(v) > 0.01 for v in deltas.values()):
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description, conversation_id) "
                    "VALUES (?, ?, 'time_decay', ?, ?, ?)",
                    (str(uuid.uuid4()), now_str,
                     json.dumps(deltas, ensure_ascii=False),
                     old.get("description", ""), None),
                )
            # 日志卫生（2026-10-16 巡审 P0）：裁旧 time_decay 行，每天最多一次。
            self._prune_emotion_log(conn, now)

    def _prune_emotion_log(self, conn, now: datetime) -> None:
        """裁掉 14 天前的 time_decay 低价值行（每天最多一次，fail-open）。

        decay 几乎每拍写一条 time_decay，表无界增长（巡审 P0）。只裁
        source='time_decay'；experience / echo 行是「情绪来路」，绝不裁
        （maybe_echo 依赖 7 天内 experience 的 narrative）。日期文件守卫
        ``.emotion_log_pruned`` 记当天日期，避免每拍都跑一次 DELETE。
        fail-open：裁剪任何异常都不影响 decay 主流程。
        """
        guard = self._db.parent / ".emotion_log_pruned"
        today = now.strftime("%Y-%m-%d")
        try:
            if guard.exists() and guard.read_text(encoding="utf-8").strip() == today:
                return
        except OSError:
            pass
        try:
            cutoff = (now - timedelta(days=EMOTION_LOG_DECAY_KEEP_DAYS)).isoformat(
                timespec="seconds")
            conn.execute(
                "DELETE FROM emotion_log WHERE source='time_decay' AND ts < ?",
                (cutoff,),
            )
            guard.write_text(today, encoding="utf-8")
        except Exception as e:  # fail-open
            logger.debug("prune emotion_log skipped: %s", e)

    def _maybe_trigger_self_expression(self, deltas: dict, now: datetime) -> None:
        """检查是否有激素显著变化 → 写 pending 标记（供 tick 消费）。

        2h 硬冷却：cooldown 文件存在且未过期 → 不写 pending。
        """
        if not any(abs(deltas.get(k, 0)) >= SELF_EXPRESSION_DELTA_THRESHOLD for k in HORMONE_KEYS):
            return
        cooldown_path = self._db.parent / ".emotion_self_expression_cooldown"
        try:
            if cooldown_path.exists():
                last_ts = float(cooldown_path.read_text().strip())
                if (now.timestamp() - last_ts) < SELF_EXPRESSION_COOLDOWN_SEC:
                    return
        except (ValueError, OSError):
            pass
        pending_path = self._db.parent / ".emotion_self_expression_pending"
        try:
            pending_path.write_text(now.isoformat(timespec="seconds"))
        except OSError:
            pass

    def get_self_expression_pending(self) -> bool:
        """检查是否有待表达的 self_expression（供 tick 消费）。

        pending 文件存在 且 cooldown 已过期 → True（tick 应强制发起 self_expression）。
        """
        pending_path = self._db.parent / ".emotion_self_expression_pending"
        if not pending_path.exists():
            return False
        cooldown_path = self._db.parent / ".emotion_self_expression_cooldown"
        try:
            if cooldown_path.exists():
                last_ts = float(cooldown_path.read_text().strip())
                if (time.time() - last_ts) < SELF_EXPRESSION_COOLDOWN_SEC:
                    return False
        except (ValueError, OSError):
            pass
        return True

    def consume_self_expression_pending(self) -> None:
        """消费 pending：清 pending 文件 + 写 cooldown（2h 内不重复发起）。"""
        pending_path = self._db.parent / ".emotion_self_expression_pending"
        cooldown_path = self._db.parent / ".emotion_self_expression_cooldown"
        try:
            pending_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            cooldown_path.write_text(str(time.time()))
        except OSError:
            pass


def _parse_emotion_response(raw: str) -> dict | None:
    """解析 LLM 返回的 JSON。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        depth = 0
        start_idx = -1
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    raw = raw[start_idx:i + 1]
                    break
        else:
            return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
