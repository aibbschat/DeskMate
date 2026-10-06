"""LLM 通道熔断（设计文档 §3 · 2026-10-06）。

核心不变量（§1.3的教训）：**配额类错误必须立刻停手并记账，禁止原地重试**。
那 28 次同路重试（日志 608 行里 `AccountQuotaExceeded` 刷了 28 次）就是反例。

本测试全程用 /tmp 临时库，不碰活库。
"""
from __future__ import annotations

import datetime
import sqlite3
import time

import pytest

from deskmate.llm_circuit_breaker import (
    KIND_QUOTA,
    KIND_RATE,
    KIND_TRANSIENT,
    KIND_UNAVAILABLE,
    MAX_COOLDOWN_S,
    TABLE,
    classify_error,
    is_open,
    prune_stale,
    record_failure,
    record_success,
)

# 火山真实错误形态（取自 2026-10-06 真实日志原文）
VOLC_QUOTA = (
    "{'error': {'code': 'AccountQuotaExceeded', 'message': 'You have exceeded the "
    "monthly usage quota. It will reset at 2026-10-06 23:59:59 +0800 CST. We recommend "
    "upgrading your plan for more quota, or waiting for the reset.', 'type': "
    "'TooManyRequests'}}"
)


@pytest.fixture
def base(tmp_path):
    return tmp_path


# --------------------------------------------------------------------------- #
# 错误解析（纯函数）
# --------------------------------------------------------------------------- #

def test_volc_quota_parses_exact_reset_time_and_forbids_retry():
    """火山：解析出精确重置时间戳，且**禁止重试**（撞墙不收手是本模块的存在理由）。"""
    now = time.mktime(datetime.datetime(2026, 10, 6, 20, 40).timetuple())
    v = classify_error(status_code=429, body=VOLC_QUOTA, now=now)
    assert v.kind == KIND_QUOTA
    assert v.retryable is False, "配额类错误绝不允许原地重试"
    assert datetime.datetime.fromtimestamp(v.cooldown_until) == datetime.datetime(
        2026, 10, 6, 23, 59, 59
    ), "应解析出错误体里的精确重置时间，而非默认退避"


def test_xiaomi_quota_has_no_reset_time_uses_default():
    """小米：`quota exhausted` 无重置时间 → 走默认退避，仍禁重试。"""
    now = time.time()
    v = classify_error(
        status_code=429,
        body='{"error":{"code":"429","message":"quota exhausted","type":"limitation"}}',
        now=now,
    )
    assert v.kind == KIND_QUOTA and v.retryable is False
    assert v.cooldown_until > now


def test_opencode_429_uses_retry_after_header():
    """opencode：429 + retry-after 头（实测 40332 ≈ 11.2h）→ 按秒数冷却。"""
    now = time.time()
    v = classify_error(status_code=429, body="", retry_after="40332", now=now)
    assert v.kind == KIND_RATE and v.retryable is False
    assert 11 * 3600 < (v.cooldown_until - now) < 12 * 3600


def test_opencode_403_is_long_cooldown_not_retryable():
    """opencode 403 FreeTierError = 匿名层不可用 → 长冷却，不重试。"""
    now = time.time()
    v = classify_error(status_code=403, body="", now=now)
    assert v.kind == KIND_UNAVAILABLE and v.retryable is False
    assert (v.cooldown_until - now) > 3600


def test_upstream_503_is_transient_and_allows_one_retry():
    """上游过载/5xx = 瞬时故障 → 短冷却**且允许一次重试**（与配额类区别对待）。"""
    now = time.time()
    v = classify_error(
        status_code=503,
        body="Streaming response failed: [503] Upstream error from Nvidia: "
             "Service temporarily overloaded",
        now=now,
    )
    assert v.kind == KIND_TRANSIENT and v.retryable is True
    assert (v.cooldown_until - now) <= 120


def test_unknown_error_is_bounded_not_infinite_retry():
    """未知错误：短冷却 + 允许一次重试，**不无限重试**。"""
    v = classify_error(status_code=418, body="??", now=time.time())
    assert v.retryable is True and v.kind == KIND_TRANSIENT


def test_cooldown_is_capped():
    """冷却上限24h：像小米那种无重置时间的通道不能把熔断推到几天之外。"""
    now = time.time()
    v = classify_error(status_code=429, body="", retry_after="999999", now=now)
    assert v.cooldown_until <= now + MAX_COOLDOWN_S + 1


