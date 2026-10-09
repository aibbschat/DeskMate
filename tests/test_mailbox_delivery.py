"""信箱 · 送达层（2026-10-09）：准入 / 视图态 / 隐藏往来会话 / REST 契约。

设计单：`reports/信箱_送达层设计单_2026-10-09.md`。
这里钉住四件当初踩过或容易再踩的事：

① `manager.list_sessions()` 返 **dict 列表**——按属性取键（`s.session_id`）会把
   `GET /v1/sessions` 打成 500。第一版就栽在这，而且旧测试因当时会话列表恰好为空、
   推导式一次都没迭代而**假绿**，所以这里必须先在库里放一条普通会话再断言。
② 会话 id 直接当 `conversations/<id>.jsonl` 文件名用，受 `_SAFE_SESSION_ID` 白名单
   约束——带冒号的 `mailbox:<信 id>` 在 `ConversationStore._file()` 就抛，轻回信恒失败。
③ 准入：系统遥测（写库失败/密钥告警…）与意图讨论流不是「一封信」，列表与未读数同口径。
④ 已读信号必须**追加**落盘并真被消费（否则信箱成她的表白墙+坟场，她学到「主动没用」）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from coworker.conversations import is_safe_session_id
from coworker.server import create_app
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from deskmate.proactivity.ledger import (
    acknowledge_ledger,
    archived_letters,
    list_letters,
    mailbox_counts,
    mailbox_evidence,
    record_mailbox_teaching,
    lookup_items,
    mark_letter_read,
    mailbox_feedback_stats,
    record_letter_seen,
    record_proactive,
    render_mailbox_self_context,
    unread_letter_count,
)


def _patch_ledger(base, rid, **cols):
    """直改台账列（测试只造既有态，不新增写路径）。"""
    sets = ", ".join(f"{k}=?" for k in cols)
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        c.execute(f"UPDATE proactive_ledger SET {sets} WHERE id=?", (*cols.values(), rid))
        c.commit()


# -- fixtures -----------------------------------------------------------------


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    """独立的 deskmate 目录（信箱视图态只落本地，Mac-only 不进 oplog）。"""
    base = tmp_path / "deskmate"
    base.mkdir()
    monkeypatch.setenv("DESKMATE_DIR", str(base))
    return base


@pytest.fixture()
def client(tmp_path, desk):
    mgr = SessionManager(data_dir=tmp_path / "data")
    # 先放一条**普通**会话：否则 GET /v1/sessions 的列表推导一次都不执行，
    # 属性/键名取错也不会报错（这颗假绿坑当初让 500 溜过了整个测试网）。
    mgr.session_store.save(
        SessionRecord(
            session_id="normal1",
            workspace=str(tmp_path),
            model="m",
            mode="interactive",
            messages=[{"role": "user", "content": "hi", "ts": 1} ],
        )
    )
    return TestClient(create_app(mgr))


def add(base, kind, content="正文", source="cloud_audit", **kw):
    kw.setdefault("evidence", "依据：昨晚你灯还亮着")
    kw.setdefault("confidence", 0.9)
    return record_proactive(base, source=source, kind=kind, content=content, **kw)


# -- ① REST 形状回归（500 那刀） ------------------------------------------------


def test_sessions_endpoint_survives_with_rows(client):
    r = client.get("/v1/sessions")
    assert r.status_code == 200, r.text
    ids = [s["session_id"] for s in r.json()["sessions"]]
    assert "normal1" in ids


# -- ③ 准入门：遥测与讨论流不入信箱 --------------------------------------------


def test_telemetry_kinds_excluded_but_care_included(desk):
    add(desk, "emotional_care", "看你昨晚灯还亮着")
    add(desk, "memory_write_error", "写库失败 stack trace")
    add(desk, "api_key_exposure_alert", "疑似密钥外泄")
    add(desk, "some_future_error", "以后新增的遥测也应默认被拦")
    kinds = [it["kind"] for it in list_letters(desk)]
    assert kinds == ["emotional_care"]
    assert unread_letter_count(desk) == 1  # 红点与列表同口径，不被日志灌爆


def test_intent_discussion_rows_not_duplicated_into_mailbox(desk):
    rid = add(desk, "opening", "我打算先查一查史料", source="intent_discuss")
    _patch_ledger(desk, rid, origin="intent_handoff")
    assert list_letters(desk) == []


def test_empty_content_guard_and_failed_marker_softened(desk):
    add(desk, "emotional_care", "   ")  # 空信守卫：递出去就是一张空信
    rid = add(desk, "idea_capture", "第三章换个收法")
    _patch_ledger(desk, rid, understanding="[understanding_failed]")
    rows = list_letters(desk)
    assert [it["id"] for it in rows] == [rid]
    assert "[understanding_failed]" not in rows[0]["understanding"]  # 工程标记不外露


def test_extra_deny_kinds_via_env(desk, monkeypatch):
    add(desk, "emotional_care", "A")
    add(desk, "plot_contradiction", "B")
    monkeypatch.setenv("DESKMATE_MAILBOX_DENY_KINDS", "plot_contradiction")
    assert [it["kind"] for it in list_letters(desk)] == ["emotional_care"]


# -- ④ 已读：视图态 + 追加式信号 + 回流文案 ------------------------------------


def test_mark_read_sets_read_at_and_clears_unread(desk):
    rid = add(desk, "emotional_care", "看你昨晚灯还亮着")
    assert unread_letter_count(desk) == 1
    assert mark_letter_read(base_dir=desk, rid=rid) is True
    assert unread_letter_count(desk) == 0
    assert list_letters(desk)[0]["read_at"]
    # 已读 ≠ 处理：status 不该被标已读动过
    assert list_letters(desk)[0]["status"] == "pending"


def test_seen_signal_appends(desk):
    for rid in ("x:1", "x:2", "x:3"):
        record_letter_seen(base_dir=desk, rid=rid)
    lines = (desk / "mailbox_seen.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3  # 覆盖式写法曾把前两条吃掉
    assert json.loads(lines[-1])["id"] == "x:3"


# -- ⑤ 沉淀进教学层（P3 teachings）：她长期记得「萧盛会看我的信」 ----------------


def _mailbox_lessons(desk):
    from deskmate.cognitive.store import CognitiveStore

    with CognitiveStore(db_path=desk / "deskmate.db")._connect() as c:
        return c.execute(
            "SELECT content, scope, source FROM teachings WHERE source='mailbox'"
        ).fetchall()


def test_reading_enough_letters_teaches_her_permanently(desk):
    """每轮提示只是一句旁白；读满 3 封才沉淀成 permanent 教训（不做无据的认知）。"""
    ids = [add(desk, "emotional_care", f"第{i}封") for i in range(3)]
    assert _mailbox_lessons(desk) == []  # 一封都没读 → 不下结论
    mark_letter_read(base_dir=desk, rid=ids[0])
    assert _mailbox_lessons(desk) == []  # 只读 1 封，还不够
    mark_letter_read(base_dir=desk, rid=ids[1])
    mark_letter_read(base_dir=desk, rid=ids[2])
    rows = _mailbox_lessons(desk)
    assert len(rows) == 1 and rows[0][1] == "permanent"
    assert "会读" in rows[0][0]


def test_teaching_upgrades_in_place_and_never_duplicates(desk):
    """证据升级 → 同一个 source 只留一条（不越学越啰嗦，不挤爆注入预算）。"""
    ids = [add(desk, "emotional_care", f"信{i}", confidence=0.9) for i in range(4)]
    for rid in ids[:3]:
        mark_letter_read(base_dir=desk, rid=rid)
    assert len(_mailbox_lessons(desk)) == 1
    # 他动手处理了一封 → 内容升级成「会读也会处理」，但**仍只有一条**
    acknowledge_ledger(base_dir=desk, rid=ids[0], new_status="actioned")
    rows = _mailbox_lessons(desk)
    assert len(rows) == 1
    assert "动手处理" in rows[0][0]
    # 再多读几封也不会长出第二条
    mark_letter_read(base_dir=desk, rid=ids[3])
    assert len(_mailbox_lessons(desk)) == 1


def test_permanent_mailbox_lesson_reaches_injection(desk):
    """沉淀的那条必须真被每轮注入吃到（get_lessons_for_injection 取 permanent）。"""
    from deskmate.cognitive.store import CognitiveStore

    ids = [add(desk, "idea_capture", f"灵感{i}") for i in range(3)]
    for rid in ids:
        mark_letter_read(base_dir=desk, rid=rid)
    lessons = CognitiveStore(db_path=desk / "deskmate.db").get_lessons_for_injection()
    assert any("信箱" in t for t in lessons)


def test_she_is_told_the_mailbox_exists(desk):
    """信箱自述是**常驻**的：不能等他读了才提，那样她拿到的永远是半句话
    （知道有人读了，不知道读的是个什么地方）——她知道自己有日记、有花园，
    信箱也得是同一个级别的物件。"""
    # 一封都没递过 → 不凭空造物件
    assert render_mailbox_self_context(desk) == ""
    rid = add(desk, "emotional_care", "看你昨晚灯还亮着")
    ctx = render_mailbox_self_context(desk)
    assert "<mailbox>" in ctx
    assert "信箱" in ctx and "稍后" in ctx and "归档" in ctx
    assert "读掉 0 封" in ctx  # 还没人读过
    # 他读过之后，报的是**窗口内**的数（方案 B：不喂存量）。
    # 走 mark_letter_read 真路径：它同时落 read_at（窗口证据）与 seen（今天读了几个）。
    mark_letter_read(base_dir=desk, rid=rid)
    ctx2 = render_mailbox_self_context(desk)
    assert "今天读了 1 封" in ctx2 and "读掉 1 封" in ctx2
    # 已读≠处理：seen 落盘 + 统计口径
    stats = mailbox_feedback_stats(desk)
    assert stats["seen_today"] == 1 and stats["replied_today"] == 0


def test_counts_are_rolling_not_cumulative(desk):
    """方案 B：给她的数字按滚动窗口算。库里堆多少是界面的事，不喂给她。"""
    a = add(desk, "emotional_care", "最近的一封")
    b = add(desk, "emotional_care", "两个月前的一封")
    mark_letter_read(base_dir=desk, rid=a)
    _patch_ledger(desk, b, read_at="2020-01-01T00:00:00")
    assert mailbox_evidence(desk) == {"read": 1, "handled": 0, "window_days": 30}
    # 界面口径不受窗口影响（列表不封顶，两封都还在已读栏里）
    assert mailbox_counts(desk)["read"] == 2
    # 不限窗口时才等于累计
    assert mailbox_evidence(desk, window_days=0)["read"] == 2


def test_lesson_never_downgrades(desk):
    """窗口是滚动的：这个月没动手不等于从没动过手——不许把「会读也会处理」退回「会读」。"""
    ids = [add(desk, "emotional_care", f"信{i}") for i in range(3)]
    for rid in ids:
        mark_letter_read(base_dir=desk, rid=rid)
    acknowledge_ledger(base_dir=desk, rid=ids[0], new_status="actioned")
    assert "动手处理" in _mailbox_lessons(desk)[0][0]
    # 把处理与阅读的痕迹都推到窗口之外（两个月前）
    old = "2020-01-01T00:00:00"
    for rid in ids:
        _patch_ledger(desk, rid, read_at=old, acknowledged_at=old)
    assert mailbox_evidence(desk)["handled"] == 0  # 窗口内确实没证据了
    record_mailbox_teaching(base_dir=desk)
    assert "动手处理" in _mailbox_lessons(desk)[0][0]  # 但教训不降级


def test_lookup_reaches_archived_letters(desk):
    """她回查旧信不用挑状态：归档里的（dismissed）也查得到。"""
    rid = add(desk, "idea_capture", "他一辈子都在替别人记账")
    acknowledge_ledger(base_dir=desk, rid=rid, new_status="dismissed")
    hits = lookup_items(desk, handle=_short_of(rid))
    assert [it["id"] for it in hits] == [rid]
    assert hits[0]["status"] == "dismissed"


def _short_of(rid: str) -> str:
    """#句柄 = 指纹段前几位（与 ledger._short_id 同口径）。"""
    return rid.rsplit(":", 1)[-1][:6]


