"""sync_daemon.py 单元测试（阶段 1 骨架验收，不碰生产库）。

覆盖草案阶段 1 验收口径：
- HLC 定序（同物理时间计数器递增、全局单调）
- 同 uuid 断网双写 → 重连 HLC 定序后者胜（LWW）
- 删除 → 双端墓碑消失
- 遗忘（valid_until / tombstone）不被旧副本复活
- 幂等重放（重复事件不翻倍）
- 按 uuid 逐条对账（count + missing + divergent）
- 双端 InMemoryTransport 跑一轮收敛
"""

from __future__ import annotations

import time

from deskmate.memory.sync_daemon import (
    HLC,
    InMemoryTransport,
    MemoryStore,
    OplogStore,
    Op,
    Record,
    SyncDaemon,
    SyncEngine,
    Transport,
    _lww_winner,
    reconcile_baseline,
    reconcile_baseline_oplog,
)


def _make_pair(node_a: str = "mac", node_b: str = "cloud"):
    """造一对引擎+传输：A 的 transport 指向 B 的 oplog，B 的指向 A 的 oplog。"""
    store_a, store_b = MemoryStore(), MemoryStore()
    oplog_a, oplog_b = OplogStore(), OplogStore()
    eng_a = SyncEngine(store_a, oplog_a, node_a)
    eng_b = SyncEngine(store_b, oplog_b, node_b)
    trans_a = InMemoryTransport(oplog_b)  # A 拉/推 B
    trans_b = InMemoryTransport(oplog_a)  # B 拉/推 A
    return eng_a, eng_b, SyncDaemon(eng_a, trans_a), SyncDaemon(eng_b, trans_b)


def test_hlc_monotonic_and_ordering():
    h1 = HLC.now("mac")
    h2 = HLC.now("mac", last=h1)
    assert h2 > h1  # 同物理时间计数器递增
    h3 = HLC.now("mac", last=h2)
    assert h3 > h2 > h1
    # 不同节点也能定序（不会相等）：node 字符串 tiebreak，"cloud" < "mac"
    assert HLC(100, 0, "cloud") < HLC(100, 0, "mac")
    assert HLC.from_str(h1.to_str()) == h1  # 序列化往返一致


def test_lww_same_uuid_later_wins():
    """同 uuid 两端各写一版（不同 HLC），重连后 HLC 后者胜。"""
    eng_a, eng_b, d_a, d_b = _make_pair()
    rid = "u1"
    # A 先写 v1（较早 HLC）
    h_a = eng_a.local_upsert("profile_items", rid, {"name": "v1"})
    time.sleep(0.002)
    # B 后写 v2（更晚 HLC）—— 模拟断网双写
    eng_b.local_upsert("profile_items", rid, {"name": "v2"})
    # 双方各跑一轮：A 拉到 B 的 v2，B 拉到 A 的 v1
    d_a.tick()
    d_b.tick()
    d_a.tick()  # 再跑一轮确保收敛
    d_b.tick()
    # 最终两端都应是更晚的 v2（LWW 后者胜）
    assert eng_a.store.get(rid).data["name"] == "v2"
    assert eng_b.store.get(rid).data["name"] == "v2"
    assert h_a is not None


def test_tombstone_delete_both_sides():
    """一端 delete（墓碑），apply 后双端该记录消失。"""
    eng_a, eng_b, d_a, d_b = _make_pair()
    rid = "u2"
    eng_a.local_upsert("diary", rid, {"text": "hello"})
    d_a.tick()
    d_b.tick()
    assert eng_b.store.get(rid) is not None
    # A 删除
    eng_a.local_delete("diary", rid)
    d_a.tick()
    d_b.tick()
    d_a.tick()
    d_b.tick()
    assert eng_a.store.get(rid).tombstone is True
    assert eng_b.store.get(rid) is None or eng_b.store.get(rid).tombstone is True
    # 活记录列表中不出现
    assert all(r.record_id != rid for r in eng_a.store.live_items())
    assert all(r.record_id != rid for r in eng_b.store.live_items())


