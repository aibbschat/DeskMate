"""主动产出管道 · 台账层 单测（P0 正确性验证）。

覆盖：
- 确定性 id 合并（同锚点/同内容 → 同 id，对应 48 条身份分裂教训）
- status 只前进不后退（跨端收敛，防反 nag 失效）
- 本地对话式对接（pending → 开场白；ack 后不再递）

测试一律用 /tmp 副本库，不碰活库。
"""
from __future__ import annotations

import pytest
import sqlite3
import tempfile
import time
import unittest.mock
from pathlib import Path

from deskmate.memory.sync_daemon import (
    HLC,
    Op,
    Record,
    SyncEngine,
    _lww_winner,
    _status_advances,
)
from deskmate.memory.sync_store_sqlite import (
    SQLiteOplogStore,
    SQLiteSyncStore,
    migrate,
)
from deskmate.proactivity.ledger import (
    STATUS_ORDER,
    acknowledge_ledger,
    build_handoff_opening,
    pending_items,
    record_proactive,
    _relevance_score,
)


def _fresh_base(node: str) -> Path:
    """建临时基目录 + deskmate.db 并建表+迁移 proactive_ledger，返回 base 目录。

    migrate 只处理「已存在」的表，故先经 ledger._ensure_table 自举建表
    （CREATE + 加同步列 + 触发器），再让 migrate 接管同步列与触发器。
    """
    base = Path(tempfile.mkdtemp(prefix="pt_proactive_"))
    db = base / "deskmate.db"
    conn = sqlite3.connect(str(db))
    from deskmate.proactivity.ledger import _ensure_table

    _ensure_table(conn)
    conn.close()
    return base


def _store(db: str) -> SQLiteSyncStore:
    return SQLiteSyncStore(sqlite3.connect(db, check_same_thread=False), "proactive_ledger")


def _rec(status: str, hlc_s: str, tombstone: bool = False) -> Record:
    return Record(
        record_id="rid1",
        data={"status": status, "content": "c"},
        hlc=HLC.from_str(hlc_s),
        tombstone=tombstone,
        valid_until=None,
    )


def _status_of(db: str, rid: str) -> str:
    with sqlite3.connect(db, check_same_thread=False) as c:
        r = c.execute(
            "SELECT status FROM proactive_ledger WHERE id=?", (rid,)
        ).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------------------
# 1. 确定性 id 合并
# ---------------------------------------------------------------------------
def test_deterministic_id_merges_same_anchor():
    from deskmate.proactivity.ledger import _deterministic_id

    # 同锚点、不同 content → 同 id（合并，不重复）
    rid1 = _deterministic_id("cloud_audit", "plot_contradiction", "ch7", "内容A")
    rid2 = _deterministic_id("cloud_audit", "plot_contradiction", "ch7", "内容B")
    assert rid1 == rid2
    # 无锚点、同 content → 同 id
    rid3 = _deterministic_id("cloud_audit", "plot_contradiction", None, "内容A")
    rid4 = _deterministic_id("cloud_audit", "plot_contradiction", None, "内容A")
    assert rid3 == rid4
    # 不同锚点 → 不同 id
    assert rid1 != rid3


# ---------------------------------------------------------------------------
# 2. 落库 + 本地对接
# ---------------------------------------------------------------------------
def test_record_and_handoff():
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base,
        source="cloud_audit",
        kind="plot_contradiction",
        content="伏笔X悬了9节",
        evidence="第七章",
        understanding="这条线不收，读者会以为你忘了前面的铺垫。",
        anchor="ch7",
        confidence=0.9,
    )
    items = pending_items(base)
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    # P0.5：pending_items 投影须带 source_node（§六展示「哪端产出」、§七跨端日上限计数）
    assert "source_node" in items[0]
    assert items[0]["source_node"] == "mac"
    opening = build_handoff_opening(base)
    assert opening is not None
    assert "伏笔X悬了9节" in opening["text"]
    # P1 守卫：已理解项正常递出（含「我的理解」行）
    assert "我的理解" in opening["text"]
    # ack 后不再 pending（不重复递）
    rid = items[0]["id"]
    assert acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert pending_items(base) == []


def test_handoff_skips_ununderstood():
    """P1 守卫（§11.5）：understanding 仍为空（加工未完成）的 pending 暂不递，避免退化念原文。"""
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线Y春秋对不上", evidence="第3章", confidence=0.9,  # 不传 understanding → 空
    )
    assert build_handoff_opening(base) is None  # 守卫：未理解不递


