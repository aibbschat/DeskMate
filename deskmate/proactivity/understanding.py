"""P1（2026-10-06）：understanding 后处理 —— 统一加工主动台账项的理解。

§5.2：对 ``understanding`` 为空的 pending 条目，小桔基于证据/记忆/世界观产出解读写回；
任何来源（cloud_audit / 日后 mobile_voice）留 ``understanding=None`` 即自动补 —— 管道统一加工，
而非各 producer 各自内联（内联耦合 judge、不利多来源复用）。
§11.5：加工完成前不递（``ledger.build_handoff_opening`` 守卫跳过）；加工失败降级递并标
``understanding_failed``（不当一等公民异常，降级递 content+evidence 并提示）。

设计：统一后处理（非内联）。函数 :func:`maybe_backfill_understanding` 由 ``tick`` 主循环按冷却周期
调用，仅在有「空 understanding 的 pending」时才调 LLM；fail-open，绝不阻断主动推理主流程。
LLM 复用 ``deskmate.memory.llm``（与 judge 同凭证、模型由 prefs.json 控制）。

§5.2 上下文接入（本期补齐）：理解不再只凭 ``content``/``evidence`` 现编，而是接住
三路 fail-open 上下文——她的 **worldview**（profile_items layer='worldview'）、
跨 ``history`` 库的 **相关记忆**（FTS5 检索）、以及项目级 **人物圣经**
（唯一项目才读、含糊不猜）——让「她的理解」真正像她、且基于事实（反现编红线）。
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
from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 10 分钟内最多跑一次（LLM 成本）；单次最多加工条数（避免长阻塞 tick）。
_BACKFILL_COOLDOWN_S = 10 * 60
_BACKFILL_BATCH = 3
# failed 标记的重试窗（修⑦）：超过此时长才允许再烧一轮 LLM。
# 设计文档 §11.12 修⑦ 写的是「上次失败距今 ≥ 60min 才重试」，这里此前误写成
# 24*3600（2026-10-06 巡检发现）⇒ 火山 Coding Plan 限额通常 1~2 小时就恢复，
# 而 failed 标记要等 24h 才翻身 = 降级文案钉住一整天，正是「绑死单一 provider」
# 的真实伤害（额度恢复了也不翻）。按文档口径回正为 60min。
_FAILED_RETRY_COOLDOWN_S = 60 * 60

# 场景职责（不含人格——身份与立场由 for_scene 底座提供，2026-11-16 靶子③）。
# 「不替他下结论」是职责边界（她要给自己的判断，不是替他做决定），保留。
_SYSTEM_PROMPT = for_scene(
    """下面是一条你主动发现/记下、还没跟他说的事。请用你自己的口吻，产出「我的理解」：
- 这条事对他意味着什么（为什么值得他注意）；
- 如果要处理，可以往哪个方向走（但不替他下结论）；
- 控制在 120 字以内，口语、像你平时跟他说话；
- 必须基于他给的证据来说，不要编造；拿不准就说拿不准；
- 若下方「你已有的上下文」里提供了你的世界观 / 相关记忆 / 人物圣经，据此深化理解、
  让话真正像你，但只基于有依据的事实，不要编造没出现的细节（反现编红线）。
