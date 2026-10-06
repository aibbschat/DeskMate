"""主动产出管道 · 台账层（2026-10-06 P0）。

所有主动产出（云端内容审计 / 手机灵感 / 日后来源）的统一落库 + 同步 + 本地对接。
生产者只调用 :func:`record_proactive`，其余（跨端同步、本地对话式对接）由管道统一解决。

设计见 ``docs/云端主动与本地对接设计_2026-10-06.md``：
- 确定性 id（``<source>:<kind>:<指纹>``）→ 双端同 id 自动合并（禁随机 uuid，对应 48 条分裂教训）。
- status 只前进不后退（``SYNC_TABLES['proactive_ledger']['ordered_status']`` + sync_daemon LWW 三处）。
- 本地对接 = 开新会话时查 pending → 注入上下文 → 对话式开场白（非浮层）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEDGER_TABLE = "proactive_ledger"

# status 优先级（高→低），与 SYNC_TABLES['proactive_ledger']['ordered_status'] 同源。
# 同步层（sync_daemon LWW）据此强制只前进；应用层 acknowledge_ledger 也兜底。
# snoozed（修⑥）插在 acknowledged 与 pending 之间：只压过 pending——远端迟到的
# pending 不得覆盖已搁置条目（反 nag）；被 in_progress/acknowledged/actioned/dismissed
# 压过——用户在任一端真处理了，处理结果跨端生效，不会被别端的「稍后」锁死。
# 「到期复活」不靠 status 回写，由 pending_items 读路径按 snoozed_until 判定（字段保持
# snoozed 可审计），这就是 §11.2②「独立轨道」的落地形态。
STATUS_ORDER = ("actioned", "dismissed", "in_progress", "acknowledged", "snoozed", "pending")

# understanding 加工失败标记（P1 §11.5）：有 LLM key 但调用失败时写回，handoff 降级递。
# 与空字符串区分：空 = 加工未完成（守卫跳过不递）；该标记 = 加工失败（降级递并提示）。
UNDERSTANDING_FAILED_MARKER = "[understanding_failed]"

# ---- 防 nag（§七 / §11.2③ / §11.7③，修④）----
# 写入门槛：confidence ≥ 0.75 且 evidence 明确才写 pending（低置信只进 prepare，不递）。
# mobile_voice 等来源由生产者自带 confidence（用户原话=高置信）与 evidence（手机端时间锚点）。
MIN_WRITE_CONFIDENCE = 0.75
# 每日主动交付上限（跨来源、跨端合计，≤3 条/天；超额安静攒到次日）。计数直接查 ledger
# 的 delivered_at（业务列进 oplog 天然跨端共享），不落 DisturbManager——进程内计数会让
# 云与 Mac 各发 3 条＝实际 6（§11.2③ 教训）。
DAILY_DELIVERY_CAP = 3
# snoozed 退避天数（§七：被搁置条目按意志层阈值后再轻递；修⑥启用）。
SNOOZE_DAYS = 3


def _short_id(rid: str) -> str:
    """稳定 #id 句柄（§六 三件套之二）：取指纹尾 6 位，如 #a1b2c3。

    完整 id（<source>:<kind>:<指纹>）仍在 items[i].id 供 API/回查；
    开场白与卡片展示短句柄，用户可「先说 #a1b2c3」顺着回查。
    """
    return (rid or "").rsplit(":", 1)[-1][:6]


def _today() -> str:
    """今日日期串（%Y-%m-%d）。

    两端节点均 Asia/Shanghai（§11.8「跨时区每日语义」暂不做）；created_at/delivered_at
    以写入端墙钟 localtime 生成，故日期串即 Asia/Shanghai 分日口径。
    """
    return time.strftime("%Y-%m-%d", time.localtime())


# ---- §11.4 三档分流（修⑨）：时效型即时递；相关型按上下文取最相关；其余攒着 ----
# 时效型来源/类型（任务异常/同步漂移/系统体检）：即时递，不受日上限与相关性约束。
URGENT_SOURCES = ("automation", "sync_health", "ops")
URGENT_KINDS = ("task_anomaly", "drift", "window_failed", "ops_report")
# 相关型：一次只递最相关的 N 条（§11.4②，防「按时间序念台账＝骚扰」）。
RELEVANT_TOP_N = 2


def _bigrams(text: str) -> set:
    """字符二元组集合——中文无分词依赖的轻量语义近似（stdlib only）。"""
    t = "".join((text or "").split())
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _relevance_score(item: dict, context: str) -> float:
    """台账项与会话上下文的重叠度（bigram 交集 / sqrt(项长)），无外部依赖。

    语义检索（BGE）按 §11.6/§11.8 属 P3「暂不做」；此处先用可解释的字面重叠兜底，
    接口（分数越高越相关、0 = 无关联不递）与日后 BGE 版本一致，可整体替换。
    """
    ctx = _bigrams(context)
    if not ctx:
        return 0.0
    blob = " ".join(
        str(item.get(k) or "") for k in ("kind", "content", "evidence", "understanding")
    )
    bi = _bigrams(blob)
    if not bi:
        return 0.0
    return len(bi & ctx) / (len(bi) ** 0.5)


# ---- 生产者注册表（§四/§九「接口留好」，修⑧）----
# 新增来源 = register_producer 登记产出函数；函数返回业务字段 dict 列表，其余由管道补。
# §九不变量：生产者不得自己广播浮层、自己写同步、自己决定何时递——那些是管道的事。
PRODUCERS: dict = {}


def register_producer(source: str):
    """把 producer_fn(**ctx) -> list[dict] 登记进 PRODUCERS（§四 注册式）。"""

    def _deco(fn):
        PRODUCERS[source] = fn
        return fn

    return _deco


def run_producer(
    source: str, ctx: Optional[dict] = None, base_dir: Optional[Path] = None
) -> int:
    """跑注册的 producer 并统一落库；返回落库条数（未过门槛被拒的不计）。fail-open。

    base_dir 显式下传（多 workspace 场景由调用方决定写哪个库），不传走默认解析。
    """
    fn = PRODUCERS.get(source)
    if fn is None:
        logger.debug("producer 未登记: %s", source)
        return 0
    n = 0
    try:
        items = fn(**(ctx or {})) or []
    except Exception as e:  # noqa: BLE001  producer 失败绝不阻断调用方主流程
        logger.debug("producer %s 执行失败（不阻断）: %s", source, e, exc_info=True)
        return 0
    for item in items:
        try:
            kwargs = dict(item)
            kwargs.pop("source", None)  # 来源由注册名决定，防止 item 自报错来源
            rid = record_proactive(base_dir=base_dir, source=source, **kwargs)
            if rid:
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("producer %s 落库失败（不阻断）: %s", source, e, exc_info=True)
    return n


# ---- 建表一次性标志（按 base_dir 分桶，进程内）----
# migrate() 每次都会 DROP 三触发器再重建 + commit（sync_store_sqlite 的幂等策略）。
# record_proactive 是逐条调用、云端审计循环每小时跑 —— 每条都重建触发器是纯浪费，
# 且长时间持写锁。故每个库只自举一次；进程重启后标志复位，可再幂等跑一次。
_ensured: set[str] = set()
_ensure_lock = threading.Lock()


def _resolve_base(base_dir: Optional[Path]) -> Path:
    """解析 DESKMATE 基目录（调用时解析，禁模块级常量 / import 期副作用）。"""
    if base_dir is not None:
        return Path(base_dir)
    try:
        from deskmate.paths import deskmate_base_dir

        return Path(deskmate_base_dir())
    except Exception:
        return Path.home() / ".deskmate"


def _ensure_table(conn: sqlite3.Connection) -> None:
    """自举建表 + 接入同步（加三列 + 触发器，幂等）。

    不依赖其它 store 的初始化顺序；测试用 /tmp 副本库也可直接跑。

    幂等由 :data:`_ensured` 按真实库路径做进程内记忆：``migrate()`` 每次都会
    DROP 三个触发器再重建 + commit，逐条调用是纯浪费（云端审计循环每小时跑）。
    仍保留一次 ``sqlite_master`` 探测——万一库被外部重建/删除（测试换库、
    运维重建），不能凭标志跳过建表。探测只是一次索引查询，远便宜于重建 DDL。
    """
    try:
        key = str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve())
    except Exception:  # noqa: BLE001  拿不到路径就退回每次自举（正确性优先于性能）
        key = None

    if key is not None and key in _ensured:
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEDGER_TABLE,)
            ).fetchone()
            if row is not None:
                return
        except sqlite3.OperationalError:
            pass  # 表没了 → 继续自举

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
        f"id TEXT PRIMARY KEY, source TEXT, kind TEXT, content TEXT, evidence TEXT,"
        f" understanding TEXT, payload TEXT, source_node TEXT, status TEXT,"
        f" confidence REAL, created_at TEXT, acknowledged_at TEXT,"
        f" delivered_at TEXT, snoozed_until TEXT)"
    )
    # 存量库升级（修④/修⑥ schema）：migrate() 只给表加同步三列、不加业务列，而触发器
    # body 按 spec business_cols 生成 json_object——表缺列时 migrate 直接 OperationalError。
    # 故先探测补齐业务列再 migrate（老库首次升级必经；新库 CREATE 已含全列，此处 no-op）。
    try:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({LEDGER_TABLE})")}
        for _c in ("delivered_at", "snoozed_until"):
            if _c not in have:
                conn.execute(f"ALTER TABLE {LEDGER_TABLE} ADD COLUMN {_c} TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        from deskmate.memory.sync_store_sqlite import migrate

        node = "mac"
        try:
            r = conn.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
            if r and r[0]:
                node = r[0]
        except Exception:
            pass
        migrate(conn, node, LEDGER_TABLE)
    except Exception as e:  # noqa: BLE001  fail-open：建表成功即可落库，同步列缺失仅影响跨端
        logger.warning("proactive ledger migrate 失败（同步列/触发器可能缺失）: %s", e)
    if key is not None:
        with _ensure_lock:
            _ensured.add(key)


def _deterministic_id(source: str, kind: str, anchor: Optional[str], content: str) -> str:
    """确定性 id = ``<source>:<kind>:<指纹>``。

    指纹优先用生产者提供的稳定锚点（如证据章节定位）；缺失时基于 content 截断 hash 兜底，
    使同内容合并。双端同 id → 同 sync_uuid → 自动合并（禁用随机 uuid）。
    """
    if anchor:
        finger = anchor
    else:
        finger = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{kind}:{finger}"


def record_proactive(
    base_dir: Optional[Path] = None,
    *,
    source: str,
    kind: str,
    content: str,
    evidence: str = "",
    understanding: Optional[str] = None,
    anchor: Optional[str] = None,
    payload: Optional[dict] = None,
    source_node: Optional[str] = None,
    confidence: float = 0.0,
) -> str:
    """落一条主动产出到 ``proactive_ledger``（status=pending），经触发器自动进 oplog 同步。

    同 ``source/kind/anchor`` 重复调用 → 合并（更新非 status 字段，status 不降级）。
    返回确定性 id。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    rid = _deterministic_id(source, kind, anchor, content)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    und = understanding if understanding is not None else ""
    # §11.6：evidence 存锚点+截断片段（oplog payload_json 每份复制全文会放大同步成本，
    # 24h 2178 条 profile_items op 空转教训）。生产者自截之外这里强制兜底。
    evidence = (evidence or "")[:500]
    # §七写入门槛（修④）：confidence ≥ 0.75 且 evidence 明确才写 pending；
    # 低置信只进 prepare（上游 evaluate_action 的静默通道），不落台账、不递（§11.7③）。
    if confidence < MIN_WRITE_CONFIDENCE or not evidence.strip():
        logger.debug(
            "主动产出未过写入门槛（confidence=%.2f, evidence=%s），不落台账",
            confidence, "有" if evidence.strip() else "无",
        )
        return ""
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        _ensure_table(c)
        if source_node is None:
            try:
                sn = c.execute("SELECT node FROM _sync_meta WHERE id=1").fetchone()
                source_node = sn[0] if sn and sn[0] else "mac"
            except Exception:
                source_node = "mac"
        cols = (
            "id, source, kind, content, evidence, understanding, payload,"
            " source_node, status, confidence, created_at, acknowledged_at,"
            " delivered_at, snoozed_until"
        )
        try:
            c.execute(
                f"INSERT INTO {LEDGER_TABLE} ({cols})"
                f" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, source, kind, content, evidence, und, payload_json,
                    source_node, "pending", confidence, now, None, None, None,
                ),
            )
        except sqlite3.IntegrityError:
            # 同 id 已存在：合并更新非 status 字段，status 不降级（防反 nag 失效）。
            # §11.11 缺陷1（修①）：
            # - understanding 仅当本次**显式提供**（not None）才覆盖；生产者不传时保留
            #   既有非空值。同内容再产出是必然事件（§11.2① 双端同结论双写）：清空理解
            #   会导致守卫又不递 + 下轮重烧 LLM，且 "" 在 business_cols 里会经触发器
            #   同步对端、把对端已加工的理解一起抹掉。
            # - created_at **不刷新**：日上限按 created_at 分日计数（§11.2③）、
            #   排序按其倒序；重复产出刷新会重置计数并把旧条顶到最前。
            row = c.execute(
                f"SELECT understanding FROM {LEDGER_TABLE} WHERE id=?", (rid,)
            ).fetchone()
            cur_und = row[0] if row else None
            merged_und = und if understanding is not None else (cur_und or "")
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET content=?, evidence=?, understanding=?,"
                f" payload=?, source_node=?, confidence=? WHERE id=?",
                (content, evidence, merged_und, payload_json, source_node, confidence, rid),
            )
        c.commit()
    return rid


