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
import os
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
#: 运维巡检类（health_producers 家族产出）＝系统简报：**只进信箱，会话开场不念**。
#: 2026-10-10 二次「还是会弹出来」：URGENT 时效型即时递把 automation 巡检异常
#: 每次开新会话都倒一遍（遥测门只拦 *_error，拦不住这批「正常名字」的巡检 kind）。
#: 口径（萧哥拍板）：这类归信箱——台账仍保留（层一每轮回查、他真问起仍可被动浮现），
#: is_mailbox_letter 仍收（信箱可见），唯独不进 build_handoff_opening 的会话池。
OPS_MAILBOX_ONLY_KINDS = (
    "task_anomaly",
    "drift",
    "window_failed",
    "ops_report",
    "stall_flag",
)
# 相关型：一次只递最相关的 N 条（§11.4②，防「按时间序念台账＝骚扰」）。
RELEVANT_TOP_N = 2


def _bigrams(text: str) -> set:
    """字符二元组集合——中文无分词依赖的轻量语义近似（stdlib only），作为 BGE 不可用时的兜底。"""
    t = "".join((text or "").split())
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _relevance_score_bigram(item: dict, context: str) -> float:
    """字面重叠兜底（BGE 不可用时）：bigram 交集 / sqrt(项长)。"""
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


def _bge_available() -> bool:
    """本地 BGE（fastembed）是否已可用；仅检查包存在，不加载模型。"""
    try:
        from deskmate.memory import local_embed
        return bool(local_embed.available())
    except Exception:  # noqa: BLE001
        return False


# BGE 挂死护栏（2026-10-16 workbuddy 记账）：模型首次加载要下载 ~185MB，弱网/沙箱下
# 会把调用线程拖死（每 5min tick 一次 = 线程泄漏慢性病）。超时一次即熔断——bigram
# 字面兜底本来就是主闸，BGE 只是高置信补充，熔断后行为不退化。
_BGE_TIMEOUT_S = float(os.environ.get("DESKMATE_BGE_TIMEOUT_S", "30"))
_BGE_EMBED_DISABLED = os.environ.get("DESKMATE_BGE_EMBED", "1") == "0"  # 测试一键关
_bge_breaker = threading.Event()   # set = 已熔断（本进程不再试 BGE）


def _bge_breaker_tripped() -> bool:
    """熔断是否已触发（含环境变量一键关）。"""
    return _BGE_EMBED_DISABLED or _bge_breaker.is_set()


def _bge_embed(texts: list) -> Optional[list]:
    """批量 embed；异常/不可用/超时/熔断返回 None。首次调用会加载模型（~185MB）。

    超时：子线程跑 embed，超 _BGE_TIMEOUT_S 未归 → 熔断（本进程不再试），返回 None。
    防的是「每 5min tick 泄漏一个挂死线程」的慢性病——bigram 主闸仍在，行为不退化。
    """
    if _bge_breaker_tripped():
        return None
    try:
        from deskmate.memory import local_embed
        if not local_embed.available():
            return None
    except Exception:  # noqa: BLE001
        return None

    box: dict = {}

    def _run() -> None:
        try:
            from deskmate.memory import local_embed
            box["emb"] = local_embed.embed(texts)
        except Exception:  # noqa: BLE001
            box["emb"] = None

    t = threading.Thread(target=_run, daemon=True, name="bge-embed")
    t.start()
    t.join(_BGE_TIMEOUT_S)
    if t.is_alive():
        _bge_breaker.set()
        logger.warning(
            "BGE embed 超时（%.0fs），本进程熔断 BGE，回落 bigram 字面兜底",
            _BGE_TIMEOUT_S,
        )
        return None
    return box.get("emb")


def _bge_reset_for_test() -> None:
    """清熔断态（**仅供测试**：一个用例熔断后不许污染同进程其他用例）。"""
    _bge_breaker.clear()


def bge_breaker_status() -> dict:
    """熔断状态诊断（供 MCP/排查）。"""
    return {
        "disabled_by_env": _BGE_EMBED_DISABLED,
        "tripped": _bge_breaker.is_set(),
        "timeout_s": _BGE_TIMEOUT_S,
    }


def _cosine(a, b) -> float:
    try:
        from deskmate.memory.semantic_search import cosine_similarity
        return float(cosine_similarity(a, b))
    except Exception:  # noqa: BLE001
        return 0.0


# BGE 语义相似度高置信门槛（P3-b 校准，实测）：BGE 中文无关句地板约 0.25–0.31、同书但不同线
# 约 0.45、真相关（共享章/人物锚点或强改写）≥0.5。低于此值视为「语义未达可靠相关」，仅取字面
# bigram 分，避免 BGE 地板把无关项误判为相关（设计 §11.4② 反骚扰底线：宁可少递不可乱递）。
BGE_SEMANTIC_PASS = 0.5