"""
)

# 修⑦：按来源补一段说明——加工口吻随来源走（mobile_voice 是灵感不是问题）。
_SOURCE_GUIDE = {
    "cloud_audit": "这条是你替他盯稿子时发现的隐患（伏笔/人物/时间线/史实）。",
    "mobile_voice": "这条是他在手机上随手丢的灵感或话题（他自己的原话，不是问题）——"
    "理解要往「这个灵感可以怎么延伸、和他正在写的东西怎么对上」走。",
    "automation": "这条是定时任务/系统自己报告的异常（没跑成、没产出）。",
    "sync_health": "这条是跨端同步的健康异常（漂移/窗口失败）。",
    "observation": "这条是你观察世界记录下的信号/趋势。",
    "reading": "这条是你读书/史料消化后的结论。",
    "writing": "这条是她盯着你《长编》写作进度发现的（停笔位点）——理解要往「是不是卡住了、"
    "要不要聊聊」走，别当成系统报错。",
}

# §5.2 后处理「基于记忆层/世界观/人物圣经」——理解不是凭空编，要接住她已有的
# 看法与记忆。三路上下文均 fail-open：任一缺失/异常都不阻断理解加工（与整条后处理同范式）。
_MEMORY_TOP_N = 3


def _gather_worldview(base_dir) -> str:
    """她的世界观/价值观（全局 profile_items layer='worldview'）。

    理解要「像她」，这一层是她自己对世界/写作的看法，最该被接住。fail-open。
    """
    try:
        from deskmate.agency.behaviors.worldview_reflection import load_worldview_beliefs

        return load_worldview_beliefs(str(base_dir) if base_dir else None) or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 worldview 失败（跳过）: %s", e)
        return ""


def _gather_memory(base_dir, item: dict) -> str:
    """相关记忆检索（全局 history 库 FTS5，fail-open）。

    用条目正文/证据做关键词，跨 ``<base>/history/*.db`` 取最相关片段，让理解接住她
    「记得的关于这事的事」，而非只凭 content 现编。也间接覆盖已摄入书稿圣经
    （若书稿经私人/史料通道被摄入 history 库）。
    """
    try:
        from pathlib import Path as _P

        from coworker.history.fts5 import search_across_dbs

        hist_dir = _P(base_dir) / "history"
        if not hist_dir.is_dir():
            return ""
        db_paths = [str(p) for p in hist_dir.glob("*.db") if p.is_file()]
        if not db_paths:
            return ""
        query = " ".join(
            str(item.get(k) or "") for k in ("content", "evidence")
        ).strip()
        if not query:
            return ""
        rows = search_across_dbs(db_paths, query, limit=_MEMORY_TOP_N)
        snippets = [r.get("snippet") or "" for r in rows if (r.get("snippet") or "").strip()]
        snippets = snippets[:_MEMORY_TOP_N]
        return "\n".join(f"- {s}" for s in snippets)
    except Exception as e:  # noqa: BLE001
        logger.debug("相关记忆检索失败（跳过）: %s", e)
        return ""


def _gather_bible(base_dir) -> str:
    """人物圣经（项目级，fail-open）：仅当用户有且仅有一个含圣经的项目才读，含糊则跳过。

    圣经是项目级资产（``<项目>/.deskmate/{CHARACTERS,OUTLINE,TIMELINE,NOTES}.md``），
    而本后处理跑在全局 ``~/.deskmate``；当前 ``active_project`` 应用层未接线，故用
    「唯一项目」启发式解析——多项目/无项目都不猜（不造脆弱项目解析器，呼应
    「根治优于 workaround」）。接住圣经能让 cloud_audit（盯稿子）类理解真正基于
    人物/情节设定，而非泛泛而谈。
    """
    try:
        from pathlib import Path as _P

        from deskmate.proactivity.context_builder import get_bible_from_workspace

        projects = _P(base_dir) / "projects"
        if not projects.is_dir():
            return ""
        hits = []
        for p in sorted(projects.iterdir()):
            if not p.is_dir():
                continue
            bible = get_bible_from_workspace(str(p))
            if bible.strip():
                hits.append((p.name, bible))
        if len(hits) != 1:
            return ""  # 零个/多个 → 不猜，避免错绑到错误的书
        return hits[0][1]
    except Exception as e:  # noqa: BLE001
        logger.debug("人物圣经解析失败（跳过）: %s", e)
        return ""


def _gather_user_facts(base_dir, item: dict) -> str:
    """她记着的「关于用户的事实」+ 近期日记软规律（fail-open）。

    修复（2026-10-08 萧哥报）：小桔库里其实知道姑射图是国子监第三章、节假日不写稿，
    但主动卡片的「理解」LLM 拿不到——learned_history 用户事实与日记此前都没接进
    ``_gather_grounding``。这里接上，让理解真正基于她已掌握的关于用户的事实。

    写作类卡片（如「六天没动笔」）特意补足写作习惯/假期关键词，让理解能接住
    「节假日不写稿」这类软规律，而不是把停笔误判成「卡住了」。
    """
    try:
        from deskmate.memory.user_fact_context import build_user_fact_context

        query = " ".join(
            str(item.get(k) or "") for k in ("content", "evidence")
        ).strip()
        # 写作类卡片主动补写作习惯/假期词，避免停笔被误判成卡住
        if (item.get("source") == "writing") or any(
            k in query for k in ("动笔", "写稿", "没写", "停笔", "写作", "没动")
        ):
            query = (query + " 写 稿 不写 假期 放假 休息 国庆 平时 习惯").strip()
        return build_user_fact_context(base_dir, query=query) or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("用户事实召回失败（跳过）: %s", e)
        return ""


def _gather_grounding(base_dir, item: dict) -> str:
    """汇总上下文（世界观 / 记忆 / 圣经 / 关于用户的事实+日记）为一段文本，供理解 prompt 参考。"""
    blocks = []
    wv = _gather_worldview(base_dir)
    if wv.strip():
        blocks.append("### 她的世界观（她自己对世界/写作的看法）\n" + wv.strip())
    mem = _gather_memory(base_dir, item)
    if mem.strip():
        blocks.append("### 相关记忆（她记得的、与这事有关的内容）\n" + mem.strip())
    bible = _gather_bible(base_dir)
    if bible.strip():
        blocks.append(bible.strip())
    uf = _gather_user_facts(base_dir, item)
    if uf.strip():
        blocks.append(uf.strip())
    return "\n\n".join(blocks)


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
        # 空内容行（content='')无加工价值，且补上 understanding 后会混过递送守卫递出空卡
        # （2026-10-07 实锤：cloud_audit:no_action）——直接跳过，不浪费 LLM。
        rows = conn.execute(
            f"SELECT id, kind, content, evidence, source FROM {LEDGER_TABLE} "
            f"WHERE status='pending' AND (understanding IS NULL OR understanding='') "
            f"AND content IS NOT NULL AND trim(content) <> '' "
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
                    f"AND content IS NOT NULL AND trim(content) <> '' "
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
            # §5.2：把 worldview/记忆/圣经 接进理解（反凭空编）；fail-open，缺则跳过。
            grounding = _gather_grounding(
                base, {"content": content, "evidence": evidence}
            )
            if grounding:
                prompt += (
                    "\n【你已有的上下文（供参考，据此深化理解，别脱离事实编造）】\n"
                )
                prompt += f"{grounding}\n"
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