def test_handoff_degrades_failed_marker():
    """P1 守卫（§11.5）：understanding_failed 标记降级递，并转友好文案。"""
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线Y春秋对不上", evidence="第3章", confidence=0.9,
    )
    from deskmate.proactivity.ledger import UNDERSTANDING_FAILED_MARKER
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            (UNDERSTANDING_FAILED_MARKER, rid),
        )
    opening = build_handoff_opening(base)
    assert opening is not None  # 降级递（不当异常丢件）
    assert "时间线Y春秋对不上" in opening["text"]
    assert "理解加工未完成" in opening["text"]  # 标记转友好文案


def test_handoff_contextual_relevant_hit():
    """本轮（§11.4② 触发入口）：活跃会话传相关 session_context → 命中并递出对应待办。"""
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝慢性中毒悬了9节未收", evidence="第9-17章", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            ("这条线要收一下，前面伏笔读者会记得。", rid),
        )
    # 会话上下文命中「康得宝」关键词 → 相关型档取中、contextual 只递 fresh_new
    opening = build_handoff_opening(
        base, session_context="我想接着写康得宝中毒那条线", contextual=True
    )
    assert opening is not None
    ids = [it["id"] for it in opening["items"]]
    assert rid in ids


def test_handoff_contextual_no_relevance_none():
    """本轮（§11.4②）：会话上下文与待办零关联 → 不递（攒着等被聊到），静默返回 None。"""
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝慢性中毒悬了9节未收", evidence="第9-17章", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            ("这条线要收一下。", rid),
        )
    # 会话在聊完全无关的主题 → 零重叠 → 不递
    opening = build_handoff_opening(
        base, session_context="今天天气真好适合出门散步", contextual=True
    )
    assert opening is None


def test_handoff_contextual_excludes_already_delivered():
    """本轮（§11.4②）：今日已递过的条目不进 contextual 返回集（不追重首页/之前会话）。"""
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝慢性中毒悬了9节未收", evidence="第9-17章", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            ("这条线要收一下。", rid),
        )
    # 先经一次带上下文的递出 → 打上 delivered_at（今日）
    first = build_handoff_opening(
        base, session_context="我想接着写康得宝中毒那条线", contextual=True
    )
    assert first is not None and rid in [it["id"] for it in first["items"]]
    # 再次同类上下文 → 该条已 delivered 今日，contextual 只取 fresh_new → 不重复递
    second = build_handoff_opening(
        base, session_context="康得宝这条线怎么收尾", contextual=True
    )
    assert second is None


def test_backfill_fills_understanding():
    """P1：maybe_backfill_understanding 调 LLM 把空 understanding 填上，handoff 随后能递出。"""
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章", confidence=0.9,
    )
    assert pending_items(base)[0]["understanding"] == ""
    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch(
             "deskmate.memory.llm.chat",
             return_value="这条线要收一下，否则读者以为你忘了前面的伏笔。",
         ):
        from deskmate.proactivity.understanding import maybe_backfill_understanding
        n = maybe_backfill_understanding(base)
    assert n == 1
    assert pending_items(base)[0]["understanding"] == "这条线要收一下，否则读者以为你忘了前面的伏笔。"
    opening = build_handoff_opening(base)
    assert opening is not None and "康得宝线" in opening["text"]


def test_backfill_no_llm_leaves_empty():
    """P1：无 LLM key 时不标 failed、留空等下轮（避免永久失败）。"""
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线矛盾", evidence="第3章", confidence=0.9,
    )
    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=False):
        from deskmate.proactivity.understanding import maybe_backfill_understanding
        n = maybe_backfill_understanding(base)
    assert n == 0
    assert pending_items(base)[0]["understanding"] == ""  # 留空等下轮，不标 failed
    assert build_handoff_opening(base) is None  # 守卫：未理解不递


def test_backfill_marks_failed_on_error():
    """P1：有 key 但 LLM 调用失败 → 标 understanding_failed 降级递。"""
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线矛盾", evidence="第3章", confidence=0.9,
    )
    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch("deskmate.memory.llm.chat", return_value=None):
        from deskmate.proactivity.understanding import maybe_backfill_understanding
        n = maybe_backfill_understanding(base)
    assert n == 0
    assert pending_items(base)[0]["understanding"] == "[understanding_failed]"
    opening = build_handoff_opening(base)
    assert opening is not None and "理解加工未完成" in opening["text"]


