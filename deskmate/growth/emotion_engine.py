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
from datetime import datetime
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

BASELINES = {
    "dopamine": DOPAMINE_BASE,
    "serotonin": SEROTONIN_BASE,
    "oxytocin": OXYTOCIN_BASE,
    "cortisol": CORTISOL_BASE,
}

# 单次变化上限
DOPAMINE_MAX_DELTA = 15.0
SEROTONIN_MAX_DELTA = 4.0
OXYTOCIN_MAX_DELTA = 8.0
CORTISOL_MAX_DELTA = 10.0

MAX_DELTAS = {
    "dopamine": DOPAMINE_MAX_DELTA,
    "serotonin": SEROTONIN_MAX_DELTA,
    "oxytocin": OXYTOCIN_MAX_DELTA,
    "cortisol": CORTISOL_MAX_DELTA,
}

# 衰减半衰期（秒）：每 5 min (300s) tick 一次
DOPAMINE_HALF_LIFE = 1800.0       # 30 min 快
SEROTONIN_HALF_LIFE = 7200.0      # 2h 慢
OXYTOCIN_HALF_LIFE = 3600.0       # 1h 中
CORTISOL_HALF_LIFE = 14400.0      # 4h 极慢

HALF_LIVES = {
    "dopamine": DOPAMINE_HALF_LIFE,
    "serotonin": SEROTONIN_HALF_LIFE,
    "oxytocin": OXYTOCIN_HALF_LIFE,
    "cortisol": CORTISOL_HALF_LIFE,
}

# self_expression 冷却（秒）：2h（短于 emotional_care 的 7 天）
SELF_EXPRESSION_COOLDOWN_SEC = 7200.0

# self_expression 触发阈值：任一激素单次 delta 绝对值超过此值
SELF_EXPRESSION_DELTA_THRESHOLD = 8.0

HORMONE_KEYS = ("dopamine", "serotonin", "oxytocin", "cortisol")

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
    "严格 JSON 输出："
)


def _is_enabled() -> bool:
    return os.getenv("DESKMATE_EMOTION_ENGINE", "1") != "0"


