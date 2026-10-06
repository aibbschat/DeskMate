"""P1（2026-10-06）：understanding 后处理 —— 统一加工主动台账项的理解。

§5.2：对 ``understanding`` 为空的 pending 条目，小桔基于证据/记忆/世界观产出解读写回；
任何来源（cloud_audit / 日后 mobile_voice）留 ``understanding=None`` 即自动补 —— 管道统一加工，
而非各 producer 各自内联（内联耦合 judge、不利多来源复用）。
§11.5：加工完成前不递（``ledger.build_handoff_opening`` 守卫跳过）；加工失败降级递并标
``understanding_failed``（不当一等公民异常，降级递 content+evidence 并提示）。

设计：统一后处理（非内联）。函数 :func:`maybe_backfill_understanding` 由 ``tick`` 主循环按冷却周期
调用，仅在有「空 understanding 的 pending」时才调 LLM；fail-open，绝不阻断主动推理主流程。
LLM 复用 ``deskmate.memory.llm``（与 judge 同凭证、模型由 prefs.json 控制）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from deskmate.proactivity.ledger import (
    LEDGER_TABLE,
    UNDERSTANDING_FAILED_MARKER,
    _resolve_base,
)

logger = logging.getLogger(__name__)

# 10 分钟内最多跑一次（LLM 成本）；单次最多加工条数（避免长阻塞 tick）。
_BACKFILL_COOLDOWN_S = 10 * 60
_BACKFILL_BATCH = 3
# failed 标记的重试窗（修⑦）：超过此时长才允许再烧一轮 LLM（火山限额等瞬时失败
# 不该把一条永久钉在降级文案上；§11.5 降级是一等公民，但不该永不翻身）。
_FAILED_RETRY_COOLDOWN_S = 24 * 3600

_SYSTEM_PROMPT = """你是小桔，萧哥（历史小说作家）的创作搭档与桌面 AI 助手。
下面是一条你主动发现/记下、还没跟他说的事。请用你自己的口吻，产出「我的理解」：
- 这条事对他意味着什么（为什么值得他注意）；
- 如果要处理，可以往哪个方向走（但不替他下结论）；
- 控制在 120 字以内，口语、像你平时跟他说话；
- 必须基于他给的证据来说，不要编造；拿不准就说拿不准。
"""

# 修⑦：按来源补一段说明——加工口吻随来源走（mobile_voice 是灵感不是问题）。
_SOURCE_GUIDE = {
    "cloud_audit": "这条是你替他盯稿子时发现的隐患（伏笔/人物/时间线/史实）。",
    "mobile_voice": "这条是他在手机上随手丢的灵感或话题（他自己的原话，不是问题）——"
    "理解要往「这个灵感可以怎么延伸、和他正在写的东西怎么对上」走。",
    "automation": "这条是定时任务/系统自己报告的异常（没跑成、没产出）。",
    "sync_health": "这条是跨端同步的健康异常（漂移/窗口失败）。",
    "observation": "这条是你观察世界记录下的信号/趋势。",
    "reading": "这条是你读书/史料消化后的结论。",
}


def _llm_available() -> bool:
    """LLM 是否可用（有 key 且 provider 可达）。lazy import，避免模块加载副作用。"""
    try:
        from deskmate.memory.llm import llm_available

        return bool(llm_available())
    except Exception:  # noqa: BLE001
        return False


def _call_llm(user_prompt: str, system: str = _SYSTEM_PROMPT) -> Optional[str]:
    """调 LLM 产出理解；无 key / 调用失败返回 None。复用 deskmate.memory.llm.chat。"""
    try:
        from deskmate.memory.llm import chat, llm_available

        if not llm_available():
            return None
        out = chat(
            system=system,
            user=user_prompt,
            temperature=0.4,
            max_tokens=400,
        )
        return (out or "").strip() or None
    except Exception as e:  # noqa: BLE001
        logger.debug("understanding LLM 调用失败: %s", e)
        return None


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        r = conn.execute(
            "SELECT value FROM proactivity_meta WHERE key=?", (key,)
        ).fetchone()
        return r[0] if r else None
    except Exception:  # noqa: BLE001
        return None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO proactivity_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def maybe_backfill_understanding(
    base_dir: Optional[Path] = None, *, limit: int = _BACKFILL_BATCH
) -> int:
    """扫描 ``pending`` 且 ``understanding`` 为空的条目，逐个调 LLM 产出理解写回。

    返回本次成功加工条数（不含降级标 ``failed`` 的）。fail-open，异常返回 0。

    守卫：
    - LLM 不可用（无 key）→ **不标 failed**、留空等下轮（避免永久失败）；
    - 有 key 但调用失败 → 标 ``UNDERSTANDING_FAILED_MARKER`` 降级递（§11.5）；
    - 冷却期内（10min）直接跳过，避免每轮 tick 都烧 LLM。
    冷却时间戳存本地 ``proactivity_meta`` 表（非同步表，本端簿记）。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    conn = None
    try:
        conn = sqlite3.connect(str(db), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proactivity_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        # 冷却：避免每轮 tick 都烧 LLM
        now = time.time()
        last = _meta_get(conn, "last_understanding_backfill_at")
        if last and (now - float(last)) < _BACKFILL_COOLDOWN_S:
            return 0
        rows = conn.execute(
            f"SELECT id, kind, content, evidence, source FROM {LEDGER_TABLE} "
            f"WHERE status='pending' AND (understanding IS NULL OR understanding='') "
            f"ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            # failed 重试窗（修⑦，见 _FAILED_RETRY_COOLDOWN_S 注释）
            last_fail = _meta_get(conn, "last_failed_retry_at")
            if not last_fail or (now - float(last_fail)) >= _FAILED_RETRY_COOLDOWN_S:
                rows = conn.execute(
                    f"SELECT id, kind, content, evidence, source FROM {LEDGER_TABLE} "
                    f"WHERE status='pending' AND understanding = ? "
                    f"ORDER BY created_at ASC LIMIT ?",
                    (UNDERSTANDING_FAILED_MARKER, limit),
                ).fetchall()
                if rows:
                    _meta_set(conn, "last_failed_retry_at", str(now))
        if not rows:
            _meta_set(conn, "last_understanding_backfill_at", str(now))
            conn.commit()
            return 0
        # 仅当 LLM 可用才加工；不可用则留空等下轮（不标 failed）
        if not _llm_available():
            return 0
        done = 0
        for rid, kind, content, evidence, source in rows:
            prompt = f"【发现类型】{kind}\n【事项】{content}\n"
            if evidence:
                prompt += f"【证据】{evidence}\n"
            prompt += "请产出你的理解（120 字以内）："
            guide = _SOURCE_GUIDE.get(source or "", "")
            system = _SYSTEM_PROMPT + (f"\n【来源说明】{guide}\n" if guide else "")
            understanding = _call_llm(prompt, system=system)
            if understanding:
                conn.execute(
                    f"UPDATE {LEDGER_TABLE} SET understanding=? WHERE id=?",
                    (understanding, rid),
                )
                done += 1
            else:
                # 有 key 但调用失败 → 降级递（§11.5）
                conn.execute(
                    f"UPDATE {LEDGER_TABLE} SET understanding=? WHERE id=?",
                    (UNDERSTANDING_FAILED_MARKER, rid),
                )
            conn.commit()
        _meta_set(conn, "last_understanding_backfill_at", str(now))
        return done
    except Exception as e:  # noqa: BLE001
        logger.debug("understanding 后处理失败（不阻断）: %s", e, exc_info=True)
        return 0
    finally:
        if conn is not None:
            conn.close()