# --------------------------------------------------------------------------- #
# 存储与状态流转
# --------------------------------------------------------------------------- #

def test_record_failure_creates_table_with_sync_triggers(base):
    """记账自动建表 + 三触发器 + 同步三列（进 SYNC_TABLES 才能跨端收敛）。"""
    record_failure("ark:m", classify_error(status_code=429, body=VOLC_QUOTA), base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        trg = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                (f"trg_{TABLE}%",))
        ]
        idx = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
                (f"idx_{TABLE}%",))
        ]
        sync_cols = [
            r[1] for r in c.execute(f"PRAGMA table_info({TABLE})")
            if r[1].startswith("sync_")
        ]
        ops = c.execute(
            "SELECT count(*) FROM _sync_oplog WHERE table_name=?", (TABLE,)
        ).fetchone()[0]
    assert len(trg) == 3, "AI/AU/AD 三触发器齐"
    assert idx, "sync_uuid 唯一索引在"
    assert len(sync_cols) == 3, "sync_uuid/sync_hlc/sync_tombstone 齐"
    assert ops >= 1, "熔断写入必须经触发器进 oplog（跨端同步的前提）"


def test_is_open_lifecycle(base):
    """未熔断→ False；熔断后 True；record_success 后 False。"""
    assert is_open("ark:m", base_dir=base) is False
    record_failure("ark:m", classify_error(status_code=429, body=VOLC_QUOTA), base_dir=base)
    assert is_open("ark:m", base_dir=base) is True
    record_success("ark:m", base_dir=base)
    assert is_open("ark:m", base_dir=base) is False


def test_strike_count_accumulates(base):
    """重复失败累加 strike_count（可以看出「撞了几次墙」）。"""
    for _ in range(3):
        record_failure("ark:m", classify_error(status_code=429, body="quota exhausted"),
                       base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        state, strikes = c.execute(
            f"SELECT state, strike_count FROM {TABLE} WHERE channel_key='ark:m'"
        ).fetchone()
    assert state == "open" and strikes == 3


def test_cooldown_expiry_allows_half_open_retry(base):
    """⚠️ 验收锚点：冷却到期后必须能**再试一次**（half-open）。

    这条曾是一个真bug —— 写入存 ISO 时间串而 `is_open` 用 `float()` 读，
    ValueError → 走 `return True` ⇒ **永久熔断**，配额重置后也恢复不了
    （正是 §1.3 那条债的同类形态）。本例锁死该行为。
    """
    v = classify_error(status_code=429, body="", retry_after="1", now=time.time())
    record_failure("ark:soon", v, base_dir=base)
    assert is_open("ark:soon", base_dir=base) is True, "冷却期内应熔断"
    time.sleep(1.4)
    assert is_open("ark:soon", base_dir=base) is False, (
        "冷却到期后必须放行（允许试一次）——否则配额重置了也恢复不了，"
        "就是 §1.3 那条债换个地方复发"
    )


def test_unparseable_cooldown_fails_closed(base):
    """cooldown_until 读不出来（脏数据/格式被改坏）→ **保守当熔断中**。

    绝不静默当"可用"：宁可少用一条通道，也不无限打已死的路。
    """
    record_failure("ark:dirty", classify_error(status_code=429, body="quota exhausted"),
                   base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        c.execute(f"UPDATE {TABLE} SET cooldown_until='2026-10-06 21:30:00' "
                  "WHERE channel_key='ark:dirty'")
        c.commit()
    assert is_open("ark:dirty", base_dir=base) is True


def test_prune_stale_removes_delisted_models(base):
    """下架模型的记录被清理（防表无限膨胀 + 下架模型不参与轮替）。"""
    record_failure("opencode:gone-free", classify_error(status_code=403), base_dir=base)
    record_failure("ark:keep", classify_error(status_code=429, body="quota exhausted"),
                   base_dir=base)
    removed = prune_stale({"ark:keep"}, base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        left = [r[0] for r in c.execute(f"SELECT channel_key FROM {TABLE}")]
    assert removed == 1 and left == ["ark:keep"]


def test_audit_written_locally(base):
    """每次熔断/关闭都留一行审计（本地簿记，不同步）。"""
    record_failure("ark:m", classify_error(status_code=429, body=VOLC_QUOTA), base_dir=base)
    record_success("ark:m", base_dir=base)
    lines = (base / "llm_channel_audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 2
    assert '"event": "open"' in lines[0]
    assert '"event": "close"' in lines[1]
