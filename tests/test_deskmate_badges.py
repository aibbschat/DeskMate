"""勋章体系（deskmate.growth.badges + engine）测试。

覆盖：
- 注册表完整性（id 唯一 / 门类 / 稀有度 / 阈值 / 隐藏遮蔽）
- 判定与解锁（对话 / 编校 / 圆桌 / 报告 / 书稿）
- 进度条（未解锁时按真实数值）
- 跨实例持久化（解锁时间落盘、非补录）
- 历史回溯伦理（能回溯给真实时间，不能的标「补录」，且迁移不刷屏报喜）
- 展示排序（已得在前）

全部用 tmp_path 隔离，不触碰 ~/.deskmate。
"""

import pytest

from deskmate.growth import badges as reg
from deskmate.growth.badges import BadgeContext
from deskmate.growth.engine import GrowthEngine
from deskmate.growth.state import BACKFILLED_MARK, GrowthState, save_state


def _ctx() -> BadgeContext:
    """空状态上下文，供 metric 调用与序列化。"""
    return BadgeContext(state=GrowthState())


def _find(items, bid: str) -> dict:
    for it in items:
        if it["id"] == bid:
            return it
    raise AssertionError(f"badge {bid} not found in {[i['id'] for i in items]}")


# ---------------------------------------------------------------------------
# 1. 注册表完整性
# ---------------------------------------------------------------------------

def test_registry_nonempty_and_unique_ids():
    assert len(reg.BADGES) > 0
    ids = [b.id for b in reg.BADGES]
    assert len(ids) == len(set(ids)), "勋章 id 不能重复"


def test_registry_categories_rarities_thresholds():
    valid_cats = set(reg.CATEGORY_LABELS)
    valid_rar = set(reg.RARITY_ORDER)
    for b in reg.BADGES:
        assert b.category in valid_cats, f"{b.id} 门类非法"
        assert b.rarity in valid_rar, f"{b.id} 稀有度非法"
        assert b.target > 0, f"{b.id} 阈值必须 > 0"
        assert callable(b.metric), f"{b.id} metric 必须是可调用"


def test_registry_metric_returns_int():
    ctx = _ctx()
    for b in reg.BADGES:
        val = b.metric(ctx)
        assert isinstance(val, int), f"{b.id} metric 应返回 int"


def test_hidden_badges_flagged():
    hidden = [b for b in reg.BADGES if b.hidden]
    assert hidden, "应至少存在一枚隐藏勋章"
    for b in hidden:
        assert b.hidden is True


def test_serialize_masks_hidden_when_locked():
    ctx = _ctx()
    for b in reg.BADGES:
        if not b.hidden:
            continue
        s = reg.serialize(b, ctx)  # 空状态 → 未解锁
        assert s["masked"] is True
        assert s["name"] == reg.HIDDEN_NAME
        assert s["progress"] == 0
        assert s["maxProgress"] == 1


# ---------------------------------------------------------------------------
# 2. 判定与解锁
# ---------------------------------------------------------------------------

def test_first_chat_unlock_and_persist(tmp_path):
    eng = GrowthEngine(base_dir=tmp_path)
    eng.record_chat()  # total_chats -> 1
    snap = eng.badge_snapshot()
    first = _find(snap["badges"], "first_chat")
    assert first["unlocked"] is True
    assert not first["backfilled"]
    assert first["unlockedAt"]  # 真实时间

    # 跨实例：重新加载后依然已得，且时间非补录
    eng2 = GrowthEngine(base_dir=tmp_path)
    snap2 = eng2.badge_snapshot()
    first2 = _find(snap2["badges"], "first_chat")
    assert first2["unlocked"] is True
    assert not first2["backfilled"]
    assert first2["unlockedAt"] == first["unlockedAt"]


def test_writing_unlocks_chapters_and_words(tmp_path):
    eng = GrowthEngine(base_dir=tmp_path)
    eng.record_writing(chapters=5)
    items = eng.badge_items()
    ch10 = _find(items, "chapters_10")
    assert ch10["unlocked"] is False
    assert ch10["progress"] == 5
    assert ch10["maxProgress"] == 10

    # 累计到 10 章 → 解锁「十章之功」
    eng.record_writing(chapters=5)
    ch10b = _find(eng.badge_items(), "chapters_10")
    assert ch10b["unlocked"] is True

    # 单日 2 万字 → 解锁「万言」+ 隐藏「一日千言」
    eng.record_writing(words=20_000)
    words = _find(eng.badge_items(), "words_10k")
    assert words["unlocked"] is True
    daily = _find(eng.badge_items(), "daily_10k")
    assert daily["unlocked"] is True
    assert daily["masked"] is False  # 解锁后隐藏勋章显形


