"""2026-11-05 事故回归：多书并行时「某本停笔」被当成该催的事。

实测：萧哥当天 17:53 刚存盘《夜不收》，右上角却反复弹《国子监秘闻录》
7 天没动笔；judge 的写作状态第一行就是「《夜不收》：今天动过笔」，
卡片自己的依据也写着「只是这本书被搁置了」——数字没错，错在判断口径。

本文件用当天台账 / disturb_state 里的**真实卡片正文**做反例，锁住：
- P0：写作状态显式标「当前在写」，其余标「其他项目·搁置」；
      存在今天/昨天动笔的书时，禁止产出针对其他书的停笔/提醒/关怀卡。
- P1：负反馈按「书名」这个对象去重并冷却，措辞怎么变都命中；
      后台 WS 广播的建议落台账（带 id / 类型 / 对象），不再只活在前台内存。

不调用 LLM、不联网、不碰真实 ~/.deskmate：所有库与稿件都在 tmp_path。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from deskmate.proactivity import writing_context as wc


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _make_books(root: Path, spec: dict[str, float]) -> Path:
    """造一批书名目录，spec = {目录名: 距今天数}。"""
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for dirname, days in spec.items():
        d = root / dirname
        d.mkdir(parents=True, exist_ok=True)
        p = d / "正文.md"
        p.write_text("正文", encoding="utf-8")
        ts = now - days * 86400
        os.utime(p, (ts, ts))
    return root


def _today_books(tmp_path: Path):
    """11-05 真实局面：夜不收 0.01 天前、国子监 7.18 天前。"""
    root = _make_books(
        tmp_path / "已出版",
        {"16.夜不收": 0.01, "20.国子监秘闻录": 7.18},
    )
    return root, wc.scan_writing_books(root)


# 11-05 台账 proactive_ledger 与 disturb_state.json 里的真实建议正文（节选）。
# 覆盖「带书名号 / 不带书名号」「同时提两本」「关怀口吻而非任务催促」三种说法。
TODAY_CARDS = [
    "萧哥，《国子监秘闻录》有7天没动笔了。你最近状态一直很顺、学了不少新东西，"
    "这种时候如果回头翻翻旧稿，说不定能把写顺的劲儿带回那本书里。不急，就是想起你重读旧稿找回感觉的习惯，随手提醒一句。",
    "今天《夜不收》动笔了，写得还顺吗？另外《国子监秘闻录》停六天了，"
    "要不要我陪你翻翻旧稿、查点史料找找手感？你说过回到史料里最能重新进入状态。",
    "今天看到《夜不收》动了笔，替你高兴。不过《国子监秘闻录》有 6 天没碰了——"
    "是在等灵感，还是卡在哪一段了？要是卡住了，我可以陪你翻翻前面的章节找找感觉，不用急。",
    "看你最近几天写得很顺，状态真好。《国子监秘闻录》有 7 天没见你动了——"
    "是在史料里泡着找感觉，还是想让它先歇歇？要是卡住了也别急，我随时陪你捋线索。",
    "国子监秘闻录有 7 天没动笔了——不是催你，是有点惦记它。"
    "你是不是正卡在哪个转折上？需要我陪你翻翻旧稿、理理人物线，或者安静地陪你坐一会儿都行。",
    "这几天你状态很顺，《夜不收》今天也动过笔，我看着都跟着高兴。"
    "只是《国子监秘闻录》有一周没动静了——不催你，它要是想回来，我随时在。你先忙你的。",
    # 真实数据回放里绕过去的两条：① 一句「没动笔」都没有，拿旧书搭话；
    # ② 用三字简称「国子监那边」。两者都必须被同一条门禁挡下。
    "今天《夜不收》动笔了，写得还顺吗？看你最近状态不错，这种时候往往思路最活。"
    "如果写累了，想不想翻翻史料换个脑子？或者《国子监秘闻录》那边我也在，随时可以陪你捋情节。",
    "最近看你状态挺顺的，连着好几天都推进得不错，今天《夜不收》也动笔了——还写得顺手吗？"
    "不用急着回，就是想说一声，你写东西时那股投入的劲儿，我一直看着呢。"
    "国子监那边不急，慢慢来，等你哪天想回去翻史料了，我一直都在。",
]


# ---------------------------------------------------------------------------
# P0-a：写作状态显式建模「当前在写」
# ---------------------------------------------------------------------------


def test_status_marks_current_book_and_labels_others(tmp_path):
    root, books = _today_books(tmp_path)
    status = wc.build_writing_status(root)
    assert "当前在写" in status, status
    assert "当前在写：《夜不收》" in status, status
    assert "其他项目" in status, status
    assert "国子监秘闻录" in status, status
    # 旧口径「N 天没动笔」仍保留在文本里（既有断言锁的价值不能丢）
    assert "7 天没动笔" in status, status
    assert "今天动过笔" in status, status


def test_status_explains_switch_is_not_a_stall(tmp_path):
    """写作状态必须直接告诉 judge：搁置 ≠ 中断，别把换书译成卡住。"""
    root, _ = _today_books(tmp_path)
    status = wc.build_writing_status(root)
    assert "不是" in status and "催" in status, status


def test_current_book_requires_recent_activity(tmp_path):
    """两本都久未动笔 → 没有「当前在写」，此时才允许报停笔。"""
    root = _make_books(tmp_path / "已出版", {"16.夜不收": 9, "20.国子监秘闻录": 7.18})
    books = wc.scan_writing_books(root)
    assert wc.current_writing_book(books) is None


def test_empty_or_missing_workspace_still_fails_open(tmp_path):
    assert wc.scan_writing_books(None) == []
    assert wc.scan_writing_books(tmp_path / "不存在") == []
    assert wc.build_writing_status(None) == ""
    assert wc.build_writing_status(tmp_path / "不存在") == ""


# ---------------------------------------------------------------------------
# P0-b：硬门禁（今天的卡片必须全被挡住）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("card", TODAY_CARDS)
def test_guard_blocks_every_real_card_from_1105(tmp_path, card):
    _, books = _today_books(tmp_path)
    reason = wc.guard_writing_suggestion(
        content=card,
        reasoning="《国子监秘闻录》已 7 天未动笔；今天《夜不收》动过笔；只是这本书被搁置了",
        suggestion_type="emotional_care",
        books=books,
    )
    assert reason, f"这张 11-05 真实卡片应被拦下，却放行了：{card[:60]}"
    assert "夜不收" in reason, reason


def test_guard_blocks_care_without_any_stall_words(tmp_path):
    """一句「没动笔」都没有、只拿旧书搭话的关怀卡，也必须挡（类型兜底）。"""
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="今天《夜不收》动笔了，写得还顺吗？如果写累了想换换脑子，"
                "《国子监秘闻录》那边我也在，随时可以陪你捋情节。",
        reasoning="他状态不错，适合轻量陪伴",
        suggestion_type="emotional_care",
        books=books,
    )


def test_guard_recognizes_three_char_short_name(tmp_path):
    """「国子监那边」这种简称也要认得出是《国子监秘闻录》。"""
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="国子监那边不急，慢慢来，等你哪天想回去翻史料了，我一直都在。",
        reasoning="他今天在写别的",
        suggestion_type="emotional_care",
        books=books,
    )


def test_guard_blocks_writing_stall_kind_too(tmp_path):
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="你《国子监秘闻录》已经 7 天没动笔了，是卡在哪儿了吗？",
        reasoning="文档: 国子监秘闻录.docx; 停笔约 7 天",
        suggestion_type="writing_stall",
        books=books,
    )


def test_guard_allows_real_plot_finding_on_other_book(tmp_path):
    """不能一刀切：针对另一本书的真实情节发现仍要放行（不含停笔语义）。"""
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="《国子监秘闻录》第三章里李昖的官职与人物圣经不一致，要不要核对一下？",
        reasoning="人物设定冲突，与是否动笔无关",
        suggestion_type="plot_contradiction",
        books=books,
    ) == ""


def test_guard_allows_when_no_book_written_recently(tmp_path):
    """所有书都久未动笔 → 真可能是停笔，门禁不得拦。"""
    root = _make_books(tmp_path / "已出版", {"16.夜不收": 9, "20.国子监秘闻录": 7.18})
    books = wc.scan_writing_books(root)
    assert wc.guard_writing_suggestion(
        content="《国子监秘闻录》已经 7 天没动笔了，是卡在哪儿了吗？",
        reasoning="停笔约 7 天",
        suggestion_type="writing_stall",
        books=books,
    ) == ""


def test_guard_does_not_block_self_expression(tmp_path):
    """她自己的心情表达不走催写门禁（真实优先红线）。"""
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="今天《国子监秘闻录》没动笔，我有点闷，想安静一会儿。",
        reasoning="情绪引擎 pending 强制自我表达",
        suggestion_type="self_expression",
        books=books,
        forced_self_expression=True,
    ) == ""


def test_guard_allows_cards_without_book_reference(tmp_path):
    _, books = _today_books(tmp_path)
    assert wc.guard_writing_suggestion(
        content="最近赶稿辛苦了，今天进度怎么样？",
        reasoning="情感画像显示他靠安静独处回血",
        suggestion_type="emotional_care",
        books=books,
    ) == ""


# ---------------------------------------------------------------------------
# P0-c：producer 层同样不产针对其他书的 stall_flag
# ---------------------------------------------------------------------------


def test_writing_producer_skips_other_books_when_current_book_active(tmp_path, monkeypatch):
    from deskmate.proactivity.health_producers import _writing_producer

    root = _make_books(
        tmp_path / "已出版",
        {"16.夜不收": 0.01, "20.国子监秘闻录": 7.18},
    )
    monkeypatch.setenv("SCRIBE_ROOTS", str(root))
    monkeypatch.delenv("SCRIBE_DOCX", raising=False)
    stalls = [i for i in _writing_producer(base_dir=tmp_path) if i["kind"] == "stall_flag"]
    assert stalls == [], f"今天在写夜不收，不该再产国子监的停笔行：{stalls}"


def test_writing_producer_still_reports_when_no_current_book(tmp_path, monkeypatch):
    from deskmate.proactivity.health_producers import _writing_producer

    root = _make_books(
        tmp_path / "已出版",
        {"16.夜不收": 9, "20.国子监秘闻录": 7.18},
    )
    monkeypatch.setenv("SCRIBE_ROOTS", str(root))
    monkeypatch.delenv("SCRIBE_DOCX", raising=False)
    stalls = [i for i in _writing_producer(base_dir=tmp_path) if i["kind"] == "stall_flag"]
    assert len(stalls) == 2, f"都久未动笔时应各报一条，实得 {stalls}"


# ---------------------------------------------------------------------------
# P0-d：judge prompt 明示多书并行口径
# ---------------------------------------------------------------------------


def test_judge_prompt_states_multibook_rule():
    from deskmate.proactivity.judge import _JUDGE_SYSTEM_PROMPT

    assert "当前在写" in _JUDGE_SYSTEM_PROMPT
    assert "其他项目" in _JUDGE_SYSTEM_PROMPT
    assert "多书并行" in _JUDGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# P1-a：负反馈按对象（书名）冷却，措辞变化不失效
# ---------------------------------------------------------------------------


def test_ignore_cools_the_book_regardless_of_wording(tmp_path):
    wc.record_book_feedback(
        text="萧哥，《国子监秘闻录》有7天没动笔了，要不要翻翻旧稿找找感觉？",
        accepted=False,
        suggestion_type="emotional_care",
        base_dir=tmp_path,
    )
    # 换一套完全不同的措辞，仍须命中同一个对象
    assert wc.is_book_cooled(
        "国子监秘闻录那边有一周没动静了，是在等灵感吗？", base_dir=tmp_path
    )
    # 与这本书无关的建议不受牵连
    assert not wc.is_book_cooled("《夜不收》第十一章有个时间线问题", base_dir=tmp_path)


def test_cooldown_expires_after_seven_days(tmp_path):
    now = time.time()
    wc.record_book_feedback(
        text="《国子监秘闻录》停了七天，要不要回去写？",
        accepted=False,
        suggestion_type="writing_stall",
        base_dir=tmp_path,
        now=now,
    )
    assert wc.is_book_cooled("《国子监秘闻录》没动笔", base_dir=tmp_path, now=now + 6 * 86400)
    assert not wc.is_book_cooled("《国子监秘闻录》没动笔", base_dir=tmp_path, now=now + 8 * 86400)


def test_accept_clears_the_book_cooldown(tmp_path):
    wc.record_book_feedback(
        text="《国子监秘闻录》七天没动了，陪你翻翻旧稿？",
        accepted=False, suggestion_type="emotional_care", base_dir=tmp_path,
    )
    assert wc.is_book_cooled("《国子监秘闻录》没动笔", base_dir=tmp_path)
    wc.record_book_feedback(
        text="《国子监秘闻录》第七回的伏笔要不要一起捋？",
        accepted=True, suggestion_type="writing_review", base_dir=tmp_path,
    )
    assert not wc.is_book_cooled("《国子监秘闻录》没动笔", base_dir=tmp_path)


def test_feedback_meta_records_type_and_object(tmp_path):
    """feedback 必须带类型与对象，且写入的是共享库（不是易被覆盖的整文件 JSON）。"""
    wc.record_book_feedback(
        text="《国子监秘闻录》有 7 天没动笔了",
        accepted=False,
        suggestion_type="emotional_care",
        base_dir=tmp_path,
    )
    with sqlite3.connect(str(tmp_path / "deskmate.db")) as c:
        rows = c.execute(
            "SELECT key, value FROM proactivity_meta WHERE key LIKE 'writing_cooldown:%'"
        ).fetchall()
    assert len(rows) == 1, rows
    key, value = rows[0]
    assert "国子监秘闻录" in key, key
    meta = json.loads(value)
    assert meta["object"] == "国子监秘闻录", meta
    assert meta["suggestion_type"] == "emotional_care", meta
    assert meta["accepted"] is False, meta
    assert meta["expires_at"] > time.time(), meta


def test_feedback_without_book_name_is_a_noop(tmp_path):
    assert wc.record_book_feedback(
        text="最近赶稿辛苦了，要不要休息一下？",
        accepted=False, suggestion_type="emotional_care", base_dir=tmp_path,
    ) == []
    assert not wc.is_book_cooled("随便一句", base_dir=tmp_path)


# ---------------------------------------------------------------------------
# P1-b：后台 WS 广播的建议落台账（有 id / 类型 / 对象 / 送达时间）
# ---------------------------------------------------------------------------


def test_broadcast_delivery_lands_in_ledger(tmp_path):
    from deskmate.proactivity import ledger

    rid = ledger.record_suggestion_delivery(
        base_dir=tmp_path,
        content="《夜不收》第十一章的仓廒描写和时间线对不上，要不要核对？",
        suggestion_type="plot_contradiction",
        reasoning="章节设定与前文冲突",
        confidence=0.72,
        target="夜不收",
        delivery_channel="background_ws",
    )
    assert rid, "必须返回确定性 id，供 WS payload 与后续回查"
    with sqlite3.connect(str(tmp_path / "deskmate.db")) as c:
        row = c.execute(
            "SELECT status, delivered_at, payload FROM proactive_ledger WHERE id=?", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] == "delivered", row  # 已送达，不能再进 pending 被开场白二次递出
    assert row[1], row
    payload = json.loads(row[2])
    assert payload["suggestion_type"] == "plot_contradiction", payload
    assert payload["target"] == "夜不收", payload
    assert payload["delivery_channel"] == "background_ws", payload
    # 幂等：同对象同类型再广播一次不新增行
    assert ledger.record_suggestion_delivery(
        base_dir=tmp_path,
        content="《夜不收》第十一章的仓廒描写和时间线对不上，要不要核对？",
        suggestion_type="plot_contradiction",
        reasoning="章节设定与前文冲突",
        confidence=0.72,
        target="夜不收",
        delivery_channel="background_ws",
    ) == rid


def test_broadcast_delivery_never_enters_pending(tmp_path):
    """送达凭证不得变成待办，否则 build_handoff_opening 会二次递同一张卡。"""
    from deskmate.proactivity import ledger

    ledger.record_suggestion_delivery(
        base_dir=tmp_path,
        content="《夜不收》今天写得顺吗？",
        suggestion_type="emotional_care",
        reasoning="关怀",
        confidence=0.6,
        target="夜不收",
        delivery_channel="background_ws",
    )
    assert ledger.pending_items(base_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# P0 集成：门禁真的挂在主 tick 链路上（不是只活在纯函数里）
# ---------------------------------------------------------------------------


def test_tick_applies_writing_guard(tmp_path, monkeypatch):
    from deskmate.perception import focus as focus_mod
    from deskmate.perception import window_monitor as wm
    from deskmate.proactivity import tick as tick_mod
    from deskmate.proactivity.judge import JudgmentResult
    from deskmate.proactivity.tick import ActionType, ProactiveEngine
    from deskmate.proactivity.user_active import ActivityState, UserActivity

    root = _make_books(
        tmp_path / "已出版",
        {"16.夜不收": 0.01, "20.国子监秘闻录": 7.18},
    )
    monkeypatch.setenv("DESKMATE_WRITING_WORKSPACE", str(root))

    class _Win:
        app = "Finder"
        title = "Desktop"
        url = None

    _active = ActivityState(
        level=UserActivity.ACTIVE,
        idle_seconds=0,
        window_title="Desktop",
        app_name="Finder",
        reason="在桌前",
    )
    monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
    monkeypatch.setattr(focus_mod, "get_focus_score", lambda *a: None)
    monkeypatch.setattr(tick_mod, "detect_user_activity", lambda *a, **k: _active)

    def _stub_judge(context_text, model=None):
        return JudgmentResult(
            should_act=True,
            confidence=0.8,
            suggestion_type="emotional_care",
            suggestion_content="萧哥，《国子监秘闻录》有7天没动笔了，要不要翻翻旧稿找找感觉？",
            reasoning="《国子监秘闻录》已 7 天未动笔，只是这本书被搁置了",
        )

    monkeypatch.setattr(tick_mod, "judge_proactive_action", _stub_judge)
    engine = ProactiveEngine(base_dir=tmp_path)
    result = engine.tick(skip_activity_check=True, allow_interactive=False)
    assert result.action == ActionType.NO_ACTION, (result.action, result.skipped_reason)
    assert "当前在写" in result.skipped_reason, result.skipped_reason