def test_no_anchor_distinct_content_distinct_rows():
    """P0.5：tick 不再传 anchor → 同 (source,kind) 下不同 content 各成一行（不过合并）。

    直接修复「anchor=suggestion_type 致同类型恒为单行、新发现被旧 status 吞掉」。
    同 content 仍合并（保 48 分裂防护，对应确定性 id 血泪教训）。
    """
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="plot_contradiction",
        content="伏笔A 悬了9节", evidence="第七章", confidence=0.9,
    )
    record_proactive(
        base_dir=base, source="cloud_audit", kind="plot_contradiction",
        content="伏笔B 人物动机矛盾", evidence="第九章", confidence=0.9,
    )
    items = pending_items(base)
    assert len(items) == 2  # 不过合并，两条独立 #id
    # 相同 content 二次写入 → 合并为一行（保 48 分裂防护）
    record_proactive(
        base_dir=base, source="cloud_audit", kind="plot_contradiction",
        content="伏笔A 悬了9节", evidence="第七章（复核）", confidence=0.9,
    )
    items2 = pending_items(base)
    assert len(items2) == 2
    a = [i for i in items2 if i["content"] == "伏笔A 悬了9节"][0]
    assert "复核" in a["evidence"]  # 合并行 evidence 被刷新


def test_acknowledge_no_downgrade():
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线Y春秋对不上", evidence="第3章", anchor="tlY", confidence=0.9,
    )
    # 先 actioned，再尝试 dismiss→ 允许（同终态）；尝试回 pending → 拒绝
    acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert not acknowledge_ledger(base, rid=rid, new_status="pending")  # 降级被拒
    assert _status_of(str(base / "deskmate.db"), rid) == "actioned"


# ---------------------------------------------------------------------------
# 3. status 只前进（纯函数 + LWW）
# ---------------------------------------------------------------------------
def test_status_advances_pure():
    assert _status_advances("pending", "actioned", STATUS_ORDER) is True
    assert _status_advances("actioned", "pending", STATUS_ORDER) is False
    assert _status_advances("pending", "pending", STATUS_ORDER) is False
    assert _status_advances("pending", "unknown", STATUS_ORDER) is False


def test_lww_winner_status_priority():
    ordered = STATUS_ORDER
    # actioned（hlc 早） vs pending（hlc 晚）：status 优先 → actioned 胜
    a_old = _rec("actioned", "0000000000000001.00000000.mac")
    b_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(a_old, b_new, ordered) is a_old

    # 同 status → 退回 hlc LWW
    c_old = _rec("pending", "0000000000000001.00000000.mac")
    d_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(c_old, d_new, ordered) is d_new

    # 无 ordered_status → 纯 hlc
    e_old = _rec("actioned", "0000000000000001.00000000.mac")
    f_new = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(e_old, f_new) is f_new  # 默认无 status 优先 → hlc 晚者胜


# ---------------------------------------------------------------------------
# 4. _apply_one_to 集成：跨端 status 只前进
# ---------------------------------------------------------------------------
def _payload(status: str) -> dict:
    return {
        "id": "rid1", "source": "cloud_audit", "kind": "plot_contradiction",
        "content": "c", "evidence": "", "understanding": "", "payload": "{}",
        "source_node": "mac", "status": status, "confidence": 0.9,
        "created_at": "t", "acknowledged_at": "",
    }


def test_apply_one_to_status_forward_no_downgrade():
    """本端 actioned（hlc 新）不被对端迟到/更旧的 pending 覆盖。"""
    base = _fresh_base("mac")
    st = _store(str(base / "deskmate.db"))
    eng = SyncEngine(st, SQLiteOplogStore(sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False)), "mac", "proactive_ledger")
    # 本地写 actioned（hlc 较新）
    eng.local_upsert("proactive_ledger", "rid1", _payload("actioned"))
    # 对端迟到 pending（hlc 更早）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    op_pending = Op("proactive_ledger", "rid1", "upsert", old_hlc, _payload("pending"), origin="cloud")
    eng.apply_remote([op_pending])
    assert _status_of(str(base / "deskmate.db"), "rid1") == "actioned"


def test_apply_one_to_status_advances():
    """本端 pending（hlc 较旧）被对端来的新 actioned 覆盖（前进）。"""
    base = _fresh_base("cloud")
    st = _store(str(base / "deskmate.db"))
    eng = SyncEngine(st, SQLiteOplogStore(sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False)), "cloud", "proactive_ledger")
    # 本地写 pending（hlc 较旧）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    st.upsert("rid1", _payload("pending"), old_hlc)  # 直写，绕过 oplog
    # 对端来的 actioned（hlc 新）
    new_hlc = HLC.from_str("0000000000000099.00000000.mac")
    op_actioned = Op("proactive_ledger", "rid1", "upsert", new_hlc, _payload("actioned"), origin="mac")
    eng.apply_remote([op_actioned])
    assert _status_of(str(base / "deskmate.db"), "rid1") == "actioned"