def test_forgetting_not_revived_by_stale_copy():
    """记录已失效（valid_until 过期 / 墓碑），旧副本（更早 HLC）重放不复活。"""
    eng_a, eng_b, d_a, d_b = _make_pair()
    rid = "u3"
    past = time.time_ns() // 1_000_000 - 10_000
    # A 写入一条已带 valid_until 过期的记录
    h_a = HLC.now("mac")
    eng_a.store.upsert(rid, {"x": 1}, h_a, valid_until=past)
    eng_a.oplog.append(Op("profile_items", rid, "upsert", h_a, {"x": 1}))
    # B 也写入同 uuid 但更晚、且是活记录（模拟 B 后发更“新”的版本）
    h_b = HLC.now("cloud", last=h_a)
    eng_b.store.upsert(rid, {"x": 2}, h_b)  # 无 valid_until → 活
    eng_b.oplog.append(Op("profile_items", rid, "upsert", h_b, {"x": 2}))
    # A 同步到 B 的更新版 → A 应接受（v2 活）
    d_a.tick()
    d_b.tick()
    d_a.tick()
    # 现在 A 侧该记录是活 v2
    assert eng_a.store.get(rid).data["x"] == 2
    # 关键：构造一个“旧副本”——更早 HLC 的同 uuid 事件重放，不应把 v2 覆盖回 v1
    stale = Op("profile_items", rid, "upsert", h_a, {"x": 1})
    eng_a.apply_remote([stale])
    assert eng_a.store.get(rid).data["x"] == 2, "旧副本不得复活/覆盖更晚版本"


def test_idempotent_replay():
    """重复 oplog 事件重放不翻倍、不污染。"""
    eng_a, _eng_b, d_a, _d_b = _make_pair()
    rid = "u4"
    h = eng_a.local_upsert("profile_items", rid, {"k": "v"})
    op = Op("profile_items", rid, "upsert", h, {"k": "v"})
    n_before = len(eng_a.store.items())
    eng_a.apply_remote([op, op, op])  # 重复三次
    assert len(eng_a.store.items()) == n_before  # 幂等，记录数不变
    assert eng_a.store.get(rid).data["k"] == "v"


def test_same_hlc_upsert_still_idempotent():
    """同hlc 的**纯 upsert** 重复重放仍须幂等（不得因A 改动而破坏既有语义）。

    与同hlc delete→upsert 复活用例互补：本条锁住"同hlc 同类型重复不翻倍"
    （MemoryStore 语义：同hlc 同 op 重放必须幂等）。
    """
    eng_a, _eng_b, _d_a, _d_b = _make_pair()
    rid = "u-idem"
    h = HLC(2000, 0, "cloud")
    payload = {"k": "v"}
    for _ in range(3):
        eng_a.apply_remote([Op("profile_items", rid, "upsert", h, payload, origin="cloud")])
    # 幂等判据：3 次同hlc 重放后仍只有 1 条，且内容为最后一次的值
    assert len(eng_a.store.items()) == 1, "同hlc upsert 重复重放必须幂等"
    assert eng_a.store.get(rid).data["k"] == "v"


def test_strictly_older_upsert_still_blocked_by_tombstone():
    """严格更早 hlc 的 upsert 仍被墓碑拦住（防遗忘复活不得被A 削弱）。"""
    eng_a, _eng_b, _d_a, _d_b = _make_pair()
    rid = "u-stale"
    h_new = HLC(3000, 0, "cloud")
    eng_a.apply_remote([Op("profile_items", rid, "delete", h_new, {}, tombstone=True, origin="cloud")])
    # 更早 hlc 的 upsert → 必须仍被拒（记录仍为墓碑态）
    eng_a.apply_remote(
        [Op("profile_items", rid, "upsert", HLC(2000, 0, "cloud"), {"k": "old"}, origin="cloud")]
    )
    rec = eng_a.store.get(rid)
    assert rec.tombstone is True, "严格更早的 upsert 不得复活已删记录"
    assert rec.data.get("k") != "old", "墓碑态不得被更早的 upsert 写入内容"


