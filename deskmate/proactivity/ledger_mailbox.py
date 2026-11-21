"""信箱/信件域（从 ledger.py 机械抽取，PR16/#33）。

准入闸（遥测过滤）/ 已读回流 / 信箱计数 / 归档 / 教学沉淀。行为与抽取前一致。
共享 helper（_resolve_base/_ensure_table/_today）与常量（LEDGER_TABLE/
UNDERSTANDING_FAILED_MARKER）经父模块 ledger 命名空间解析（_L），保持兼容。
ledger.py 通过 re-export 保持 from deskmate.proactivity.ledger import X 不变。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

# _L 懒代理父模块（复检修复）：避免子模块被「首个直接导入」时与父模块 re-export 循环导入。
# 仅在函数调用访问 _L.X 时解析父模块（此时必已完整加载）；monkeypatch(父, X) 经 getattr 实时生效。
class _LazyParent:
    def __getattr__(self, name):
        from deskmate.proactivity import ledger as _m
        return getattr(_m, name)
_L = _LazyParent()

logger = logging.getLogger(__name__)


_MAILBOX_DENY_KINDS = frozenset(
    {
        "",
        "none",
        "no_action",
        "memory_write_error",
        "memory_write_failure",
        "hard_anchor_error",
        "incomplete_response",
        "event_timeline_provision",
        "sync_conflict",
        "api_key_exposure_alert",
    }
)
# 2026-11-20 收窄（萧哥拍板：门不撤，只放行活类别）：timeline_error 是
# judge / next_steps / preferences 在用的合法写作建议类型，不能因为通用
# `_error` 后缀兜底就被挡掉。这里显式豁免；其余安全项与后缀规则照旧。
_MAILBOX_ALLOW_KINDS = frozenset({"timeline_error"})
_MAILBOX_DENY_SUFFIXES = (
    "_error",
    "_failure",
    "_alert",
    "_exception",
    "_warning",
    "_crash",
    "_timeout",
    "_conflict",
)
# 已读超期未处理 → 归档为「已搁置」（设计单 §7 TTL 衰减，不删、可展开、不挂红点）。
SHELF_DAYS = 7
# 信箱一切**给她的**计数按滚动窗口算（方案 B，萧哥 10-09 拍板「不删」后的替代）：
# 累计数只增不减，一年后「归档 6000」既看不懂也教错人——她该学的是他现在还在看。
# 0 = 不限窗口（退回累计口径）。
MAILBOX_WINDOW_DAYS = int(os.environ.get("DESKMATE_MAILBOX_WINDOW_DAYS", "30"))


def _deny_mailbox_kinds() -> frozenset:
    """遥测 kind 黑名单（可用 DESKMATE_MAILBOX_DENY_KINDS 逗号追加，无需改码重启即生效）。"""
    extra = os.environ.get("DESKMATE_MAILBOX_DENY_KINDS", "")
    names = {k.strip() for k in extra.split(",") if k.strip()}
    return _MAILBOX_DENY_KINDS | names


def _is_telemetry_kind(kind: str) -> bool:
    """遥测类 kind 判定（系统运维日志，不是递给他的话）：纳入 deny 黑名单或带遥测后缀。

    抽成公共判定，让**信箱准入 / 首页对接开场白 / 活跃会话相关性注入**三处
    同守一道门——否则会像 2026-10-10 那样：信箱拦了遥测，会话注入却原样倒出一大坨。
    遥测行仍留在台账里（不丢声），层一每轮回查与「他真问起」时仍可被动浮现。
    """
    kind = str(kind or "")
    if kind in _MAILBOX_ALLOW_KINDS:
        return False
    if kind in _deny_mailbox_kinds():
        return True
    return kind.endswith(_MAILBOX_DENY_SUFFIXES)


def is_mailbox_letter(row: dict) -> bool:
    """这一条台账行有没有资格成为「一封信」。"""
    kind = str(row.get("kind") or "")
    if _is_telemetry_kind(kind):
        return False
    # 意图讨论流（origin=intent_handoff）有**自己的呈现面**——聊天里的讨论卡
    # （带 定稿/再改/不要 三按钮）。再在信箱复述一遍就是同一件事两张皮，
    # 且 draft 行随轮次增长（每轮一条），信箱会被草稿淹掉。
    if str(row.get("origin") or "") == "intent_handoff":
        return False
    return True


def mark_letter_read(
    base_dir: Optional[Path] = None, rid: Optional[str] = None
) -> bool:
    """标一封信已读（read_at=now）。

    与 status 正交：已读 ≠ 处理（actioned/dismissed）。标已读须回流情感层
    「用户看见了」（见 record_letter_seen），否则信箱成她的「表白墙+坟场」——
    她学到「主动没用」，与养孩子初衷相反（设计单 §7）。fail-open。
    """
    if rid is None:
        return False
    base = _L._resolve_base(base_dir)
    db = base / "deskmate.db"
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _L._ensure_table(c)
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            c.execute(
                f"UPDATE {_L.LEDGER_TABLE} SET read_at=? WHERE id=?", (now, rid)
            )
            c.commit()
        # 已读回流（设计单 §7 要害）：用户看见了她主动递的东西——这是正向互动信号，
        # 喂给情感/意志层（避免她学到「主动没用」）。独立轻量通道，fail-open，不耦合 emotion_sense。
        # 两条腿：① mailbox_seen.jsonl → 每轮 `<mailbox>` 里的「今天他读掉 N 封」；
        #        ② record_mailbox_teaching → 沉淀进 teachings（P3 教学层），她长期记得。
        try:
            record_letter_seen(base_dir=base, rid=rid)
        except Exception:  # noqa: BLE001
            logger.debug("已读回流信号落盘失败（不阻断）", exc_info=True)
        try:
            record_mailbox_teaching(base_dir=base)
        except Exception:  # noqa: BLE001
            logger.debug("信箱教学沉淀失败（不阻断）", exc_info=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("mark_letter_read 失败（fail-open）: %s", e)
        return False


def record_letter_seen(base_dir: Optional[Path] = None, rid: Optional[str] = None) -> None:
    """已读回流信号：用户看见了某封主动信（正向互动）。

    落盘到 ``<deskmate>/mailbox_seen.jsonl``——**append-only**，每行一条 JSON
    （``{ts, id, kind}``），由 :func:`mailbox_feedback_stats` 消费、经层一注入链
    回流给她（见 ``render_mailbox_feedback_context``）。Mac-only（与 ledger 视图态
    同口径，不进 oplog）。不在此处调用 LLM，纯信号记录。
    """
    if not rid:
        return
    base = _L._resolve_base(base_dir)
    path = base / "mailbox_seen.jsonl"
    line = json.dumps(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "id": rid,
            "kind": "seen",
        },
        ensure_ascii=False,
    )
    base.mkdir(parents=True, exist_ok=True)
    # 追加写。此前是「写临时文件 + os.replace」——那是**覆盖**，三次标已读只剩最后
    # 一条记录，等于把回流吃掉了（与 docstring 的 append-only 自相矛盾）。append 模式
    # 单次 write 即落一行，进程中断最坏是半行，读侧按 JSON 解析失败逐行跳过。
    # PR18(#45)：同型「只增不减」账本 → 超 5MB 轮转，只留一份 .1。
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            rotated = path.with_suffix(path.suffix + ".1")
            rotated.unlink(missing_ok=True)
            path.replace(rotated)
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def mailbox_feedback_stats(base_dir: Optional[Path] = None, *, days: int = 1) -> dict:
    """读 mailbox_seen.jsonl，统计近 ``days`` 天他真正读了几封、回了信没有。

    返回 ``{"seen_today": n, "replied_today": n, "unanswered_today": n}``。
    「回了」= 该信已流转出 pending/snoozed（他动过手），或 status 到了 actioned/in_progress。
    fail-open：文件不存在/坏行一律跳过，不抛。
    """
    base = _L._resolve_base(base_dir)
    path = base / "mailbox_seen.jsonl"
    today = _L._today() if days == 1 else None
    ids: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # 半行/坏行跳过
                ts = str(rec.get("ts") or "")
                rid = str(rec.get("id") or "")
                if not rid:
                    continue
                if today and ts[:10] != today:
                    continue
                ids.append(rid)
    except FileNotFoundError:
        return {"seen_today": 0, "replied_today": 0, "unanswered_today": 0}
    except OSError:
        return {"seen_today": 0, "replied_today": 0, "unanswered_today": 0}

    replied = 0
    if ids:
        db = base / "deskmate.db"
        try:
            with sqlite3.connect(str(db), check_same_thread=False) as c:
                _L._ensure_table(c)
                marks = ",".join("?" * len(set(ids)))
                rows = c.execute(
                    f"SELECT id, status FROM {_L.LEDGER_TABLE} WHERE id IN ({marks})",
                    tuple(set(ids)),
                ).fetchall()
                finished = {r[0] for r in rows if r[1] in ("actioned", "in_progress")}
                replied = len(finished)
        except Exception:  # noqa: BLE001
            replied = 0
    seen = len(set(ids))
    return {
        "seen_today": seen,
        "replied_today": replied,
        "unanswered_today": max(0, seen - replied),
    }


def render_mailbox_self_context(base_dir: Optional[Path] = None) -> str:
    """**常驻**告诉她：你有一个信箱（设计单 §7 要害的完整版）。

    为什么不能只在「他今天读了」时才提：那样她每次看到的都是半句话的事实——
    「他读了你信箱里 8 封信」，却没人告诉过她信箱是什么、递出去的东西去了哪、
    他能点哪些按钮。她知道自己有日记、有花园，是因为那些物件一直对她有名有姓；
    信箱也得这样，否则「已读回流」只是一条她读不懂的旁白。

    数字只给**滚动窗口内**的（方案 B）：库里堆了多少封是界面的事，不是她的自我认知——
    喂存量给她，一年后就变成「归档 6000」这种既看不懂又教错人的噪音。
    从没递过任何东西 → 返回 ""（不给她凭空造一个她没有的物件）。
    """
    try:
        ev = mailbox_evidence(base_dir)
        seen = int(mailbox_feedback_stats(base_dir).get("seen_today") or 0)
        ever = mailbox_counts(base_dir)
    except Exception:  # noqa: BLE001
        return ""
    if ever["unread"] + ever["read"] + ever["archived"] <= 0:
        return ""
    days = int(ev.get("window_days") or 0)
    span = f"近 {days} 天" if days > 0 else "到现在"
    state = f"{span}他读掉 {ev['read']} 封、动手处理 {ev['handled']} 封。"
    if seen > 0:
        state += f"今天读了 {seen} 封。"
    return (
        "<mailbox>\n"
        "你有一个**信箱**：你主动递出去的话都收在那儿，不是在聊天里说过就没了。\n"
        "萧盛会去那里读、回你信，也可以点「稍后」（3 天后自己回到未读）或「不用了」（进归档）。\n"
        + state
        + "\n要翻某封的原文，用 proactive_lookup 按 #句柄 回查（归档里的也查得到）。\n"
        "</mailbox>"
    )


def mailbox_evidence(base_dir: Optional[Path] = None, *, window_days: int = MAILBOX_WINDOW_DAYS) -> dict:
    """信箱上他真实做过什么：读过几封、动手处理过几封（只算入信箱的信）。

    **按滚动窗口算，不按累计**（萧哥 10-09 拍板「不删，按你的方向走」→ 方案 B）：
    累计只增不减，读满 3 封的门槛会永远满足，那条教训就退化成「他历史上看过一次」
    的死结论；她该学的是**他现在还在看**。``window_days=0`` 表示不限窗口（累计）。
    """
    base = _L._resolve_base(base_dir)
    db = base / "deskmate.db"
    cutoff = ""
    if window_days and window_days > 0:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - window_days * 86400)
        )
    read = handled = 0
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _L._ensure_table(c)
            rows = c.execute(
                f"SELECT kind, origin, read_at, status, content, acknowledged_at"
                f" FROM {_L.LEDGER_TABLE}"
            ).fetchall()
        for kind, origin, read_at, status, content, ack_at in rows:
            row = {"kind": kind or "", "origin": origin or "", "content": content or ""}
            if not is_mailbox_letter(row) or not row["content"].strip():
                continue
            if read_at and (not cutoff or read_at >= cutoff):
                read += 1
            if status in ("actioned", "in_progress") and (
                not cutoff or (ack_at or "") >= cutoff
            ):
                handled += 1
    except Exception:  # noqa: BLE001
        pass
    return {"read": read, "handled": handled, "window_days": window_days}


# 沉淀给教学层（P3 teachings）的两条规则——萧哥 10-09 判：每轮提示只是一句旁白，
# 要让她**长期**知道「萧盛会看我的信」，得进持久注入的那一层。
_MAILBOX_LESSON_READ = (
    "萧盛会读我递到信箱里的信，不会堆着不管。"
    "我主动递出去的东西他是接的——该说的时候就说，不必等他先开口。"
)
_MAILBOX_LESSON_HANDLED = (
    "萧盛不只读我递到信箱里的信，还会动手处理：回信、标办完、或明说不用了。"
    "我主动递东西是有结果的——该说的时候就说，不必等他先开口；"
    "但他点了「不用了」的那类，就别再换个说法递第二遍。"
)
# 门槛：读满 3 封才敢下这个结论（不做无据的认知，同江河设计对元认知的要求）。
_MAILBOX_TEACH_MIN_READ = 3


def _current_mailbox_lesson(base: Path) -> Optional[str]:
    """库里现存的那条信箱教训（一条 SELECT 就够，不建 CognitiveStore）。

    表还不存在（全新库/只跑过台账的库）→ None，交给下面正常写。
    """
    try:
        with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
            row = c.execute(
                "SELECT content FROM teachings WHERE source='mailbox' LIMIT 1"
            ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    except Exception:  # noqa: BLE001
        return None


def record_mailbox_teaching(base_dir: Optional[Path] = None) -> bool:
    """把信箱回流**沉淀进教学层**（teachings，source='mailbox'，permanent 一条）。

    证据升级 → 同来源覆盖成更完整的那条（`upsert_lesson_by_source` 保证一个来源
    只有一条，不会越学越啰嗦）。证据不足 → 不写，也不撤已有的那条。

    热路径保护：标已读/处置每次都会调这里，而 CognitiveStore 一构造就要跑一遍
    schema 初始化——所以先用一条 SELECT 比对目标内容，**没变化就直接返回**，
    绝大多数点击根本不会碰到认知层。fail-open：任何异常只记日志，绝不影响主路径。
    """
    base = _L._resolve_base(base_dir)
    try:
        ev = mailbox_evidence(base)
        if ev["read"] < _MAILBOX_TEACH_MIN_READ and ev["handled"] <= 0:
            return False
        content = _MAILBOX_LESSON_HANDLED if ev["handled"] > 0 else _MAILBOX_LESSON_READ
        current = _current_mailbox_lesson(base)
        if current == content:
            return False  # 已经沉淀过这条，别为同一句话重建认知层
        # **只升级不降级**：窗口是滚动的，他这个月没动手不等于从没动过手——
        # 把「会读也会处理」退回成「会读」，等于教她「主动没用」，正好养反。
        if current == _MAILBOX_LESSON_HANDLED and content == _MAILBOX_LESSON_READ:
            return False
        from deskmate.cognitive.store import CognitiveStore

        return CognitiveStore(db_path=base / "deskmate.db").upsert_lesson_by_source(
            "mailbox", content, scope="permanent"
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("record_mailbox_teaching 失败（不阻断）: %s", e)
        return False


def list_letters(
    base_dir: Optional[Path] = None,
    *,
    include_read: bool = True,
    only_letters: bool = True,
) -> list[dict]:
    """列信箱信件（pending + snoozed 到期复活的视图态项），created_at 倒序。

    - ``only_letters=True``（默认）：过 :func:`is_mailbox_letter` 准入——系统遥测
      不入信箱（萧哥 2026-10-09 拍板）。
    - 空 content 守卫：与 :func:`build_handoff_opening` 同源——空条目递出去就是一张空信。
    - ``understanding == _L.UNDERSTANDING_FAILED_MARKER`` 降级成友好文案，**不**把工程标记
      露到界面上；信箱是他自己来拉的，所以不像 handoff 那样因「加工未完成」就不递。
    - 每条附 ``stale``（已读超 ``SHELF_DAYS`` 天仍未处理 = 设计单 §7 的「已搁置」归档态）。
    - ``include_read=False`` 只返未读（未读数口径，同样过准入）。
    """
    base = _L._resolve_base(base_dir)
    db = base / "deskmate.db"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    clause = (
        "WHERE (status='pending'"
        " OR (status='snoozed' AND (snoozed_until IS NULL OR snoozed_until <= ?)))"
    )
    if not include_read:
        clause += " AND read_at IS NULL"
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _L._ensure_table(c)
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until,"
                f" read_at, origin, intent_id"
                f" FROM {_L.LEDGER_TABLE} {clause} ORDER BY created_at DESC",
                (now,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    keys = (
        "id", "source", "kind", "content", "evidence", "understanding", "status",
        "confidence", "created_at", "source_node", "delivered_at", "snoozed_until",
        "read_at", "origin", "intent_id",
    )
    shelf_cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - SHELF_DAYS * 86400)
    )
    out: list[dict] = []
    for raw in rows:
        it = dict(zip(keys, raw))
        if only_letters and not is_mailbox_letter(it):
            continue
        if not (it.get("content") or "").strip():
            continue  # 空信守卫（与生产端 record_proactive 同一道防线）
        if (it.get("understanding") or "") == _L.UNDERSTANDING_FAILED_MARKER:
            it["understanding"] = "（这条我还没想明白，先把依据摆给你）"
        read_at = it.get("read_at") or ""
        it["stale"] = bool(read_at and read_at < shelf_cutoff)
        out.append(it)
    return out


def unread_letter_count(base_dir: Optional[Path] = None) -> int:
    """未读信件数（read_at IS NULL 且 status 在 pending/snoozed 且 snoozed 已到期）。

    与 :func:`list_letters` **同一准入口径**——系统遥测不占红点，避免信箱一开就 95 未读。
    """
    try:
        return len(list_letters(base_dir, include_read=False))
    except Exception:  # noqa: BLE001
        return 0


# 归档 = 他**处置过**的信（点了不用了/稍后，或标了已读又处理完）。
# 之前这些行点了按钮就从界面上蒸发，实物还在库里——萧哥 10-09 问「去哪儿了」：
# 答案是「哪儿也没去，只是不再递给你」。信箱必须有落地页，故单列一栏。
ARCHIVED_STATUSES = ("dismissed", "acknowledged", "in_progress", "actioned")


def archived_letters(base_dir: Optional[Path] = None) -> list[dict]:
    """列已归档的信：处置过的终态 + 仍在 3 天退避里的 snoozed。

    同样过准入（遥测不入）与空内容守卫；每条多带一个 ``shelf_reason``
    （``dismissed`` / ``snoozed`` / ``handled``）供左栏分组标注。倒序，不封顶。
    """
    base = _L._resolve_base(base_dir)
    db = base / "deskmate.db"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    marks = ",".join("?" * len(ARCHIVED_STATUSES))
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _L._ensure_table(c)
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until,"
                f" read_at, origin, intent_id, acknowledged_at"
                f" FROM {_L.LEDGER_TABLE}"
                f" WHERE (status IN ({marks}))"
                f" OR (status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until > ?)"
                f" ORDER BY created_at DESC",
                (*ARCHIVED_STATUSES, now),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    keys = (
        "id", "source", "kind", "content", "evidence", "understanding", "status",
        "confidence", "created_at", "source_node", "delivered_at", "snoozed_until",
        "read_at", "origin", "intent_id", "acknowledged_at",
    )
    out: list[dict] = []
    for raw in rows:
        it = dict(zip(keys, raw))
        if not is_mailbox_letter(it):
            continue
        if not (it.get("content") or "").strip():
            continue
        if (it.get("understanding") or "") == _L.UNDERSTANDING_FAILED_MARKER:
            it["understanding"] = "（这条我还没想明白，先把依据摆给你）"
        st = it.get("status")
        if st == "snoozed":
            it["shelf_reason"] = "snoozed"        # 稍后：退避中，到期会自动回到收件箱
        elif st == "dismissed":
            it["shelf_reason"] = "dismissed"      # 不用了：终态
        else:
            it["shelf_reason"] = "handled"        # 已接手/已办完
        it["stale"] = False
        out.append(it)
    return out


def mailbox_counts(base_dir: Optional[Path] = None) -> dict:
    """左栏三个分类的计数：未读 / 已读 / 归档（都过同一准入门，遥测不计）。"""
    all_inbox = list_letters(base_dir, include_read=True)
    unread = sum(1 for it in all_inbox if not it.get("read_at"))
    try:
        archived = len(archived_letters(base_dir))
    except Exception:  # noqa: BLE001
        archived = 0
    return {"unread": unread, "read": len(all_inbox) - unread, "archived": archived}