def _relevance_score(item: dict, context: str) -> float:
    """台账项与会话上下文的相关性（0 = 不相关、不递）。

    P3-b：字面 bigram 重叠为**主闸**（设计 §11.4② 证据随行——章/人物名等锚点精确匹配，
    稳定可解释、不受 embedding 版本漂移影响）；BGE 语义相似度仅在**高置信**（≥
    BGE_SEMANTIC_PASS）时补充，捕捉「共享语义但不共享字面」的改写/同义场景。两者取较大值。
    fastembed 未装 / 异常时回落纯 bigram，行为不退化。
    """
    blob = " ".join(
        str(item.get(k) or "") for k in ("kind", "content", "evidence", "understanding")
    )
    ctx = (context or "").strip()
    if not (blob.strip() and ctx):
        return 0.0
    bg = _relevance_score_bigram(item, context)
    if _bge_available():
        emb = _bge_embed([blob, ctx])
        if emb and len(emb) == 2:
            sim = _cosine(emb[0], emb[1])
            if sim >= BGE_SEMANTIC_PASS:
                # 高置信语义相关：取 max（字面有重叠以字面为主，纯语义强相关以语义为主）
                return max(bg, sim)
            # 语义未达高置信：仅取字面分（BGE 地板不污染相关性判定）
            return bg
    return bg


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
            # R0 把关（第0层 指纹锚定）：矛盾类判定缺锚点 → 拒收，不落台账、不造 34× 重复。
            # 精准命中 34× 的泄漏点（矛盾判定一律经 producer 产出落库），不影响非 judge 链路。
            if kwargs.get("kind") in _ANCHOR_REQUIRED_KINDS and not kwargs.get("anchor"):
                logger.debug(
                    "R0 拒收 producer=%s kind=%s：矛盾类判定缺锚点，不落台账",
                    source, kwargs.get("kind"),
                )
                continue
            rid = record_proactive(base_dir=base_dir, source=source, **kwargs)
            if rid:
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("producer %s 落库失败（不阻断）: %s", source, e, exc_info=True)
    return n


def run_producers_all(base_dir: Optional[Path] = None, *, exclude: tuple = ("cloud_audit",)) -> int:
    """周期性跑全部已注册 producer（除已在 tick SUGGEST 分支跑的 cloud_audit），fail-open。

    各 producer 内部按 ``proactivity_meta`` 自节流（每小时 / 每日），故可从任意 30s tick
    或每小时云端窗口无脑调用，不会高频打扰。确定性 id 保证跨节点 / 跨调用不重复写。
    返回总落库条数。

    触发接入（§11.14 部署）：
    - Mac 桌面端：``ProactiveEngine.tick()``（复用 understanding 后处理范式，用户不在也跑）；
    - 云端：``scripts/xiaoju_agency_tick.py`` 的 hourly 窗口。
    """
    # 确保健康生产者模块已注册（@register_producer 在 import 时生效）
    try:
        import deskmate.proactivity.health_producers  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    n = 0
    for src in list(PRODUCERS.keys()):
        if src in exclude:
            continue
        ctx = {"base_dir": base_dir} if base_dir is not None else {}
        try:
            n += run_producer(src, ctx=ctx, base_dir=base_dir)
        except Exception as e:  # noqa: BLE001
            logger.debug("producer %s 调用失败（不阻断）: %s", src, e, exc_info=True)
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
        f" delivered_at TEXT, snoozed_until TEXT,"
        f" visibility TEXT DEFAULT 'sync',"
        # R13（2026-10-08）：意图讨论态字段。origin 标记条目来源
        # （proactive_default / intent_handoff），intent_id 回溯意图，
        # discuss_phase 区分 opening/draft/final，discuss_round 是讨论轮次。
        f" origin TEXT DEFAULT 'proactive_default',"
        f" intent_id TEXT,"
        f" discuss_phase TEXT,"
        f" discuss_round INTEGER)"
    )
    # 存量库升级（修④/修⑥/P3-a schema）：migrate() 只给表加同步三列、不加业务列，而触发器
    # body 按 spec business_cols 生成 json_object——表缺列时 migrate 直接 OperationalError。
    # 故先探测补齐业务列再 migrate（老库首次升级必经；新库 CREATE 已含全列，此处 no-op）。
    # P3-a：visibility 列（行级「只在本端、不同步」开关，§11.6 隐私分级）一并补齐。
    # R13：origin/intent_id/discuss_phase/discuss_round 列（意图讨论态）。
    try:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({LEDGER_TABLE})")}
        new_cols = {
            "delivered_at": "TEXT",
            "snoozed_until": "TEXT",
            "visibility": "TEXT DEFAULT 'sync'",
            "origin": "TEXT DEFAULT 'proactive_default'",
            "intent_id": "TEXT",
            "discuss_phase": "TEXT",
            "discuss_round": "INTEGER",
            # 信箱呈现层（2026-10-09）：用户「标已读/已展开」时间戳。
            # 空 = 未读；与 status 正交——已读但未处理 = read_at 非空且 status 仍 pending/snoozed。
            # 仅本地视图态，不进 oplog（Mac-only，不同步，决策5）。
            "read_at": "TEXT",
        }
        for _c, _t in new_cols.items():
            if _c not in have:
                conn.execute(f"ALTER TABLE {LEDGER_TABLE} ADD COLUMN {_c} {_t}")
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