def test_reconcile_by_uuid():
    store_a, store_b = MemoryStore(), MemoryStore()
    oplog_a, oplog_b = OplogStore(), OplogStore()
    eng_a = SyncEngine(store_a, oplog_a, "mac")
    eng_b = SyncEngine(store_b, oplog_b, "cloud")

    eng_a.local_upsert("profile_items", "a1", {"v": 1})
    eng_a.local_upsert("profile_items", "a2", {"v": 2})
    eng_b.local_upsert("profile_items", "a2", {"v": 999})  # 同 uuid 分歧
    eng_b.local_upsert("profile_items", "b1", {"v": 3})  # A 缺失

    rep = eng_a.reconcile(eng_b.store)
    assert rep["count_local"] == 2
    assert rep["count_remote"] == 2
    assert "b1" in rep["missing_local"]
    assert "a1" in rep["missing_remote"]
    assert "a2" in rep["divergent"]
    assert rep["converged"] is False

    # 收敛后应对账通过
    eng_a.apply_remote(eng_b.oplog.all())
    eng_b.apply_remote(eng_a.oplog.all())
    rep2 = eng_a.reconcile(eng_b.store)
    assert rep2["converged"] is True


def test_roundtrip_inmemory_converge():
    """双端各写各的，跑几轮 tick 后完全收敛（count + 内容一致）。"""
    eng_a, eng_b, d_a, d_b = _make_pair()
    for i in range(3):
        eng_a.local_upsert("diary", f"a{i}", {"i": i})
        eng_b.local_upsert("diary", f"b{i}", {"i": i})
    for _ in range(4):  # 多跑几轮确保增量互达
        d_a.tick()
        d_b.tick()
    rep = eng_a.reconcile(eng_b.store)
    assert rep["converged"] is True
    assert rep["count_local"] == 6
    # 逐条内容一致
    for r in eng_a.store.live_items():
        other = eng_b.store.get(r.record_id)
        assert other is not None
        assert other.data == r.data


def test_clock_skew_remote_event_not_skipped():
    """回归（review 发现的丢更新缺陷）：拉取游标按对端 oplog 的整数 seq 走，
    不按 HLC。云端一条 physical 更小（时钟落后/旧设备补传）但尚未拉过的事件，
    绝不能被本端较高的本地 HLC 顶高游标后永久跳过。"""
    from deskmate.memory.sync_daemon import Op as _Op

    store_m, oplog_m = MemoryStore(), OplogStore()
    oplog_c = OplogStore()  # 云端对端
    eng_m = SyncEngine(store_m, oplog_m, "mac")
    d_m = SyncDaemon(eng_m, InMemoryTransport(oplog_c))

    # Mac 本地先写并同步一轮（推/拉），拉取游标不应被自己的 HLC 顶高
    hm = eng_m.local_upsert("diary", "m1", {"v": 1})
    d_m.tick()

    # 云端随后补一条【physical 更早】的事件（HLC 落在 hm 之前）
    hc = HLC(hm.physical - 5, 0, "cloud")
    oplog_c.append(_Op("diary", "c1", "upsert", hc, {"v": 2}))

    # 再同步：必须收到 c1
    d_m.tick()
    assert eng_m.store.get("c1") is not None, "时钟落后的远端事件被游标永久跳过（丢更新）"
    assert eng_m.store.get("c1").data["v"] == 2
    # 第二轮拉到：上一轮推过去的 m1 + 新补的 c1（共 2 条），c1 未被漏
    assert d_m.pull_after_seq == 2


def test_pull_cursor_uses_max_seq_not_len_on_gap():
    """回归（二蛋 2026-10-03 游标缺陷）：对端 oplog 经 GC 后 seq 不连续/高位，
    pull_after_seq 必须按返回 op 的【最大 seq】推进，而非 += len(remote_ops)。
    否则 seq 有空洞时反复重拉同一批（浪费），seq 整体回退时 pull_after_seq 越过
    新事件 seq → 永久漏同步且不报错。"""
    # 对端 oplog 经 GC 后只剩 seq 100/200/300（大空洞、高位）
    remote_ops = [
        Op("profile_items", "r100", "upsert", HLC(100, 0, "cloud"), seq=100),
        Op("profile_items", "r200", "upsert", HLC(200, 0, "cloud"), seq=200),
        Op("profile_items", "r300", "upsert", HLC(300, 0, "cloud"), seq=300),
    ]

    class _GapTransport(Transport):
        def pull_since(self, after_seq):
            return [o for o in remote_ops if (o.seq or 0) > after_seq]

        def push(self, ops):
            pass

    store_m, oplog_m = MemoryStore(), OplogStore()
    eng_m = SyncEngine(store_m, oplog_m, "mac")
    d_m = SyncDaemon(eng_m, _GapTransport())

    res = d_m.tick()
    # 关键：一轮直接到 max seq=300，而非 +=len(3)=3（旧 bug 会让游标停在低位）
    assert res["pulled"] == 3
    assert res["pull_after_seq"] == 300, res
    # 之后新事件 seq=301 立即被拉到，且不再重拉 100/200/300
    remote_ops.append(Op("profile_items", "r301", "upsert", HLC(301, 0, "cloud"), seq=301))
    res2 = d_m.tick()
    assert res2["pulled"] == 1, res2
    assert res2["pull_after_seq"] == 301, res2