def test_cross_node_status_converge():
    """双向同步后两端都收敛到 actioned（不降级、不分歧）。"""
    base_mac = _fresh_base("mac")
    base_cloud = _fresh_base("cloud")
    db_mac = str(base_mac / "deskmate.db")
    db_cloud = str(base_cloud / "deskmate.db")
    # mac 本地写 actioned（hlc 新）
    eng_mac = SyncEngine(_store(db_mac), SQLiteOplogStore(sqlite3.connect(db_mac, check_same_thread=False)), "mac", "proactive_ledger")
    eng_mac.local_upsert("proactive_ledger", "rid1", _payload("actioned"))
    # cloud 本地写 pending（hlc 旧，模拟更早产生但迟到）
    old_hlc = HLC.from_str("0000000000000000.00000000.cloud")
    _store(db_cloud).upsert("rid1", _payload("pending"), old_hlc)
    # cloud 的迟到 pending apply → mac（应保留 actioned）
    eng_mac.apply_remote([Op("proactive_ledger", "rid1", "upsert", old_hlc, _payload("pending"), origin="cloud")])
    assert _status_of(db_mac, "rid1") == "actioned"
    # mac 的 actioned op apply → cloud（应前进到 actioned）
    eng_cloud = SyncEngine(_store(db_cloud), SQLiteOplogStore(sqlite3.connect(db_cloud, check_same_thread=False)), "cloud", "proactive_ledger")
    eng_cloud.apply_remote(eng_mac.oplog.since(0))
    assert _status_of(db_cloud, "rid1") == "actioned"


def test_ensure_table_runs_migrate_once_per_db():
    """_ensure_table 幂等记忆：同一库第二次调用不再跑 migrate（防重复 DDL 持写锁）。

    云端审计循环每小时跑、逐条 record_proactive —— migrate() 每次都会 DROP 三触发器
    再重建 + commit，逐条重建是纯浪费。这里用计数替身锁住「只跑一次」。
    """
    import deskmate.memory.sync_store_sqlite as sync_sql
    import deskmate.proactivity.ledger as ledger_mod

    base = _fresh_base("mac")
    db = base / "deskmate.db"

    calls = {"n": 0}
    real_migrate = sync_sql.migrate

    def counting_migrate(conn, node, tables=None):
        calls["n"] += 1
        return real_migrate(conn, node, tables)

    with unittest.mock.patch.object(ledger_mod, "_ensured", set()), \
         unittest.mock.patch.object(sync_sql, "migrate", counting_migrate):
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            ledger_mod._ensure_table(c)
        assert calls["n"] == 1, "首次必须自举（建表 + migrate）"
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            ledger_mod._ensure_table(c)
        assert calls["n"] == 1, f"同库第二次不应再跑 migrate，实际又跑了 {calls['n'] - 1} 次"
        # 表确实在（不是靠标志骗过）
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            assert c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_ledger'"
            ).fetchone() is not None


def test_ensure_table_recreates_after_table_dropped():
    """库被外部重建（表没了）时，即便标志还在也必须重新自举 —— 不能凭标志跳过建表。"""
    import deskmate.proactivity.ledger as ledger_mod

    base = _fresh_base("mac")
    db = base / "deskmate.db"

    with unittest.mock.patch.object(ledger_mod, "_ensured", set()):
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            ledger_mod._ensure_table(c)
            c.execute("DROP TABLE proactive_ledger")
            c.commit()
        # 第二次：表已被删 → 必须重新建出来
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            ledger_mod._ensure_table(c)
            assert c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_ledger'"
            ).fetchone() is not None, "表被删后必须重建，不能凭 _ensured 标志跳过"