# ---- R0（第0层 指纹锚定，2026-10-09）----
# 矛盾类发现（写作场景的设定/时间线/伏笔/人物一致性判定）必须有锚点：无锚点 → 模型每次重判
# 都换措辞 → 退回 content-hash 永远算新 id → 同一问题被反复落库（实测 plot_contradiction
# 单问题两天攒 34 封）。
#
# 落地方式（关键决策）：锚点合并放在 ``_resolve_dedup_rid``（有锚点 → 同 id 合并，治措辞漂移，
# 34→1 的核心）；「矛盾类无锚点 → 拒收」的把关放在 ``run_producer`` 的条目产出环节，而不是
# ``record_proactive`` 本身。
#   - 这样精准命中 34× 的泄漏点：矛盾判定一律经 producer 产出 → 落库；且不影响其它直接调
#     ``record_proactive`` 的链路（intent_discuss 的 opening/draft、手机端 mobile_voice 的
#     idea_capture/topic_note 都用非锚点 kind；单测也直接用裸 record_proactive 当夹具）。
#   - 不引入「无锚点模糊去重兜底（bigram Jaccard 合并相似措辞）」：实测会把内容不同、仅
#     evidence 模板相同的两条记录误并（如两封不同的 emotional_care 信共用同一模板 → 同 rid
#     → 错误合并，违反「不误杀」验收）；模糊网收益有限，真正的 34→1 靠锚点合并即达成。
_ANCHOR_REQUIRED_KINDS = frozenset(
    {
        "plot_contradiction",
        "missing_foreshadow",
        "character_inconsistency",
        "timeline_error",
        "plot_repetition",
    }
)

#: 手机端 / 「记住这个」入口**允许**的 kind 白名单（2026-10-10）。
#:
#: 为什么需要：``POST /v1/deskmate/proactive/capture`` 的 ``kind`` 直接取自 HTTP body，
#: 原实现只 ``.strip()`` 就落库。实测手机端传 ``kind=plot_contradiction`` 可直写矛盾类
#: 且 ``anchor=None``，产出 ``mobile_voice:plot_contradiction:973f653b…``。
#: 而 R0 的拒收把关上移到了 ``run_producer`` 的条目产出环节 ⇒ 这条 HTTP 路径成了绕过口：
#: 矛盾类本应「无锚点即拒收」，却能绕过拒收直接落库。
#:
#: 与其把写门级拒收加回 record_proactive（会误伤 19 个测试函数 / 24 处夹具，
#: 见 R0 commit f22cfda 的设计取舍），不如在此处按**来源能力**收口：
#: 手机端本就只该记「想到的」「想记的」两类，矛盾判定是 judge/cloud_audit 的职责。
MOBILE_KINDS = frozenset({"idea_capture", "topic_note"})


def is_mobile_kind_allowed(kind: Optional[str]) -> bool:
    """手机端/「记住这个」入口的 kind 是否在白名单内（None/空 视为默认 idea_capture，放行）。

    判定口径与落库口径一致：空值落到 ``idea_capture``，故空值放行。
    """
    k = (kind or "idea_capture").strip() or "idea_capture"
    return k in MOBILE_KINDS


def _resolve_dedup_rid(
    c,
    source: str,
    kind: str,
    anchor: Optional[str],
    content: str,
    evidence: str,
) -> str:
    """R0：决定本次落库的确定性 id。

    - 有锚点 → ``source:kind:anchor``（同锚点天然合并，治措辞漂移，34→1 的关键）。
    - 无锚点 → 旧 content-hash 兜底（同内容合并，不比以前差）。

    「矛盾类无锚点 → 拒收」的把关上移到 ``run_producer`` 的条目产出环节（见
    ``_ANCHOR_REQUIRED_KINDS`` 使用处），此处不做拒收，避免误伤直接调 ``record_proactive``
    的非 judge 链路与单测夹具。
    """
    if anchor:
        return f"{source}:{kind}:{anchor}"
    # 旧兜底：content 截断 hash 使同内容合并。
    return _deterministic_id(source, kind, None, content)