def test_stale_shelf_after_seven_days(desk):
    rid = add(desk, "emotional_care", "早")
    assert list_letters(desk)[0]["stale"] is False
    _patch_ledger(desk, rid, read_at="2020-01-01T00:00:00")
    assert list_letters(desk)[0]["stale"] is True  # 已搁置，但不删、仍在列表里


# -- ② 轻回信：隐藏往来会话（charset-safe、幂等、不进侧栏、可提升） ------------


def test_reply_creates_charset_safe_hidden_session(client, desk):
    letter = "mobile_voice:idea_capture:abcd"  # 带冒号的信 id
    r = client.post(f"/v1/deskmate/mailbox/{letter}/reply")
    body = r.json()
    assert body["ok"] is True, body
    sid = body["session_id"]
    assert is_safe_session_id(sid) is True  # 否则 save() 在 _file() 就抛
    assert sid.startswith("mailbox-")
    # 幂等：同一封信永远同一个会话
    assert client.post(f"/v1/deskmate/mailbox/{letter}/reply").json()["session_id"] == sid
    # 隐藏：不进侧栏
    assert sid not in [s["session_id"] for s in client.get("/v1/sessions").json()["sessions"]]


def test_promote_surfaces_thread_and_mailbox_remembers(client, desk):
    letter = add(desk, "emotional_care", "看你昨晚灯还亮着")
    sid = client.post(f"/v1/deskmate/mailbox/{letter}/reply").json()["session_id"]
    assert client.post(f"/v1/deskmate/mailbox/{letter}/promote").json()["ok"] is True
    ids = [s["session_id"] for s in client.get("/v1/sessions").json()["sessions"]]
    assert sid in ids  # 提升后进侧栏，气泡续聊
    item = next(
        it for it in client.get("/v1/deskmate/mailbox").json()["items"] if it["id"] == letter
    )
    assert item["thread_promoted"] is True
    assert item["mailbox_session_id"] == sid  # 信箱留「看对话」引用