# ---------------------------------------------------------------------------
# 5. 修①：去重合并不清空 understanding / 不刷 created_at（§11.11 缺陷1）
# ---------------------------------------------------------------------------
def test_rereemit_preserves_understanding_and_created_at():
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章", confidence=0.9,
    )
    # 模拟后处理补上的理解
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            ("这条线要收一下。", rid),
        )
        c.commit()
        before = c.execute(
            "SELECT created_at FROM proactive_ledger WHERE id=?", (rid,)
        ).fetchone()[0]
    # 同内容再产出（§11.2① 双端同结论双写是必然事件）：不传 understanding
    record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章（复核）", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        row = c.execute(
            "SELECT understanding, evidence, created_at FROM proactive_ledger WHERE id=?",
            (rid,),
        ).fetchone()
    assert row[0] == "这条线要收一下。"  # 理解保留（不再被 "" 清空、不再同步抹掉对端）
    assert "复核" in row[1]  # evidence 仍刷新（合并更新非 status 字段）
    assert row[2] == before  # created_at 不刷新（日上限计数/排序不被重置）
    # 显式提供新理解 → 覆盖（生产者自带理解的通路）
    record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章",
        understanding="新理解", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        assert (
            c.execute("SELECT understanding FROM proactive_ledger WHERE id=?", (rid,)).fetchone()[0]
            == "新理解"
        )
    opening = build_handoff_opening(base)
    assert opening is not None and "我的理解" in opening["text"]


# ---------------------------------------------------------------------------
# 6. 修④：写入门槛 + 证据截断（§七/§11.7③）
# ---------------------------------------------------------------------------
def test_write_gate_blocks_low_confidence_and_empty_evidence():
    base = _fresh_base("mac")
    rid1 = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="低置信发现", evidence="第3章", confidence=0.5,
    )
    assert rid1 == ""  # 低置信不落台账（只进 prepare）
    rid2 = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="无依据发现", evidence=" ", confidence=0.9,
    )
    assert rid2 == ""  # evidence 不明确不落台账
    assert pending_items(base) == []
    rid3 = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="过门槛发现", evidence="第3章", confidence=0.75,
    )
    assert rid3 != ""
    assert len(pending_items(base)) == 1


def test_evidence_truncated_at_500():
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="mobile_voice", kind="idea_capture",
        content="灵感", evidence="长" * 2000, confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        ev = c.execute("SELECT evidence FROM proactive_ledger").fetchone()[0]
    assert len(ev) == 500  # §11.6：oplog payload 每份复制，必须截断


# ---------------------------------------------------------------------------
# 7. 修④：每日交付上限 + 幂等重现 + 次日补位（§七/§11.2③）
# ---------------------------------------------------------------------------
def test_daily_cap_reshow_and_tomorrow_refill():
    base = _fresh_base("mac")
    for i in range(5):
        record_proactive(
            base_dir=base, source="cloud_audit", kind="timeline_error",
            content=f"发现{i}", evidence=f"第{i}章", confidence=0.9,
            understanding=f"第{i}条的理解。",
        )
    op1 = build_handoff_opening(base)
    assert op1 is not None and len(op1["items"]) == 3  # 日上限
    assert "#" in op1["text"]  # #id 句柄在开场白里
    ids1 = {it["id"] for it in op1["items"]}
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        marked = {
            r[0]
            for r in c.execute(
                "SELECT id FROM proactive_ledger"
                " WHERE delivered_at IS NOT NULL AND delivered_at <> ''"
            )
        }
    assert marked == ids1  # 交付标记已写（跨端共享的日上限计数凭证）
    # 同日重开首页：幂等呈现同样 3 条，不追加（不追弹）
    op2 = build_handoff_opening(base)
    assert op2 is not None and {it["id"] for it in op2["items"]} == ids1
    # 处理掉这 3 条 → 剩余 2 条今天没额度（超额安静攒到次日）
    for rid in ids1:
        assert acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert build_handoff_opening(base) is None
    # 模拟次日：交付标记拨回过去 → 额度重置，剩余条目递出
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET delivered_at=? WHERE delivered_at IS NOT NULL",
            ("2000-01-01T00:00:00",),
        )
        c.commit()
    op4 = build_handoff_opening(base)
    assert op4 is not None and len(op4["items"]) == 2
    assert not (ids1 & {it["id"] for it in op4["items"]})