def _deterministic_id(source: str, kind: str, anchor: Optional[str], content: str) -> str:
    """确定性 id = ``<source>:<kind>:<指纹>``。

    指纹优先用生产者提供的稳定锚点（如证据章节定位）；缺失时基于 content 截断 hash 兜底，
    使同内容合并。双端同 id → 同 sync_uuid → 自动合并（禁用随机 uuid）。
    R0 后该兜底路径只对**非矛盾类** kind 生效（矛盾类无锚点直接拒收，见 ``_resolve_dedup_rid``）。
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
    visibility: str = "sync",
    # R13（2026-10-08）：意图讨论态。bypass_threshold=True 跳过 confidence 门槛
    # （intent_handoff 的开场白短句过不了 0.75 门槛，但它是用户已同意的意图产物，
    # 不该被拦）。origin 标记条目来源（proactive_default / intent_handoff），
    # intent_id 回溯意图，discuss_phase 区分 opening/draft/final，discuss_round 是轮次。
    bypass_threshold: bool = False,
    origin: str = "proactive_default",
    intent_id: Optional[str] = None,
    discuss_phase: Optional[str] = None,
    discuss_round: Optional[int] = None,
) -> str:
    """落一条主动产出到 ``proactive_ledger``（status=pending），经触发器自动进 oplog 同步。

    同 ``source/kind/anchor`` 重复调用 → 合并（更新非 status 字段，status 不降级）。
    返回确定性 id。R0（2026-10-09）：矛盾类 kind 缺 anchor 且无相似既有行时**拒收**
    （返回空串，不落台账），避免无锚点矛盾判定被反复重判、把重复搬进库。

    R13：``bypass_threshold=True`` 时跳过 confidence 门槛，用于 intent_handoff
    的开场白短句（用户已同意的意图产物，不该被 confidence 门槛拦）。``origin`` /
    ``intent_id`` / ``discuss_phase`` / ``discuss_round`` 用于意图讨论态标记。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    und = understanding if understanding is not None else ""
    # §11.6：evidence 存锚点+截断片段（oplog payload_json 每份复制全文会放大同步成本，
    # 24h 2178 条 profile_items op 空转教训）。生产者自截之外这里强制兜底。
    evidence = (evidence or "")[:500]
    # 空内容守卫（2026-10-07）：content 是递出卡片的正文，为空时条目毫无意义，
    # 却会因 understanding 后处理补上文字而混过 build_handoff_opening 的守卫被递出空卡
    # （已踩：cloud_audit:no_action 空 content + 有理解 → 递出一张空卡）。源头拦截。
    if not (content or "").strip():
        logger.debug("主动产出 content 为空（source=%s kind=%s），不落台账", source, kind)
        return ""
    # §七写入门槛（修④）：confidence ≥ 0.75 且 evidence 明确才写 pending；
    # 低置信只进 prepare（上游 evaluate_action 的静默通道），不落台账、不递（§11.7③）。
    # R13：bypass_threshold=True 时跳过门槛（intent_handoff 的开场白短句
    # 过不了 0.75 门槛，但它是用户已同意的意图产物，不该被拦）。
    if not bypass_threshold and (confidence < MIN_WRITE_CONFIDENCE or not evidence.strip()):
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
        # R0（第0层 指纹锚定）：确定性 id（有锚点→source:kind:anchor 合并；无锚点→content-hash 兜底）。
        # 「矛盾类无锚点→拒收」的把关在 run_producer 的条目产出环节（见 _ANCHOR_REQUIRED_KINDS），
        # 不在此处，避免误伤非 judge 链路与单测夹具。
        rid = _resolve_dedup_rid(c, source, kind, anchor, content, evidence)
        cols = (
            "id, source, kind, content, evidence, understanding, payload,"
            " source_node, status, confidence, created_at, acknowledged_at,"
            " delivered_at, snoozed_until, visibility,"
            " origin, intent_id, discuss_phase, discuss_round"
        )
        try:
            c.execute(
                f"INSERT INTO {LEDGER_TABLE} ({cols})"
                f" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, source, kind, content, evidence, und, payload_json,
                    source_node, "pending", confidence, now, None, None, None,
                    visibility, origin, intent_id, discuss_phase, discuss_round,
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
                f" payload=?, source_node=?, confidence=?,"
                f" origin=?, intent_id=?, discuss_phase=?, discuss_round=?"
                f" WHERE id=?",
                (content, evidence, merged_und, payload_json, source_node, confidence,
                 origin, intent_id, discuss_phase, discuss_round, rid),
            )
        c.commit()
    return rid


# ---- 共享节流簿记（R2 跨节点 + Leak B judge 节流，2026-10-10，同源单一薄记）----
# 读 proactive_ledger（在 SYNC_TABLES，跨节点同步）：任一节点在窗口内已产出 → 视为「已产出」，
# 从而 Mac+云端同一时段只烧一轮 judge / 只报一次健康巡检。R2 与 Leak B 共用这一张薄记，
# 不各建一套。
# 注意：本块刻意放在 _ANCHOR_REQUIRED_KINDS / MOBILE_KINDS 区域之外 —— 两者相邻关系由
# test_producer_finding_key 的「零交集」断言守护，请勿把新集合插到它们之间。
_HEALTH_KINDS = frozenset({"task_anomaly", "drift", "window_failed", "stall_flag"})


