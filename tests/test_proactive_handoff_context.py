"""§11.16 灵动三层 单测：她递出卡片后要「接得上」（2026-10-07）。

覆盖：
- 层一①：delivered_today_items 口径（今日已递、未终态过滤）
         + render_delivered_context 渲染（#id/content/依据/理解、失败标记降级、截断）
         + build_full_context_for_turn 真实挂上「我今天已递给用户的待办」块
- 层二②：lookup_items 短句柄 / 完整 id 前缀 / 关键词回查；kind 前缀不误命中
- 层三③：compose_speak_text 文案 + mark_delivered 补账（幂等 / 终态不记）

测试一律用 /tmp 副本库，不碰活库。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from deskmate.proactivity.ledger import (
    UNDERSTANDING_FAILED_MARKER,
    _short_id,
    compose_speak_text,
    delivered_today_items,
    lookup_items,
    mark_delivered,
    record_proactive,
    render_delivered_context,
)


def _fresh_base() -> Path:
    base = Path(__import__("tempfile").mkdtemp(prefix="pt_handoff_ctx_"))
    db = base / "deskmate.db"
    conn = sqlite3.connect(str(db))
    from deskmate.proactivity.ledger import _ensure_table

    _ensure_table(conn)
    conn.close()
    return base


def _seed(base: Path, content: str = "AI圈轮巡回执 ok 但没产出", **kw) -> str:
    kw.setdefault("understanding", "疑似火山限额跑空")
    return record_proactive(
        base_dir=base, source="cloud_audit", kind="drift",
        content=content, evidence="automation 日志 08:30",
        confidence=0.9, **kw,
    )


def _set_delivered(base: Path, rid: str, days_ago: int = 0) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days_ago * 86400))
    with sqlite3.connect(str(base / "deskmate.db"), check_same_thread=False) as c:
        c.execute("UPDATE proactive_ledger SET delivered_at=? WHERE id=?", (ts, rid))
        c.commit()


# ---------------------------------------------------------------------------
# 层一①：delivered_today_items 口径
# ---------------------------------------------------------------------------
def test_delivered_today_filters_status_and_date():
    base = _fresh_base()
    r_today = _seed(base)
    r_old = _seed(base, content="昨天的旧账")
    r_done = _seed(base, content="已处理的")
    _set_delivered(base, r_today, days_ago=0)
    _set_delivered(base, r_old, days_ago=1)
    _set_delivered(base, r_done, days_ago=0)
    from deskmate.proactivity.ledger import acknowledge_ledger

    acknowledge_ledger(base_dir=base, rid=r_done, new_status="actioned")

    items = delivered_today_items(base)
    ids = [i["id"] for i in items]
    assert r_today in ids
    assert r_old not in ids      # 昨天递过的不算今天
    assert r_done not in ids     # actioned 终态不注入


def test_render_delivered_context_block():
    base = _fresh_base()
    r = _seed(base)
    _set_delivered(base, r)
    ctx = render_delivered_context(delivered_today_items(base))
    assert "## 我今天已递给用户的待办" in ctx
    assert f"#{_short_id(r)}" in ctx
    assert "AI圈轮巡回执 ok 但没产出" in ctx
    assert "依据：automation 日志 08:30" in ctx
    assert "我的理解：疑似火山限额跑空" in ctx


def test_render_delivered_context_empty():
    assert render_delivered_context([]) == ""


def test_render_delivered_context_failed_marker_degrades():
    base = _fresh_base()
    r = _seed(base, understanding=UNDERSTANDING_FAILED_MARKER)
    _set_delivered(base, r)
    ctx = render_delivered_context(delivered_today_items(base))
    assert UNDERSTANDING_FAILED_MARKER not in ctx
    assert "理解加工未完成" in ctx


def test_render_delivered_context_truncates_long_items():
    base = _fresh_base()
    r = _seed(base, content="长" * 500)
    _set_delivered(base, r)
    ctx = render_delivered_context(delivered_today_items(base))
    assert "……" in ctx or "…" in ctx
    assert len(ctx) < 2000


def test_build_full_context_injects_delivered_block():
    """端到端挂线：MemoryManager(base_dir=tmp) 的每轮上下文含今日待办块。"""
    base = _fresh_base()
    r = _seed(base)
    mark_delivered(base_dir=base, rid=r)

    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=base)
    ctx = mgr.build_full_context_for_turn(user_message="那个轮巡怎么回事")
    assert "## 我今天已递给用户的待办" in ctx
    assert f"#{_short_id(r)}" in ctx
    assert "AI圈轮巡回执 ok 但没产出" in ctx


# ---------------------------------------------------------------------------
# 层二②：lookup_items 回查
# ---------------------------------------------------------------------------
def test_lookup_by_short_handle():
    base = _fresh_base()
    r = _seed(base)
    hits = lookup_items(base, handle="#" + _short_id(r))
    assert [h["id"] for h in hits] == [r]


def test_lookup_by_full_id_prefix():
    base = _fresh_base()
    r = _seed(base)
    hits = lookup_items(base, handle="cloud_audit:drift")
    assert r in [h["id"] for h in hits]


def test_lookup_by_query_likes_content():
    base = _fresh_base()
    r = _seed(base)
    hits = lookup_items(base, query="火山限额")
    assert [h["id"] for h in hits] == [r]


def test_lookup_handle_does_not_match_kind_prefix():
    """kind 文本（如 drift）不得被当成指纹段误命中。"""
    base = _fresh_base()
    r = _seed(base)  # id = cloud_audit:drift:<指纹>
    hits = lookup_items(base, handle="drift")
    assert hits == []


def test_lookup_miss_returns_empty():
    base = _fresh_base()
    _seed(base)
    assert lookup_items(base, handle="#zzzzzz") == []
    assert lookup_items(base, query="不存在的关键词xyz") == []


# ---------------------------------------------------------------------------
# 层三③：compose_speak_text + mark_delivered
# ---------------------------------------------------------------------------
def test_compose_speak_text_voice():
    base = _fresh_base()
    r = _seed(base)
    item = lookup_items(base, handle="#" + _short_id(r))[0]
    text = compose_speak_text(item)
    assert text.startswith("对了，说到这个——AI圈轮巡回执 ok 但没产出")
    assert "（依据：automation 日志 08:30）" in text
    assert "疑似火山限额跑空" in text
    assert f"#{_short_id(r)}" in text


def test_compose_speak_text_omits_empty_understanding():
    base = _fresh_base()
    r = _seed(base, understanding=None)
    item = lookup_items(base, handle="#" + _short_id(r))[0]
    text = compose_speak_text(item)
    assert "我的理解" not in text
    assert "对了，说到这个——" in text


def test_compose_speak_text_failed_marker_degrades():
    base = _fresh_base()
    r = _seed(base, understanding=UNDERSTANDING_FAILED_MARKER)
    item = lookup_items(base, handle="#" + _short_id(r))[0]
    text = compose_speak_text(item)
    assert UNDERSTANDING_FAILED_MARKER not in text
    assert "理解加工没完成" in text


def test_mark_delivered_idempotent_and_terminal_safe():
    base = _fresh_base()
    r = _seed(base)
    assert mark_delivered(base_dir=base, rid=r) is True
    first = delivered_today_items(base)
    assert [i["id"] for i in first] == [r]
    # 幂等：再调不报错、日期不刷掉
    assert mark_delivered(base_dir=base, rid=r) is True
    assert [i["id"] for i in delivered_today_items(base)] == [r]

    from deskmate.proactivity.ledger import acknowledge_ledger

    acknowledge_ledger(base_dir=base, rid=r, new_status="dismissed")
    assert mark_delivered(base_dir=base, rid=r) is False  # 终态不记
    assert lookup_items(base, handle="#" + _short_id(r))[0]["status"] == "dismissed"


def test_mark_delivered_unknown_rid():
    base = _fresh_base()
    assert mark_delivered(base_dir=base, rid="nope:nope:ffff") is False
