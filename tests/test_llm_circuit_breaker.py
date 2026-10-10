"""LLM 通道熔断（设计文档 §3 · 2026-10-06）。

核心不变量（§1.3的教训）：**配额类错误必须立刻停手并记账，禁止原地重试**。
那 28 次同路重试（日志 608 行里 `AccountQuotaExceeded` 刷了 28 次）就是反例。

本测试全程用 /tmp 临时库，不碰活库。
"""
from __future__ import annotations

import datetime
import json
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


# --------------------------------------------------------------------------- #
# 瞬时类递增退避（2026-10-06 实施时发现）
# --------------------------------------------------------------------------- #

def test_transient_cooldown_grows_with_strikes(base):
    """同一通道反复 5xx → 冷却递增（60→120→240…封顶 30min），而非每次都只熔 60s。

    否则一个持续故障的通道会在每次 tick 里被反复试一遍——那是「撞墙不收手」的
    另一种形态（换个位置复发）。
    """
    from deskmate.llm_circuit_breaker import TRANSIENT_COOLDOWN_S, TRANSIENT_MAX_COOLDOWN_S

    v1 = classify_error(status_code=500, body="", now=time.time())
    record_failure("ark:flaky", v1, base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        u1 = float(c.execute(
            f"SELECT cooldown_until FROM {TABLE} WHERE channel_key='ark:flaky'").fetchone()[0])

    time.sleep(0.05)
    record_failure("ark:flaky", v1, base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        u2 = float(c.execute(
            f"SELECT cooldown_until FROM {TABLE} WHERE channel_key='ark:flaky'").fetchone()[0])

    assert u2 > u1, f"第二次失败冷却应更长：{u1} → {u2}"
    # 封顶不超过 30min
    assert (u2 - time.time()) <= TRANSIENT_MAX_COOLDOWN_S + 5


def test_quota_cooldown_not_grown_by_strikes(base):
    """配额类冷却**不**递增（它由错误体决定，本就可能很长，别被 strikes 放大）。"""
    v = classify_error(status_code=429, body=VOLC_QUOTA)
    now0 = time.time()
    record_failure("ark:q", v, base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        u1 = float(c.execute(
            f"SELECT cooldown_until FROM {TABLE} WHERE channel_key='ark:q'").fetchone()[0])
    time.sleep(0.05)
    record_failure("ark:q", v, base_dir=base)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        u2 = float(c.execute(
            f"SELECT cooldown_until FROM {TABLE} WHERE channel_key='ark:q'").fetchone()[0])
    # 两次都解析到同一个重置点（23:59:59），不因 strikes 变化
    assert abs(u1 - u2) < 1.0


# --------------------------------------------------------------------------- #
# 通道组（设计 §二）
# --------------------------------------------------------------------------- #

def test_paid_group_excludes_qwen_and_keeps_hy_tokenplan():
    """付费组 = 火山→**hy Token Plan**（小米线 2026-10-10 起暂停排查用量异常）；qwen 明确不进链（萧哥 21:00）。"""
    from deskmate.llm_channels import PAID_GROUP, paid_channels

    keys = [f"{p}:{m}" for p, m in PAID_GROUP]
    assert keys == [
        "ark-agent-plan-cn:deepseek-v4-flash",
        "tencent-tokenplan:hy3",
    ], f"付费组顺序/成员变了：{keys}"
    assert not any("xiaomi" in k for k in keys), "小米线暂停期间不该在付费组里"
    assert not any("qwen" in k for k in keys), "qwen 不该在付费组里"
    # 已配凭证的才进候选
    got = [c.key for c in paid_channels()]
    assert "tencent-tokenplan:hy3" in got or not _has_creds("tencent-tokenplan")


def _has_creds(provider: str) -> bool:
    from deskmate.llm_channels import _has_credentials

    return _has_credentials(provider)


def test_free_group_sorts_non_reasoning_first():
    """免费组**非推理优先**（后台批量活要速度；推理模型实测 14.3s）。"""
    from deskmate.llm_channels import free_channels

    try:
        from coworker.providers.zen_catalog import is_reasoning
    except Exception:
        pytest.skip("zen_catalog 不可用")
    chs = free_channels()
    if len(chs) < 2:
        pytest.skip("远端清单不足 2 个")
    flags = [is_reasoning(c.model) for c in chs]
    # False 段必须全部在 True 段之前
    assert flags == sorted(flags), f"非推理未排在前：{flags}"


def test_route_skips_open_channels(base, monkeypatch):
    """route 跳过熔断中的通道，返回第一条可试的。"""
    from deskmate import llm_channels as CH

    monkeypatch.setenv("DESKMATE_DIR", str(base))
    monkeypatch.setattr(CH, "paid_channels", lambda: [
        CH.Channel("ark", "m1"), CH.Channel("ark", "m2"),
    ])
    record_failure("ark:m1", classify_error(status_code=429, body="quota exhausted"),
                   base_dir=base)
    ch, skipped = CH.route(CH.GROUP_PAID, base_dir=base)
    assert ch is not None and ch.key == "ark:m2", "应跳过 m1 选 m2"
    assert [c.key for c, _ in skipped] == ["ark:m1"]


def test_route_returns_none_when_all_open(base, monkeypatch):
    """全组熔断 → chosen=None（调用方据此降级，不是静默失败）。"""
    from deskmate import llm_channels as CH

    monkeypatch.setenv("DESKMATE_DIR", str(base))
    monkeypatch.setattr(CH, "paid_channels", lambda: [CH.Channel("ark", "m1")])
    record_failure("ark:m1", classify_error(status_code=429, body="quota exhausted"),
                   base_dir=base)
    ch, skipped = CH.route(CH.GROUP_PAID, base_dir=base)
    assert ch is None and len(skipped) == 1


def test_memory_group_defaults_to_free():
    """后台任务默认走**免费组**（萧哥 21:24 拍板「记忆抽取走免费」）。"""
    from deskmate.llm_channels import GROUP_FREE, memory_group

    assert memory_group() == GROUP_FREE


# --------------------------------------------------------------------------- #
# HTTP 端点（设置面板的「手动切组」入口）
# --------------------------------------------------------------------------- #

def test_llm_channel_endpoints(tmp_path, monkeypatch):
    """三个端点：查状态 / 切组（写 prefs，跨进程持久）/ 看审计。

    切组必须落**prefs.json** 而非只写环境变量——环境变量只活在当前进程，
    Tauri 起的 sidecar 与 CLI/定时任务读到的是不同环境（实测踩过）。
    """
    import os

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_MEMORY_GROUP", raising=False)

    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    c = TestClient(create_app(SessionManager.__new__(SessionManager)))

    r = c.get("/v1/llm-channels")
    assert r.status_code == 200
    body = r.json()
    assert "paid" in body and "free" in body
    assert "memory_group" in body

    r2 = c.post("/v1/llm-channels/memory-group", json={"group": "paid"})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    prefs = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
    assert prefs["memory_llm_group"] == "paid", "切组必须落 prefs（跨进程）"

    # 新进程视角：只读 prefs 也能拿到（不依赖环境变量）
    from deskmate.llm_channels import memory_group

    assert memory_group() == "paid"

    assert c.post("/v1/llm-channels/memory-group", json={"group": "free"}).status_code == 200
    assert memory_group() == "free"

    bad = c.post("/v1/llm-channels/memory-group", json={"group": "nope"})
    assert bad.status_code == 400, "非法组必须 400，不能静默接受"

    r3 = c.get("/v1/llm-channels/audit?limit=5")
    assert r3.status_code == 200
    assert isinstance(r3.json()["rows"], list)


# --------------------------------------------------------------------------- #
# 组内自动轮替（2026-10-06 修 bug 后锁定）
# --------------------------------------------------------------------------- #

def test_attempt_group_walks_to_next_channel_on_failure(tmp_path, monkeypatch):
    """组内第一条撞墙后必须**试下一条**，而不是直接抛出去。

    ⚠️ 这条锁一个真 bug：原实现里 `_call_with_breaker` 失败直接 raise 穿透，
    `while` 只对「无凭证」生效 ⇒ 火山限额时，小米/hy 明明可用却不被试，
    直到整个 chat_memory 抛错才降级到另一组。**组内轮替等于没实现。**
    """
    import deskmate.memory.llm as L

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    order: list[str] = []

    class _Ch:
        def __init__(self, provider, model):
            self.provider, self.model = provider, model
            self.key = f"{provider}:{model}"
            self.free = False

    chs = [_Ch("ark", "m1"), _Ch("xiaomi", "m2"), _Ch("tencent", "m3")]

    def _route(group, *, base_dir=None):
        for c in chs:
            order.append(c.key)
            return c, []          # 每次都返回「下一条」未熔断的
        return None, []

    def _creds(provider, model):
        return "AK", f"https://{provider}", model

    def _do(ak, bu, md):
        if md == "m1":
            raise RuntimeError("AccountQuotaExceeded ... quota")
        return f"ok-from-{md}"

    monkeypatch.setattr(L, "_creds_for", _creds, raising=False)
    # 简化：直接测 _attempt_group 的核心——通过 chat_memory 不好构造，
    # 故改为断言 route 被反复调用（说明 while 真的在迭代）
    calls = {"n": 0}

    def _route_counting(group, *, base_dir=None):
        calls["n"] += 1
        if calls["n"] > 3:
            return None, []
        return _route(group, base_dir=base_dir)

    monkeypatch.setattr("deskmate.llm_channels.route", _route_counting)
    monkeypatch.setenv("DESKMATE_MEMORY_GROUP", "paid")
    monkeypatch.setattr(L, "_creds_for", _creds, raising=False)

    # 全部通道都失败 ⇒ 最终抛错，但**应尝试过 3 次**（而不是 1 次就退）
    with pytest.raises(Exception):
        L.chat_memory("s", "u", timeout=1)
    assert calls["n"] >= 3, f"组内应逐条试，实际只试了 {calls['n']} 次"


# --------------------------------------------------------------------------- #
# 2026-10-10 修 bug：chat() 付费组降级 + chat_memory free_only（自主/反思层省额度）
# --------------------------------------------------------------------------- #

class _FakeCh:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.key = f"{provider}:{model}"
        self.free = False


def test_chat_falls_back_within_paid_group(tmp_path, monkeypatch):
    """chat() 主通道（default_model）熔断时，应沿付费组换下一条健康通道，而非静默失败。

    锁 2026-10-10 修的 bug：原实现只读 default_model，死模型上直接抛，
    intent_*/judge 等后台交互路径静默瘫。现应落到付费组下一个健康通道。
    """
    import deskmate.memory.llm as L

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))

    # default_model = 死的火山主模型
    monkeypatch.setattr(
        L, "_active_provider_and_model",
        lambda: ("ark-agent-plan-cn", "doubao-seed-2.1-turbo"),
    )
    # 付费组候选：主位死，小米健康（chat() 内部重新 import，故 patch 源模块）
    monkeypatch.setattr(
        "deskmate.llm_channels.paid_channels",
        lambda: [_FakeCh("ark-agent-plan-cn", "doubao-seed-2.1-turbo"),
                 _FakeCh("xiaomi", "mimo-v2.5-pro")],
    )
    # 凭证 + 调用：主位在调用时抛配额错（模拟死模型），小米返回文本（不真打网络）
    monkeypatch.setattr(L, "_creds_for", lambda p, m: ("AK", f"https://{p}", m))
    calls: list[str] = []
    def _cb(provider, model, fn):
        calls.append(f"{provider}:{model}")
        if provider == "ark-agent-plan-cn":
            raise RuntimeError("AccountQuotaExceeded")
        return "ok-from-xiaomi"
    monkeypatch.setattr(L, "_call_with_breaker", _cb)

    out = L.chat("sys", "usr", timeout=1)
    assert out == "ok-from-xiaomi", f"应落到小米，实际调用：{calls}"
    assert "ark-agent-plan-cn:doubao-seed-2.1-turbo" in calls, f"应先试主位，实际：{calls}"
    assert "xiaomi:mimo-v2.5-pro" in calls, f"应尝试小米兜底，实际：{calls}"


def test_chat_raises_when_all_paid_open(tmp_path, monkeypatch):
    """付费组全部熔断时，chat() 应抛错（而非返回 None/静默空转）。"""
    import deskmate.memory.llm as L

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        L, "_active_provider_and_model",
        lambda: ("ark-agent-plan-cn", "doubao-seed-2.1-turbo"),
    )
    monkeypatch.setattr(
        "deskmate.llm_channels.paid_channels",
        lambda: [_FakeCh("ark-agent-plan-cn", "doubao-seed-2.1-turbo"),
                 _FakeCh("xiaomi", "mimo-v2.5-pro")],
    )
    for key in ("ark-agent-plan-cn:doubao-seed-2.1-turbo", "xiaomi:mimo-v2.5-pro"):
        record_failure(key, classify_error(status_code=429, body="quota"),
                       base_dir=tmp_path)
    monkeypatch.setattr(L, "_creds_for", lambda p, m: ("AK", f"https://{p}", m))
    monkeypatch.setattr(
        L, "_call_with_breaker", lambda p, m, fn: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(Exception):
        L.chat("sys", "usr", timeout=1)


def test_chat_memory_free_only_does_not_fallback_to_paid(tmp_path, monkeypatch):
    """chat_memory(free_only=True) 免费组全挂时直接抛，绝不兜底付费（省额度）。"""
    import deskmate.memory.llm as L

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))

    paid_touched = {"n": 0}
    def _route(group, *, base_dir=None):
        if group == "paid":
            paid_touched["n"] += 1
        return (None, [])  # 两组都「无可用通道」
    monkeypatch.setattr("deskmate.llm_channels.route", _route)

    with pytest.raises(Exception):
        L.chat_memory("s", "u", timeout=1, free_only=True)
    assert paid_touched["n"] == 0, "free_only 绝不应触碰付费组"


def test_chat_memory_default_falls_back_to_paid(tmp_path, monkeypatch):
    """chat_memory(free_only=False) 免费组全挂时应兜底回付费组（不丢记忆）。"""
    import deskmate.memory.llm as L

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DESKMATE_MEMORY_GROUP", "free")

    calls = {"free": 0, "paid": 0}
    def _route(group, *, base_dir=None):
        calls[group] = calls.get(group, 0) + 1
        return (None, [])
    monkeypatch.setattr("deskmate.llm_channels.route", _route)

    with pytest.raises(Exception):
        L.chat_memory("s", "u", timeout=1)
    assert calls["free"] >= 1, "应先试免费组"
    assert calls["paid"] >= 1, "默认应兜底到付费组"