def test_roundtable_report_manuscript(tmp_path):
    eng = GrowthEngine(base_dir=tmp_path)
    eng.record_roundtable()
    assert _find(eng.badge_items(), "first_roundtable")["unlocked"] is True

    eng.record_report()
    assert _find(eng.badge_items(), "first_report")["unlocked"] is True

    eng.record_manuscript(total=1)
    assert _find(eng.badge_items(), "first_manuscript")["unlocked"] is True

    # record_manuscript(total=N) 幂等：传总数而非累加
    eng.record_manuscript(total=1)
    assert eng.get_counter(reg.COUNTER_MANUSCRIPTS) == 1


def test_hidden_serendipity_locked_masked_in_fresh_state(tmp_path):
    eng = GrowthEngine(base_dir=tmp_path)
    items = eng.badge_items()
    for bid in ("night_owl", "early_bird", "daily_10k", "new_year_vigil", "comeback"):
        b = _find(items, bid)
        assert b["unlocked"] is False
        assert b["masked"] is True


# ---------------------------------------------------------------------------
# 3. 历史回溯伦理
# ---------------------------------------------------------------------------

def _seed_legacy_state(tmp_path):
    """构造一个「勋章体系上线前」就已存在数据的状态：有对话与记忆，
    但没有 badges_unlocked、也没有迁移标记。"""
    st = GrowthState()
    st.total_chats = 100
    st.memories_learned = 1
    st.recent_events = [
        {"kind": "memory_learned", "count": 1, "ts": "2024-06-01T08:00:00+00:00"}
    ]
    st.updated_at = "2024-06-01T08:00:00+00:00"
    save_state(st, tmp_path)


def test_migration_backfills_without_spamming(tmp_path):
    _seed_legacy_state(tmp_path)
    eng = GrowthEngine(base_dir=tmp_path)  # 加载 → 视为迁移
    snap = eng.badge_snapshot()

    # 迁移时不作为新解锁上报，避免一上来弹一屏报喜
    assert snap["newlyUnlocked"] == []

    # 对话类勋章无法回溯（record_chat 不写事件）→ 标补录
    chat = _find(snap["badges"], "first_chat")
    assert chat["unlocked"] is True
    assert chat["backfilled"] is True

    chat100 = _find(snap["badges"], "chat_100")
    assert chat100["unlocked"] is True
    assert chat100["backfilled"] is True

    # 记忆类勋章可回溯到真实事件时间 → 非补录
    mem = _find(snap["badges"], "first_memory")
    assert mem["unlocked"] is True
    assert mem["backfilled"] is False
    assert mem["unlockedAt"] == "2024-06-01T08:00:00+00:00"


def test_migration_mark_persisted(tmp_path):
    _seed_legacy_state(tmp_path)
    GrowthEngine(base_dir=tmp_path).badge_snapshot()
    # 二次加载不再迁移：已解锁勋章不重复上报
    eng2 = GrowthEngine(base_dir=tmp_path)
    snap2 = eng2.badge_snapshot()
    assert snap2["newlyUnlocked"] == []
    # 且迁移标记已落盘（badges_unlocked 不再为空）
    assert eng2.state.badges_unlocked


# ---------------------------------------------------------------------------
# 4. 展示排序
# ---------------------------------------------------------------------------

def test_sort_for_display_unlocked_first():
    ctx = _ctx()
    items = [reg.serialize(b, ctx) for b in reg.BADGES]
    ordered = reg.sort_for_display(items)
    # 找到首个未解锁的位置，其之前应全是已解锁
    seen_locked = False
    for it in ordered:
        if it["unlocked"]:
            assert not seen_locked, "已解锁勋章必须排在未解锁之前"
        else:
            seen_locked = True


def test_snapshot_summary_counts(tmp_path):
    eng = GrowthEngine(base_dir=tmp_path)
    eng.record_chat()
    eng.record_writing(chapters=1)
    snap = eng.badge_snapshot()
    summary = snap["summary"]
    assert summary["total"] == len(reg.BADGES)
    assert summary["unlocked"] >= 2  # 初见 + 校书郎
    # 五门四阶分组齐全
    assert len(summary["categories"]) == 5
    assert len(summary["rarities"]) == 4