# ---------------------------------------------------------------------------
# 基线对账的同 hlc 决胜（2026-10-05 存量补齐时发现）
# ---------------------------------------------------------------------------


def test_lww_winner_same_hlc_live_beats_tombstone():
    """同 hlc 的「墓碑 vs 活记录」→ 活记录胜（不得让墓碑压过同刻的存在）。

    真实库病灶：触发器 hlc 仅毫秒精度+ counter 曾不自增，同一毫秒的 delete→upsert
    产出同 hlc 的墓碑/活记录组合（两端 physical/counter/node 三段完全相同），
    共 65 条 emotional 记录在Mac 侧停留于墓碑态、云端持有活数据。
    """
    h = HLC(1000, 0, "cloud")
    tomb = Record(record_id="x", data={}, hlc=h, tombstone=True)
    live = Record(record_id="x", data={"layer": "emotional"}, hlc=h, tombstone=False)
    assert _lww_winner(tomb, live) is live, "本端墓碑 vs 远端活 → 远端活应胜"
    assert _lww_winner(live, tomb) is live, "本端活 vs 远端墓碑 → 本端活应胜"


def test_lww_winner_strictly_later_hlc_wins_regardless_of_type():
    """hlc 不同时仍是「晚者胜」（墓碑晚于活记录 → 墓碑胜，防遗忘复活不被削弱）。"""
    h_old, h_new = HLC(1000, 0, "cloud"), HLC(2000, 0, "cloud")
    old_live = Record(record_id="x", data={"v": 1}, hlc=h_old, tombstone=False)
    new_tomb = Record(record_id="x", data={}, hlc=h_new, tombstone=True)
    assert _lww_winner(old_live, new_tomb) is new_tomb, "更晚的墓碑应胜（真删除）"
    assert _lww_winner(new_tomb, old_live) is new_tomb
    new_live = Record(record_id="x", data={"v": 2}, hlc=h_new, tombstone=False)
    assert _lww_winner(new_tomb, new_live) is new_live, "更晚的活记录应胜"


def test_reconcile_baseline_lww_same_hlc_revives_local():
    """端到端：基线对账遇到「本端墓碑 /远端同hlc 活记录」应把本端复活。"""
    h = HLC(1000, 0, "cloud")
    local = MemoryStore()
    local.delete("emo", h)  # 本端墓碑
    remote = MemoryStore()
    remote.upsert("emo", {"layer": "emotional", "content": "云端情感数据"}, h)
    rep = reconcile_baseline(local, remote, "mac")
    rec = local.get("emo")
    assert rec is not None and rec.tombstone is False, f"本端应被同hlc 活记录复活：{rec}"
    assert rec.data["content"] == "云端情感数据"
    assert rep["resolved"] == 1, rep


def test_reconcile_baseline_oplog_lww_same_hlc_revives_local():
    """同 hlc 决胜在 oplog 版基线对账同样生效（真实库 65 条走的就是这条路径）。"""
    h = HLC(1000, 0, "cloud")
    local = MemoryStore()
    local.delete("emo", h)
    remote = MemoryStore()
    remote.upsert("emo", {"layer": "emotional", "content": "云端情感数据"}, h)
    oplog = OplogStore()
    eng = SyncEngine(local, oplog, "mac")
    rep = reconcile_baseline_oplog(local, remote, eng, "profile_items", "mac")
    rec = local.get("emo")
    assert rec is not None and rec.tombstone is False, f"本端应被复活：{rec}"
    assert rec.data["content"] == "云端情感数据"
    assert rep["resolved"] == 1, rep
    # 远端本就持有该活记录，无需回推 → oplog 不应产生该 record 的 op
    assert not [o for o in oplog.since(0) if o.record_id == "emo"], "远端胜不应回推"