def test_mailbox_list_shape_and_unread_filter(client, desk):
    a = add(desk, "emotional_care", "第一封")
    b = add(desk, "idea_capture", "第二封")
    add(desk, "memory_write_error", "日志")  # 遥测：不进列表也不占红点
    view = client.get("/v1/deskmate/mailbox").json()
    assert view["ok"] is True
    assert view["unread_count"] == 2
    assert {it["id"] for it in view["items"]} == {a, b}
    first = view["items"][0]
    assert {"id", "content", "read", "read_at", "stale", "mailbox_session_id",
            "thread_promoted"} <= set(first)
    assert first["read"] is False and first["stale"] is False
    mark_letter_read(base_dir=desk, rid=a)
    unread = client.get("/v1/deskmate/mailbox?status=unread").json()
    assert [it["id"] for it in unread["items"]] == [b]
    assert unread["unread_count"] == 1


def test_snooze_and_dismiss_land_in_archive(client, desk):
    """点了「稍后/不用了」的信不能蒸发——必须能在归档里回查（萧哥 10-09 追问）。"""
    a = add(desk, "emotional_care", "稍后说")
    b = add(desk, "idea_capture", "不用了")
    assert client.post(f"/v1/deskmate/mailbox/{a}/snooze").json()["ok"] is True
    assert client.post(f"/v1/deskmate/mailbox/{b}/dismiss").json()["ok"] is True
    # 收件箱清空
    assert client.get("/v1/deskmate/mailbox").json()["items"] == []
    # 但归档里两封都在，且带得上架原因
    arch = client.get("/v1/deskmate/mailbox?status=archived").json()
    assert {it["id"] for it in arch["items"]} == {a, b}
    reasons = {it["id"]: it["shelf_reason"] for it in arch["items"]}
    assert reasons == {a: "snoozed", b: "dismissed"}
    # 左栏三档计数：收件箱 0（2 封都在归档），未读/已读/归档各归各
    counts = client.get("/v1/deskmate/mailbox").json()["counts"]
    assert counts == {"unread": 0, "read": 0, "archived": 2}


def test_snoozed_letter_returns_to_inbox_after_backoff(desk):
    """退避到期后它自己回到收件箱（现有 3 天口径），归档只是让它这段时间可见。"""
    a = add(desk, "emotional_care", "过几天再说")
    with sqlite3.connect(str(desk / "deskmate.db")) as c:
        c.execute(
            "UPDATE proactive_ledger SET status='snoozed',"
            " snoozed_until='2020-01-01T00:00:00' WHERE id=?",
            (a,),
        )
        c.commit()
    assert [it["id"] for it in list_letters(desk)] == [a]  # 已到期 → 回收件箱
    assert archived_letters(desk) == []  # 不再算归档