# ---------------------------------------------------------------------------
# 8. 修⑥：snoozed 轨道（稍后 → 退避 → 到期复活 → 可流转）
# ---------------------------------------------------------------------------
def test_snoozed_lifecycle():
    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="稍后再说的发现", evidence="第3章", confidence=0.9,
        understanding="这条可以缓缓。",
    )
    assert build_handoff_opening(base) is not None
    assert acknowledge_ledger(base, rid=rid, new_status="snoozed")
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        st, until = c.execute(
            "SELECT status, snoozed_until FROM proactive_ledger WHERE id=?", (rid,)
        ).fetchone()
    assert st == "snoozed" and until and until > time.strftime("%Y-%m-%dT%H:%M:%S")
    assert build_handoff_opening(base) is None  # 退避期内不递
    # 到期复活（读路径判定，status 保持 snoozed 可审计）
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET snoozed_until=? WHERE id=?",
            ("2000-01-01T00:00:00", rid),
        )
        c.commit()
    op = build_handoff_opening(base)
    assert op is not None and op["items"][0]["id"] == rid
    # 复活后正常流转：snoozed → acknowledged → actioned；不可降回 pending/被终态翻案
    assert acknowledge_ledger(base, rid=rid, new_status="acknowledged")
    assert not acknowledge_ledger(base, rid=rid, new_status="pending")
    assert acknowledge_ledger(base, rid=rid, new_status="actioned")
    assert not acknowledge_ledger(base, rid=rid, new_status="snoozed")


def test_lww_snoozed_beats_pending():
    """修⑥：远端迟到的 pending 不得覆盖已 snoozed 的条目（反 nag）。"""
    a = _rec("snoozed", "0000000000000001.00000000.mac")
    b = _rec("pending", "0000000000000002.00000000.cloud")
    assert _lww_winner(a, b, STATUS_ORDER) is a
    # 用户处理结果压过「稍后」：acknowledged 胜 snoozed（跨端 ack 不被别端稍后锁死）
    c2 = _rec("acknowledged", "0000000000000001.00000000.mac")
    assert _lww_winner(c2, a, STATUS_ORDER) is c2


# ---------------------------------------------------------------------------
# 9. 修⑤：基线对账带 ordered_status（§11.2② 补账）
# ---------------------------------------------------------------------------
def test_baseline_reconcile_ordered_status():
    from deskmate.memory.sync_daemon import reconcile_baseline

    base_mac = _fresh_base("mac")
    base_cloud = _fresh_base("cloud")
    st_mac = _store(str(base_mac / "deskmate.db"))
    st_cloud = _store(str(base_cloud / "deskmate.db"))
    # mac：actioned（hlc 旧）；cloud：pending（hlc 新）——纯 LWW 会翻车，ordered 必须 actioned 胜
    st_mac.upsert("rid1", _payload("actioned"), HLC.from_str("0000000000000001.00000000.mac"))
    st_cloud.upsert("rid1", _payload("pending"), HLC.from_str("0000000000000002.00000000.cloud"))
    rep = reconcile_baseline(st_mac, st_cloud, "mac", STATUS_ORDER)
    assert rep["resolved"] >= 1
    assert _status_of(str(base_mac / "deskmate.db"), "rid1") == "actioned"
    assert _status_of(str(base_cloud / "deskmate.db"), "rid1") == "actioned"


# ---------------------------------------------------------------------------
# 10. 修⑨：相关性三档分流（§11.4）
# ---------------------------------------------------------------------------
def test_relevance_tiers():
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章 康得宝", confidence=0.9,
        understanding="这条线要收。",
    )
    record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="春秋历法换算错", evidence="第3章 历法", confidence=0.9,
        understanding="时间线对不上。",
    )
    record_proactive(
        base_dir=base, source="sync_health", kind="drift",
        content="云端 oplog 漂移超阈值", evidence="窗口 17:02 NOT_ALIGNED", confidence=0.9,
        understanding="同步坏了得看。",
    )
    op = build_handoff_opening(base, session_context="我想接着写康得宝中毒那条线")
    assert op is not None
    contents = [it["content"] for it in op["items"]]
    assert any("康得宝" in c for c in contents)  # 相关型：命中上下文
    assert any("漂移" in c for c in contents)  # 时效型：即时递
    assert not any("春秋" in c for c in contents)  # 无关的攒着（等被聊到再递）