def pending_items(base_dir: Optional[Path] = None) -> list[dict]:
    """查所有 ``status='pending'`` 的台账项（created_at 倒序），供本地对接注入会话上下文。"""
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until"
                f" FROM {LEDGER_TABLE}"
                f" WHERE status='pending'"
                f"    OR (status='snoozed' AND (snoozed_until IS NULL OR snoozed_until <= ?))"
                f" ORDER BY created_at DESC",
                (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # 表尚未建（首次运行前）→ 无 pending
    keys = ("id", "source", "kind", "content", "evidence", "understanding", "status", "confidence", "created_at", "source_node", "delivered_at", "snoozed_until")
    return [dict(zip(keys, r)) for r in rows]


def build_handoff_opening(
    base_dir: Optional[Path] = None,
    *,
    session_context: str = "",
    contextual: bool = False,
) -> Optional[dict]:
    """本地对接：生成开新会话时的对话式开场白（非浮层硬弹）。

    返回 {"text": 开场白, "items": [完整项…]}；无 eligible 条目或今日额度用尽返回 None。
    三件套：注入上下文（items 含 evidence+understanding）、稳定 #id 句柄（开场白与卡片
    均带 #指纹尾6位；完整 id 在 items[i].id 供回查）、证据随行（防编造）。

    守卫（P1 §11.5「understanding 也要有据，反现编」）：
    - understanding 仍为空（加工未完成）→ 暂不递，避免退化成念原文；后处理补上后再递。
    - understanding == UNDERSTANDING_FAILED_MARKER（加工失败）→ 降级递 content+evidence，
      并把标记转成友好提示「（理解加工未完成，仅呈现依据）」，不当一等公民异常。

    防 nag 凭证（§七/§11.2③/§11.7③，修④）：
    - 每日交付上限 DAILY_DELIVERY_CAP（跨来源、跨端合计）：计数查 ledger 的 delivered_at
      （业务列进 oplog 天然跨端共享）；超额安静攒到次日。
    - 今日已递过（delivered_at=今日）且仍可递的条目继续呈现（回首页幂等，不是追弹）；
      新条目只取剩余额度。

    三档分流（§11.4，修⑨）：
    - 时效型（URGENT_SOURCES/URGENT_KINDS）：即时递，不受日上限与相关性约束；
    - 相关型：给了 session_context 时按 bigram 重叠取最相关 RELEVANT_TOP_N 条（零关联不递）；
    - 无上下文（DeskHome 空会话首页）：时效型 + 按 created_at 倒序取剩余日额度
      （轻递，§六「开新会话递」原语义，量被日上限封顶）；
    - 其余攒着，等被「聊到」再递。

    contextual（活跃会话相关性注入，本轮新增＝§11.4② 触发入口）：
    - 仅当传入 session_context 才有意义；返回集只取 fresh_new（今日尚未递过的相关/时效新条目），
      剔除 already（今日已递），避免与首页/之前会话追重；零相关 → fresh_new 为空 → 返回 None（静默）。
    - 调用方：前端 deskmate 活跃会话把最近对话作 session_context 传入，聊到某主题才递对应待办。
    """
    items = pending_items(base_dir)  # pending + snoozed 到期复活（§七 退避后轻递）
    if not items:
        return None
    pool = []
    for it in items:
        und = it.get("understanding") or ""
        if und == UNDERSTANDING_FAILED_MARKER:
            it = dict(it)
            it["understanding"] = "（理解加工未完成，仅呈现依据）"
        elif not und:  # 加工未完成 → 按 §11.5 不递，等后处理补上再递
            continue
        else:
            it = dict(it)
        pool.append(it)
    if not pool:
        return None
    today = _today()
    urgent_ids = {
        it["id"]
        for it in pool
        if it.get("source") in URGENT_SOURCES or it.get("kind") in URGENT_KINDS
    }
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            _ensure_table(c)  # 老库升级：确保 delivered_at/snoozed_until 列在
        except Exception:  # noqa: BLE001
            pass
        try:
            r = c.execute(
                f"SELECT count(*) FROM {LEDGER_TABLE}"
                f" WHERE delivered_at IS NOT NULL AND delivered_at <> ''"
                f" AND substr(delivered_at,1,10)=?",
                (today,),
            ).fetchone()
            delivered_today = int(r[0]) if r else 0
        except sqlite3.OperationalError:
            delivered_today = 0
        already = [it for it in pool if (it.get("delivered_at") or "")[:10] == today]
        fresh = [it for it in pool if (it.get("delivered_at") or "")[:10] != today]
        fresh_urgent = [it for it in fresh if it["id"] in urgent_ids]
        fresh_rest = [it for it in fresh if it["id"] not in urgent_ids]
        if session_context.strip():
            # 相关型（§11.4②）：按上下文重叠取最相关 N 条，零关联的攒着不递
            fresh_rest.sort(
                key=lambda x: _relevance_score(x, session_context), reverse=True
            )
            fresh_rest = [
                it
                for it in fresh_rest
                if _relevance_score(it, session_context) > 0
            ][:RELEVANT_TOP_N]
        else:
            # 无上下文（空会话首页）：按 created_at 倒序轻递，量由日上限封顶（§六原语义）
            fresh_rest.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        room = max(0, DAILY_DELIVERY_CAP - delivered_today)
        fresh_new = fresh_urgent + fresh_rest[:room]  # 时效型不占额度
        if fresh_new:
            now_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            for it in fresh_new:
                try:
                    c.execute(
                        f"UPDATE {LEDGER_TABLE} SET delivered_at=? WHERE id=?",
                        (now_ts, it["id"]),
                    )
                except sqlite3.OperationalError:
                    break  # 老库缺列 → 不记交付标记（fail-open，仍递）
            c.commit()
    chosen = already + fresh_new
    if contextual:
        # 活跃会话相关性注入：只递今日尚未递过的相关/时效新条目，不重复呈现 already
        # （今日已递），避免与首页/之前会话追重；零相关 fresh_new 为空 → 返回 None（静默）。
        chosen = fresh_new
    if not chosen:
        return None  # 今日额度用尽 / 无相关项：安静攒到次日
    chosen.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    lines = []
    for i, it in enumerate(chosen, 1):
        ev = f"（依据：{it['evidence']}）" if it.get("evidence") else ""
        un = f"\n    我的理解：{it['understanding']}" if it.get("understanding") else ""
        lines.append(f"{i}. {it['content']}{ev}{un}\n    #{_short_id(it['id'])}")
    text = (
        "我替你盯着这个家，发现几件事还没收：\n"
        + "\n".join(lines)
        + "\n你要先处理哪个？（回复序号或条目 #id 即可）"
    )
    return {"text": text, "items": chosen}


def acknowledge_ledger(
    base_dir: Optional[Path] = None,
    rid: Optional[str] = None,
    *,
    new_status: str,
) -> bool:
    """用户处理台账项：流转 status（pending→acknowledged/in_progress→actioned/dismissed）。

    仅允许前进或终态（不降级）；应用层兜底，同步层在跨端收敛时强制。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    if rid is None:
        return False
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        _ensure_table(c)
        cur = c.execute(f"SELECT status FROM {LEDGER_TABLE} WHERE id=?", (rid,)).fetchone()
        if cur is None:
            return False
        cur_status = cur[0]
        if (
            cur_status in STATUS_ORDER
            and new_status in STATUS_ORDER
            and STATUS_ORDER.index(new_status) > STATUS_ORDER.index(cur_status)
        ):
            return False  # 降级尝试（目标优先级更低）→ 拒绝
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        if new_status == "snoozed":
            # §七/修⑥：「稍后」→ snoozed + snoozed_until（SNOOZE_DAYS 天后读路径自动复活
            # 轻递）。不写 acknowledged_at——「看了」≠「处理了」（§六）。
            until = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + SNOOZE_DAYS * 86400)
            )
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET status=?, snoozed_until=? WHERE id=?",
                (new_status, until, rid),
            )
        elif new_status in ("actioned", "dismissed", "acknowledged", "in_progress"):
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET status=?, acknowledged_at=? WHERE id=?",
                (new_status, now, rid),
            )
        else:
            c.execute(f"UPDATE {LEDGER_TABLE} SET status=? WHERE id=?", (new_status, rid))
        c.commit()
    return True