def _throttled_shared(
    base_dir,
    source: str,
    window_s: float,
    *,
    kinds: Optional[set] = None,
    exclude_kinds: Optional[set] = None,
) -> bool:
    """跨节点节流判据：source 在 window_s 内是否已在（同步来的）proactive_ledger 产出过。

    返回 True=应节流（跳过）。fail-open：表不存在/异常 → 返回 False（不节流、照常产出）。

    - kinds：只认这些 kind（R2 健康巡检按 kind 隔离，automation 不误节流 writing）。
    - exclude_kinds：排除这些 kind（Leak B judge 节流排除健康巡检 kind，避免互相误伤）。
    """
    try:
        db = _resolve_base(base_dir) / "deskmate.db"
        conn = sqlite3.connect(str(db), check_same_thread=False)
        try:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - window_s)
            )
            sql = f"SELECT 1 FROM {LEDGER_TABLE} WHERE source=? AND created_at >= ?"
            args: list = [source, cutoff]
            if kinds:
                sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
                args += list(kinds)
            if exclude_kinds:
                sql += " AND kind NOT IN (%s)" % ",".join("?" * len(exclude_kinds))
                args += list(exclude_kinds)
            sql += " LIMIT 1"
            hit = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
        return hit is not None
    except Exception:  # noqa: BLE001
        return False


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


def delivery_suppressed_by_mood(base_dir: Optional[Path] = None) -> bool:
    """情绪门（活感 v2 §三.4）：她低落/烦躁时，主动递出先不递（她不想说）。

    只压「她主动找你聊」的非时效型递出；时效型（系统异常/运维巡检）照常递，
    那不属于她的情绪调度。

    fail-open：情绪引擎不可用 / 被关（``DESKMATE_EMOTION_ENGINE=0``）/ 取数异常
    → 返回 ``False``（照常递，绝不静默丢件）。
    """
    try:
        from deskmate.growth.emotion_engine import EmotionEngine, _is_enabled

        if not _is_enabled():
            return False
        mood = EmotionEngine(base_dir=base_dir).get_mood_label()
        return (mood or {}).get("label") in ("低落", "烦躁")
    except Exception:
        return False