def test_relevance_bigram_primary_bge_high_confidence():
    """P3-b 校准锁定（证据级）：字面主闸 + BGE 仅高置信补充的防骚扰底线。

    - 共享字面锚点（章/人物名）→ 主闸命中，必 > 0（不受 embedding 版本漂移影响）；
    - 同书不同线（BGE 约 0.45，低于 BGE_SEMANTIC_PASS=0.5）→ 视作无关（攒着）；
    - 完全无关（天气）→ 0。
    防止 BGE 无关地板（~0.25–0.31）把无关项误判为相关、退化为「按时间念台账」骚扰。
    """
    item_anchor = {
        "kind": "missing_foreshadow",
        "content": "康得宝慢性中毒悬了9节未收",
        "evidence": "第9-17章",
        "understanding": "这条线要收",
    }
    # 共享字面锚点（康得宝）→ 主闸命中
    score_anchor = _relevance_score(item_anchor, "我想接着写康得宝中毒那条线")
    assert score_anchor > 0.0, "共享字面锚点应判相关"

    # 同书不同线（春秋历法 vs 康得宝上下文）→ BGE < 高置信门槛 → 判无关
    item_diff_thread = {
        "kind": "timeline_error",
        "content": "春秋历法换算错",
        "evidence": "第3章 历法",
        "understanding": "时间线对不上",
    }
    score_diff = _relevance_score(item_diff_thread, "我想接着写康得宝中毒那条线")
    assert score_diff == 0.0, "同书不同线应判无关（BGE 地板不得污染）"

    # 完全无关（天气）→ 0
    score_unrelated = _relevance_score(item_anchor, "今天天气真好适合出门散步")
    assert score_unrelated == 0.0, "完全无关应判 0"


# ---------------------------------------------------------------------------
# 11. 修⑧：生产者注册表（§四/§九）
# ---------------------------------------------------------------------------
def test_producer_registry():
    base = _fresh_base("mac")
    from deskmate.memory.sync_store_sqlite import SYNC_TABLES
    from deskmate.proactivity.ledger import PRODUCERS, register_producer, run_producer

    spec = SYNC_TABLES["proactive_ledger"]
    assert "delivered_at" in spec["business_cols"] and "snoozed_until" in spec["business_cols"]
    assert "snoozed" in spec["ordered_status"]

    @register_producer("test_src")
    def _p(kind="k", content="c", evidence="e", confidence=0.9):
        return [{"kind": kind, "content": content, "evidence": evidence, "confidence": confidence}]

    assert "test_src" in PRODUCERS
    n = run_producer(
        "test_src", ctx={"content": "注册表产出", "evidence": "依据"}, base_dir=base
    )
    assert n == 1
    items = pending_items(base)
    assert items and items[0]["source"] == "test_src"
    assert run_producer("no_such_source") == 0  # 未登记来源 → 0，不炸
    PRODUCERS.pop("test_src", None)


# ---------------------------------------------------------------------------
# 12. 修⑦：understanding 按来源自适应 prompt + failed 重试窗
# ---------------------------------------------------------------------------
def test_backfill_source_adaptive_prompt():
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="mobile_voice", kind="idea_capture",
        content="想写一个在沙漠开咖啡馆的故事", evidence="手机端 2026-10-06 15:30", confidence=0.9,
    )
    captured = {}

    def fake_chat(**kw):
        captured.update(kw)
        return "这个点子可以往世界观上延伸。"

    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch("deskmate.memory.llm.chat", side_effect=fake_chat):
        from deskmate.proactivity.understanding import maybe_backfill_understanding

        assert maybe_backfill_understanding(base) == 1
    sys_prompt = captured.get("system", "")
    assert "【来源说明】" in sys_prompt and "灵感" in sys_prompt  # 按来源换了口吻
    assert pending_items(base)[0]["understanding"] == "这个点子可以往世界观上延伸。"


def test_backfill_uses_grounding_context():
    """§5.2：理解加工把 worldview/记忆/圣经 接进 prompt（反凭空编）。

    模拟三路上下文（mock _gather_grounding），断言它们进了发给 LLM 的 user prompt，
    同时原事项/证据仍在。fail-open：mock 掉即不碰活库/不真搜。
    """
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="missing_foreshadow",
        content="康得宝线 9-17 章无复现", evidence="第9-17章", confidence=0.9,
    )
    captured = {}

    def fake_chat(**kw):
        captured.update(kw)
        return "结合她看重事实准确的世界观，这条线得收。"

    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch("deskmate.memory.llm.chat", side_effect=fake_chat), \
         unittest.mock.patch(
            "deskmate.proactivity.understanding._gather_grounding",
            return_value=(
                "### 她的世界观\n看清谁在争比站队重要。\n"
                "### 相关记忆\n萧哥强调伏笔必收。"
            ),
        ):
        from deskmate.proactivity.understanding import maybe_backfill_understanding

        assert maybe_backfill_understanding(base) == 1
    user_prompt = captured.get("user", "")
    assert "康得宝" in user_prompt            # 原事项仍在
    assert "看清谁在争" in user_prompt        # worldview 进了 prompt
    assert "伏笔必收" in user_prompt          # 相关记忆进了 prompt
    assert pending_items(base)[0]["understanding"] == "结合她看重事实准确的世界观，这条线得收。"