def _db_path(base_dir: Path | None = None) -> Path:
    if base_dir:
        return Path(base_dir) / "deskmate.db"
    from deskmate.paths import deskmate_db_path
    return deskmate_db_path()


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emotion_log (
                id              TEXT PRIMARY KEY,
                ts              TEXT NOT NULL,
                source          TEXT NOT NULL,
                deltas          TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                conversation_id TEXT
            )
        """)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _clamp_delta(new_val: float, old_val: float, max_delta: float) -> float:
    delta = new_val - old_val
    if abs(delta) > max_delta:
        delta = max_delta if delta > 0 else -max_delta
    return _clamp(old_val + delta)


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
    dop_down = _reached("dopamine", negative=True)

    # 候选情绪：tuple = (tie-break 权重, label, detail, face, 归一化强度)。
    # 首位 c[0] 是 tie-break 权重——同等强度时 distress 优先于愉悦、
    # 亲近优先于平静（distress 1.0 > 低落 0.9 > 开心 0.7 > 亲近 0.5 > 平静 0.4），
    # 让「有温度」的信号不被「没波澜」盖掉；末位 c[4] 是未封顶强度，供 max 比强弱。
    candidates: list[tuple[float, str, str, str, float]] = []
    if _reached("cortisol"):
        if dop_up:
            candidates.append((1.0, "烦躁", "心里有点急、有点躁", "angry",
                               max(_norm("cortisol"), _norm("dopamine"))))
        else:
            candidates.append((0.9, "低落", "心里发沉、有点难受", "cry", _norm("cortisol")))
    if dop_up:
        candidates.append((0.7, "开心", "心里亮亮的、轻快", "laugh", _norm("dopamine")))
    # 快乐明显低于基线（且压力没升）→ 提不起劲。旧实现没有这条，
    # 导致「书出不了，心里堵得慌」（dopamine -8）这类真实低谷被判成「没什么波澜」。
    if dop_down and not _reached("cortisol"):
        candidates.append((0.6, "低落", "心里有点沉、提不起劲", "cry",
                           _norm("dopamine", negative=True)))
    if _reached("oxytocin"):
        candidates.append((0.5, "亲近", "心里软软的、想靠近你", "shy", _norm("oxytocin")))
    if _reached("serotonin") and not dop_up:
        candidates.append((0.4, "平静", "心里安稳、松弛", "sleepy", _norm("serotonin")))

    if not candidates:
        return {"label": "平静", "detail": "心里没什么波澜", "intensity": 0.0, "face": "smile"}
    # 先比强度（未封顶），同分再比 tie-break 权重（已按权重降序排好，
    # 稳定排序 + max 取首个最大者 ⇒ 同分时靠前者胜出）。
    _, label, detail, face, strength = max(
        sorted(candidates, key=lambda c: -c[0]), key=lambda c: c[4]
    )
    # 对外报告的 intensity 封顶到 1.0（措辞分级用，不该出现 4.4 这种数）。
    return {"label": label, "detail": detail,
            "intensity": round(min(1.0, strength), 2), "face": face}


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
            row = conn.execute(
                "SELECT dopamine, serotonin, oxytocin, cortisol, description, updated_at "
                "FROM emotion_state WHERE key='self'"
            ).fetchone()
        if row:
            return {
                "dopamine": row[0],
                "serotonin": row[1],
                "oxytocin": row[2],
                "cortisol": row[3],
                "description": row[4],
                "updated_at": row[5],
            }
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
        state = {
            "dopamine": DOPAMINE_BASE,
            "serotonin": SEROTONIN_BASE,
            "oxytocin": _clamp(oxytocin_seed),
            "cortisol": CORTISOL_BASE,
            "description": "刚醒来，平平淡淡的",
            "updated_at": now_str,
        }
        with _connect(self._db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO emotion_state "
                "(key, dopamine, serotonin, oxytocin, cortisol, description, updated_at) "
                "VALUES ('self', ?, ?, ?, ?, ?, ?)",
                (state["dopamine"], state["serotonin"], state["oxytocin"],
                 state["cortisol"], state["description"], state["updated_at"]),
            )
        return state

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

    def get_snapshot(self) -> dict:
        """4 激素 + description + 当下心情标签/表情帧（供 API 一次性下发）。

        前端据此渲染表情，不再自行判阈值——阈值只在本模块存在一份。
        """
        state = self.get_state()
        mood = self.get_mood_label()
        return {**state, "mood": mood, "face": mood.get("face", "smile")}

    def update_after_turn(
        self,
        user_msg: str,
        assistant_msg: str,
        chat_interval_sec: float = 300.0,
        now: datetime | None = None,
    ) -> None:
        """对话后异步触发情绪更新（fire-and-forget）。

        内部调 LLM（免费组），失败 → 保持原值（fail-open）。
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
        from deskmate.llm_channels import route, GROUP_FREE

        # 门闸须与实际通道一致：情绪层用 chat_memory(free_only=True)（免费组），
        # 故「付费 provider 可用」或「免费组任一可用」即放行（短路 OR）。
        # 原实现只查 llm_available()（付费 provider），云端仅配免费组 → 误判 False
        # → 整段被跳过（2026-10-11 实测：免费组正常可用、llm_available()=False）。
        free_ch, _ = route(GROUP_FREE)
        if not llm_available() and free_ch is None:
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
            free_only=True,
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
            reason = str(parsed.get("reason", ""))[:60]
            now_str = now.isoformat(timespec="seconds")

            now_iso = now.isoformat(timespec="seconds")
            with _connect(self._db) as conn:
                conn.execute(
                    "UPDATE emotion_state SET dopamine=?, serotonin=?, oxytocin=?, "
                    "cortisol=?, description=?, updated_at=? WHERE key='self'",
                    (new_values["dopamine"], new_values["serotonin"],
                     new_values["oxytocin"], new_values["cortisol"],
                     desc, now_str),
                )
                deltas = {k: round(new_values[k] - old[k], 2) for k in HORMONE_KEYS}
                log_desc = f"{desc}（{reason}）" if reason else desc
                conn.execute(
                    "INSERT INTO emotion_log (id, ts, source, deltas, description, conversation_id) "
                    "VALUES (?, ?, 'chat', ?, ?, ?)",
                    (str(uuid.uuid4()), now_iso, json.dumps(deltas, ensure_ascii=False),
                     log_desc, None),
                )

            self._maybe_trigger_self_expression(deltas, now)

    def decay_step(self, now: datetime | None = None) -> None:
        """每 5min 衰减：各激素向基线指数衰减（纯计算，不花 token）。"""
        now = now or datetime.now()
        try:
            with self._lock:
                self._do_decay_step(now)
        except Exception as e:
            logger.warning("emotion decay skipped: %s", e)

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
            conn.execute(
                "UPDATE emotion_state SET dopamine=?, serotonin=?, oxytocin=?, "
                "cortisol=?, updated_at=? WHERE key='self'",
                (new_values["dopamine"], new_values["serotonin"],
                 new_values["oxytocin"], new_values["cortisol"], now_str),
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