def close_loop_pending_text(base_dir: Optional[Path] = None, *, limit: int = 3, max_age_days: int = 30) -> str:
    """阶段四（2026-10-24）：把 close_loop 降级提案附带到开聊开场白里。

    close_loop_pending.jsonl 原本没有任何递送入口，提案会静默躺着。
    这里只读、只取最近 30 天内 status=proposed 的行（最多 3 条），不改文件、
    不改台账 items；萧盛表态后由对话主助手把对应行清掉（见 deskmate.md 阶段五）。
    """
    try:
        base = _resolve_base(base_dir)
        path = base / "memory" / "close_loop_pending.jsonl"
        if not path.is_file():
            return ""
        now = time.time()
        summaries: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status is not None and status != "proposed":
                continue
            try:
                ts = float(entry.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts and (now - ts) > max_age_days * 86400:
                continue
            summary = str(entry.get("action_summary") or "").strip()
            if summary:
                summaries.append(summary)
        if not summaries:
            return ""
        lines = "\n".join(f"- {s}" for s in summaries[-limit:])
        return (
            "\n另外还有几件上次没做完的事（需你点头我才做，回复做 / 不要 / 稍后即可）：\n"
            + lines
        )
    except Exception:
        return ""


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
    - 时效型（URGENT_SOURCES/URGENT_KINDS）：即时递，不受日上限与相关性约束
      （运维巡检类 OPS_MAILBOX_ONLY_KINDS 除外：信箱收、开场不念，2026-10-10）；
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
    # 情绪门（活感 v2 §三.4）：她低落/烦躁时，非时效型主动递出先不递。
    suppressed = delivery_suppressed_by_mood(base_dir)
    pool = []
    for it in items:
        # 情绪门：她低落/烦躁时压住「她主动找你聊」的递出；系统异常/运维（时效型）
        # 不归她的情绪调度，照常递（避免隐患被无限压下）。
        if suppressed and it.get("source") not in URGENT_SOURCES and it.get("kind") not in URGENT_KINDS:
            continue
        # 空内容守卫（2026-10-07）：content 为空的条目递出去就是一张空卡，
        # 无论 understanding 是否已补，一律不进池（与 record_proactive 同源防线）。
        if not (it.get("content") or "").strip():
            continue
        und = it.get("understanding") or ""
        if und == UNDERSTANDING_FAILED_MARKER:
            it = dict(it)
            it["understanding"] = "（理解加工未完成，仅呈现依据）"
        elif not und:  # 加工未完成 → 按 §11.5 不递，等后处理补上再递
            continue
        else:
            it = dict(it)
        # 遥测（系统运维日志，如 memory_write_error / *_error）不主动念：与信箱准入
        # 同守一道门（_is_telemetry_kind），否则会话注入会把日志原文倒一大坨（2026-10-10）。
        # 条目仍留台账——层一每轮回查、他真问起时仍可被动浮现（不丢声）。
        if _is_telemetry_kind(it.get("kind")):
            continue
        # 运维巡检类（OPS_MAILBOX_ONLY_KINDS，2026-10-10）＝系统简报：信箱收、台账留，
        # 开场白/相关性注入都不念——时效型即时递不覆盖系统简报（二次「弹出来」修正）。
        if it.get("kind") in OPS_MAILBOX_ONLY_KINDS:
            continue
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
        # 阶段六（2026-10-24）：跟进状态之“到期”——待处理项龄期超过 3 天要让萧盛看得见，
        # 超过 7 天建议重新评估或放弃；解析失败则跳过标签，绝不阻断递送。
        age_label = ""
        try:
            if it.get("status") == "pending" and it.get("created_at"):
                _created = time.strptime(str(it["created_at"])[:19], "%Y-%m-%dT%H:%M:%S")
                _age_days = (time.time() - time.mktime(_created)) / 86400.0
                if _age_days > 7:
                    age_label = f"（已等 {int(_age_days)} 天，超期：建议重新评估或放弃）"
                elif _age_days > 3:
                    age_label = f"（已等 {int(_age_days)} 天）"
        except Exception:
            age_label = ""
        lines.append(f"{i}. {it['content']}{ev}{un}{age_label}\n    #{_short_id(it['id'])}")
    text = (
        "我替你盯着这个家，发现几件事还没收：\n"
        + "\n".join(lines)
        + "\n你要先处理哪个？（回复序号或条目 #id 即可）"
    )
    # 阶段四：附带递送 close_loop 旧提案（不改 items、不改 ack 流程）。
    try:
        extra = close_loop_pending_text(base_dir)
        if extra:
            text += extra
    except Exception:
        pass
    # 阶段六：下一步任务直达——每条 chosen 项自带可执行下一步，萧盛回复 S 编号
    # 即可开工，不用再交代一遍；next_steps 字段供前端与主对话取用，前端忽略未知字段兼容。
    ns_steps: list[dict] = []
    try:
        from deskmate.proactivity.next_steps import render_next_steps_section

        ns_text, ns_steps = render_next_steps_section(chosen)
        if ns_text:
            text += ns_text
    except Exception:
        ns_steps = []
    return {"text": text, "items": chosen, "next_steps": ns_steps}


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
    # 他动手处理了（actioned/in_progress）→ 信箱那条教训升级到「他会读也会处理」。
    # fail-open：沉淀失败绝不影响状态流转本身。
    if new_status in ("actioned", "in_progress"):
        try:
            record_mailbox_teaching(base_dir=base)
        except Exception:  # noqa: BLE001
            logger.debug("信箱教学沉淀失败（不阻断）", exc_info=True)
    return True


# ---- 信箱呈现层（2026-10-09）：准入白名单 + 标已读 + 列信 + 未读数 + 已读回流 ----
# 信箱是 ledger 的门面：以下函数只动视图态（read_at），不动 status 机与跨端同步。
# 所有回复内容走同一聊天引擎（见 coworker/server/app.py mailbox 端点），ledger 只持元数据。

# 准入（萧哥 2026-10-09 拍板）：系统遥测是她给自己的**运维日志**，不是递给他的话，
# 不入信箱（实测本机 95 条 pending 里 12 条是 memory_write_error、还有密钥外泄告警），
# 否则信箱变成错误日志倾倒场。这类行仍留在台账里，活跃会话的相关性注入
# （build_handoff_opening(contextual=True)）与层一每轮回查都照常能捞到它们——不丢声。
#
# 口径取「默认入、点名拦」（江河设计 §4.4 自由默认 · 拦是例外）：新 kind 不必登记
# 就能出现在信箱；只有明确列在下面的运维/错误类被挡。命名带 error/failure/alert… 后缀
# 的按遥测处理，是防「以后新增一个 xxx_error 又漏进来」的兜底，不是主判据。
_MAILBOX_DENY_KINDS = frozenset(
    {
        "",
        "none",
        "no_action",
        "memory_write_error",
        "memory_write_failure",
        "timeline_error",
        "hard_anchor_error",
        "incomplete_response",
        "event_timeline_provision",
        "sync_conflict",
        "api_key_exposure_alert",
    }
)
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
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _ensure_table(c)
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET read_at=? WHERE id=?", (now, rid)
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
    base = _resolve_base(base_dir)
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
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def mailbox_feedback_stats(base_dir: Optional[Path] = None, *, days: int = 1) -> dict:
    """读 mailbox_seen.jsonl，统计近 ``days`` 天他真正读了几封、回了信没有。

    返回 ``{"seen_today": n, "replied_today": n, "unanswered_today": n}``。
    「回了」= 该信已流转出 pending/snoozed（他动过手），或 status 到了 actioned/in_progress。
    fail-open：文件不存在/坏行一律跳过，不抛。
    """
    base = _resolve_base(base_dir)
    path = base / "mailbox_seen.jsonl"
    today = _today() if days == 1 else None
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
                _ensure_table(c)
                marks = ",".join("?" * len(set(ids)))
                rows = c.execute(
                    f"SELECT id, status FROM {LEDGER_TABLE} WHERE id IN ({marks})",
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
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    cutoff = ""
    if window_days and window_days > 0:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - window_days * 86400)
        )
    read = handled = 0
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _ensure_table(c)
            rows = c.execute(
                f"SELECT kind, origin, read_at, status, content, acknowledged_at"
                f" FROM {LEDGER_TABLE}"
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
    base = _resolve_base(base_dir)
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
    - ``understanding == UNDERSTANDING_FAILED_MARKER`` 降级成友好文案，**不**把工程标记
      露到界面上；信箱是他自己来拉的，所以不像 handoff 那样因「加工未完成」就不递。
    - 每条附 ``stale``（已读超 ``SHELF_DAYS`` 天仍未处理 = 设计单 §7 的「已搁置」归档态）。
    - ``include_read=False`` 只返未读（未读数口径，同样过准入）。
    """
    base = _resolve_base(base_dir)
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
            _ensure_table(c)
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until,"
                f" read_at, origin, intent_id"
                f" FROM {LEDGER_TABLE} {clause} ORDER BY created_at DESC",
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
        if (it.get("understanding") or "") == UNDERSTANDING_FAILED_MARKER:
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
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    marks = ",".join("?" * len(ARCHIVED_STATUSES))
    try:
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            _ensure_table(c)
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until,"
                f" read_at, origin, intent_id, acknowledged_at"
                f" FROM {LEDGER_TABLE}"
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
        if (it.get("understanding") or "") == UNDERSTANDING_FAILED_MARKER:
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


# ---- §11.16「灵动三层」（2026-10-07）：她递出卡片后要「接得上」----
# 萧哥评审：卡片只是弹层，她递出时自己不知道递了什么；用户点「聊这个」=把她的话
# 当用户消息发回去，她零上下文瞎接。三层补：①每轮注入「今天已递待办」②#id 回查工具
# ③「聊这个」让她说（她先开口）。本组函数为①②供数 + ③的文案组装。

_DELIVERED_CONTEXT_ITEM_MAX = 300  # 单条注入字符上限（防超长 content 吃光上下文预算）
_DELIVERED_CONTEXT_TOTAL_MAX = 1_500  # 整个待办块上限


def delivered_today_items(
    base_dir: Optional[Path] = None, limit: int = 10
) -> list[dict]:
    """今天已递出、且用户尚未终态处理的台账项（delivered_at 倒序）。

    排除 actioned/dismissed：已收口的条目再注入每轮上下文是纯噪声。
    """
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            rows = c.execute(
                f"SELECT id, source, kind, content, evidence, understanding, status,"
                f" confidence, created_at, source_node, delivered_at, snoozed_until"
                f" FROM {LEDGER_TABLE}"
                f" WHERE delivered_at IS NOT NULL AND delivered_at <> ''"
                f" AND substr(delivered_at,1,10)=?"
                f" AND status NOT IN ('actioned','dismissed')"
                f" ORDER BY delivered_at DESC LIMIT ?",
                (_today(), int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # 表/列尚未建（老库）→ 无待办
    keys = ("id", "source", "kind", "content", "evidence", "understanding", "status", "confidence", "created_at", "source_node", "delivered_at", "snoozed_until")
    return [dict(zip(keys, r)) for r in rows]


def render_delivered_context(items: list) -> str:
    """把今天已递待办渲染成每轮注入块（§六.1 承诺的「注入上下文」落地）。

    块内给足 #id + content + 依据 + 我的理解，让她「接话时从这儿展开」；
    细节全文由 proactive_lookup 按 #id 回查（句柄即 _short_id）。
    """
    if not items:
        return ""
    lines = [
        "## 我今天已递给用户的待办",
        "（用户回话若涉及这些，从这儿展开接；要全文细节用 proactive_lookup 按 #id 回查）",
    ]
    total = 0
    for it in items:
        und = (it.get("understanding") or "").strip()
        if und == UNDERSTANDING_FAILED_MARKER:
            und = "（理解加工未完成，仅呈现依据）"
        body = f"- #{_short_id(it['id'])} {it.get('content') or ''}"
        ev = (it.get("evidence") or "").strip()
        if ev:
            body += f"（依据：{ev}）"
        if und:
            body += f"\n  我的理解：{und}"
        if len(body) > _DELIVERED_CONTEXT_ITEM_MAX:
            body = body[: _DELIVERED_CONTEXT_ITEM_MAX] + "…"
        if total + len(body) > _DELIVERED_CONTEXT_TOTAL_MAX:
            lines.append("- …（更多待办用 proactive_lookup 回查）")
            break
        lines.append(body)
        total += len(body)
    return "\n".join(lines)


def lookup_items(
    base_dir: Optional[Path] = None,
    *,
    handle: str = "",
    query: str = "",
    limit: int = 5,
) -> list[dict]:
    """按 #id 短句柄 / 完整 id / 关键词回查台账（§六.2 承诺的「句柄回查」落地）。

    - handle：用户口中的「#a1b2c3」。含 ':' 视为完整 id 前缀；否则按指纹段前缀匹配
      （id = <source>:<kind>:<指纹>，_short_id 取指纹尾 6，故 SQL 候选 + Python 复核
      指纹段 startswith，避免命中 kind 文本误报）。指纹可能是锚点明文也可能是 sha1，
      不做字符集假设。
    - query：content/evidence/understanding 的子串（LIKE）语义检索。
    - 两者都给 → 取交集语义（handle 命中优先，query 再滤）。
    """
    handle = (handle or "").strip().lstrip("#").strip()
    query = (query or "").strip()
    if not handle and not query:
        return []
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            if handle and ":" in handle:
                rows = c.execute(
                    f"SELECT id, source, kind, content, evidence, understanding, status,"
                    f" confidence, created_at, source_node, delivered_at, snoozed_until"
                    f" FROM {LEDGER_TABLE} WHERE id LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (handle + "%", int(limit)),
                ).fetchall()
            elif handle:
                # 候选：指纹段以 handle 开头（前导冒号锚定，拒 kind 文本误命中）
                rows = c.execute(
                    f"SELECT id, source, kind, content, evidence, understanding, status,"
                    f" confidence, created_at, source_node, delivered_at, snoozed_until"
                    f" FROM {LEDGER_TABLE} WHERE id LIKE ? ORDER BY created_at DESC LIMIT 50",
                    ("%:" + handle + "%",),
                ).fetchall()
                rows = [
                    r for r in rows
                    if str(r[0]).rsplit(":", 1)[-1].startswith(handle)
                ][: int(limit)]
            else:
                like = f"%{query}%"
                rows = c.execute(
                    f"SELECT id, source, kind, content, evidence, understanding, status,"
                    f" confidence, created_at, source_node, delivered_at, snoozed_until"
                    f" FROM {LEDGER_TABLE}"
                    f" WHERE content LIKE ? OR evidence LIKE ? OR understanding LIKE ?"
                    f" ORDER BY created_at DESC LIMIT ?",
                    (like, like, like, int(limit)),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
    keys = ("id", "source", "kind", "content", "evidence", "understanding", "status", "confidence", "created_at", "source_node", "delivered_at", "snoozed_until")
    items = [dict(zip(keys, r)) for r in rows]
    if handle and query:
        # 双条件：handle 命中集内再按 query 滤一遍
        items = [
            it for it in items
            if query in (it.get("content") or "")
            or query in (it.get("evidence") or "")
            or query in (it.get("understanding") or "")
        ]
    return items


def mark_delivered(
    base_dir: Optional[Path] = None, rid: Optional[str] = None
) -> bool:
    """补记 delivered_at（她真「说出口」的落会话路径，§11.16 层三）。

    build_handoff_opening 递卡时已记；本函数给「聊这个」等卡片外路径补账——
    交付口径统一走 delivered_at（跨端共享，§11.2③）。今日已记不刷新（保留首次
    交付时间）；终态（actioned/dismissed）不记，delivered_today_items 本就不含它们。
    """
    if rid is None:
        return False
    base = _resolve_base(base_dir)
    db = base / "deskmate.db"
    with sqlite3.connect(str(db), check_same_thread=False) as c:
        try:
            row = c.execute(
                f"SELECT status, delivered_at FROM {LEDGER_TABLE} WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                return False
            status, delivered_at = row
            if status in ("actioned", "dismissed"):
                return False
            if (delivered_at or "")[:10] == _today():
                return True  # 今日已记，幂等
            c.execute(
                f"UPDATE {LEDGER_TABLE} SET delivered_at=? WHERE id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()), rid),
            )
            c.commit()
        except sqlite3.OperationalError:
            return False
    return True


def compose_speak_text(item: dict) -> str:
    """「聊这个」让她先开口的文案组装（server 端定稿，前端不控 transcript 文本）。

    第一人称复述自己递过的条目——她「知道自己在说什么」，用户接上即续聊。
    understanding 为空则省略该行（不出现「我的理解：」空挂）；加工失败标记降级提示。
    """
    content = (item.get("content") or "").strip()
    evidence = (item.get("evidence") or "").strip()
    und = (item.get("understanding") or "").strip()
    if und == UNDERSTANDING_FAILED_MARKER:
        und = "（这条的理解加工没完成，我只能给你看依据）"
    lines = [f"对了，说到这个——{content}"]
    if evidence:
        lines.append(f"（依据：{evidence}）")
    if und:
        lines.append(und)
    lines.append(
        f"（这条是 #{_short_id(item.get('id') or '')}，之后报这个号我就能调出来）"
    )
    return "\n".join(lines)