def test_backfill_no_grounding_when_empty():
    """§5.2：三路上下文全空时仍正常加工（fail-open，不阻断）。"""
    base = _fresh_base("mac")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线矛盾", evidence="第3章", confidence=0.9,
    )
    captured = {}

    def fake_chat(**kw):
        captured.update(kw)
        return "时间线得对一下。"

    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch("deskmate.memory.llm.chat", side_effect=fake_chat), \
         unittest.mock.patch(
            "deskmate.proactivity.understanding._gather_grounding", return_value=""
        ):
        from deskmate.proactivity.understanding import maybe_backfill_understanding

        assert maybe_backfill_understanding(base) == 1
    user_prompt = captured.get("user", "")
    assert "时间线矛盾" in user_prompt
    assert "你已有的上下文" not in user_prompt  # 空上下文不附加段落


def test_failed_retry_after_cooldown():
    from deskmate.proactivity.ledger import UNDERSTANDING_FAILED_MARKER

    base = _fresh_base("mac")
    rid = record_proactive(
        base_dir=base, source="cloud_audit", kind="timeline_error",
        content="时间线矛盾", evidence="第3章", confidence=0.9,
    )
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute(
            "UPDATE proactive_ledger SET understanding=? WHERE id=?",
            (UNDERSTANDING_FAILED_MARKER, rid),
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS proactivity_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        c.execute(
            "INSERT OR REPLACE INTO proactivity_meta VALUES ('last_failed_retry_at', ?)",
            (str(time.time() - 2 * 86400),),  # 冷却已过（2 天前）
        )
        c.commit()
    with unittest.mock.patch("deskmate.memory.llm.llm_available", return_value=True), \
         unittest.mock.patch("deskmate.memory.llm.chat", return_value="重试成功了。"):
        from deskmate.proactivity.understanding import maybe_backfill_understanding

        assert maybe_backfill_understanding(base) == 1
    assert pending_items(base)[0]["understanding"] == "重试成功了。"  # 不再永久钉在降级文案


def test_daemon_reconcile_baseline_rejects_multi_table_engine():
    """方法壳 SyncDaemon.reconcile_baseline 在多表引擎下必须 fail-fast 报可操作提示。

    2026-10-06 巡检发现：多表引擎（SyncEngine 传 {table: store}）会把 self.store
    置None（"多表不暴露单一 store"），而修⑤ 给方法壳加的
    `getattr(self.engine.store, "_spec", {})` 会抛
    `AttributeError: 'NoneType' object has no attribute 'items'`。
    生产基线走reconcile_baseline_oplog（逐表），本方法壳无生产调用方，
    但它是个地雷：谁调谁炸，且报错完全指不到根因。
    """
    from deskmate.memory.sync_daemon import MemoryStore, SyncDaemon, SyncEngine

    # 用 _fresh_base（record_proactive 固定写 <base>/deskmate.db，名字不能自造）
    base = _fresh_base("mac")
    db = str(base / "deskmate.db")
    record_proactive(
        base_dir=base, source="cloud_audit", kind="k", content="c", evidence="e",
        confidence=0.9,
    )
    store = _store(db)
    oplog = SQLiteOplogStore(sqlite3.connect(db, check_same_thread=False))
    # 多表引擎形态（runner 生产形态：stores dict）
    multi = SyncEngine({"proactive_ledger": store}, oplog, "mac")
    assert multi.store is None, "多表引擎下 store 应为 None（构造器显式置空）"
    with pytest.raises(RuntimeError, match="只支持单表引擎"):
        SyncDaemon(multi, transport=None).reconcile_baseline(MemoryStore())

    # 单表引擎形态：正常路径不应抛
    single = SyncEngine(
        store, SQLiteOplogStore(sqlite3.connect(db, check_same_thread=False)), "mac"
    )
    assert single.store is not None
    SyncDaemon(single, transport=None).reconcile_baseline(MemoryStore())


def test_failed_retry_cooldown_is_60min_not_24h():
    """failed 重试窗必须是 60min（文档 §11.12 修⑦ 口径），不是 24h。

    火山 Coding Plan 限额通常 1~2 小时恢复；写成 24h 会让降级文案钉住一整天——
    额度恢复了也不翻，这正是「绑死单一 provider」在代码里的第一处真实伤害。
    """
    from deskmate.proactivity import understanding as U

    assert U._FAILED_RETRY_COOLDOWN_S == 60 * 60, (
        f"failed 重试窗应为 3600s（60min），实际 {U._FAILED_RETRY_COOLDOWN_S}s"
    )
